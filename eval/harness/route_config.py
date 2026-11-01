"""A route's configuration as typed records: its request policy and its upstream.

The envelope persists them as plain tables; the proxy and the spend CLI parse them here, so a
configuration the proxy would refuse is never stored, and a request is judged by one parser.

A route's `max_charge` is declared from the endpoint's provider-enforced limits
(`prices.request_charge_bound`): input up to the model's context window, which already counts
whatever the provider adds to a request (tool prompts, message framing), and output up to the
enforced maximum. The policy keeps a request inside what that bound prices: text and tool content
only, so nothing the provider expands or bills separately (a file, document, image, audio, a
plugin or a server tool) gets through. `max_body_bytes` is a transport limit, not the input bound.
"""

import json
import urllib.parse
from dataclasses import dataclass

# Content a route admits, for both wire APIs: text, tool calls and results, and the model's own
# reasoning. Every typed object in `messages`, `input` and `system` must be one of these.
CONTENT_TYPES = frozenset({
    # Anthropic Messages
    "text", "tool_use", "tool_result", "thinking", "redacted_thinking",
    "ephemeral",  # cache_control: a cache write, priced at the dearest input-side rate
    # OpenAI Responses
    "message", "input_text", "output_text", "function_call", "function_call_output", "reasoning",
    "summary_text", "reasoning_text", "custom_tool_call", "custom_tool_call_output",
})
_CONTENT_FIELDS = ("messages", "input", "system")


@dataclass(frozen=True)
class Policy:
    """A route's request allowlist."""
    paths: frozenset[str]
    models: frozenset[str]
    fields: frozenset[str]
    tool_types: frozenset[str]
    max_body_bytes: int
    output_limit: tuple[str, int] | None  # (field, cap) where the provider enforces a requested maximum


@dataclass(frozen=True)
class Upstream:
    base_url: str
    settler: str


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
    if not _positive_int(size):
        raise ValueError("policy max_body_bytes must be a positive integer")
    limit = raw.get("output_limit")
    if limit is not None:
        if not (isinstance(limit, dict) and isinstance(limit.get("field"), str) and _positive_int(limit.get("cap"))):
            raise ValueError("policy output_limit must be {field: str, cap: positive int}")
        limit = (limit["field"], limit["cap"])
    return Policy(strings("paths"), strings("models"), strings("fields"), strings("tool_types"), size, limit)


def parse_upstream(raw) -> Upstream:
    """Where a route forwards: an http(s) base URL and a settler name. Raises ValueError otherwise."""
    if not isinstance(raw, dict) or not isinstance(raw.get("base_url"), str) or not isinstance(raw.get("settler"), str):
        raise ValueError("an upstream is {base_url: str, settler: str}")
    url = urllib.parse.urlsplit(raw["base_url"])
    if url.scheme not in ("http", "https") or not url.netloc or url.query or url.fragment:
        raise ValueError(f"upstream base_url {raw['base_url']!r} is not an http(s) base URL")
    if not raw["settler"]:
        raise ValueError("an upstream names its settler")
    return Upstream(raw["base_url"].rstrip("/"), raw["settler"])


def request_problem(policy: Policy, path: str, body: bytes) -> str | None:
    """Why a request falls outside its route's policy, or None. `path` is the part after the route."""
    endpoint = path.split("?", 1)[0]
    if endpoint not in policy.paths:
        return f"path {endpoint!r} is not allowed"
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
    model = request.get("model")
    if not isinstance(model, str) or model not in policy.models:
        return f"model {model!r} is not allowed"
    tools = request.get("tools", [])
    if not isinstance(tools, list):
        return "tools must be a list"
    for tool in tools:
        kind = tool.get("type", "") if isinstance(tool, dict) else None
        if not isinstance(kind, str) or kind not in policy.tool_types:
            return f"tool type {kind!r} is not allowed"
    for name in _CONTENT_FIELDS:
        problem = _content_problem(request.get(name))
        if problem:
            return problem
    if policy.output_limit:
        field, cap = policy.output_limit
        if not _positive_int(request.get(field)) or request[field] > cap:
            return f"{field} must be set at or below {cap}"
    return None


def _content_problem(node) -> str | None:
    if isinstance(node, list):
        return next((p for p in map(_content_problem, node) if p), None)
    if not isinstance(node, dict):
        return None
    if "type" in node:
        kind = node["type"]
        if not isinstance(kind, str) or kind not in CONTENT_TYPES:
            return f"content type {kind!r} is not allowed"
    # A tool call's arguments are the model's own output, sent back as text; they are not walked.
    return next((p for key, value in node.items()
                 if not (key == "input" and node.get("type") == "tool_use")
                 for p in [_content_problem(value)] if p), None)


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
