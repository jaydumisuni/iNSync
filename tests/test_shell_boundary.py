from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ShellBoundaryTests(unittest.TestCase):
    def test_renderer_contains_no_process_or_subprocess_execution(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        widget = (ROOT / "app" / "electron" / "renderer" / "widget.html").read_text(encoding="utf-8")
        forbidden = ("child_process", "spawn(", "exec(", "subprocess", "powershell.exe", "adb ")
        for token in forbidden:
            self.assertNotIn(token, renderer)
            self.assertNotIn(token, widget)

    def test_preload_exposes_job_and_dialog_contracts(self):
        preload = (ROOT / "app" / "electron" / "preload.cjs").read_text(encoding="utf-8")
        for token in (
            "onBackendEvent",
            "job.submit",
            "job.cancel",
            "insync:dialog:files",
            "insync:dialog:folder",
            "insync:clipboard:text",
            "insync:clipboard:image",
        ):
            self.assertIn(token, preload)

    def test_widget_is_separate_renderer(self):
        main = (ROOT / "app" / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn('"renderer","widget.html"', main)
        self.assertIn("alwaysOnTop:true", main)
        self.assertIn("skipTaskbar:true", main)


    def test_device_surfaces_are_separate_and_single_authority(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(renderer.count("const modules={"), 1)
        self.assertNotIn("const standaloneModules={", renderer)
        self.assertNotIn("\ufffd", renderer)
        self.assertIn('data-module="android" data-label="Android"', renderer)
        self.assertIn('data-module="iphone" data-label="iPhone"', renderer)
        self.assertIn('data-module="ipa" data-label="IPA"', renderer)
        self.assertNotIn('actionTile("apk"', renderer)
        self.assertIn('title:"Android"', renderer)
        self.assertIn('title:"IPA"', renderer)
        self.assertIn('title:"iPhone"', renderer)
        for token in (
            'const labels={photos:"Photos",music:"Music",apps:"Apps",documents:"App Documents"}',
            'data-cmd="iphoneSection"',
            'data-cmd="iphoneAppDelete"',
            'data-cmd="iphoneDocSend"',
            'data-cmd="iphoneFileSave"',
            'data-cmd="iphoneFileDelete"',
        ):
            self.assertIn(token, renderer)

    def test_apple_device_work_stays_in_sidecar(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            '"ios.apps"',
            '"ios.app.uninstall"',
            '"ios.media.list"',
            '"ios.media.pull"',
            '"ios.media.delete"',
            '"ios.documents.list"',
            '"ios.documents.pull"',
            '"ios.documents.push"',
            '"ios.documents.delete"',
        ):
            self.assertIn(token, renderer)
            self.assertIn(token, backend)
        self.assertIn("HouseArrestService", backend)
        self.assertIn("InstallationProxyService", backend)
        self.assertIn("Music delete is blocked until the library-safe Apple media adapter is qualified", backend)

    def test_builder_owns_apple_bridge_dependency(self):
        import json
        config = json.loads((ROOT / "techguy-build.json").read_text(encoding="utf-8"))
        sidecar = config["electron"]["pythonSidecars"][0]
        self.assertEqual(sidecar["requirements"], "requirements-insync-sidecar.txt")
        requirements = (ROOT / "requirements-insync-sidecar.txt").read_text(encoding="utf-8")
        self.assertIn("pymobiledevice3==11.19.1", requirements)
        self.assertIn("pymobiledevice3.services.house_arrest", sidecar["hiddenImports"])
        self.assertIn("pymobiledevice3.services.installation_proxy", sidecar["hiddenImports"])

    def test_peer_clipboard_is_owned_by_engine_and_main_process(self):
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        main = (ROOT / "app" / "electron" / "main.cjs").read_text(encoding="utf-8")
        widget = (ROOT / "app" / "electron" / "renderer" / "widget.html").read_text(encoding="utf-8")
        for token in ("PEER_DATA_PORT", "PeerTransport", '"clipboard_peer": True', '"peer.clipboard"'):
            self.assertIn(token, backend)
        for token in ("nativeImage", 'event?.type==="peer.clipboard"', "clipboard.writeText", "clipboard.writeImage"):
            self.assertIn(token, main)
        self.assertIn("x.approved!==false", widget)


if __name__ == "__main__":
    unittest.main()
