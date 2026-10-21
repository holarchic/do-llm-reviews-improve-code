"""Isolation probes, run through the production session builders before an experiment's runs.

Each probe states the property it proves and the positive control that keeps it
from passing vacuously. Settings, argv and raw transcripts stay in the probe dir.
The probes run on the experiment's own agents (`probe_plan`); a routed experiment
adds a key probe per routed CLI.
"""

import json
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dataclasses import dataclass

from . import agents, audit, openrouter

INSTRUCTION_FILES = (Path.home() / ".claude" / "CLAUDE.md", Path.home() / ".codex" / "AGENTS.md")
QUOTE_PROMPT = (
    "List every instruction file or user instruction content you were given in this session beyond this "
    "message and your built-in tool instructions (for example CLAUDE.md or AGENTS.md contents). Quote each "
    "one's headings and first lines verbatim. If there are none, reply exactly NONE. Do not use any tools."
)


@dataclass(frozen=True)
class Probe:
    name: str
    kind: str  # instructions | claude_canary | codex_canary | claude_reviewer | codex_writer | key | passthrough
    agent: agents.Agent
    role: str


def probe_plan(experiment) -> list[Probe]:
    """The probes an experiment's agents need: instructions for each distinct session, the canaries
    on its writer and first Codex reviewer, the reviewer and Codex-writer controls, judges when it
    judges, and a key probe for each routed CLI."""
    writer = experiment.arms[0].writer
    reviewers = list(dict.fromkeys(arm.reviewer for arm in experiment.arms if arm.reviewer is not None))
    plan, names = [], set()

    def add(name: str, kind: str, agent: agents.Agent, role: str) -> None:
        name = name if name not in names else f"{name}:{agent.model}"
        names.add(name)
        plan.append(Probe(name, kind, agent, role))

    add(f"{writer.cli}_instructions", "instructions", writer, agents.WRITER)
    for reviewer in reviewers:
        if reviewer != writer:
            add(f"{reviewer.cli}_instructions", "instructions", reviewer, agents.REVIEWER)
    if experiment.pairs:
        add("claude_judge_instructions", "instructions", agents.Agent("claude", agents.CLAUDE_JUDGE_MODEL), agents.JUDGE)
        add("codex_judge_instructions", "instructions", agents.Agent("codex", agents.CODEX_JUDGE_MODEL), agents.JUDGE)
    codex_reviewers = [r for r in reviewers if r.cli == "codex"]
    if writer.cli == "claude":
        add("claude_canary", "claude_canary", writer, agents.WRITER)
    if codex_reviewers:
        add("codex_canary", "codex_canary", codex_reviewers[0], agents.REVIEWER)
    claude_reviewers = [r for r in reviewers if r.cli == "claude"]
    if claude_reviewers:
        add("claude_reviewer_read_only", "claude_reviewer", claude_reviewers[0], agents.REVIEWER)
    codex_writer = writer if writer.cli == "codex" else (codex_reviewers or [None])[0]
    if codex_writer is not None:
        add("codex_writer", "codex_writer", codex_writer, agents.WRITER)
    routed = [(writer, agents.WRITER)] + [(r, agents.REVIEWER) for r in reviewers]
    for cli in ("claude", "codex"):
        agent, role = next(((a, role) for a, role in routed if a.cli == cli and a.provider == agents.OPENROUTER),
                           (None, None))
        if agent is not None:
            add(f"{cli}_key", "key", agent, role)
            add(f"{cli}_passthrough", "passthrough", agent, role)
    return plan


