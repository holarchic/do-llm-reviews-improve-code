"""Prompt assembly for every model call in the eval.

Reviewer prompts are built from the fenced templates in the shipped
codex-review skill and the shipped rubric file, read as bytes at call time,
so the eval grades the product's text rather than a copy of it.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CODEX_REVIEW_SKILL = REPO_ROOT / "skills" / "codex-review" / "SKILL.md"
BUILD_LOOP_SKILL = REPO_ROOT / "skills" / "build-loop" / "SKILL.md"
RUBRIC = REPO_ROOT / "skills" / "build-loop" / "references" / "code-architecture-rules.md"

PLAN_PATH = "plans/plan.md"
CONTRACT_NOTE = (
    "## Contract\n\n"
    "TASK.md is the authoritative contract. SPEC.md and the plan are the builder's own documents and "
    "cannot weaken it: flag anything in them, or in the code, that contradicts or omits a TASK.md "
    "requirement.\n"
)

_WRITER_PREAMBLE = """You are working alone in a git repository (the current directory). TASK.md describes the task.

- Hidden tests will import exactly the interface TASK.md names; match module, function and exception names exactly.
- Use the `python3` on PATH and the standard library only. Write tests with `unittest` and run them with `python3 -m unittest`.
- Work only inside this directory. Do not commit: the harness commits when you finish.
"""


CODE_SECTION = "## Prompt assembly — code review"
PLAN_SECTION = "## Prompt assembly — plan review"


def fenced_block(markdown: str, heading: str, *, within: str | None = None) -> str:
    """Body of the first fenced block after the line equal to `heading`, searching
    from the `within` line when headings repeat across sections."""
    lines = markdown.splitlines()
    start = lines.index(heading, lines.index(within) if within else 0)
    opened = None
    body: list[str] = []
    for line in lines[start + 1:]:
        if opened is None:
            if line.startswith("```"):
                opened = line
            continue
        if line == "```":
            return "\n".join(body) + "\n"
        body.append(line)
    raise ValueError(f"no closed fenced block after {heading!r}")


def code_rules_section() -> str:
    """The build-loop skill's `## Code` section, the rules a builder keeps in context."""
    text = BUILD_LOOP_SKILL.read_text(encoding="utf-8")
    match = re.search(r"^## Code\n.*?(?=^## )", text, flags=re.MULTILINE | re.DOTALL)
    if match is None:
        raise ValueError("build-loop SKILL.md has no ## Code section")
    return match.group(0)


def rubric() -> str:
    return RUBRIC.read_text(encoding="utf-8")


def _skill() -> str:
    return CODEX_REVIEW_SKILL.read_text(encoding="utf-8")


def _with_rules(prompt: str) -> str:
    return (
        f"{prompt}\n# Coding rules\n\nFollow these rules; the reviewer grades against them.\n\n"
        f"{code_rules_section()}\n# Full rubric\n\n{rubric()}"
    )


def solo_writer() -> str:
    return _WRITER_PREAMBLE + "\nImplement TASK.md, with tests.\n"


def loop_plan_writer() -> str:
    return _with_rules(
        _WRITER_PREAMBLE
        + f"""
This step is planning only — write no code yet.

1. Write `SPEC.md`: objective, the exact public interface, every behaviour TASK.md requires, and your design decisions.
2. Write `{PLAN_PATH}` with sections: Goal, Approach, Assumptions (each with its risk if wrong), Tradeoffs considered,
   Open questions, and Chunks — each chunk a step with a `→ verify:` check.

A reviewer will review the plan before you build.
"""
    )


def loop_plan_revision_writer(verdict: str) -> str:
    return _with_rules(
        _WRITER_PREAMBLE
        + f"""
This step is planning only — write no code yet. A reviewer reviewed `{PLAN_PATH}` and SPEC.md:

<review>
{verdict.strip()}
</review>

Revise `{PLAN_PATH}` and SPEC.md to resolve every HIGH and MEDIUM item. Where you disagree with an item, keep
your design and record the reason under Open questions in the plan. TASK.md stays authoritative.
"""
    )


