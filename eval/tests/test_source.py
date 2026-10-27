import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import score, source, tasks

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@localhost", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}

BASE_CALC = "def add(a, b):\n    return a + b\n"
PRE_REVIEW_CALC = BASE_CALC + "\n\ndef div(a, b):\n    return a / b\n"
REFERENCE_CALC = BASE_CALC + textwrap.dedent("""

    def div(a, b):
        if b == 0:
            raise ValueError("division by zero")
        return a / b


    safe_div = div
""")
BASE_TEST = textwrap.dedent("""
    import unittest
    from calc import add

    class AddTest(unittest.TestCase):
        def test_add(self):
            self.assertEqual(add(2, 3), 5)
""")
FEATURE_TEST = textwrap.dedent("""
    import unittest
    import calc

    class DivTest(unittest.TestCase):
        def test_div(self):
            self.assertEqual(calc.div(6, 3), 2)
""")
# ZeroDivisionError on pre_review is a crash inside the call: the defect itself.
REVIEW_TEST = textwrap.dedent("""
    import unittest
    import calc

    class ZeroTest(unittest.TestCase):
        def test_zero_is_a_value_error(self):
            with self.assertRaises(ValueError):
                calc.div(1, 0)
""")
DEFECTS = {"defects": [{"id": "zero", "fix_commit": "c3", "basis": "TASK.md: zero divisor is a ValueError",
                        "tests": ["tests.test_hidden_review_div.ZeroTest.test_zero_is_a_value_error"],
                        "crash_is_defect": True}]}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, capture_output=True, text=True, check=True).stdout


