import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

import app
import ubuntu_status


class UbuntuReportTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, "ubuntu-updates.json")
        path_patch = patch.object(app, "UBUNTU_STATUS_PATH", self.path)
        path_patch.start()
        self.addCleanup(path_patch.stop)

    def write(self, report):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(report, f)

    def test_missing_and_invalid_reports_never_claim_current(self):
        self.assertEqual(app.ubuntu_status()["state"], "unknown")
        self.write({"checkedAt": time.time(), "count": True})
        self.assertEqual(app.ubuntu_status()["state"], "unknown")
        self.write({"checkedAt": time.time(), "count": -1})
        self.assertEqual(app.ubuntu_status()["state"], "unknown")
        self.write({"checkedAt": time.time() + 600, "count": 0})
        self.assertEqual(app.ubuntu_status()["state"], "unknown")
        self.write({"checkedAt": time.time(), "count": 2, "packages": []})
        self.assertEqual(app.ubuntu_status()["state"], "unknown")

    def test_fresh_current_and_available(self):
        self.write({"checkedAt": time.time(), "count": 0, "packages": []})
        self.assertEqual(app.ubuntu_status()["state"], "current")
        packages = [{"name": "openssl", "installed": "2.9", "candidate": "3.0",
                     "source": "jammy-security", "architecture": "amd64"}]
        self.write({"checkedAt": time.time(), "count": 1, "packages": packages})
        self.assertEqual(app.ubuntu_status()["count"], 1)
        self.assertEqual(app.ubuntu_status()["state"], "available")
        self.assertEqual(app.ubuntu_status()["packages"], packages)
        self.write({"checkedAt": time.time(), "count": 90})
        self.assertEqual(app.ubuntu_status()["state"], "available")
        self.assertIsNone(app.ubuntu_status()["packages"])

    def test_stale_and_failed_check_are_unknown(self):
        self.write({"checkedAt": time.time() - app.UBUNTU_STATUS_MAX_AGE - 1, "count": 0})
        self.assertEqual(app.ubuntu_status()["state"], "unknown")
        self.write({"checkedAt": time.time(), "error": "APT failed"})
        self.assertEqual(app.ubuntu_status()["state"], "unknown")

    def test_api_exposes_host_status_even_without_portainer(self):
        packages = [{"name": "openssl", "installed": "2.9", "candidate": "3.0",
                     "source": "jammy-security", "architecture": "amd64"}]
        self.write({"checkedAt": time.time(), "count": 1, "packages": packages})
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with patch.object(app, "CONFIG_PATH", os.path.join(os.path.dirname(self.path), "config.json")):
            with urllib.request.urlopen("http://127.0.0.1:%d/api/data" % server.server_port) as response:
                data = json.load(response)
        self.assertEqual(data["ubuntu"]["state"], "available")
        self.assertEqual(data["ubuntu"]["count"], 1)
        self.assertEqual(data["ubuntu"]["packages"], packages)


class HostCheckTest(unittest.TestCase):
    def test_lists_upgradable_packages_after_refresh(self):
        output = "Listing... Done\nopenssl/jammy-updates 3.0 amd64 [upgradable from: 2.9]\nlinux/jammy-security 6.0 amd64 [upgradable from: 5.9]\n"
        with patch.object(ubuntu_status.subprocess, "run", side_effect=[SimpleNamespace(), SimpleNamespace(stdout=output)]) as run:
            packages = ubuntu_status.check_updates()
            self.assertEqual([p["name"] for p in packages], ["linux", "openssl"])
            self.assertEqual(packages[1]["installed"], "2.9")
            self.assertEqual(packages[1]["candidate"], "3.0")
            self.assertEqual(packages[1]["source"], "jammy-updates")
            self.assertEqual(run.call_args_list[0].args[0][0], "apt-get")
            self.assertEqual(run.call_args_list[1].args[0], ["apt", "list", "--upgradable"])

    def test_unexpected_apt_output_fails_instead_of_reporting_current(self):
        with patch.object(ubuntu_status.subprocess, "run", side_effect=[SimpleNamespace(), SimpleNamespace(stdout="unexpected\n")]):
            with self.assertRaises(ValueError):
                ubuntu_status.check_updates()

    def test_successful_check_writes_package_details(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "ubuntu-updates.json")
            packages = [{"name": "openssl", "installed": "2.9", "candidate": "3.0",
                         "source": "jammy-security", "architecture": "amd64"}]
            with patch.object(ubuntu_status, "check_updates", return_value=packages), \
                    patch.object(ubuntu_status.sys, "argv", ["ubuntu_status.py", path]):
                self.assertEqual(ubuntu_status.main(), 0)
            with open(path, encoding="utf-8") as f:
                report = json.load(f)
            self.assertEqual(report["count"], 1)
            self.assertEqual(report["packages"], packages)

    def test_failed_refresh_publishes_unknown_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "ubuntu-updates.json")
            with patch.object(ubuntu_status, "check_updates", side_effect=subprocess.CalledProcessError(1, "apt-get")), \
                    patch.object(ubuntu_status.sys, "argv", ["ubuntu_status.py", path]):
                self.assertEqual(ubuntu_status.main(), 1)
            with open(path, encoding="utf-8") as f:
                report = json.load(f)
            self.assertIn("error", report)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
