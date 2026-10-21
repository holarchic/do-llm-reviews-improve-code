import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import agents, experiments, loop, prompts, tasks

DEV = tasks.Task(tasks.TASKS_ROOT / "dev-intervals")
REFERENCE = (DEV.path("reference") / "intervals.py").read_text()


def session(out: Path, terminal=agents.COMPLETED, text="done") -> agents.Session:
    out.mkdir(parents=True, exist_ok=True)
    transcript = out / "transcript.jsonl"
    transcript.touch()
    return agents.Session(terminal, "fake", text, transcript, 0.1)


class FakeAgents:
    """Writer writes the reference on any build/solo step; reviewer answers from a script."""

    def __init__(self, review_text="VERDICT: YELLOW\nISSUES:\n1. [HIGH] intervals.py:1 — x\n"
                                   "   Basis: TASK.md behaviour 2 — adjacent spans stay apart\n"):
        self.review_text = review_text
        self.prompts: list[tuple[str, str]] = []
        self.writer_infra_left = 0

    def run_claude(self, model, prompt, *, role, cwd, out_dir):
        self.prompts.append((role, prompt))
        if role == agents.REVIEWER:
            return session(out_dir, text=self.review_text)
        if self.writer_infra_left:
            self.writer_infra_left -= 1
            (cwd / "junk.txt").write_text("partial")
            return session(out_dir, terminal=agents.INFRA, text="")
        if "Build the plan" in prompt or "Implement TASK.md" in prompt:
            (cwd / "intervals.py").write_text(REFERENCE)
        if "planning only" in prompt:
            (cwd / "plans").mkdir(exist_ok=True)
            (cwd / "plans" / "plan.md").write_text("# plan\n")
        return session(out_dir)

    def run_codex(self, model, prompt, *, role, cwd, out_dir):
        self.prompts.append((role, prompt))
        return session(out_dir, text=self.review_text)


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_arm(self, arm, fake):
        with mock.patch.object(agents, "run_claude", fake.run_claude), \
             mock.patch.object(agents, "run_codex", fake.run_codex):
            return loop.execute_run(DEV, experiments.PILOT.arm(arm), self.tmp, manifest_hash=None)

    def test_reviewer_never_green_hits_both_caps_and_head_is_scored(self):
        record = self.run_arm("A2", FakeAgents())
        self.assertEqual(record["status"], "complete")
        self.assertIn("plan_capped", record["flags"])
        self.assertIn("review_capped", record["flags"])
        self.assertEqual(record["rounds"], {"plan": 5, "phase": 5})
        names = [step["name"] for step in record["steps"]]
        self.assertEqual(sum(n.startswith("plan-review-") for n in names), 5)
        self.assertEqual(sum(n.startswith("plan-revise-") for n in names), 4)
        self.assertEqual(sum(n.startswith(("correctness-review-", "architecture-review-")) for n in names), 10)
        self.assertEqual(sum(n.startswith("fix-") for n in names), 4)
        self.assertEqual(record["scores"]["primary"], 1.0)

    def test_green_reviewer_ends_each_loop_after_one_round(self):
        record = self.run_arm("A3", FakeAgents(review_text="VERDICT: GREEN\nISSUES:\nNone\n"))
        self.assertEqual(record["rounds"], {"plan": 1, "phase": 1})
        self.assertEqual(record["flags"], [])

    def test_low_only_yellow_does_not_block(self):
        record = self.run_arm("A3", FakeAgents(review_text="VERDICT: YELLOW\n1. [LOW] a.py:1 — nit\n"))
        self.assertEqual(record["rounds"], {"plan": 1, "phase": 1})

    def test_malformed_verdict_counts_as_yellow(self):
        record = self.run_arm("A2", FakeAgents(review_text="Looks good to me."))
        self.assertIn("plan_capped", record["flags"])
        self.assertTrue(any(f.startswith("malformed_verdict:") for f in record["flags"]))

    def test_infra_retry_resets_to_last_commit(self):
        fake = FakeAgents()
        fake.writer_infra_left = 1
        record = self.run_arm("A1", fake)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(len(record["steps"][0]["attempts"]), 2)
        repo = self.tmp / "dev-intervals--A1" / "exec-1" / "repo"
        self.assertFalse((repo / "junk.txt").exists())
        self.assertEqual(record["scores"]["primary"], 1.0)

    def test_infra_exhausted_makes_run_invalid(self):
        fake = FakeAgents()
        fake.writer_infra_left = 3
        record = self.run_arm("A1", fake)
        self.assertEqual(record["status"], "invalid")
        self.assertNotIn("scores", record)
        self.assertEqual(len(record["steps"][0]["attempts"]), 3)

    def test_solo_writer_never_sees_rules(self):
        fake = FakeAgents()
        self.run_arm("A1", fake)
        self.assertEqual(len(fake.prompts), 1)
        self.assertNotIn("Full rubric", fake.prompts[0][1])


