import contextlib
import dataclasses
import io
import json
import shutil
import unittest
from unittest import mock

from eval.harness import agents, experiments, judge, loop, manifest, pilot, prompts, records, replay, source, tasks
from eval.tests.test_source import BASE_TEST, PRE_REVIEW_CALC, REFERENCE_CALC, FixtureCase

PLANNED = "\n# built from a plan\n"
OPUS = experiments.OPUS
SOLO = loop.Arm("A1", OPUS)
LOOP = loop.Arm("A3", OPUS, experiments.SOL)


class PromptTest(unittest.TestCase):
    def test_every_replay_prompt_says_the_repository_exists(self):
        replay = [
            prompts.solo_writer(brownfield=True),
            prompts.loop_plan_writer(brownfield=True),
            prompts.loop_plan_revision_writer("V", brownfield=True),
            prompts.loop_build_writer(brownfield=True),
            prompts.loop_fix_writer({"correctness": "x"}, brownfield=True),
            prompts.plan_review(brownfield=True),
            prompts.code_review("correctness", "abc", commits="c", final_state="f", brownfield=True),
            prompts.code_review("architecture", "abc", commits="c", final_state="f", brownfield=True),
        ]
        for text in replay:
            self.assertIn("existing repository", text)
            self.assertNotIn("empty repository", text)

    def test_the_replay_writer_differs_from_the_pilot_writer_only_in_its_preamble(self):
        swapped = prompts.loop_build_writer(brownfield=True).replace(
            prompts._BROWNFIELD_PREAMBLE, prompts._WRITER_PREAMBLE)
        self.assertEqual(swapped, prompts.loop_build_writer())


class StubAgentCase(FixtureCase):
    """The fixture task with stub agents: the writer writes `self.calc`, marked when it built a plan, and drops
    the repo's tests; a judge picks the version whose patch carries the mark."""

    def setUp(self):
        super().setUp()
        self.assertEqual(tasks.validate(self.task), [])
        self.seen_task_md = []
        self.offline_flags = []
        self.judge_prompts = []
        self.calc = REFERENCE_CALC

    def agent(self, model, prompt, *, role, cwd, out_dir, provider=agents.NATIVE, offline=False):
        self.offline_flags.append(offline)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "transcript.jsonl").touch()
        if role == agents.REVIEWER:
            return agents.Session(agents.COMPLETED, "ok", "VERDICT: GREEN\nISSUES:\nNone\n",
                                  out_dir / "transcript.jsonl", 0.1, reported_models=[model])
        if role == agents.JUDGE:
            self.judge_prompts.append(prompt)
            winner = "X" if PLANNED.strip() in prompt.split("# Version Y")[0] else "Y"
            return agents.Session(agents.COMPLETED, "ok", json.dumps({"winner": winner, "reasons": ["r"]}),
                                  out_dir / "transcript.jsonl", 0.1, reported_models=[model])
        self.seen_task_md.append((cwd / "TASK.md").read_text())
        if "existing repository" in prompt and ("Implement TASK.md" in prompt or "Build the plan" in prompt):
            (cwd / "calc.py").write_text(self.calc + (PLANNED if "Build the plan" in prompt else ""))
            shutil.rmtree(cwd / "tests")  # a build that drops the repo's tests gains nothing
        return agents.Session(agents.COMPLETED, "ok", "done", out_dir / "transcript.jsonl", 0.1,
                              reported_models=[model])

    def execute(self, arm: loop.Arm) -> dict:
        with mock.patch.object(agents, "run_claude", self.agent), mock.patch.object(agents, "run_codex", self.agent):
            return loop.execute_run(self.task, arm, self.tmp / "runs", rep=1, manifest_hash="d", offline=True)



