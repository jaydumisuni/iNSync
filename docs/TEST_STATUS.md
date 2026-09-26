# iNSync test status - 2026-09-26

## Source and boundary proof

- Patrol project root on ATHENA: PASS.
- Patrol project root on KRATOS: PASS.
- Frozen product source baseline: 517bd4c.
- Python backend compile: PASS.
- Renderer JavaScript parse: PASS.
- ATHENA product unittest suite: 33/33 PASS.
- KRATOS product unittest suite: 33/33 PASS on 517bd4c.
- Renderer has one active module authority.
- APK and Android are separate surfaces sharing the same ADB engine.
- Android content surface includes Photos, Videos and Apps.
- Android media Large view hydrates real preview image data when available.
- Long device/app/media lists scroll without a visible scrollbar.
- iPhone and IPA are separate surfaces.
- Backend startup is silent; peer discovery/transport listeners are lazy.
- Renderer contains no direct process/subprocess execution.
- Job queue/progress/cancellation remains sidecar-owned.

## Live engine operations

- sharing.status / sharing.toggle
- peer.list / peer.configure / peer.approve
- peer.files.send / peer.files.roots / peer.files.list / peer.files.pull
- clipboard.text / clipboard.image
- files.copy
- adb.devices / adb.info / adb.apps / adb.install / adb.uninstall / adb.app.export / adb.app.export
- adb.wifi.status / adb.wifi.enable / adb.wifi.connect / adb.wifi.disconnect / adb.wifi.usb / adb.wifi.pair
- adb.media.list / adb.media.preview / adb.media.pull / adb.media.delete
- ios.status / ios.validate / ios.ipa
- ios.apps / ios.app.uninstall
- ios.media.list / ios.media.preview / ios.media.pull / ios.media.delete
- ios.documents.list / ios.documents.pull / ios.documents.push / ios.documents.delete
- console.status / console.pkg capability contracts

## APK app-manager and Wi-Fi ADB proof

Source evidence was recovered from D:\projects\THETECHGUY Device Manager\new eco system before implementation. The existing PC tools prove the intended tcpip 5555 -> wlan0 -> connect sequence; the Device Manager app-manager flow proves all/user/system package listing; and WirelessPairActivity.kt proves Android 11+ pairing remains an OS Wireless debugging flow.

Live itel A6611L proof:

- USB serial: 157503761E002929.
- APK app manager returned 270 installed packages: 17 user + 253 system.
- Get-to-PC exported com.thetechguy.ttgservicemode.apk: 102,877 bytes, SHA-256 3d171aa13da0b6a00a120e295b37dfe9bab1a995d442c145bb6afc851c5dcd79. Proof output was removed after verification.
- Direct package-manager measurements were ~0.16-0.19 seconds. The earlier 35-second timeout was traced to iNSync polling a child process while leaving stdout/stderr in undrained Windows pipes.
- run_process and binary process capture now spill stdout/stderr to temporary files while retaining cancellation and timeout semantics.
- Regression proof writes 512 KiB of child stdout and completes without pipe deadlock.
- Phone Wi-Fi address during current proof: 192.168.23.53; USB and 192.168.23.53:5555 were both live ADB transports.
- adb.wifi.enable switched adbd to TCP 5555 and connected 192.168.23.53:5555.
- Over the wireless serial, iNSync read itel A6611L / Android 15 / SDK 35 / storage and the same 270-package inventory.
- A later proof observed Wireless ADB already active again: adb_enabled=1, service.adb.tcp.port=5555; adb.wifi.connect returned already connected to 192.168.23.53:5555.
- Android 11+ pairing is implemented as IP:port + pairing code support. No live pairing is claimed because no phone-generated pairing code was supplied during this proof.

## Android physical proof

- Real ADB device media enumeration completed successfully.
- Two real device photos were returned during proof.
- adb.media.preview returned actual image data for the first device image.
- Screenshot_20260925-181003.png returned 807,918 characters of preview data during the current proof.
- Screenshot_20260925-181003.png returned 807,918 characters of preview data during the current proof.
- APK installation remains a separate UI surface from Android phone content.
- System/user package removal uses direct Android uninstall first, then user-0 package removal fallback where required.