class VerdictTest(unittest.TestCase):
    def test_labels(self):
        for rule in (False, True):
            with self.subTest(basis_rule=rule):
                self.assertFalse(loop.parse_verdict("VERDICT: GREEN\n", basis_rule=rule).blocking)
                self.assertTrue(loop.parse_verdict("VERDICT: YELLOW\nno tags", basis_rule=rule).blocking)
                self.assertTrue(loop.parse_verdict("", basis_rule=rule).malformed)
        self.assertTrue(loop.parse_verdict("**VERDICT: RED**\n1. [MEDIUM] x", basis_rule=False).blocking)


MIXED = """VERDICT: YELLOW
SUMMARY: s

ISSUES:
1. [MEDIUM] a.py:1 — cited
   Basis: SPEC Acceptance Criteria row 2 — wrong order
   Fix: sort
2. [HIGH] a.py:9 — uncited
   Fix: harden
3. [MEDIUM] a.py:12 — empty basis
   Basis:
   Fix: x
4. [LOW] a.py:20 — nit
   Fix: y

CLAIMS CHECKED:
1. Basis: this line is not inside an issue
"""


class BasisRuleTest(unittest.TestCase):
    def test_uncited_and_empty_basis_are_low(self):
        verdict = loop.parse_verdict(MIXED, basis_rule=True)
        self.assertEqual([(i.number, i.stated, i.effective) for i in verdict.issues],
                         [(1, "MEDIUM", "MEDIUM"), (2, "HIGH", "LOW"), (3, "MEDIUM", "LOW"), (4, "LOW", "LOW")])
        self.assertTrue(verdict.blocking)

    def test_basis_belongs_to_its_own_issue(self):
        text = "VERDICT: YELLOW\n1. [MEDIUM] a — uncited\n   Fix: x\n2. [LOW] b — nit\n   Basis: rule — y\n"
        verdict = loop.parse_verdict(text, basis_rule=True)
        self.assertEqual(verdict.issues[0].effective, "LOW")
        self.assertFalse(verdict.blocking)

    def test_without_the_rule_stated_severities_stand(self):
        verdict = loop.parse_verdict(MIXED.replace("   Basis: SPEC Acceptance Criteria row 2 — wrong order\n", ""),
                                     basis_rule=False)
        self.assertTrue(verdict.blocking)
        self.assertTrue(all(i.effective == i.stated for i in verdict.issues))

    def test_only_uncited_findings_do_not_block(self):
        text = MIXED.replace("   Basis: SPEC Acceptance Criteria row 2 — wrong order\n", "")
        self.assertFalse(loop.parse_verdict(text, basis_rule=True).blocking)

    def test_writer_sees_effective_severities_across_passes(self):
        verdicts = {"correctness": loop.parse_verdict(MIXED, basis_rule=True),
                    "architecture": loop.parse_verdict("VERDICT: YELLOW\n1. [MEDIUM] m.py:3 — uncited\n", basis_rule=True)}
        prompt = prompts.loop_fix_writer({n: v.text for n, v in verdicts.items()}, loop.severity_note(verdicts))
        self.assertIn("correctness issue 1: MEDIUM\n", prompt)
        self.assertIn("correctness issue 2: HIGH → LOW (no basis)", prompt)
        self.assertIn("architecture issue 1: MEDIUM → LOW (no basis)", prompt)
        self.assertIn("Fix every issue whose effective severity above is HIGH or MEDIUM", prompt)
        self.assertNotIn("Fix every HIGH and MEDIUM issue", prompt)

    def test_plan_revision_sees_effective_severities(self):
        verdict = loop.parse_verdict(MIXED, basis_rule=True)
        prompt = prompts.loop_plan_revision_writer(verdict.text, loop.severity_note({"plan": verdict}))
        self.assertIn("plan issue 2: HIGH → LOW (no basis)", prompt)
        self.assertIn("resolve every item whose effective severity above is HIGH or MEDIUM", prompt)

    def test_shipped_template_requires_basis(self):
        self.assertTrue(prompts.basis_required())


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def transcript(self, *events):
        path = self.tmp / "t.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        return path

    def claude(self, events, exit_code=0, timed_out=False, role=agents.WRITER):
        proc = agents.procs.ProcResult(exit_code, timed_out, 1.0)
        return agents.classify_claude(self.transcript(*events), proc, role).terminal

    def test_claude_classes(self):
        ok = {"type": "result", "subtype": "success", "is_error": False, "result": "hi", "usage": {}}
        partial = {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}
        self.assertEqual(self.claude([ok]), agents.COMPLETED)
        self.assertEqual(self.claude([partial], exit_code=1), agents.INFRA)
        self.assertEqual(self.claude([{**ok, "is_error": True}]), agents.INFRA)
        self.assertEqual(self.claude([{**ok, "subtype": "error_during_execution"}]), agents.INFRA)
        self.assertEqual(self.claude([ok], exit_code=1), agents.INFRA)
        self.assertEqual(self.claude([partial], exit_code=None, timed_out=True), agents.TIMEOUT)
        self.assertEqual(self.claude([partial], exit_code=None, timed_out=True, role=agents.JUDGE), agents.INFRA)

    def codex(self, events, final="VERDICT: GREEN", exit_code=0):
        final_path = self.tmp / "final.txt"
        final_path.write_text(final)
        proc = agents.procs.ProcResult(exit_code, False, 1.0)
        return agents.classify_codex(self.transcript(*events), final_path, proc, agents.REVIEWER, "m").terminal

    def test_codex_classes(self):
        done = {"type": "turn.completed", "usage": {"input_tokens": 1}}
        self.assertEqual(self.codex([done]), agents.COMPLETED)
        self.assertEqual(self.codex([{"type": "error", "message": "usage limit"}, done]), agents.INFRA)
        self.assertEqual(self.codex([{"type": "turn.started"}], exit_code=1), agents.INFRA)
        self.assertEqual(self.codex([done], final=""), agents.INFRA)


