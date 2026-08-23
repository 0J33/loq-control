#!/usr/bin/env bash
# Install the ojee-loq AGENT — the headless service that exposes this machine's
# hardware over HTTP, for the web module and for ojee-console to mount.
#
#   sudo ./install-agent.sh              install + enable + start
#   sudo ./install-agent.sh --undo       remove everything it installed
#
# This is separate from install.sh, which installs the DESKTOP app for the
# current user and needs no privilege at all. You can run either, or both.
#
# What gets installed, and where:
#
#   /usr/local/lib/ojee-loq/            the agent, root-owned
#   /usr/local/lib/ojee-loq/loq-privhelper
#                                       the ONLY thing granted passwordless
#                                       root; a fixed verb list, every argument
#                                       validated. Root-owned and NOT writable
#                                       by the service user — otherwise the
#                                       agent could rewrite its own helper and
#                                       the whole model collapses.
#   /etc/sudoers.d/ojee-loq             NOPASSWD on that one file only
#   /etc/ojee-loq/agent.env             the token; mode 0640, never in git
#   /etc/systemd/system/ojee-loq.service
#
# The service runs as a normal user, binds to the Tailscale address by default,
# and refuses to start without a token.

set -euo pipefail

LIB=/usr/local/lib/ojee-loq
ETC=/etc/ojee-loq
UNIT=/etc/systemd/system/ojee-loq.service
SUDOERS=/etc/sudoers.d/ojee-loq
SRC="$(cd "$(dirname "$0")" && pwd)"

RUN_USER="${SUDO_USER:-$USER}"

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m+\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31mx\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"

# ── undo ───────────────────────────────────────────────────────────────
if [ "${1:-}" = "--undo" ]; then
  echo
  echo "Removing the ojee-loq agent"
  systemctl disable --now ojee-loq.service 2>/dev/null || true
  rm -f "$UNIT"; ok "unit removed"
  systemctl daemon-reload
  rm -f "$SUDOERS"; ok "sudoers entry removed"
  rm -rf "$LIB";   ok "$LIB removed"
  if [ -d "$ETC" ]; then
    warn "$ETC kept — it holds your token. Remove it by hand if you want it gone."
  fi
  echo
  exit 0
fi

echo
echo "ojee-loq agent"
echo

# ── 1. sanity ──────────────────────────────────────────────────────────
command -v python3 >/dev/null || die "python3 is required"
[ -f "$SRC/agent.py" ] || die "run this from the repo root (agent.py not found)"
id "$RUN_USER" >/dev/null 2>&1 || die "no such user: $RUN_USER"
say "service user: $RUN_USER"

