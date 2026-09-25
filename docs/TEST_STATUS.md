# iNSync test status - 2026-09-25

## Source and boundary proof

- Patrol project root: PASS.
- JavaScript syntax: main/preload/sidecar/main renderer/widget renderer PASS.
- Python backend compile: PASS.
- Product unittest suite: 11/11 PASS.
- Renderer has one active module authority.
- Android popup has no duplicate APK identity; package controls live under Android.
- IPA and iPhone are separate module surfaces.
- iPhone popup includes Photos, Music, Apps and App Documents.
- Renderer contains no direct transport process execution.
- Job queue/progress/cancellation remains sidecar-owned.
- Approved-peer text/image clipboard transport is sidecar-owned; received clipboard writes occur in Electron main.

## Live engine operations

- sharing.status / sharing.toggle
- peer.list / peer.configure / peer.approve
- clipboard.text / clipboard.image
- files.copy
- adb.devices / adb.info / adb.apps / adb.install / adb.uninstall / adb.disable
- ios.status / ios.validate / ios.ipa
- ios.apps / ios.app.uninstall
- ios.media.list / ios.media.pull / ios.media.delete
- ios.documents.list / ios.documents.pull / ios.documents.push / ios.documents.delete
- console.status / console.pkg capability contracts

## Apple-device proof

- Builder bundles `pymobiledevice3==11.19.1` in the compiled Windows sidecar.
- Compiled sidecar starts successfully from the packaged application.
- Installed sidecar starts successfully from Program Files.
- Installed capability snapshot reports `ios_bridge=true`, `ios_apps=true`, `ios_media=true`, `ios_documents=true`, and `ipa_install=true`.
- Live connected-device read succeeded through the installed sidecar: one iPhone detected, product type iPhone14,4, iOS 18.5, and device storage total/free read successfully.
- Photos support list/save-to-PC/delete.
- Music support list/save-to-PC; raw deletion is blocked until a library-safe media adapter is qualified.
- User apps support list/delete.
- App Documents use House Arrest/AFC for list/send/save/delete.

## Builder / installer proof

- Builder sidecar dependency preparation: PASS.
- PyInstaller Apple-enabled sidecar compilation: PASS.
- Electron packaging readiness: PASS.
- Electron packaging: PASS.
- Packaged application runtime smoke: PASS.
- Graphical installer readiness: PASS.
- Graphical installer creation: PASS.
- Installer package verification: PASS.
- Installer ZIP verification: PASS.
- Installer dry-run: PASS.
- Target execution: 12/12 COMPLETE.
- Verified setup: `installer_output/iNSync-gui-installer-20260925_175155/iNSync Setup.exe`.
- Installed application root: `C:\Program Files (x86)\THETECHGUY Digital Solutions\iNSync`.
- Installed app hash equals the verified packaged app hash.
- Installed backend hash equals the verified packaged backend hash.

## Builder correction discovered during proof

Windows PowerShell 5.1 does not provide PowerShell Core's automatic `$IsWindows` variable. Builder's Python-sidecar verification therefore looked for an extensionless sidecar even after PyInstaller correctly produced `.exe`.

`THETECHGUY Software Builder` was corrected to use a platform check based on `Win32NT`; the regression contract passes 6/6 in `tests/test_live_download_build_progress.py`.

## Live networking proof

- ATHENA Wi-Fi -> Ethernet ICS is working.
- Ethernet private gateway: `192.168.250.1/24`.
- iNSync sharing state is engine-owned and remains separate from renderer execution.

## Remaining capability gates

- Console package installation is still adapter-pending in the console projects/roadmaps.
- Music deletion remains gated for Apple library integrity.
- Device mutations are not considered physically proved merely because the UI/backend contract exists; destructive operations require explicit live test selection.