class FixtureCase(unittest.TestCase):
    """A three-commit source repo (base adds `add`, pre_review adds a `div` that crashes on zero, reference
    makes zero a ValueError) and a replay task over it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        repo = self.tmp / "upstream"
        (repo / "tests").mkdir(parents=True)
        git(repo, "init", "-q")
        (repo / "tests" / "__init__.py").write_text("")
        (repo / "tests" / "test_calc.py").write_text(BASE_TEST)
        commits = []
        for text in (BASE_CALC, PRE_REVIEW_CALC, REFERENCE_CALC):
            (repo / "calc.py").write_text(text)
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "c")
            commits.append(git(repo, "rev-parse", "HEAD").strip())
        patcher = mock.patch.object(source, "SOURCES_ROOT", self.tmp / "sources")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.task = tasks.Task(self.tmp / "tasks" / "fixture")
        hidden = self.task.path("hidden")
        hidden.mkdir(parents=True)
        (hidden / "test_hidden_feature_div.py").write_text(FEATURE_TEST)
        (hidden / "test_hidden_review_div.py").write_text(REVIEW_TEST)
        self.task.path("TASK.md").write_text("Add div(a, b); a zero divisor raises ValueError.\n")
        self.task.path("source.json").write_text(json.dumps(
            {"url": str(repo), "base": commits[0], "pre_review": commits[1], "reference": commits[2]}))
        self.write_defects(DEFECTS)

    def write_defects(self, defects: dict) -> None:
        self.task.path("defects.json").write_text(json.dumps(defects))

    def add_review_test(self, name: str, body: str, defect: dict) -> None:
        (self.task.path("hidden") / name).write_text(textwrap.dedent(body))
        data = json.loads(self.task.path("defects.json").read_text())
        data["defects"].append(defect)
        self.write_defects(data)


class ValidateTest(FixtureCase):
    def test_a_valid_task_records_groups_evidence_and_regressions(self):
        self.assertEqual(tasks.validate(self.task), [])
        expected = self.task.expected()
        self.assertEqual(expected["feature"], ["tests.test_hidden_feature_div.DivTest.test_div"])
        self.assertEqual(expected["regression"], ["tests.test_calc.AddTest.test_add"])
        evidence = expected["defects"][0]["pre_review"][DEFECTS["defects"][0]["tests"][0]]
        self.assertEqual((evidence["outcome"], evidence["stage"], evidence["exc"]), ("error", "call", "ZeroDivisionError"))

    def test_a_crash_counts_only_when_the_record_says_it_is_the_defect(self):
        defects = json.loads(json.dumps(DEFECTS))
        defects["defects"][0]["crash_is_defect"] = False
        self.write_defects(defects)
        problems = tasks.validate(self.task)
        self.assertTrue(any("does not reach the defect" in p for p in problems), problems)

    def test_a_review_test_that_fails_only_in_its_teardown_is_refused(self):
        self.add_review_test("test_hidden_review_teardown.py", """
            import unittest
            import calc

            class TeardownTest(unittest.TestCase):
                def test_div_works(self):
                    self.assertEqual(calc.div(4, 2), 2)

                def tearDown(self):
                    self.assertTrue(hasattr(calc, "safe_div"))
        """, {"id": "teardown", "tests": ["tests.test_hidden_review_teardown.TeardownTest.test_div_works"]})
        problems = "\n".join(tasks.validate(self.task))
        self.assertIn("does not reach the defect on pre_review (fail, teardown", problems)

    def test_a_failed_subtest_is_its_tests_outcome(self):
        self.add_review_test("test_hidden_review_sub.py", """
            import unittest
            import calc

            class SubTest(unittest.TestCase):
                def test_zero(self):
                    for b in (2, 0):
                        with self.subTest(b=b):
                            with self.assertRaises(ValueError):
                                calc.div(1, b) if b == 0 else (_ for _ in ()).throw(ValueError())
        """, {"id": "sub", "tests": ["tests.test_hidden_review_sub.SubTest.test_zero"], "crash_is_defect": True})
        self.assertEqual(tasks.validate(self.task), [])
        evidence = self.task.expected()["defects"][1]["pre_review"]["tests.test_hidden_review_sub.SubTest.test_zero"]
        self.assertEqual((evidence["outcome"], evidence["stage"]), ("error", "call"))

    def test_attribution_must_hold_on_every_pre_review_run(self):
        test_id = "tests.test_hidden_review_div.T.test"
        good = score.SuiteOutcome({test_id: "fail"}, False, {test_id: {"exc": "AssertionError", "stage": "call"}})
        drift = score.SuiteOutcome({test_id: "fail"}, False, {test_id: {"exc": "AssertionError", "stage": "setup"}})
        stable = {"reference": score.SuiteOutcome({test_id: "pass"}, False), "base": good, "pre_review": good}
        defects = [{"id": "d", "tests": [test_id]}]
        self.assertEqual(source._anchor_problems(self.task, stable, [], defects, [good, good, good]), [])
        problems = source._anchor_problems(self.task, stable, [], defects, [good, drift, good])
        self.assertIn("does not reach the defect on pre_review (fail, setup", "\n".join(problems))

    def test_a_control_flag_must_be_a_boolean(self):
        data = json.loads(self.task.path("source.json").read_text())
        self.task.path("source.json").write_text(json.dumps({**data, "control": "false"}))
        with self.assertRaises(source.SourceError):
            source.Source.load(self.task.path("source.json"))

    def test_a_review_test_that_passes_on_pre_review_is_refused(self):
        self.add_review_test("test_hidden_review_ok.py", """
            import unittest
            import calc

            class OkTest(unittest.TestCase):
                def test_div_works(self):
                    self.assertEqual(calc.div(4, 2), 2)
        """, {"id": "ok", "tests": ["tests.test_hidden_review_ok.OkTest.test_div_works"]})
        self.assertIn("passes on pre_review", "\n".join(tasks.validate(self.task)))

    def test_a_review_test_that_only_misses_a_name_is_refused(self):
        self.add_review_test("test_hidden_review_name.py", """
            import unittest
            import calc

            class NameTest(unittest.TestCase):
                def test_safe(self):
                    with self.assertRaises(ValueError):
                        calc.safe_div(1, 0)
        """, {"id": "name", "tests": ["tests.test_hidden_review_name.NameTest.test_safe"], "crash_is_defect": True})
        # safe_div exists only on reference: on pre_review the test dies on the missing name, not the defect.
        problems = tasks.validate(self.task)
        self.assertEqual(problems, ["fixture: review test tests.test_hidden_review_name.NameTest.test_safe (name) "
                                    "does not reach the defect on pre_review (error, call, AttributeError)"])

    def test_a_flaky_hidden_test_is_refused(self):
        marker = self.tmp / "seen"
        marker.mkdir()
        # Passes the first time it runs against a given copy of calc.py's anchor, fails after: varies per round.
        self.add_review_test("test_hidden_review_flaky.py", f"""
            import hashlib, pathlib, unittest
            import calc

            class FlakyTest(unittest.TestCase):
                def test_once(self):
                    key = hashlib.sha256(pathlib.Path(calc.__file__).read_bytes()).hexdigest()
                    seen = pathlib.Path({str(marker)!r}) / key
                    first = not seen.exists()
                    seen.touch()
                    self.assertTrue(first)
                    with self.assertRaises(ValueError):
                        calc.div(1, 0)
        """, {"id": "flaky", "tests": ["tests.test_hidden_review_flaky.FlakyTest.test_once"]})
        self.assertIn("flaky test tests.test_hidden_review_flaky", "\n".join(tasks.validate(self.task)))

    def test_empty_groups_and_unclaimed_review_tests_are_refused(self):
        self.task.path("hidden/test_hidden_feature_div.py").unlink()
        self.write_defects({"defects": []})
        problems = "\n".join(tasks.validate(self.task))
        self.assertIn("empty feature group", problems)
        self.assertIn("claim each review test exactly once", problems)

    def test_the_seed_is_base_plus_task_md_without_history(self):
        dest = self.tmp / "seed"
        source.seed(self.task, dest)
        files = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
        self.assertEqual(files, ["TASK.md", "calc.py", "tests/__init__.py", "tests/test_calc.py"])
        self.assertEqual((dest / "calc.py").read_text(), BASE_CALC)

    def test_a_missing_commit_fails_loudly(self):
        data = json.loads(self.task.path("source.json").read_text())
        data["reference"] = "0" * 40
        self.task.path("source.json").write_text(json.dumps(data))
        self.assertIn("has no commit reference", "\n".join(tasks.validate(self.task)))


class ScoreTest(FixtureCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(tasks.validate(self.task), [])

    def build(self, calc: str, *, tests: dict[str, str] | None = None) -> Path:
        artifact = self.tmp / f"build-{len(list(self.tmp.glob('build-*')))}"
        source.seed(self.task, artifact)
        (artifact / "calc.py").write_text(calc)
        for name, text in (tests or {}).items():
            (artifact / "tests" / name).write_text(text)
        return artifact

    def score(self, artifact: Path) -> dict:
        return source.score_build(artifact, self.task, self.tmp / f"scoring-{artifact.name}")["replay"]

    def test_the_reference_avoids_every_defect_and_regresses_nothing(self):
        result = self.score(self.build(REFERENCE_CALC))
        self.assertEqual((result["review"], result["feature"], result["regressions"]), (1.0, 1.0, 0.0))

    def test_the_historical_build_ships_the_defect(self):
        result = self.score(self.build(PRE_REVIEW_CALC))
        self.assertEqual((result["review"], result["feature"]), (0.0, 1.0))
        self.assertEqual(result["defects"], [{"id": "zero", "avoided": False}])

    def test_a_build_that_breaks_existing_behaviour_regresses(self):
        result = self.score(self.build(REFERENCE_CALC.replace("return a + b", "return a - b")))
        self.assertEqual(result["regressions"], 1.0)

    def test_a_build_cannot_raise_a_score_by_editing_tests(self):
        rigged = {"test_calc.py": "import unittest\n", "test_hidden_review_div.py": "import unittest\n"}
        result = self.score(self.build(PRE_REVIEW_CALC.replace("a + b", "a - b"), tests=rigged))
        self.assertEqual((result["review"], result["regressions"]), (0.0, 1.0))


if __name__ == "__main__":
    unittest.main()
