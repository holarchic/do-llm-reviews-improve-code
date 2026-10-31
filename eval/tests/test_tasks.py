import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import manifest, tasks


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


class ValidateCommandTest(unittest.TestCase):
    """`validate --task` touches only the tasks it names: a frozen task's expected.json is never rewritten."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.root)
        for name in ("dev-a", "dev-b"):
            shutil.copytree(tasks.TASKS_ROOT / "dev-intervals", self.root / name)
            (self.root / name / "expected.json").write_text("frozen\n")
        self.manifests = Path(tempfile.mkdtemp(prefix="eval-test-manifests-"))
        self.addCleanup(shutil.rmtree, self.manifests)
        for name, value in (("MANIFEST", self.manifests / "MANIFEST.json"), ("MANIFESTS", self.manifests)):
            patcher = mock.patch.object(manifest, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def freeze(self, task_id: str, text: str | None = None) -> None:
        files = {f"eval/tasks/{task_id}/expected.json": "0" * 64}
        (self.manifests / "replay.json").write_text(text if text is not None else json.dumps({"files": files}))

    def validate(self, *argv: str) -> int:
        from eval.__main__ import main
        load = tasks.load_tasks
        with mock.patch.object(tasks, "load_tasks", lambda: load(self.root)), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return main(["validate", *argv])

    def expected(self, name: str) -> str:
        return (self.root / name / "expected.json").read_text()

    def test_only_the_named_task_is_validated(self):
        self.assertEqual(self.validate("--task", "dev-a"), 0)
        self.assertNotEqual(self.expected("dev-a"), "frozen\n")
        self.assertEqual(self.expected("dev-b"), "frozen\n")

    def test_a_repeated_task_flag_validates_each(self):
        self.assertEqual(self.validate("--task", "dev-a", "--task", "dev-b"), 0)
        self.assertNotEqual(self.expected("dev-a"), "frozen\n")
        self.assertNotEqual(self.expected("dev-b"), "frozen\n")

    def test_an_unknown_task_validates_nothing(self):
        with mock.patch.object(tasks, "validate") as validated:
            self.assertEqual(self.validate("--task", "dev-a", "--task", "nope"), 2)
        validated.assert_not_called()
        self.assertEqual(self.expected("dev-a"), "frozen\n")

    def test_a_named_frozen_task_is_refused_and_nothing_validated(self):
        self.freeze("dev-b")
        with mock.patch.object(tasks, "validate") as validated:
            self.assertEqual(self.validate("--task", "dev-a", "--task", "dev-b"), 2)
        validated.assert_not_called()
        self.assertEqual((self.expected("dev-a"), self.expected("dev-b")), ("frozen\n", "frozen\n"))

    def test_bare_validate_skips_a_frozen_task(self):
        self.freeze("dev-b")
        self.assertEqual(self.validate(), 0)
        self.assertNotEqual(self.expected("dev-a"), "frozen\n")
        self.assertEqual(self.expected("dev-b"), "frozen\n")

    def test_an_unreadable_manifest_refuses_everything(self):
        for text in ("{not json", '{"files": []}', '{"files": "broken"}', '{"files": null}', '{"files": {"1": 2}}',
                     "[]", "{}"):
            with self.subTest(text=text), mock.patch.object(tasks, "validate") as validated:
                self.freeze("dev-b", text=text)
                self.assertEqual(self.validate("--task", "dev-a"), 3)
                validated.assert_not_called()

    def test_a_manifest_that_cannot_be_read_refuses_everything(self):
        self.freeze("dev-b")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")), \
                mock.patch.object(tasks, "validate") as validated:
            self.assertEqual(self.validate("--task", "dev-a"), 3)
        validated.assert_not_called()


class FrozenTasksTest(unittest.TestCase):
    def test_the_real_manifests_freeze_every_v1_task_and_no_v2(self):
        from eval.harness import frozen

        held = frozen.frozen_tasks()
        replay = ("email", "greenhouse", "workable-gem", "setup-commands", "watchlist", "company-notes")
        for name in replay:
            self.assertEqual(held.get(f"jobscan-{name}"), "replay.json")
            self.assertNotIn(f"jobscan-{name}-v2", held)
        self.assertEqual(held.get("bill-split"), "MANIFEST.json")
        self.assertNotIn("dev-intervals", held)


if __name__ == "__main__":
    unittest.main()