if __name__ == "__main__":
    unittest.main()


class FollowupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def followup(self, base_source: str, followup_source: str) -> dict:
        def build_writer(model, prompt, *, role, cwd, out_dir):
            (cwd / "intervals.py").write_text(base_source)
            return session(out_dir)

        def followup_writer(model, prompt, *, role, cwd, out_dir):
            assert (cwd / "FOLLOWUP.md").exists()
            (cwd / "intervals.py").write_text(followup_source)
            return session(out_dir)

        with mock.patch.object(agents, "run_claude", build_writer):
            loop.execute_run(DEV, experiments.PILOT.arm("A1"), self.tmp, manifest_hash=None)
        with mock.patch.object(agents, "run_claude", followup_writer):
            return loop.execute_followup(DEV, self.tmp / "dev-intervals--A1" / "exec-1", experiments.PILOT.arm("A1"), manifest_hash=None)

    def test_reference_followup_scores_full_without_regressions(self):
        record = self.followup(REFERENCE, (DEV.path("reference_followup") / "intervals.py").read_text())
        self.assertEqual(record["scores"]["new_fraction"], 1.0)
        self.assertEqual(record["scores"]["regressions"], [])

    def test_change_that_breaks_adjacency_is_a_regression(self):
        broken = (DEV.path("reference_followup") / "intervals.py").read_text().replace(
            "merged[-1][1] + 1", "merged[-1][1]")
        record = self.followup(REFERENCE, broken)
        self.assertEqual(record["scores"]["regressions"], ["test_merge.MergeTest.test_adjacent_merge"])

    def test_failure_present_before_is_persistent_not_regression(self):
        weak = (DEV.path("wrong") / "adjacent-not-merged" / "intervals.py").read_text()
        followup = weak + "\n\ndef total_covered(spans):\n    return sum(e - s + 1 for s, e in merge(spans))\n"
        record = self.followup(weak, followup)
        self.assertEqual(record["scores"]["regressions"], [])
        self.assertEqual(record["scores"]["persistent_failures"], ["test_merge.MergeTest.test_adjacent_merge"])


class HardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_with(self, writer, arm="A1"):
        with mock.patch.object(agents, "run_claude", writer):
            return loop.execute_run(DEV, experiments.PILOT.arm(arm), self.tmp, manifest_hash=None)

    def test_session_exception_is_a_retried_infra_attempt(self):
        calls = []

        def writer(model, prompt, *, role, cwd, out_dir):
            calls.append(cwd)
            if len(calls) == 1:
                raise OSError("spawn failed")
            (cwd / "intervals.py").write_text(REFERENCE)
            return session(out_dir)

        record = self.run_with(writer)
        self.assertEqual(record["status"], "complete")
        attempts = record["steps"][0]["attempts"]
        self.assertEqual([a["terminal"] for a in attempts], [agents.INFRA, agents.COMPLETED])
        self.assertIn("harness exception", attempts[0]["reason"])

    def test_score_uses_committed_files_only(self):
        def writer(model, prompt, *, role, cwd, out_dir):
            (cwd / "intervals.py").write_text(REFERENCE)
            with open(cwd / ".gitignore", "a") as handle:
                handle.write("intervals.py\n")
            return session(out_dir)

        record = self.run_with(writer)
        self.assertEqual(record["scores"]["primary"], 0.0)
        self.assertFalse((self.tmp / "dev-intervals--A1" / "exec-1" / "artifact" / "intervals.py").exists())

    def test_scoring_crash_makes_run_invalid(self):
        def writer(model, prompt, *, role, cwd, out_dir):
            return session(out_dir)

        with mock.patch.object(loop.score, "score_build", side_effect=RuntimeError("boom")):
            record = self.run_with(writer)
        self.assertEqual(record["status"], "invalid")
        self.assertIn("scoring error", record["invalid_reason"])

    def test_session_cwd_is_neutral(self):
        seen = []

        def writer(model, prompt, *, role, cwd, out_dir):
            seen.append(cwd)
            return session(out_dir)

        self.run_with(writer)
        self.assertNotIn("dev-intervals", str(seen[0]))
        self.assertNotIn("A1", seen[0].parent.name)


class TimeoutAndPreservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_arm(self, arm, fake):
        with mock.patch.object(agents, "run_claude", fake.run_claude), \
             mock.patch.object(agents, "run_codex", fake.run_codex):
            return loop.execute_run(DEV, experiments.PILOT.arm(arm), self.tmp, manifest_hash=None)

    def test_build_timeout_stops_reviews_and_scores_head(self):
        fake = FakeAgents(review_text="VERDICT: GREEN\n")
        original = fake.run_claude

        def run_claude(model, prompt, *, role, cwd, out_dir):
            result = original(model, prompt, role=role, cwd=cwd, out_dir=out_dir)
            if "Build the plan" in prompt:
                return agents.Session(agents.TIMEOUT, "wall clock", "", result.transcript, 1.0)
            return result

        fake.run_claude = run_claude
        record = self.run_arm("A3", fake)
        self.assertEqual(record["status"], "complete")
        self.assertIn("timeout:build", record["flags"])
        self.assertEqual([s["name"] for s in record["steps"]][-1], "build")
        self.assertEqual(record["scores"]["primary"], 1.0)

    def test_reviewer_timeout_stops_the_run(self):
        fake = FakeAgents()
        fake.run_codex = lambda model, prompt, *, role, cwd, out_dir: agents.Session(
            agents.TIMEOUT, "wall clock", "", session(out_dir).transcript, 1.0)
        record = self.run_arm("A3", fake)
        self.assertEqual(record["status"], "complete")
        self.assertEqual([s["name"] for s in record["steps"]], ["plan", "plan-review-1"])
        self.assertEqual(record["scores"]["primary"], 0.0)

    def test_preservation_failure_is_recorded_invalid(self):
        def writer(model, prompt, *, role, cwd, out_dir):
            return session(out_dir)

        with mock.patch.object(agents, "run_claude", writer), \
             mock.patch.object(loop.shutil, "copytree", side_effect=OSError("disk full")):
            record = loop.execute_run(DEV, experiments.PILOT.arm("A1"), self.tmp, manifest_hash=None)
        self.assertEqual(record["status"], "invalid")
        self.assertIn("preserving the repo failed", record["invalid_reason"])
        self.assertTrue((self.tmp / "dev-intervals--A1" / "exec-1" / "record.json").exists())


