"""The v1 replay's addendum, from its raw run records.

Post hoc: nothing here was pre-registered and none of it enters a verdict. It reads what the replay's
frozen reports leave out: which review blocked in each round, how many findings each review raised, how
much code each fix changed, where the reviewed arm stood before any code review, and every defect's
outcome by build. `collect` reads records into plain values and refuses any that don't reproduce the
frozen report, the functions under it derive each table, and `render` is the only place numbers
become text.

    python3 -B -m eval.results.replay_addendum     (from the repo root, with v1's records in eval/data/replay)
"""

import itertools
import json
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median, stdev

from eval.harness import loop, pilot, prompts, records

EVAL_ROOT = Path(__file__).resolve().parents[1]
DATA = EVAL_ROOT / "data" / "replay"
OUT = Path(__file__).with_name("replay-addendum.md")
TASKS = ("jobscan-email", "jobscan-greenhouse", "jobscan-workable-gem", "jobscan-setup-commands",
         "jobscan-watchlist", "jobscan-company-notes")
SOLO, REVIEWED = "A1", "A3"
REPS = (1, 2)
STAGES = ("plan", "correctness", "architecture")
# The frozen report's own figures: records that don't reproduce them are not v1's.
REPORTED_REVIEW = {SOLO: 44.6, REVIEWED: 72.1}
REPORTED_CAPS, REPORTED_TIMEOUTS = 14, 2
BASES = {"TASK.md": "a TASK.md statement", "norm": "a norm", "existing behaviour": "existing behaviour"}
_REVIEW_STEP = re.compile(r"(plan|correctness|architecture)-review-(\d+)")
_GIT_ENV = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1"}


@dataclass(frozen=True)
class Review:
    stage: str
    round: int
    blocking: bool
    findings: int  # HIGH or MEDIUM, as the loop counted them: one without a basis is LOW


@dataclass(frozen=True)
class Round:
    """A reviewed build after `build` (round 0) or after `fix-k` (round k), as `rounds` re-scored it."""
    number: int
    review: float | None
    feature: float
    regressions: float
    source_lines: int  # added plus deleted by the fix that made this round; 0 at round 0
    test_lines: int


@dataclass(frozen=True)
class Build:
    task: str
    arm: str
    rep: int
    flags: tuple[str, ...]
    review: float | None  # None for a control
    avoided: dict[str, bool]  # defect id -> avoided
    reviews: tuple[Review, ...]
    rounds: tuple[Round, ...]


@dataclass(frozen=True)
class Defect:
    task: str
    id: str
    basis: str  # a key of BASES
    solo: int  # builds, of the replicates, that avoided it
    reviewed: int


def collect() -> list[Build]:
    digest = json.loads((EVAL_ROOT / "manifests" / "replay.json").read_text(encoding="utf-8"))["digest"]
    basis_rule = prompts.basis_required()
    builds = []
    for task, arm, rep in itertools.product(TASKS, (SOLO, REVIEWED), REPS):
        exec_dir = pilot.complete_build(DATA / "runs", task, arm, digest, rep)
        if exec_dir is None:
            raise SystemExit(f"no complete build of {task} {arm} r{rep} under manifest {digest[:12]}")
        record = records.build(exec_dir / "record.json", digest)
        replay = record["scores"]["replay"]
        builds.append(Build(task=task, arm=arm, rep=rep, flags=tuple(record["flags"]), review=replay["review"],
                            avoided={d["id"]: d["avoided"] for d in replay["defects"]},
                            reviews=_reviews(record, exec_dir, basis_rule), rounds=_rounds(record, exec_dir)))
    _check(builds)
    return builds


def _reviews(record: dict, exec_dir: Path, basis_rule: bool) -> tuple[Review, ...]:
    reviews = []
    for index, step in enumerate(record["steps"]):
        match = _REVIEW_STEP.fullmatch(step["name"])
        if match is None or "verdict" not in step:  # a review that timed out left no verdict
            continue
        final = loop.attempt_dir(exec_dir, index, step["name"], len(step["attempts"])) / "final.txt"
        verdict = loop.parse_verdict(final.read_text(encoding="utf-8"), basis_rule=basis_rule)
        if (verdict.label, verdict.blocking) != (step["verdict"]["label"], step["verdict"]["blocking"]):
            raise SystemExit(f"{record['run_id']} {step['name']}: this parser reads the verdict differently "
                             "from the run, so its finding counts are not the run's")
        reviews.append(Review(match.group(1), int(match.group(2)), verdict.blocking,
                              sum(issue.effective != "LOW" for issue in verdict.issues)))
    return tuple(reviews)


