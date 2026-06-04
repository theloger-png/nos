#!/usr/bin/env bash
set -euo pipefail

# ── constants ──────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

NOS_USER=nos
VENV=/opt/nos/venv
NOS_LIBDIR=/usr/lib/nos
NOS_CONFDIR=/opt/nos/config
NOS_STATEDIR=/var/lib/nos
NOS_RUNDIR=/run/nos

# ── helpers ────────────────────────────────────────────────────────────────────
info()  { printf '\e[1;34m[nos-install]\e[0m %s\n' "$*"; }
ok()    { printf '\e[1;32m[nos-install]\e[0m %s\n' "$*"; }
warn()  { printf '\e[1;33m[nos-install]\e[0m %s\n' "$*" >&2; }
die()   { printf '\e[1;31m[nos-install]\e[0m ERROR: %s\n' "$*" >&2; exit 1; }

# ── root check ─────────────────────────────────────────────────────────────────
[[ "${EUID}" -eq 0 ]] || die "This script must be run as root (try: sudo $0)"

# ── 1. system dependencies ─────────────────────────────────────────────────────
info "Installing system packages…"
apt-get update -qq
apt-get install -y --no-install-recommends \
    frr frr-pythontools \
    iproute2 bridge-utils \
    "linux-headers-$(uname -r)" \
    clang llvm libbpf-dev linux-tools-common \
    libmnl-dev libcjson-dev libsystemd-dev \
    python3.12 python3.12-venv python3-pip \
    traceroute
ok "System packages installed."

# ── 2. system user ─────────────────────────────────────────────────────────────
info "Creating system user '${NOS_USER}'…"
if id -u "${NOS_USER}" &>/dev/null; then
    warn "User '${NOS_USER}' already exists — skipping."
else
    useradd --system --no-create-home --shell /usr/sbin/nologin "${NOS_USER}"
    ok "User '${NOS_USER}' created."
fi

# ── 2a. frrvty group membership ───────────────────────────────────────────────
info "Adding '${NOS_USER}' to frrvty group for vtysh access…"
if getent group frrvty &>/dev/null; then
    usermod -aG frrvty "${NOS_USER}"
    ok "Added '${NOS_USER}' to frrvty."
else
    warn "Group 'frrvty' not found — install frr first, then re-run this script."
fi

# ── 2b. frr group membership (read /etc/frr/daemons) ─────────────────────────
info "Adding '${NOS_USER}' to frr group for FRR daemons file access…"
if getent group frr &>/dev/null; then
    usermod -aG frr "${NOS_USER}"
    ok "Added '${NOS_USER}' to frr group."
else
    warn "Group 'frr' not found — install frr first, then re-run this script."
fi

# ── 2c. sudoers rule — FRR daemon management ─────────────────────────────────
# /etc/frr/daemons is owned frr:frr 640; the frr group has read-only access.
# nos-cli needs to write the file and restart FRR when protocols are committed.
# A targeted sudoers rule grants exactly those two operations, nothing more.
info "Installing sudoers rule for FRR daemon management…"
cat > /etc/sudoers.d/nos-frr <<'SUDOERS'
# Allow the nos service account to update /etc/frr/daemons and /etc/frr/frr.conf and restart FRR.
# These are written by nos/drivers/frr/client.py:FRRClient.sync_daemons().
nos ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/frr/daemons
nos ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/frr/frr.conf
nos ALL=(ALL) NOPASSWD: /bin/systemctl restart frr
SUDOERS
chmod 0440 /etc/sudoers.d/nos-frr
ok "Sudoers rule installed at /etc/sudoers.d/nos-frr."

# ── 2d. FRR log file permissions ────────────────────────────────────────────────
info "Fixing FRR log file permissions…"
frr_log="/var/log/frr/frr-reload.log"
if [[ ! -e "${frr_log}" ]]; then
    touch "${frr_log}"
    ok "  Created ${frr_log}."
fi
chown frr:frr "${frr_log}"
chmod 0664 "${frr_log}"
ok "FRR log file permissions fixed."

# ── 2e. FRR runtime directory permissions ───────────────────────────────────────
info "Fixing FRR runtime directory permissions…"
mkdir -p /var/run/frr
chown frr:frr /var/run/frr
chmod 0775 /var/run/frr
ok "FRR runtime directory permissions fixed."

# ── 3. directories ─────────────────────────────────────────────────────────────
info "Creating runtime directories…"
install -d -m 0755 -o root    -g root         /opt/nos
chown root:"${NOS_USER}" /opt/nos
chmod 775 /opt/nos
install -d -m 0750 -o root    -g "${NOS_USER}" "${NOS_CONFDIR}"
install -d -m 0750 -o root    -g "${NOS_USER}" "${NOS_CONFDIR}/rollback"
install -d -m 0750 -o root    -g "${NOS_USER}" "${NOS_LIBDIR}"
install -d -m 0750 -o "${NOS_USER}" -g "${NOS_USER}" "${NOS_STATEDIR}"
install -d "${NOS_RUNDIR}"
chown root:"${NOS_USER}" "${NOS_RUNDIR}"
chmod 775 "${NOS_RUNDIR}"
ok "Directories ready."

