"""Headless Claude Code and Codex sessions, isolated from the operator's config.

Every model call in the eval goes through run_claude or run_codex, so the
isolation flags, tool policy per role, timeouts and terminal classification
live here once.

Isolation:
- Claude: project-only setting sources (no user CLAUDE.md), no MCP, no slash
  commands. A per-session settings file turns on the Bash sandbox with
  denyRead over the private roots, and denies native file tools the same roots
  (denyRead does not govern them). The sandbox fails closed.
- Codex: a throwaway CODEX_HOME holding only auth and effort, so no global
  AGENTS.md or config loads. Codex's own read-only sandbox blocks writes, not
  reads, so Codex runs inside an outer macOS Seatbelt profile instead (its own
  sandbox bypassed, as Codex documents for externally sandboxed runs) that
  denies reads and writes of the private roots and other runs; it may write
  its own home, and its cwd only in the writer role.
- Routed sessions (provider "openrouter") get their routing and key from
  openrouter.py; the operator's Codex login is not copied into their home.
- Offline sessions (the replay's) reach no network beyond localhost from their
  tool commands. Claude's Bash sandbox allows no domain. Codex's Seatbelt
  profile denies every outbound connection but localhost, its model calls go
  through a per-session allowlist proxy (egress.py), and its web search is off.
- The transcript audit is a diagnostic: access is denied by the OS, and the
  probes prove it. Audit hits are recorded, not counted as contamination.
"""

import json
import os
import shutil
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from . import audit, egress, openrouter, procs

CLAUDE_JUDGE_MODEL = "claude-fable-5-1"
CODEX_JUDGE_MODEL = "gpt-6-astra"
CODEX_EFFORT = "high"

WRITER, REVIEWER, JUDGE = "writer", "reviewer", "judge"
TIMEOUT_SECONDS = {WRITER: 1800, REVIEWER: 1200, JUDGE: 900}

COMPLETED, TIMEOUT, INFRA = "completed", "timeout", "infra"
NATIVE, OPENROUTER = "native", "openrouter"

_FILE_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")
_CLAUDE_TOOLS = {
    WRITER: ["--tools", "Bash,Read,Edit,Write,Glob,Grep", "--permission-mode", "acceptEdits"],
    REVIEWER: ["--tools", "Read,Glob,Grep,Bash"],
    JUDGE: ["--tools", "Read,Glob,Grep"],
}


@dataclass(frozen=True)
class Agent:
    """Which CLI runs a session, on which model, served by whom."""
    cli: str  # "claude" | "codex"
    model: str
    provider: str = NATIVE  # NATIVE: the CLI's own login | OPENROUTER


def run(agent: Agent, prompt: str, *, role: str, cwd: Path, out_dir: Path, offline: bool = False) -> "Session":
    session = {"claude": run_claude, "codex": run_codex}[agent.cli]
    return session(agent.model, prompt, role=role, cwd=cwd, out_dir=out_dir, provider=agent.provider,
                   offline=offline)


@dataclass(frozen=True)
class Session:
    terminal: str
    reason: str
    final_text: str
    transcript: Path
    seconds: float
    usage: dict = field(default_factory=dict)
    reported_models: list[str] = field(default_factory=list)
    audit_hits: list[str] = field(default_factory=list)

    def unrouted_models(self, agent: "Agent") -> list[str]:
        """Models a routed session reported besides its own (e.g. a background call to an Anthropic model)."""
        if agent.provider != OPENROUTER:
            return []
        return [model for model in self.reported_models if model != agent.model]

    def to_record(self) -> dict:
        return {
            "terminal": self.terminal,
            "reason": self.reason,
            "transcript": str(self.transcript),
            "seconds": round(self.seconds, 1),
            "usage": self.usage,
            "reported_models": self.reported_models,
            "audit_hits": self.audit_hits,
        }


def claude_settings(role: str, cwd: Path, provider: str = NATIVE, offline: bool = False) -> dict:
    roots = [str(root) for root in audit.sandbox_deny_roots()]
    filesystem: dict = {"denyRead": roots}
    if role == REVIEWER:
        # Read-only like the Codex reviewer: any shell command, no writes to the repo.
        filesystem["denyWrite"] = [os.path.realpath(cwd)]
    routing = {"apiKeyHelper": openrouter.claude_key_helper()} if provider == OPENROUTER else {}
    # The CLI's own model traffic is outside the Bash sandbox; tool commands get localhost only.
    network = {"network": {"allowedDomains": [], "allowLocalBinding": True}} if offline else {}
    return {
        **routing,
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": role in (WRITER, REVIEWER),
            "allowUnsandboxedCommands": False,
            "filesystem": filesystem,
            **network,
        },
        # "//" marks an absolute path in permission rules.
        "permissions": {"deny": [f"{tool}(/{root}/**)" for tool in _FILE_TOOLS for root in roots]
                        + (["Edit", "Write"] if role != WRITER else [])},
    }