# ── 2. files ───────────────────────────────────────────────────────────
install -d -m 0755 "$LIB" "$LIB/ui" "$LIB/public" "$LIB/public/themes"
install -m 0644 "$SRC"/agent.py "$SRC"/hardware.py "$SRC"/control.py "$SRC"/theme.py "$LIB/"
install -m 0644 "$SRC"/ui/* "$LIB/ui/"
install -m 0644 "$SRC"/public/*.html "$SRC"/public/*.js "$SRC"/public/*.css "$SRC"/public/*.svg "$LIB/public/" 2>/dev/null || true
install -m 0644 "$SRC"/public/themes/*.css "$LIB/public/themes/" 2>/dev/null || true
ok "agent installed to $LIB"

# root:root and 0755 is the point — the service user must not be able to edit
# the file that sudo will run as root.
install -o root -g root -m 0755 "$SRC/loq-privhelper" "$LIB/loq-privhelper"
ok "privileged helper installed (root-owned, not writable by $RUN_USER)"

# ── 3. sudoers ─────────────────────────────────────────────────────────
# visudo -c against a temp file first: a malformed sudoers drop-in can lock
# every user out of sudo on the machine, and that is not a recoverable mistake
# on a laptop you are not sitting in front of.
TMP=$(mktemp)
cat > "$TMP" <<EOF
# ojee-loq — passwordless root for ONE validated helper, nothing else.
# Deliberately not: tee (overwrites any root file), sh -c (a root shell),
# or nvidia-smi (does not cover sysfs, so it never travels alone).
$RUN_USER ALL=(root) NOPASSWD: $LIB/loq-privhelper
EOF
chmod 0440 "$TMP"
if visudo -c -f "$TMP" >/dev/null 2>&1; then
  install -o root -g root -m 0440 "$TMP" "$SUDOERS"
  rm -f "$TMP"
  ok "sudoers entry installed for $RUN_USER"
else
  rm -f "$TMP"
  die "generated sudoers file failed validation — nothing was installed"
fi

# ── 4. token ───────────────────────────────────────────────────────────
install -d -m 0755 "$ETC"
if [ -f "$ETC/agent.env" ]; then
  ok "keeping the existing token in $ETC/agent.env"
else
  TOKEN=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)
  cat > "$ETC/agent.env" <<EOF
# ojee-loq agent configuration.
LOQ_TOKEN=$TOKEN

# Bind address. Defaults to this machine's Tailscale IP, so the agent is not
# reachable from the LAN or from localhost-only tunnels by accident. Set to
# 127.0.0.1 if you front it with a reverse proxy on the same host.
#LOQ_BIND=
LOQ_PORT=8300

# Advanced controls (CPU TDP, GPU clocks, GPU TGP, GPU mode) are OFF by
# default. They can make a machine unstable, and the failure mode is one you
# have to walk over to. Set to 1 to permit them.
LOQ_ALLOW_ADVANCED=0

# Seconds before an unconfirmed advanced change is rolled back.
LOQ_REVERT_SECONDS=15
EOF
  chown root:"$RUN_USER" "$ETC/agent.env"
  chmod 0640 "$ETC/agent.env"
  ok "generated a token in $ETC/agent.env (mode 0640)"
fi

# ── 5. unit ────────────────────────────────────────────────────────────
cat > "$UNIT" <<EOF
[Unit]
Description=ojee-loq hardware agent
Documentation=https://github.com/0J33/ojee-loq
# Tailscale is how this is reached and, by default, what it binds to. Wanting
# it rather than requiring it means a Tailscale restart does not take the
# agent down with it.
Wants=network-online.target tailscaled.service
After=network-online.target tailscaled.service

[Service]
Type=simple
User=$RUN_USER
EnvironmentFile=$ETC/agent.env
# The microphone and touchpad controls talk to the user's session services —
# pactl needs the PulseAudio/PipeWire socket, gsettings needs the session bus.
# A system unit has neither by default, so those two controls silently vanished
# from the UI: capabilities() probes them, finds nothing, and correctly does not
# render a control that cannot work. Pointing at the session's runtime dir gives
# them back.
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u "$RUN_USER")
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u "$RUN_USER")/bus
WorkingDirectory=$LIB
ExecStart=/usr/bin/python3 -u $LIB/agent.py
Restart=on-failure
RestartSec=3

# The agent reads sysfs and forks the helper; it needs no more than that.
NoNewPrivileges=no
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$ETC
ProtectControlGroups=yes
RestrictSUIDSGID=no
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictRealtime=yes

[Install]
WantedBy=multi-user.target
EOF
ok "unit written to $UNIT"

# NoNewPrivileges and RestrictSUIDSGID are deliberately OFF: both would block
# sudo, and sudo to the validated helper is the entire privilege model. Every
# other hardening directive above stays on.

systemctl daemon-reload
systemctl enable --now ojee-loq.service

echo
sleep 2
if systemctl is-active --quiet ojee-loq.service; then
  ok "ojee-loq is running"
  BIND=$(systemctl show -p MainPID --value ojee-loq.service)
  echo
  say "token:  sudo grep LOQ_TOKEN $ETC/agent.env"
  say "logs:   journalctl -u ojee-loq -f"
  say "check:  curl -H \"Authorization: Bearer \$TOKEN\" http://\$(tailscale ip -4):8300/api/health"
  echo
  say "Mount it in ojee-console by adding this to the console's modules config:"
  echo
  echo "    { \"id\": \"loq\", \"name\": \"LOQ\", \"origin\": \"http://$(hostname):8300\" }"
else
  warn "the service did not come up — journalctl -u ojee-loq -n 30"
  systemctl status ojee-loq.service --no-pager -l | tail -20 || true
fi
echo
