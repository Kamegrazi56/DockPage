import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

import app


class UpdatesTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        config = patch.object(app, "CONFIG_PATH", folder.name + "/config.json")
        config.start()
        self.addCleanup(config.stop)
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

    def test_deployed_version_fallbacks(self):
        for changes, expected in [
            ({"Labels": {"org.opencontainers.image.version": "1.26.3", "org.label-schema.version": "old"}}, "1.26.3"),
            ({"Labels": {"org.label-schema.version": "2.0"}}, "2.0"),
            ({"Labels": None, "Image": "registry:5000/team/web:stable"}, "Tag stable · sha256:local"),
            ({"Image": "registry:5000/team/web"}, "Tag latest · sha256:local"),
            ({"Image": "web@sha256:remote"}, "sha256:local"),
            ({"Image": "sha256:12345678901234567890", "ImageID": ""}, "sha256:123456789012"),
            ({"Image": "", "ImageID": ""}, "Indisponible"),
        ]:
            with self.subTest(changes=changes):
                self.assertEqual(app.deployed_version(dict(self.container, **changes)), expected)

    def test_deployment_metadata_survives_restart_and_registry_failure(self):
        endpoint = {"Id": 1, "Name": "host", "Type": 1, "Status": 1}
        self.container.update(Created=1700000000, Labels={"org.opencontainers.image.version": "1.0"})
        with patch.object(app, "load_config", return_value={}), patch.object(app, "load_items", return_value={}), \
                patch.object(app, "api", side_effect=lambda path: [endpoint] if path == "/endpoints" else [self.container]), \
                patch.object(app, "image_version", return_value={"state": "unknown"}):
            before = app.collect()[0]["containers"][0]
            self.container.update(Status="Up 1 second", State="running")
            after = app.collect()[0]["containers"][0]
            self.assertEqual(before["deployedAt"], 1700000000)
            self.assertEqual(after["deployedAt"], before["deployedAt"])
            self.assertEqual(after["deployedVersion"], "1.0")
            self.container.update(Id="new", Created=1800000000, Labels={"org.opencontainers.image.version": "2.0"})
            replaced = app.collect()[0]["containers"][0]
            self.assertEqual(replaced["deployedAt"], 1800000000)
            self.assertEqual(replaced["deployedVersion"], "2.0")
            self.container.pop("Created")
            self.assertIsNone(app.collect()[0]["containers"][0]["deployedAt"])

    def test_image_change_observation_is_persistent_and_not_deployment_time(self):
        c = {"name": "web", "imageId": "sha256:old", "deployedAt": 100}
        envs = [{"id": 1, "containers": [c]}]
        app.record_image_changes(envs)
        self.assertIsNone(c["imageChangedAt"])
        c["deployedAt"] = 200  # recréation avec la même image
        app.record_image_changes(envs)
        self.assertIsNone(c["imageChangedAt"])
        c["imageId"] = "sha256:new"
        with patch.object(app.time, "time", return_value=300):
            app.record_image_changes(envs)
        self.assertEqual(c["imageChangedAt"], 300)
        c["deployedAt"] = 400
        script = ("import app,json,sys; app.CONFIG_PATH=sys.argv[1]; "
                  "envs=json.loads(sys.argv[2]); app.record_image_changes(envs); "
                  "print(json.dumps(envs))")
        result = subprocess.check_output([sys.executable, "-c", script, app.CONFIG_PATH, json.dumps(envs)], text=True)
        self.assertEqual(json.loads(result)[0]["containers"][0]["imageChangedAt"], 300)
        c["imageId"] = None
        app.record_image_changes(envs)
        self.assertIsNone(c["imageChangedAt"])
        c["imageId"] = "sha256:new"
        app.record_image_changes(envs)
        self.assertEqual(c["imageChangedAt"], 300)
        c["imageId"] = "sha256:old"  # retour arrière également observé
        with patch.object(app.time, "time", return_value=500):
            app.record_image_changes(envs)
        self.assertEqual(c["imageChangedAt"], 500)
        envs[0]["id"] = 2
        app.record_image_changes(envs)
        self.assertIsNone(c["imageChangedAt"])

    def test_image_history_write_failure_does_not_advance_saved_observation(self):
        c = {"name": "web", "imageId": "old"}
        envs = [{"id": 1, "containers": [c]}]
        app.record_image_changes(envs)
        c["imageId"] = "new"
        with patch.object(app, "save_config", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                app.record_image_changes(envs)
        with open(app.CONFIG_PATH + ".images.json") as f:
            self.assertEqual(json.load(f)["1/web"], {"imageId": "old", "observedAt": None})
        with open(app.CONFIG_PATH + ".images.json", "w") as f:
            f.write("invalid json")
        with self.assertRaises(ValueError):
            app.record_image_changes(envs)

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
