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
  AGENTS.md or config loads. The read-only sandbox blocks writes, not reads;
  the transcript audit is the control there.
"""

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import audit, procs

WRITER_MODEL = "claude-haiku-5-5"
SAME_REVIEWER_MODEL = "claude-haiku-5-5"
CROSS_REVIEWER_MODEL = "gpt-6-luna"
CLAUDE_JUDGE_MODEL = "claude-fable-5-1"
CODEX_JUDGE_MODEL = "gpt-6-astra"
CODEX_EFFORT = "high"

WRITER, REVIEWER, JUDGE = "writer", "reviewer", "judge"
TIMEOUT_SECONDS = {WRITER: 1800, REVIEWER: 1200, JUDGE: 900}

COMPLETED, TIMEOUT, INFRA = "completed", "timeout", "infra"

_FILE_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")
_CLAUDE_TOOLS = {
    WRITER: ["--tools", "Bash,Read,Edit,Write,Glob,Grep", "--permission-mode", "acceptEdits"],
    REVIEWER: ["--tools", "Read,Glob,Grep,Bash"],
    JUDGE: ["--tools", "Read,Glob,Grep"],
}


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


def claude_settings(role: str, cwd: Path) -> dict:
    roots = [str(root) for root in audit.sandbox_deny_roots()]
    filesystem: dict = {"denyRead": roots}
    if role == REVIEWER:
        # Read-only like the Codex reviewer: any shell command, no writes to the repo.
        filesystem["denyWrite"] = [os.path.realpath(cwd)]
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": role in (WRITER, REVIEWER),
            "allowUnsandboxedCommands": False,
            "filesystem": filesystem,
        },
        # "//" marks an absolute path in permission rules.
        "permissions": {"deny": [f"{tool}(/{root}/**)" for tool in _FILE_TOOLS for root in roots]
                        + (["Edit", "Write"] if role != WRITER else [])},
    }


def claude_argv(model: str, prompt: str, role: str, settings_path: Path) -> list[str]:
    # The prompt sits right after -p: --allowedTools is variadic and would swallow a trailing one.
    return [
        "claude", "-p", prompt,
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


def run_claude(model: str, prompt: str, *, role: str, cwd: Path, out_dir: Path) -> Session:
    # Absolute: the CLI runs in cwd, where a relative settings path would not resolve.
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    settings_path = out_dir / "settings.json"
    settings_path.write_text(json.dumps(claude_settings(role, cwd), indent=2), encoding="utf-8")
    argv = claude_argv(model, prompt, role, settings_path)
    (out_dir / "argv.json").write_text(json.dumps(argv, indent=2), encoding="utf-8")
    transcript = out_dir / "transcript.jsonl"
    proc = procs.run(argv, cwd=cwd, timeout=TIMEOUT_SECONDS[role], stdout_path=transcript,
                     stderr_path=out_dir / "stderr.log")
    return _finish(classify_claude(transcript, proc, role), cwd)


def run_codex(model: str, prompt: str, *, role: str, cwd: Path, out_dir: Path) -> Session:
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / "final.txt"
    argv = codex_argv(model, prompt, final_path)
    (out_dir / "argv.json").write_text(json.dumps(argv, indent=2), encoding="utf-8")
    transcript = out_dir / "transcript.jsonl"
    codex_home = Path(tempfile.mkdtemp(prefix="eval-codex-home-"))
    try:
        os.chmod(codex_home, 0o700)
        shutil.copy2(Path.home() / ".codex" / "auth.json", codex_home / "auth.json")
        (codex_home / "config.toml").write_text(f'model_reasoning_effort = "{CODEX_EFFORT}"\n', encoding="utf-8")
        env = {**os.environ, "CODEX_HOME": str(codex_home)}
        proc = procs.run(argv, cwd=cwd, timeout=TIMEOUT_SECONDS[role], stdout_path=transcript,
                         stderr_path=out_dir / "stderr.log", env=env)
    finally:
        shutil.rmtree(codex_home, ignore_errors=True)
    return _finish(classify_codex(transcript, final_path, proc, role, model), cwd)


def codex_argv(model: str, prompt: str, final_path: Path) -> list[str]:
    return [
        "codex", "--sandbox=read-only", "-m", model, "-c", f"model_reasoning_effort={CODEX_EFFORT}",
        "exec", "--skip-git-repo-check", "--ephemeral", "--json", "-o", str(final_path), prompt,
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
    if not hits:
        return session
    return Session(INFRA, "contamination: private path in a tool call", "", session.transcript,
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
