import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import loop, manifest, pilot


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.task_file = self.tmp / "eval" / "tasks" / "t" / "TASK.md"
        self.harness_file = self.tmp / "eval" / "harness" / "loop.py"
        for path in (self.task_file, self.harness_file):
            path.parent.mkdir(parents=True)
            path.write_text("original\n")
        for target, value in (("MANIFEST", self.tmp / "MANIFEST.json"), ("REPO_ROOT", self.tmp),
                              ("frozen_files", lambda: [self.task_file, self.harness_file])):
            patcher = mock.patch.object(manifest, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.digest = manifest.freeze()

    def test_unchanged_inputs_verify(self):
        self.assertEqual(manifest.verify(), self.digest)

    def test_changed_task_byte_refuses_run(self):
        self.task_file.write_text("originaL\n")
        with mock.patch.object(loop, "execute_run", side_effect=AssertionError("must not run")):
            with self.assertRaisesRegex(manifest.ManifestMismatch, "TASK.md"):
                pilot.run_builds(self.tmp / "data")

    def test_changed_harness_byte_refuses_judging(self):
        self.harness_file.write_text("original \n")
        with self.assertRaisesRegex(manifest.ManifestMismatch, "loop.py"):
            pilot.run_judging(self.tmp / "data")

    def test_added_file_refuses(self):
        extra = self.tmp / "eval" / "tasks" / "t" / "new.py"
        extra.write_text("x\n")
        with mock.patch.object(manifest, "frozen_files", lambda: [self.task_file, self.harness_file, extra]):
            with self.assertRaisesRegex(manifest.ManifestMismatch, "new.py"):
                manifest.verify()


if __name__ == "__main__":
    unittest.main()
