# tvbox architecture

Status: **Phases 1 to 5 implemented.** Phase 0 review answers are in
[§6](#6-review-outcome-phase-0); where the implementation departed from this
plan, DECISIONS.md says why.

`tvbox` is the working name. It is defined once in `config.mk` (`NAME`) and is
the prefix for packages (`tvbox-*`), the repo (`[tvbox]`), config paths
(`/etc/tvbox/`, `~/.config/tvbox/`) and units (`tvbox-*.service`).

## 1. Goals that drive the design

1. **Couch-only operation.** Every function, including recovery from a bad
   update, has to be reachable with a controller or the phone. Anything that
   needs a keyboard is a bug or a documented emergency path.
2. **Everything is a package.** The ISO and installer only partition, pacstrap
   a meta package and write a handful of machine-specific files. All behaviour
   (configs, daemons, UI, browser setup) lives in packages from our own
   `[tvbox]` repo, so `pacman -Syu` updates it and snapper can roll it back.
3. **Appliance reliability.** Always on, no suspend, survives the TV being
   switched off, restarts anything that crashes, keeps writes low, never
   updates by itself.
4. **Customizable, not locked down.** Root stays read-write; user overrides
   live in `/etc/tvbox/` and `~/.config/tvbox/` and take precedence over the
   package defaults in `/usr/share/tvbox/`.

## 2. Big picture

```
                          ┌─────────────────── tv user (systemd --user) ───────────────────┐
 Xbox pad (USB/BT) ─┐     │                                                                │
 BLE remote (HID)  ─┼─evdev─► tvbox-inputd ──uinput──► (virtual kbd+mouse) ──► focused app  │
 USB keyboard ──────┘     │      │  ▲    │                                                 │
                          │      │  │    └── sway IPC (focus → per-app bindings,          │
                          │      │  │                  mouse mode, workspace switch)       │
                          │  input.sock (JSON lines)                                       │
                          │      ▼  │                                                      │
 Phone (LAN, token) ─HTTP/WS─► tvbox-hub ──sway IPC──► sway (kiosk compositor)             │
                          │   (aiohttp)  ├─ systemctl --user ─► tvbox-app@<svc>.service    │
                          │      │  ▲    │                       (Chrome/Jellyfin client)  │
                          │      │  │    ├─ CDP (127.0.0.1) ─► app health, text focus, media│
                          │      │  │    ├─ D-Bus ─► NetworkManager, BlueZ, logind, systemd│
                          │      │  │    └─ wpctl/pw-dump ─► PipeWire/WirePlumber          │
                          │      WS │                                                      │
                          │      ▼  │                                                      │
                          │  tvbox-shell (GTK4 + WebKitGTK)                                │
                          │    ├─ home screen  (normal fullscreen window, workspace "home")│
                          │    └─ overlay      (layer-shell: system menu, OSK, OSD)        │
                          └──────────────────────────────┬─────────────────────────────────┘
                                                         │ /run/tvbox/updater.sock
                                          tvbox-updater (root, socket-activated)
                                          pacman · snapper · grub-editenv
```

One web UI codebase (`src/web/`) is served by `tvbox-hub` and rendered in three
places: the home screen, the overlay and the phone. Only the layout differs
(TV 10-foot vs phone touch).

## 3. Components

### 3.1 Installer ISO (`iso/`, Phase 1)

archiso profile derived from `releng`, plus:

- the `[tvbox]` repo embedded at `/opt/tvbox/repo` (installs work without our
  repo being online; Arch packages still come from mirrors);
- `tvbox-install`, a `dialog` TUI that auto-starts on tty1: **pick disk →
  connect Wi-Fi (skipped if Ethernet is up) → confirm → install → reboot.**
  It refuses disks that are mounted or hold the live medium.

What the installer does (and nothing else):

1. GPT: 1 GiB ESP (FAT32, mounted at `/efi`), rest btrfs.
2. btrfs subvolumes, mounted `noatime,compress=zstd:1`:

   | subvolume                | mount point              | why separate                          |
   |--------------------------|--------------------------|---------------------------------------|
   | `@`                      | `/`                      | snapshotted and rolled back           |
   | `@home`                  | `/home`                  | logins/profiles survive rollback      |
   | `@snapshots`             | `/.snapshots`            | snapper; must not be inside `@`       |
   | `@var_log`               | `/var/log`               | logs survive rollback (debugging)     |
   | `@var_cache_pacman_pkg`  | `/var/cache/pacman/pkg`  | don't snapshot package cache          |
   | `@var_tmp`               | `/var/tmp`               | junk, not snapshotted                 |

   **`/boot` stays inside `@`**, so kernel + initramfs are snapshotted together
   with `/usr/lib/modules`. Booting an old snapshot therefore boots the kernel
   that matches its modules. Only GRUB's own EFI binary lives on the ESP.
3. `pacstrap` `base linux-lts linux-firmware intel-ucode tvbox-base`.
4. Machine-specific files only: fstab, hostname, locale/timezone (defaults,
   changeable later), the `[tvbox]` stanza in `/etc/pacman.conf`, Wi-Fi
   profile copied to NetworkManager, the `tv` user, `grub-install`.

### 3.2 Packages (`pkgs/`)

| package               | contents                                                                                       |
|-----------------------|------------------------------------------------------------------------------------------------|
| `tvbox-release`       | version file, `/etc/pacman.d/tvbox-mirrorlist` (Phase 0 placeholder, exists now)               |
| `tvbox-keyring`       | repo signing key for pacman-key (once signing is decided)                                      |
| `tvbox-base`          | meta package: depends on everything below + system config as drop-ins (see 3.3)               |
| `tvbox-session`       | greetd autologin, sway kiosk config, `tvbox-session.target`                                    |
| `tvbox-core`          | Python code in `/usr/lib/tvbox`: `tvbox-inputd`, `tvbox-hub`, `tvbox-shell`, `tvbox-ctl`; web UI, default bindings, user units |
| `tvbox-updater`       | root update/rollback service                                                                    |
| `tvbox-browser`       | Chromium dependency, managed policies, Widevine fetcher; later our extensions (nav scripts, quality). Per-service flags live in `tvbox-app` (tvbox-core) |
| third-party (AUR)     | rebuilt and pinned in `pkgs/aur.list`: browser (if from AUR), `xpadneo-dkms`, others as needed |

System configuration is shipped as drop-ins under `/usr/lib/...` (journald,
sysctl, logind, sleep, udev, NetworkManager, tmpfiles, polkit) rather than by
owning files in `/etc`. The user can still override anything in `/etc` and
pacman never produces `.pacnew` conflicts for it.

Our own packages get `pkgver = VERSION.r<commit count>`, so every commit is a
strictly newer version and the box sees it as an update.

### 3.3 Base system configuration (`tvbox-base`, Phase 1)

- **Boot:** GRUB + `grub-btrfs` (snapshot submenu, regenerated by `grub-btrfsd`
  when snapshots change) + `grub-btrfs-overlayfs` initramfs hook so a read-only
  snapshot boots with a tmpfs overlay. Rationale and the controller problem:
  §5.
- **Snapshots:** `snapper` config for `/` only, timeline **off**, `number`
  cleanup keeping ~5 pre/post pairs, `snap-pac` for pre/post on every pacman
  transaction. `@home` is not snapshotted (browser profiles churn).
- **Rollback:** `tvbox-rollback <n>` replaces `@` with a writable snapshot of
  snapshot *n* (old `@` kept as `@rollback-<date>` until next cleanup) and
  reboots. Exposed in the UI, never run automatically.
- **Power:** `sleep.conf.d` disables suspend/hibernate; logind ignores lid
  switch and power-key-short-press (a bare laptop board may have a floating lid
  sensor); no swayidle, DPMS never off.
- **Writes:** journald `SystemMaxUse=64M`, `RuntimeMaxUse=32M`; zram swap,
  no swapfile; browser disk caches on tmpfs (`$XDG_RUNTIME_DIR`) with a size
  cap; `noatime`; snapper timeline off.
- **Network:** NetworkManager (iwd backend not needed; default wpa_supplicant
  is fine). **Bluetooth:** BlueZ with `AutoEnable=true`, fast reconnect.
- **SSH:** `sshd` enabled, `PasswordAuthentication no`, root login off.
  `authorized_keys` for `tv` is seeded by the installer (optional prompt,
  e.g. a GitHub username to fetch keys from) and editable from the phone.
- **Input permissions:** udev rule giving the `input` group access to
  `/dev/uinput`; `tv` is in `input`. `xpadneo-dkms` for Bluetooth Xbox pads.
- **Video:** `intel-media-driver`, `LIBVA_DRIVER_NAME=iHD` in the session.
- **Audio:** PipeWire + WirePlumber, rule preferring the HDMI sink by default.

### 3.4 Session (`tvbox-session`, Phase 1)

- `greetd` with `initial_session` autologin of `tv` running `tvbox-session`
  (exports env, `exec sway`). If sway dies, greetd starts it again.
- sway config: no bar, `default_border none`, one **workspace per app**, every
  app window fullscreen, cursor hidden (`seat * hide_cursor` + inputd moves it
  off-screen outside mouse mode).
- Output scale from `~/.config/tvbox/display.toml` (auto: 1 for 1080p, 2 for
  4K), applied live via `swaymsg output`. Overscan: see §5.
- sway's config `exec`s `systemctl --user start tvbox-session.target`, which
  pulls in all user services below.

### 3.5 `tvbox-inputd` (Phase 2) — the most important piece

Python, asyncio, `python-evdev`. Language choice: §5.

**Devices.** Watches `/dev/input` (udev events), classifies each device with a
device profile (match by name / vendor:product / capabilities):

- `gamepad` (Xbox USB via `xpad`, Bluetooth via `xpadneo`) — grabbed
  (`EVIOCGRAB`) so apps only see our synthesized events;
- `remote` (arrows + OK without an alphabet; or any device named by a
  `[[device]]` rule in bindings.toml, which is how the keyboard-like ESP32 BLE
  remote is declared) — grabbed;
- `keyboard` / `mouse` (a real one plugged in for debugging) — left alone
  entirely. Keyboards reach the system menu through sway bindings
  (`Ctrl+Alt+M`, Menu key) and a sway binding mode while the menu is open.
  (Changed in Phase 2, see DECISIONS.md.)

**Two-layer mapping.**

1. *Device profile*: physical code → **logical button** from a shared
   vocabulary: `up down left right ok back home menu play_pause vol_up
   vol_down mute` (remote-like, every device can produce these) plus gamepad
   extras `x y lb rb lt rt view start ls rs` and four-way stick directions
   `ls_up`… / `rs_up`… (the analog position is used in mouse mode).
   Xbox: A→`ok`, B→`back`, Guide→`home`, Start→`start`, …; BLE remote:
   `KEY_ENTER`→`ok`, `KEY_PLAYPAUSE`→`play_pause`, …; phone sends logical
   buttons directly.
2. *Bindings* (TOML): logical button → **action**, with `press` / `long` /
   `repeat`, global and per app.

```toml
# /usr/share/tvbox/bindings.toml (defaults)  <  /etc/tvbox/bindings.toml  <  ~/.config/tvbox/bindings.toml
[timing]
long_press_ms = 500
repeat_delay_ms = 350
repeat_hz = 12

[global]
up    = { press = "key:Up", repeat = true }
ok    = "key:Return"
back  = "key:Escape"
home  = { press = "ui:home", long = "ui:system_menu" }
start = "key:space"                         # play/pause
lb    = "key:Left"                          # seek back
rb    = "key:Right"                         # seek forward
lt    = { press = "volume:-5", repeat = true }
rt    = { press = "volume:+5", repeat = true }
y     = "ui:keyboard"

[app.youtube]                               # app ids come from services.toml
start = "key:k"
back  = "key:Escape"

[app.netflix]
start = "key:space"
```

Action vocabulary: `key:<xkb combo>` (via uinput), `ui:<home|system_menu|
keyboard|app_switcher|back>`, `volume:<±n|mute>`, `audio:next_output`,
`mouse:toggle`, `app:<restart|launch:<id>>`, `none`.

**Modes.** `app` (normal), `ui` (system menu / OSK open: navigation buttons are
sent to the hub as UI events instead of keys) and `mouse` (left stick →
pointer with acceleration, A → click, right stick → wheel). The hub sets the
mode; Guide long press always works in every mode.

**Focus.** inputd subscribes to sway workspace events itself; the focused
workspace's name is the app id (→ per-app bindings). This doesn't go through
the hub, so input keeps working if the hub is restarting.

**Control socket.** `$XDG_RUNTIME_DIR/tvbox/input.sock`, JSON lines; the
protocol is documented at the top of `src/tvbox/inputd.py`. The hub, the
phone remote (through the hub) and `tvbox-ctl` use it.

**Reload.** inotify on the three bindings files; invalid files are rejected
with the error surfaced in the UI, the last good config stays active.

**Output.** One persistent uinput device ("tvbox virtual input": keyboard +
consumer keys + relative pointer + wheel), created once so apps never see a
device appear/disappear.

### 3.6 `tvbox-hub` (Phases 2–6)

Python, aiohttp, `dbus-fast`. The control center:

- **HTTP/WebSocket server** on port 8080. Requests from loopback (home
  screen, overlay) are trusted; requests from the LAN (phone) need a device
  token (see DECISIONS.md, Phase 5, for the exact rules). Serves `src/web/`.
- **App manager.** Services are defined in `services.toml` (same override
  chain as bindings):

  ```toml
  [[service]]
  id = "youtube"
  name = "YouTube"
  kind = "browser"
  url = "https://www.youtube.com/tv"
  user_agent = "tv"          # a name from [user_agents], or a full string
  color = "#e62117"          # tile colour

  [[service]]
  id = "jellyfin"
  name = "Jellyfin"
  kind = "native"
  exec = ["jellyfin-desktop", "--tv", "--fullscreen"]
  ```

  Each running service is a **templated systemd user unit**
  `tvbox-app@<id>.service` (restart policy, logs, cgroup memory accounting for
  free). The hub starts/stops units and moves between sway workspaces. Recently
  used apps stay running in the background; the least recently used one is
  stopped when free memory falls under a threshold. On switching away the
  hub pauses media in the old app.
- **Browser instances.** One user-data-dir per service under
  `~/.local/share/tvbox/profiles/<id>/` (logins persist), Wayland, VA-API
  flags, per-service UA and extensions, `--remote-debugging-port` on
  127.0.0.1 (random, recorded in the runtime dir) for health checks, text-field
  focus detection (auto OSK) and media control.
- **System menu / settings backend:** NetworkManager, BlueZ (pair, trust,
  connect), PipeWire output switching (`wpctl`), display scale, restart
  session, reboot (logind via polkit rule for `tv`), updates (via updater).
- **Watchdog** (Phase 6): unit exit → systemd restarts it; frozen renderer →
  CDP ping timeout or `targetCrashed` → restart unit, return home if needed;
  restarts logged to the journal and a small ring buffer for the health page.
  The hub and inputd themselves use systemd `WatchdogSec` + `sd_notify`.
- **Health:** unit states, restarts, CPU temperature (hwmon `coretemp`),
  uptime, free disk, memory.

### 3.7 `tvbox-shell` (Phases 2–4)

Python, GTK4 + `gtk4-layer-shell` + WebKitGTK 6. One process, two surfaces
rendering the hub's web UI:

- **Home screen**: a normal fullscreen window on workspace `home`. Large tiles,
  D-pad focus navigation, readable at 3 m.
- **Overlay**: a layer-shell surface on the `overlay` layer, so it is above
  fullscreen apps. Hosts the system menu, the on-screen keyboard and the
  volume/app-switch OSD. It never takes keyboard focus: while it is open,
  inputd is in `ui` mode and navigation arrives over the hub's WebSocket. The
  on-screen keyboard's text reaches the focused app through DevTools
  (`Input.insertText`) for browser services and `wtype` (sway's
  virtual-keyboard protocol) for others; Enter/Backspace/arrows go through
  inputd's uinput device. (Changed in Phase 4, see DECISIONS.md.)

Why not use Chromium for these: Chrome windows cannot be layer-shell
surfaces, and keeping the home screen out of the browser means home and the
menu still work when Chrome crashes or is being updated.

### 3.8 `tvbox-updater` (Phase 6)

Root, socket-activated on `/run/tvbox/updater.sock` (group `tv`). Narrow API:

- `check`: `checkupdates` (temporary sync db, no partial-upgrade risk) →
  list of `name old → new`, download size, and flags: *needs reboot* if
  `linux*`, `intel-ucode`, `glibc`, `systemd`, `mesa`, `intel-media-driver`
  or `tvbox-*` session packages change.
- `apply`: `pacman -Syu --noconfirm`, progress streamed to the UI; snap-pac
  takes the pre/post snapshots; the pre snapshot number is stored as the
  boot fallback (§5).
- `rollback <n>`, `snapshots`, `reboot-into <n>` (one-shot `grub-reboot`).

Never runs on a timer. The UI shows the summary first, applies on confirm,
then offers a reboot when flagged.

### 3.9 Phone remote (Phase 5)

Served by the hub on the LAN. Pairing: Settings → "Pair phone" shows a QR code
with `http://<box-ip>:8080/pair?t=<one-time token, 5 min>`. Visiting it
exchanges the token for a long-lived random device token (cookie); paired
devices are listed and revocable in settings. Every LAN request without a
valid token gets 401. Pages: remote (D-pad, back, home, play/pause, volume,
menu, touchpad mouse, type-into-app text field, app switcher), settings and
updates (same components as the TV), bindings editor (server-side validation
with the same parser inputd uses), health.

## 4. Repository layout

```
Makefile, config.mk     build entry points / project-wide settings
scripts/                build + test tooling (run-in-builder, build-*, qemu, qmp)
tools/                  builder container image
pkgs/<name>/PKGBUILD    our packages;  pkgs/aur.list  pinned third-party packages
iso/                    archiso profile + installer            (Phase 1)
src/tvbox/              Python package: inputd, hub, shell, ctl (Phase 2+)
src/web/                shared web UI (home, overlay, phone)   (Phase 2+)
src/extensions/tvnav/   d-pad navigation extension; site rules in sites/<site>.js (Phase 4)
tests/unit/             pytest, runs on any host
tests/qemu/             QEMU end-to-end tests
docs/                   ARCHITECTURE, DECISIONS, USER_GUIDE
```

### Build and test flow

```
make packages   pkgs/* + aur.list → build/pkgs     (Arch builder container)
make repo       build/pkgs → build/repo/tvbox.db   (+ signing when SIGN_KEY set)
make iso        iso/ + build/repo → build/iso/*.iso   (privileged container)
make qemu-iso   boot ISO, UEFI/OVMF, NVMe test disk at build/qemu/disk.qcow2
make qemu-disk  boot the installed test disk
make serve-repo serve build/repo at http://10.0.2.2:8800/repo for the guest,
                so the update flow can be tested against new local builds
make qemu-install / make qemu-session end-to-end tests in the VM
make lint / make qemu-smoke / make test
```

Builds run in an Arch container (`tools/builder.Dockerfile`, docker or podman)
on any Linux host; on an Arch host `CONTAINER=none` runs them natively.

### Testing in QEMU, and what QEMU cannot test

- The VM is q35 + OVMF with an **NVMe** disk (same device naming as the real
  board), virtio-gpu, xHCI with USB keyboard/tablet, user networking with the
  guest's SSH and hub ports forwarded to `127.0.0.1:2222` / `:8080`.
- Scripted interaction via QMP (`scripts/qmp.py`: send-key, type,
  screendump, quit) and the serial log.
- A fake Xbox controller can be created *inside* the guest with uinput
  (same name/capabilities as `xpad`) to test inputd end to end; a real
  controller can be passed through with `QEMU_USB_HOST=045e:<pid>`.
- Not testable in QEMU (hardware phase checklist): VA-API/iHD decoding and
  dropped frames, HDMI hotplug + audio, Bluetooth (controller, headphones),
  Widevine playback quality, thermals.

## 5. Key decisions and trade-offs (details in DECISIONS.md)

**Python for the daemons.** All needed libraries (`python-evdev` incl. uinput,
`python-aiohttp`, `dbus-fast`, `tomllib`, PyGObject/GTK4/WebKitGTK) are in Arch
repos, so packages need no vendoring; one language for inputd, hub and shell
means the bindings parser/validator is literally shared between inputd and the
phone editor. Input latency of an asyncio evdev loop is well under a
millisecond, far below a frame. Rust would use less memory and crash less, but
the daemons are small, event-driven (idle CPU ≈ 0) and restarted by systemd in
under a second. Revisit only if inputd misbehaves on hardware.

**GRUB + grub-btrfs (not Limine).** Both can list snapper snapshots. GRUB wins
on: `grub-btrfs` is in [extra] and maintained alongside snapper/snap-pac;
`grubenv` gives us boot-success flags and one-shot `grub-reboot` that the
updater needs (below); and GRUB reads btrfs, so `/boot` can stay inside `@`
(kernel snapshotted with its modules). Limine needs kernels on the FAT ESP and
an AUR sync tool to copy them per snapshot.

**The controller cannot drive the boot menu.** Neither GRUB nor any bootloader
reads an Xbox controller (it is not a USB HID keyboard; Bluetooth isn't
available at all before the OS). The literal requirement "pick the previous
snapshot from the boot menu with the controller" is not achievable. Proposal:

1. GRUB sets `boot_pending=1` in grubenv each boot; `tvbox-boot-ok.service`
   clears it once the session has been healthy for ~2 minutes.
2. If GRUB starts and `boot_pending` is still set (the last boot never got
   healthy) twice in a row, it boots the snapshot recorded as the update
   fallback automatically, after showing the menu for a while (a keyboard
   still works there). This uses a tvbox-generated top-level GRUB entry with
   a fixed ID, not grub-btrfs's submenu: GRUB can't select entries inside
   grub-btrfs's `configfile` menu unattended (found in Phase 1 testing).
3. Booted into a snapshot, the TV shows "Started from the backup made before
   the update on <date>" with *Keep this (roll back permanently)* / *Try the
   update again*, operable with the controller.
4. Settings → System → Snapshots lets you pick any snapshot and "boot into it
   once" or "roll back to it" — the controller-friendly equivalent of the boot
   menu.

**Overscan.** Most TVs have a "Just Scan"/"Screen Fit" mode that should be
used first. As a fallback, apps are shown as single tiled windows with sway
`gaps outer` instead of true fullscreen, which insets everything; HTML5
fullscreen video still covers the whole output (normal TV behaviour). i915
has no underscan property.

**HDMI hotplug.** When the TV is switched off sway may lose the output and
park workspaces on a fallback output; they return on reconnect. If browsers or
audio misbehave on hardware, the robust fallback is forcing the connector on
with the TV's captured EDID (`drm.edid_firmware=` + `video=HDMI-A-1:e`), so
the kernel never sees a disconnect.

**Browser.** Decided in Phase 3: Chromium + Widevine fetched on the box (see
DECISIONS.md). The candidates were:

- *Google Chrome* (AUR, rebuilt into `[tvbox]`): Widevine built in and
  auto-updating, best chance for VA-API + DRM. Caveat: my understanding is
  that recent branded Chrome ignores `--load-extension`, so our nav scripts
  and uBlock would be force-installed via managed policy
  (`ExtensionInstallForcelist`) from a CRX + update manifest served locally by
  the hub, which Chrome on Linux allows. Needs verification.
- *Chromium* ([extra]) + Widevine CDM copied from Chrome's package: unpacked
  extensions work, but Widevine is a manual hop that can break.

Either way uBlock Origin will be **uBlock Origin Lite** (MV3); classic uBO
needs MV2, which Chromium-based browsers no longer support.

## 6. Review outcome (Phase 0)

Settled:

1. **Name:** `tvbox` stays.
2. **Boot fallback:** automatic fallback to the pre-update snapshot + on-TV
   rollback choice, as in §5.
3. **Kernel:** `linux-lts`.
4. **Phone remote:** plain HTTP on the LAN with token auth.
5. **SSH:** `sshd` enabled by default, key-only (no password login). Keys are
   added at install time or later from the phone settings page.
6. **RAM:** 32 GB. Generous enough to keep all five services alive in the
   background; the LRU eviction threshold stays as a safety net.
7. **Home screen/overlay:** WebKitGTK (`tvbox-shell`, §3.7).
8. **Browser:** Chrome vs Chromium+Widevine decided by testing in Phase 3
   (outcome: Chromium, Widevine downloaded from Google on the box).

9. **Repo hosting and signing:** CI builds, signs and publishes `[tvbox]` to
   GitHub Pages at `https://andri1411.github.io/mediaOS/x86_64`. See
   DECISIONS.md, "Package repository: GitHub Pages, signed in CI".
