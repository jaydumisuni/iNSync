# iNSync Companion for PS4

THETECHGUY iNSync Companion is the console-side peer for iNSync.

## v1 contract

- Title ID: `TTGI00001`
- Content ID: `IV0000-TTGI00001_00-INSYNCCOMPANION0`
- TCP API: `49560`
- Pairing requires one physical Cross-button approval on the console.
- The companion owns one shared install queue used by both the PC and PS4 UI.
- Installed-title inventory is read-only from `/user/app` + `/user/appmeta/<TITLE_ID>/param.sfo`.
- BGFT controls: start, pause, resume, stop, progress.
- Pending queue order is companion-owned. `Move to top` reorders work before BGFT registration.

## Bootstrap rule

FTP is transport only. It cannot execute or install a new app by itself.

Bootstrap order:
1. Companion already running -> pair and use it.
2. Remote Package Installer (`12800`) -> install Companion PKG automatically.
3. GoldHEN BinLoader (`9090`) -> a small bootstrap payload can install the staged Companion PKG.
4. GoldHEN FTP only -> stage the Companion PKG to `/data/pkg`; one console-side install is required once.

After the companion is installed, flatZ RPI is not required for normal iNSync package management.

## Console controls

- Cross: approve a pending pair request
- Up/Down: select item
- Triangle: pause/resume active install
- Square: move a pending item to the top
- Circle: cancel selected item
- L1/R1: switch Queue / Installed Games

## Build

Requires OpenOrbis PS4 Toolchain v0.5.4 or compatible.

```sh
export OO_PS4_TOOLCHAIN=/path/to/OpenOrbis/PS4Toolchain
make eboot
make package
```
