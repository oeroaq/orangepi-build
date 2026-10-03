"""Exercise DVFS ordering and safe failure handling against a sysfs fixture."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "package/r2s-platform/root/usr/lib/r2s/cpu-dvfs.sh"


class CpuTests(unittest.TestCase):
    def exercise(self, mode="normal", driver="cpufreq-dt"):
        parent = ROOT / "_ci/tests"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            policy = Path(directory)
            self.assertTrue(policy.resolve().is_relative_to(parent.resolve()))
            values = {"scaling_driver": driver, "scaling_available_frequencies": "614400 1000000 1600000",
                      "scaling_min_freq": "614400", "scaling_max_freq": "1600000",
                      "scaling_cur_freq": "1600000", "scaling_governor": "performance"}
            for name, value in values.items():
                (policy / name).write_text(value + "\n")
            # Only the backend write is simulated. The installed policy's
            # guards, reads, transitions and rollback execute unchanged.
            program = r'''
. "$1"
mode=$3
r2s_cpu_write() {
    printf '%s\n' "$3" >> "$1/transitions"
    if [ "$3" = performance ] && [ "$mode" = fail-high ]; then return 1; fi
    printf '%s\n' "$3" > "$1/$2"
    case "$3" in
        powersave) printf '614400\n' > "$1/scaling_cur_freq" ;;
        performance)
            if [ "$mode" = wrong-rate ]; then
                printf '1228800\n' > "$1/scaling_cur_freq"
            else
                printf '1600000\n' > "$1/scaling_cur_freq"
            fi ;;
    esac
}
r2s_cpu_policy "$2"
'''
            result = subprocess.run(["sh", "-c", program, "cpu-test", str(LIBRARY), str(policy), mode],
                                    text=True, capture_output=True)
            events = (policy / "transitions").read_text().splitlines() if (policy / "transitions").exists() else []
            current = (policy / "scaling_cur_freq").read_text().strip()
            return result, events, current

    def test_inherited_nominal_clock_still_gets_a_low_high_transition(self):
        result, events, current = self.exercise()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, ["powersave", "performance"])
        self.assertEqual(current, "1600000")
        self.assertIn("nominal OPP=1600000 kHz verified", result.stdout)

    def test_failed_or_incorrect_nominal_transition_returns_to_low_opp(self):
        for mode in ("fail-high", "wrong-rate"):
            with self.subTest(mode=mode):
                result, events, current = self.exercise(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(events, ["powersave", "performance", "powersave"])
                self.assertEqual(current, "614400")

    def test_unexpected_driver_is_rejected_without_writes(self):
        result, events, _ = self.exercise(driver="unexpected")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
