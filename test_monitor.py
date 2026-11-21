import json
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

import app


class MonitorTest(unittest.TestCase):
    def setUp(self):
        app.monitors.clear()
        self.addCleanup(app.monitors.clear)

    def test_validation(self):
        for interval in [10, 60, 86400, None]:
            app.validate_monitor({'monitorInterval': interval})
        for interval in [0, 9, 86401, True, 10.5, '60']:
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                app.validate_monitor({'monitorInterval': interval})
        for mode, target in [('http', 'https://host/health'), ('tcp', 'tcp://[::1]:443'), ('http', '')]:
            app.validate_monitor({'monitor': mode, 'monitorUrl': target})
        for mode, target in [('ping', ''), ('http', 'file:///etc/passwd'),
                             ('http', 'http://user:secret@host'), ('tcp', 'tcp://host'),
                             ('tcp', 'tcp://host:70000'), ('tcp', 'tcp://host:80/path')]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                app.validate_monitor({'monitor': mode, 'monitorUrl': target})

    def test_http_tcp_and_failures_on_local_server(self):
        class Handler(app.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.server.headers_seen = self.headers
                self.send_response(204 if self.path == '/health' else 503)
                self.end_headers()

        server = app.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            target = '127.0.0.1:%s' % server.server_port
            self.assertEqual(app.probe_service('http', 'http://' + target + '/health')['state'], 'up')
            self.assertNotIn('X-API-Key', server.headers_seen)
            self.assertEqual(app.probe_service('http', 'http://' + target + '/error')['state'], 'down')
            self.assertEqual(app.probe_service('tcp', 'tcp://' + target)['state'], 'up')
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        self.assertEqual(app.probe_service('tcp', 'tcp://' + target)['state'], 'down')
        with patch.object(app.socket, 'create_connection', side_effect=TimeoutError('timeout')):
            self.assertEqual(app.probe_service('tcp', 'tcp://' + target)['state'], 'down')

    def test_targets_and_invalid_fallback_isolation(self):
        items = {'a': {'monitor': 'tcp', 'url': 'https://[::1]'},
                 'b': {'monitor': 'tcp', 'url': 'http://bad:invalid'},
                 'c': {'monitor': 'http', 'monitorUrl': 'http://explicit/health'}, 'off': {}}
        with patch.object(app, 'load_items', return_value=items):
            self.assertEqual(app.monitor_targets(), {'a': ('tcp', 'tcp://[::1]:443'),
                                                    'b': ('tcp', ''), 'c': ('http', 'http://explicit/health')})

    def test_history_uptime_reset_and_disable(self):
        targets = {'web': ('http', 'http://web')}
        with patch.object(app, 'monitor_targets', return_value=targets), patch.object(app, 'probe_service') as probe, \
                patch.object(app.time, 'time') as clock:
            for i in range(62):
                clock.return_value = i * app.MONITOR_INTERVAL
                probe.return_value = {'state': 'up' if i % 2 else 'down', 'detail': 'test', 'ms': 1}
                app.check_monitors()
            self.assertEqual(len(app.monitors['web']['history']), 60)
            self.assertEqual(app.monitors['web']['uptime'], 50)
            targets['web'] = ('http', 'http://changed')
            app.check_monitors()
            self.assertEqual(len(app.monitors['web']['history']), 1)
            targets.clear()
            app.check_monitors()
            self.assertEqual(app.monitors, {})

    def test_independent_intervals_and_change_without_history_loss(self):
        items = {'fast': {'monitor': 'http', 'monitorUrl': 'http://fast', 'monitorInterval': 10},
                 'slow': {'monitor': 'tcp', 'monitorUrl': 'tcp://slow:80', 'monitorInterval': 60},
                 'default': {'monitor': 'http', 'monitorUrl': 'http://default'}}
        with patch.object(app, 'load_items', return_value=items), patch.object(app.time, 'time') as clock, \
                patch.object(app, 'MONITOR_INTERVAL', 30), \
                patch.object(app, 'probe_service', return_value={'state': 'up', 'detail': 'ok', 'ms': 1}) as probe:
            clock.return_value = 100
            app.check_monitors()
            self.assertEqual(probe.call_count, 3)
            clock.return_value = 109
            app.check_monitors()
            self.assertEqual(probe.call_count, 3)
            clock.return_value = 110
            app.check_monitors()
            self.assertEqual(probe.call_count, 4)
            self.assertEqual(app.monitors['slow']['checked'], 100)
            clock.return_value = 130
            app.check_monitors()
            self.assertEqual(app.monitors['default']['checked'], 130)
            items['slow']['monitorInterval'] = 10
            app.check_monitors()
            self.assertEqual(len(app.monitors['slow']['history']), 2)
            items['fast']['monitorInterval'] = 60
            clock.return_value = 140
            app.check_monitors()
            self.assertEqual(app.monitors['fast']['checked'], 130)

    def test_discard_result_if_settings_changed_during_probe(self):
        with patch.object(app, 'monitor_targets', side_effect=[{'web': ('http', 'http://old')}, {}]), \
                patch.object(app, 'probe_service', return_value={'state': 'up', 'detail': 'ok', 'ms': 1}):
            app.check_monitors()
        self.assertEqual(app.monitors, {})

    def test_settings_persist_api_exposes_results_and_disable_clears(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'CONFIG_PATH', folder + '/config.json'):
            server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = 'http://127.0.0.1:%s' % server.server_port
                for mode in ['http', 'tcp', '']:
                    data = {'monitor': mode, 'monitorUrl': ('tcp://host:80' if mode == 'tcp' else 'http://host'),
                            'monitorInterval': 15}
                    req = urllib.request.Request(url + '/api/item', json.dumps({'key': 'web', 'data': data}).encode(),
                                                 {'Content-Type': 'application/json'})
                    with urllib.request.urlopen(req):
                        pass
                    self.assertEqual(app.load_items()['web'].get('monitor', ''), mode)
                    self.assertEqual(app.load_items()['web']['monitorInterval'], 15)
                    self.assertNotIn('web', app.monitors)
                    with patch.object(app, 'probe_service', return_value={'state': 'up', 'detail': 'ok', 'ms': 1}):
                        app.check_monitors()
                    with urllib.request.urlopen(url + '/api/data') as response:
                        payload = json.load(response)
                    self.assertEqual('web' in payload['monitors'], bool(mode))
                    self.assertEqual(payload['items']['web']['monitorInterval'], 15)
                data['monitorInterval'] = 9
                req = urllib.request.Request(url + '/api/item', json.dumps({'key': 'web', 'data': data}).encode(),
                                             {'Content-Type': 'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(req)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                self.assertEqual(app.load_items()['web']['monitorInterval'], 15)
                data['monitorInterval'] = None
                req = urllib.request.Request(url + '/api/item', json.dumps({'key': 'web', 'data': data}).encode(),
                                             {'Content-Type': 'application/json'})
                with urllib.request.urlopen(req):
                    pass
                self.assertNotIn('monitorInterval', app.load_items()['web'])
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
