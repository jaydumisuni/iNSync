# iNSync finish handoff — 2026-09-26

## Authority

Current project root: `D:\projects\iNSync`

Current branch: `feature/standalone-functional-blend`

Frozen starting commit for this pass: `c215e65` — `Finish scrollable panels media previews and widget peers`

Patrol status at start of this pass: PASS.

Working tree at start of this pass: clean.

Do not redesign the product while closing this pass. The user has reduced the finish scope to the three defects below, then KRATOS installation and remaining two-machine proof.

## Current installed baseline on ATHENA

The installed copy and current Builder output were rechecked before this pass. These three installed files hash-match the current Builder output byte-for-byte:

- `iNSync.exe`
- `resources\app.asar`
- `resources\sidecars\iNSync-backend.exe`

The installed/source baseline already contains:

- APK -> Install / Apps / Wi-Fi ADB
- installed Android app listing and management
- Wi-Fi ADB status / tcpip 5555 / connect / disconnect / USB mode
- Android 11+ pairing / mDNS discovery support
- Android Photos / Videos / Apps
- iPhone / IPA split
- streamed peer file send / remote browse / receive
- floating widget
- THETECHGUY Builder packaging

Do not reopen or redesign those features unless a proof step exposes a regression.

## Finish scope — no drift

### 1. Fix Android/APK app-list overlap

Observed from the user's screenshot:

- app rows overlap visually
- package/app identity becomes unreadable
- Delete/Get actions must remain aligned and usable
- list must retain hidden-scroll behavior

Required implementation direction:

- stable per-row minimum height
- one clean row per app
- identity column must be `min-width:0`
- app/package text must stay readable without spilling into adjacent rows
- actions remain a fixed-width right column
- do not add a visible scrollbar
- preserve app icons and existing All / User / System / search behavior

This CSS affects both Android Apps and APK Apps because they share `.app-row` / `.app-list`.

### 2. Fix floating widget peer clipping

Observed from the user's screenshot:

- `No approved peers` is partially clipped
- approved-peer content must also remain usable if more peers appear

Required behavior:

- keep the widget compact
- clipboard pane gets the same hidden-scroll interaction used by the main app
- `No approved peers` must be fully visible
- approved peer rows can scroll with mouse wheel/trackpad without a visible scrollbar
- interacting with the widget must not make it collapse while the pointer/focus is inside it

Do not redesign the original three-state glass widget.

### 3. Add real Windows system-tray behavior for minimized iNSync

Current evidence:

- `main.cjs` has no Electron `Tray` object
- current minimize path hides main and shows the floating widget
- widget itself uses `skipTaskbar:true`

Required behavior:

- when main iNSync is minimized/hidden into widget mode, create/show the iNSync tray icon in the Windows taskbar `^` system-tray area
- double-click tray icon restores/focuses the main iNSync window
- when the main window is restored, destroy/hide the tray icon so it disappears again
- keep the floating widget behavior when minimized
- second-instance activation must still restore the main window
- tray icon must use the real iNSync app icon, not a blank/default Electron icon
- do not leave duplicate tray icons after repeated minimize/restore cycles
- app Exit still performs a real exit and cleanup

## Exact execution order

1. Recover current icon assets used by packaged iNSync and select the correct tray icon source.
2. Implement app-row layout correction.
3. Implement widget hidden-scroll / peer-empty visibility correction.
4. Implement Electron Tray lifecycle tied to widget/minimized state.
5. Add regression tests for all three contracts.
6. Run Patrol.
7. Run renderer syntax + backend compile + full iNSync tests.
8. Build the exact frozen revision through THETECHGUY Software Builder.
9. Install that exact build on ATHENA.
10. Prove installed hashes against Builder output.
11. Prove minimize -> tray appears -> double-click -> main restores -> tray disappears.
12. Prove Apps list visually/structurally no longer overlaps.
13. Prove widget peer area no longer clips and hidden-scroll still works.
14. Install the same frozen artifact on KRATOS.
15. Verify KRATOS installed hashes match the same artifact.
16. With ATHENA and KRATOS on the same network, test the remaining two-machine paths that have not yet had installed-to-installed proof.
17. Update this handoff and `docs/TEST_STATUS.md` with exact results, hashes, commits, and any remaining hardware/capability gates.

## Network / machine proof context

ATHENA and KRATOS are currently available through Oracle.

Prior real peer proof already established:

- ATHENA and KRATOS discovered each other on the same Wi-Fi
- ATHENA -> KRATOS streamed file transfer passed with matching SHA-256
- KRATOS -> ATHENA remote-root browse + pull passed with matching SHA-256
- ATHENA Internet sharing remained running and unchanged during peer proof

The next two-machine pass is specifically for the installed application behavior and any still-unproved network/clipboard/user-facing flows. Do not repeat already-proved lower-level transport work unless the installed build disagrees.

## Documentation rule for continuation

After every meaningful boundary, append to this file:

- what changed
- exact commit/hash
- what was proved
- what failed
- what remains

Do not claim completion from source code alone. Installed/runtime proof is required where the requirement is user-visible or machine-to-machine.
