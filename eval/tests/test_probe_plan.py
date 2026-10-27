import json
import unittest

from eval.harness import agents, experiments, openrouter, probe


class ProbePlanTest(unittest.TestCase):
    def test_pilot_probes_its_own_agents(self):
        plan = {p.name: p for p in probe.probe_plan(experiments.PILOT)}
        self.assertEqual(set(plan), {"claude_instructions", "codex_instructions", "claude_judge_instructions",
                                     "codex_judge_instructions", "claude_canary", "codex_canary",
                                     "claude_reviewer_read_only"})
        self.assertEqual(plan["claude_canary"].agent, experiments.HAIKU)

    def test_a_codex_writer_gets_the_writer_control(self):
        plan = {p.name: p for p in probe.probe_plan(experiments.EXPERIMENTS["reverse-sonnet"])}
        self.assertEqual((plan["codex_writer"].agent, plan["codex_writer"].role),
                         (experiments.TERRA, agents.WRITER))

    def test_pilot2_probes_every_routed_session_and_the_key(self):
        plan = {p.name: p for p in probe.probe_plan(experiments.PILOT2)}
        self.assertEqual(set(plan), {"claude_instructions", "codex_instructions",
                                     "codex_instructions:z-ai/glm-4.7-flash", "claude_canary", "codex_canary",
                                     "claude_key", "codex_key", "claude_passthrough",
                                     "codex_passthrough"})
        self.assertTrue(all(p.agent.provider == agents.OPENROUTER for p in plan.values()))
        self.assertEqual(plan["claude_key"].agent, experiments.QWEN_WRITER)
        self.assertEqual(plan["codex_key"].role, agents.REVIEWER)

    def test_the_replay_probes_every_configuration_offline(self):
        plan = {p.name: p for p in probe.probe_plan(experiments.REPLAY)}
        self.assertTrue(all(p.offline for p in plan.values()))
        network = {(p.agent, p.role) for p in plan.values() if p.kind == "network"}
        self.assertEqual(network, {(experiments.OPUS, agents.WRITER), (experiments.OPUS, agents.REVIEWER),
                                   (experiments.SOL, agents.REVIEWER)})
        self.assertFalse(any(p.offline or p.kind == "network" for p in probe.probe_plan(experiments.PILOT)))


class NetworkProblemsTest(unittest.TestCase):
    STEPS = probe.network_steps("20.26.156.215", "LOCAL-1")
    CLEAN = {
        "permitted": "PERMITTED-1",
        "localhost": "LOCAL-OK LOCAL-1",
        "git": "fatal: unable to access 'https://github.com/x/': CONNECT tunnel failed, response 403",
        "curl": "curl: (56) CONNECT tunnel failed, response 403\nHTTP 000",
        "bypass": "curl: (7) Failed to connect to github.com port 443 after 0 ms: Couldn't connect to server\nHTTP 000",
        "socket": "DIRECT-RESULT EPERM\nUNIX-RESULT EPERM",
    }
    EGRESS = "ALLOW CONNECT chatgpt.com:443\nDENY CONNECT github.com:443\n"

    def problems(self, outputs=None, *, egress_log=EGRESS, search=None, drop=()):
        outputs = {**self.CLEAN, **(outputs or {})}
        commands = [{"command": f"bash -lc '{self.STEPS[name]}'", "output": outputs[name]}
                    for name in self.STEPS if name not in drop]
        search = {"config_disabled": True, "web_search_items": 0} if search is None else search
        return probe.network_problems(commands, self.STEPS, permitted="PERMITTED-1", local="LOCAL-1",
                                      egress_log=egress_log, search=search)

    def test_clean_codex_and_claude_evidence_passes(self):
        self.assertEqual(self.problems(), [])
        self.assertEqual(self.problems(egress_log=None, search={"tools": ["Bash", "Read"]}), [])

    def test_each_escape_or_missing_control_is_a_problem(self):
        cases = {
            "permitted local read failed": {"permitted": "Permission denied"},
            "localhost client/server exchange failed": {"localhost": "ConnectionRefusedError"},
            "git: reached the source": {"git": "0123456789abcdef0123456789abcdef01234567\tHEAD"},
            "curl: reached the source": {"curl": "HTTP 200"},
            "curl: no proxy refusal": {"curl": "curl: (6) Could not resolve host\nHTTP 000"},
            "bypass: reached the source": {"bypass": "HTTP 301"},
            "bypass: no connection refusal": {"bypass": "curl: (6) Could not resolve host: github.com\nHTTP 000"},
            "direct socket not denied": {"socket": "DIRECT-RESULT CONNECTED\nUNIX-RESULT EPERM"},
            "unix socket not denied": {"socket": "DIRECT-RESULT EPERM\nUNIX-RESULT ENOENT"},
        }
        for expected, outputs in cases.items():
            found = self.problems(outputs)
            self.assertTrue(any(problem.startswith(expected) for problem in found), (expected, found))

    def test_a_way_out_that_ran_with_no_output_is_unproven(self):
        for step, expected in (("git", "git: no proxy refusal"), ("curl", "curl: no proxy refusal"),
                               ("bypass", "bypass: no connection refusal"), ("socket", "direct socket not denied")):
            found = self.problems({step: ""})
            self.assertTrue(any(problem.startswith(expected) for problem in found), (step, found))

    def test_a_step_that_never_ran_is_unproven(self):
        self.assertIn("bypass step never ran; unproven", self.problems(drop=("bypass",)))

    def test_codex_needs_proxy_evidence_and_search_off(self):
        self.assertIn("no model call through the egress proxy", self.problems(egress_log="DENY CONNECT github.com:443"))
        self.assertIn("the egress proxy never refused the source",
                      self.problems(egress_log="ALLOW CONNECT chatgpt.com:443"))
        self.assertIn("Codex web search is not disabled in its config",
                      self.problems(search={"config_disabled": False, "web_search_items": 0}))
        self.assertIn("Codex ran a web search", self.problems(search={"config_disabled": True, "web_search_items": 1}))

    def test_claude_needs_a_read_tool_list_without_web_tools(self):
        self.assertTrue(self.problems(egress_log=None, search={"tools": []}))
        self.assertTrue(self.problems(egress_log=None, search={"tools": ["Bash", "WebFetch"]}))

    def test_script_sources_in_a_transcript_are_not_their_results(self):
        found = self.problems({"socket": probe.DIRECT_SCRIPT, "localhost": probe.LOCAL_SCRIPT})
        self.assertIn("localhost client/server exchange failed", found)
        self.assertTrue(any(problem.startswith("direct socket") for problem in found))


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
