# iNSync

THETECHGUY DIGITAL SOLUTIONS local transport and device bridge.

## Product shape

iNSync uses one transport core with capability-gated adapters for:

- Internet sharing / normal-mode rollback
- PC peer discovery, approval, provider/receiver roles and selective sharing
- Streamed iNSync peer file send, remote shared-root browse and receive
- Local, mapped-drive and UNC file transfer
- Approved-peer text/image clipboard sync
- Android phone content through ADB: Photos, Videos and Apps
- A separate APK installer surface for APK/APKS/XAPK/split sets
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
- The minimized widget keeps the three original glass states: red disconnected, blue receiving, green sending. Its dropdown adds mode selection without replacing those states.
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
- Product source baseline: 3ba5b95 (Add streamed peer file sharing and remote browse).
- Patrol: PASS on ATHENA and KRATOS.
- Product tests: 26/26 PASS on ATHENA and 26/26 PASS on KRATOS.
- Renderer/backend syntax and compile gates: PASS.
- Android physical proof: real connected-device photo listing and real image preview data returned through ADB.
- APK app-manager physical proof on itel A6611L: 270 installed packages listed (17 user + 253 system) in 0.54 seconds after correcting the subprocess output deadlock.
- Wi-Fi ADB physical proof on itel A6611L: USB serial -> tcpip 5555 -> 192.168.23.53:5555; iNSync read Android 15 device info and all 270 packages through the wireless serial, then returned adbd to USB mode successfully.
- Android 11+ pairing support follows the existing THETECHGUY Device Manager evidence: the phone owns the Wireless debugging/pairing-code UI, while iNSync accepts the displayed IP:port + pairing code and routes it through the backend pairing operation. A live pair is only claimed when a phone-generated pairing endpoint/code is supplied.
- PC peer discovery physically proved between ATHENA 172.20.10.3 and KRATOS 172.20.10.2.
- ATHENA -> KRATOS streamed file proof: 2,500,000 bytes, matching SHA-256 a8c012d9cf1f86d0c02756344953d4aca88b025b9466bac453c11eae60f3dc3c.
- KRATOS -> ATHENA remote-root browse + streamed receive proof: 2,300,000 bytes, matching SHA-256 efb16b3ca462f3860775e8b312728234e07dec727c29ebf755a3523975b4b261.
- Temporary proof listeners/files were removed afterward. Normal peer state was restored to ATHENA provider / KRATOS receiver, Internet-only, with mutual approval retained.
- ATHENA Wi-Fi -> Ethernet ICS remained Running at 192.168.250.1/24 throughout the peer proof.
- Previous live Apple proof detected iPhone14,4 on iOS 18.5 through the bundled bridge.
- Builder's installer taskbar-icon correction is published on fix/installer-taskbar-window-icon-20260925.

## Capability gates

- iPhone Music deletion remains blocked until the Apple media-library database can be updated safely.
- PlayStation/Xbox/Switch install backends remain adapter-pending per console/firmware; the UI does not claim unsupported physical capability.
- Destructive device operations require explicit user selection/confirmation.