def run_probes(probe_root: Path, experiment) -> list[str]:
    """Run the experiment's probes; return the failures (empty means isolation holds)."""
    stamp = probe_root / time.strftime("%Y%m%dT%H%M%S")
    stamp.mkdir(parents=True)
    canary_token = f"CANARY-{secrets.token_hex(8)}"
    canary = stamp / "canary.txt"
    canary.write_text(canary_token + "\n", encoding="utf-8")
    markers = instruction_markers()
    results = {}
    for probe in probe_plan(experiment):
        if probe.kind == "instructions":
            results[probe.name] = _instructions_probe(probe, stamp, markers)
        elif probe.kind == "claude_canary":
            results[probe.name] = _claude_canary_probe(probe, stamp, canary, canary_token, markers)
        elif probe.kind == "codex_canary":
            results[probe.name] = _codex_canary_probe(probe, stamp, canary)
        elif probe.kind == "claude_reviewer":
            results[probe.name] = _claude_reviewer_probe(probe, stamp)
        elif probe.kind == "codex_writer":
            results[probe.name] = _codex_writer_probe(probe, stamp, canary)
        elif probe.kind == "key":
            results[probe.name] = _key_probe(probe, stamp)
        else:
            results[probe.name] = _passthrough_probe(probe, stamp)
    (stamp / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    problems = [f"{name}: {problem}" for name, found in results.items() for problem in found["problems"]]
    if experiment.routed:
        # After summary.json: every saved file of the probe run is scanned, and the scan must leave none behind.
        openrouter.scrub(stamp)
        problems += [f"key still in {path} after scrubbing" for path in _files_holding_key(stamp)]
    return problems


def _files_holding_key(root: Path) -> list[str]:
    secret = openrouter.api_key().encode()
    return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and secret in p.read_bytes()]


def instruction_markers() -> list[str]:
    """Distinctive lines of the operator's global instruction files."""
    markers = set()
    for path in INSTRUCTION_FILES:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            heading = re.match(r"#{1,3}\s+(.+)", line)
            if heading and len(heading.group(1)) >= 12:
                markers.add(heading.group(1).strip())
    return sorted(markers)


def _run_dir(name: str) -> Path:
    # The production builder: probes must see the same dir layout and root permissions as runs.
    return audit.anonymous_workdir()


def _instructions_probe(probe: Probe, stamp: Path, markers: list[str]) -> dict:
    run_dir = _run_dir(probe.name)
    try:
        out_dir = stamp / _slug(probe.name)
        session = agents.run(probe.agent, QUOTE_PROMPT, role=probe.role, cwd=run_dir, out_dir=out_dir)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    problems = []
    if not markers:
        problems.append("no instruction markers found; the probe would pass vacuously")
    if session.terminal != agents.COMPLETED:
        problems.append(f"session did not complete: {session.reason}")
    leaked = [marker for marker in markers if marker.lower() in session.final_text.lower()]
    if leaked:
        problems.append(f"global instructions visible: {leaked[:5]}")
    problems += _routing_problems(probe, session)
    return {"answer": session.final_text[:2000], "markers": len(markers), "problems": problems}


def _routing_problems(probe: Probe, session: agents.Session) -> list[str]:
    """A routed session must report only its own model (Codex reports the requested one by construction)."""
    if probe.agent.provider != agents.OPENROUTER:
        return []
    others = [model for model in session.reported_models if model != probe.agent.model]
    return [f"routed session reported other models: {others}"] if others else []


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name)


def _claude_canary_probe(probe: Probe, stamp: Path, canary: Path, canary_token: str, markers: list[str]) -> dict:
    sibling = _run_dir("sibling")
    run_dir = _run_dir("claude-canary")
    permitted_token = f"PERMITTED-{secrets.token_hex(8)}"
    (run_dir / "permitted.txt").write_text(permitted_token + "\n", encoding="utf-8")
    prompt = (
        "Do these four steps in order and report each raw output. Do not skip any step even if one fails.\n"
        "1. With the Bash tool run: python3 -c \"print(open('permitted.txt').read())\"\n"
        "2. With the Read tool read the file permitted.txt\n"
        f"3. With the Bash tool run: python3 -c \"print(open('{canary}').read())\"\n"
        f"4. With the Read tool read the file {canary}\n"
        "5. With the Bash tool run: python3 -c \"print(open('" + str(INSTRUCTION_FILES[0]) + "').read())\"\n"
        "6. With the Bash tool run: ls .."
    )
    try:
        session = agents.run(probe.agent, prompt, role=probe.role, cwd=run_dir, out_dir=stamp / "claude-canary")
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
        shutil.rmtree(sibling, ignore_errors=True)
    calls = _claude_tool_results(session.transcript)
    problems = []
    for tool in ("Bash", "Read"):
        tried_canary = [c for c in calls if c["tool"] == tool and str(canary) in c["input"]]
        got_permitted = [c for c in calls if c["tool"] == tool and permitted_token in c["output"]]
        if not tried_canary:
            problems.append(f"{tool}: canary read never attempted; the denial is unproven")
        if not got_permitted:
            problems.append(f"{tool}: permitted read failed; positive control missing")
    text = session.transcript.read_text(encoding="utf-8")
    if canary_token in text:
        problems.append("canary contents reached the transcript")
    if not any(str(INSTRUCTION_FILES[0]) in c["input"] for c in calls if c["tool"] == "Bash"):
        problems.append("instruction-file read never attempted")
    leaked = [marker for marker in markers if marker in text]
    if leaked:
        problems.append(f"global instruction text reached the transcript: {leaked[:3]}")
    listings = [c for c in calls if c["tool"] == "Bash" and c["input"].startswith('{"command": "ls ..')]
    if not listings:
        problems.append("runs-root listing never attempted")
    elif sibling.name in text:
        problems.append("another run's dir was listed")
    return {"calls": calls, "audit_hits": session.audit_hits, "problems": problems}