class RunTest(StubAgentCase):
    """A replay run: seeded from base, built, scored on the replay groups, saved and read back by the records
    loader every consumer uses."""

    def test_a_replay_run_starts_only_offline_and_every_session_is_offline(self):
        with mock.patch.object(agents, "run_claude", self.agent), self.assertRaises(ValueError):
            loop.execute_run(self.task, SOLO, self.tmp / "runs", rep=1, manifest_hash="d")
        self.assertEqual(self.offline_flags, [])
        self.assertFalse((self.tmp / "runs").exists())
        self.execute(LOOP)
        self.assertGreater(len(self.offline_flags), 2)
        self.assertTrue(all(self.offline_flags))

    def test_a_solo_replay_run_is_seeded_scored_and_reloadable(self):
        record = self.execute(SOLO)
        self.assertEqual(record["status"], "complete", record.get("invalid_reason"))
        self.assertTrue(self.seen_task_md)
        replay = record["scores"]["replay"]
        self.assertEqual((replay["review"], replay["feature"], replay["regressions"]), (1.0, 1.0, 0.0))
        saved = self.tmp / "runs" / record["run_id"] / record["exec"] / "record.json"
        self.assertEqual(records.build(saved, "d")["scores"]["replay"]["defects"], [{"id": "zero", "avoided": True}])

    def test_a_loop_replay_run_ships_the_historical_defect_when_the_writer_does(self):
        self.calc = PRE_REVIEW_CALC
        record = self.execute(LOOP)
        self.assertEqual(record["status"], "complete", record.get("invalid_reason"))
        self.assertEqual(record["scores"]["replay"]["review"], 0.0)
        self.assertIn("plan-review-1", [step["name"] for step in record["steps"]])

    def test_malformed_and_control_replay_records(self):
        record = self.execute(SOLO)
        saved = self.tmp / "runs" / record["run_id"] / record["exec"] / "record.json"
        data = json.loads(saved.read_text())
        data["scores"]["replay"]["review"] = None  # defects present, so review must be a fraction
        saved.write_text(json.dumps(data))
        self.assertIsNone(records.build(saved, "d"))
        data["scores"]["replay"]["defects"] = []  # a control: no defects, review None
        saved.write_text(json.dumps(data))
        self.assertIsNotNone(records.build(saved, "d"))


CAUGHT = [f"c{n}" for n in range(6)]
STAGE_DATES = {"A1": "2026-11-02", "A3": "2026-11-03", "A2": "2026-11-05"}
CONTROLS = ["k0", "k1"]


def replay_record(arm: str, review, feature: float, regressions: float) -> dict:
    defects = [] if review is None else [{"id": "d", "avoided": review == 1.0}]
    return {"run_id": "x", "task": "t", "arm": arm, "rep": 1, "exec": "exec-1", "status": "complete", "manifest": "d",
            "started": STAGE_DATES[arm] + "T09:00:00+00:00", "finished": STAGE_DATES[arm] + "T10:00:00+00:00",
            "cli_versions": {"claude": "2.4.0", "codex": "0.164.0" if arm != "A2" else "0.165.0"},
            "flags": [], "rounds": {"plan": 1, "phase": 1},
            "steps": [{"name": "build", "attempts": [{"seconds": 60.0, "usage": {}}]}],
            "scores": {"replay": {"review": review, "feature": feature, "regressions": regressions,
                                  "defects": defects, "outcomes": {}}}}


def results(review: dict[str, list[float]], feature=None, regressions=None) -> replay.Results:
    """review: arm -> per-caught-task review fraction (the same for both replicates); controls get None."""
    built = replay.Results(digest="d", caught=list(CAUGHT), controls=list(CONTROLS), arms=["A1", "A2", "A3"])
    for arm, values in review.items():
        for index, task in enumerate(CAUGHT + CONTROLS):
            value = values[index] if index < len(CAUGHT) else None
            built.builds.setdefault(arm, {})[task] = [
                replay_record(arm, value, (feature or {}).get(arm, 1.0), (regressions or {}).get(arm, 0.0))
                for _ in range(2)]
    return built


