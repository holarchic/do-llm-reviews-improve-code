"""Find tool calls in a session transcript that touch private material.

Claude writers are also denied private reads by the Bash sandbox; for Codex
sessions this scan is the only control, so it detects access, it cannot
prevent it. A command that builds a path at run time (string concatenation,
environment variables) evades it — that residual risk is reported.
"""

import json
import os
import re
from pathlib import Path

HOME = Path.home()
RUNS_ROOT = Path("/private/tmp/eval-runs")
EVAL_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOTS = (
    EVAL_ROOT,
    HOME / "Library" / "CloudStorage" / "Dropbox",
    HOME / "pg",
    HOME / ".claude",
    HOME / ".codex",
    RUNS_ROOT,
)
_ACCESS_FIELDS = ("command", "file_path", "path", "pattern", "notebook_path")
_STOP = r"[^\s'\"`;|&<>(),=]"
# Any token starting with ~ or .., or containing a slash: relative tokens matter
# because a symlink planted in the run dir can point into a private root.
_PATH_TOKEN = re.compile(rf"~{_STOP}*|\.\.{_STOP}*|{_STOP}*/{_STOP}*")


def private_roots() -> list[Path]:
    """Private roots as written and as resolved (symlinks like ~/pg resolve into Dropbox)."""
    return _with_resolved(PRIVATE_ROOTS)


def sandbox_deny_roots() -> list[Path]:
    """Roots the Claude Bash sandbox denies. The runs root is left out because each
    run dir sits inside it; sibling runs are covered by the audit alone."""
    return _with_resolved(tuple(root for root in PRIVATE_ROOTS if root != RUNS_ROOT))


def _with_resolved(roots: tuple[Path, ...]) -> list[Path]:
    return sorted(set(roots) | {Path(os.path.realpath(root)) for root in roots})


def audit_transcript(transcript: Path, run_dir: Path, *, extra_roots: tuple[Path, ...] = ()) -> list[str]:
    """Return one line per tool-call path under a private root; the run dir itself is exempt."""
    roots = private_roots() + [Path(os.path.realpath(r)) for r in extra_roots]
    own = Path(os.path.realpath(run_dir))
    hits = []
    for text in _tool_call_texts(transcript):
        for token in _PATH_TOKEN.findall(text):
            resolved = _resolve(token, run_dir)
            if _is_within(resolved, own):
                continue
            if any(_is_within(resolved, root) for root in roots):
                hits.append(f"{resolved} (from: {text[:200]})")
    return hits


def _tool_call_texts(transcript: Path):
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        yield from _claude_tool_inputs(event)
        yield from _codex_commands(event)


def _claude_tool_inputs(event: dict):
    message = event.get("message") if event.get("type") == "assistant" else None
    content = message.get("content") if isinstance(message, dict) else None
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_input = block.get("input")
            if isinstance(tool_input, dict):
                # Only fields that name what the tool touches; file content written is not an access.
                yield from _strings({key: value for key, value in tool_input.items() if key in _ACCESS_FIELDS})


def _strings(value):
    # Raw string values, not a JSON dump: escaped newlines would glue tokens together.
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _codex_commands(event: dict):
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") == "command_execution":
        yield str(item.get("command", ""))


def _resolve(token: str, run_dir: Path) -> Path:
    token = token.rstrip(".:")
    if token.startswith("~"):
        path = Path(os.path.expanduser(token))
    elif token.startswith("/"):
        path = Path(token)
    else:
        path = run_dir / token
    return Path(os.path.realpath(path))


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents
