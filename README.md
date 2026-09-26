# iNSync

THETECHGUY DIGITAL SOLUTIONS local transport and device bridge.

## Product shape

iNSync uses one transport core with capability-gated adapters for:

- Internet sharing / normal-mode rollback
- PC peer discovery, approval, provider/receiver roles and selective sharing
- Streamed iNSync peer file send, remote shared-root browse and receive
- Local, mapped-drive and UNC file transfer
- Approved-peer text/image clipboard sync, including automatic copy-here/paste-there mode, including automatic copy-here/paste-there mode
- Android phone content through ADB: Photos, Videos and Apps, including real video-frame previews and installed-app artwork
- A separate APK surface for APK/APKS/XAPK/split installs, installed-app listing/delete and Get-to-PC export
- IPA validation/install as its own Apple package surface
- iPhone Photos, Music export, installed apps and app Documents
- Console package/file surfaces behind qualified console-specific adapters

The full application and minimized connection/clipboard widget share one main-process state contract. Transport work never runs in the Electron renderer.

## UI contract

The supplied ghost/glass artwork is the active visual authority.

- APK is a dedicated install tile.
- Android is a separate content surface with Photos, Videos and Apps.
- Android uses List/Large views, hidden scrollbars and real photo previews when the device can provide them.
- Android device/tool selection uses the Lumi-style searchable combobox.
- IPA is separate from iPhone.
- iPhone exposes Photos, Music, Apps and App Documents with List/Large views.
- Popups use the compact glass maximize/close controls and dedicated trimmed transparent icons.
- The minimized widget keeps the three original glass states: red disconnected, blue receiving, green sending. It physically docks to a screen edge with an 11-pixel reveal handle, remembers edge/Y/display across restart, and auto-starts collapsed after Windows login.
- Console surfaces use the same interaction language but remain capability-gated until their specific adapters are qualified.

## Engine boundary

Renderer -> preload IPC -> Electron main -> JSONL sidecar -> queued worker jobs.

Long-running operations return a job ID immediately. The sidecar streams progress/results and supports cancellation.

Verbose subprocess output is captured through temporary files rather than undrained Windows pipes, preventing large ADB/package-manager output from blocking a child process while the engine is polling cancellation/timeouts.

Peer file transfer uses the approved-peer transport on TCP 49550. Files are streamed in chunks rather than embedded in the JSON envelope. Remote browsing exposes only configured shared roots unless whole-pc scope is explicitly selected. Relative paths are validated against traversal.

The Apple-device bridge is bundled into the compiled sidecar by THETECHGUY Software Builder through requirements-insync-sidecar.txt; customer PCs do not need a manual Python installation.

## Reused Device Manager evidence

Wi-Fi ADB and package-manager behavior was recovered before implementation from D:\projects\THETECHGUY Device Manager\new eco system:

- PC scripts establish the legacy wireless path as adb tcpip 5555, read wlan0, then connect to <device-ip>:5555.
- The all-in-one Device Manager flow establishes package listing for all, user and system apps through Android package-manager queries.
- WirelessPairActivity.kt establishes Android 11+ Wireless debugging as an OS pairing flow and discovers _adb-tls-pairing._tcp; iNSync therefore supports the corresponding pairing endpoint/code without pretending to bypass the Android pairing prompt.

## Build ownership

Application source and product behavior live in this repository. THETECHGUY Software Builder owns Electron staging, Python sidecar dependencies, PyInstaller sidecar compilation, Electron packaging, ASAR/fuse hardening, native packaging and graphical installers. Patrol owns project-placement and ownership enforcement.

## Current proof baseline

