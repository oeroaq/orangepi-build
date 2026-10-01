"""Verify guest commands do not inherit unusable host HOME/TMPDIR paths."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ChrootEnvironmentTests(unittest.TestCase):
    def test_guest_environment_is_overridden_in_inherited_bash_function(self):
        parent = ROOT / "_ci/tests"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            folder = Path(temporary)
            fake = folder / "chroot"
            fake.write_text('#!/usr/bin/python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            # Use the interpreter executing these tests on both macOS/Linux.
            import sys
            fake.write_text(fake.read_text().replace('/usr/bin/python3', sys.executable))
            fake.chmod(0o755)
            env = {**os.environ, "PATH": str(folder) + os.pathsep + os.environ["PATH"],
                   "TMPDIR": str(folder / "host-tmp"), "HOME": str(folder / "host-home")}
            output = subprocess.check_output(['bash', '-c',
                'source ci/r2s/chroot-env.sh; bash -c \'chroot "$1" /bin/sh -c "maintainer script"\' _ "$1"',
                '_', str(folder / 'guest')], cwd=ROOT, env=env, text=True)
            self.assertEqual(json.loads(output), [str(folder / 'guest'), '/usr/bin/env',
                'TMPDIR=/tmp', 'HOME=/root', '/bin/sh', '-c', 'maintainer script'])


if __name__ == '__main__':
    unittest.main()
