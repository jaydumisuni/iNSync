# iNSync

THETECHGUY DIGITAL SOLUTIONS local transport and device bridge.

## Product shape

iNSync uses one transport core with capability-gated adapters for:

- Internet sharing / normal-mode rollback
- PC-to-PC file and Internet sharing
- Selective text/image clipboard exchange
- Android ADB, APK install and app management
- iPhone / IPA transport with signing/provisioning checks
- Console package/file transport behind qualified device-specific adapters

The full application and the minimized clipboard/connection widget share one main-process state contract. The minimized widget is a real desktop window and only appears when the main iNSync window is minimized.

## Connection state

The original ghost glasses in the supplied artwork are recolored in-place:

- Red - disconnected
- Blue - receiving
- Green - sending

No second glasses overlay is rendered.

## Engine boundary

The Electron renderers do not run transport commands.

Renderer -> preload IPC -> Electron main -> JSONL sidecar -> queued worker jobs.

Long-running operations return a job ID immediately. The engine streams progress/result events and supports cancellation. This keeps the UI responsive while ADB installs, file copies, network transitions, and future device adapters are active.

## Functional UI

Current popup controls include:

- Sharing status / Start sharing / Back to normal
- File and folder explorers with destination selection
- PC Share using local, mapped, or UNC destinations
- Clipboard text/image selection
- Android device refresh, APK selection/install, user-app list/uninstall
- IPA selection/device status/install when a qualified iOS backend exists
- Console package selection/status/install queue surfaces

Unsupported backend capabilities remain visibly gated instead of being simulated as working.

## Build ownership

Application source lives in this repository.

THETECHGUY Software Builder owns:
- Electron staging
- Python sidecar compilation
- ASAR/fuse hardening
- native package generation
- signing/release packaging
- graphical installers

Patrol owns project-placement and ownership enforcement.

Native release targets are built on their native host.

## Current proof

ATHENA:
- Patrol allows D:\projects\iNSync
- Patrol allows D:\projects\THETECHGUY Software Builder
- JSONL engine protocol works through the Node sidecar bridge
- job submission returns immediately
- file-copy engine transfers byte-identical data
- running file-copy job cancellation is proved
- live sharing status resolves Wi-Fi -> Ethernet without mutating the working connection
- ADB is available and ADB device discovery runs as an engine job
- renderer/preload/main/sidecar syntax checks pass
- product tests pass 5/5

KRATOS:
- prior Linux DEB install and main-window/widget transition were proved
- Linux release will be rebuilt from the same source after this Windows functional freeze
