from __future__ import annotations

import json
import re
import re
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
            'const labels={photos:"Photos",videos:"Videos",music:"Music",apps:"Apps",documents:"App Documents"}',
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
        preload = (ROOT / "app" / "electron" / "preload.cjs").read_text(encoding="utf-8")
        main = (ROOT / "app" / "electron" / "main.cjs").read_text(encoding="utf-8")
        for token in (
            '#card{-webkit-app-region:no-drag',
            '.top{-webkit-app-region:drag',
            'function expandWidget()',
            'window.ttg.widgetExpand(true)',
            'window.ttg.widgetExpand(false)',
            'shell.addEventListener("pointerenter"',
            'shell.addEventListener("pointerleave"',
            'collapseTimer=setTimeout',
            'id="edgeClose"',
        ):
            self.assertIn(token, widget)
        self.assertIn('widgetExpand:(expanded)=>ipcRenderer.invoke("insync:widget:expand",!!expanded)', preload)
        self.assertIn('ipcMain.handle("insync:widget:expand"', main)
        self.assertNotIn('transform:translateX(181px)', widget)

    def test_widget_edge_position_and_clipboard_settings_persist_across_restart(self):
        main = (ROOT / "app" / "electron" / "main.cjs").read_text(encoding="utf-8")
        widget = (ROOT / "app" / "electron" / "renderer" / "widget.html").read_text(encoding="utf-8")
        for token in (
            'WIDGET_HANDLE=11',
            'insync-state.json',
            'widgetDock={edge:"right",y:null,displayId:null}',
            'screen.getDisplayMatching(bounds)',
            'persistDockFromWindow()',
            'widgetWindow.on("moved"',
            'app.setLoginItemSettings({openAtLogin:true',
            '"--insync-startup"',
            'if(action==="close"){showWidget(false)',
        ):
            self.assertIn(token, main)
        for token in (
            'id="autoClipboard"',
            'clipboardAuto',
            'clipboardTargets',
            'window.ttg.stateSet({mode:"clipboard"',
        ):
            self.assertIn(token, widget)

    def test_apk_apps_have_fixed_get_and_delete_actions(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            'data-cmd="appGet"',
            '>Get</button><button class="danger mini" data-cmd="appUninstall"',
            '.app-actions{display:flex',
            'overflow-wrap:anywhere',
            '"adb.app.export"',
            'def adb_app_export_job(',
            '["shell", "pm", "path", package]',
        ):
            self.assertIn(token, renderer if token.startswith(("data-", ">", ".", "overflow")) else backend)

    def test_media_scrollbars_are_hidden_and_photo_placeholders_are_not_labels(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        for token in (
            '::-webkit-scrollbar{display:none!important',
            'scrollbar-width:none!important',
            'class="media-placeholder media-preview-needed"',
            'IntersectionObserver',
            'requestPreviewNode',
            'data-preview-platform',
        ):
            self.assertIn(token, renderer)
        self.assertNotIn('kind==="photos"?"PHOTO"', renderer)

    def test_auto_clipboard_is_main_process_owned_and_persistent(self):
        main = (ROOT / "app" / "electron" / "main.cjs").read_text(encoding="utf-8")
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        for token in (
            'clipboardAuto:false',
            'clipboardTargets:[]',
            'setInterval(()=>{clipboardTick()},450)',
            'queueClipboardOperation("clipboard.text"',
            'queueClipboardOperation("clipboard.image"',
            'clipboard.writeText(event.text)',
            'clipboard.writeImage(image)',
            'saveRuntimeState()',
        ):
            self.assertIn(token, main)
        self.assertIn('Auto clipboard', renderer)
        self.assertIn('data-cmd="clipAll"', renderer)

    def test_sharing_elevation_has_no_visible_powershell_launcher(self):
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        sharing = backend.split("def sharing_toggle", 1)[1].split("def sharing_status_job", 1)[0]
        self.assertIn("ShellExecuteExW", backend)
        self.assertIn("SW_HIDE = 0", backend)
        self.assertIn("_run_elevated_hidden(", sharing)
        self.assertNotIn("Start-Process -FilePath 'powershell.exe'", sharing)


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
            '"adb_wifi": bool(adb) and bool(modern_adb)',
            '"adb_wifi_pair": bool(modern_adb)',
        ):
            self.assertIn(token, backend)

    def test_wifi_adb_contract_matches_device_manager_evidence(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        self.assertIn('["tcpip", "5555"]', backend)
        self.assertIn('["shell", "ip", "-f", "inet", "addr", "show", "wlan0"]', backend)
        self.assertIn('_adb_modern_base() + ["connect", endpoint]', backend)
        self.assertIn('_adb_modern_base() + ["pair", endpoint, code]', backend)
        self.assertIn("Android 11+ Pairing", renderer)
        self.assertIn("Pair device with pairing code", renderer)
        self.assertIn('ADB_MODERN_SERVER_PORT = int(os.environ.get("INSYNC_ADB_MODERN_PORT", "5041"))', backend)
        self.assertIn('return _bundled_adb_path() or shutil.which("adb")', backend)


    def test_renderer_refreshes_backend_capabilities_on_start_and_module_open(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        self.assertIn('if(!engineSnapshot)refreshEngine()', renderer)
        self.assertIn('$("#backdrop").classList.contains("open")', renderer)
        self.assertIn('apply(state);await refreshEngine()', renderer)


    def test_apk_wireless_debugging_has_mdns_discovery_and_owned_adb_runtime(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            'data-cmd="wifiMdns"',
            'wifiUseService',
            'mDNS services',
            '"adb.wifi.mdns"',
            '_adb_modern_base() + ["mdns", "services"]',
            'INSYNC_ADB_PATH',
            '"android-platform-tools"',
        ):
            self.assertIn(token, renderer if token.startswith("data-cmd") or token in ("wifiUseService", "mDNS services") else backend)
        runtime = ROOT / "resources" / "android-platform-tools"
        for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "libwinpthread-1.dll"):
            self.assertTrue((runtime / name).is_file(), name)


    def test_popup_panels_scroll_independently_without_visible_scrollbars(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        for token in (
            ".modal-body>.card{overflow-y:auto!important",
            ".modal-body>.card::-webkit-scrollbar{display:none!important",
            ".modal-body.single{grid-template-columns:1fr}",
            "body.classList.toggle(\"single\",parts.length===1)",
        ):
            self.assertIn(token, renderer)
        self.assertIn(".backdrop{position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:transparent;backdrop-filter:none", renderer)
        self.assertIn(".modal-title img{width:64px;height:64px;object-fit:contain;filter:none}", renderer)
        self.assertIn(".logo{position:absolute", renderer)
        self.assertNotIn(".logo{position:absolute;left:42.2%;bottom:3.2%;width:15.6%;height:auto;z-index:8;pointer-events:none;filter:drop-shadow", renderer)

    def test_android_apps_lazy_load_real_package_icons(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            "function appIconFor(app)",
            'class="app-icon"',
            "app-icon-needed",
            'submit("adb.app.icon"',
            'data.android.appIcons',
        ):
            self.assertIn(token, renderer)
        for token in (
            "def adb_app_icon_job(",
            "def _apk_icon_data_url(",
            '["shell", "pm", "path", package]',
            '"adb.app.icon": adb_app_icon_job',
            '"adb_app_icons": bool(adb)',
        ):
            self.assertIn(token, backend)

    def test_phone_video_surfaces_use_lazy_real_frame_previews(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        config = (ROOT / "techguy-build.json").read_text(encoding="utf-8")
        for token in (
            "data-preview-kind=\"'+esc(kind)+'\"",
            'const labels={photos:"Photos",videos:"Videos",music:"Music",apps:"Apps",documents:"App Documents"}',
            'const tabs=["photos","videos","music","apps","documents"]',
            'section==="photos"||section==="videos"',
        ):
            self.assertIn(token, renderer)
        for token in (
            "def _video_frame_from_command(",
            "def _video_frame_from_file(",
            'if kind == "videos":',
            'IOS_CAMERA_ROLL_ROOT = "DCIM"',
            "IOS_VIDEO_EXTS",
            '"media_video_preview": bool(ffmpeg_path())',
        ):
            self.assertIn(token, backend)
        self.assertIn('"imageio-ffmpeg>=0.5,<1"', config)
        self.assertIn('"imageio_ffmpeg"', config)

    def test_phone_lists_have_compact_blended_search_and_iphone_camera_roll_scope(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            '.list-search{height:34px',
            'id="androidListSearch"',
            'id="iphoneListSearch"',
            'id="apkAppSearch"',
            'data.android.search[section]',
            'data.iphone.search[section]',
            'Camera Roll only / frames / send to PC / delete',
        ):
            self.assertIn(token, renderer)
        for token in (
            'IOS_CAMERA_ROLL_ROOT = "DCIM"',
            '"photos": IOS_CAMERA_ROLL_ROOT',
            '"videos": IOS_CAMERA_ROLL_ROOT',
            'source="camera-roll" if kind in {"photos", "videos"} else "media-library"',
        ):
            self.assertIn(token, backend)
    def test_widget_peer_list_scrolls_and_each_peer_can_be_removed(self):
        widget = (ROOT / "app" / "electron" / "renderer" / "widget.html").read_text(encoding="utf-8")
        for token in (
            'id="peerList"',
            ".peer-list{display:grid",
            "overflow-y:auto",
            "scrollbar-width:none",
            "data-peer-remove",
            'submit("peer.approve",{peer_id:id,approved:false})',
            'if(job.operation==="peer.approve")refreshPeers()',
        ):
            self.assertIn(token, widget)
        self.assertNotIn('id="peerText"', widget)


    def test_android_app_rows_are_non_overlapping_and_readable(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        for token in (
            ".android-content .app-list{align-content:start;grid-auto-rows:max-content",
            ".android-content .app-row{min-height:58px",
            "background:rgba(7,14,30,.86)",
            ".android-content .app-identity>div:last-child{min-width:0}",
        ):
            self.assertIn(token, renderer)

    def test_widget_peer_empty_state_scroll_and_double_click_restore(self):
        widget = (ROOT / "app" / "electron" / "renderer" / "widget.html").read_text(encoding="utf-8")
        for token in (
            ".clipboard{display:grid",
            "overflow-y:auto",
            "scrollbar-width:none",
            ".peer-empty{min-height:30px",
            "data-peer-remove",
            'card.addEventListener("dblclick"',
            "window.ttg.showMain()",
        ):
            self.assertIn(token, widget)

    def test_android_and_apk_app_rows_expose_green_get_beside_delete(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        self.assertIn('.get{border:1px solid rgba(57,255,145,.28)', renderer)
        self.assertGreaterEqual(renderer.count('class="get mini" data-cmd="appGet"'), 2)
        self.assertIn('submit("adb.app.export",{package:pkg,serial:data.android.serial,destination},currentModule==="android"?"android":"apk")', renderer)

    def test_every_rendered_main_command_has_a_handler(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        commands = set(re.findall(r'data-cmd=["\']([^"\']+)', renderer))
        handlers = set(re.findall(r'cmd===?["\']([^"\']+)', renderer))
        self.assertTrue(commands)
        self.assertEqual(sorted(commands - handlers), [])

    def test_every_static_ui_backend_operation_is_registered(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        operations = set(re.findall(r'submit\(["\']([^"\']+)', renderer))
        registry_block = backend.split("OPERATIONS:", 1)[1].split("def snapshot()", 1)[0]
        registered = set(re.findall(r'["\']([^"\']+)["\']\s*:', registry_block))
        self.assertTrue(operations)
        self.assertEqual(sorted(operations - registered), [])

    def test_iphone_documents_support_folder_navigation_destination_and_creation(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            'docPath:"/Documents"',
            'data-cmd="iphoneDocOpen"',
            'data-cmd="iphoneDocUp"',
            'data-cmd="iphoneDocNewFolder"',
            'Send files here',
            'destination_path:data.iphone.docPath',
            'path:data.iphone.docPath',
        ):
            self.assertIn(token, renderer)
        for token in (
            'for name in await docs.listdir(current):',
            '"is_dir": is_dir',
            '"entry_type": "folder" if is_dir else "file"',
            'destination = _ios_document_path(destination_path)',
            'async def _ios_documents_mkdir_pmd(',
            '"ios.documents.mkdir": ios_documents_mkdir_job',
        ):
            self.assertIn(token, backend)

    def test_iphone_documents_send_supports_multiple_files_and_refreshes_selected_app(self):
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            'title:"Send files to "+data.iphone.docPath,multi:true',
            'local_paths:r.paths',
            'destination_path:data.iphone.docPath',
            'job.operation==="ios.documents.push"&&r.bundle_id',
            'submit("ios.documents.list",{bundle_id:r.bundle_id,path:data.iphone.docPath},"iphone",true)',
        ):
            self.assertIn(token, renderer)
        for token in (
            'async def _ios_documents_push_pmd(',
            'destination_path: str = IOS_DOCUMENTS_ROOT',
            'for local_path in local_paths:',
            'progress_bar=False',
            'raw_paths = job.params.get("local_paths")',
            '"items": uploaded',
        ):
            self.assertIn(token, backend)

    def test_iphone_documents_are_scoped_to_house_arrest_documents_root(self):
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            'IOS_DOCUMENTS_ROOT = "/Documents"',
            'for name in await docs.listdir(current):',
            'destination = _ios_document_path(destination_path)',
            'raise ValueError("App Documents path is outside /Documents")',
        ):
            self.assertIn(token, backend)

    def test_classic_adb_prefers_bundled_v41_client_over_path_fallback(self):
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        block = backend.split("def adb_path()", 1)[1].split("def ffmpeg_path()", 1)[0]
        self.assertIn('return _bundled_adb_path() or shutil.which("adb")', block)
        self.assertNotIn('return shutil.which("adb") or adb_modern_path()', block)
        self.assertIn("ecosystem-owned server on 5037", block)

    def test_apple_bridge_operations_have_bounded_usb_and_job_timeouts(self):
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            "IOS_USB_SCAN_TIMEOUT = 8.0",
            "IOS_PAIR_TIMEOUT = 20.0",
            "asyncio.wait_for(coro, timeout=timeout)",
            "async def _pmd_usb_ids()",
            "Apple USB scan timed out",
            'raise ConnectionError("No iPhone connected")',
            'return _run_async(_ios_status_pmd(serial), timeout=15)',
            'calculate_sizes=False',
            'timeout=30)',
            'timeout=60)',
            'timeout=600)',
        ):
            self.assertIn(token, backend)

    def test_backend_jsonl_is_ascii_safe_on_windows_codepages(self):
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        emit = backend.split("def emit(", 1)[1].split("def reply(", 1)[0]
        reply = backend.split("def reply(", 1)[1].split("def ok(", 1)[0]
        self.assertIn("ensure_ascii=True", emit)
        self.assertIn("ensure_ascii=True", reply)
        self.assertNotIn("ensure_ascii=False", emit)
        self.assertNotIn("ensure_ascii=False", reply)

    def test_backend_state_changes_are_owned_by_main_process_and_broadcast_to_both_windows(self):
        main = (ROOT / "app" / "electron" / "main.cjs").read_text(encoding="utf-8")
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        for token in (
            "function applyBackendStateEvent(event)",
            'job.operation==="sharing.status"||job.operation==="sharing.toggle"',
            'job.operation==="peer.configure"',
            'job.operation==="peer.approve"',
            "saveRuntimeState();",
            "broadcastState();",
        ):
            self.assertIn(token, main)
        self.assertIn('window.ttg.onState(next=>{apply(next);if($("#backdrop").classList.contains("open"))renderModule()})', renderer)
        self.assertIn('window.ttg.stateSet({connection:x.connection})', renderer)

    def test_windows_build_uses_insync_specific_icon(self):
        config = json.loads((ROOT / "techguy-build.json").read_text(encoding="utf-8"))
        icon_rel = config.get("icons", {}).get("windows", "")
        self.assertEqual(icon_rel, "app/electron/renderer/assets/insync-app-icon.ico")
        icon = ROOT / icon_rel
        self.assertTrue(icon.is_file())
        self.assertEqual(icon.suffix.lower(), ".ico")

    def test_main_restore_reveals_immediately_and_reasserts_after_renderer_reload(self):
        main = (ROOT / "app" / "electron" / "main.cjs").read_text(encoding="utf-8")
        block = main.split("function showMain(){", 1)[1].split("function createTray(){", 1)[0]
        self.assertIn("revealMainWindow(w);", block)
        self.assertIn('w.webContents.once("did-finish-load"', block)
        self.assertIn("setTimeout(()=>{if(!w.isDestroyed())revealMainWindow(w)},350)", block)
        self.assertNotIn('w.once("ready-to-show"', block)

    def test_sharing_selected_uplink_metric_is_authoritative_and_restored(self):
        backend = (ROOT / "backend" / "insync_backend.py").read_text(encoding="utf-8")
        for token in (
            'sharing-uplink-baseline.json',
            'Set-NetIPInterface -InterfaceAlias $publicName -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 5',
            'function Restore-UplinkMetric($path)',
            'automaticMetric=[string]$pubIf.AutomaticMetric',
            'interfaceMetric=[int]$pubIf.InterfaceMetric',
            'Restore-UplinkMetric $uplinkBaselinePath',
        ):
            self.assertIn(token, backend)

    def test_pc_bound_transfers_restore_main_window_before_destination_dialog(self):
        main = (ROOT / "app" / "electron" / "main.cjs").read_text(encoding="utf-8")
        renderer = (ROOT / "app" / "electron" / "renderer" / "index.html").read_text(encoding="utf-8")
        for token in (
            "async function visibleDialogOwner()",
            "showMain();",
            "if(!w||w.isDestroyed()||!w.isVisible())return undefined",
            "dialog.showOpenDialog(owner",
        ):
            self.assertIn(token, main)
        for token in (
            "async function choosePcFolder(title)",
            "await window.ttg.showMain()",
            'toast("Save to: "+dest.path)',
            'choosePcFolder("Save Android content to PC")',
            'choosePcFolder("Get "+pkg+" to PC")',
            'choosePcFolder("Save iPhone file to PC")',
        ):
            self.assertIn(token, renderer)

    def test_backend_timeout_and_renderer_recovery_prevent_indefinite_freeze(self):
        sidecar = (ROOT / "app" / "electron" / "sidecar.cjs").read_text(encoding="utf-8")
        main = (ROOT / "app" / "electron" / "main.cjs").read_text(encoding="utf-8")
        for token in (
            "this.invokeTimeoutMs=Math.max(3000,Number(this.manifest.invokeTimeoutMs||15000))",
            'new Error("backend timeout: "+method)',
            "this.pending.delete(id)",
            "try{child.kill()}catch{}",
            'new Error("backend stopped")',
        ):
            self.assertIn(token, sidecar)
        for token in (
            'win.on("unresponsive"',
            'win.on("responsive"',
            'win.webContents.on("render-process-gone"',
            "win.webContents.reloadIgnoringCache()",
        ):
            self.assertIn(token, main)
    def test_system_tray_uses_insync_icon_and_restores_main_window(self):
        main = (ROOT / "app" / "electron" / "main.cjs").read_text(encoding="utf-8")
        for token in (
            "nativeImage,Tray,Menu",
            "let tray=null",
            "function revealMainWindow(w)",
            "w.setSkipTaskbar(false)",
            "function createTray()",
            '"insync-logo-transparent.png"',
            'tray.setToolTip("iNSync")',
            'tray.on("double-click",()=>showMain())',
            '{label:"Open iNSync",click:()=>showMain()}',
            "createTray();",
        ):
            self.assertIn(token, main)


if __name__ == "__main__":
    unittest.main()
