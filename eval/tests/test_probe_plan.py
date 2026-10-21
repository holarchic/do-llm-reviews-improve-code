import json
import unittest

from eval.harness import agents, experiments, openrouter, probe


class ProbePlanTest(unittest.TestCase):
    def test_pilot_keeps_its_eight_probes_on_its_own_agents(self):
        plan = {p.name: p for p in probe.probe_plan(experiments.PILOT)}
        self.assertEqual(set(plan), {"claude_instructions", "codex_instructions", "claude_judge_instructions",
                                     "codex_judge_instructions", "claude_canary", "codex_canary",
                                     "claude_reviewer_read_only", "codex_writer"})
        self.assertEqual(plan["claude_canary"].agent, experiments.HAIKU)
        self.assertEqual((plan["codex_writer"].agent, plan["codex_writer"].role), (experiments.LUNA, agents.WRITER))

    def test_pilot2_probes_every_routed_session_and_the_key(self):
        plan = {p.name: p for p in probe.probe_plan(experiments.PILOT2)}
        self.assertEqual(set(plan), {"claude_instructions", "codex_instructions",
                                     "codex_instructions:z-ai/glm-5.3-flash", "claude_canary", "codex_canary",
                                     "codex_writer", "claude_key", "codex_key", "claude_passthrough",
                                     "codex_passthrough"})
        self.assertTrue(all(p.agent.provider == agents.OPENROUTER for p in plan.values()))
        self.assertEqual(plan["claude_key"].agent, experiments.DEEPSEEK_WRITER)
        self.assertEqual(plan["codex_key"].role, agents.REVIEWER)


class KeyProblemsTest(unittest.TestCase):
    TOKEN = "PERMITTED-abc"

    def output(self, names="HOME PATH", read="KEY-READ-DENIED", ps="PS-DENIED"):
        return f'{{"out": "ENV-NAMES: {names}\\n{read}\\n{ps}\\n{self.TOKEN}\\n"}}'

    def test_clean_output_passes_and_reports_ps(self):
        self.assertEqual(probe.key_problems(self.output(), self.TOKEN, []), {"ps": "denied", "problems": []})
        self.assertEqual(probe.key_problems(self.output(ps="PS-RAN lines: 9"), self.TOKEN, [])["ps"], "ran")

    def test_the_script_source_in_a_transcript_is_not_its_output(self):
        transcript = json.dumps({"out": probe.KEY_SCRIPT}) + "\n" + self.output()
        self.assertEqual(probe.key_problems(transcript, self.TOKEN, [])["problems"], [])
        self.assertTrue(probe.key_problems(json.dumps({"out": probe.KEY_SCRIPT}), self.TOKEN, [])["problems"])

    def test_each_failure(self):
        for name in openrouter.KEY_VARIABLES:
            self.assertTrue(probe.key_problems(self.output(names=f"HOME {name} PATH"), self.TOKEN, [])["problems"])
        self.assertIn("the key file was readable",
                      probe.key_problems(self.output(read="KEY-READ-OK"), self.TOKEN, [])["problems"])
        self.assertIn("key-file read never attempted",
                      probe.key_problems(self.output(read=""), self.TOKEN, [])["problems"])
        vacuous = probe.key_problems('{"out": "nothing ran"}', self.TOKEN, [])["problems"]
        self.assertTrue(any("vacuous" in problem for problem in vacuous))
        self.assertEqual(probe.key_problems(self.output(), self.TOKEN, ["routed x"])["problems"], ["routed x"])


if __name__ == "__main__":
    unittest.main()
