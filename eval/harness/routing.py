"""The routing context: which envelope and proxy a routed session goes through, and for which job.

A routed session (provider ≠ native) launches only while a context is active (`for_envelope`) and
its thread names a job (`job`). `session_base` gives the session its proxy URL; `refusal` tells
the runner the envelope refused it. One context per process; jobs are per thread.
"""

import contextlib
import threading

from . import envelope as envelope_mod
from . import proxy as proxy_mod


class RoutingRequired(Exception):
    """A routed session asked to launch with no routing context: it would bypass the envelope."""


_active = None
_active_lock = threading.Lock()
_job = threading.local()


def route_name(provider: str, model: str) -> str:
    """A route's name as it appears in a URL path: one route per provider and model."""
    return f"{provider}--{model.replace('/', '__')}"


@contextlib.contextmanager
def active(proxy):
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


def session_base(provider: str, model: str, attempt: str) -> str:
    """The proxy base URL for one attempt's session; raises unless a context and a job are active."""
    with _active_lock:
        current = _active
    job_id = getattr(_job, "value", None)
    if current is None or job_id is None:
        raise RoutingRequired(f"{provider} {model}: no routing context, so no envelope bounds this session")
    route = route_name(provider, model)
    if route not in current.routes:
        raise RoutingRequired(f"no route {route} in the active envelope")
    return current.session(job_id, attempt)[route]


def refusal(attempt: str) -> str | None:
    """The spend refusal recorded against an attempt, if any."""
    with _active_lock:
        current = _active
    return None if current is None else current.env.refusal(attempt)


@contextlib.contextmanager
def for_envelope(name: str, *, root=None):
    """Own the envelope, serve its proxy and make it the active context. Exit runs in reverse: the
    context closes, the proxy drains its handlers and workers until its deadline, and then ownership is
    released; a straggler past the deadline finds the envelope closed and can write nothing."""
    with envelope_mod.Envelope.owned(name, root=root) as env, proxy_mod.Proxy(env) as served, active(served):
        yield env