class AblationTest(unittest.TestCase):
    """Arm switches change exactly the steps and prompts they name."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.calls = []

    def fakes(self, review_text):
        fake = FakeAgents(review_text=review_text)

        def claude(model, prompt, *, role, cwd, out_dir):
            self.calls.append(("claude", role, prompt))
            return fake.run_claude(model, prompt, role=role, cwd=cwd, out_dir=out_dir)

        def codex(model, prompt, *, role, cwd, out_dir):
            self.calls.append(("codex", role, prompt))
            if role == agents.WRITER:
                return fake.run_claude(model, prompt, role=role, cwd=cwd, out_dir=out_dir)
            return fake.run_codex(model, prompt, role=role, cwd=cwd, out_dir=out_dir)

        return claude, codex

    def run_arm(self, arm, review_text="VERDICT: GREEN\n"):
        claude, codex = self.fakes(review_text)
        with mock.patch.object(agents, "run_claude", claude), mock.patch.object(agents, "run_codex", codex):
            return loop.execute_run(DEV, arm, self.tmp, manifest_hash=None)

    def names(self, record):
        return [step["name"] for step in record["steps"]]

    def test_rubric_off_writer_gets_no_rules_reviewer_keeps_rubric(self):
        arm = loop.Arm("X", experiments.HAIKU, experiments.LUNA, writer_rules=False)
        self.run_arm(arm)
        writer_prompts = [p for cli, role, p in self.calls if role == agents.WRITER]
        reviewer_prompts = [p for cli, role, p in self.calls if role == agents.REVIEWER]
        self.assertTrue(writer_prompts)
        self.assertFalse(any("Full rubric" in p or "## Code" in p for p in writer_prompts))
        self.assertTrue(all("Code Architecture Rule Set" in p for p in reviewer_prompts[1:]))

    def test_architecture_pass_off_runs_one_pass_per_round(self):
        arm = loop.Arm("X", experiments.HAIKU, experiments.LUNA, passes=("correctness",))
        record = self.run_arm(arm)
        self.assertIn("correctness-review-1", self.names(record))
        self.assertFalse(any(n.startswith("architecture-review") for n in self.names(record)))

    def test_plan_review_off_has_no_plan_review_steps(self):
        arm = loop.Arm("X", experiments.HAIKU, experiments.LUNA, plan_review=False)
        record = self.run_arm(arm)
        self.assertEqual(self.names(record)[:2], ["plan", "build"])
        self.assertNotIn("plan", record["rounds"])

    def test_codex_writer_arm_and_its_followup_use_codex(self):
        arm = loop.Arm("G", agents.Agent("codex", "gpt-5.6-terra"), agents.Agent("claude", "claude-sonnet-5-5"))
        record = self.run_arm(arm)
        self.assertEqual(record["scores"]["primary"], 1.0)
        writers = {cli for cli, role, _ in self.calls if role == agents.WRITER}
        reviewers = {cli for cli, role, _ in self.calls if role == agents.REVIEWER}
        self.assertEqual((writers, reviewers), ({"codex"}, {"claude"}))
        self.calls.clear()
        claude, codex = self.fakes("VERDICT: GREEN\n")
        with mock.patch.object(agents, "run_claude", claude), mock.patch.object(agents, "run_codex", codex):
            loop.execute_followup(DEV, self.tmp / "dev-intervals--G" / "exec-1", arm, manifest_hash=None)
        self.assertEqual([cli for cli, role, _ in self.calls], ["codex"])
