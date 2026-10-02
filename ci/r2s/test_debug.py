"""Safety regressions for a helper that can replace the real PID 1."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "package/r2s-platform/root/usr/sbin/r2s-debug"


class DebugHelperTests(unittest.TestCase):
    def test_help_is_available_without_root_mounts_or_tracing(self):
        result = subprocess.run(["sh", str(SCRIPT), "--help"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exec r2s-debug systemd", result.stdout)
        self.assertIn("python-init", result.stdout)

    def test_systemd_rejects_a_child_process_before_preparing_any_trace(self):
        result = subprocess.run(["sh", str(SCRIPT), "systemd"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("PID 1 shell", result.stderr)
        self.assertNotIn("R2S_DEBUG: mode=", result.stderr)
        self.assertNotIn("logs=", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_unknown_scenario_is_rejected_instead_of_executing_a_command(self):
        result = subprocess.run(["sh", str(SCRIPT), "reboot"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Usage:", result.stderr)
        self.assertNotIn("R2S_DEBUG: mode=", result.stderr)


if __name__ == "__main__":
    unittest.main()
