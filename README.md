# iNSync

THETECHGUY DIGITAL SOLUTIONS local transport and device bridge.

## Product shape

iNSync uses one transport core with capability-gated adapters for:

- Internet sharing / normal-mode rollback
- PC peer discovery, roles and selective sharing
- Local, mapped-drive and UNC file transfer
- Approved-peer text/image clipboard sync
- Android ADB package install and app management
- IPA validation/install as its own Apple package surface
- iPhone Photos, Music export, installed apps and app Documents
- Console package/file surfaces behind qualified console-specific adapters

The full application and the minimized clipboard/connection widget share one main-process state contract. Transport work never runs in the Electron renderer.

## UI contract

The supplied standalone/glass popup language is the active UI authority.

- Android has one Android surface. APK/APKS/XAPK/split-package work lives inside Android; there is no duplicate APK tile in the popup.
- IPA is its own popup for choosing, validating and installing `.ipa`.
- iPhone is separate from IPA and exposes Photos, Music, Apps and App Documents.
- Photos: list, save to PC, delete.
- Music: list and save to PC. Raw delete is intentionally blocked until a library-safe Apple media adapter is qualified.
- Apps: list installed user apps and delete.
- App Documents: list, send a PC file into Documents, save to PC, delete.
- Console popups keep the same iNSync glass language; package install remains capability-gated until the matching console backend is qualified.

The original ghost glasses are recolored in place: red = disconnected, blue = receiving, green = sending. No second glasses overlay is rendered.

## Engine boundary

Renderer -> preload IPC -> Electron main -> JSONL sidecar -> queued worker jobs.

Long-running operations return a job ID immediately. The sidecar streams progress/results and supports cancellation, preventing ADB installs, file transfers, Apple-device work and network changes from blocking the UI.

The Apple-device bridge is bundled into the compiled sidecar by THETECHGUY Software Builder through the project-declared `requirements-insync-sidecar.txt`; customer PCs do not need a manual Python installation.

## Build ownership

Application source and product behavior live in this repository. THETECHGUY Software Builder owns Electron staging, Builder-owned Python sidecar dependencies, PyInstaller sidecar compilation, Electron packaging, ASAR/fuse hardening, native packaging, signing/release packaging and graphical installers. Patrol owns project-placement and ownership enforcement.

## Current Windows proof

- Patrol allows `D:\projects\iNSync`.
- Product tests: 11/11 PASS.
- Renderer/widget/main/preload/sidecar syntax and Python compile: PASS.
- Builder flow: 12/12 complete.
- Builder runtime smoke: PASS.
- Graphical installer verification: PASS.
- Installer ZIP verification: PASS.
- Installer dry-run: PASS.
- Installed `iNSync.exe` and `iNSync-backend.exe` hashes match the verified Builder payloads.
- Installed compiled sidecar reports ADB, peer clipboard, iOS bridge, iOS apps, iOS media, iOS Documents, IPA install and Windows sharing capabilities.
- Live connected iPhone proof: one iPhone detected through bundled `pymobiledevice3`; iOS 18.5, iPhone14,4 and device storage were read successfully.
- ATHENA Wi-Fi -> Ethernet Internet sharing remained working while iNSync was rebuilt and installed.

## Capability gates

- Music deletion is intentionally blocked until the Apple music-library database can be updated safely.
- Console package install remains gated per console/firmware until the PS/Xbox/Switch adapters are qualified.
- Destructive operations require explicit user selection/confirmation.
