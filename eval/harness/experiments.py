"""Named experiments: which arms run, which pairs are judged, and where the data goes.

Tier and reverse-direction experiments reuse the pilot's arm roles — A1 solo,
A1r its replicate, A2 reviewed by the writer's own family, A3 by the other
family — so their reports read the same way. Ablations compare the full loop
with one switch off at a time. The pilot, pilot2, the replay and plain-review
carry pre-registered decision rules; every other experiment needs its own
`eval/prereg/<name>.md`, frozen, before it can run, and is descriptive until it
defines one.

plain-review is paired (`report="paired"`): its baseline is built once per task,
and every other arm is a review cell run on copies of that build.

pilot2 is judge-free: open-weight models through OpenRouter, three replicates per
cell, scored by hidden tests and planted bugs (`report="replicated"`), under its
own pre-registered rule.

Model pairs follow the operator's tier map: Fable↔Astra, Opus↔Sol,
Sonnet↔Terra, Haiku↔Luna.
"""

from dataclasses import dataclass

from .agents import DEEPSEEK, OPENROUTER, Agent
from .loop import PLAIN, Arm

HAIKU = Agent("claude", "claude-haiku-5-5")
SONNET = Agent("claude", "claude-sonnet-5-5")
OPUS = Agent("claude", "claude-opus-5-5")
LUNA = Agent("codex", "gpt-6-luna")
TERRA = Agent("codex", "gpt-5.6-terra")
SOL = Agent("codex", "gpt-6.1-sol")
# Pilot 2's writer tier: DeepSeek V4.1 Flash solved the calibrated tasks at 100%, so the writer is a
# ~30B mixture-of-experts coder, reviewed by itself or by GLM's model of the same size class.
QWEN_WRITER = Agent("claude", "qwen/qwen3-coder-30b-a3b-instruct", OPENROUTER)
QWEN_REVIEWER = Agent("codex", "qwen/qwen3-coder-30b-a3b-instruct", OPENROUTER)
GLM_REVIEWER = Agent("codex", "z-ai/glm-4.7-flash", OPENROUTER)

PILOT_TASKS = ("bill-split", "config-includes", "dep-resolver", "log-windows", "token-bucket")
PILOT2_TASKS = ("calc-expr", "cron-next", "ignore-match", "json-patch", "semver-range")
# Filled as each task is authored, audited and validated.
REPLAY_TASKS: tuple[str, ...] = ("jobscan-email", "jobscan-greenhouse", "jobscan-workable-gem", "jobscan-setup-commands",
                                 "jobscan-watchlist", "jobscan-company-notes")
# The audited task versions; grows as each new task is authored and validated, and is fixed at the freeze.
PLAIN_REVIEW_TASKS: tuple[str, ...] = ("jobscan-email-v2", "jobscan-greenhouse-v2", "jobscan-workable-gem-v2",
                                       "jobscan-setup-commands-v2", "jobscan-watchlist-v2",
                                       "jobscan-company-notes-v2")


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
    # Whose pre-registered rule applies: "pilot", "pilot2", "replay", "plain-review" or none (descriptive).
    decision_rule: str | None
    tasks: tuple[str, ...] = PILOT_TASKS
    # Replicates per task × arm; above 1, run ids end in --r<n>. In a paired experiment: runs per review
    # cell, numbered from 1, while the baseline is built once per task with no replicate number.
    reps: int = 1
    parallel: int = 3
    budget_usd: float | None = None  # pilot 2's OpenRouter spend cap (history; new routed runs use `envelope`)
    envelope: str | None = None  # the spend envelope a routed experiment draws on (envelope.py)
    # "judged" (report.py) | "replicated" (replicated.py) | "replay" (replay.py) | "paired" (cells.py, paired.py)
    report: str = "judged"
    offline: bool = False  # tool commands reach no network beyond localhost (agents.py); a source task requires it

    def arm(self, name: str) -> Arm:
        return next(arm for arm in self.arms if arm.name == name)

    @property
    def arm_names(self) -> tuple[str, ...]:
        return tuple(arm.name for arm in self.arms)

    @property
    def cell_arms(self) -> tuple[str, ...]:
        """The review cells: arms whose reviewer reviews a stored build of the baseline once."""
        return tuple(arm.name for arm in self.arms if arm.review == PLAIN)

    @property
    def treatments(self) -> tuple[str, ...]:
        return tuple(arm.name for arm in self.arms if arm.reviewer is not None)

    @property
    def rep_ids(self) -> tuple[int | None, ...]:
        return (None,) if self.reps == 1 else tuple(range(1, self.reps + 1))

    @property
    def routed_providers(self) -> frozenset[str]:
        return frozenset(agent.provider for arm in self.arms for agent in (arm.writer, arm.reviewer)
                         if agent is not None and agent.routed)

    @property
    def routed(self) -> bool:
        return any(agent is not None and agent.routed for arm in self.arms for agent in (arm.writer, arm.reviewer))


