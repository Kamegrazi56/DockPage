import json
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

import app


class UpdatesTest(unittest.TestCase):
    def setUp(self):
        app.versions.clear()
        app.update_attempts.clear()
        self.container = {"Id": "abc", "ImageID": "sha256:local", "Image": "nginx:stable",
                          "State": "running", "Labels": {}, "Names": ["/web"]}
        self.local = {"RepoDigests": ["nginx@sha256:old"]}
        self.remote = {"Descriptor": {"digest": "sha256:new"}}

    def test_running_image_digest_and_cache(self):
        with patch.object(app, "api", side_effect=[self.local, self.remote]) as api:
            self.assertEqual(app.image_version(1, self.container)["state"], "available")
            self.assertEqual(app.image_version(1, self.container)["state"], "available")
            self.assertEqual(api.call_count, 2)
            self.assertIn("sha256%3Alocal", api.call_args_list[0].args[0])
            self.assertIn("nginx%3Astable", api.call_args_list[1].args[0])
        app.versions.clear()
        self.remote["Descriptor"]["digest"] = "sha256:old"
        with patch.object(app, "api", side_effect=[self.local, self.remote]):
            self.assertEqual(app.image_version(1, self.container)["state"], "current")

    def test_unknown_and_pinned_never_claim_current(self):
        for result in [OSError("Registry unavailable"), {"RepoDigests": []}]:
            app.versions.clear()
            with patch.object(app, "api", side_effect=[result]):
                self.assertEqual(app.image_version(1, self.container)["state"], "unknown")
        self.container["Image"] = "nginx@sha256:fixed"
        with patch.object(app, "api") as api:
            self.assertEqual(app.image_version(1, self.container)["state"], "pinned")
            api.assert_not_called()

    def test_expired_cache_and_replaced_image_are_rechecked(self):
        with patch.object(app, "time") as clock, patch.object(app, "api", side_effect=[self.local, self.remote] * 3) as api:
            clock.time.return_value = 100
            clock.strftime.return_value = "20/11 12:00"
            app.image_version(1, self.container)
            clock.time.return_value = 100 + app.UPDATE_INTERVAL
            app.image_version(1, self.container)
            self.container["ImageID"] = "sha256:replacement"
            app.image_version(1, self.container)
            self.assertEqual(api.call_count, 6)

    def test_opt_in_and_safe_states(self):
        with patch.object(app, "load_items", return_value={}), patch.object(app, "api") as api:
            app.maybe_update(1, "host/web", self.container, {"state": "available"})
            api.assert_not_called()
        with patch.object(app, "load_items", return_value={"host/web": {"autoUpdate": True}}), patch.object(app, "api") as api:
            for state in ["current", "unknown", "pinned"]:
                app.maybe_update(1, "host/web", self.container, {"state": state})
            for changes in [{"State": "exited"}, {"Labels": {"com.docker.swarm.service.id": "svc"}},
                            {"Image": "portainer/agent:latest"}, {"Image": "me/dockpage:latest"}]:
                c = dict(self.container, **changes)
                version = {"state": "available"}
                app.maybe_update(1, "host/web", c, version)
                self.assertTrue(version["autoBlocked"])
            api.assert_not_called()

    def test_recreate_and_no_duplicate(self):
        inspect = {"State": {"Running": True}, "HostConfig": {"NetworkMode": "bridge"}}
        with patch.object(app, "load_items", return_value={"host/web": {"autoUpdate": True}}), patch.object(app, "api", side_effect=[inspect, {}]) as api:
            version = {"state": "available"}
            app.maybe_update(1, "host/web", self.container, version)
            self.assertEqual(version["state"], "updated")
            self.assertEqual(api.call_args.args, ("/docker/1/containers/abc/recreate", {"PullImage": True}))
            app.maybe_update(1, "host/web", self.container, {"state": "available"})
            self.assertEqual(api.call_count, 2)

    def test_failure_and_shared_network(self):
        inspect = {"State": {"Running": True}, "HostConfig": {"NetworkMode": "container:vpn"}}
        for responses in [[inspect], [OSError("timeout")]]:
            app.update_attempts.clear()
            with patch.object(app, "load_items", return_value={"host/web": {"autoUpdate": True}}), patch.object(app, "api", side_effect=responses) as api:
                version = {"state": "available"}
                app.maybe_update(1, "host/web", self.container, version)
                self.assertEqual(version["state"], "error")
                again = {"state": "available"}
                app.maybe_update(1, "host/web", self.container, again)
                self.assertEqual(again["state"], "error")
                self.assertEqual(api.call_count, 1)

    def test_collect_survives_registry_failure(self):
        endpoint = {"Id": 1, "Name": "host", "Type": 1, "Status": 1}
        with patch.object(app, "load_config", return_value={}), patch.object(app, "api", side_effect=[[endpoint], [self.container], OSError("offline")]):
            env = app.collect()[0]
            self.assertNotIn("error", env)
            self.assertEqual(env["containers"][0]["version"]["state"], "unknown")

    def test_http_setting_persists_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "CONFIG_PATH", folder + "/config.json"):
            server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = "http://127.0.0.1:%s" % server.server_port
                for enabled in [True, False]:
                    data = json.dumps({"key": "host/web", "data": {"name": "Web", "autoUpdate": enabled}}).encode()
                    req = urllib.request.Request(url + "/api/item", data=data, headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req) as response:
                        self.assertEqual(response.status, 200)
                    with urllib.request.urlopen(url + "/api/data") as response:
                        item = json.load(response)["items"]["host/web"]
                    self.assertEqual(item.get("autoUpdate", False), enabled)
                    self.assertEqual(item["name"], "Web")
                for content_type, value, status in [("text/plain", True, 415), ("application/json", "true", 400)]:
                    data = json.dumps({"key": "host/web", "data": {"autoUpdate": value}}).encode()
                    req = urllib.request.Request(url + "/api/item", data=data, headers={"Content-Type": content_type})
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(req)
                    self.assertEqual(error.exception.code, status)
                    error.exception.close()
                    self.assertNotIn("autoUpdate", app.load_items()["host/web"])
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
