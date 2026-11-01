"""The loopback reverse proxy every routed model request passes through, so the envelope bounds it.

A routed session's CLI calls `http://127.0.0.1:<port>/s/<token>/<route>/...`; the token names the
job and attempt the session belongs to. For each request the proxy, in order:
1. checks the route's policy (paths, exact models, required output limit; no fallbacks, provider
   overrides, plugins, server-side tools or images) and that the credential is the route's key:
   anything else is a 403 with no upstream call;
2. admits it through the envelope (a 429 and a refusal stuck to the attempt otherwise);
3. forwards it unchanged and streams the response back byte for byte, taking the provider's
   billing id from the response as it passes;
4. leaves the request unresolved until its settler reports the billed charge (or that nothing was
   billed). A lookup that fails, a 404 or an error inside a started stream keeps it pending: none
   of them proves the request was free. A request with no billing id stays unresolved until the
   operator resolves it against the provider's billing.

A reconciliation worker reads each scope's key usage at least every RECONCILE_SECONDS; a failed
read leaves the snapshot to expire, which refuses new requests. Headers and bodies are never
logged or stored.
"""

import hashlib
import http.client
import contextlib
import http.server
import json
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field

from . import envelope as envelope_mod
from . import openrouter, prices

RECONCILE_SECONDS = 240
SETTLE_SECONDS = 5
SHUTDOWN_SECONDS = 60
_FORBIDDEN_KEYS = ("models", "provider", "plugins", "transforms", "route")
_SERVER_TOOL = re.compile(r"web_search|web_fetch|code_execution|computer|file_search", re.I)
_IMAGE_PARTS = {"image", "input_image", "image_url"}
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
               "transfer-encoding", "upgrade", "host", "content-length"}


class RoutingRequired(Exception):
    """A routed session asked to launch with no routing context active: it would bypass the envelope."""


_active: "Proxy | None" = None
_active_lock = threading.Lock()
_job = threading.local()


@contextlib.contextmanager
def active(proxy: "Proxy"):
    """Routed sessions launched inside this block go through `proxy`; outside it they are refused."""
    global _active
    with _active_lock:
        if _active is not None:
            raise RoutingRequired("a routing context is already active")
        _active = proxy
    try:
        yield proxy
    finally:
        with _active_lock:
            _active = None


@contextlib.contextmanager
def job(job_id: str):
    """The job (reservation) this thread's routed sessions belong to."""
    previous = getattr(_job, "value", None)
    _job.value = job_id
    try:
        yield
    finally:
        _job.value = previous


def route_name(provider: str, model: str) -> str:
    """A route's name as it appears in a URL path: one route per provider and model."""
    return f"{provider}--{model.replace('/', '__')}"


def session_base(provider: str, model: str, attempt: str) -> str:
    """The proxy base URL for one attempt's session on this provider and model."""
    with _active_lock:
        proxy = _active
    job_id = getattr(_job, "value", None)
    if proxy is None or job_id is None:
        raise RoutingRequired(f"{provider} {model}: no routing context, so no envelope bounds this session")
    route = route_name(provider, model)
    if route not in proxy.upstreams:
        raise RoutingRequired(f"no route {route} in the active envelope")
    return proxy.session(job_id, attempt)[route]


def refusal(attempt: str) -> str | None:
    """The spend refusal recorded against an attempt, if any."""
    with _active_lock:
        proxy = _active
    return None if proxy is None else proxy.env.refusal(attempt)


def upstreams_from(env: envelope_mod.Envelope) -> dict[str, "Upstream"]:
    """The proxy's upstreams, from the routes the envelope records (every route needs a settler)."""
    built = {}
    state = env.state
    for name, route in state["routes"].items():
        upstream = route.get("upstream") or {}
        if upstream.get("settler") != "openrouter":
            raise RoutingRequired(f"route {name} has no known settler")
        key = openrouter.api_key()
        settler = OpenRouterSettler(upstream["base_url"].rstrip("/") + "/v1", key)
        built[name] = Upstream(upstream["base_url"], settler, route["scope"],
                               state["scopes"][route["scope"]]["fingerprint"])
    return built


@contextlib.contextmanager
def for_envelope(name: str, *, root=None):
    """Own the envelope, serve its proxy and make it the active routing context."""
    with envelope_mod.Envelope.owned(name, root=root) as env, Proxy(env, upstreams_from(env)) as proxy, \
            active(proxy):
        yield env


def fingerprint(credential: str) -> str:
    return hashlib.sha256(credential.encode()).hexdigest()[:16]