class DecisionTest(unittest.TestCase):
    def outcomes(self, built: replay.Results) -> dict[str, str]:
        decision = replay.decide(built, experiments.REPLAY)
        return {c.name: c.outcome for c in decision.comparisons}

    def test_a_loop_effect_needs_fifteen_points_and_four_of_six_tasks(self):
        built = results({"A1": [0.5] * 6, "A2": [0.6] * 6, "A3": [0.7, 0.7, 0.7, 0.7, 0.5, 0.5]})
        # A3 − A1 = +13.3 on four tasks: short of +15.
        self.assertEqual(self.outcomes(built)["loop"], "no effect")
        built = results({"A1": [0.5] * 6, "A2": [0.6] * 6, "A3": [0.8, 0.8, 0.8, 0.7, 0.5, 0.5]})
        self.assertEqual(self.outcomes(built), {"loop": "effect", "cross-family": "no effect"})

    def test_cross_family_compares_against_self_review(self):
        built = results({"A1": [0.2] * 6, "A2": [0.4] * 6, "A3": [0.6] * 6})
        self.assertEqual(self.outcomes(built), {"loop": "effect", "cross-family": "effect"})

    def test_headroom_on_the_mean_and_on_reachable_wins(self):
        self.assertEqual(self.outcomes(results({"A1": [0.9] * 6, "A2": [0.5] * 6, "A3": [1.0] * 6}))["loop"],
                         "uninformative")
        # Mean exactly 85 but one task below 100: four wins are out of reach.
        self.assertEqual(self.outcomes(results({"A1": [0.1, 1, 1, 1, 1, 1], "A2": [0.5] * 6, "A3": [1.0] * 6}))["loop"],
                         "uninformative")
        # Mean exactly 85 with four tasks below 100: still informative, and +15 on four tasks fires.
        self.assertEqual(self.outcomes(results({"A1": [0.775] * 4 + [1, 1], "A2": [0.5] * 6,
                                                "A3": [1.0] * 6}))["loop"], "effect")

    def test_feature_loss_and_regressions_block_an_effect(self):
        review = {"A1": [0.2] * 6, "A2": [0.2] * 6, "A3": [0.8] * 6}
        self.assertEqual(self.outcomes(results(review, feature={"A3": 0.9}))["loop"], "no effect")
        self.assertEqual(self.outcomes(results(review, regressions={"A3": 0.01}))["loop"], "no effect")

    def test_a_missing_a1_or_a3_build_is_incomplete(self):
        for arm in ("A1", "A3"):
            built = results({"A1": [0.5] * 6, "A2": [0.5] * 6, "A3": [0.5] * 6})
            built.missing[arm] = [f"c0--{arm}--r2"]
            decision = replay.decide(built, experiments.REPLAY)
            self.assertEqual(decision.verdict, "INCOMPLETE")
            text = replay.render(built, decision, experiments.REPLAY)
            self.assertIn("INCOMPLETE", text)
            self.assertIn(f"- build c0--{arm}--r2", text)

    def staged(self, review: dict[str, list[float]], a2_missing: int) -> replay.Results:
        """A2's builds staged out: none, or some, of its 16 builds missing."""
        built = results(review)
        a2_runs = [f"{task}--A2--r{rep}" for task in CAUGHT + CONTROLS for rep in (1, 2)]
        if a2_missing:
            built.missing["A2"] = a2_runs[:a2_missing]
        if a2_missing == len(a2_runs):
            built.builds.pop("A2", None)
        return built

    def test_without_a_loop_effect_a_run_without_a2_is_complete(self):
        for review in ({"A1": [0.5] * 6, "A3": [0.5] * 6},  # no effect
                       {"A1": [0.9] * 6, "A3": [1.0] * 6}):  # headroom
            built = self.staged(review, a2_missing=16)
            decision = replay.decide(built, experiments.REPLAY)
            self.assertEqual(decision.verdict, "DECIDED")
            self.assertEqual(self.outcomes(built)["cross-family"], "not run")
            text = replay.render(built, decision, experiments.REPLAY)
            self.assertIn("A2 not run", text)
            self.assertNotIn("| A2 |", text)

    def test_a_loop_effect_requires_every_a2_build(self):
        for a2_missing in (16, 3):
            built = self.staged({"A1": [0.2] * 6, "A2": [0.4] * 6, "A3": [0.6] * 6}, a2_missing)
            decision = replay.decide(built, experiments.REPLAY)
            self.assertEqual(decision.verdict, "INCOMPLETE", a2_missing)
            self.assertEqual([c.outcome for c in decision.comparisons], ["effect"])
            self.assertIn("**Loop (A3 vs A1):** effect", replay.render(built, decision, experiments.REPLAY))

    def test_render_keeps_controls_out_of_the_review_table(self):
        built = results({"A1": [0.5] * 6, "A2": [0.5] * 6, "A3": [0.8] * 6})
        text = replay.render(built, replay.decide(built, experiments.REPLAY), experiments.REPLAY)
        review_table = text.split("## Review-caught defects avoided (%)")[1].split("##")[0]
        self.assertNotIn("k0", review_table)
        self.assertIn("| A3 | 80.0 ± 0.0 |", review_table)
        self.assertIn("**Loop (A3 vs A1):** effect", text)
        self.assertIn("| A2 | 2026-11-05 to 2026-11-05 | claude 2.4.0; codex 0.165.0 |", text)
        self.assertIn("The CLI versions differ between stages", text)

    def test_a_partial_a2_without_a_loop_effect_is_left_out_and_counted(self):
        built = self.staged({"A1": [0.5] * 6, "A2": [0.5] * 6, "A3": [0.5] * 6}, a2_missing=3)
        decision = replay.decide(built, experiments.REPLAY)
        text = replay.render(built, decision, experiments.REPLAY)
        self.assertEqual(decision.verdict, "DECIDED")
        self.assertIn("A2 left out: the loop effect did not pass, and 3 of its builds are missing.", text)
        self.assertNotIn("| A2 |", text)


