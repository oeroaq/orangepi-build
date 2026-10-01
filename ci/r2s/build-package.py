#!/usr/bin/env python3
"""Build the declarative r2s-platform .deb without copying package-owned files."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "package/r2s-platform/root/usr/lib/r2s"))
import model


def main():
    stage = ROOT / "_ci/package/r2s-platform"
    if stage.exists():
        raise SystemExit("Refusing to overwrite an existing package staging tree")
    shutil.copytree(ROOT / "package/r2s-platform/root", stage)
    debian = stage / "DEBIAN"
    debian.mkdir()
    version = "1.0+git." + subprocess.check_output(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, text=True).strip()
    (debian / "control").write_text((ROOT / "package/r2s-platform/debian/control").read_text().replace("@VERSION@", version))
    shutil.copyfile(ROOT / "package/r2s-platform/debian/postinst", debian / "postinst")
    os.chmod(debian / "postinst", 0o755)
    (stage / "etc/r2s/doh.toml").write_text(model.doh_config())
    # Only configuration owned by our source package becomes a Debian conffile.
    conffiles = sorted("/" + str(path.relative_to(stage)) for path in (stage / "etc").rglob("*") if path.is_file())
    (debian / "conffiles").write_text("\n".join(conffiles) + "\n")
    for path in stage.rglob("*"):
        if path.is_file():
            if path.parent.name in ("sbin", "dispatcher.d") or path.name in ("postinst", "watch-links"):
                os.chmod(path, 0o755)
            elif path.parent.name == "sudoers.d":
                os.chmod(path, 0o440)
            else:
                os.chmod(path, 0o644)
        elif path.is_dir():
            os.chmod(path, 0o755)
    output = ROOT / f"output/debs/r2s-platform_{version}_all.deb"
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(stage), str(output)], check=True)
    print(output)


if __name__ == "__main__":
    main()