def claude_argv(model: str, role: str, settings_path: Path) -> list[str]:
    # The prompt arrives on stdin: argv is visible to every process (`ps`), so it carries no content.
    return [
        "claude", "-p",
        "--model", model,
        "--setting-sources", "project",
        "--settings", str(settings_path),
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format", "stream-json",
        "--verbose",
        *_CLAUDE_TOOLS[role],
    ]


def run_claude(model: str, prompt: str, *, role: str, cwd: Path, out_dir: Path, provider: str = NATIVE,
               offline: bool = False) -> Session:
    # Absolute: the CLI runs in cwd, where a relative settings path would not resolve.
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    settings_path = out_dir / "settings.json"
    settings_path.write_text(json.dumps(claude_settings(role, cwd, provider, offline), indent=2), encoding="utf-8")
    argv = claude_argv(model, role, settings_path)
    prompt_path = out_dir / "prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    (out_dir / "argv.json").write_text(json.dumps(argv, indent=2), encoding="utf-8")
    transcript = out_dir / "transcript.jsonl"
    env = openrouter.claude_env(model) if provider == OPENROUTER else None
    proc = procs.run(argv, cwd=cwd, timeout=TIMEOUT_SECONDS[role], stdout_path=transcript,
                     stderr_path=out_dir / "stderr.log", env=env, stdin_path=prompt_path)
    return _finish(classify_claude(transcript, proc, role), cwd)


def run_codex(model: str, prompt: str, *, role: str, cwd: Path, out_dir: Path, provider: str = NATIVE,
              offline: bool = False) -> Session:
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / "final.txt"
    transcript = out_dir / "transcript.jsonl"
    # Under the unlistable runs root, like run dirs: no other session can find it.
    codex_home = audit.anonymous_workdir()
    try:
        os.chmod(codex_home, 0o700)
        config = f'model_reasoning_effort = "{CODEX_EFFORT}"\n'
        if offline:
            config += 'web_search = "disabled"\n'
        if provider == OPENROUTER:
            config += openrouter.CODEX_PROVIDER_CONFIG
            env = openrouter.codex_env(codex_home)
        else:
            shutil.copy2(Path.home() / ".codex" / "auth.json", codex_home / "auth.json")
            env = {**os.environ, "CODEX_HOME": str(codex_home)}
        (codex_home / "config.toml").write_text(config, encoding="utf-8")
        profile = out_dir / "seatbelt.sb"
        profile.write_text(codex_seatbelt_profile(cwd, codex_home, writable=role == WRITER, offline=offline),
                           encoding="utf-8")
        # Every file Codex touches lives in its own home and is copied out after: out_dir is under a
        # read-denied root, and node aborts at start-up if it cannot stat its stdout.
        inner = {name: codex_home / name for name in ("final.txt", "transcript.jsonl", "stderr.log")}
        prompt_path = codex_home / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        argv = ["sandbox-exec", "-f", str(profile), *codex_argv(model, inner["final.txt"])]
        (out_dir / "argv.json").write_text(json.dumps(argv, indent=2), encoding="utf-8")
        with (egress.Proxy(out_dir / "egress.log") if offline else nullcontext()) as proxy:
            proc = procs.run(argv, cwd=cwd, timeout=TIMEOUT_SECONDS[role], stdout_path=inner["transcript.jsonl"],
                             stderr_path=inner["stderr.log"], env={**env, **proxy.env()} if proxy else env,
                             stdin_path=prompt_path)
        shutil.copy2(codex_home / "config.toml", out_dir / "codex-config.toml")
        for name, path in inner.items():
            if path.exists():
                shutil.copy2(path, out_dir / name)
    finally:
        shutil.rmtree(codex_home, ignore_errors=True)
    return _finish(classify_codex(transcript, final_path, proc, role, model), cwd)


