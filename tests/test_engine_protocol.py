from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend" / "insync_backend.py"


class EngineProtocolTests(unittest.TestCase):
    def start_backend(self):
        return subprocess.Popen(
            [sys.executable, str(BACKEND)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env={**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )

    @staticmethod
    def send(proc, payload):
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    @staticmethod
    def read(proc):
        assert proc.stdout is not None
        return json.loads(proc.stdout.readline())

    def test_snapshot_and_async_file_copy(self):
        with tempfile.TemporaryDirectory(prefix="insync-test-") as td:
            root = Path(td)
            src = root / "source.txt"
            dst = root / "dest"
            src.write_bytes(b"iNSync engine proof\n" * 1024)

            proc = self.start_backend()
            try:
                ready = self.read(proc)
                self.assertEqual(ready["event"], "engine.ready")

                self.send(proc, {"id": 1, "method": "app.snapshot", "params": {}})
                while True:
                    msg = self.read(proc)
                    if msg.get("id") == 1:
                        snapshot = msg["result"]
                        break
                self.assertTrue(snapshot["ok"])
                self.assertGreaterEqual(snapshot["engine"]["workers"], 1)

                started = time.perf_counter()
                self.send(proc, {
                    "id": 2,
                    "method": "job.submit",
                    "params": {
                        "operation": "files.copy",
                        "params": {"sources": [str(src)], "destination": str(dst)},
                    },
                })
                job_id = None
                while job_id is None:
                    msg = self.read(proc)
                    if msg.get("id") == 2:
                        job_id = msg["result"]["job"]["id"]
                self.assertLess(time.perf_counter() - started, 0.5)

                final = None
                deadline = time.time() + 10
                while time.time() < deadline and final is None:
                    msg = self.read(proc)
                    if msg.get("event") == "job.finished" and msg["data"]["job"]["id"] == job_id:
                        final = msg["data"]["job"]
                self.assertIsNotNone(final)
                self.assertEqual(final["status"], "completed")
                self.assertEqual((dst / src.name).read_bytes(), src.read_bytes())
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()

    def test_cancel_request_reaches_job(self):
        with tempfile.TemporaryDirectory(prefix="insync-cancel-test-") as td:
            root = Path(td)
            src = root / "large.bin"
            dst = root / "dest"
            with src.open("wb") as handle:
                handle.seek(64 * 1024 * 1024 - 1)
                handle.write(b"\0")

            proc = self.start_backend()
            try:
                self.read(proc)
                self.send(proc, {
                    "id": 3,
                    "method": "job.submit",
                    "params": {
                        "operation": "files.copy",
                        "params": {"sources": [str(src)], "destination": str(dst)},
                    },
                })
                job_id = None
                while job_id is None:
                    msg = self.read(proc)
                    if msg.get("id") == 3:
                        job_id = msg["result"]["job"]["id"]

                self.send(proc, {"id": 4, "method": "job.cancel", "params": {"job_id": job_id}})
                ack = None
                final = None
                deadline = time.time() + 10
                while time.time() < deadline and (ack is None or final is None):
                    msg = self.read(proc)
                    if msg.get("id") == 4:
                        ack = msg["result"]
                    if msg.get("event") == "job.finished" and msg["data"]["job"]["id"] == job_id:
                        final = msg["data"]["job"]

                self.assertTrue(ack["ok"])
                self.assertIsNotNone(final)
                self.assertEqual(final["status"], "cancelled")
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()


if __name__ == "__main__":
    unittest.main()