def max_charge(rates: prices.Rates, tier: prices.Tier | None, *, max_input_tokens: int, max_output_tokens: int,
               per_request_usd: float = 0.0) -> float:
    """A request's worst case: all its input at the dearest input-side rate the route allows (uncached,
    any cache write, a long-context tier), all its output at the dearest output rate."""
    candidates = [rates] + ([tier.rates] if tier is not None else [])
    input_rate = max(max([r.input] + [p for _, p in r.cache_write]) for r in candidates)
    output_rate = max(r.output for r in candidates)
    return (max_input_tokens * input_rate + max_output_tokens * output_rate) / 1e6 + per_request_usd


def policy_problem(policy: dict, path: str, body: bytes) -> str | None:
    """Why a request falls outside its route's policy, or None. `path` is the part after the route."""
    if not any(path.split("?")[0].endswith(allowed) for allowed in policy.get("paths", ())):
        return f"path {path!r} is not allowed"
    try:
        request = json.loads(body)
    except ValueError:
        return "the body is not JSON"
    if not isinstance(request, dict):
        return "the body is not a JSON object"
    if request.get("model") not in policy.get("models", ()):
        return f"model {request.get('model')!r} is not allowed"
    for key in _FORBIDDEN_KEYS:
        if key in request:
            return f"{key!r} is not allowed"
    for tool in request.get("tools") or ():
        if not isinstance(tool, dict) or _SERVER_TOOL.search(str(tool.get("type", ""))):
            return "a server-side tool is not allowed"
    if _has_image(request.get("messages")) or _has_image(request.get("input")):
        return "image input is not allowed"
    limit = policy.get("output_limit")  # {"field": "max_tokens", "cap": 64000}: the provider enforces it
    if limit:
        value = request.get(limit["field"])
        if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= limit["cap"]:
            return f"{limit['field']} must be set at or below {limit['cap']}"
    return None