def _families(name: str, title: str, writer: Agent, same: Agent, cross: Agent, seed: int, prereg: str,
              data_dir: str, decision_rule: str | None = None) -> Experiment:
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
        decision_rule=None,
    )


PILOT = _families("pilot", "Eval pilot report", HAIKU, HAIKU, LUNA, 20261021, "PREREG.md", "", decision_rule="pilot")

PILOT2 = Experiment(
    name="pilot2",
    title="Eval pilot 2 report",
    arms=(Arm("A1", QWEN_WRITER), Arm("A2", QWEN_WRITER, QWEN_REVIEWER), Arm("A3", QWEN_WRITER, GLM_REVIEWER)),
    baseline="A1",
    replicate=None,
    followup_arms=(),
    pairs=(),
    seed=20261201,
    data_dir="pilot2",
    prereg="prereg/pilot2.md",
    decision_rule="pilot2",
    tasks=PILOT2_TASKS,
    reps=3,
    parallel=5,
    budget_usd=10.0,
    report="replicated",
)

# Real phases of a loop-built repo, rebuilt from their starting tree (tasks with a source.json): the shipped
# configuration (A3) against solo (A1) and against the writer reviewing itself (A2).
REPLAY = Experiment(
    name="replay",
    title="Eval replay report",
    arms=(Arm("A1", OPUS), Arm("A2", OPUS, OPUS), Arm("A3", OPUS, SOL)),
    baseline="A1",
    replicate=None,
    followup_arms=(),
    pairs=(("A1", "A3"),),  # the design judgment: descriptive, never in the decision rule
    seed=20261022,
    data_dir="replay",
    prereg="prereg/replay.md",
    decision_rule="replay",
    tasks=REPLAY_TASKS,
    reps=2,
    report="replay",
    offline=True,
)

# Plain review, the base experiment. The writer builds each task alone, once (`none`); each review cell then
# runs on copies of that build: the same model in a fresh session (`same`) and the other family's (`other`).
# Paired: a cell is compared with the build it started from.
PLAIN_REVIEW = Experiment(
    name="plain-review",
    title="Eval plain-review report",
    arms=(Arm("none", OPUS), Arm("same", OPUS, OPUS, review=PLAIN), Arm("other", OPUS, SOL, review=PLAIN)),
    baseline="none",
    replicate=None,
    followup_arms=(),
    pairs=(),
    seed=20261111,
    data_dir="plain-review",
    prereg="prereg/plain-review.md",
    decision_rule="plain-review",
    tasks=PLAIN_REVIEW_TASKS,
    reps=2,
    report="paired",
    offline=True,
)

# B's candidate routes, as A3b admits them: every route under Claude Code (a writer) and Codex (a
# reviewer). Never scheduled, frozen or scored (no tasks): its probes and the admission checks run on it.
_ADMISSION_MODELS = ((OPENROUTER, "z-ai/glm-5.3"), (OPENROUTER, "z-ai/glm-5.3-flash"),
                     (DEEPSEEK, "deepseek-v4-pro"), (DEEPSEEK, "deepseek-flash"))
ADMISSION = Experiment(
    name="admission",
    title="Route admission (A3b)",
    arms=(Arm("R0", Agent("claude", *reversed(_ADMISSION_MODELS[0]))),)  # the registry's reviewer-less baseline
    + tuple(Arm(f"R{i}", Agent("claude", model, provider), Agent("codex", model, provider))
            for i, (provider, model) in enumerate(_ADMISSION_MODELS, 1)),
    baseline="R0",
    replicate=None,
    followup_arms=(),
    pairs=(),
    seed=20261106,
    data_dir="admission",
    prereg="prereg/admission.md",  # none exists: nothing here is scored
    decision_rule=None,
    tasks=(),
    envelope="B",
    offline=True,
)

EXPERIMENTS = {experiment.name: experiment for experiment in (
    PILOT,
    PILOT2,
    REPLAY,
    PLAIN_REVIEW,
    tier("sonnet", SONNET, TERRA, seed=20261101),
    tier("opus", OPUS, SOL, seed=20261102),
    reverse("reverse-sonnet", TERRA, SONNET, seed=20261103),
    reverse("reverse-opus", SOL, OPUS, seed=20261104),
    ablation("ablation-sonnet", SONNET, TERRA, seed=20261105),
    ADMISSION,
)}
