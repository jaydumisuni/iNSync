# iNSync test status — 2026-09-25

## Proved

- Patrol: registered project root.
- Electron renderer/preload/main syntax checks pass.
- Python sidecar compiles and responds to the app.snapshot RPC.
- Builder-owned Linux DEB packaging completes.
- Fresh-install runtime permissions are usable by a normal desktop user.
- KRATOS package launches.
- Main window -> minimized widget -> main window transition is proved.
- Connection colors are bound to one shared state contract: red disconnected, blue receiving, green sending.

## Live implementation

- ADB discovery and user-app listing use a real adb executable when present.
- APK installation routes through adb when a concrete APK path is supplied.
- Android uninstall backend exists for explicit package names.

## Capability-gated / still requires physical backend qualification

- Windows Internet-sharing mutation inside iNSync. ATHENA's standalone sharing proof exists, but it is intentionally not rebound until Windows iNSync qualification.
- Peer clipboard transport.
- iOS IPA signing/device-service transport.
- Console-specific package installation backends.

Missing adapters do not redesign the UI/core. They fill the existing capability boundary.