def _has_image(node) -> bool:
    if isinstance(node, dict):
        return node.get("type") in _IMAGE_PARTS or any(_has_image(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_image(v) for v in node)
    return False


class Settler:
    """How one provider reports billing. `billing_id` scans response bytes; `settle` returns
    ("billed", usd), ("not_billed", 0.0) or ("pending", None); `key_usage` returns the key's usage in USD."""

    def billing_id(self, data: bytes) -> str | None:
        raise NotImplementedError

    def settle(self, billing_id: str) -> tuple[str, float | None]:
        raise NotImplementedError

    def key_usage(self) -> float:
        raise NotImplementedError


class OpenRouterSettler(Settler):
    _ID = re.compile(rb'"id"\s*:\s*"(gen-[0-9A-Za-z-]+)"')

    def __init__(self, api_base: str, key: str):
        self.api_base, self._key = api_base.rstrip("/"), key

    def billing_id(self, data: bytes) -> str | None:
        match = self._ID.search(data)
        return match.group(1).decode() if match else None

    def settle(self, billing_id: str) -> tuple[str, float | None]:
        try:
            data = self._get(f"/generation?id={urllib.parse.quote(billing_id)}")["data"]
        except Exception:  # a 404 or an outage proves nothing: keep it pending
            return ("pending", None)
        cost = data.get("total_cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            return ("billed", float(cost))
        return ("pending", None)

    def key_usage(self) -> float:
        usage = self._get("/key")["data"]["usage"]
        if not isinstance(usage, (int, float)) or isinstance(usage, bool):
            raise ValueError(f"usage is {usage!r}")
        return float(usage)

    def _get(self, path: str) -> dict:
        request = urllib.request.Request(self.api_base + path, headers={"Authorization": f"Bearer {self._key}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)


@dataclass
class Upstream:
    """Where a route's requests go, and who settles them."""
    base_url: str  # e.g. https://openrouter.ai/api
    settler: Settler
    scope: str
    key_fingerprint: str


@dataclass
class _Owner:
    job: str
    attempt: str


class Proxy:
    """`with Proxy(env, upstreams) as proxy:` serves on an ephemeral loopback port. `session(job, attempt)`
    gives a session's base URL per route."""

    def __init__(self, env: envelope_mod.Envelope, upstreams: dict[str, Upstream], *, settle_seconds=SETTLE_SECONDS,
                 reconcile_seconds=RECONCILE_SECONDS, shutdown_seconds=SHUTDOWN_SECONDS):
        self.env, self.upstreams = env, upstreams
        self._owners: dict[str, _Owner] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._settle_seconds, self._reconcile_seconds = settle_seconds, reconcile_seconds
        self._shutdown_seconds = shutdown_seconds
        self._server = None
        self._threads: list[threading.Thread] = []

    def __enter__(self) -> "Proxy":
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                proxy._handle(self)

            def do_GET(self):
                self.send_error(405)

            def log_message(self, *args):  # headers and bodies are never logged
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._threads = [threading.Thread(target=self._server.serve_forever, daemon=True),
                         threading.Thread(target=self._settle_loop, daemon=True),
                         threading.Thread(target=self._reconcile_loop, daemon=True)]
        self.reconcile_now()
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()  # stop admitting
        self._server.server_close()
        deadline = time.monotonic() + self._shutdown_seconds
        while time.monotonic() < deadline and self._pending():
            self.settle_now()
            time.sleep(min(self._settle_seconds, 1.0))
        self._stop.set()  # whatever is still unresolved stays in the ledger

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def session(self, job: str, attempt: str) -> dict[str, str]:
        """The base URL of each route for one attempt's session."""
        token = secrets.token_hex(12)
        with self._lock:
            self._owners[token] = _Owner(job, attempt)
        return {route: f"http://127.0.0.1:{self.port}/s/{token}/{route}" for route in self.upstreams}

    # ---- settlement and reconciliation

    def settle_now(self) -> None:
        for request_id, entry in self.env.state["requests"].items():
            if entry["state"] != "unresolved" or not entry["billing_id"] or entry["route"] not in self.upstreams:
                continue
            try:
                outcome, usd = self.upstreams[entry["route"]].settler.settle(entry["billing_id"])
            except Exception:  # proves nothing: keep it pending
                continue
            if outcome == "billed":
                self.env.settle(request_id, usd)
            elif outcome == "not_billed":
                self.env.settle(request_id, None)

    def reconcile_now(self) -> None:
        done = set()
        for upstream in self.upstreams.values():
            if upstream.scope in done:
                continue
            done.add(upstream.scope)  # one reading per key, however many routes share it
            try:
                self.env.reconcile(upstream.scope, upstream.settler.key_usage())
            except Exception:  # no valid reading: the snapshot expires and admission stops
                continue

    def _pending(self) -> bool:
        return any(r["state"] == "unresolved" and r["billing_id"] for r in self.env.state["requests"].values())

    def _settle_loop(self) -> None:
        while not self._stop.wait(self._settle_seconds):
            try:
                self.settle_now()
            except Exception:  # a settler fault leaves requests unresolved: the worst case stays counted
                continue

    def _reconcile_loop(self) -> None:
        while not self._stop.wait(self._reconcile_seconds):
            self.reconcile_now()

    # ---- one request

    def _handle(self, handler) -> None:
        parts = handler.path.split("/", 4)  # "", "s", token, route, rest
        if len(parts) < 5 or parts[1] != "s":
            return _reply(handler, 404, "unknown path")
        with self._lock:
            owner = self._owners.get(parts[2])
        upstream = self.upstreams.get(parts[3])
        if owner is None or upstream is None:
            return _reply(handler, 403, "unknown session or route")
        rest = "/" + parts[4]
        try:
            length = int(handler.headers.get("Content-Length", ""))
        except ValueError:
            return _reply(handler, 411, "a Content-Length is required")
        body = handler.rfile.read(length)
        route = self.env.state["routes"].get(parts[3])
        if route is None:
            return _reply(handler, 403, "unknown route")
        problem = policy_problem(route["policy"], rest, body)
        if problem:
            return _reply(handler, 403, problem)
        credential = _credential(handler.headers)
        if credential is None or fingerprint(credential) != upstream.key_fingerprint:
            return _reply(handler, 403, "the credential is not this route's key")
        request_id = uuid.uuid4().hex
        try:
            self.env.admit(request_id, parts[3], job=owner.job, attempt=owner.attempt)
        except envelope_mod.Refused as exc:
            return _reply(handler, 429, f"spend refused: {exc}")
        self._forward(handler, upstream, rest, body, request_id)

    def _forward(self, handler, upstream: Upstream, rest: str, body: bytes, request_id: str) -> None:
        target = urllib.parse.urlsplit(upstream.base_url.rstrip("/") + rest)
        connection_type = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(target.netloc, timeout=600)
        seen, recorded = b"", False
        try:
            headers = {k: v for k, v in handler.headers.items() if k.lower() not in _HOP_BY_HOP}
            connection.request("POST", target.path + (f"?{target.query}" if target.query else ""), body=body,
                               headers=headers)
            response = connection.getresponse()
            handler.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in _HOP_BY_HOP:
                    handler.send_header(key, value)
            handler.send_header("Connection", "close")
            handler.end_headers()
            while True:
                chunk = response.read1(65536) if hasattr(response, "read1") else response.read(65536)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
                if not recorded:
                    seen = (seen + chunk)[-65536:]
                    billing_id = upstream.settler.billing_id(seen)
                    if billing_id:
                        self.env.record_billing_id(request_id, billing_id)
                        recorded = True
        except (OSError, http.client.HTTPException):
            pass  # an error mid-stream keeps the request unresolved: it may still be billed
        finally:
            connection.close()


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
