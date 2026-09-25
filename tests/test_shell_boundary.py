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


if __name__ == "__main__":
    unittest.main()
