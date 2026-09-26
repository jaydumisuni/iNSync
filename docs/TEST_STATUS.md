# iNSync test status - 2026-09-26

## Source and boundary proof

- Patrol project root on ATHENA: PASS.
- Patrol project root on KRATOS: PASS.
- Frozen product source baseline: e5fe414.
- Python backend compile: PASS.
- Renderer JavaScript parse: PASS.
- ATHENA product unittest suite: 22/22 PASS.
- KRATOS product unittest suite: 22/22 PASS.
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
- adb.devices / adb.info / adb.apps / adb.install / adb.uninstall
- adb.media.list / adb.media.preview / adb.media.pull / adb.media.delete
- ios.status / ios.validate / ios.ipa
- ios.apps / ios.app.uninstall
- ios.media.list / ios.media.preview / ios.media.pull / ios.media.delete
- ios.documents.list / ios.documents.pull / ios.documents.push / ios.documents.delete
- console.status / console.pkg capability contracts

## Android physical proof

- Real ADB device media enumeration completed successfully.
- Two real device photos were returned during proof.
- adb.media.preview returned actual image data for the first device image.
- APK installation remains a separate UI surface from Android phone content.
- System/user package removal uses direct Android uninstall first, then user-0 package removal fallback where required.

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
