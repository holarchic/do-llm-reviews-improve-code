"""Sessions routed through OpenRouter: the key, each CLI's routing, and the leak scan.

The key lives in a 0600 file whose dir is a private root, so no tool call can
read it. It never appears in argv, settings or config files:
- Claude Code fetches it through `apiKeyHelper`, a command Claude Code runs itself,
  outside the Bash sandbox; the key is never in any environment. (Its
  CLAUDE_CODE_SUBPROCESS_ENV_SCRUB alternative makes sandboxed `python3` need approval.)
- Codex reads it from OPENROUTER_API_KEY in its own environment; the config
  excludes the credential variables from its tool shells (its defaults do not).
The probes prove both. Every saved file is still scanned and redacted afterwards
(`scrub`), since the session processes hold the key in memory.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

KEY_DIR = Path.home() / ".config" / "openrouter"
KEY_PATH = KEY_DIR / "key"
ANTHROPIC_BASE_URL = "https://openrouter.ai/api"
API_BASE_URL = "https://openrouter.ai/api/v1"
# Every variable that can carry a credential to a routed session; none may reach its tool shells.
KEY_VARIABLES = ("OPENROUTER_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
REDACTED = b"[openrouter-key]"
_AGENT_PREFIXES = ("CLAUDE", "CODEX_", "AI_AGENT")
# Claude Code reaches for its own model aliases for background work; each must name the routed model.
_CLAUDE_MODEL_VARIABLES = ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                           "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
                           "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")
def codex_provider_config(base_url: str = API_BASE_URL) -> str:
    """Codex's provider block; a routed session's `base_url` is the routing proxy's (routing.py)."""
    return f"""model_provider = "openrouter"

[model_providers.openrouter]
name = "OpenRouter"
base_url = "{base_url}"
env_key = "OPENROUTER_API_KEY"
wire_api = "responses"

[shell_environment_policy]
inherit = "all"
ignore_default_excludes = false
exclude = [{", ".join(f'"{name}"' for name in KEY_VARIABLES)}]
"""


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens, as listed on 2026-10-21. Descriptive only: routing picks the provider."""
    input: float
    cache_read: float
    output: float


PRICES = {
    "qwen/qwen3-coder-30b-a3b-instruct": Price(input=0.07, cache_read=0.07, output=0.28),
    "z-ai/glm-4.7-flash": Price(input=0.0605, cache_read=0.01, output=0.4),
}


def api_key() -> str:
    key = KEY_PATH.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError(f"{KEY_PATH} is empty")
    return key


def _inherited() -> dict[str, str]:
    """The harness's environment without credentials or the variables of the agent session that may
    be running the harness (its effort, session ids, messaging token): a routed session starts clean."""
    return {name: value for name, value in os.environ.items()
            if name not in KEY_VARIABLES and not name.startswith(_AGENT_PREFIXES)}


def claude_key_helper() -> str:
    """Claude Code's `apiKeyHelper`: Claude Code reads the key from its output; it is never placed in an environment."""
    return f"/bin/cat {KEY_PATH}"


def claude_env(model: str, base_url: str = ANTHROPIC_BASE_URL) -> dict[str, str]:
    """The environment of a Claude Code session routed to `model`; its key comes from claude_key_helper().
    A routed session's `base_url` is the routing proxy's (routing.py)."""
    return {
        **_inherited(),
        "ANTHROPIC_BASE_URL": base_url,
        **{name: model for name in _CLAUDE_MODEL_VARIABLES},
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def codex_env(codex_home: Path) -> dict[str, str]:
    """The environment of a Codex session whose home holds `codex_provider_config()`."""
    return {**_inherited(), "CODEX_HOME": str(codex_home), "OPENROUTER_API_KEY": api_key()}


def scrub(root: Path, key: str | None = None) -> list[str]:
    """Redact the key in every regular file under root; delete the .git of any repo that holds it in an object
    or a metadata file (compressed objects can't be redacted in place). Returns the paths hit, relative to root."""
    secret = (key or api_key()).encode()
    hits = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or ".git" in path.relative_to(root).parts:
            continue
        try:
            data = path.read_bytes()
        except OSError:  # a file that can't be checked can't be kept
            path.unlink(missing_ok=True)
            hits.append(str(path.relative_to(root)))
            continue
        if secret in data:
            hits.append(str(path.relative_to(root)))
            try:
                path.write_bytes(data.replace(secret, REDACTED))
            except OSError:  # a copy that can't be redacted is removed
                path.unlink(missing_ok=True)
    for git_dir in sorted(p for p in root.rglob(".git") if p.is_dir() and not p.is_symlink()):
        if _git_dir_holds(git_dir, secret):
            shutil.rmtree(git_dir)
            hits.append(str(git_dir.relative_to(root)))
    return hits


def _git_dir_holds(git_dir: Path, secret: bytes) -> bool:
    """The key in any metadata file (config, logs, hooks) or object; a repo whose files or objects can't be read is
    assumed to hold it."""
    try:
        if any(secret in p.read_bytes() for p in git_dir.rglob("*") if p.is_file() and not p.is_symlink()):
            return True
    except OSError:
        return True
    completed = subprocess.run(["git", f"--git-dir={git_dir}", "cat-file", "--batch-all-objects", "--batch"],
                               capture_output=True, env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin",
                                                         "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    return completed.returncode != 0 or secret in completed.stdout


def spent(ledger: Path, phase: str) -> float | None:
    """Pilot 2's recorded spend for a phase (its last reading minus its baseline), for its report."""
    try:
        entry = json.loads(ledger.read_text(encoding="utf-8"))[phase]
        return entry["readings"][-1] - entry["baseline"]
    except (FileNotFoundError, KeyError, IndexError, json.JSONDecodeError):
        return None