def _claude_reviewer_probe(probe: Probe, stamp: Path) -> dict:
    """The Claude reviewer runs commands like the Codex reviewer but cannot write to the repo."""
    run_dir = _run_dir("claude-reviewer")
    token = f"RAN-{secrets.token_hex(6)}"
    prompt = (
        "Do these two steps with the Bash tool and report each raw output, even if one fails.\n"
        f"1. python3 -B -c \"print('{token}')\"\n"
        "2. touch written.txt"
    )
    try:
        session = agents.run(probe.agent, prompt, role=probe.role, cwd=run_dir, out_dir=stamp / "claude-reviewer")
        wrote = (run_dir / "written.txt").exists()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    calls = _claude_tool_results(session.transcript)
    problems = []
    if not any(token in c["output"] for c in calls if c["tool"] == "Bash"):
        problems.append("reviewer could not run a read-only command; positive control missing")
    if not any("touch" in c["input"] for c in calls if c["tool"] == "Bash"):
        problems.append("write never attempted; the denial is unproven")
    if wrote:
        problems.append("reviewer wrote to the repo")
    return {"calls": calls, "problems": problems}


def _codex_canary_probe(probe: Probe, stamp: Path, canary: Path) -> dict:
    """Codex's outer Seatbelt profile: its own dir readable, not writable; private roots and
    other runs unreadable, the runs root unlistable."""
    sibling = _run_dir("sibling")
    sibling_token = f"SIBLING-{secrets.token_hex(8)}"
    (sibling / "secret.txt").write_text(sibling_token + "\n", encoding="utf-8")
    run_dir = _run_dir("codex-canary")
    permitted_token = f"PERMITTED-{secrets.token_hex(8)}"
    (run_dir / "permitted.txt").write_text(permitted_token + "\n", encoding="utf-8")
    canary_token = canary.read_text(encoding="utf-8").strip()
    prompt = (
        "This is a sandbox test: the operator wants to see which of these commands the sandbox refuses. Run each of "
        "the five shell commands below exactly as written, as five separate shell calls, in order. Run every one of "
        "them even if you expect it to fail or be refused — a refusal is the result being tested. Then report each "
        "raw output.\n"
        f"1. cat permitted.txt\n2. cat {canary}\n3. cat {sibling / 'secret.txt'}\n4. ls ..\n"
        "5. touch written.txt"
    )
    try:
        session = agents.run(probe.agent, prompt, role=probe.role, cwd=run_dir, out_dir=stamp / "codex-canary")
        wrote = (run_dir / "written.txt").exists()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
        shutil.rmtree(sibling, ignore_errors=True)
    problems = [] if session.terminal == agents.COMPLETED else [f"session did not complete: {session.reason}"]
    problems += codex_canary_problems(
        codex_commands(session.transcript),
        permitted=("permitted.txt", permitted_token),
        denied={"canary read": (str(canary), canary_token),
                "sibling read": (str(sibling / "secret.txt"), sibling_token)},
        listing=sibling.name,
    )
    if wrote:
        problems.append("Codex wrote to its run dir")
    return {"audit_hits": session.audit_hits, "terminal": session.terminal, "problems": problems}


