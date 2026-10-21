import json
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from eval.harness import planted, records, score, tasks

REFERENCE = "def add(a, b):\n    return a + b\n"
MUTANTS = {
    "off-by-one": "def add(a, b):\n    return a + b + 1\n",
    "negatives": "def add(a, b):\n    return a + b if a >= 0 else a - b\n",
    "large-only": "def add(a, b):\n    return a + b if a < 1000 else 0\n",
}


def subtle_mutants() -> dict[str, str]:
    """Each fails exactly one of the four hidden tests below (one per first argument)."""
    firsts = (1, 0, -2, 5)
    return {f"m{i}": f"def add(a, b):\n    return a + b if a != {firsts[i % 4]} else 99\n"
            for i in range(tasks.MIN_MUTANTS)}


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(textwrap.dedent(text))


class _TaskFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.task = tasks.Task(self.tmp / "task")
        write(self.task.root, {"reference/calc.py": REFERENCE,
                               **{f"mutants/{name}/calc.py": text for name, text in MUTANTS.items()}})

    def build(self, files: dict[str, str]) -> Path:
        artifact = self.tmp / "artifact"
        write(artifact, files)
        return artifact


class PlantedScoreTest(_TaskFixture):
    def test_split_build_with_public_api_tests_catches_what_its_tests_exercise(self):
        artifact = self.build({
            # The build's public module re-exports a helper module; the reference replaces calc.py only.
            "calc.py": "from _impl import add\n",
            "_impl.py": "def add(a, b):\n    return b + a\n",
            "test.py": """
                import unittest
                from calc import add
                class T(unittest.TestCase):
                    def test_small(self):
                        self.assertEqual(add(2, 2), 4)
            """,
            "tests/__init__.py": "",
            "tests/test_signs.py": """
                import unittest
                from calc import add
                class T(unittest.TestCase):
                    def test_negative(self):
                        self.assertEqual(add(-1, 1), 0)
            """,
            # Tests of the build's own helper pass on the reference and can never catch a planted bug.
            "test_helper.py": """
                import unittest
                import _impl
                class T(unittest.TestCase):
                    def test_helper(self):
                        self.assertEqual(_impl.add(1, 1), 2)
            """,
            # An internal name the reference lacks: a load error, excluded.
            "test_internal.py": "from calc import _secret\n",
        })
        result = planted.score_build(artifact, self.task, self.tmp / "work")
        self.assertEqual(sorted(result["caught"]), ["negatives", "off-by-one"])
        self.assertEqual(result["missed"], ["large-only"])
        self.assertAlmostEqual(result["fraction"], 2 / 3)
        self.assertEqual(result["own_tests"], {"collected": 3, "pass_on_reference": 3, "fail_on_reference": 0,
                                               "load_errors": 1})
        self.assertTrue(records._planted(result))

    def test_tests_failing_on_the_reference_do_not_count(self):
        artifact = self.build({
            "calc.py": "def add(a, b):\n    return a + b + 1\n",
            # Written against the build's own bug: fails on the reference, so it can't be credited.
            "test_calc.py": """
                import unittest
                from calc import add
                class T(unittest.TestCase):
                    def test_matches_own_code(self):
                        self.assertEqual(add(1, 1), 3)
            """,
        })
        result = planted.score_build(artifact, self.task, self.tmp / "work")
        self.assertEqual(result["caught"], [])
        self.assertEqual(result["fraction"], 0.0)
        self.assertEqual(result["own_tests"]["fail_on_reference"], 1)

    def test_a_build_without_tests_catches_nothing(self):
        result = planted.score_build(self.build({"calc.py": REFERENCE}), self.task, self.tmp / "work")
        self.assertEqual((result["fraction"], result["own_tests"]["collected"]), (0.0, 0))

    def test_each_run_starts_from_a_fresh_copy(self):
        # A test that writes into its cwd must not see the previous run's file.
        artifact = self.build({
            "calc.py": REFERENCE,
            "test_calc.py": """
                import os, unittest
                from calc import add
                class T(unittest.TestCase):
                    def test_fresh(self):
                        self.assertFalse(os.path.exists("marker"))
                        open("marker", "w").close()
                        self.assertEqual(add(-1, 1), 0)
            """,
        })
        result = planted.score_build(artifact, self.task, self.tmp / "work")
        self.assertEqual(sorted(result["caught"]), ["negatives", "off-by-one"])
        self.assertFalse((artifact / "marker").exists())


class RunInPlaceTest(_TaskFixture):
    def test_hidden_suites_keep_their_pattern(self):
        artifact = self.build({"calc.py": REFERENCE, "test.py": "import unittest\nclass T(unittest.TestCase):\n"
                                                                "    def test_x(self):\n        pass\n"})
        self.assertEqual(len(score.run_in_place(artifact, self.tmp / "a").outcomes), 1)
        self.assertEqual(len(score.run_in_place(artifact, self.tmp / "b", pattern="test_*.py").outcomes), 0)


class ValidateMutantsTest(_TaskFixture):
    def setUp(self):
        super().setUp()
        write(self.task.root, {
            "TASK.md": "add\n", "traps.md": "none\n",
            "hidden/test_calc.py": """
                import unittest
                from calc import add
                class T(unittest.TestCase):
                    def test_a(self): self.assertEqual(add(1, 2), 3)
                    def test_b(self): self.assertEqual(add(0, 0), 0)
                    def test_c(self): self.assertEqual(add(-2, 1), -1)
                    def test_d(self): self.assertEqual(add(5, 5), 10)
            """,
            "wrong/sub/calc.py": "def add(a, b):\n    return a - b\n",
        })
        shutil.rmtree(self.task.path("mutants"))

    def mutants(self, texts: dict[str, str]):
        write(self.task.root, {f"mutants/{name}/calc.py": text for name, text in texts.items()})

    def test_a_valid_task_without_followup_or_degraded(self):
        self.mutants(subtle_mutants())
        self.assertEqual(tasks.validate(self.task), [])
        self.assertEqual(list(json.loads(self.task.path("expected.json").read_text())), ["hidden"])
        self.assertEqual(tasks.missing_parts(self.task, followup=False, degraded=False, mutants=True), [])
        self.assertEqual(tasks.missing_parts(self.task, followup=True, degraded=True, mutants=True),
                         ["task: no follow-up group", "task: no degraded/"])

    def test_a_crashing_or_harmless_mutant_fails_validation(self):
        texts = subtle_mutants()
        texts["m0"] = "def add(a, b):\n    raise RuntimeError\n"  # fails every test: not subtle
        texts["m1"] = REFERENCE  # fails nothing: not a bug
        self.mutants(texts)
        problems = tasks.validate(self.task)
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("mutants/m0" in p or "mutants/m1" in p for p in problems))

    def test_too_few_mutants(self):
        self.mutants({"m0": "def add(a, b):\n    return 0\n"})
        self.assertEqual(tasks.validate(self.task), [f"task: needs at least {tasks.MIN_MUTANTS} mutants"])

    def test_partial_followup_group_is_missing_parts(self):
        write(self.task.root, {"FOLLOWUP.md": "more\n"})
        self.assertIn("missing hidden_followup", tasks.validate(self.task)[0])


if __name__ == "__main__":
    unittest.main()
