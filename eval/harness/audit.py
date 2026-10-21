"""Private roots, and a diagnostic scan of tool calls that name them.

The roots feed the OS-level denials (Claude's Bash sandbox, the Codex Seatbelt
profile); those, proved by the probes, are the control. The transcript scan
only reports which tool calls named a private path — static parsing of shell
commands cannot be complete (`cd` chains, variables, built strings).
"""

import json
import os
import re
import secrets
from pathlib import Path

from .openrouter import KEY_DIR

HOME = Path.home()
RUNS_ROOT = Path("/private/tmp/eval-runs")
EVAL_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOTS = (
    EVAL_ROOT,
    HOME / "Library" / "CloudStorage" / "Dropbox",
    HOME / "pg",
    HOME / ".claude",
    HOME / ".codex",
    # The operator's global instructions: ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md link here.
    HOME / ".agents",
    KEY_DIR,
    RUNS_ROOT,
)
# Per tool, the input fields that name what it touches. Grep's pattern is search text, not a path.
_ACCESS_FIELDS = {
    "Bash": ("command",),
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "Write": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("path", "pattern"),
    "Grep": ("path",),
}
_STOP = r"[^\s'\"`;|&<>(),=]"
# Any token starting with ~ or .., or containing a slash: relative tokens matter
# because a symlink planted in the run dir can point into a private root.
_PATH_TOKEN = re.compile(rf"~{_STOP}*|\.\.{_STOP}*|{_STOP}*/{_STOP}*")


def anonymous_workdir() -> Path:
    """A neutral, unguessable dir under the runs root. The root is not listable even by its
    owner (0311: write and traverse only), so a session cannot find another run's dir;
    the name reveals no task or arm."""
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    RUNS_ROOT.chmod(0o311)
    path = RUNS_ROOT / secrets.token_hex(8)
    path.mkdir()
    return path


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
    roots = private_roots() + _with_resolved(tuple(extra_roots))
    own = Path(os.path.realpath(run_dir))
    hits = []
    for text in _tool_call_texts(transcript):
        for token in _PATH_TOKEN.findall(text):
            # Check the path as written and where it leads: a link can hide either side.
            for path in _candidates(token, run_dir):
                if _is_within(path, own):
                    continue
                if any(_is_within(path, root) for root in roots):
                    hits.append(f"{path} (from: {text[:200]})")
                    break
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
                # File content written is not an access; an unknown tool is scanned whole.
                fields = _ACCESS_FIELDS.get(block.get("name"))
                yield from _strings(tool_input if fields is None else
                                    {key: value for key, value in tool_input.items() if key in fields})


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


def _candidates(token: str, run_dir: Path) -> tuple[Path, Path]:
    """The token's lexical absolute path and its resolved target. Only a trailing colon is
    trimmed: dots are path components (`..`)."""
    token = token.rstrip(":")
    if token.startswith("~"):
        path = Path(os.path.expanduser(token))
    elif token.startswith("/"):
        path = Path(token)
    else:
        path = Path(os.path.realpath(run_dir)) / token
    return Path(os.path.normpath(path)), Path(os.path.realpath(path))


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents
