#!/usr/bin/env python3
"""Apply/verify the R2S kernel contract, never silently accept missing symbols."""
import argparse
from pathlib import Path
import subprocess


CONTRACT = Path(__file__).with_name("kernel.required")


def requirements():
    for line in CONTRACT.read_text().splitlines():
        if line and not line.startswith("#"):
            symbol, modes = line.split()
            yield symbol, modes


def verify(path):
    lines = Path(path).read_text().splitlines()
    config = dict(line.split("=", 1) for line in lines if line.startswith("CONFIG_") and "=" in line)
    config.update({line[2:-11]: "n" for line in lines if line.startswith("# CONFIG_") and line.endswith(" is not set")})
    missing = [f"{symbol}: expected {modes}, got {config.get(symbol, 'unset')}"
               for symbol, modes in requirements() if config.get(symbol, "missing") not in modes]
    if missing:
        raise ValueError("Kernel contract failed:\n" + "\n".join(missing))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["apply", "verify"])
    parser.add_argument("config")
    args = parser.parse_args()
    path = Path(args.config).resolve()
    if args.command == "apply":
        for symbol, modes in requirements():
            # Prefer built-in for either-mode boot/storage prerequisites.
            switch = "--disable" if modes == "n" else "--module" if modes == "m" else "--enable"
            subprocess.run([str(path.parent / "scripts/config"), "--file", str(path),
                            switch, symbol.removeprefix("CONFIG_")], check=True)
    else:
        verify(path)


if __name__ == "__main__":
    main()