def _rounds(record: dict, exec_dir: Path) -> tuple[Round, ...]:
    root = DATA / "rounds" / record["run_id"] / record["exec"]
    rounds, previous = [], None
    for number in itertools.count():
        try:
            stored = json.loads((root / f"round-{number}" / "score.json").read_text(encoding="utf-8"))
        except FileNotFoundError:  # past the build's last round; a solo build has none
            break
        replay = stored["scores"]["replay"]
        source, tests = (0, 0) if previous is None else _lines_changed(exec_dir / "repo", previous, stored["commit"])
        rounds.append(Round(number, replay["review"], replay["feature"], replay["regressions"], source, tests))
        previous = stored["commit"]
    return tuple(rounds)


def _lines_changed(repo: Path, before: str, after: str) -> tuple[int, int]:
    """Lines added plus deleted between two commits, as (source, tests). Documents and the plan are
    left out: a fix round rewrites them whether or not it touches code."""
    numstat = subprocess.run(["git", "diff", "--numstat", before, after], cwd=repo, env=_GIT_ENV,
                             capture_output=True, text=True, check=True).stdout
    source = tests = 0
    for line in numstat.splitlines():
        added, deleted, path = line.split("\t", 2)
        if added == "-" or path.endswith(".md") or path.startswith("plans/"):
            continue
        changed = int(added) + int(deleted)
        if path.startswith("tests/") or Path(path).name.startswith("test_"):
            tests += changed
        else:
            source += changed
    return source, tests


def _check(builds: list[Build]) -> None:
    for arm, reported in REPORTED_REVIEW.items():
        if round(arm_review(builds, arm), 1) != reported:
            raise SystemExit(f"{arm} review mean {arm_review(builds, arm):.1f}, the frozen report has {reported}")
    caps = sum(flag.endswith("_capped") for b in builds for flag in b.flags)
    timeouts = sum(flag.startswith("timeout:") for b in builds for flag in b.flags)
    if (caps, timeouts) != (REPORTED_CAPS, REPORTED_TIMEOUTS):
        raise SystemExit(f"{caps} caps and {timeouts} timeouts, the frozen report has "
                         f"{REPORTED_CAPS} and {REPORTED_TIMEOUTS}")
    for b in of(builds, REVIEWED):
        if not b.rounds or b.rounds[-1].review != b.review:
            raise SystemExit(f"{b.task} {b.arm} r{b.rep}: its last scored round is not the build the record scored")


def of(builds: list[Build], arm: str, task: str | None = None) -> list[Build]:
    return [b for b in builds if b.arm == arm and task in (None, b.task)]


def caught(builds: list[Build]) -> list[str]:
    return [task for task in TASKS if of(builds, SOLO, task)[0].review is not None]


def task_review(builds: list[Build], arm: str, task: str, *, at_build: bool = False) -> float:
    """Defects avoided, in points: the task mean over replicates, at the end or at the `build` commit."""
    return mean(100 * (b.rounds[0].review if at_build else b.review) for b in of(builds, arm, task))


def arm_review(builds: list[Build], arm: str, *, at_build: bool = False) -> float:
    return mean(task_review(builds, arm, task, at_build=at_build) for task in caught(builds))


def reviews_at(builds: list[Build], stage: str, number: int | None = None) -> list[Review]:
    return [r for b in of(builds, REVIEWED) for r in b.reviews if r.stage == stage and number in (None, r.round)]


def capped(builds: list[Build]) -> list[Build]:
    return [b for b in of(builds, REVIEWED) if "review_capped" in b.flags]


def moved(build: Build, number: int) -> bool:
    before, after = build.rounds[number - 1], build.rounds[number]
    return (before.review, before.feature, before.regressions) != (after.review, after.feature, after.regressions)


def gains(builds: list[Build]) -> list[tuple[Build, int]]:
    """Each (build, round) where a fix raised defects avoided."""
    return [(b, r.number) for b in of(builds, REVIEWED) if b.review is not None
            for r in b.rounds[1:] if r.review > b.rounds[r.number - 1].review]


def blocked(build: Build, stage: str, number: int) -> bool:
    return any(r.blocking for r in build.reviews if r.stage == stage and r.round == number)


def defects(builds: list[Build]) -> list[Defect]:
    rows = []
    for task in caught(builds):
        listed = json.loads((EVAL_ROOT / "tasks" / task / "defects.json").read_text(encoding="utf-8"))["defects"]
        for d in listed:
            basis = next(key for key in BASES if d["basis"].startswith(key))
            rows.append(Defect(task, d["id"], basis, *(sum(b.avoided[d["id"]] for b in of(builds, arm, task))
                                                       for arm in (SOLO, REVIEWED))))
    return rows


def agreeing(builds: list[Build], arm: str) -> int:
    """Defects on which the arm's replicates had the same outcome."""
    return sum(len({b.avoided[d.id] for b in of(builds, arm, d.task)}) == 1 for d in defects(builds))