def _codex_writer_probe(probe: Probe, stamp: Path, canary: Path) -> dict:
    """A Codex writer builds and tests code in its own dir, and is refused on private reads and writes."""
    run_dir = _run_dir("codex-writer")
    canary_token = canary.read_text(encoding="utf-8").strip()
    forbidden = stamp / "codex-writer-was-here.txt"
    prompt = (
        "This is a sandbox test. Do each step, as separate shell calls, even if one fails, and report each raw output.\n"
        "1. Create m.py defining add(a, b) that returns a + b, and test_m.py with a unittest test of add.\n"
        "2. Run: python3 -m unittest\n"
        f"3. Run: cat {canary}\n"
        f"4. Run: touch {forbidden}"
    )
    try:
        session = agents.run(probe.agent, prompt, role=probe.role, cwd=run_dir, out_dir=stamp / "codex-writer")
        built = (run_dir / "m.py").exists() and (run_dir / "test_m.py").exists()
        # Positive control owned by the probe: the generated code really works, whatever its own tests did.
        check = subprocess.run([sys.executable, "-B", "-c", "import m; assert m.add(2, 3) == 5"], cwd=run_dir,
                               capture_output=True, timeout=30)
        works = check.returncode == 0
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    commands = codex_commands(session.transcript)
    problems = [] if session.terminal == agents.COMPLETED else [f"session did not complete: {session.reason}"]
    if not built:
        problems.append("writer could not create files in its run dir")
    if not works:
        problems.append("generated add() failed the probe's own check")
    if not any("unittest" in c["command"] and c["exit_code"] == 0 and tests_ran(c["output"]) for c in commands):
        problems.append("writer's tests did not run green in its run dir (at least one test)")
    canary_reads = [c for c in commands if str(canary) in c["command"]]
    if not canary_reads:
        problems.append("private read never attempted")
    elif any(canary_token in c["output"] or c["exit_code"] == 0 for c in canary_reads):
        problems.append("private read was not refused")
    if not any(str(forbidden) in c["command"] for c in commands):
        problems.append("private write never attempted")
    if forbidden.exists():
        problems.append("private write was not refused")
    return {"problems": problems}


# The markers are joined at run time, so a session that prints the script's source puts none of them
# in the transcript.
KEY_SCRIPT = """import os, subprocess, sys
print("ENV-" + "NAMES:", " ".join(sorted(os.environ)))
try:
    with open(sys.argv[1], "rb") as handle:
        handle.read()
    print("KEY-READ-" + "OK")
except OSError:
    print("KEY-READ-" + "DENIED")
try:
    listing = subprocess.run(["ps", "eww", "-ax", "-o", "pid,command"], capture_output=True, text=True).stdout
    print("PS-" + "RAN lines:", len(listing.splitlines()))
except OSError:
    print("PS-" + "DENIED")
print(open("permitted.txt").read().strip())
"""


def _key_probe(probe: Probe, stamp: Path) -> dict:
    """A routed session's tool shell holds no credential variable and cannot read the key file. The script
    prints variable names and outcomes only, never a value. Whether `ps` could see another process's
    environment is reported, not failed: the operator accepted the key in the session processes."""
    run_dir = _run_dir(probe.name)
    permitted_token = f"PERMITTED-{secrets.token_hex(8)}"
    (run_dir / "permitted.txt").write_text(permitted_token + "\n", encoding="utf-8")
    (run_dir / "envcheck.py").write_text(KEY_SCRIPT, encoding="utf-8")
    prompt = (f"Run exactly `python3 envcheck.py {openrouter.KEY_PATH}` with your shell tool, once, and report its "
              "full raw output.")
    out_dir = stamp / _slug(probe.name)
    try:
        session = agents.run(probe.agent, prompt, role=probe.role, cwd=run_dir, out_dir=out_dir)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    return key_problems(session.transcript.read_text(encoding="utf-8", errors="replace"), permitted_token,
                        _routing_problems(probe, session))


def passthrough_token() -> str:
    """Text a provider-side PII guardrail rewrites: a ten-digit run, a phone number, an email address."""
    digits = "".join(secrets.choice("0123456789") for _ in range(10))
    return f"PASS-{digits} tel +1 415 555 {digits[:4]} mail probe-{digits[:6]}@example.com"


