# mediaOS (working name: tvbox)

A small Arch Linux–based distribution for a dedicated media PC behind a TV,
fully usable from the couch with an Xbox controller or a phone.

- Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Decisions and things that didn't work: [docs/DECISIONS.md](docs/DECISIONS.md)
- Original brief: [media-distro-prompt.md](media-distro-prompt.md)

**Status:** Phase 5 (phone remote with QR pairing, bindings editor, health page) on top
of Phases 1–4 (installer, base system, input daemon, system menu, home screen, browser
services, YouTube TV, Jellyfin, navigation for Netflix/Disney+, on-screen keyboard).

## Building

Requirements: `make`, docker or podman (or an Arch host), and for testing
`qemu-system-x86_64` + OVMF (`edk2-ovmf` on Arch, `ovmf` on Debian/Ubuntu).

```sh
make help         # list targets
make packages     # build pkgs/* into build/pkgs (Arch container)
make repo         # pacman repo in build/repo
make iso          # installer ISO in build/iso   (from Phase 1)
make qemu-iso     # boot the ISO in QEMU (UEFI, NVMe test disk)
make qemu-disk    # boot the installed test disk
make serve-repo   # let the VM pacman -Syu from your local build
make lint         # shellcheck + python checks
make qemu-smoke   # self-test of the QEMU harness, headless
make qemu-install # unattended install from the ISO + checks on the booted system
make qemu-session # input, menu, launcher, navigation, keyboard and phone checks in that VM
```

Docker needs to be usable by your user (`sudo usermod -aG docker $USER`, then
log in again or prefix commands with `sg docker -c "make iso"`).

`make lint` also runs the unit tests when `python3` has `pytest`, `evdev` and
`aiohttp`; otherwise point it at an interpreter that does:
`PYTHON=~/venv/bin/python make lint` (in a venv without Python headers,
`pip install pytest aiohttp evdev-binary`).

While developing, `tests/qemu/vm.sh up | push | reboot | ssh | tv | shot | down`
drives the installed test VM: `push` installs the packages from `build/repo`
without reinstalling the system.

Settings live in `config.mk` and can be overridden per call, e.g.
`make qemu-iso QEMU_MEM=8G QEMU_DISPLAY=vnc`.

Nothing in this repo ever writes to a real disk on the development machine;
installs are only tested on the QEMU disk image under `build/qemu/`.
