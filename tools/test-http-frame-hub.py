#!/usr/bin/env python3
import importlib.util
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "server", "agilex-remote-server.py")
spec = importlib.util.spec_from_file_location("srv_http_hub", SERVER)
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

J0 = b"\xff\xd8first-frame\xff\xd9"
J1 = b"\xff\xd8second-frame\xff\xd9"


class _Handler(BaseHTTPRequestHandler):
    requests = 0

    def do_GET(self):
        type(self).requests += 1
        body = J0 if type(self).requests == 1 else J1
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class HttpFrameHubTests(unittest.TestCase):
    def setUp(self):
        _Handler.requests = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/brio.jpg"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_shared_http_frames_follow_acquire_wait_release(self):
        hub = srv.HttpFrameHub(self.url, poll_hz=60, timeout=0.3)
        self.assertTrue(hub.acquire())
        seq, frame = hub.wait(None, timeout=2)
        self.assertEqual(frame, J0)
        self.assertEqual(seq, 1)
        seq2, frame2 = hub.wait(seq, timeout=2)
        self.assertEqual(frame2, J1)
        self.assertGreater(seq2, seq)
        self.assertEqual(hub.stats()["dev"], self.url)
        self.assertEqual(hub.stats()["consumers"], 1)
        hub.release()
        deadline = time.monotonic() + 2
        while hub.snapshot() is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(hub.snapshot())
        self.assertEqual(hub.stats()["consumers"], 0)

    def test_ui_exposes_global_brio_as_cam4(self):
        with open(os.path.join(HERE, "..", "server", "remote-ui.html"), encoding="utf-8") as f:
            ui = f.read()
        self.assertIn("GLOBAL / LOGITECH BRIO", ui)
        self.assertIn("{ cam: 4, img: 'camImg4', ns: 'camNs4' }", ui)
        self.assertIn("/video?cam=4", ui)

    def test_recorder_schema_includes_global_sequence(self):
        with open(SERVER, encoding="utf-8") as f:
            source = f.read()
        self.assertIn('f"cam{cam}_seq" for cam in self.hubs', source)
        self.assertIn('"cam4": "global Logitech BRIO"', source)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(HttpFrameHubTests)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if result.wasSuccessful():
        print("PASS shared HTTP frame acquire/wait/release")
        print("PASS UI exposes global BRIO as cam4")
        print("PASS recorder schema includes global sequence")
    raise SystemExit(0 if result.wasSuccessful() else 1)