def loop_build_writer() -> str:
    return _with_rules(
        _WRITER_PREAMBLE
        + f"""
Build the plan in `{PLAN_PATH}`, chunk by chunk, to SPEC.md and TASK.md. Write tests for each chunk and run them.
Tick each chunk in the plan as it passes its check.
"""
    )


def loop_fix_writer(verdicts: dict[str, str]) -> str:
    reviews = "\n\n".join(f"<{name}-review>\n{text.strip()}\n</{name}-review>" for name, text in verdicts.items())
    return _with_rules(
        _WRITER_PREAMBLE
        + f"""
Reviewers reviewed your build:

{reviews}

Fix every HIGH and MEDIUM issue. Check each claim against the code before changing it; where a finding is wrong,
leave the code and record why in a Log section at the end of `{PLAN_PATH}`. Keep SPEC.md in step with any contract
change. Run the tests.
"""
    )


def followup_writer() -> str:
    return (
        _WRITER_PREAMBLE.replace("TASK.md describes the task.", "TASK.md describes what the code does today.")
        + "\nFOLLOWUP.md describes a change to the existing code. Make the change, keep everything TASK.md requires "
        "working, and update the tests.\n"
    )


def plan_review() -> str:
    skill = _skill()
    header = fenced_block(skill, "### 1. Header", within=PLAN_SECTION)
    header = header.replace("<plan file path>", PLAN_PATH).replace("<path/to/plan.md>", PLAN_PATH)
    header = header.replace(
        "<1-3 sentences: what the plan is for, what triggered it, what phase it follows.>",
        "Plan for building the task in TASK.md in an empty repository (first phase).",
    )
    context = (
        "## Context\n\n- **What this plan changes:** see the plan's Goal and Approach.\n"
        "- **Explicit non-goals:** N/A\n- **Preconditions:** N/A\n"
    )
    assumptions = "## Assumptions and tradeoffs\n\nSee the plan's Assumptions and Tradeoffs sections.\n"
    questions = "## Open questions for review\n\nSee the plan's Open questions section.\n"
    return "\n".join([
        header, CONTRACT_NOTE, context, assumptions, questions,
        fenced_block(skill, "### 5. Plan rubric"),
        fenced_block(skill, "### 6. Verdict format"),
    ])


def code_review(pass_name: str, base: str, *, commits: str, final_state: str) -> str:
    """Correctness ("correctness") or architecture ("architecture") pass over base..HEAD."""
    skill = _skill()
    commands = f"git diff {base}..HEAD\ngit log --oneline {base}..HEAD"
    what = fenced_block(skill, "### 0. What to review")
    what = what.replace("<diff-fetch commands for the scope>", commands)
    what = re.sub(r"\n<For --uncommitted:.*?>\n", "\n", what)
    if pass_name == "architecture":
        what += "\nThen read each module the diff touches in full, not just the changed lines.\n"
    profile_heading = {"correctness": "### Profile A — Correctness (precision)",
                       "architecture": "### Profile B — Architecture (recall)"}[pass_name]
    header = fenced_block(skill, "### 1. Header", within=CODE_SECTION)
    header = header.replace("<feature/phase/chunk name>", "the TASK.md build").replace(
        "<1-3 sentences: what was implemented, plan reference, prior plan-review verdict if any.>",
        f"The task in TASK.md, built to SPEC.md and `{PLAN_PATH}`.",
    )
    sections = [
        what,
        header,
        f"## Commits to review\n\n{commits.strip() or 'None'}\n",
        f"## Final state\n\n{final_state.strip()}\n",
        "## Load-bearing claims to verify\n\nN/A\n",
        "## Specific concerns (fresh-eye spots)\n\nN/A\n",
        "## Hard constraints\n\n- Every TASK.md interface name and behaviour holds.\n",
        CONTRACT_NOTE,
        rubric(),
        fenced_block(skill, profile_heading),
        fenced_block(skill, "### 8. Verdict format"),
    ]
    return "\n".join(sections)
