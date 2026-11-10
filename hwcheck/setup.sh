#!/usr/bin/env bash
# hwcheck setup - installs everything needed on Pop!_OS / Ubuntu / Debian
#
# Usage (as a normal user, NOT with sudo):
#     bash setup.sh
#
# After it finishes, just run:   hwcheck

set -euo pipefail

# ---------- helpers ----------
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; C='\033[0;36m'; N='\033[0m'
step() { echo -e "\n${C}==>${N} $*"; }
ok()   { echo -e "${G}  ok${N} $*"; }
warn() { echo -e "${Y}  warn${N} $*"; }
die()  { echo -e "${R}error:${N} $*" >&2; exit 1; }

# ---------- checks ----------
[ "$(id -u)" -ne 0 ] || die "Run this as a normal user (bash setup.sh), not with sudo. It asks for sudo when needed."
command -v apt-get >/dev/null 2>&1 || die "This script needs an apt-based system (Pop!_OS / Ubuntu / Debian)."
command -v sudo    >/dev/null 2>&1 || die "sudo is not installed."

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SRC_DIR/hwcheck.py" ] || die "hwcheck.py not found next to setup.sh ($SRC_DIR)."

INSTALL_DIR="$HOME/.hwcheck"
VENV_DIR="$INSTALL_DIR/venv"
LAUNCHER="/usr/local/bin/hwcheck"

# ---------- 1. system packages ----------
step "Installing system packages (apt)"
sudo apt-get update -y
sudo apt-get install -y \
    python3 python3-venv python3-pip \
    smartmontools dmidecode stress-ng memtester sysbench fio \
    pciutils lm-sensors util-linux
ok "system packages ready"

# ---------- 2. install files + virtualenv ----------
step "Setting up $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
if [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
    cp -f "$SRC_DIR/hwcheck.py" "$INSTALL_DIR/hwcheck.py"
fi
chmod +x "$INSTALL_DIR/hwcheck.py"

if [ ! -x "$VENV_DIR/bin/python" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install --upgrade psutil rich textual
"$VENV_DIR/bin/python" -c "import psutil, rich, textual" \
    || die "Python packages failed to import."
ok "virtualenv ready"

# ---------- 3. temperature sensors ----------
step "Loading temperature sensor modules"
MODS=()
if grep -qi "GenuineIntel" /proc/cpuinfo; then
    CPU_MOD="coretemp"
else
    CPU_MOD="k10temp"
fi
for m in "$CPU_MOD" nct6775; do
    if sudo modprobe "$m" 2>/dev/null; then
        MODS+=("$m")
        ok "loaded $m"
    else
        warn "$m not available on this machine (skipped)"
    fi
done
if [ "${#MODS[@]}" -gt 0 ]; then
    # load them again on every boot
    printf "%s\n" "${MODS[@]}" | sudo tee /etc/modules-load.d/hwcheck-sensors.conf >/dev/null
    ok "will load automatically at boot"
else
    warn "no sensor modules loaded; temperatures may be unavailable"
fi

# ---------- 4. launcher command ----------
step "Creating the 'hwcheck' command"
sudo tee "$LAUNCHER" >/dev/null <<EOF
#!/usr/bin/env bash
# hwcheck launcher (created by setup.sh)
PY="$VENV_DIR/bin/python"
APP="$INSTALL_DIR/hwcheck.py"
if [ "\$(id -u)" -eq 0 ]; then
    exec "\$PY" "\$APP" "\$@"
else
    exec sudo -E "\$PY" "\$APP" "\$@"
fi
EOF
sudo chmod +x "$LAUNCHER"
ok "installed $LAUNCHER"

# ---------- done ----------
echo
echo -e "${G}Setup complete!${N}"
echo "  hwcheck            # open the TUI"
echo "  hwcheck report     # plain text report"
echo "  hwcheck report --bench"
echo "  hwcheck snapshot   # save baseline"
echo "  hwcheck compare    # compare with baseline"
echo "  hwcheck stress     # 60s CPU stress test"
