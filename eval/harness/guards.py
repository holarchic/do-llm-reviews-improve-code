"""The harm guards of a registered comparison, stated in test counts.

A treatment beats its comparator on defects avoided only while it satisfies the declared harm
tolerance. The regression guard counts, per task, the frozen regression tests a build did not
pass: task t's Δ_t is the treatment's extra failures as a share of its R_t tests, averaged over
replicates, in percentage points. The guard passes when the task mean of Δ_t is at most
MEAN_LIMIT and no task's Δ_t exceeds TASK_LIMIT, so a handful of broken tests is tolerated and a
broken module is not. On a 155-test task with two replicates the cap allows 3 extra failures over
both builds, on a 420-test task 8, and they may sit in one build: it is never a per-build limit.
Zero tolerance (mean Δ ≤ 0) is v1's rule and is kept as a sensitivity verdict.

The caller owns scope: the regression guard runs over caught tasks and controls, the feature
floor and headroom over caught tasks only. Regression limits compare exact fractions of counts,
so a count at the cap never rounds across it; feature and review means are means of pass
fractions over different denominators and take a float tolerance.
"""

import math
from dataclasses import dataclass
from fractions import Fraction
from statistics import mean

MEAN_LIMIT = Fraction(1, 2)
TASK_LIMIT = Fraction(1)
FEATURE_FLOOR = 5.0
WIN_SHARE = Fraction(2, 3)
# Pass fractions over different denominators make exact thresholds land a few ulps short.
TOLERANCE = 1e-9


def failures(outcomes: dict[str, str], regression: list[str]) -> int:
    """The frozen regression tests a build did not pass; a test with no outcome failed."""
    return sum(outcomes.get(test_id) != "pass" for test_id in regression)


@dataclass(frozen=True)
class TaskRegressions:
    """One task's regression failures per replicate, for the treatment and the comparator."""

    task: str
    tests: int
    treatment: tuple[int, ...]
    comparator: tuple[int, ...]

    def __post_init__(self):
        counts = (self.tests, *self.treatment, *self.comparator)
        if not all(isinstance(n, int) and not isinstance(n, bool) for n in counts):
            raise ValueError(f"{self.task}: test and failure counts must be integers")
        if self.tests < 1:
            raise ValueError(f"{self.task}: no regression tests")
        if not self.treatment or len(self.treatment) != len(self.comparator):
            raise ValueError(f"{self.task}: replicates {len(self.treatment)} vs {len(self.comparator)}")
        if any(count < 0 or count > self.tests for count in self.treatment + self.comparator):
            raise ValueError(f"{self.task}: a failure count outside 0..{self.tests}")

    @property
    def delta(self) -> Fraction:
        """Δ_t in percentage points: 100 × (Σ treatment − Σ comparator) / (replicates × tests)."""
        extra = sum(self.treatment) - sum(self.comparator)
        return Fraction(100 * extra, len(self.treatment) * self.tests)


@dataclass(frozen=True)
class RegressionGuard:
    tasks: tuple[TaskRegressions, ...]
    mean: Fraction
    over_cap: tuple[str, ...]  # tasks whose Δ_t exceeds TASK_LIMIT

    @property
    def mean_ok(self) -> bool:
        return self.mean <= MEAN_LIMIT

    @property
    def passed(self) -> bool:
        return self.mean_ok and not self.over_cap

    @property
    def zero_tolerance(self) -> bool:
        """The sensitivity verdict: no rise in regressions at all, v1's rule."""
        return self.mean <= 0


def regression_guard(tasks: list[TaskRegressions]) -> RegressionGuard:
    if not tasks:
        raise ValueError("no tasks")
    deltas = [task.delta for task in tasks]
    return RegressionGuard(tasks=tuple(tasks), mean=sum(deltas, Fraction(0)) / len(deltas),
                           over_cap=tuple(task.task for task in tasks if task.delta > TASK_LIMIT))


def feature_ok(treatment_mean: float, comparator_mean: float) -> bool:
    """Feature tests passed (arm means over caught tasks, in points) fall by at most FEATURE_FLOOR."""
    return treatment_mean - comparator_mean >= -FEATURE_FLOOR - TOLERANCE


def wins_needed(caught: int) -> int:
    return math.ceil(WIN_SHARE * caught)


@dataclass(frozen=True)
class Headroom:
    comparator_mean: float
    open_tasks: int  # caught tasks whose comparator mean is below 100
    needed: int
    limit: float

    @property
    def ok(self) -> bool:
        return self.comparator_mean <= self.limit + TOLERANCE and self.open_tasks >= self.needed


def headroom(comparator_review: dict[str, float], threshold: float) -> Headroom:
    """Room for a +threshold gain: the comparator's review mean and the caught tasks it left below 100.

    `comparator_review` maps each caught task to the comparator's task mean, in points.
    """
    if not comparator_review:
        raise ValueError("no caught tasks")
    values = list(comparator_review.values())
    return Headroom(comparator_mean=mean(values), open_tasks=sum(v < 100 - TOLERANCE for v in values),
                    needed=wins_needed(len(values)), limit=100.0 - threshold)