- Branch: feature/standalone-functional-blend
- Product source baseline: 517bd4c (Finish APK export media previews widget and clipboard sync).
- Patrol: PASS on ATHENA and KRATOS.
- Product tests: 33/33 PASS on ATHENA and 33/33 PASS on KRATOS.
- Renderer/backend syntax and compile gates: PASS.
- Android physical proof: real connected-device photo listing and real image preview data returned through ADB; Screenshot_20260925-181003.png returned 807,918 characters of preview data. Large view no longer renders a PHOTO label and hydrates visible image tiles with real preview bytes.
- APK app-manager physical proof on itel A6611L: 270 installed packages listed (17 user + 253 system) in 0.50 seconds after correcting the subprocess output deadlock.
- APK Get-to-PC physical proof: com.thetechguy.ttgservicemode exported as a 102,877-byte APK with SHA-256 3d171aa13da0b6a00a120e295b37dfe9bab1a995d442c145bb6afc851c5dcd79; proof output was removed afterward.
- Wi-Fi ADB physical proof on itel A6611L: the current device reports adb_enabled=1, service.adb.tcp.port=5555 and a live wireless serial 192.168.23.53:5555 alongside USB; adb.wifi.connect returns already connected.
- Android 11+ pairing support follows the existing THETECHGUY Device Manager evidence: the phone owns the Wireless debugging/pairing-code UI, while iNSync accepts the displayed IP:port + pairing code and routes it through the backend pairing operation. A live pair is only claimed when a phone-generated pairing endpoint/code is supplied.
- PC peer discovery physically proved between ATHENA 172.20.10.3 and KRATOS 172.20.10.2.
- ATHENA -> KRATOS streamed file proof: 2,500,000 bytes, matching SHA-256 a8c012d9cf1f86d0c02756344953d4aca88b025b9466bac453c11eae60f3dc3c.
- KRATOS -> ATHENA remote-root browse + streamed receive proof: 2,300,000 bytes, matching SHA-256 efb16b3ca462f3860775e8b312728234e07dec727c29ebf755a3523975b4b261.
- Temporary proof listeners/files were removed afterward. Normal peer state was restored to ATHENA provider / KRATOS receiver, Internet-only, with mutual approval retained.
- ATHENA Wi-Fi -> Ethernet ICS remained Running at 192.168.250.1/24 throughout the peer proof.
- Sharing elevation uses direct ShellExecuteExW + SW_HIDE; Windows may still show UAC consent when required, but iNSync no longer launches a visible intermediate PowerShell console when Sending/Disconnected is clicked.
- Clipboard automatic mode is main-process owned: selected peer IDs, text/image types, direction and ON/OFF state persist in AppData; received peer clipboard data is written directly into the Windows clipboard so normal Paste works on the other machine.
- Sharing elevation uses direct ShellExecuteExW + SW_HIDE; Windows may still show UAC consent when required, but iNSync no longer launches a visible intermediate PowerShell console when Sending/Disconnected is clicked.
- Clipboard automatic mode is main-process owned: selected peer IDs, text/image types, direction and ON/OFF state persist in AppData; received peer clipboard data is written directly into the Windows clipboard so normal Paste works on the other machine.
- Previous live Apple proof detected iPhone14,4 on iOS 18.5 through the bundled bridge.
- Builder's installer taskbar-icon correction is published on fix/installer-taskbar-window-icon-20260925.

## Android ADB runtime

- APK has Install / Apps / Wi-Fi ADB tabs.
- Apps supports All / User / System filtering, search, Get, Delete and refresh.
- The Android app-list backend no longer risks a Windows pipe deadlock on large pm-list-packages output; subprocess stdout/stderr spill to temporary files while cancellation/timeouts remain active.
- Live itel proof returned 270 installed packages (17 user / 253 system) through the engine in about 0.5 seconds.
- Wi-Fi ADB implements status, classic tcpip-5555 enable, connect, disconnect, USB mode, Android 11+ pairing-code support and mDNS discovery.
- iNSync carries the owned ADB 1.0.41 / platform-tools 36.0.2 runtime recovered from THETECHGUY Device Manager/new eco system.
- Existing ecosystem USB/classic ADB stays on port 5037. Modern iNSync Wireless Debugging uses the bundled 1.0.41 server on isolated port 5041, preventing version fights with tools such as TechGuy-IMEI.
- Physical coexistence of 5037 and 5041 was proved. The current phone needs one RSA approval for the newly generated modern-server key before the final 5041 device-operation proof.

## Capability gates

- iPhone Music deletion remains blocked until the Apple media-library database can be updated safely.
- PlayStation/Xbox/Switch install backends remain adapter-pending per console/firmware; the UI does not claim unsupported physical capability.
- Destructive device operations require explicit user selection/confirmation.
