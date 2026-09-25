# iNSync

THETECHGUY DIGITAL SOLUTIONS local transport utility.

## Locked product shape

iNSync uses one transport core with capability-gated adapters for:

- Internet sharing / normal-mode rollback
- PC-to-PC file and Internet sharing
- Selective text/image clipboard exchange
- Android ADB, APK install and app management
- iPhone / IPA transport with signing/provisioning checks
- Console package/file transport behind device-specific qualified adapters

The full app and the minimized clipboard/connection widget share the same Electron main-process state. The minimized widget is a real desktop window, not an in-page imitation.

## UI state

Connection state is visible through the ghost glasses and widget:

- Red — disconnected
- Blue — receiving
- Green — sending

The connection glass can switch between Connection and Clipboard. The popup windows are compact glass panels with close/maximize controls and no default scrolling.

## Build ownership

Application source lives here. THETECHGUY Software Builder owns Electron staging, sidecar packaging, release packaging and installers.

Native release targets must be built on their native host.

## Current proof

Linux DEB:
- installs cleanly on KRATOS
- main Electron window launches as a normal user
- minimize hides the main window and shows the 236x248 widget
- Open iNSync hides the widget and restores the main window

Backend adapters that are not physically qualified remain capability-gated rather than simulated as working.
