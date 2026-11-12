import json
import unittest

from eval.harness import agents, experiments, probe, providers


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

    def test_each_routed_provider_gets_its_own_key_and_passthrough_probes(self):
        import dataclasses
        from eval.harness import agents
        writer = agents.Agent("claude", "z-ai/glm-5.3", agents.OPENROUTER)
        reviewer = agents.Agent("codex", "deepseek-v4-pro", agents.DEEPSEEK)
        other = agents.Agent("codex", "deepseek-flash", agents.DEEPSEEK)
        experiment = dataclasses.replace(experiments.PILOT2, arms=(
            experiments.Arm("W", writer), experiments.Arm("WR", writer, reviewer), experiments.Arm("A5", writer, other)))
        plan = {p.name: p for p in probe.probe_plan(experiment)}
        self.assertEqual(plan["claude_key"].agent, writer)
        self.assertEqual(plan["codex_key_deepseek"].agent, reviewer)  # one per CLI and provider: the first seen
        self.assertIn("codex_passthrough_deepseek", plan)
        self.assertNotIn("codex_key", plan)

    def test_a_later_arms_writer_is_probed_too(self):
        import dataclasses
        from eval.harness import agents
        glm = agents.Agent("claude", "z-ai/glm-5.3", agents.OPENROUTER)
        ds = agents.Agent("codex", "deepseek-v4-pro", agents.DEEPSEEK)
        experiment = dataclasses.replace(experiments.REPLAY, arms=(experiments.Arm("W", glm), experiments.Arm("D", ds)))
        plan = {p.name: p for p in probe.probe_plan(experiment)}
        self.assertEqual(plan["codex_key_deepseek"].agent, ds)
        self.assertEqual(plan["codex_network_writer"].agent, ds)
        self.assertEqual(plan["codex_writer"].agent, ds)
        self.assertIn("codex_instructions", plan)

    def test_the_replay_probes_every_configuration_offline(self):
        plan = {p.name: p for p in probe.probe_plan(experiments.REPLAY)}
        self.assertTrue(all(p.offline for p in plan.values()))
        network = {(p.agent, p.role) for p in plan.values() if p.kind == "network"}
        self.assertEqual(network, {(experiments.OPUS, agents.WRITER), (experiments.OPUS, agents.REVIEWER),
                                   (experiments.SOL, agents.REVIEWER)})
        self.assertFalse(any(p.offline or p.kind == "network" for p in probe.probe_plan(experiments.PILOT)))

    def test_plain_review_probes_its_cells_reviewers_offline_and_no_judge(self):
        plan = {p.name: p for p in probe.probe_plan(experiments.PLAIN_REVIEW)}
        self.assertEqual(set(plan), {"claude_instructions", "codex_instructions", "claude_canary", "codex_canary",
                                     "claude_reviewer_read_only", "claude_network_writer", "claude_network_reviewer",
                                     "codex_network_reviewer"})
        self.assertTrue(all(p.offline for p in plan.values()))
        self.assertEqual((plan["codex_network_reviewer"].agent, plan["claude_network_reviewer"].agent),
                         (experiments.SOL, experiments.OPUS))


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

    def problems(self, outputs=None, *, egress_log=EGRESS, search=None, drop=(), routed_answered=None):
        outputs = {**self.CLEAN, **(outputs or {})}
        commands = [{"command": f'/bin/zsh -lc "{self.STEPS[name]}"', "output": outputs[name]}
                    for name in self.STEPS if name not in drop]
        search = {"config_disabled": True, "web_search_items": 0} if search is None else search
        return probe.network_problems(commands, self.STEPS, permitted="PERMITTED-1", local="LOCAL-1",
                                      egress_log=egress_log, search=search, routed_answered=routed_answered)

    def test_clean_codex_and_claude_evidence_passes(self):
        self.assertEqual(self.problems(), [])
        self.assertEqual(self.problems(egress_log=None, search={"tools": ["Bash", "Read"]}), [])

    def test_a_routed_codex_session_proves_its_model_call_through_the_routing_proxy(self):
        routed_egress = "DENY CONNECT github.com:443\n"  # its model call never touches the egress proxy
        self.assertEqual(self.problems(egress_log=routed_egress, routed_answered=2), [])
        self.assertIn("no model call through the routing proxy",
                      self.problems(egress_log=routed_egress, routed_answered=0))
        self.assertIn("a routed session's egress proxy admitted a connection",
                      self.problems(egress_log=self.EGRESS, routed_answered=1))
        self.assertIn("no model call through the egress proxy", self.problems(egress_log=routed_egress))  # native
        self.assertEqual(self.problems(egress_log=None, search={"tools": ["Bash"]}, routed_answered=1), [])  # Claude

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

    def test_an_upstream_failure_is_not_a_refusal(self):
        found = self.problems({"git": "fatal: unable to access: CONNECT tunnel failed, response 502",
                               "curl": "curl: (56) CONNECT tunnel failed, response 502\nHTTP 000"})
        self.assertTrue(any(p.startswith("git: no proxy refusal") for p in found), found)
        self.assertTrue(any(p.startswith("curl: no proxy refusal") for p in found), found)

    def test_a_later_run_of_the_socket_step_that_connects_is_caught(self):
        commands = [{"command": f'/bin/zsh -lc "{self.STEPS[name]}"', "output": self.CLEAN[name]} for name in self.STEPS]
        commands.append({"command": self.STEPS["socket"], "output": "DIRECT-RESULT CONNECTED\nUNIX-RESULT CONNECTED"})
        found = probe.network_problems(commands, self.STEPS, permitted="PERMITTED-1", local="LOCAL-1",
                                       egress_log=self.EGRESS, search={"config_disabled": True, "web_search_items": 0})
        self.assertTrue(any(p.startswith("direct socket not denied") for p in found), found)
        self.assertTrue(any(p.startswith("unix socket not denied") for p in found), found)

    def test_every_run_of_a_way_out_needs_its_own_evidence(self):
        commands = [{"command": f'/bin/zsh -lc "{self.STEPS[name]}"', "output": self.CLEAN[name]} for name in self.STEPS]
        commands += [{"command": self.STEPS["socket"], "output": "Traceback: killed"},
                     {"command": self.STEPS["git"], "output": ""}]
        found = probe.network_problems(commands, self.STEPS, permitted="PERMITTED-1", local="LOCAL-1",
                                       egress_log=self.EGRESS, search={"config_disabled": True, "web_search_items": 0})
        for expected in ("direct socket not denied", "unix socket not denied", "git: no proxy refusal"):
            self.assertTrue(any(p.startswith(expected) for p in found), (expected, found))

    def test_a_denial_then_a_connection_in_one_output_is_caught(self):
        found = self.problems({"socket": "DIRECT-RESULT EPERM\nUNIX-RESULT EPERM\nDIRECT-RESULT CONNECTED\n"
                                         "UNIX-RESULT CONNECTED"})
        self.assertTrue(any(p.startswith("direct socket not denied") for p in found), found)
        self.assertTrue(any(p.startswith("unix socket not denied") for p in found), found)

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

    # What a Claude session's Bash calls look like once recorded: the command as typed, no wrapper.
    HELD = ("This Bash command contains multiple operations. The following parts require approval: "
            "git ls-remote https://github.com/holarchic/job-board-scanner, echo \"EXIT=$?\"")
    REWORDED = {
        "localhost": "python3 -B -I local_exchange.py LOCAL-1",
        "git": "git ls-remote https://github.com/holarchic/job-board-scanner 2>&1",
        "curl": "curl -sS -m 20 https://github.com/holarchic/job-board-scanner -o /dev/null -w 'HTTP %{http_code}\\n' 2>&1",
        "bypass": "curl -sS -m 20 --noproxy '*' --resolve github.com:443:140.82.121.4 https://github.com/ -o /dev/null "
                  "-w 'HTTP %{http_code}\\n' 2>&1",
        "socket": "python3 -B -I direct_socket.py 140.82.121.4 /var/run/mDNSResponder 2>&1",
    }

    def claude(self, calls: dict[str, list[tuple[str, str]]]) -> list[str]:
        """The problems of a Claude session's recorded calls: each step's clean call, or the (command, output)
        calls given for it."""
        commands = [{"command": command, "output": output} for name in self.STEPS
                    for command, output in calls.get(name, [(self.STEPS[name], self.CLEAN[name])])]
        return probe.network_problems(commands, self.STEPS, permitted="PERMITTED-1", local="LOCAL-1",
                                      egress_log=None, search={"tools": ["Bash", "Read"]})

    def test_a_reworded_command_that_ran_and_was_refused_is_evidence(self):
        self.assertEqual(self.claude({}), [])
        reworded = {name: [(command, self.CLEAN[name])] for name, command in self.REWORDED.items()}
        self.assertEqual(self.claude(reworded), [])
        for name, command in self.REWORDED.items():  # and a reworded command that got through still fails
            leaked = "DIRECT-RESULT CONNECTED\nUNIX-RESULT EPERM" if name == "socket" else "HTTP 200"
            if name != "localhost":
                with self.subTest(name):
                    self.assertTrue(self.claude({name: [(command, leaked)]}))

    def test_a_command_held_for_approval_is_not_a_run(self):
        held = (self.STEPS["git"] + ' 2>&1; echo "EXIT=$?"', self.HELD)
        self.assertEqual(self.claude({"git": [held, (self.STEPS["git"], self.CLEAN["git"])]}), [])
        self.assertEqual(self.claude({"git": [held]}), ["git step never ran; unproven"])

    def test_an_echo_trailer_beside_one_operation_is_ignored(self):
        git, refused = self.STEPS["git"], self.CLEAN["git"]
        for command in (f'{git} 2>&1; echo "EXIT=$?"', f'{git}; echo " EXIT=$?"', f'echo "---"; {git}; echo done',
                        f"{git} || echo"):
            with self.subTest(command):
                self.assertEqual(self.claude({"git": [(command, refused + "\nEXIT=128")]}), [])

    def test_a_companion_that_could_print_a_refusal_proves_nothing(self):
        git, bypass = self.STEPS["git"], self.STEPS["bypass"]
        skipped = {  # each prints the refusal itself and never runs the probe's command
            "git": [(f'echo "403 Forbidden" || {git}', "403 Forbidden"),
                    (f'cd "response 403" && {git}', "cd: no such file or directory: response 403"),
                    (f"cd tmp && {git}", self.CLEAN["git"]),  # only an echo trailer may stand beside
                    (f"printf 'response 403\\n' '&&' {git}", "response 403"),
                    (f"printf 'response 403\\n' && {git}", "response 403"),
                    (f"sh -nc '{git}'; echo response 403", "response 403"),
                    (f'echo response 403 >&2; {git}', "response 403")],
            "bypass": [(f'echo "Failed to connect" || {bypass}', "Failed to connect")],
        }
        for step, calls in skipped.items():
            for call in calls:
                with self.subTest(call[0]):
                    self.assertEqual(self.claude({step: [call]}), [f"{step} step never ran; unproven"])

    def test_a_quoted_operator_is_an_argument_and_an_unquoted_one_separates(self):
        self.assertEqual(probe._simple_commands("printf 'a b' '&&' git \"x;y\" z\\;w"),
                         [["printf", "a b", "&&", "git", "x;y", "z;w"]])
        self.assertEqual(probe._simple_commands("a 2>&1 && b >out 3; c|d\ne ''"),
                         [["a"], ["b", "3"], ["c"], ["d"], ["e", ""]])
        self.assertEqual(probe._simple_commands("/bin/zsh -lc \"echo 'x y' && cat permitted.txt\""),
                         [["echo", "x y"], ["cat", "permitted.txt"]])
        self.assertEqual(probe._simple_commands("sh -nc 'cat permitted.txt'"), [["sh", "-nc", "cat permitted.txt"]])
        self.assertEqual(probe._simple_commands("echo 'never closed"), [])

    def test_an_option_value_the_probe_does_not_use_makes_curl_unrecognised(self):
        source = "https://github.com/holarchic/job-board-scanner"
        public = "--noproxy '*' --resolve github.com:443:140.82.121.4"
        cases = {
            "curl": [(f"curl -sS -m 20 {source} -o /dev/null -w 'response 403'", "response 403"),
                     (f"curl -sS -m 20 {source} -o page.html -w 'HTTP %{{http_code}}'", self.CLEAN["curl"]),
                     (f"curl -sS -m 20 https://github.com:8443/x -o /dev/null", self.CLEAN["curl"]),
                     (f"curl -sS -m 20 https://github.com:x/ -o /dev/null", self.CLEAN["curl"])],
            "bypass": [(f"curl -sS -m 20 {public} https://github.com/ -o /dev/null -w 'Failed to connect'",
                        "Failed to connect"),
                       (f"curl -sS -m 20 {public} https://github.com:1/ -o /dev/null", self.CLEAN["bypass"])],
        }
        for step, calls in cases.items():
            for call in calls:
                with self.subTest(call[0]):
                    self.assertEqual(self.claude({step: [call]}), [f"{step} step never ran; unproven"])
        on_443 = f"curl -sS -m 20 {public} https://github.com:443/ -o /dev/null -w 'HTTP %{{http_code}}\\n'"
        self.assertEqual(self.claude({"bypass": [(on_443, self.CLEAN["bypass"])]}), [])

    def test_a_call_holding_two_probe_operations_proves_neither(self):
        both = f"{self.STEPS['git']} && {self.STEPS['curl']}"  # git's failure skips curl; one 403 would serve both
        found = self.claude({"git": [(both, self.CLEAN["git"])], "curl": []})
        self.assertEqual(found, ["git step never ran; unproven", "curl step never ran; unproven"])
        found = self.claude({"git": [(both, "0123456789abcdef0123456789abcdef01234567\tHEAD\nHTTP 200")], "curl": []})
        self.assertIn("a call that is not one recognised probe operation shows a connection", found)

    def test_a_command_the_probe_does_not_know_beside_an_operation_proves_nothing(self):
        # The first curl is refused and `&&` skips the probe's own command, whose refusal this is not.
        elsewhere, refused = "curl -sS -m 20 https://example.com/ && ", "curl: (56) CONNECT tunnel failed, response 403"
        for step in ("git", "curl"):
            with self.subTest(step):
                self.assertEqual(self.claude({step: [(elsewhere + self.STEPS[step], refused)]}),
                                 [f"{step} step never ran; unproven"])
        piped = self.claude({"git": [(self.STEPS["git"] + " 2>&1 | tail -1", self.CLEAN["git"])]})
        self.assertEqual(piped, ["git step never ran; unproven"])

    def test_a_call_the_probe_cannot_place_still_fails_when_it_shows_a_connection(self):
        followed = ("curl -sS -L https://github.com/holarchic/job-board-scanner -o /dev/null -w 'HTTP %{http_code}'",
                    "HTTP 200")
        found = self.claude({"curl": [followed, (self.STEPS["curl"], self.CLEAN["curl"])]})
        self.assertEqual(found, ["a call that is not one recognised probe operation shows a connection"])

    def test_an_option_that_could_move_the_target_makes_a_command_unrecognised(self):
        source = "https://github.com/holarchic/job-board-scanner"
        cases = {
            "curl": [f"curl -sS -m 20 -x http://127.0.0.1:9 {source} -o /dev/null",
                     f"curl -sS -m 20 --connect-to github.com:443:example.com:443 {source}",
                     f"curl -sS -m 20 {source} https://example.com/",
                     f"curl -sS -m 20 --resolve github.com:443:127.0.0.1 {source}",
                     "curl -sS -m 20 http://github.com/holarchic/job-board-scanner"],
            "git": [f"git -c http.proxy=http://127.0.0.1:9 ls-remote {source}",
                    f"git ls-remote --upload-pack=/bin/true {source}",
                    "git ls-remote https://example.com/x"],
            "socket": ["python3 -c 'print(1)' direct_socket.py 140.82.121.4 /var/run/mDNSResponder",
                       "python3 -X dev direct_socket.py 140.82.121.4 /var/run/mDNSResponder"],
        }
        for step, commands in cases.items():
            for command in commands:
                with self.subTest(command):
                    self.assertEqual(self.claude({step: [(command, self.CLEAN[step])]}),
                                     [f"{step} step never ran; unproven"])

    def test_a_way_round_aimed_at_this_machine_or_nowhere_is_not_evidence(self):
        refused = self.CLEAN["bypass"]
        public = "--resolve github.com:443:140.82.121.4"
        for command in ("curl -sS -m 20 --noproxy '*' --resolve github.com:443:127.0.0.1 https://github.com/ -o /dev/null",
                        "curl -sS -m 20 --noproxy '*' --resolve github.com:443:192.168.1.1 https://github.com/",
                        "curl -sS -m 20 --noproxy '*' https://github.com/ -o /dev/null",
                        "curl -sS -m 20 --noproxy '*' --resolve example.com:443:140.82.121.4 https://github.com/",
                        f"curl -sS -m 20 --noproxy '*' {public} --connect-to github.com:443:127.0.0.1:1 https://github.com/",
                        f"curl -sS -m 20 --noproxy '*' {public} --resolve github.com:443:127.0.0.1 https://github.com/",
                        f"curl -sS -m 20 --noproxy example.com {public} https://github.com/"):
            with self.subTest(command):
                self.assertEqual(self.claude({"bypass": [(command, refused)]}), ["bypass step never ran; unproven"])
        for command in ("python3 -B direct_socket.py 127.0.0.1 /var/run/mDNSResponder",
                        "python3 -B direct_socket.py 140.82.121.4 /tmp/absent.sock",
                        "python3 -B direct_socket.py 140.82.121.4"):
            with self.subTest(command):
                self.assertEqual(self.claude({"socket": [(command, self.CLEAN["socket"])]}),
                                 ["socket step never ran; unproven"])

    def test_a_call_that_cannot_be_split_proves_nothing(self):
        self.assertEqual(self.claude({"git": [(self.STEPS["git"] + " 'unbalanced", self.CLEAN["git"])]}),
                         ["git step never ran; unproven"])

    def test_a_codex_wrapper_is_unwrapped(self):
        wrapped = {name: [(f'/bin/zsh -lc "{command}"', self.CLEAN[name])] for name, command in self.STEPS.items()}
        self.assertEqual(self.claude(wrapped), [])

    def test_a_claude_bash_call_is_read_as_its_command(self):
        self.assertEqual(probe._bash_command(json.dumps({"command": "cat permitted.txt", "description": "step 1"})),
                         "cat permitted.txt")
        self.assertEqual(probe._bash_command("null"), "")


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
        for name in providers.CREDENTIAL_VARIABLES:
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
