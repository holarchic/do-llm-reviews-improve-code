"""Named experiments: which arms run, which pairs are judged, and where the data goes.

Tier and reverse-direction experiments reuse the pilot's arm roles — A1 solo,
A1r its replicate, A2 reviewed by the writer's own family, A3 by the other
family — so their reports read the same way. Ablations compare the full loop
with one switch off at a time. Only the pilot carries a pre-registered decision
rule; every other experiment needs its own `eval/prereg/<name>.md`, frozen,
before it can run.

Model pairs follow the operator's tier map: Fable↔Astra, Opus↔Sol,
Sonnet↔Terra, Haiku↔Luna.
"""

from dataclasses import dataclass

from .agents import Agent
from .loop import Arm

HAIKU = Agent("claude", "claude-haiku-5-5")
SONNET = Agent("claude", "claude-sonnet-5-5")
OPUS = Agent("claude", "claude-opus-5-5")
LUNA = Agent("codex", "gpt-6-luna")
TERRA = Agent("codex", "gpt-5.6-terra")
SOL = Agent("codex", "gpt-6.1-sol")


@dataclass(frozen=True)
class Experiment:
    name: str
    title: str
    arms: tuple[Arm, ...]
    baseline: str
    replicate: str | None
    followup_arms: tuple[str, ...]
    pairs: tuple[tuple[str, str], ...]
    seed: int
    data_dir: str  # under eval/data; the pilot's data sits at the root
    prereg: str  # relative to eval/
    decision_rule: bool  # the pilot's pre-registered GO/NO-GO rule applies

    def arm(self, name: str) -> Arm:
        return next(arm for arm in self.arms if arm.name == name)

    @property
    def arm_names(self) -> tuple[str, ...]:
        return tuple(arm.name for arm in self.arms)

    @property
    def treatments(self) -> tuple[str, ...]:
        return tuple(arm.name for arm in self.arms if arm.reviewer is not None)


def _families(name: str, title: str, writer: Agent, same: Agent, cross: Agent, seed: int, prereg: str,
              data_dir: str, decision_rule: bool = False) -> Experiment:
    return Experiment(
        name=name,
        title=title,
        arms=(Arm("A1", writer), Arm("A1r", writer), Arm("A2", writer, same), Arm("A3", writer, cross)),
        baseline="A1",
        replicate="A1r",
        followup_arms=("A1", "A2", "A3"),
        pairs=(("A1", "A2"), ("A1", "A3"), ("A2", "A3")),
        seed=seed,
        data_dir=data_dir,
        prereg=prereg,
        decision_rule=decision_rule,
    )


def tier(name: str, claude: Agent, gpt: Agent, seed: int) -> Experiment:
    """Claude writes; reviewed by Claude (A2) or by the GPT model of the same tier (A3)."""
    return _families(name, f"Eval report: {name}", claude, claude, gpt, seed, f"prereg/{name}.md", name)


def reverse(name: str, gpt: Agent, claude: Agent, seed: int) -> Experiment:
    """GPT writes; reviewed by GPT (A2) or by the Claude model of the same tier (A3)."""
    return _families(name, f"Eval report: {name}", gpt, gpt, claude, seed, f"prereg/{name}.md", name)


def ablation(name: str, writer: Agent, reviewer: Agent, seed: int) -> Experiment:
    """The full loop against itself with one switch off: writer rules, architecture pass, plan review."""
    full = Arm("L", writer, reviewer)
    arms = (
        Arm("S", writer),
        full,
        Arm("L-rubric", writer, reviewer, writer_rules=False),
        Arm("L-arch", writer, reviewer, passes=("correctness",)),
        Arm("L-plan", writer, reviewer, plan_review=False),
    )
    return Experiment(
        name=name,
        title=f"Eval report: {name}",
        arms=arms,
        baseline="S",
        replicate=None,
        followup_arms=tuple(arm.name for arm in arms),
        pairs=(("S", "L"), ("L", "L-rubric"), ("L", "L-arch"), ("L", "L-plan")),
        seed=seed,
        data_dir=name,
        prereg=f"prereg/{name}.md",
        decision_rule=False,
    )


PILOT = _families("pilot", "Eval pilot report", HAIKU, HAIKU, LUNA, 20261021, "PREREG.md", "", decision_rule=True)

EXPERIMENTS = {experiment.name: experiment for experiment in (
    PILOT,
    tier("sonnet", SONNET, TERRA, seed=20261101),
    tier("opus", OPUS, SOL, seed=20261102),
    reverse("reverse-sonnet", TERRA, SONNET, seed=20261103),
    reverse("reverse-opus", SOL, OPUS, seed=20261104),
    ablation("ablation-sonnet", SONNET, TERRA, seed=20261105),
)}
