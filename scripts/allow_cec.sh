#!/bin/bash
# allow_cec.sh — let the assistant talk to the TV without a password
#
# Every TV command is a raw CEC frame written to /dev/tegra_cec, which is
# root-owned:
#     printf '\x40\x04' | sudo tee /dev/tegra_cec > /dev/null
#
# src/tv.py runs that as `sudo -n tee /dev/tegra_cec`. Without a rule saying
# this user may do it without authenticating, it fails with
#     sudo: a password is required
# and there is nothing else it could do: the assistant runs as a systemd user
# service with no terminal, so there is nobody to type a password at. The `-n`
# is what turns that into an instant, reportable failure rather than a hang.
#
# The rule grants root-write to *that one file* — not to `tee` in general, and
# emphatically not to a shell. That is the whole reason tv.py pipes into `tee`
# instead of the more obvious `sudo sh -c 'printf ... > /dev/tegra_cec'`:
# sudoers matches arguments, so pinning the path is possible here and would be
# meaningless with a shell in the middle.
#
# Run it once, on the dev kit, and type your password when sudo asks:
#     bash scripts/allow_cec.sh
#
# The alternative, not taken here: a udev rule handing /dev/tegra_cec to the
# `video` group would drop sudo from the path entirely. It also hands the CEC
# bus to every process that user runs, and it has to survive a driver reload,
# so one pinned sudoers line is the smaller thing to own.

set -euo pipefail

USER_NAME="$(id -un)"

CEC_DEVICE="/dev/tegra_cec"

# Resolved, not the bare word "tee": sudoers matches the full path.
TEE_BIN="$(command -v tee || true)"
if [ -z "$TEE_BIN" ]; then
    for candidate in /usr/bin/tee /bin/tee; do
        [ -x "$candidate" ] && TEE_BIN="$candidate" && break
    done
fi
if [ -z "$TEE_BIN" ]; then
    echo "Could not find tee. That should not be possible." >&2
    exit 1
fi

# A warning, not an error: the rule is still correct if the driver simply is
# not loaded yet, and refusing to install it would just mean coming back here.
if [ ! -e "$CEC_DEVICE" ]; then
    echo "Note: $CEC_DEVICE does not exist right now — the tegra_cec driver is"
    echo "      not loaded. Installing the rule anyway; the TV will not answer"
    echo "      until that device shows up."
    echo
fi

DROP_IN="/etc/sudoers.d/home_automation_cec"
RULE="$USER_NAME ALL=(root) NOPASSWD: $TEE_BIN $CEC_DEVICE"

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
echo "Check it, and then test it for real — the second command should turn the"
echo "TV on without ever asking for a password:"
echo "    sudo -l | grep tegra"
echo "    printf '\\x40\\x04' | sudo -n tee $CEC_DEVICE > /dev/null"