def sign_flip_p(differences: list[float]) -> float:
    """Exact and one-sided: the share of sign assignments whose total is at least the observed one."""
    sizes = [abs(d) for d in differences if d]
    totals = (sum(sign * size for sign, size in zip(signs, sizes))
              for signs in itertools.product((1, -1), repeat=len(sizes)))
    return sum(total >= sum(differences) - 1e-9 for total in totals) / 2 ** len(sizes)


def render(builds: list[Build]) -> str:
    return "\n".join(_head(builds) + _verdict_lines(builds) + _finding_lines(builds) + _fix_lines(builds)
                     + _stage_lines(builds) + _defect_lines(builds) + _firmness_lines(builds) + _reading(builds))


def _head(builds: list[Build]) -> list[str]:
    figures = " and ".join(f"{REPORTED_REVIEW[arm]}" for arm in (SOLO, REVIEWED))
    return [
        "# Replay: addendum from the run records", "",
        "Not pre-registered, and no part of a verdict. The verdict in `replay-report.md` stands as the "
        "pre-registered rule gives it: **no effect**. This file reports what the run records hold and the "
        "frozen reports leave out.", "",
        f"- Builds: {len(builds)} ({SOLO} and {REVIEWED}, {len(TASKS)} tasks, {len(REPS)} replicates), the ones "
        "`replay-report.md` scored.",
        "- Command: `python3 -B -m eval.results.replay_addendum`. It refuses records that don't reproduce the "
        f"report's review means ({figures}), its {REPORTED_CAPS} round caps and {REPORTED_TIMEOUTS} timeouts, "
        "or whose reviews today's verdict parser reads differently from the run.",
        f"- Every table is {REVIEWED}'s unless it names {SOLO}: a solo build has no reviews and no rounds.", ""]


def _verdict_lines(builds: list[Build]) -> list[str]:
    last = max(r.round for b in of(builds, REVIEWED) for r in b.reviews)
    rows = []
    for stage in STAGES:
        cells = [_share(reviews_at(builds, stage, number)) for number in range(1, last + 1)]
        rows.append([stage, *cells, _share(reviews_at(builds, stage))])
    ended, controls = capped(builds), [b for b in of(builds, REVIEWED) if b.review is None]
    held = {stage: sum(blocked(b, stage, last) for b in ended) for stage in ("correctness", "architecture")}
    return [
        "## Which review blocked, by round", "",
        "Blocking reviews over reviews run. A review blocks when its verdict is not GREEN and it raises a HIGH "
        "or MEDIUM finding that cites a basis. The correctness and architecture reviews are the two passes of "
        "each code-review round.", "",
        *_table(["Review", *[f"Round {n}" for n in range(1, last + 1)], "All"], rows), "",
        f"- {len(ended)} builds ran code review to its round cap. In round {last} the architecture review blocked "
        f"in {held['architecture']} of them and the correctness review in {held['correctness']}.",
        f"- {sum(b in ended for b in controls)} of the {len(controls)} control builds are among them, though a "
        "control's phase carries no review-caught defect.", ""]


def _finding_lines(builds: list[Build]) -> list[str]:
    last = max(r.round for b in of(builds, REVIEWED) for r in b.reviews)
    rows = [[stage, *[_spread([r.findings for r in reviews_at(builds, stage, n)]) for n in range(1, last + 1)]]
            for stage in STAGES]
    return ["## Findings per review", "",
            "HIGH and MEDIUM findings citing a basis, per review: median [lowest–highest].", "",
            *_table(["Review", *[f"Round {n}" for n in range(1, last + 1)]], rows), ""]


def _fix_lines(builds: list[Build]) -> list[str]:
    rows = []
    for number in range(1, max(len(b.rounds) for b in of(builds, REVIEWED))):
        fixed = [b for b in of(builds, REVIEWED) if len(b.rounds) > number]
        rows.append([f"`fix-{number}`", str(len(fixed)), _spread([b.rounds[number].source_lines for b in fixed]),
                     _spread([b.rounds[number].test_lines for b in fixed]),
                     str(sum(moved(b, number) for b in fixed))])
    return ["## What each fix changed", "",
            "Lines added plus deleted by a fix, per build, as median [lowest–highest]; `plans/` and markdown are "
            "left out. A build's scores moved when its defects avoided, feature tests passed or existing tests "
            "broken differ from the round before.", "",
            *_table(["Fix", "Builds", "Source lines", "Test lines", "Builds whose scores moved"], rows), ""]