## UI/runtime regression proof

- Global application/modal/media/app scrollbars are hidden with Chromium and Firefox scrollbar rules while wheel/trackpad scrolling remains enabled.
- APK Apps uses a fixed action column: Get and Delete cannot be pushed out by long package names.
- Android app Delete remains fixed/visible.
- Large photo cards no longer show the generic PHOTO label; visible cards request real Android/iPhone preview data through IntersectionObserver hydration.
- The floating widget is physically docked by the Electron main process: only an 11-pixel edge handle remains when collapsed, pointer entry expands it, its draggable header snaps to the nearest edge, and edge/Y/display are persisted in AppData.
- iNSync registers the packaged app for Windows login with --insync-startup so the saved edge widget returns collapsed after login.
- Auto Clipboard persists selected peer IDs, text/image types and direction. Copy changes are watched by Electron main and delivered through the approved-peer backend; received data is written to the local Windows clipboard.
- Sharing elevation uses direct ShellExecuteExW with SW_HIDE, removing the visible intermediate PowerShell console flash. UAC consent can still be shown by Windows when elevation is required.

## Peer transport physical proof

ATHENA and KRATOS were tested on the same Wi-Fi subnet:

- ATHENA: 172.20.10.3
- KRATOS: 172.20.10.2
- ATHENA peer ID: 73f3469e922f440ba2078f58608eca62
- KRATOS peer ID: 2d050243ce504b10a0134fef0b65bf6d

Discovery succeeded in both directions with mutual approval.

ATHENA -> KRATOS:

- Streamed 2,500,000-byte file through peer.files.send.
- Sender SHA-256: a8c012d9cf1f86d0c02756344953d4aca88b025b9466bac453c11eae60f3dc3c.
- KRATOS on-disk SHA-256 matched exactly.

KRATOS -> ATHENA:

- ATHENA queried KRATOS shared roots through peer.files.roots.
- ATHENA listed the remote root through peer.files.list.
- ATHENA pulled a 2,300,000-byte file through peer.files.pull.
- Remote and local SHA-256 matched: efb16b3ca462f3860775e8b312728234e07dec727c29ebf755a3523975b4b261.

Path traversal checks are covered by tests, and remote browse exposes only configured shared roots unless whole-PC sharing is explicitly selected. Proof listeners and files were removed after verification.

## Apple-device proof

- Builder bundles pymobiledevice3==11.19.1 in the Windows sidecar.
- Previous installed-sidecar live proof detected one iPhone, product type iPhone14,4, iOS 18.5, and storage total/free.
- Photos support list/preview/save-to-PC/delete.
- Music supports list/save-to-PC; raw delete remains gated for library integrity.
- User apps support list/delete.
- App Documents use House Arrest/AFC for list/send/save/delete.

## Builder / installer proof

Builder fixes are isolated and published on fix/installer-taskbar-window-icon-20260925:

- 8c1b8c8 - force native installer taskbar/window icon.
- bf1a92e - allow explicit Builder Python runtime in isolated Patrol workspaces.
- Windows PowerShell sidecar executable detection is also corrected in the Builder lineage.

The graphical installer pipeline has already passed readiness, creation, package verification, ZIP verification and dry-run with zero issues. A final package/install proof is repeated after each frozen product-source change.

## Live networking proof

- ATHENA Wi-Fi -> Ethernet ICS remains Running.
- Ethernet private gateway remains 192.168.250.1/24.
- Peer proof used ATHENA/KRATOS Wi-Fi addresses and did not alter ICS.
- Normal peer configuration after proof: ATHENA provider / KRATOS receiver, Internet-only, mutual approval retained.

## Remaining capability gates

- PlayStation/Xbox/Switch package installation remains adapter-pending until those console-specific backends are qualified.
- iPhone Music deletion remains gated for Apple library integrity.
- Hardware-dependent operations are only marked physically proved where matching hardware was present for the test.
