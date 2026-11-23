#!/usr/bin/env python3
"""Sur l'hôte Ubuntu : actualise APT et écrit un relevé lisible par DockPage."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time


PACKAGE_LINE = re.compile(
    r"^(?P<name>[^\s/]+)/(?P<source>\S+)\s+"
    r"(?P<candidate>\S+)\s+(?P<architecture>\S+)\s+"
    r"\[upgradable from: (?P<installed>[^\]]+)\]$"
)


def check_updates():
    env = dict(os.environ, LC_ALL="C")
    subprocess.run(["apt-get", "-o", "APT::Update::Error-Mode=any", "update"],
                   env=env, capture_output=True, text=True, timeout=300, check=True)
    result = subprocess.run(["apt", "list", "--upgradable"], env=env,
                            capture_output=True, text=True, timeout=60, check=True)
    lines = [line.strip() for line in result.stdout.splitlines()
             if line.strip() and not line.startswith("Listing...")]
    packages = []
    for line in lines:
        match = PACKAGE_LINE.fullmatch(line)
        if not match:
            raise ValueError("Sortie APT inattendue")
        packages.append(match.groupdict())
    return sorted(packages, key=lambda package: package["name"].casefold())


def write_report(path, report):
    folder = os.path.dirname(os.path.abspath(path))
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                         prefix=".ubuntu-updates-", delete=False) as f:
            name = f.name
            json.dump(report, f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def main():
    if len(sys.argv) != 2:
        print("Usage: ubuntu_status.py /chemin/vers/data/ubuntu-updates.json", file=sys.stderr)
        return 2
    try:
        packages = check_updates()
        report = {"checkedAt": int(time.time()), "count": len(packages), "packages": packages}
        code = 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("Vérification APT impossible : %s" % error, file=sys.stderr)
        report = {"checkedAt": int(time.time()), "error": "Vérification APT impossible"}
        code = 1
    try:
        write_report(sys.argv[1], report)
    except OSError as error:
        print("Écriture du relevé impossible : %s" % error, file=sys.stderr)
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