# ── tmpfiles.d — recreate /run/nos and /var/run/frr on every reboot ──────────
info "Installing tmpfiles.d config…"
echo "d /run/nos 0775 root ${NOS_USER} - -" > /etc/tmpfiles.d/nos.conf
echo "d /var/run/frr 0775 frr frr - -" >> /etc/tmpfiles.d/nos.conf
ok "tmpfiles.d config installed."

# ── kernel modules — dummy for lo0 loopback interfaces ──────────────────────
info "Configuring kernel modules for loopback (dummy) support…"
grep -qxF 'dummy' /etc/modules-load.d/nos.conf 2>/dev/null \
    || printf 'dummy\n' >> /etc/modules-load.d/nos.conf
modprobe dummy 2>/dev/null || warn "modprobe dummy: module may already be built-in"
ok "dummy module configured (/etc/modules-load.d/nos.conf)."

# ── 4. build PFE ──────────────────────────────────────────────────────────────
info "Building PFE (C/XDP)…"
(cd "${REPO_ROOT}/pfe" && make clean && make)
ok "PFE build complete."

# ── 5. install PFE artifacts ──────────────────────────────────────────────────
info "Installing PFE binary and XDP object…"
install -m 0755 "${REPO_ROOT}/pfe/nos-pfe"          /usr/local/sbin/nos-pfe
install -m 0644 "${REPO_ROOT}/pfe/xdp/xdp_prog.o"   "${NOS_LIBDIR}/xdp_prog.o"
ok "PFE artifacts installed."

# ── 6. Python venv + package ──────────────────────────────────────────────────
info "Creating Python venv at ${VENV}…"
python3.12 -m venv "${VENV}"
ok "Venv created."

info "Installing NOS Python package into venv…"
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet "${REPO_ROOT}"
ok "Python package installed."

# ── 6a. grant cap_net_admin to Python 3.12 ────────────────────────────────────
info "Granting cap_net_admin to /usr/bin/python3.12…"
if [[ -x /usr/bin/python3.12 ]]; then
    setcap cap_net_admin+eip /usr/bin/python3.12
    ok "cap_net_admin granted to /usr/bin/python3.12."
else
    warn "/usr/bin/python3.12 not found — skipping setcap (nos-cli will need root to manage interfaces)."
fi

# ── 7. CLI entry point ────────────────────────────────────────────────────────
info "Installing CLI entry point…"
cat > /usr/local/bin/nos-cli <<'EOF'
#!/usr/bin/env bash
exec /opt/nos/venv/bin/python -m nos.cli.shell "$@"
EOF
chmod 0755 /usr/local/bin/nos-cli
ok "CLI entry point installed at /usr/local/bin/nos-cli."

# ── 8. config defaults ────────────────────────────────────────────────────────
info "Copying default config files to ${NOS_CONFDIR}…"
for src in "${REPO_ROOT}"/config/*.json; do
    dst="${NOS_CONFDIR}/$(basename "${src}")"
    if [[ -e "${dst}" ]]; then
        warn "  ${dst} already exists — skipping."
    else
        install -m 0640 -o root -g "${NOS_USER}" "${src}" "${dst}"
        ok "  Installed ${dst}."
    fi
done

# ── 9. systemd services ───────────────────────────────────────────────────────
info "Installing systemd service units…"
for unit in "${REPO_ROOT}"/systemd/*.service; do
    name="$(basename "${unit}")"
    install -m 0644 "${unit}" "/etc/systemd/system/${name}"
    ok "  Installed /etc/systemd/system/${name}."
done

info "Reloading systemd and enabling services…"
systemctl daemon-reload
for unit in "${REPO_ROOT}"/systemd/*.service; do
    name="$(basename "${unit}")"
    systemctl enable "${name}"
    ok "  Enabled ${name}."
done

# ── summary ───────────────────────────────────────────────────────────────────
cat <<SUMMARY

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  NOS installation complete
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Binary       /usr/local/sbin/nos-pfe
  XDP object   ${NOS_LIBDIR}/xdp_prog.o
  CLI          /usr/local/bin/nos-cli
  Venv         ${VENV}
  Config       ${NOS_CONFDIR}
  State        ${NOS_STATEDIR}
  Run          ${NOS_RUNDIR}

  Start services:
    systemctl start nos-pfe
    systemctl start nos-cli
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SUMMARY
