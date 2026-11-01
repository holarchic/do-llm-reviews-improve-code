"""The loopback reverse proxy every routed model request passes through, so the envelope bounds it.

A routed session's CLI calls `http://127.0.0.1:<port>/s/<token>/<route>/...`; the token names the
job and attempt it belongs to. For each request, in order:
1. the route's policy, an allowlist: an exact endpoint path, an exact model, only the top-level
   fields and tool types the route allows, no image input, and an output limit where the route
   requires one; then the credential must be the route's key. Anything else is a 403 with no
   upstream call;
2. admission through the envelope (a 429, and a refusal stuck to the attempt, otherwise);
3. forwarding unchanged, except that the response is asked for unencoded so its billing id can be
   read from the parsed events (at the settler's fixed paths) as the bytes stream back as they came;
4. the request stays unresolved until its settler reports the billed charge or that nothing was
   billed. A failed lookup, a 404, an error inside a started stream, no billing id, or two
   different ones all keep its worst case counted.

Shutdown refuses new requests, drains the handlers in flight and the settlement and
reconciliation workers, and only then lets the envelope's owner close; anything a straggler tries
to write after that is refused by the closed envelope. Headers and bodies are never logged.
"""

import http.client
import http.server
import json
import secrets
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass

from . import envelope as envelope_mod
from . import settlers as settlers_mod

RECONCILE_SECONDS = 240
SETTLE_SECONDS = 5
SHUTDOWN_SECONDS = 60
_JSON_LIMIT = 16 * 1024 * 1024
_IMAGE_PARTS = {"image", "input_image", "image_url"}
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
               "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}


@dataclass(frozen=True)
class Policy:
    """A route's request allowlist. `max_body_bytes` bounds a request's input: a byte-level tokenizer
    makes at least one byte per token, so it is also a bound on the request's input tokens."""
    paths: frozenset[str]
    models: frozenset[str]
    fields: frozenset[str]
    tool_types: frozenset[str]
    max_body_bytes: int
    output_limit: tuple[str, int] | None  # (field, cap) where the provider enforces a requested maximum