class EndToEndTest(StubAgentCase):
    """Stub agents through the real schedule, run, resume, collect, decide and render."""

    def run_builds(self, experiment, only_arm=None) -> int:
        with mock.patch.object(pilot, "scored_tasks", lambda e: [self.task]), \
                mock.patch.object(manifest, "verify", return_value="d"), \
                mock.patch.object(agents, "run_claude", self.agent), mock.patch.object(agents, "run_codex", self.agent):
            return pilot.run_builds(self.tmp / "data", experiment, only_arm=only_arm)

    def test_a_staged_run_builds_one_arm_and_reports_without_a2(self):
        experiment = dataclasses.replace(experiments.REPLAY, tasks=("fixture",), parallel=1)
        for arm in ("A1", "A3"):
            self.assertEqual(self.run_builds(experiment, only_arm=arm), 0)
        runs = sorted(p.parent.name for p in (self.tmp / "data" / "runs").glob("*/exec-*"))
        self.assertEqual(runs, ["fixture--A1--r1", "fixture--A1--r2", "fixture--A3--r1", "fixture--A3--r2"])
        with mock.patch.object(pilot, "scored_tasks", lambda e: [self.task]), \
                mock.patch.object(manifest, "verify", return_value="d"):
            built = replay.collect(self.tmp / "data", experiment)
        decision = replay.decide(built, experiment)
        self.assertEqual((decision.verdict, built.missing), ("DECIDED", {"A2": ["fixture--A2--r1", "fixture--A2--r2"]}))
        self.assertIn("A2 not run", replay.render(built, decision, experiment))

    def test_run_refuses_an_unknown_arm(self):
        from eval.__main__ import main
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["run", "--experiment", "replay", "--arm", "A9"]), 2)
        self.assertIn("--arm must be one of A1, A2, A3", err.getvalue())

    def test_runs_resume_and_report(self):
        experiment = dataclasses.replace(experiments.REPLAY, tasks=("fixture",), parallel=1)
        self.assertEqual(self.run_builds(experiment), 0)
        execs = sorted(p.name for p in (self.tmp / "data" / "runs").glob("*/exec-*"))
        self.assertEqual(len(execs), 6)
        self.assertEqual(self.run_builds(experiment), 0)  # resume: nothing rebuilt
        self.assertEqual(len(list((self.tmp / "data" / "runs").glob("*/exec-*"))), 6)
        with mock.patch.object(pilot, "scored_tasks", lambda e: [self.task]), \
                mock.patch.object(manifest, "verify", return_value="d"):
            built = replay.collect(self.tmp / "data", experiment)
        self.assertEqual((built.missing, built.caught), ({}, ["fixture"]))
        decision = replay.decide(built, experiment)
        # Every arm avoids the defect: the loop effect is out of reach, so A2 is never compared.
        self.assertEqual([c.outcome for c in decision.comparisons], ["uninformative", "not run"])
        self.assertIn("| A1 | 100.0 ± 0.0 | 100.0 |", replay.render(built, decision, experiment))


