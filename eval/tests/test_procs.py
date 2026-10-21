import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import procs


class ProcsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_term_ignoring_member_is_killed_after_leader_exits(self):
        # The leader dies on TERM; a member that ignores TERM must still die.
        pid_file = self.tmp / "grandchild.pid"
        script = f"sh -c 'trap \"\" TERM; echo $$ > {pid_file}; sleep 60' & wait"
        with mock.patch.object(procs, "GRACE_SECONDS", 1):
            result = procs.run(["sh", "-c", script], cwd=self.tmp, timeout=1, stdout_path=self.tmp / "out")
        self.assertTrue(result.timed_out)
        grandchild = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _alive(grandchild):
            time.sleep(0.1)
        self.assertFalse(_alive(grandchild))

    def test_completed_child_reports_exit_code(self):
        result = procs.run(["sh", "-c", "exit 3"], cwd=self.tmp, timeout=10, stdout_path=self.tmp / "out")
        self.assertEqual((result.exit_code, result.timed_out), (3, False))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
