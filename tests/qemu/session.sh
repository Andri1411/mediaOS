#!/bin/bash
# Session tests on the installed test VM. A fake Xbox controller is plugged in
# inside the guest and drives the input daemon (input_test.py), the system
# menu (menu_test.py), the home screen and services (launcher_test.py), and the
# navigation extension and on-screen keyboard (keyboard_test.py); the phone
# remote is tested from the host (phone_test.py).
#
#   tests/qemu/session.sh        needs the disk from `make qemu-install`
#   PUSH=1 tests/qemu/session.sh first install the packages from build/repo and reboot
#   KEEP_VM=1                  leave the VM running afterwards
# shellcheck source=../../scripts/lib.sh
. "$(dirname "$0")/../../scripts/lib.sh"
vm="$ROOT/tests/qemu/vm.sh"

"$vm" up
[[ ${KEEP_VM:-0} == 1 ]] || trap '"$vm" down' EXIT
if [[ ${PUSH:-0} == 1 ]]; then
    "$vm" push
    "$vm" reboot
fi
"$vm" put "$ROOT/tests/qemu/input_test.py" /root/input_test.py
"$vm" put "$ROOT/tests/qemu/menu_test.py" /root/menu_test.py
"$vm" put "$ROOT/tests/qemu/launcher_test.py" /root/launcher_test.py
"$vm" put "$ROOT/tests/qemu/keyboard_test.py" /root/keyboard_test.py
failed=0
log "input checks (fake Xbox pad)"
"$vm" ssh 'cd /root && python input_test.py' || failed=1
log "system menu checks"
"$vm" ssh 'cd /root && python menu_test.py' || failed=1
log "home screen and services checks"
"$vm" ssh 'cd /root && python launcher_test.py' || failed=1
log "navigation extension, on-screen keyboard and mouse checks"
"$vm" ssh 'cd /root && python keyboard_test.py' || failed=1
# From the host, through QEMU's port forward: arrives like a phone on the LAN.
log "phone remote checks"
python3 "$ROOT/tests/qemu/phone_test.py" "${QEMU_HUB_PORT:-8080}" || failed=1
"$vm" ssh 'curl -sf -X POST -d "{\"cmd\":\"home\"}" 127.0.0.1:8080/api/cmd >/dev/null'; sleep 2

# A real (QEMU USB) keyboard: its keys are not handled by inputd, sway's
# bindings open and drive the menu. Also checks that the menu is drawn.
log "keyboard and screen checks"
qmp=(python3 "$ROOT/scripts/qmp.py" "$BUILD_DIR/qemu-test/qmp.sock")
overlay() { "$vm" ssh 'curl -s 127.0.0.1:8080/api/state' | python3 -c 'import json,sys; print(json.load(sys.stdin)["overlay"])'; }
check() {  # check <description> <command...>
    local desc=$1; shift
    if "$@" >/dev/null; then printf '  PASS %s\n' "$desc"; else printf '  FAIL %s\n' "$desc"; failed=1; fi
}
shot="$BUILD_DIR/qemu-test"
"${qmp[@]}" screendump "$shot/menu-closed.ppm"
"${qmp[@]}" screendump "$shot/home.png"
"${qmp[@]}" send-key ctrl-alt-m; sleep 2
check "keyboard: Ctrl+Alt+M opens the menu" test "$(overlay)" = menu
"${qmp[@]}" screendump "$shot/menu-open.ppm"
"${qmp[@]}" screendump "$shot/menu-open.png"
check "the menu is visible on screen" python3 "$ROOT/tests/qemu/screendiff.py" "$shot/menu-closed.ppm" "$shot/menu-open.ppm" --more 0.5
"${qmp[@]}" send-key down down down ret; sleep 2      # Mute
muted() { "$vm" ssh 'curl -s 127.0.0.1:8080/api/state' | grep -q '"muted": true'; }
check "keyboard: arrows and Enter drive the menu" muted
"${qmp[@]}" send-key ret esc; sleep 2
check "keyboard: Escape closes it" test "$(overlay)" = None
"${qmp[@]}" screendump "$shot/menu-after.ppm"
# (not an exact comparison: the placeholder screen's uptime line may change)
check "nothing is left on screen afterwards" python3 "$ROOT/tests/qemu/screendiff.py" "$shot/menu-closed.ppm" "$shot/menu-after.ppm" --less 0.05

"${qmp[@]}" send-key ctrl-alt-m; sleep 2
"${qmp[@]}" send-key up up up ret; sleep 2            # Settings (third from the bottom)
"${qmp[@]}" screendump "$shot/settings.ppm"
"${qmp[@]}" screendump "$shot/settings.png"
check "menu: Settings opens the settings screen" python3 "$ROOT/tests/qemu/screendiff.py" "$shot/menu-closed.ppm" "$shot/settings.ppm" --more 0.2
"${qmp[@]}" send-key esc; sleep 1
"${qmp[@]}" screendump "$shot/home-after.ppm"
check "Back returns to the tiles" python3 "$ROOT/tests/qemu/screendiff.py" "$shot/menu-closed.ppm" "$shot/home-after.ppm" --less 0.05

((failed == 0)) || die "some checks failed"
log "all checks passed"