class PatchExportTest(FixtureCase):
    """What a judge sees of a replayed build: its patch from the starting tree."""

    def build(self):
        build = self.tmp / "build"
        source.seed(self.task, build)
        (build / "calc.py").write_text(PRE_REVIEW_CALC)
        (build / "tests" / "test_calc.py").unlink()
        (build / "extra.py").write_text("NEW = 1\n")
        (build / "notes.md").write_text("review notes\n")
        (build / "plans").mkdir()
        (build / "plans" / "plan.py").write_text("PLAN = 1\n")
        return build

    def test_the_patch_shows_each_changed_file_whole_and_nothing_else(self):
        changed = judge.export_patch(self.task, self.build(), self.tmp / "export")
        self.assertEqual(sorted(changed), ["calc.py", "extra.py", "tests/test_calc.py"])
        patch = (self.tmp / "export" / judge.PATCH).read_text()
        self.assertIn(" def add(a, b):", patch)  # unchanged lines of a changed file, as context
        self.assertIn("+def div(a, b):", patch)
        self.assertIn("+NEW = 1", patch)
        for line in BASE_TEST.strip().splitlines():  # a deleted file, in full
            self.assertIn("-" + line, patch)
        for absent in ("review notes", "PLAN", "TASK.md", "tests/__init__.py"):
            self.assertNotIn(absent, patch)

    def test_a_pair_over_the_prompt_budget_is_judged_on_its_source(self):
        build = self.build()
        prompts_seen = []

        def fake(model, prompt, *, role, cwd, out_dir, provider=agents.NATIVE, offline=False):
            prompts_seen.append(prompt)
            out_dir.mkdir(parents=True, exist_ok=True)
            return agents.Session(agents.COMPLETED, "ok", '{"winner": "TIE", "reasons": []}',
                                  out_dir / "transcript.jsonl", 0.1, reported_models=[model])

        task_text = self.task.path("TASK.md").read_text()
        judge.export_patch(self.task, build, self.tmp / "full")
        whole = len(judge.build_prompt(task_text, self.tmp / "full", self.tmp / "full", scope=judge.PATCH_SCOPE))
        with mock.patch.object(agents, "run_claude", fake), mock.patch.object(judge, "PROMPT_CHARS", whole - 1):
            result = judge.judge_pair("fable", self.task, build, build, self.tmp / "judged", offline=True)
        self.assertEqual(result["scope"], judge.SOURCE_SCOPE)
        self.assertEqual(len(prompts_seen), 2)
        self.assertTrue(all(judge.SOURCE_ONLY_NOTE in p and "test_calc" not in p for p in prompts_seen))
        self.assertIn("+def div(a, b):", prompts_seen[0])
        with mock.patch.object(agents, "run_claude", fake), mock.patch.object(judge, "PROMPT_CHARS", 0), \
                self.assertRaises(judge.PromptTooLong):
            judge.judge_pair("fable", self.task, build, build, self.tmp / "too-long", offline=True)
        with self.assertRaises(ValueError):  # a replayed build is judged only offline
            judge.judge_pair("fable", self.task, build, build, self.tmp / "online")
        self.assertEqual(len(prompts_seen), 2)

    def test_the_patch_ignores_the_hosts_git_configuration(self):
        build = self.build()
        home = self.tmp / "home"
        home.mkdir()
        (home / ".gitconfig").write_text("[diff]\n\tnoprefix = true\n\tmnemonicPrefix = true\n[color]\n\tui = always\n")
        with mock.patch.dict("os.environ", {"HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig")}):
            judge.export_patch(self.task, build, self.tmp / "export")
        patch = (self.tmp / "export" / judge.PATCH).read_text()
        self.assertIn("--- a/calc.py\n+++ b/calc.py", patch)
        self.assertNotIn("\x1b[", patch)