def _stage_lines(builds: list[Build]) -> list[str]:
    columns = ((SOLO, False), (REVIEWED, True), (REVIEWED, False))
    rows = [[task, *[f"{task_review(builds, arm, task, at_build=at):.1f}" for arm, at in columns]]
            for task in caught(builds)]
    rows.append(["Arm mean", *[f"{arm_review(builds, arm, at_build=at):.1f}" for arm, at in columns]])
    early = [f"{b.task} r{b.rep} ({next(f for f in b.flags if f.startswith('timeout:')).split(':')[1]})"
             for b in of(builds, REVIEWED) if any(f.startswith("timeout:") for f in b.flags)]
    return ["## Where the reviewed arm stood before code review", "",
            f"Review-caught defects avoided (%), task mean over replicates. {REVIEWED} at `build` is its `build` "
            "commit: after its plan and plan review, before any code review.", "",
            *_table(["Task", SOLO, f"{REVIEWED} at `build`", f"{REVIEWED} at the end"], rows), "",
            f"- Timed out, and scored where they stopped: {'; '.join(early)}.", ""]


def _defect_lines(builds: list[Build]) -> list[str]:
    listed, reps = defects(builds), len(REPS)
    rows = [[d.task, d.id, BASES[d.basis], f"{d.solo}/{reps}", f"{d.reviewed}/{reps}"] for d in listed]
    more, fewer = sum(d.reviewed > d.solo for d in listed), sum(d.reviewed < d.solo for d in listed)
    by_basis = []
    for basis, label in BASES.items():
        group = [d for d in listed if d.basis == basis]
        total = reps * len(group)
        by_basis.append([label, str(len(group)), f"{sum(d.solo for d in group)}/{total}",
                         f"{sum(d.reviewed for d in group)}/{total}"])
    return ["## Each defect, by build", "",
            f"Builds, of {reps}, that avoided the defect. The basis is the one its task's `defects.json` gives for "
            "the expected behaviour.", "",
            *_table(["Task", "Defect", "Basis", SOLO, REVIEWED], rows), "",
            f"- {REVIEWED} avoided the defect in more builds than {SOLO} on {more} defects, in fewer on {fewer}, "
            f"and in as many on {len(listed) - more - fewer}.",
            f"- The replicates of an arm had the same outcome on {agreeing(builds, SOLO)} of {len(listed)} defects "
            f"for {SOLO} and {agreeing(builds, REVIEWED)} of {len(listed)} for {REVIEWED}.", "",
            "Builds that avoided the defect, by basis:", "",
            *_table(["Basis", "Defects", SOLO, REVIEWED], by_basis), ""]


def _firmness_lines(builds: list[Build]) -> list[str]:
    tasks = [task_review(builds, REVIEWED, t) - task_review(builds, SOLO, t) for t in caught(builds)]
    per_defect = [100 * (d.reviewed - d.solo) / len(REPS) for d in defects(builds)]
    t = mean(tasks) / (stdev(tasks) / math.sqrt(len(tasks)))
    return ["## How firm the gain is", "",
            f"The report's gain is {mean(tasks):+.1f} points. A sign-flip test asks how often differences of "
            "these sizes, each given a random sign, add up to at least what was seen; it is exact and one-sided.", "",
            f"- By task ({len(tasks)}): differences {', '.join(f'{d:+.1f}' for d in tasks)}; t = {t:.2f} on "
            f"{len(tasks) - 1} degrees of freedom; sign-flip p = {sign_flip_p(tasks):.4f}, the smallest value "
            f"{len(tasks)} tasks can give.",
            f"- By defect ({len(per_defect)}, treated as independent although they share tasks): mean difference "
            f"{mean(per_defect):+.1f} points; sign-flip p = {sign_flip_p(per_defect):.4f}.",
            "- These describe this sample. They are not a significance claim, and the pre-registered rule is a "
            "continuation rule.", ""]


def _reading(builds: list[Build]) -> list[str]:
    raised = gains(builds)
    unprompted = sum(not blocked(b, "correctness", number) for b, number in raised)
    return ["## Reading", "",
            "- The loop under test ends a review on GREEN, on the user's ruling over a disagreement, or at the "
            "round cap, and its re-review prompt carries the previous round's findings with what was done about "
            "each (`skills/codex-review/SKILL.md`). The harness has no user and its re-review prompts carry no "
            "previous round, so a reviewed build ends on GREEN or at the cap. The cost and the round caps in "
            "`replay-report.md` describe that unattended loop.",
            f"- A fix raised a build's defects avoided {len(raised)} times. {unprompted} of them followed a round "
            "in which the correctness review did not block, so the fix answered the architecture review alone.",
            "- Lines changed are a count, not a judgment: these tests cannot see whether the later fixes improved "
            "the design.", ""]


def _share(reviews: list[Review]) -> str:
    return f"{sum(r.blocking for r in reviews)}/{len(reviews)}" if reviews else "—"


def _spread(values: list[int]) -> str:
    return f"{median(values):g} [{min(values)}–{max(values)}]" if values else "—"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(row) + " |" for row in rows]


if __name__ == "__main__":
    OUT.write_text(render(collect()), encoding="utf-8")