def parse_policy(raw) -> Policy:
    """A route's policy record, checked: raises ValueError for anything malformed."""
    def strings(key):
        value = raw.get(key)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"policy {key} must be a list of strings")
        return frozenset(value)
    if not isinstance(raw, dict):
        raise ValueError("a policy is a table")
    size = raw.get("max_body_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("policy max_body_bytes must be a positive integer")
    limit = raw.get("output_limit")
    if limit is not None:
        if not (isinstance(limit, dict) and isinstance(limit.get("field"), str) and isinstance(limit.get("cap"), int)
                and not isinstance(limit.get("cap"), bool) and limit["cap"] > 0):
            raise ValueError("policy output_limit must be {field: str, cap: positive int}")
        limit = (limit["field"], limit["cap"])
    return Policy(strings("paths"), strings("models"), strings("fields"), strings("tool_types"), size, limit)


def policy_problem(policy: Policy, path: str, body: bytes) -> str | None:
    """Why a request falls outside its route's policy, or None. `path` is the part after the route."""
    if path.split("?", 1)[0] not in policy.paths:
        return f"path {path.split('?', 1)[0]!r} is not allowed"
    if len(body) > policy.max_body_bytes:
        return f"the body is over {policy.max_body_bytes} bytes"
    try:
        request = json.loads(body)
    except ValueError:
        return "the body is not JSON"
    if not isinstance(request, dict):
        return "the body is not a JSON object"
    extra = sorted(set(request) - policy.fields)
    if extra:
        return f"field {extra[0]!r} is not allowed"
    if request.get("model") not in policy.models:
        return f"model {request.get('model')!r} is not allowed"
    for tool in request.get("tools") or ():
        if not isinstance(tool, dict) or str(tool.get("type", "")) not in policy.tool_types:
            return f"tool type {tool.get('type') if isinstance(tool, dict) else tool!r} is not allowed"
    if _has_image(request.get("messages")) or _has_image(request.get("input")):
        return "image input is not allowed"
    if policy.output_limit:
        field, cap = policy.output_limit
        value = request.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= cap:
            return f"{field} must be set at or below {cap}"
    return None


def _has_image(node) -> bool:
    if isinstance(node, dict):
        return node.get("type") in _IMAGE_PARTS or any(_has_image(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_image(v) for v in node)
    return False


@dataclass
class _Owner:
    job: str
    attempt: str


class Proxy:
    """`with Proxy(env) as proxy:` serves on an ephemeral loopback port; `session(job, attempt)` gives
    a session's base URL per route. `settler_for` builds a settler from an upstream record."""

    def __init__(self, env: envelope_mod.Envelope, *, settler_for=settlers_mod.from_upstream,
                 settle_seconds=SETTLE_SECONDS, reconcile_seconds=RECONCILE_SECONDS,
                 shutdown_seconds=SHUTDOWN_SECONDS, warn=None):
        self.env, self._settler_for = env, settler_for
        self._warn = warn or (lambda message: print(f"WARN proxy: {message}", file=sys.stderr, flush=True))
        self.routes = frozenset(env.route_names())
        self._settlers: dict[str, settlers_mod.Settler] = {}
        self._owners: dict[str, _Owner] = {}
        self._lock = threading.Lock()
        self._in_flight = 0
        self._drained = threading.Condition(self._lock)
        self._closing = False
        self._stop = threading.Event()
        self._settle_seconds, self._reconcile_seconds = settle_seconds, reconcile_seconds
        self._shutdown_seconds = shutdown_seconds
        self._server = None
        self._workers: list[threading.Thread] = []

    def __enter__(self) -> "Proxy":
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                proxy._serve(self)

            def do_GET(self):
                self.send_error(405)

            def log_message(self, *args):  # headers and bodies are never logged
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.reconcile_now()
        self._workers = [threading.Thread(target=self._loop, args=(self._settle_seconds, self.settle_now), daemon=True),
                         threading.Thread(target=self._loop, args=(self._reconcile_seconds, self.reconcile_now),
                                          daemon=True)]
        for worker in self._workers:
            worker.start()
        return self

    def __exit__(self, *exc) -> None:
        deadline = time.monotonic() + self._shutdown_seconds
        with self._lock:
            if self._closing:
                return  # already shut down: a second exit is a no-op
            self._closing = True  # new requests get a 503
        self._server.shutdown()
        self._server.server_close()
        with self._lock:
            while self._in_flight and time.monotonic() < deadline:
                self._drained.wait(max(0.0, deadline - time.monotonic()))
        # A last round of settlement, on its own thread so a hung lookup can't hold shutdown past the deadline.
        drain = threading.Thread(target=self._drain, args=(deadline,), daemon=True)
        drain.start()
        self._stop.set()
        for thread in (drain, *self._workers):
            thread.join(max(0.0, deadline - time.monotonic()))
        # What is still unresolved stays in the ledger; the owner closes next, and refuses stragglers.

    def _drain(self, deadline: float) -> None:
        while time.monotonic() < deadline and self.env.pending_settlements():
            try:
                self.settle_now()
            except envelope_mod.EnvelopeError:
                return  # ownership ended: what stays unresolved stays counted
            except Exception as exc:  # unexpected: surfaced, and its requests stay counted
                self._warn(f"settlement at shutdown failed: {exc!r}")
                return
            time.sleep(min(self._settle_seconds, 1.0, max(0.0, deadline - time.monotonic())))

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def session(self, job: str, attempt: str) -> dict[str, str]:
        token = secrets.token_hex(12)
        with self._lock:
            self._owners[token] = _Owner(job, attempt)
        return {route: f"http://127.0.0.1:{self.port}/s/{token}/{route}" for route in self.routes}

    # ---- settlement and reconciliation

    def settle_now(self) -> None:
        for pending in self.env.pending_settlements():
            outcome = self._settler(pending.upstream).settle(pending.billing_id)
            # Committed only if the request still has exactly this id: the envelope checks it atomically.
            if isinstance(outcome, settlers_mod.Billed):
                self.env.settle_looked_up(pending.request, pending.billing_id, outcome.usd)
            elif isinstance(outcome, settlers_mod.NotBilled):
                self.env.settle_looked_up(pending.request, pending.billing_id, None)

    def reconcile_now(self) -> None:
        for scope in self.env.scopes():
            route = next((self.env.route(n) for n in sorted(self.routes) if self.env.route(n).scope == scope), None)
            if route is None:
                continue
            try:
                usage = self._settler(route.upstream).key_usage()
            except settlers_mod.UsageUnavailable as exc:  # the snapshot expires and admission stops
                self._warn(f"scope {scope}: no usage reading: {exc}")
                continue
            self.env.reconcile(scope, usage)  # one reading per key, however many routes share it

    def _settler(self, upstream: dict | None) -> settlers_mod.Settler:
        key = json.dumps(upstream, sort_keys=True)
        with self._lock:
            if key not in self._settlers:
                self._settlers[key] = self._settler_for(upstream or {})
            return self._settlers[key]

    def _loop(self, seconds: float, work) -> None:
        while not self._stop.wait(seconds):
            try:
                work()
            except envelope_mod.EnvelopeError:
                return  # ownership ended
            except Exception as exc:  # unexpected: surfaced; unresolved requests keep their worst case
                self._warn(f"{work.__name__} failed: {exc!r}")

    # ---- one request

    def _serve(self, handler) -> None:
        with self._lock:
            if self._closing:
                closing = True
            else:
                closing = False
                self._in_flight += 1
        if closing:
            return _reply(handler, 503, "the proxy is shutting down")
        try:
            self._handle(handler)
        except envelope_mod.EnvelopeError:
            pass  # ownership ended under this request: it stays as the ledger last recorded it
        finally:
            with self._lock:
                self._in_flight -= 1
                self._drained.notify_all()

    def _handle(self, handler) -> None:
        parts = handler.path.split("/", 4)  # "", "s", token, route, rest
        if len(parts) < 5 or parts[1] != "s":
            return _reply(handler, 404, "unknown path")
        with self._lock:
            owner = self._owners.get(parts[2])
        route = self.env.route(parts[3]) if parts[3] in self.routes else None
        if owner is None or route is None:
            return _reply(handler, 403, "unknown session or route")
        try:
            policy = parse_policy(route.policy)
        except ValueError as exc:
            return _reply(handler, 403, f"the route's policy is malformed: {exc}")
        try:
            length = int(handler.headers.get("Content-Length", ""))
        except ValueError:
            return _reply(handler, 411, "a Content-Length is required")
        if not 0 <= length <= policy.max_body_bytes:
            return _reply(handler, 413, f"a body is at most {policy.max_body_bytes} bytes")
        body = handler.rfile.read(length)
        rest = "/" + parts[4]
        problem = policy_problem(policy, rest, body)
        if problem:
            return _reply(handler, 403, problem)
        credential = _credential(handler.headers)
        if credential is None or envelope_mod.fingerprint(credential) != route.key_fingerprint:
            return _reply(handler, 403, "the credential is not this route's key")
        request_id = secrets.token_hex(16)
        try:
            self.env.admit(request_id, route.name, job=owner.job, attempt=owner.attempt, version=route.version)
        except envelope_mod.Refused as exc:
            return _reply(handler, 429, f"spend refused: {exc}")
        self._forward(handler, route, rest, body, request_id)

    def _forward(self, handler, route: envelope_mod.Route, rest: str, body: bytes, request_id: str) -> None:
        target = urllib.parse.urlsplit(str(route.upstream["base_url"]).rstrip("/") + rest)
        connection_type = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(target.netloc, timeout=600)
        reader = _BillingReader(self._settler(route.upstream), lambda gen: self.env.record_billing_id(request_id, gen))
        try:
            headers = {k: v for k, v in handler.headers.items() if k.lower() not in _HOP_BY_HOP}
            headers["Accept-Encoding"] = "identity"  # the billing id must be readable
            connection.request("POST", target.path + (f"?{target.query}" if target.query else ""), body=body,
                               headers=headers)
            response = connection.getresponse()
            reader.content_type = response.getheader("Content-Type", "")
            handler.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in _HOP_BY_HOP:
                    handler.send_header(key, value)
            handler.send_header("Connection", "close")
            handler.end_headers()
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
                reader.feed(chunk)
            reader.finish()
        except (OSError, http.client.HTTPException):
            pass  # an error mid-stream keeps the request unresolved: it may still be billed
        finally:
            connection.close()
            self.env.capture_complete(request_id)  # only now may settlement look its billing id up


class _BillingReader:
    """Parses a response as it streams (SSE `data:` events, or a whole JSON body) and reports each
    billing id the settler finds at its fixed paths; the envelope makes two different ones ambiguous."""

    def __init__(self, settler: settlers_mod.Settler, report):
        self.settler, self.report = settler, report
        self.content_type, self._line, self._body, self._seen = "", b"", b"", set()

    def feed(self, chunk: bytes) -> None:
        if "json" in self.content_type and "event-stream" not in self.content_type:
            if len(self._body) < _JSON_LIMIT:
                self._body += chunk
            return
        self._line += chunk
        *lines, self._line = self._line.split(b"\n")
        for line in lines:
            if line.startswith(b"data:"):
                self._event(line[5:].strip())

    def finish(self) -> None:
        if self._body:
            self._event(self._body)
        elif self._line.startswith(b"data:"):
            self._event(self._line[5:].strip())

    def _event(self, data: bytes) -> None:
        try:
            event = json.loads(data)
        except ValueError:
            return
        if isinstance(event, dict):
            gen = self.settler.billing_id(event)
            if gen and gen not in self._seen:
                self._seen.add(gen)
                self.report(gen)


def _credential(headers) -> str | None:
    auth = headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-api-key")


def _reply(handler, status: int, message: str) -> None:
    payload = json.dumps({"error": {"type": "proxy", "message": message}}).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)
