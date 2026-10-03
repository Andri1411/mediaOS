#!/bin/bash
# End-to-end test: unattended install from the ISO onto a fresh QEMU disk,
# then boot the installed system and check it over SSH.
#
#   tests/qemu/install.sh [iso]     (default: newest build/iso/*.iso)
#   SKIP_INSTALL=1 tests/qemu/install.sh   re-run only the boot checks
#
# State (disk, logs, screenshots, ssh key) is kept in $BUILD_DIR/qemu-test.
# Remote commands are single-quoted on purpose (they expand in the VM).
# shellcheck disable=SC2016
# shellcheck source=../../scripts/lib.sh
. "$(dirname "$0")/../../scripts/lib.sh"

state="$BUILD_DIR/qemu-test"
iso=${1:-$(newest_file "$BUILD_DIR"/iso/*.iso || true)}
install_timeout=${INSTALL_TIMEOUT:-5400}
boot_timeout=${BOOT_TIMEOUT:-1200}
ssh_port=${QEMU_SSH_PORT:-2222}
qmp="python3 $ROOT/scripts/qmp.py $state/qmp.sock"

mirror_port=${MIRROR_PORT:-8801}

# Caching Arch mirror on the host: faster repeat runs, and works where the
# guest can't reach the internet directly. Shared by install and boot checks.
python3 "$ROOT/scripts/mirror-cache.py" --port "$mirror_port" --cache "$BUILD_DIR/mirror-cache" \
    2>"$BUILD_DIR/mirror-cache.log" &
mirror_pid=$!
cleanup() { kill "$mirror_pid" 2>/dev/null || true; }
trap cleanup EXIT

export QEMU_STATE_DIR=$state QEMU_DISPLAY=${QEMU_DISPLAY:-none}

wait_qemu_exit() {  # wait_qemu_exit <pid> <timeout>
    local deadline=$((SECONDS + $2))
    while kill -0 "$1" 2>/dev/null; do
        ((SECONDS < deadline)) || { kill "$1"; die "QEMU still running after $2 s"; }
        if grep -qaE 'Shell> |TVBOX-INSTALL: FAILED' "$state/serial.log" 2>/dev/null; then
            kill "$1"; die "boot or install failed (see $state/serial.log)"
        fi
        sleep 5
    done
}

ssh_vm() {
    ssh -q -i "$state/id_ed25519" -p "$ssh_port" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 root@127.0.0.1 "$@"
}

# ------------------------------------------------------------------ install
if [[ ${SKIP_INSTALL:-0} != 1 ]]; then
    [[ -f $iso ]] || die "no ISO; run 'make iso' first"
    rm -rf "$state"; mkdir -p "$state"
    ssh-keygen -q -t ed25519 -N '' -C tvbox-test -f "$state/id_ed25519"
    cat > "$state/autoinstall" <<END
disk=/dev/nvme0n1
hostname=tvbox
timezone=UTC
ssh_key=$(cat "$state/id_ed25519.pub")
mirror=http://10.0.2.2:$mirror_port/\$repo/os/\$arch
END
    log "installing from $iso (timeout ${install_timeout}s)"
    QEMU_AUTOINSTALL="$state/autoinstall" "$ROOT/scripts/qemu.sh" iso "$iso" \
        >"$state/qemu-install.out" 2>&1 &
    pid=$!
    wait_qemu_exit "$pid" "$install_timeout"
    cp "$state/serial.log" "$state/serial-install.log"
    grep -a 'TVBOX-INSTALL:' "$state/serial-install.log" | tr -d '\r' >&2 || true
    grep -qa 'TVBOX-INSTALL: DONE' "$state/serial-install.log" || die "install did not finish"
    log "install finished"
fi

# ------------------------------------------------------------------ boot
log "booting installed system"
"$ROOT/scripts/qemu.sh" disk >"$state/qemu-boot.out" 2>&1 &
pid=$!
trap '$qmp quit 2>/dev/null || kill "$pid" 2>/dev/null || true; cleanup' EXIT

boot_start=$SECONDS
deadline=$((SECONDS + boot_timeout))
until ssh_vm true 2>/dev/null; do
    kill -0 "$pid" 2>/dev/null || die "QEMU exited during boot (see $state/qemu-boot.out)"
    ((SECONDS < deadline)) || die "no SSH after ${boot_timeout}s"
    sleep 10
done
log "SSH up after $((SECONDS - boot_start)) s"

failed=0
check() {  # check <description> <remote command>
    local out
    if out=$(ssh_vm "$2" 2>&1); then
        printf '  \033[32mPASS\033[0m %s\n' "$1" >&2
    else
        printf '  \033[31mFAIL\033[0m %s\n%s\n' "$1" "$out" >&2
        failed=1
    fi
}

# Give the session a moment to come up before checking it.
for _ in $(seq 60); do ssh_vm 'pgrep -u tv -x sway' >/dev/null 2>&1 && break; sleep 5; done

check "root is btrfs subvolume @"   'findmnt -no FSTYPE,OPTIONS / | grep -q "^btrfs .*subvol=/@\(,\|$\)"'
check "all subvolumes mounted"      'for m in /home /.snapshots /var/log /var/cache/pacman/pkg /var/tmp /efi; do findmnt -n "$m" >/dev/null || { echo "missing $m"; exit 1; }; done'
check "/boot is inside @"           '[ "$(findmnt -no SOURCE --target /boot)" = "$(findmnt -no SOURCE /)" ]'
check "linux-lts running"           'uname -r | grep -q lts'
check "no failed units"             'systemctl --failed --no-legend | grep . && exit 1 || true'
check "greetd + sway session as tv" 'pgrep -u tv -x sway'
check "home screen is up"            'for i in $(seq 30); do curl -sf 127.0.0.1:8080/api/state | grep -q "\"ui_clients\": \[\"home\", \"overlay\"\]" && exit 0; sleep 2; done; exit 1'
check "sway responds over IPC"      'sudo -u tv env XDG_RUNTIME_DIR=/run/user/$(id -u tv) sh -c "swaymsg -s \$(ls \$XDG_RUNTIME_DIR/sway-ipc.*.sock | head -1) -t get_outputs" | grep -q "\"active\": true"'
check "user services started"       'systemctl --user -M tv@ is-active tvbox-session.target'
check "snapper config"              'snapper -c root list >/dev/null'
check "zram swap active"            'swapon --show | grep -q zram'
check "journald size limit"         'systemd-analyze cat-config systemd/journald.conf | grep -q SystemMaxUse=64M'
check "suspend disabled"            'systemd-analyze cat-config systemd/sleep.conf | grep -q AllowSuspend=no'
check "sshd password auth off"      'sshd -T | grep -qix "passwordauthentication no"'
check "[tvbox] repo configured"     'pacman-conf -r tvbox >/dev/null && pacman-key --list-keys D605F45D284E377016CC5C3B14BC0D4639884EB7 >/dev/null'
check "pacman makes pre/post snapshots" \
    'sed "/^\[tvbox\]/,/^\$/d" /etc/pacman.conf > /tmp/pacman-notvbox.conf; before=$(snapper -c root --csvout list | wc -l); pacman --config /tmp/pacman-notvbox.conf -Sy --noconfirm tree; after=$(snapper -c root --csvout list | wc -l); [ $((after - before)) -eq 2 ]'
check "snapshots appear in GRUB menu" \
    'for i in $(seq 30); do grep -q "snapshots" /boot/grub/grub-btrfs.cfg 2>/dev/null && exit 0; sleep 2; done; exit 1'

# Phase 2: input layer and system menu (details: tests/qemu/input.sh)
check "inputd, hub and shell running" 'for u in tvbox-inputd tvbox-hub tvbox-shell; do systemctl --user -M tv@ is-active -q $u || { echo "$u not active"; exit 1; }; done'
check "virtual input device present" 'grep -q "tvbox virtual input" /proc/bus/input/devices'
check "xpadneo module built by DKMS" 'dkms status | grep -q "hid-xpadneo.*installed"'
check "system menu opens and closes" \
    'api() { curl -sf -X POST -d "$1" 127.0.0.1:8080/api/cmd >/dev/null; }; state() { curl -sf 127.0.0.1:8080/api/state; };
     for i in $(seq 30); do state | grep -q "\"overlay\"\]" && break; sleep 1; done;
     api "{\"cmd\":\"action\",\"action\":\"ui:system_menu\"}" && sleep 1 && state | grep -q "\"overlay\": \"menu\"" &&
     api "{\"cmd\":\"close\"}" && state | grep -q "\"overlay\": null"'

# Phase 3: launcher, browser, Jellyfin (details: tests/qemu/session.sh)
check "default services on the home screen" \
    'curl -sf 127.0.0.1:8080/api/state | python -c "import json,sys; ids=[s[\"id\"] for s in json.load(sys.stdin)[\"services\"]]; sys.exit(ids != [\"youtube\",\"netflix\",\"disney\",\"floatplane\",\"jellyfin\"])"'
check "browser, policies and Jellyfin client installed" \
    'chromium --version >/dev/null && test -f /etc/chromium/policies/managed/tvbox.json && command -v jellyfin-desktop >/dev/null'
check "Widevine fetch set up" \
    'systemctl is-enabled -q tvbox-widevine.service && { test -f /var/lib/tvbox/WidevineCdm/manifest.json || systemctl is-active tvbox-widevine.service | grep -qE "activating|active"; }'

# Phase 4: navigation extension, on-screen keyboard (details: tests/qemu/session.sh)
check "navigation extension and typing tool installed" \
    'test -f /usr/share/tvbox/extensions/tvnav/manifest.json && command -v wtype >/dev/null'

# Phase 5: the phone remote is reachable from the LAN but only after pairing
check "hub answers the LAN, refuses unpaired clients" \
    'code=$(curl -s -o /dev/null -w "%{http_code}" "http://$(ip -4 -o addr show scope global | awk "{print \$4}" | cut -d/ -f1 | head -1):8080/api/state"); [ "$code" = 401 ]'

$qmp screendump "$state/screen.png" && log "screenshot: $state/screen.png"
((failed == 0)) || die "some checks failed"
log "all checks passed"
