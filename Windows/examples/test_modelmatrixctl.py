from __future__ import annotations
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import modelmatrixctl  # noqa: E402

class Handler(BaseHTTPRequestHandler):
    seen = []
    def do_GET(self):
        type(self).seen.append(("GET", self.path, dict(self.headers)))
        if self.path == '/api/dashboard/session':
            body = b'{"authenticated":true}'
            self.send_response(200); self.send_header('Set-Cookie', 'modelmatrix_session=fixture; Path=/'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if self.path.startswith('/api/status'):
            body = json.dumps({'profiles': [], 'platform': 'windows'}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        type(self).seen.append(("POST", self.path, dict(self.headers)))
        length = int(self.headers.get('Content-Length', '0'))
        if self.path == '/api/start' and self.headers.get('Origin') and self.headers.get('Cookie') == 'modelmatrix_session=fixture':
            self.rfile.read(length)
            body = b'{"started":true}'
            self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        self.send_response(403); self.end_headers()
    def log_message(self, *_): pass

class CLIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler); cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls): cls.server.shutdown(); cls.server.server_close()
    def test_dry_run_never_connects(self):
        result = modelmatrixctl.main(['--dry-run', '--json', 'start', 'demo'])
        self.assertEqual(result, 0)
    def test_status_uses_json_http(self):
        endpoint = f'http://127.0.0.1:{self.server.server_port}'
        old = sys.stdout; sys.stdout = io.StringIO()
        try: result = modelmatrixctl.main(['--endpoint', endpoint, '--json', 'status']); output = json.loads(sys.stdout.getvalue())
        finally: sys.stdout = old
        self.assertEqual(result, 0); self.assertTrue(output['ok']); self.assertEqual(output['platform'], 'windows')
    def test_unreachable_is_failure(self):
        self.assertNotEqual(modelmatrixctl.main(['--endpoint', 'http://127.0.0.1:1', '--json', 'status']), 0)
    def test_mutation_primes_loopback_session_and_origin(self):
        endpoint = f'http://127.0.0.1:{self.server.server_port}'
        old = sys.stdout; sys.stdout = io.StringIO()
        try:
            result = modelmatrixctl.main(['--endpoint', endpoint, '--json', 'start', 'demo'])
            output = json.loads(sys.stdout.getvalue())
        finally: sys.stdout = old
        self.assertEqual(result, 0)
        self.assertTrue(output['ok'])
        self.assertEqual([item[0:2] for item in Handler.seen[-2:]], [('GET', '/api/dashboard/session'), ('POST', '/api/start')])

if __name__ == '__main__': unittest.main()