def codex_seatbelt_profile(cwd: Path, home: Path, *, writable: bool = False, offline: bool = False) -> str:
    """Deny reads and writes of every private root and of other runs. The session reads its cwd and
    its own home, writes its home, and writes its cwd only when `writable` (the writer role). Offline,
    every outbound connection is denied, Unix sockets included (the Docker socket is a way out), but
    localhost, where the session's egress proxy listens.
    Later rules win in Seatbelt, so the own dirs are re-allowed after their roots are denied."""
    own = os.path.realpath(cwd)
    own_home = os.path.realpath(home)
    denied = " ".join(f'(subpath "{root}")' for root in audit.sandbox_deny_roots())
    runs_roots = sorted({str(audit.RUNS_ROOT), os.path.realpath(audit.RUNS_ROOT)})
    runs = " ".join(f'(subpath "{root}")' for root in runs_roots)
    # Stat (not list) of the runs root itself: node aborts at start-up if it cannot resolve its cwd.
    runs_stat = " ".join(f'(literal "{root}")' for root in runs_roots)
    return (
        "(version 1)\n(allow default)\n"
        f"(deny file-read* {denied} {runs})\n"
        f"(allow file-read-metadata {runs_stat})\n"
        f'(allow file-read* (subpath "{own}") (subpath "{own_home}"))\n'
        f"(deny file-write* {denied} {runs})\n"
        f'(allow file-write* (subpath "{own_home}"))\n'
        + (f'(allow file-write* (subpath "{own}"))\n' if writable else "")
        + ('(deny network-outbound)\n(allow network-outbound (remote ip "localhost:*"))\n' if offline else "")
    )


def codex_argv(model: str, final_path: Path) -> list[str]:
    # "-" reads the prompt from stdin: argv is visible to every process, so it carries no content.
    return [
        "codex", "--dangerously-bypass-approvals-and-sandbox", "-m", model,
        "-c", f"model_reasoning_effort={CODEX_EFFORT}",
        "exec", "--skip-git-repo-check", "--ephemeral", "--json", "-o", str(final_path), "-",
    ]


def classify_claude(transcript: Path, proc: procs.ProcResult, role: str) -> Session:
    result = None
    for event in _events(transcript):
        if event.get("type") == "result":
            result = event
    if proc.timed_out:
        return _timed_out(transcript, proc, role, final_text=(result or {}).get("result") or "")
    if result is None:
        return Session(INFRA, f"no result event (exit {proc.exit_code})", "", transcript, proc.seconds)
    usage = result.get("usage") or {}
    models = sorted((result.get("modelUsage") or {}).keys())
    if result.get("is_error") or result.get("subtype") != "success" or proc.exit_code != 0:
        reason = f"error result: subtype={result.get('subtype')} exit={proc.exit_code}: {str(result.get('result'))[:300]}"
        return Session(INFRA, reason, "", transcript, proc.seconds, usage, models)
    return Session(COMPLETED, "ok", result.get("result") or "", transcript, proc.seconds, usage, models)


def classify_codex(transcript: Path, final_path: Path, proc: procs.ProcResult, role: str, model: str) -> Session:
    errors, usage, completed = [], {}, False
    for event in _events(transcript):
        kind = event.get("type")
        if kind in ("error", "turn.failed"):
            errors.append(json.dumps(event)[:300])
        elif kind == "turn.completed":
            completed, usage = True, event.get("usage") or {}
    final_text = final_path.read_text(encoding="utf-8") if final_path.exists() else ""
    if proc.timed_out:
        return _timed_out(transcript, proc, role, final_text=final_text)
    # Codex events don't name the model; the requested one is recorded.
    models = [model]
    if errors or not completed or proc.exit_code != 0 or not final_text.strip():
        reason = f"exit={proc.exit_code} completed={completed} errors={errors[:3]}"
        return Session(INFRA, reason, "", transcript, proc.seconds, usage, models)
    return Session(COMPLETED, "ok", final_text, transcript, proc.seconds, usage, models)


def _timed_out(transcript: Path, proc: procs.ProcResult, role: str, *, final_text: str) -> Session:
    # A judge has no partial artifact to score, so its timeout is an infrastructure failure.
    terminal = INFRA if role == JUDGE else TIMEOUT
    return Session(terminal, f"wall clock {TIMEOUT_SECONDS[role]}s", final_text, transcript, proc.seconds)


def _finish(session: Session, cwd: Path) -> Session:
    hits = audit.audit_transcript(session.transcript, cwd)
    return Session(session.terminal, session.reason, session.final_text, session.transcript,
                   session.seconds, session.usage, session.reported_models, hits)


def _events(transcript: Path):
    if not transcript.exists():
        return
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event
