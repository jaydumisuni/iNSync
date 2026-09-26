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
        self.assertIn('data-module="apk" data-label="APK"', renderer)
        self.assertNotIn('actionTile("apk"', renderer)
        self.assertIn('title:"Android"', renderer)
        self.assertIn('title:"APK"', renderer)
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


    def test_backend_start_is_silent_until_peer_features_are_used(self):
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        sidecar = (ROOT / "app" / "electron" / "sidecar.cjs").read_text(encoding="utf-8")
        self.assertIn("windowsHide:true", sidecar)
        self.assertIn("creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == \"win32\" else 0)", backend)
        self.assertIn("self._thread: threading.Thread | None = None", backend)
        self.assertIn("def ensure_peer_network() -> None:", backend)
        discovery_ctor = backend.split("class PeerDiscovery:", 1)[1].split("    def start(self)", 1)[0]
        transport_ctor = backend.split("class PeerTransport:", 1)[1].split("    def start(self)", 1)[0]
        self.assertNotIn("self._thread.start()", discovery_ctor)
        self.assertNotIn("self._thread.start()", transport_ctor)

    def test_android_has_no_guarded_system_app_path(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        self.assertNotIn('"Guarded"', renderer)
        self.assertNotIn('data-cmd="appDisable"', renderer)
        self.assertIn('["shell", "pm", "uninstall", "--user", "0", package]', backend)
        self.assertIn('"method": "pm-uninstall-user-0"', backend)
        self.assertIn('{"package": package, "kind": "system", "action": "uninstall"}', backend)

    def test_device_lists_scroll_without_visible_scrollbar_and_support_views(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        for token in (
            'data-cmd="iphoneView"',
            'data-view="list"',
            'data-view="large"',
            'scrollbar-width:none',
            '.app-list::-webkit-scrollbar',
            '.media-list::-webkit-scrollbar',
            '.media-list.large',
        ):
            self.assertIn(token, renderer)
        self.assertNotIn("slice(0,6).map(app=>", renderer)
        self.assertNotIn("slice(0,7).map(item=>iphoneFileRow", renderer)

    def test_popup_controls_and_trimmed_icons_use_dedicated_assets(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="maximize-glyph"', renderer)
        self.assertIn('class="modal-window-btn close"', renderer)
        self.assertNotIn('>Max</button>', renderer)
        self.assertIn(".icon-apk", renderer)
        self.assertIn('data-module="apk" data-label="APK"', renderer)
        icon_root = ROOT / "app" / "electron" / "renderer" / "assets" / "icons"
        for name in ("sharing","playstation","xbox","switch","iphone","ipa","apk","android","files","clipboard","pc","home"):
            self.assertTrue((icon_root / f"{name}.png").is_file(), name)

    def test_widget_stays_expanded_during_interaction(self):
        widget = (ROOT / "app" / "electron" / "renderer" / "widget.html").read_text(encoding="utf-8")
        for token in (
            '#shell:hover #card,#card.expanded,#card:focus-within',
            '#card{-webkit-app-region:no-drag',
            '.top{-webkit-app-region:drag',
            'function expandWidget()',
            'card.addEventListener("pointerdown",expandWidget)',
            'card.addEventListener("focusin",expandWidget)',
            'collapseTimer=setTimeout',
        ):
            self.assertIn(token, widget)


    def test_android_content_and_apk_are_separate_surfaces_with_shared_adb_engine(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            'const labels={photos:"Photos",videos:"Videos",apps:"Apps"}',
            'data-cmd="androidSection"',
            'data-cmd="androidView"',
            'data-cmd="androidFileSave"',
            'data-cmd="androidFileDelete"',
            'title:"APK"',
            'data-cmd="apkBrowse"',
            'data-cmd="apkInstall"',
        ):
            self.assertIn(token, renderer)
        for token in (
            '"adb.media.list"',
            '"adb.media.preview"',
            '"adb.media.pull"',
            '"adb.media.delete"',
            '"adb.install"',
        ):
            self.assertIn(token, backend)
        self.assertIn("content://media/external/", backend)
        self.assertIn('exec-out", "cat"', backend)
        self.assertNotIn("packagePaths", renderer)

    def test_lumi_combobox_contract_is_used_for_android_controls(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        for token in (
            ".lumi-combobox-host",
            ".lumi-combobox-list",
            ".lumi-combobox-option",
            'placeholder="Search choices..."',
            'function lumiDevicePicker(label)',
            'function lumiAndroidTools(apkMode=false)',
            'class="lumi-chevron"',
        ):
            self.assertIn(token, renderer)
        self.assertNotIn('id="adbDevice"', renderer)

    def test_large_media_view_uses_real_preview_images(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            'class="media-thumb"',
            'data.android.previews',
            'data.iphone.previews',
            'schedulePreviewHydration()',
            '"adb.media.preview"',
            '"ios.media.preview"',
        ):
            self.assertIn(token, renderer if token != '"ios.media.preview"' else renderer)
        self.assertIn("base64.b64encode(raw)", backend)
        self.assertIn("get_file_contents(remote)", backend)

    def test_widget_uses_original_three_state_glass_controls_with_mode_dropdown(self):
        widget = (ROOT / "app" / "electron" / "renderer" / "widget.html").read_text(encoding="utf-8")
        for token in (
            'class="state-pill disconnected"',
            'class="state-pill receiving"',
            'class="state-pill sending"',
            'data-state="disconnected"',
            'data-state="receiving"',
            'data-state="sending"',
            'id="modeCombo"',
            'class="chev"',
            'data-mode="clipboard"',
        ):
            self.assertIn(token, widget)
        self.assertNotIn('<select id="mode">', widget)


    def test_peer_file_transport_and_remote_browser_are_engine_owned(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            '"peer.files.send"',
            '"peer.files.roots"',
            '"peer.files.list"',
            '"peer.files.pull"',
            "def send_file(",
            "def pull_file(",
            "peer.file.received",
            '"peer_files": True',
            '"peer_file_browse": True',
        ):
            self.assertIn(token, backend)
        for token in (
            'data-cmd="filesMode"',
            'data-mode="send"',
            'data-mode="receive"',
            'data-file-peer-id=',
            'filesRemoteOpen',
            'filesRemoteSave',
            'submit("peer.files.send"',
            'submit("peer.files.pull"',
        ):
            self.assertIn(token, renderer)
        self.assertIn("Only explicitly shared roots are exposed", renderer)
        self.assertIn("scrollbar-width:none", renderer)


    def test_apk_surface_lists_apps_and_exposes_wifi_adb_controls(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            'data-cmd="apkSection"',
            '["install","apps","wifi"]',
            'id="apkAppSearch"',
            'data-cmd="apkAppsRefresh"',
            'data-cmd="apkAppFilter"',
            'data-cmd="appUninstall"',
            'data-cmd="wifiStatus"',
            'data-cmd="wifiEnable"',
            'data-cmd="wifiConnect"',
            'data-cmd="wifiDisconnect"',
            'data-cmd="wifiUsb"',
            'data-cmd="wifiPair"',
            'id="wifiPairEndpoint"',
            'id="wifiPairCode"',
        ):
            self.assertIn(token, renderer)
        for token in (
            '"adb.wifi.status"',
            '"adb.wifi.enable"',
            '"adb.wifi.connect"',
            '"adb.wifi.disconnect"',
            '"adb.wifi.usb"',
            '"adb.wifi.pair"',
            '"adb_wifi": bool(adb)',
            '"adb_wifi_pair": bool(adb)',
        ):
            self.assertIn(token, backend)

    def test_wifi_adb_contract_matches_device_manager_evidence(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        self.assertIn('["tcpip", "5555"]', backend)
        self.assertIn('["shell", "ip", "-f", "inet", "addr", "show", "wlan0"]', backend)
        self.assertIn('[adb, "connect", endpoint]', backend)
        self.assertIn('[adb, "pair", endpoint, code]', backend)
        self.assertIn("Android 11+ Pairing", renderer)
        self.assertIn("Pair device with pairing code", renderer)


    def test_renderer_refreshes_backend_capabilities_on_start_and_module_open(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        self.assertIn('if(!engineSnapshot)refreshEngine()', renderer)
        self.assertIn('$("#backdrop").classList.contains("open")', renderer)
        self.assertIn('apply(state);await refreshEngine()', renderer)


if __name__ == "__main__":
    unittest.main()
