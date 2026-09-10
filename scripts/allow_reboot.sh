#!/bin/bash
# allow_reboot.sh — let the assistant reboot the Pi without a password
#
# The voice "reboot" action runs `sudo -n reboot`. Without a rule saying this
# user may reboot without authenticating, that fails with
#     sudo: a password is required
# and it cannot do anything else: the assistant runs as a systemd user service
# with no terminal, so there is nobody to type a password at.
#
# This grants exactly one permission — reboot, for you, no password — and
# nothing else. Run it once, on the Pi, and type your password when sudo asks:
#     bash scripts/allow_reboot.sh

set -euo pipefail

USER_NAME="$(id -un)"

# Resolved, not the bare word "reboot": sudoers matches the full path, and on a
# login shell /sbin is often not even on PATH.
REBOOT_BIN="$(command -v reboot || true)"
if [ -z "$REBOOT_BIN" ]; then
    for candidate in /sbin/reboot /usr/sbin/reboot; do
        [ -x "$candidate" ] && REBOOT_BIN="$candidate" && break
    done
fi
if [ -z "$REBOOT_BIN" ]; then
    echo "Could not find the reboot binary — is this actually the Pi?" >&2
    exit 1
fi

DROP_IN="/etc/sudoers.d/home_automation_reboot"
RULE="$USER_NAME ALL=(root) NOPASSWD: $REBOOT_BIN"

echo "Rule:  $RULE"
echo "File:  $DROP_IN"
echo

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
printf '%s\n' "$RULE" > "$TMP"

# Checked before it goes live, and installed 0440 root:root, because a sudoers
# file that does not parse breaks sudo for everyone — and you cannot sudo your
# way out of that.
if ! sudo visudo -cqf "$TMP"; then
    echo "Refusing to install: that rule does not parse." >&2
    exit 1
fi

sudo install -o root -g root -m 0440 "$TMP" "$DROP_IN"

echo "Installed."
echo
echo "Not tested here on purpose — the only way to prove it works is to"
echo "actually reboot. Ask the assistant to reboot the pi, or check with:"
echo "    sudo -l | grep -i reboot"
