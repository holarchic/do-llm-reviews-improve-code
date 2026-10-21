import json
import shutil
import tempfile
import unittest
from pathlib import Path

from eval.harness import tasks


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.task = tasks.Task(self.tmp / "dev-intervals")
        shutil.copytree(tasks.TASKS_ROOT / "dev-intervals", self.task.root)
        self.task.path("expected.json").unlink()

    def test_valid_task_writes_expected(self):
        self.assertEqual(tasks.validate(self.task), [])
        expected = json.loads(self.task.path("expected.json").read_text())
        self.assertEqual(len(expected["hidden"]), 8)
        self.assertEqual(len(expected["hidden_followup"]), 4)

    def test_wrong_variant_that_passes_is_rejected(self):
        shutil.copy(self.task.path("reference") / "intervals.py",
                    self.task.path("wrong") / "assumes-sorted" / "intervals.py")
        problems = tasks.validate(self.task)
        self.assertEqual(problems, ["dev-intervals: check failed — wrong/assumes-sorted fails hidden"])
        self.assertFalse(self.task.path("expected.json").exists())

    def test_followup_test_passing_on_base_is_rejected(self):
        shutil.copy(self.task.path("reference_followup") / "intervals.py",
                    self.task.path("reference") / "intervals.py")
        self.assertIn("dev-intervals: check failed — reference fails every follow-up test",
                      tasks.validate(self.task))

    def test_broken_reference_is_rejected(self):
        (self.task.path("reference") / "intervals.py").write_text("raise ImportError\n")
        self.assertTrue(tasks.validate(self.task))


if __name__ == "__main__":
    unittest.main()
