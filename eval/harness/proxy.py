"""The loopback reverse proxy every routed model request passes through, so the envelope bounds it.

A routed session's CLI calls `http://127.0.0.1:<port>/s/<token>/<route>/...`; the token names the
job and attempt it belongs to. For each request, in order:
1. the route's policy (`route_config`), an allowlist: an exact endpoint path, an exact model, only
   the top-level fields and tool types the route allows, text and tool content only, and an output
   limit where the route requires one; then the credential, which every credential header must
   carry identically, must be the route's key. Anything else is a 403 with no upstream call;
2. admission through the envelope (a 429, and a refusal stuck to the attempt, otherwise);
3. forwarding unchanged, except that the credential headers carry exactly the checked key and the
   response is asked for unencoded, so its billing id can be read from the parsed events (at the
   settler's fixed paths) as the bytes stream back as they came;
4. the request stays unresolved until its settler reports the billed charge or that nothing was
   billed. A failed lookup, a 404, an error inside a started stream, no billing id, or two
   different ones all keep its worst case counted. A metered route's charge is the usage its
   response reported, recorded event by event on the request; one whose response never completed
   with valid usage stays at its worst case.

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
from . import route_config
from . import settlers as settlers_mod

RECONCILE_SECONDS = 240
SETTLE_SECONDS = 5
SHUTDOWN_SECONDS = 60
_JSON_LIMIT = 16 * 1024 * 1024
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
               "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}
_CREDENTIAL_HEADERS = {"authorization", "x-api-key"}


@dataclass
class _Owner:
    job: str
    attempt: str


class Proxy:
    """`with Proxy(env) as proxy:` serves on an ephemeral loopback port; `session(job, attempt)` gives
    a session's base URL per route. `settler_for(upstream, key_fingerprint)` builds the settler of one
    billing scope, or raises `settlers.NoSettler`."""

    def __init__(self, env: envelope_mod.Envelope, *, settler_for=settlers_mod.from_upstream,
                 settle_seconds=SETTLE_SECONDS, reconcile_seconds=RECONCILE_SECONDS,
                 shutdown_seconds=SHUTDOWN_SECONDS, warn=None):
        self.env, self._settler_for = env, settler_for
        self._warn = warn or (lambda message: print(f"WARN proxy: {message}", file=sys.stderr, flush=True))
        self.routes = frozenset(env.route_names())
        self._settlers: dict[tuple[route_config.Upstream, str], settlers_mod.Settler] = {}
        self._policies: dict[str, route_config.Policy | str] = {}  # by route version: parsed once
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
        try:
            self.reconcile_now()
            self._workers = [threading.Thread(target=self._loop, args=(self._settle_seconds, self.settle_now),
                                              daemon=True),
                             threading.Thread(target=self._loop, args=(self._reconcile_seconds, self.reconcile_now),
                                              daemon=True)]
            for worker in self._workers:
                worker.start()
        except BaseException:
            self.__exit__(None, None, None)  # a failed start releases what it took: __exit__ isn't called for it
            raise
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
            try:
                settler = self._settler(pending.upstream, pending.key_fingerprint)
            except settlers_mod.NoSettler as exc:  # it stays counted at its admitted bound
                self._warn(f"request {pending.request}: no settler: {exc}")
                continue
            outcome = settler.settle(pending)
            # Committed only if the request still has exactly this id: the envelope checks it atomically.
            if isinstance(outcome, settlers_mod.Billed):
                self.env.settle_looked_up(pending.request, pending.billing_id, outcome.usd, served=outcome.served)
            elif isinstance(outcome, settlers_mod.NotBilled):
                self.env.settle_looked_up(pending.request, pending.billing_id, None)

    def reconcile_now(self) -> None:
        for scope in self.env.scopes():
            route = next((self.env.route(n) for n in sorted(self.routes) if self.env.route(n).scope == scope), None)
            if route is None:
                continue
            try:
                usage = self._settler(route.upstream, route.key_fingerprint).key_usage()
            except (settlers_mod.UsageUnavailable, settlers_mod.NoSettler) as exc:  # the snapshot expires and
                self._warn(f"scope {scope}: no usage reading: {exc}")              # admission stops
                continue
            self.env.reconcile(scope, usage)  # one reading per key, however many routes share it

    def _settler(self, upstream: dict | None, key_fingerprint: str) -> settlers_mod.Settler:
        """The settler of one billing scope at one upstream, built once."""
        try:
            parsed = route_config.parse_upstream(upstream)
        except ValueError as exc:
            raise settlers_mod.NoSettler(str(exc)) from None
        with self._lock:
            if (parsed, key_fingerprint) not in self._settlers:
                self._settlers[parsed, key_fingerprint] = self._settler_for(parsed, key_fingerprint)
            return self._settlers[parsed, key_fingerprint]

    def _policy(self, route: envelope_mod.Route) -> route_config.Policy | str:
        """The route's parsed policy, or why it doesn't parse; parsed once per route version."""
        with self._lock:
            if route.version not in self._policies:
                try:
                    self._policies[route.version] = route_config.parse_policy(route.policy)
                except ValueError as exc:
                    self._policies[route.version] = str(exc)
            return self._policies[route.version]

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
        policy = self._policy(route)
        if isinstance(policy, str):
            return _reply(handler, 403, f"the route's policy is malformed: {policy}")
        try:
            length = int(handler.headers.get("Content-Length", ""))
        except ValueError:
            return _reply(handler, 411, "a Content-Length is required")
        if not 0 <= length <= policy.max_body_bytes:
            return _reply(handler, 413, f"a body is at most {policy.max_body_bytes} bytes")
        body = handler.rfile.read(length)
        rest = "/" + parts[4]
        problem = route_config.request_problem(policy, rest, body)
        if problem:
            return _reply(handler, 403, problem)
        credential = _credential(handler.headers)
        if credential is None or envelope_mod.fingerprint(credential) != route.key_fingerprint:
            return _reply(handler, 403, "the credential is not this route's key")
        try:
            settler = self._settler(route.upstream, route.key_fingerprint)
        except settlers_mod.NoSettler as exc:  # nothing could settle it: not admitted
            return _reply(handler, 503, f"this route can't be settled: {exc}")
        request_id = secrets.token_hex(16)
        try:
            self.env.admit(request_id, route.name, job=owner.job, attempt=owner.attempt, version=route.version)
        except envelope_mod.Refused as exc:
            return _reply(handler, 429, f"spend refused: {exc}")
        self._forward(handler, route, settler, rest, body, request_id, credential)

    def _forward(self, handler, route: envelope_mod.Route, settler: settlers_mod.Settler, rest: str, body: bytes,
                 request_id: str, credential: str) -> None:
        target = urllib.parse.urlsplit(route_config.parse_upstream(route.upstream).base_url + rest)
        connection_type = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(target.netloc, timeout=600)
        reader = _BillingReader(settler, lambda gen: self.env.record_billing_id(request_id, gen),
                                lambda reading: self.env.record_reading(request_id, **_fields(reading)))
        try:
            sent = {k.lower() for k in handler.headers.keys()}
            headers = {k: v for k, v in handler.headers.items()
                       if k.lower() not in _HOP_BY_HOP and k.lower() not in _CREDENTIAL_HEADERS}
            # Exactly the credential that was checked, under each header the client sent it in.
            if "authorization" in sent:
                headers["Authorization"] = f"Bearer {credential}"
            if "x-api-key" in sent:
                headers["x-api-key"] = credential
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
    billing id the settler finds at its fixed paths (the envelope makes two different ones
    ambiguous), and each event's reading for a metered route."""

    def __init__(self, settler: settlers_mod.Settler, report, report_reading):
        self.settler, self.report, self.report_reading = settler, report, report_reading
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
            reading = self.settler.read(event)
            if not reading.empty:
                self.report_reading(reading)


def _fields(reading: settlers_mod.EventReading) -> dict:
    metered = reading.metered
    return {"metered": metered.usd if isinstance(metered, settlers_mod.Usage) else None,
            "invalid": metered.reason if isinstance(metered, settlers_mod.Invalid) else None,
            "served": reading.served, "complete": reading.complete}


def _credential(headers) -> str | None:
    """The one key a request carries. Clients may send it as both a Bearer token and `x-api-key`;
    every such header must hold the same key, or there is no credential."""
    keys = []
    for auth in headers.get_all("Authorization") or ():
        if not auth.lower().startswith("bearer "):
            return None
        keys.append(auth[7:].strip())
    keys += [value.strip() for value in headers.get_all("x-api-key") or ()]
    return keys[0] if keys and len(set(keys)) == 1 and keys[0] else None


def _reply(handler, status: int, message: str) -> None:
    payload = json.dumps({"error": {"type": "proxy", "message": message}}).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)
