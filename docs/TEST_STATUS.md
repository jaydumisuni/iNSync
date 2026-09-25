# iNSync test status - 2026-09-25

## Proved

- Patrol: iNSync is a registered project root.
- Patrol: THETECHGUY Software Builder remains Builder-owned.
- Electron main/preload/sidecar JavaScript syntax: PASS.
- Main renderer JavaScript syntax: PASS.
- Widget renderer JavaScript syntax: PASS.
- Python engine compile: PASS.
- Product unittest suite: 5/5 PASS.
- JSONL sidecar event bridge: PASS.
- Engine startup event: PASS.
- Job queue submission latency proof: 0.43 ms in local engine proof.
- File copy job: byte-identical result PASS.
- Job cancellation: PASS on an in-flight 256 MB copy proof.
- ADB capability detected on ATHENA.
- ADB device refresh through queued engine job: PASS.
- Read-only Windows sharing status through engine: PASS.
- Live sharing state resolved as Wi-Fi public -> Ethernet private.
- Main renderer contains no child_process, subprocess, PowerShell, or ADB execution path.
- Minimized widget is a separate BrowserWindow, not an in-page overlay.

## Live engine operations

- sharing.status
- sharing.toggle
- adb.devices
- adb.apps
- adb.install
- adb.uninstall
- files.copy
- peer.list contract
- clipboard.text contract
- clipboard.image contract
- ios.status
- ios.ipa
- console.status contract
- console.pkg contract

## Installed Windows runtime proof

- Builder result: complete, exitCode 0.
- Graphical installer verification: PASS.
- Installer ZIP verification: PASS.
- Installer dry-run: PASS.
- Installed iNSync.exe hash matches the verified build payload.
- Installed iNSync-backend.exe hash matches the verified compiled sidecar.
- Installed main window: one visible compact main surface (~820x750 configured).
- Minimize transition: main leaves the desktop and the 224x262 floating widget becomes the only on-screen iNSync surface.
- Second-instance transition: existing main window restores and widget hides; no duplicate main instance is created.
- Installed transparent-window proof: empty artwork corners match the underlying desktop pixel-for-pixel.
- Live ATHENA sharing remained running throughout the build/install/runtime proof.

## Physically qualified

- ATHENA Wi-Fi -> Ethernet Internet sharing works live.
- Local/network path file-copy engine works with progress/cancel.
- ADB engine path is available on ATHENA.

## Capability-gated / next qualification

- iNSync peer pairing/approval and peer-native clipboard transport.
- iOS IPA install requires libimobiledevice/ideviceinstaller or the qualified TTG iOS adapter.
- Console-specific PKG/package install backends.
- Receiver-side iNSync automatic role detection beyond the current shared state contract.

Missing adapters fill the existing capability boundary; they do not redesign the application.
