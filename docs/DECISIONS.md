# Decisions log

Non-obvious choices, newest phase last. Each entry: what, why, alternatives,
and anything that was tried and didn't work. Design rationale that belongs to
the whole system lives in [ARCHITECTURE.md §5](ARCHITECTURE.md#5-key-decisions-and-trade-offs-details-in-decisionsmd);
this file records the concrete decisions as they are made.

## Phase 0 — skeleton, build tooling, QEMU harness

### Make, not just
`make` is preinstalled on every dev machine and CI runner; `just` would be one
more thing to install for no real gain. Targets: `make help`.

### Builds run in an Arch container
`makepkg`, `repo-add` and `mkarchiso` only exist on Arch.
`scripts/run-in-builder.sh` runs each build step in `tools/builder.Dockerfile`
(docker or podman, auto-detected) and runs it natively when the host is Arch
(`CONTAINER=none`). The container is a clean environment per run, which is
what `makechrootpkg` would give us; `makechrootpkg` itself is not used because
it needs systemd-nspawn, which does not run reliably inside docker.
- The entrypoint remaps the `builder` user to the host UID so outputs in
  `build/` are owned by the developer, and `makepkg` (which refuses root)
  works.
- Only `make iso` uses `--privileged` (mkarchiso mounts filesystems).
- `EXTRA_CA_CERT=… make builder` bakes an extra CA into the image for
  networks with TLS-intercepting proxies.

### Package versions = VERSION.r<commit count>
Every commit yields a strictly newer `pkgver`, so the box sees any new build as
an update without anyone bumping versions by hand. PKGBUILDs read it from
`$TVBOX_PKGVER`. CI must check out full history (`fetch-depth: 0`).

### Packages can depend on each other within one build
`build-packages.sh` registers each built package in a throwaway local repo
(`_buildlocal`) that pacman inside the builder can resolve, then builds the
next one. Order: pinned AUR packages (`pkgs/aur.list`) first, then
`pkgs/build-order`.

### Third-party packages are pinned AUR commits, rebuilt into [tvbox]
The box never talks to the AUR. `pkgs/aur.list` pins each AUR package to a git
commit, so an upstream PKGBUILD change is reviewed (by bumping the pin) before
it reaches the box.

### QEMU test machine mirrors the board
q35 + OVMF (UEFI), **NVMe** disk so the installer sees `/dev/nvme0n1` like on
the real mainboard, virtio-gpu (optional `QEMU_GL=1` for virgl), xHCI with USB
keyboard + tablet, optional USB passthrough of a real controller
(`QEMU_USB_HOST=vid:pid`), optional HDA audio. Serial log and a QMP socket per
run allow scripted tests; `scripts/qmp.py` is stdlib-only so it runs anywhere.
No KVM → falls back to TCG automatically (slow but works in CI).

`make qemu-smoke` boots the bare firmware headless, waits for OVMF output on the
serial log, takes a QMP screendump, sends keys and quits. It validates the
harness itself (OVMF detection, UEFI vars copy, disk creation, QMP) on any
host, independent of Arch.

### Tried and didn't work
- **Building packages/ISO from the cloud dev session used for Phase 0:** its
  network policy blocks Arch mirrors (`*.mirror.pkgbuild.com`,
  `mirrors.kernel.org`, `archive.archlinux.org`) and the AUR
  (`aur.archlinux.org`); pacman gets 403 from the egress proxy. The builder
  image therefore could not be built there. Not a design issue: CI (GitHub
  runners) and any normal machine have mirror access. Everything that doesn't
  need mirrors (lint, QEMU harness) was run there.
- **shellcheck SC2054** flags QEMU's comma-separated option values as array
  mistakes; disabled file-wide in `scripts/qemu.sh` only.

### Phase 0 review answers
Name `tvbox`; automatic boot fallback instead of controller-driven GRUB menu;
`linux-lts`; phone remote over plain HTTP with token auth; sshd on (key-only);
32 GB RAM, so all services may stay alive in the background; home
screen/overlay in WebKitGTK; browser chosen by testing in Phase 3.

### Package repository: GitHub Pages, signed in CI
- **Hosting:** every push to `main` builds the repo in CI and, if a signing key
  is configured, deploys it to GitHub Pages (`make pages` stages it as
  `x86_64/` with symlinks replaced by real files, since static hosting serves
  no symlinks). The box's `/etc/pacman.d/tvbox-mirrorlist` points there. Only
  the latest packages are hosted; old versions are not needed because rollback
  uses snapper snapshots, not package downgrades.
- **Signing:** one ed25519 key without passphrase, created once by
  `scripts/gen-signing-key.sh` on the owner's machine. The private half lives
  only in the GitHub secret `TVBOX_SIGNING_KEY` (plus the owner's backup);
  the public half is committed in `pkgs/tvbox-keyring` and installed into
  pacman's keyring. `[tvbox]` uses `SigLevel = Required`, so the box rejects
  any unsigned or foreign package. Unsigned builds (no secret, or from
  branches and PRs) are built and uploaded as CI artifacts but never published.
- **Why not generate the key in the dev session:** the private key would pass
  through the session transcript. The script refuses to write the private key
  inside the repository.
- **Requirement:** GitHub Pages for a private repository needs a paid GitHub
  plan. With a free account the repository must be public, or the Pages site
  must come from a separate public repository.

## Phase 1 — installer ISO, base system, btrfs/snapper, session

### ISO: releng, trimmed, UEFI only
`iso/` started as archiso's `releng` profile with BIOS/syslinux, the speech
and memtest entries, cloud-init, VM guest agents, modem/VPN tooling and most
rescue tools removed. The target is UEFI-only, so systemd-boot is the only
ISO boot mode. Kernel params add `console=ttyS0` so QEMU tests can read the
serial log; on real hardware without a serial port it is harmless.

### The installer is a package (`tvbox-installer`)
It is linted, versioned and built like everything else, and the ISO just
installs it. It autostarts on tty1 (`/root/.zlogin`); other ttys and SSH get
a normal shell for rescue work. A `dialog` TUI asks only for the disk, Wi-Fi
(only when no wired connection works) and an optional GitHub username to
import SSH keys, then a default-*no* confirmation.

### Unattended install only via QEMU fw_cfg
Automated tests answer the installer through a QEMU fw_cfg blob
(`opt/tvbox/autoinstall`). Real hardware has no fw_cfg, so there is no kernel
parameter or file on the stick that could make a real machine wipe its disk
without a confirmation.

### Disk layout
1 GiB ESP at `/efi` (GRUB's EFI binary only) + one btrfs partition with
`@ @home @snapshots @var_log @var_cache_pacman_pkg @var_tmp`, mounted
`noatime,compress=zstd:1`. `/boot` is inside `@`. `subvolid=` is stripped from
fstab so a rolled-back `@` mounts by name. No swap partition: zram.

### What the installer still writes by hand
Only machine-specific or one-time things: fstab, hostname, locale, timezone
(auto-detected from IP, fallback UTC), the `[tvbox]` stanza in
`/etc/pacman.conf`, users, SSH keys, the Wi-Fi profile, the snapper config
(copied from a template in `tvbox-base`), and 3 lines in `/etc/default/grub`.
`/etc/default/grub` is owned by the `grub` package and Arch's GRUB has no
`grub.d` drop-in directory for it, so editing it once at install time is the
least bad option.

### Enabling units from a package without fighting the user
`tvbox-base` lists the units it wants in `/usr/share/tvbox/enabled-units`. Its
install script enables each unit the first time it appears and records it in
`/var/lib/tvbox/enabled-units.seen`. A unit added in a later version is
enabled on upgrade, and a unit the user disabled stays disabled. A blanket
`systemctl preset-all` was rejected because it would also reset units we
don't own.

### GRUB is reinstalled on every grub upgrade
Arch doesn't re-run `grub-install` when the grub package updates, which can
leave an old EFI binary with new modules. A pacman hook runs
`tvbox-grub-update`, which installs the named entry plus the removable path
`EFI/BOOT/BOOTX64.EFI` (bare boards sometimes lose NVRAM entries) and
regenerates `grub.cfg`.

### initramfs uses busybox/udev hooks, not systemd
`grub-btrfs-overlayfs` (makes read-only snapshots bootable with a tmpfs
overlay) is a busybox `run_latehook` hook and does not work with the systemd
initramfs, so the HOOKS line uses `base udev …`. No `fsck` hook (btrfs
doesn't need it).

### Session: greetd + restart loop
greetd auto-logs in `tv` once (`initial_session`). `tvbox-session` restarts
sway if it exits with an error and gives up after 5 crashes within 10 s each,
so a broken compositor falls back to a text login (`default_session`) instead
of a crash loop. greetd's own config file belongs to the greetd package, so
ours is selected with a `greetd.service` drop-in (`--config`).
`WLR_RENDERER_ALLOW_SOFTWARE=1` lets sway start in QEMU without virgl; on
real hardware the Intel GPU is used anyway.

### Who can do what
`tv` has no password, no sudo, and is in `input video audio`. Maintenance is
`ssh root@box`, key-only. If `tv` (which runs the browsers) is compromised,
that doesn't give root.

### No packages built with --syncdeps
Our packages are `arch=any` with no build step, so `makepkg --nodeps` is used
for them. Otherwise building `tvbox-base` would install its whole runtime
dependency tree (kernel, mesa, ...) into the builder. AUR packages still use
`--syncdeps`.

### Tried and didn't work (Phase 1)
- **Separate `intel-ucode.img` initrd in the ISO boot entry:** current archiso
  embeds microcode in the initramfs (mkinitcpio `microcode` hook) and no
  longer ships the file; systemd-boot failed with "Error preparing initrd: Not
  found" and OVMF fell through to the UEFI shell. Removed the line.
- **releng's `systemd-time-wait-sync`:** blocks boot until NTP succeeds, with
  no time limit. On a network that blocks NTP (the QEMU test sandbox, some
  guest Wi-Fi) the installer never starts. Removed from the ISO; the RTC is
  accurate enough for signature checks and timesyncd still runs.
- **Guest downloading directly from Arch mirrors in the test sandbox:** the
  sandbox only allows HTTPS through an intercepting proxy, and the VM
  doesn't trust its certificate, so the installer reported "no network".
  `tests/qemu/install.sh` now starts `scripts/mirror-cache.py`, a small
  caching HTTP mirror on the host (packages cached in `build/mirror-cache`,
  databases always fresh). The installer takes `mirror=` from the
  fw_cfg answers. This is useful anyway: repeat test installs no longer download ~1 GB.
  The installer's online check now probes the first configured mirror instead
  of a hard-coded host.
- **foot as the placeholder screen in QEMU:** under TCG, foot drew its
  background but no glyphs. The cause turned out to be Mesa's llvmpipe (see
  below): with the pixman renderer in VMs, foot renders text normally. The
  placeholder still uses `pango-view` + the sway background, which is simpler
  than a terminal for a static status screen.
- **Leaving the embedded repo's sync db on the installed system:** a local
  (unsigned) dev ISO left `/var/lib/pacman/sync/tvbox.db` behind, and with
  `SigLevel = Required` every pacman operation then failed with "missing
  required signature". The installer now deletes it after pacstrap, so the box
  fetches the signed db from its own mirror.
- **sway output power on right after power off** failed once with "Backend
  commit failed" in QEMU and worked on retry. To keep in mind for HDMI
  hotplug handling (Phase 6): retry output re-enable.
- **`grub-reboot` into a grub-btrfs snapshot entry:** GRUB follows
  `next_entry` into the "snapshots" submenu but stops there and waits for a
  key. grub-btrfs loads its menu with `configfile`, and GRUB doesn't carry the
  default entry or the timeout into configfile menus. Manual selection with a
  keyboard works. **Consequence for the automatic boot fallback (Phase 6):** it
  cannot point at grub-btrfs entries. tvbox will generate its own top-level
  menuentry, with a fixed ID, for the recorded fallback snapshot
  (`/etc/grub.d/` script reading grubenv), and use that for both the
  automatic fallback and "boot into snapshot once" from the TV menu.

### Verified: booting a snapshot (QEMU)
Selected `@snapshots/2/snapshot` by hand in GRUB's "snapshots" menu. It booted
the snapshot's own kernel (`/@snapshots/2/snapshot/boot/vmlinuz-linux-lts`),
`/` was the read-only snapshot under a writable tmpfs overlay
(`grub-btrfs-overlayfs`), and the sway session started. The only failed unit
was `systemd-remount-fs` (it tries to apply fstab's root options to the
overlay). That's harmless, but the Phase 6 health page must not report it
as a fault while booted into a snapshot.
- **Mesa llvmpipe under QEMU TCG:** sway crashed in `libgallium` (a jump to a
  null address in JIT-compiled code). That's also the likely cause of foot's
  missing glyphs. In a VM, `tvbox-session` now selects wlroots' pixman
  renderer (`WLR_RENDERER=pixman`). Testing for a GPU render node didn't
  work: virtio-gpu exposes `/dev/dri/renderD128` even without 3D. The real box
  is never a VM and keeps the GLES renderer.
  `/etc/tvbox/session.conf` can override it (e.g. GLES with `QEMU_GL=1`).
- **pango-view segfaults on an unknown output extension** (`foo.png.tmp`).
  The status screen's temporary file is now `*.new.png`.
- **`systemctl restart greetd` lands on the text login:** greetd runs its
  `initial_session` (auto-login) only once per boot. "Restart session" in
  the TV menu (Phase 2) must therefore restart sway through
  `tvbox-session`'s loop (e.g. `swaymsg exit` with a non-zero code), never by
  restarting greetd. The sway crash above confirmed the loop works: sway
  came back by itself.

### Phase 1 status
**Tested in QEMU** (`make qemu-install`, clean install from the ISO, all 17
checks pass): unattended install onto NVMe; btrfs subvolume layout and mounts;
`/boot` inside `@`; linux-lts boots via GRUB; no failed units; greetd
auto-login → sway as `tv`; status screen renders; user session target up;
snapper config; zram; journald limits; suspend disabled; sshd key-only;
`[tvbox]` repo + key configured; pacman creates pre/post snapshots; snapshots
appear in GRUB. Booting a snapshot by hand from GRUB was verified separately.

**Not testable in QEMU, needs the real box:** the interactive installer UI
including Wi-Fi via iwd; UEFI NVRAM behaviour on the real board (the removable
`BOOTX64.EFI` fallback); Intel GPU with the GLES renderer (QEMU uses pixman);
HDMI output, 4K scaling and hotplug; Bluetooth; PipeWire HDMI audio; thermals.

**Open for later phases:** automatic boot fallback needs its own GRUB entry
(Phase 6); `systemd-remount-fs` "fails" when booted into a snapshot (health
page must not flag it); retry output power-on for HDMI hotplug.

## Phase 2 — input daemon, bindings, controller defaults, system menu

### How a button becomes an action
Two layers, as planned. `devices.py` turns raw evdev events into logical
buttons; `engine.py` turns buttons into actions using `bindings.toml`.
- A binding without `long` fires when the button goes **down** (lowest
  latency). A binding with `long` fires its short action on **release** and
  its long action after `long_press_ms` while still held. `repeat` and `long`
  can't be combined on one button; the parser rejects it.
- `key:` actions are taps (down + up at once), not holds. Hold-repeat is done
  by the daemon (`repeat = true`), so the rate is the same in every app and
  does not depend on each client's own key-repeat settings.
- The left stick produces `ls_up`… buttons, which fall back to the d-pad
  bindings unless bound themselves; the right stick produces `rs_*`, unbound
  by default. Sticks are four-way with hysteresis, so a menu never moves
  diagonally or flaps near the threshold.
- When the focused app or the mode changes under a held button, the button
  is cancelled: its repeat stops and its release fires nothing. Otherwise a
  long press that opens the menu would also "press" something in the menu.

### The system menu button can't be configured away
The parser rejects a config in which no `[global]` button opens
`ui:system_menu`, and rejects `[app.*]` sections that rebind such a button.
That is what "always reachable no matter what app is focused" means in
practice; it also protects the phone's bindings editor (Phase 5) from locking
the user out.

### A broken bindings file never takes the controller away
On reload, a file with errors is rejected as a whole and the previous bindings
stay active. At startup, if the user file is broken, the daemon falls back to
`/etc` + defaults, then defaults alone. The errors are in `tvbox-ctl status`,
the journal, and the system menu shows a notice. `tvbox-ctl check [file]`
validates without touching the running daemon.

### Keyboards are left alone (change from the Phase 0 plan)
ARCHITECTURE said a plugged-in keyboard would be read without grabbing it,
with "only explicitly bound global keys intercepted". evdev can't intercept
single keys: either the device is grabbed and everything is re-emitted, or the
app sees every key too, and a bound key would then act twice. So:
- **gamepads** and **remotes** (devices with arrows + OK but no alphabet) are
  grabbed and go through the bindings;
- **keyboards** are not touched by inputd at all. Their way into the system
  menu is sway: `Ctrl+Alt+M` or the Menu key opens it, `Ctrl+Alt+H` goes home,
  and while the menu is open the hub switches sway into a binding mode where
  arrows/Enter/Escape drive the menu instead of the app;
- a keyboard-like device that should behave as a remote (the future ESP32 BLE
  remote presents itself as a full keyboard) gets a `[[device]]` rule in
  bindings.toml: `profile = "remote"`, `grab = true`, optional extra key map.

### The focused app is the sway workspace name
One workspace per app, named after the service id (Phase 3). inputd subscribes
to sway's workspace events itself, so per-app bindings work without the hub.
Matching on window `app_id`/class was rejected: all Chrome instances share
one unless each is started with its own class, and the workspace is already
unique.

### While the overlay is open, nothing reaches the app
In `ui` mode, d-pad/stick/A/B go to the hub as navigation events. Other
buttons only fire `ui:`, `volume:`, `audio:` and `mouse:` actions; `key:` and
`app:` actions are dropped. If the shell or the hub dies while the menu is
open, the hub (or its restarted successor) puts inputd back into the previous
mode, and the hub refuses to open the menu while no shell is connected, so
the controller can't get stuck steering an invisible menu. Both cases are in
the VM test.

### Mouse mode came early
Basic mouse mode (left stick = pointer with a quadratic curve and a speed
ramp, right stick = scroll, A = click) is in Phase 2 instead of Phase 4,
because the menu item would otherwise do nothing and the virtual device has
to declare its pointer capabilities at creation anyway. The cursor is hidden
with sway's `seat * hide_cursor 100` and shown with `hide_cursor 0` in mouse
mode. Phase 4 still owes: tuning on the real TV, right click, drag.

### One overlay window, mapped only when needed
`tvbox-shell` is a GTK4 layer-shell window (overlay layer, anchored to all
edges, keyboard interactivity none, empty input region) showing the hub's
page in WebKitGTK. The page tells the shell when it has something to show
(menu or OSD) and the window is unmapped otherwise, so sway doesn't blend a
transparent fullscreen surface over the video all day and can scan the video
out directly. The page keeps its WebSocket while unmapped.

### Hub listens on loopback only for now, and loopback is not trusted blindly
`127.0.0.1:8080` until the phone remote brings token authentication
(Phase 5). The QEMU port forward to 8080 therefore answers nothing yet; tests
talk to the hub over SSH.

A web page running in one of the box's own browsers can also send requests to
127.0.0.1 (a cross-site POST, or a WebSocket, which no CORS rule stops). The
hub therefore refuses any request whose `Origin` is not its own or whose
`Host` is not `127.0.0.1`/`localhost` (DNS rebinding). Otherwise an ad on a
streaming site could reboot the box. Phase 5 must keep this check when it
adds the LAN listener.

### Volume
`wpctl` on `@DEFAULT_AUDIO_SINK@`, capped at 100 % (`-l 1.0`). Trigger
repeats arrive faster than `wpctl` runs, so the hub sums pending steps and
applies them in one call. Default step is 2 % at 12 Hz (24 %/s) from the
triggers and 5 % per press in the menu. Output list from `pw-dump`.

### Restart session and reboot
"Restart session" creates `$XDG_RUNTIME_DIR/tvbox/restart-session` and tells
sway to exit; `tvbox-session` restarts sway when the flag exists and treats a
clean exit without it as a logout (see Phase 1: greetd logs in automatically
only once per boot). "Reboot" is plain `systemctl reboot` from the hub; logind
allows it for the `tv` user's active session without a polkit rule (verified
in the VM, also with a root SSH session open).

### Packaging
- `tvbox-core` installs the Python code to `/usr/lib/tvbox`, not
  site-packages: that path contains the Python minor version, and an
  `arch=any` package there would break on every Arch Python bump until
  rebuilt. The launchers in `/usr/bin` add the directory to `sys.path`.
- User units are enabled by shipping the
  `tvbox-session.target.wants/` symlinks in the package; no install script.
- `sway` now runs `dbus-update-activation-environment … && systemctl --user
  start tvbox-session.target` as one command, because separate `exec` lines
  run concurrently and the shell needs `WAYLAND_DISPLAY`.
- `xpadneo-dkms` is pinned in `pkgs/aur.list` and built with `--nocheck`
  (its check step wants kernel headers in the builder; DKMS builds the module
  on the box, verified against linux-lts 6.18 in the VM). Without xpadneo the
  kernel's generic driver reports triggers and right stick on different axes;
  the gamepad profile detects that layout too.

### Tried and didn't work (Phase 2)
- **inotify on `~/.config/tvbox` before it exists:** the watch silently
  failed and saving a new user bindings file did nothing. inputd now creates
  the directory at startup.
- **`journalctl --user -M tv@` as root:** "Connecting to a machine as non-root
  is not supported". `tests/qemu/vm.sh tv <cmd>` runs commands as `tv` with
  its runtime dir instead.
- **Passing a command through `ssh host sh -c '…' -- args`:** ssh joins its
  arguments into one string, so the arguments never reach `"$@"`.
  `vm.sh tv` quotes them with `printf %q`.
- **`pacman -U --needed` for pushing dev builds into the VM:** uncommitted
  changes have the same `pkgver`, so nothing was installed. `vm.sh push`
  always reinstalls.
- **Exact screenshot comparison after closing the menu:** the placeholder
  screen redraws its uptime line. The test compares the share of changed
  pixels instead (`tests/qemu/screendiff.py`).
- **`pip install evdev` on the dev host (Linux Mint):** needs Python headers.
  `evdev-binary` has wheels; see README.
- **WebKitGTK in the VM** logs Mesa/Vulkan errors (no GPU) and falls back to
  software rendering. Harmless there; says nothing about the real box.

### Phase 2 status
**Tested in QEMU.** `make qemu-install` (clean install, 21 checks) and
`make qemu-input` (72 checks) pass, from a freshly built ISO. The input test plugs a fake Xbox
controller into the guest through uinput, with the name, IDs and capabilities
the kernel's `xpad` driver reports, and reads what comes out of the virtual
input device:
default mapping from the brief (A, B, Start, bumpers, d-pad, left stick,
triggers, Y, Xbox short/long); hold-repeat; user override and per-app
bindings; reload on save; rejection of a broken file with the old bindings
kept; ui and mouse modes; hot-unplug/replug; daemon killed and restarted;
system menu opened with the controller, volume/mute/app switch/mouse mode
from the menu, nothing typed into the app meanwhile; shell or hub killed with
the menu open; restart session; keyboard path into the menu; the menu
actually visible on screen and gone afterwards. Reboot from the menu and the
xpadneo DKMS build were checked by hand. Idle CPU of all three daemons is 0 %;
the shell with its WebKit processes uses about 300 MB.

**Not testable in QEMU, needs the real box:** a real Xbox controller over USB
(the fake one follows `xpad`, but trigger ranges and the Guide button differ
between controller generations) and over Bluetooth with xpadneo (pairing is
not in the UI yet: `bluetoothctl` over SSH until the settings screen exists);
stick feel, dead zones and mouse-mode speed on a TV; the overlay over real
video with the GLES renderer (transparency, and whether fullscreen video
still gets direct scanout once the overlay is unmapped); HDMI audio volume
and output switching (the VM has one emulated sound card); whether key taps
of zero length are accepted by every app (fine for sway and terminals).

**Left for later phases, deliberately:** Home and Switch app only switch
sway workspaces and Restart app only acts on a `tvbox-app@<id>` unit, both of
which the launcher provides in Phase 3; Settings is a disabled menu entry;
`ui:keyboard` shows a "later version" notice (Phase 4); the phone sends
buttons through the same `button` command the tests use (Phase 5).

### Phase 2 review answers
- **Back:** B stays Escape globally; browser apps get Alt+Left as a per-app
  binding when the launcher defines them (Phase 3).
- **Unbound buttons** (X, stick clicks, right stick outside mouse mode): left
  unbound until real use shows what is missing. Candidate: X = mouse mode
  toggle.
- **Volume:** the box controls its own output volume (triggers, menu, later
  the phone). This is a requirement, not a convenience: it must keep working
  for every output, including Bluetooth.

## Phase 3 — launcher, browser profiles, YouTube TV, Jellyfin client

### Browser: Chromium from [extra], Widevine fetched on the box
Tested in the VM with Chromium 153:
- `youtube.com/tv` serves the TV interface with a webOS smart-TV user agent;
  sign-in offers a QR code / `yt.be/activate` code, so no keyboard is needed.
  Guest mode, playback (720p VP9 in software), Start = play/pause and d-pad
  navigation work with the controller.
- Widevine: Chromium has none. With Google's `WidevineCdm` directory in
  `/var/lib/tvbox/WidevineCdm` and a hint file
  (`<profile>/WidevineCdm/latest-component-updated-widevine-cdm`) pointing at
  it, `requestMediaKeySystemAccess("com.widevine.alpha")` succeeds. No file in
  Chromium's install directory is touched.
- Extensions install through managed policy (`ExtensionInstallForcelist`), and
  Chromium still honours `--load-extension` for our own scripts in Phase 4.

Why not Google Chrome: its terms don't allow redistribution, and `[tvbox]` is
a public repository; Chrome also ignores `--load-extension`, which Phase 4
needs. Why not ship the Widevine module in a package: same redistribution
problem. Instead `tvbox-widevine-update` (root) reads Google's apt index for
Chrome, downloads the `.deb`, checks its SHA-256 against the index, and
extracts only the module. It runs once at first boot and after pacman
upgrades `chromium` (a hook), i.e. never by itself later. If it fails, DRM
services don't play until it succeeds; everything else works.

**Still to verify on hardware:** actual DRM playback on Netflix/Disney+
(needs accounts), and VA-API decoding (`chrome://gpu`, `chrome://media-internals`).
The flags are set (`AcceleratedVideoDecodeLinuxGL`,
`AcceleratedVideoDecodeLinuxZeroCopyGL`, `VaapiIgnoreDriverChecks`), but their
names change between Chromium versions and the VM has no GPU.

### uBlock Origin Lite, by policy
Classic uBlock Origin needs Manifest V2, which Chromium 153 no longer loads.
uBO Lite is force-installed from the Chrome Web Store through
`/etc/chromium/policies/managed/tvbox.json` and updates itself. It applies to
every browser service; a per-service off switch is not there yet (it would be
a hostname list in uBO Lite's managed settings). The same policy file turns
off the password manager, autofill, translate, notifications and metrics.

### One Chromium instance per service
`tvbox-app@<id>.service` runs `tvbox-app <id>`, which builds the command line
from `services.toml`: own `--user-data-dir` under
`~/.local/share/tvbox/profiles/<id>` (logins persist), an app window (`--app=<url>`, originally `--kiosk`, see Phase 5), Wayland, disk cache on tmpfs (`$XDG_RUNTIME_DIR`, 256 MB cap),
`--password-store=basic` (there is no keyring daemon), DevTools on a random
loopback port recorded in the profile. Separate instances cost memory (32 GB
is plenty) and buy isolation: a crashed or wedged Netflix doesn't take
YouTube with it, and per-service flags and user agents are trivial.

### Windows are placed by cgroup, not by app id
The hub listens for new sway windows, reads the window's pid, finds the
`tvbox-app@<id>` unit in `/proc/<pid>/cgroup` and moves the window to
workspace `<id>`. This works for any program without knowing its app id
(Jellyfin, popups a site opens, a service the user added), and also when an
app is slow to start and the user has gone elsewhere meanwhile.

### Switching away
- Leaving a browser service pauses its `<video>`/`<audio>` elements through
  DevTools (`pause = false` in services.toml turns that off). Native apps are
  not paused yet (Jellyfin keeps playing in the background; to be handled
  with its own API or a key in Phase 4/6).
- Apps keep running in the background. When available memory drops under
  1.5 GB, the least recently used background app is stopped (checked on
  every launch and once a minute).
- A crashed app is restarted by systemd in place. An app that exits cleanly
  stays stopped and the hub returns to the home screen when its workspace is
  empty.

### Home screen
A second window of `tvbox-shell` (WebKitGTK, page `/home` from the hub) on
workspace `home`. Unlike the overlay it has keyboard focus, so the
controller's keys arrive as ordinary key events and inputd needs no special
mode. Tiles are text on the service's colour: no third-party logos are
shipped. Settings so far: audio output, volume, display scale, restart
session, reboot, about. Wi-Fi, Bluetooth and updates are visible but
disabled: Wi-Fi passwords need the on-screen keyboard (Phase 4) or the phone
(Phase 5); Bluetooth and updates are Phase 6.

### Jellyfin: native client from the AUR
`jellyfin-desktop` 2.0.0 (the Qt6 successor of Jellyfin Media Player) is
pinned in `pkgs/aur.list` and compiled into `[tvbox]`. `--tv --fullscreen`
starts its TV layout; it runs in the VM up to the server address prompt.
Typing the server address needs a keyboard once (USB keyboard now; on-screen
keyboard or phone later). It is linked against Qt and mpv from [extra], so it
must be rebuilt when those change sonames; CI rebuilds on every push.

### Tried and didn't work (Phase 3)
- **Builder: build dependencies of the first AUR package with dependencies.**
  `pacman` refused to install anything because `[_buildlocal]` had been added
  to pacman.conf without syncing its database. xpadneo had not hit this (no
  build dependencies). `build-packages.sh` now syncs after adding the repo.
- **makepkg's split `-debug` packages** landed in the repo; they are deleted
  after each build.
- **`[hidden]` vs CSS grid with `1fr` columns:** tiles overflowed the screen;
  `minmax(0, 1fr)` fixes it.
- **Hub opening the menu when only the home page was connected:** the hub
  counted any WebSocket client as "the shell". Pages now announce their role
  and the menu needs the overlay page.
- **`$SWAYSOCK` in long-running services after "restart session":** stale.
  `tvbox-display` gets the current socket from the hub; everything else in
  the hub already looked the socket up itself.
- **foot as a stand-in app in tests:** exits with status 1 when its window
  is closed, which systemd rightly treats as a crash. The test services wrap
  it (`sh -c 'foot; true'`).
- **Loading a video by URL into YouTube TV** (`location.href = …/tv#/watch?v=…`)
  reloads the app and lands on the account chooser; setting `location.hash`
  in the running app works. Only relevant for tests.
- **Apps after "restart session":** a browser whose compositor disappears
  exits with an error, so systemd restarted it into the new session, and its
  window appeared before the hub was listening to sway again and stayed on
  the home workspace. Two fixes: "restart session" stops all apps first, and
  the hub places every existing window whenever it (re)connects to sway, which
  also covers a sway crash and a hub restart.
- **Input test pressing A on the home screen:** with the launcher in place,
  Enter there starts YouTube. The input test now works on an empty workspace.

### Phase 3 status
**Tested in QEMU.** `make qemu-install` (clean install from a fresh ISO, 24
checks) and `make qemu-session` (99 checks: input 39, system menu 29,
launcher 24, keyboard and screen 7) pass. The launcher checks use the fake
Xbox pad: launch from the home screen tiles, switch between running services,
windows moved to their service's workspace, crash → restart in place, clean
exit → back to home, Restart app from the menu, services.toml overrides and a
broken file, display scale, and a browser service on a page served inside the
VM (own app id and workspace, user agent, profile and tmpfs cache, controller
keys reaching the page, Widevine available, uBlock Origin Lite installed,
video paused when leaving, still running in the background).

Checked by hand in the VM, with real internet: YouTube's TV interface loads
with the TV user agent; its sign-in screen offers QR/phone-code sign-in; guest
mode, d-pad navigation, video playback and Start = play/pause work; going
home pauses the video. Jellyfin's client starts in TV mode and asks for the
server address. The Widevine module was fetched from Google at first boot.

**Not testable in QEMU, needs the real box (and your accounts):**
- signing in to YouTube with Premium, and that the sign-in survives a reboot;
- Netflix and Disney+ actually playing (Widevine licence exchange), their
  resolution, and whether they accept this Chromium at all;
- VA-API hardware decoding for H.264/VP9/AV1 and dropped frames at 1080p/4K
  (`chrome://gpu`, `chrome://media-internals`, `vainfo`);
- Jellyfin against your server, with mpv using VA-API, and its controller
  navigation;
- voice search on YouTube TV (needs a microphone; none on the controller);
- memory use with all five services alive; HDMI audio.

**Known gaps, by design for now:** no on-screen keyboard yet, so typing
(Jellyfin server address, Netflix/Disney+ login, YouTube text search with a
physical keyboard layout) needs a USB keyboard until Phase 4/5; Netflix and
Disney+ are plain desktop sites until the Phase 4 navigation scripts;
Floatplane points at `floatplane.com/tv`, unverified until Phase 4; native
apps are not paused when switching away; uBlock Origin Lite has no
per-service off switch.

## Phase 4 — navigation scripts, mouse mode, on-screen keyboard

### Floatplane needs nothing special
`floatplane.com/tv` serves its TV interface with Chromium's normal user agent,
and signs in by pairing: it shows a QR code and a code for
`floatplane.com/link`. No user agent override, no navigation script. Whether
its TV interface navigates well with arrow keys after sign-in still needs an
account.

### Navigation for desktop sites: a small extension, not DevTools injection
Netflix and Disney+ (`nav = true` in services.toml) get the `tvnav` extension
loaded unpacked (`--load-extension`; verified that Chromium 153 still allows
it). Alternatives considered:
- *Injecting scripts through DevTools* (`Page.addScriptToEvaluateOnNewDocument`):
  no extension needed, but it only works while the hub holds a DevTools
  session, and an attached debugger with `Runtime` enabled is detectable by
  anti-bot scripts. Not worth the risk on sites that also do DRM.
- *Chromium's own spatial navigation flag* (`--enable-spatial-navigation`):
  moves between links only and knows nothing about dialogs, players or
  `role="button"` elements, which these sites are made of.

`tvnav` is generic: it collects what can be clicked (links, buttons, form
fields, ARIA roles, focusable elements), moves a focus ring with the arrow
keys (elements in line with the current one first, then the nearest one in
that direction), activates with Enter, and keeps the focus inside a dialog or
cookie banner while one is open. Site knowledge is in
`sites/<site>.js`, loaded only on that site's domain: when the site's player
owns the keys (Netflix `/watch`, Disney+ `/play/`, `/video/`), extra
selectors, and things to ignore. A broken site file leaves the generic
behaviour in place, and only on that site.

The site files were written from the sites' public structure and checked only
on their sign-in pages (the focus ring moves, the cookie banner is handled,
focusing the e-mail field opens the on-screen keyboard). **They need tuning
with real accounts:** profile pickers, title rows that scroll sideways, and
the players' controls.

### The extension has a fixed ID and one permission on the hub
The manifest carries a public key, so the extension ID is always
`ecgejpihnmlnjmgnejffnelbhbiehbpm` (no private key exists or is needed for an
unpacked extension). Its background worker may `POST /api/cmd` with exactly
one command, `text_focus`; the hub refuses everything else from that origin
and still refuses all other origins.

### On-screen keyboard
Part of the overlay, so it works over every app. A d-pad grid with layers for
capitals (shift applies to one letter), symbols, and accented letters
(Icelandic á é í ó ú ý þ æ ö ð and their capitals, plus ä å ø ü ß ñ ç è à ê);
X deletes, Start presses Enter and closes, B closes, Y toggles it. A line on
top echoes what was typed in this session, because the field may be hidden
behind the keyboard. It opens with Y anywhere, and by itself when a text field
gets focus in a `nav` site; it closes by itself only if it opened by itself.

How text gets into the app:
- **Browser services: DevTools `Input.insertText`.** Characters arrive as
  text input, independent of the keyboard layout.
- **Everything else: `wtype`**, which types through sway's virtual-keyboard
  protocol with a keymap made on the fly, so any Unicode character works.
- Enter, Backspace and arrows go through inputd's virtual keyboard like any
  bound key.

### Mouse mode
Left stick moves the pointer (quadratic curve plus a speed ramp while held),
right stick scrolls, A is the left button (hold to drag), X the right
button. Speeds are `[mouse] speed` and `scroll_speed` in bindings.toml. The
cursor is shown only in mouse mode (`seat * hide_cursor 0`, otherwise 100 ms).

### Not done: streaming quality (1080p on Netflix/Disney+)
The brief asks for bounded experiments (user agent/platform spoofing,
"1080p" extensions), measured with Netflix's stats overlay. All of them need a
signed-in account and working Widevine playback, which the VM can't give, so
nothing was written blind. This is the first thing to do on the real box
together with you; whatever works becomes a per-service toggle.

### Tried and didn't work (Phase 4)
- **`wtype` for every character:** Chromium dropped characters that are not
  on the current layout (í, þ, …), which `wtype` types by switching the
  virtual keyboard's keymap; terminals and GTK took them. Browsers now get
  text through DevTools. `wtype` also needs a UTF-8 locale to read its
  arguments ("Failed to deencode input argv"); the hub sets `LC_ALL=C.UTF-8`.
- **The shell kept old scripts after an update:** WebKit cached `/static`
  files, so a new `overlay.html` ran with the old `overlay.js` and the
  keyboard never appeared. The hub now sends `Cache-Control: no-cache` for
  everything.
- **The first page after an extension update had no navigation:** Chromium
  re-registers a changed unpacked extension while the start page is already
  loading, so content scripts miss it. The hub checks for the extension's
  marker after a nav service starts and reloads the page once if needed
  (seen and verified in the VM).
- **`scrollIntoView({inline: 'center'})`** scrolled Netflix's sign-in page
  sideways because its cookie banner is wider than the screen; `nearest`
  doesn't.
- **First arrow press on Disney+ landed in the footer** under the cookie
  banner; hence the dialog rule.
- **Checking the cursor with a QMP screendump:** the screenshot doesn't
  include the cursor plane, so cursor visibility in mouse mode is untested in
  the VM.
- **The input test pressed Y and then Start:** with the on-screen keyboard
  real, Y opened it and Start became its Enter key. The test closes the
  keyboard again before testing bindings.

### Phase 4 status
**Tested in QEMU.** From a freshly built ISO: `make qemu-install` passes (25
checks) and `make qemu-session` passes the system menu (29), launcher (24),
navigation/keyboard/mouse (26) and keyboard-and-screen (7) checks. The input
checks (39) failed once in that run because of the test bug above, and passed
when rerun with the fix on the same installed VM. The new checks drive a
desktop-style test page with the fake pad: focus ring, in-line movement,
dialogs keeping the focus, role-only buttons, disabled elements skipped, A
activates once; focusing a text field opens the keyboard; typing letters,
capitals, accented letters, delete, Enter submits and closes; Y/B open and
close it; the extension's narrow access to the hub; mouse mode right click,
drag, and the speed setting. Checked by hand: Netflix's and Disney+'s sign-in
pages (focus ring, cookie banner, keyboard on the e-mail field).

**Needs the real box and your accounts:** Netflix and Disney+ after sign-in
(profile picker, rows, player), Floatplane's TV interface after sign-in,
streaming quality experiments, how the keyboard and the focus ring look on a
TV from the couch, mouse-mode speed and the cursor on the TV.

## Phase 5 — phone remote, pairing, bindings editor, health page

### One server, four kinds of client
The hub now listens on all addresses (port 8080). Every request is classified
before any handler runs (`auth.classify`):
- **TV** — from loopback with a loopback `Host` and no foreign `Origin`: the
  shell's pages and local tools. Unchanged from Phase 2–4, including the
  refusal of web pages running in the box's own browsers.
- **Extension** — our `tvnav` extension, one command (`text_focus`).
- **Unpaired phone** — may load only the pairing link and static files.
- **Paired phone** — must present a device cookie. Its `Origin`, if any, must
  be the box itself as the phone sees it.
Some things stay TV-only even for paired phones: the TV's own pages,
starting a pairing and drawing its QR code. A phone may take only the
`phone` role on the WebSocket, so it can't make the hub believe the overlay
is connected.

### Pairing
Settings → Pair a phone shows a QR code and the same link as text:
`http://<box address>:8080/pair?t=<token>`. The token is random (128 bit),
works once and expires after 5 minutes; a new one is made when it runs out.
Opening the link sets a device token (256 bit) in an `HttpOnly`,
`SameSite=Strict` cookie valid for ten years. The box stores only SHA-256
hashes of device tokens (`~/.local/share/tvbox/devices.json`, mode 600), so
the file can't be used to impersonate a phone. Paired phones are listed with
a "Remove" on the TV and on every phone.

`SameSite=Strict` plus the `Origin` check stop other web sites the phone has
open from driving the box (cross-site requests carry no cookie; a forged
`Origin` is refused). A DNS-rebinding page gets no cookie either, because the
cookie belongs to the box's address.

Limitation: the cookie is bound to the address in the link. If the router
gives the box a different IP, phones must pair again. A DHCP reservation for
the box avoids that. A `tvbox.local` name (mDNS) would survive address
changes but needs Avahi on the box and isn't resolved by every phone; not
done for now.

### The phone sends controller buttons
The d-pad, OK, Back, Home, volume and play/pause buttons send the same
logical buttons a controller does (`button` down/up), so bindings, long
presses (hold Home for the menu) and hold-repeat behave identically. If the
phone's connection drops while a button is held, the hub releases it.
Typing uses the same path as the on-screen keyboard (DevTools for browsers,
`wtype` otherwise). The touchpad sends pointer steps, clicks and scroll steps
straight to inputd's virtual mouse, so it works without switching the TV to
mouse mode.

### Bindings editor
Edits `~/.config/tvbox/bindings.toml` only (the defaults are shown read-only
underneath). The text is validated with the same parser inputd uses, on top
of the defaults and `/etc`, before it is saved; a file with problems is
never written, and "Check" validates without saving. inputd picks the saved
file up through inotify as usual.

### Health page
Collected on request (every 5 s while the page is open, nothing in the
background): `tvbox-*` user units and the important system units with state
and restart counts, restart events from the user journal of the last 7 days,
CPU temperature (`coretemp`, else the package thermal zone), uptime, load,
free disk and memory, and whether the box booted from a snapshot. System
journal entries are not shown: the `tv` user can't read them, by design.

### Tried and didn't work (Phase 5)
- **Checking the WebSocket role after the upgrade:** the 403 came too late,
  the connection was already a WebSocket. The role is now checked before.
- **The QR code from `python-qrcode`'s SVG factory** has no XML
  declaration; fine for `<img>`, only the test assumed otherwise.
- **WebSocket pings to the TV's own pages:** the hub pinged every client
  every 20 s and dropped those that didn't answer within 10 s. WebKit
  suspends the hidden overlay page, so the overlay's connection was reset
  about 30 s after the shell started; a menu opened at that moment closed
  again at once (one failed run of the menu test). Pings are now only for
  phones.

### Phase 5 status
**Tested in QEMU.** From a freshly built ISO, `make qemu-install` passes (26
checks, the new one: the hub answers on the LAN address and refuses an
unpaired client). In that run three menu checks failed because of the
WebSocket ping problem above; with the fix, `make qemu-session` passes on
the same installation: input 39, system menu 29, launcher 24,
navigation/keyboard/mouse 26, keyboard-and-screen 7, and the new phone
checks 32. The phone checks run on the host and reach the VM through QEMU's
port forward, so the box sees them as a LAN client: refused before pairing,
pairing link once only, cookie flags, QR code, what a phone may not do,
launching, typing into an app, buttons, touchpad, a dropped connection
releasing a held button, WebSocket role and authentication, the bindings
editor (refusing, checking, saving and applying), health, and removing a
phone. The phone page was also rendered at phone size with headless
Chromium, and the TV's pairing screen checked on a screenshot.

**Needs a real phone and the real box:** scanning the QR code with an
iPhone and an Android phone, touch feel of the touchpad and the d-pad
(hold-repeat, long press), typing on a phone keyboard with autocorrect, the
CPU temperature sensor on the real board (the VM has none).

**Open question:** whether to add `tvbox.local` (Avahi/mDNS) so a phone
survives the box changing its IP address.

### Found while testing Phase 5 by hand in the VM
- **Netflix and Disney+ showed a black bar on the left with the page cut off
  on the right.** Reproduced on cold starts in a second VM (about 1 in 4
  boots, any service). Chromium kept drawing with the offsets of its first,
  smaller window although it reported itself fullscreen; leaving and
  re-entering fullscreen fixed it, but doing that automatically a few
  seconds after start did not catch every case. Browser services now run as
  Chromium *app windows* (`--app=<url>`, no tabs or address bar; sway makes
  them fullscreen) instead of `--kiosk`: 0 of 6 cold boots showed the bar.
  App windows ignore `--class`; their Wayland app id is
  `chrome-<host>__<path>-Default`, which nothing relies on (windows are
  placed by their systemd unit).
- **The cookie banners could not be controlled with the d-pad.** Both sites
  use OneTrust, whose banner is itself focusable (`tabindex="0"`) and
  focused on load. The focus ring sat on the banner box, and every button
  was "inside the current element" and therefore skipped. Boxes that
  contain other targets are no longer targets; when the site has focused
  such a box, the next arrow press goes inside it; and in a dialog the
  first press lands on its first button in reading order. Verified on both
  sign-in pages: Netflix down, right, OK = "Reject"; Disney+ down, down, OK.
- **YouTube offered at most 1080p.** In the VM, also on a 4K screen (scale
  2), YouTube offers up to 1080p: Chromium reports decoding as supported and
  smooth but not power-efficient (software decoding), and YouTube's TV app
  appears to use that to cap the quality. With VA-API on the real box 4K
  should appear. This is on the hardware checklist, not something the VM can
  show.