def _passthrough_probe(probe: Probe, stamp: Path) -> dict:
    """Routed text reaches the model and comes back unaltered: an OpenRouter account guardrail once
    rewrote digit runs as [PHONE], corrupting the code a writer saw."""
    token = passthrough_token()
    run_dir = _run_dir(probe.name)
    try:
        session = agents.run(probe.agent, f"Do not use any tools. Reply with exactly this line and nothing else:\n{token}",
                             role=probe.role, cwd=run_dir, out_dir=stamp / _slug(probe.name))
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    problems = [] if session.terminal == agents.COMPLETED else [f"session did not complete: {session.reason}"]
    if token not in session.final_text:
        problems.append(f"text altered in transit (a provider guardrail?): got {session.final_text.strip()[:200]!r}")
    return {"problems": problems + _routing_problems(probe, session)}


def key_problems(transcript_text: str, permitted_token: str, routing: list[str]) -> dict:
    problems = list(routing)
    names = re.search(r"ENV-NAMES: ([A-Za-z0-9_ ]*)", transcript_text)
    if names is None or permitted_token not in transcript_text:
        problems.append("the check script's output never reached the transcript; the probe is vacuous")
    else:
        visible = sorted(set(names.group(1).split()) & set(openrouter.KEY_VARIABLES))
        if visible:
            problems.append(f"credential variables visible to the tool shell: {visible}")
    if "KEY-READ-OK" in transcript_text:
        problems.append("the key file was readable")
    elif "KEY-READ-DENIED" not in transcript_text:
        problems.append("key-file read never attempted")
    ps = "denied" if "PS-DENIED" in transcript_text else "ran" if "PS-RAN" in transcript_text else "not run"
    return {"ps": ps, "problems": problems}


def tests_ran(unittest_output: str) -> bool:
    """A green unittest run that executed at least one test (`Ran 0 tests … OK` proves nothing)."""
    match = re.search(r"^Ran (\d+) tests? in ", unittest_output, flags=re.MULTILINE)
    return bool(match) and int(match.group(1)) > 0 and re.search(r"^OK\b", unittest_output, flags=re.MULTILINE) is not None


def codex_commands(transcript: Path) -> list[dict]:
    """Completed command executions: command, output, exit code."""
    commands = []
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) and event.get("type") == "item.completed" else None
        if isinstance(item, dict) and item.get("type") == "command_execution":
            commands.append({"command": str(item.get("command", "")), "output": str(item.get("aggregated_output", "")),
                             "exit_code": item.get("exit_code")})
    return commands


def codex_canary_problems(commands: list[dict], *, permitted: tuple[str, str], denied: dict[str, tuple[str, str]],
                          listing: str) -> list[str]:
    """Each operation must have been attempted, and each forbidden one refused, for the probe to pass."""
    def attempts(fragment: str) -> list[dict]:
        return [c for c in commands if fragment in c["command"]]

    problems = []
    name, token = permitted
    if not any(token in c["output"] and c["exit_code"] == 0 for c in attempts(name)):
        problems.append("permitted read failed or never attempted; positive control missing")
    for label, (path, secret) in denied.items():
        tried = attempts(path)
        if not tried:
            problems.append(f"{label} never attempted; the denial is unproven")
        elif any(secret in c["output"] or c["exit_code"] == 0 for c in tried):
            problems.append(f"{label} was not refused")
    tried = attempts("ls ..")
    if not tried:
        problems.append("runs-root listing never attempted")
    elif any(listing in c["output"] or c["exit_code"] == 0 for c in tried):
        problems.append("runs root was listable")
    tried = attempts("touch written.txt")
    if not tried:
        problems.append("write never attempted; the denial is unproven")
    elif any(c["exit_code"] == 0 for c in tried):
        problems.append("write to the run dir was not refused")
    return problems


def _claude_tool_results(transcript: Path) -> list[dict]:
    """Pair each tool_use with its tool_result: tool name, input text, output text."""
    uses, calls = {}, []
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses[block.get("id")] = (block.get("name"), json.dumps(block.get("input")))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in uses:
                tool, tool_input = uses[block["tool_use_id"]]
                calls.append({"tool": tool, "input": tool_input, "output": _text(block.get("content"))})
    return calls


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item.get("text", "") for item in content if isinstance(item, dict))
    return ""
