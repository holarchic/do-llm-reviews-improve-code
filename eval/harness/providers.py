"""The routed providers: where each one's key lives, how each CLI reaches it, and the leak scan.

Each key lives in a 0600 file whose dir is a private root, so no tool call can read it. It never
appears in argv, settings or config files:
- Claude Code fetches it through `apiKeyHelper`, a command Claude Code runs itself, outside the Bash
  sandbox; the key is never in any environment.
- Codex reads it from the provider's credential variable in its own environment; the config excludes
  every credential variable from its tool shells (its defaults do not).
The probes prove both. Every saved file is still scanned and redacted afterwards (`scrub`), since the
session processes hold the key in memory.

A routed session reaches its provider only through the routing proxy (`routing.py`): the CLI's base
URL is the route's proxy base plus the provider's path for that CLI, and the proxy forwards the rest
of the path to the provider's upstream.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

OPENROUTER, DEEPSEEK = "openrouter", "deepseek"


@dataclass(frozen=True)
class Provider:
    name: str
    upstream: str  # the base the proxy forwards a route's path to
    claude_path: str  # added under the route for Claude Code, which then posts /v1/messages
    codex_path: str  # added under the route for Codex, which then posts /responses
    codex_variable: str  # the variable Codex reads the key from; its settler (settlers.py) bears its name


PROVIDERS = {p.name: p for p in (
    Provider(OPENROUTER, "https://openrouter.ai/api", "", "/v1", "OPENROUTER_API_KEY"),
    # Anthropic Messages at /anthropic/v1/messages; Responses at /responses (Codex's base is the root).
    Provider(DEEPSEEK, "https://api.deepseek.com", "/anthropic", "", "DEEPSEEK_API_KEY"),
)}

KEY_ROOT = Path.home() / ".config"
# Per provider, read at call time (a test points one elsewhere).
KEY_PATHS: dict[str, Path] = {name: KEY_ROOT / name / "key" for name in PROVIDERS}
# Every variable that can carry a credential to a routed session; none may reach its tool shells.
CREDENTIAL_VARIABLES = tuple(sorted({p.codex_variable for p in PROVIDERS.values()}
                                    | {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"}))
_AGENT_PREFIXES = ("CLAUDE", "CODEX_", "AI_AGENT")
# Claude Code reaches for its own model aliases for background work; each must name the routed model.
_CLAUDE_MODEL_VARIABLES = ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                           "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
                           "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")


def get(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(f"unknown provider {name!r}") from None


def key_dirs() -> tuple[Path, ...]:
    """Every provider's key dir: private roots, denied to every session."""
    return tuple(KEY_ROOT / name for name in PROVIDERS)


def key_path(name: str) -> Path:
    return KEY_PATHS[get(name).name]


def api_key(name: str) -> str:
    key = key_path(name).read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError(f"{key_path(name)} is empty")
    return key


def _inherited() -> dict[str, str]:
    """The harness's environment without credentials or the variables of the agent session that may
    be running the harness (its effort, session ids, messaging token): a routed session starts clean."""
    return {name: value for name, value in os.environ.items()
            if name not in CREDENTIAL_VARIABLES and not name.startswith(_AGENT_PREFIXES)}


def claude_key_helper(name: str) -> str:
    """Claude Code's `apiKeyHelper`: Claude Code reads the key from its output; it is never placed in an environment."""
    return f"/bin/cat {key_path(name)}"


def claude_env(name: str, model: str, route_base: str) -> dict[str, str]:
    """The environment of a Claude Code session routed to `model`; its key comes from claude_key_helper()."""
    return {
        **_inherited(),
        "ANTHROPIC_BASE_URL": route_base + get(name).claude_path,
        **{variable: model for variable in _CLAUDE_MODEL_VARIABLES},
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def codex_provider_config(name: str, route_base: str) -> str:
    """Codex's provider block for a routed session."""
    provider = get(name)
    return f"""model_provider = "{provider.name}"

[model_providers.{provider.name}]
name = "{provider.name}"
base_url = "{route_base + provider.codex_path}"
env_key = "{provider.codex_variable}"
wire_api = "responses"

[shell_environment_policy]
inherit = "all"
ignore_default_excludes = false
exclude = [{", ".join(f'"{variable}"' for variable in CREDENTIAL_VARIABLES)}]
"""


def codex_env(name: str, codex_home: Path) -> dict[str, str]:
    """The environment of a Codex session whose home holds `codex_provider_config(name, ...)`."""
    return {**_inherited(), "CODEX_HOME": str(codex_home), get(name).codex_variable: api_key(name)}


def installed_keys(required=()) -> dict[str, str]:
    """Every provider key on this machine, by provider. Only an absent key of a provider not in
    `required` is skipped: a key that exists but can't be read could still be in the files."""
    keys = {}
    for name in PROVIDERS:
        try:
            keys[name] = api_key(name)
        except FileNotFoundError:
            if name in required:
                raise ValueError(f"{name}'s key file is missing, so its run can't be checked") from None
        except (OSError, ValueError) as exc:
            raise ValueError(f"{name}'s key file can't be read ({type(exc).__name__}), so nothing can be checked") from None
    return keys


def redact(text: str) -> str:
    """Text with every installed provider key replaced by its marker (an unreadable key file raises)."""
    for name, key in installed_keys().items():
        text = text.replace(key, f"[{name}-key]")
    return text


def scrub(root: Path, required=()) -> list[str]:
    """Redact every installed provider key in every regular file under root; delete the .git of any repo
    that holds one in an object or a metadata file (compressed objects can't be redacted in place).
    `required` names the providers the scrubbed run used: each must have its key here. Returns the
    paths hit, relative to root."""
    secrets = {key.encode(): f"[{name}-key]".encode() for name, key in installed_keys(required).items()}
    if not secrets:  # only routed runs are scrubbed, and a routed run had its key
        raise ValueError("no provider key is installed, so nothing can be checked")
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
        if any(secret in data for secret in secrets):
            hits.append(str(path.relative_to(root)))
            for secret, marker in secrets.items():
                data = data.replace(secret, marker)
            try:
                path.write_bytes(data)
            except OSError:  # a copy that can't be redacted is removed
                path.unlink(missing_ok=True)
    for git_dir in sorted(p for p in root.rglob(".git") if p.is_dir() and not p.is_symlink()):
        if _git_dir_holds(git_dir, tuple(secrets)):
            shutil.rmtree(git_dir)
            hits.append(str(git_dir.relative_to(root)))
    return hits


def files_holding_keys(root: Path, required=()) -> list[str]:
    """Saved files still holding an installed key (after a scrub, there must be none)."""
    secrets = [key.encode() for key in installed_keys(required).values()]
    return [str(p.relative_to(root)) for p in root.rglob("*")
            if p.is_file() and not p.is_symlink() and any(s in p.read_bytes() for s in secrets)]


def _git_dir_holds(git_dir: Path, secrets: tuple[bytes, ...]) -> bool:
    """A key in any metadata file (config, logs, hooks) or object; a repo whose files or objects can't be
    read is assumed to hold one."""
    try:
        if any(any(s in p.read_bytes() for s in secrets) for p in git_dir.rglob("*") if p.is_file() and not p.is_symlink()):
            return True
    except OSError:
        return True
    completed = subprocess.run(["git", f"--git-dir={git_dir}", "cat-file", "--batch-all-objects", "--batch"],
                               capture_output=True, env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin",
                                                         "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    return completed.returncode != 0 or any(s in completed.stdout for s in secrets)