class ReplayJudgingTest(StubAgentCase):
    """The design judgment: scheduled per caught task and replicate, run on stub builds, reported apart."""

    def test_the_replay_schedule_judges_caught_tasks_per_replicate_without_calibration(self):
        caught = [t.id for t in pilot.scored_tasks(experiments.REPLAY) if not t.source.control]
        schedule = pilot.judge_schedule(experiments.REPLAY)
        self.assertEqual(sorted((t, rep) for name, t, pair, rep in schedule if name == "fable"),
                         sorted((t, rep) for t in caught for rep in (1, 2)))
        self.assertTrue(all(pair == ("A1", "A3") for _, _, pair, _ in schedule))
        for task in pilot.scored_tasks(experiments.REPLAY):  # freeze asks for no degraded reference
            self.assertEqual(tasks.missing_parts(task, followup=False, degraded=True, mutants=False), [])

    def judging(self, experiment) -> int:
        with mock.patch.object(pilot, "scored_tasks", lambda e: [self.task]), \
                mock.patch.object(manifest, "verify", return_value="d"), \
                mock.patch.object(agents, "run_claude", self.agent), mock.patch.object(agents, "run_codex", self.agent):
            return pilot.run_judging(self.tmp / "data", experiment)

    def report(self, experiment):
        with mock.patch.object(pilot, "scored_tasks", lambda e: [self.task]), \
                mock.patch.object(manifest, "verify", return_value="d"):
            built = replay.collect(self.tmp / "data", experiment)
        decision = replay.decide(built, experiment)
        return decision, replay.render(built, decision, experiment)

    def test_replicates_are_judged_both_ways_by_both_judges_and_a_rebuild_makes_a_judgment_stale(self):
        experiment = dataclasses.replace(experiments.REPLAY, tasks=("fixture",), parallel=1)
        self.assertEqual(self.judging(experiment), 1)  # no builds yet: judging waits for them
        for arm in ("A1", "A3"):
            self.assertEqual(EndToEndTest.run_builds(self, experiment, only_arm=arm), 0)
        self.offline_flags.clear()
        self.assertEqual(self.judging(experiment), 0)
        self.assertEqual(len(self.judge_prompts), 2 * 2 * 2)  # judges × replicates × orders
        self.assertEqual(self.offline_flags, [True] * 8)
        prompt = self.judge_prompts[0]
        self.assertTrue(prompt.startswith(judge.PATCH_JUDGE_PROMPT))
        self.assertIn("-    def test_add(self):", prompt)
        self.assertNotIn("diff --git a/TASK.md", prompt)
        judged = sorted(p.parent.name + "/" + p.name for p in (self.tmp / "data" / "judging").glob("*/fixture/*"))
        self.assertEqual(judged, [f"fixture/A1-vs-A3--r{r}" for r in (1, 1, 2, 2)])
        decision, text = self.report(experiment)
        self.assertIn("| fixture | A3 | A3 | A3 | A3 | +4 |", text)
        self.assertIn("Uncalibrated", text)

        artifact = pilot.complete_build(self.tmp / "data" / "runs", "fixture", "A3", "d", 1) / "artifact"
        (artifact / "calc.py").write_text(REFERENCE_CALC)  # as if rebuilt: the r1 judgments no longer apply
        stale_decision, text = self.report(experiment)
        self.assertIn("| fixture | unavailable | A3 | unavailable | A3 | unavailable |", text)
        self.assertEqual(stale_decision, decision)
        self.judge_prompts.clear()
        self.assertEqual(self.judging(experiment), 0)
        self.assertEqual(len(self.judge_prompts), 2 * 2)  # only the stale judgments are redone
        self.assertIn("| fixture | tie | A3 | tie | A3 | +2 |", self.report(experiment)[1])


if __name__ == "__main__":
    unittest.main()
