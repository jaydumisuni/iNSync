from __future__ import annotations

import importlib.util
import json
import plistlib
import zipfile
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


    def test_peer_discovery_and_role_configuration(self):
        proc = self.start_backend()
        try:
            self.read(proc)
            self.send(proc, {"id": 10, "method": "job.submit", "params": {"operation": "peer.list", "params": {}}})
            job_id = None
            final = None
            deadline = time.time() + 10
            while time.time() < deadline and final is None:
                msg = self.read(proc)
                if msg.get("id") == 10:
                    job_id = msg["result"]["job"]["id"]
                if job_id and msg.get("event") == "job.finished" and msg["data"]["job"]["id"] == job_id:
                    final = msg["data"]["job"]
            self.assertEqual(final["status"], "completed")
            peers = final["result"]["peers"]
            self.assertTrue(any(peer.get("self") for peer in peers))

            self.send(proc, {"id": 11, "method": "job.submit", "params": {"operation": "peer.configure", "params": {"role": "provider", "scope": "internet-folders", "shared_paths": []}}})
            configured = None
            job_id = None
            deadline = time.time() + 10
            while time.time() < deadline and configured is None:
                msg = self.read(proc)
                if msg.get("id") == 11:
                    job_id = msg["result"]["job"]["id"]
                if job_id and msg.get("event") == "job.finished" and msg["data"]["job"]["id"] == job_id:
                    configured = msg["data"]["job"]
            self.assertEqual(configured["status"], "completed")
            self.assertEqual(configured["result"]["peer"]["role"], "provider")
            self.assertEqual(configured["result"]["peer"]["scope"], "internet-folders")
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    def test_ipa_validation_reads_bundle_metadata(self):
        with tempfile.TemporaryDirectory(prefix="insync-ipa-test-") as td:
            ipa = Path(td) / "sample.ipa"
            info = {
                "CFBundleDisplayName": "iNSync Test",
                "CFBundleIdentifier": "com.thetechguy.insync.test",
                "CFBundleShortVersionString": "1.2.3",
                "MinimumOSVersion": "15.0",
            }
            with zipfile.ZipFile(ipa, "w") as zf:
                zf.writestr("Payload/Test.app/Info.plist", plistlib.dumps(info))
                zf.writestr("Payload/Test.app/_CodeSignature/CodeResources", b"proof")

            proc = self.start_backend()
            try:
                self.read(proc)
                self.send(proc, {"id": 20, "method": "job.submit", "params": {"operation": "ios.validate", "params": {"path": str(ipa)}}})
                job_id = None
                final = None
                deadline = time.time() + 10
                while time.time() < deadline and final is None:
                    msg = self.read(proc)
                    if msg.get("id") == 20:
                        job_id = msg["result"]["job"]["id"]
                    if job_id and msg.get("event") == "job.finished" and msg["data"]["job"]["id"] == job_id:
                        final = msg["data"]["job"]
                self.assertEqual(final["status"], "completed")
                metadata = final["result"]["metadata"]
                self.assertEqual(metadata["bundle_id"], "com.thetechguy.insync.test")
                self.assertEqual(metadata["version"], "1.2.3")
                self.assertTrue(metadata["signed"])
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()



    def test_peer_file_helpers_preserve_relative_paths_and_block_traversal(self):
        spec = importlib.util.spec_from_file_location("insync_backend_test", BACKEND)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with self.assertRaises(ValueError):
            module._safe_peer_relative("../escape.txt")
        with self.assertRaises(ValueError):
            module._safe_peer_relative("/absolute.txt")

        with tempfile.TemporaryDirectory(prefix="insync-peer-plan-") as td:
            root = Path(td)
            folder = root / "bundle"
            folder.mkdir()
            (folder / "a.txt").write_text("a", encoding="utf-8")
            nested = folder / "nested"
            nested.mkdir()
            (nested / "b.txt").write_text("bb", encoding="utf-8")
            plan, total = module._collect_peer_file_plan([folder])
            relative = sorted(item[1] for item in plan)
            self.assertEqual(relative, ["bundle/a.txt", "bundle/nested/b.txt"])
            self.assertEqual(total, 3)


if __name__ == "__main__":
    unittest.main()
