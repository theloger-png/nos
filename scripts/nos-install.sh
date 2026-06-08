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
    traceroute \
    nftables \
    dnsmasq \
    isc-dhcp-client
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

# Add human user to frr, frrvty, and nos groups if present
HUMAN_USER="${SUDO_USER:-}"
if [[ "$HUMAN_USER" == "root" ]]; then
    HUMAN_USER=""
fi
if [[ -n "$HUMAN_USER" ]]; then
    info "Adding '${HUMAN_USER}' to frr, frrvty, and nos groups…"
    if getent group frr &>/dev/null; then
        usermod -aG frr "${HUMAN_USER}"
        ok "Added '${HUMAN_USER}' to frr group."
    else
        warn "Group 'frr' not found — install frr first, then re-run this script."
    fi
    if getent group frrvty &>/dev/null; then
        usermod -aG frrvty "${HUMAN_USER}"
        ok "Added '${HUMAN_USER}' to frrvty group."
    else
        warn "Group 'frrvty' not found — install frr first, then re-run this script."
    fi
    if getent group "${NOS_USER}" &>/dev/null; then
        usermod -aG "${NOS_USER}" "${HUMAN_USER}"
        ok "Added '${HUMAN_USER}' to ${NOS_USER} group."
    else
        warn "Group '${NOS_USER}' not found — create nos user first."
    fi
fi

# ── 2c. sudoers rule — FRR daemon management ─────────────────────────────────
# /etc/frr/daemons is owned frr:frr 640; the frr group has read-only access.
# nos-cli needs to write the file and restart FRR when protocols are committed.
# A targeted sudoers rule grants exactly those two operations, nothing more.
info "Installing sudoers rule for FRR daemon management…"

# Determine the human user running the install (via sudo)
HUMAN_USER="${SUDO_USER:-}"
if [[ "$HUMAN_USER" == "root" ]]; then
    HUMAN_USER=""
fi

# Build the sudoers content
read -r -d '' SUDOERS_CONTENT <<'SUDOERS' || true
# Allow the nos service account to update /etc/frr/daemons and /etc/frr/frr.conf and restart FRR.
# These are written by nos/drivers/frr/client.py:FRRClient.sync_daemons().
nos ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/frr/daemons
nos ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/frr/frr.conf
nos ALL=(ALL) NOPASSWD: /bin/systemctl restart frr
SUDOERS

# Add human user if present
if [[ -n "$HUMAN_USER" ]]; then
    SUDOERS_CONTENT+="
# Allow the human user to manage FRR daemons during development.
$HUMAN_USER ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/frr/daemons
$HUMAN_USER ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/frr/frr.conf
$HUMAN_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart frr"
fi

echo "$SUDOERS_CONTENT" > /etc/sudoers.d/nos-frr
chmod 0440 /etc/sudoers.d/nos-frr
ok "Sudoers rule installed at /etc/sudoers.d/nos-frr."
if [[ -n "$HUMAN_USER" ]]; then
    ok "  Added FRR sudoers rules for user '$HUMAN_USER'."
fi

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

# ── 2f. dnsmasq configuration ────────────────────────────────────────────────
info "Configuring dnsmasq for NOS DHCP server…"
install -d -m 0755 /etc/dnsmasq.d
cat > /etc/dnsmasq.d/nos-base.conf <<'DNSMASQ_BASE'
# NOS base dnsmasq config — do not edit; managed by nos-install.sh
no-hosts
no-resolv
port=0
conf-dir=/etc/dnsmasq.d/,*.conf
DNSMASQ_BASE
ok "  Wrote /etc/dnsmasq.d/nos-base.conf."
chown root:nos /etc/dnsmasq.d/
chmod 775 /etc/dnsmasq.d/
ok "dnsmasq configured."

# ── 2g. sudoers rule — dnsmasq management ──────────────────────────────────────
info "Installing sudoers rule for dnsmasq management…"

# Build the sudoers content
read -r -d '' SUDOERS_DNSMASQ <<'SUDOERS_DNSMASQ_EOF' || true
# Allow the nos service account to manage dnsmasq service.
# These operations are needed by DnsmasqDriver in nos/drivers/dhcp/dnsmasq.py.
nos ALL=(ALL) NOPASSWD: /bin/systemctl reload dnsmasq
nos ALL=(ALL) NOPASSWD: /bin/systemctl start dnsmasq
nos ALL=(ALL) NOPASSWD: /bin/systemctl stop dnsmasq
SUDOERS_DNSMASQ_EOF

# Add human user if present
HUMAN_USER="${SUDO_USER:-}"
if [[ "$HUMAN_USER" == "root" ]]; then
    HUMAN_USER=""
fi
if [[ -n "$HUMAN_USER" ]]; then
    SUDOERS_DNSMASQ+="
# Allow the human user to manage dnsmasq during development.
$HUMAN_USER ALL=(ALL) NOPASSWD: /bin/systemctl reload dnsmasq
$HUMAN_USER ALL=(ALL) NOPASSWD: /bin/systemctl start dnsmasq
$HUMAN_USER ALL=(ALL) NOPASSWD: /bin/systemctl stop dnsmasq"
fi

echo "$SUDOERS_DNSMASQ" > /etc/sudoers.d/nos-dnsmasq
chmod 0440 /etc/sudoers.d/nos-dnsmasq

# Validate the sudoers file
if visudo -c -f /etc/sudoers.d/nos-dnsmasq 2>/dev/null; then
    ok "Sudoers rule installed and validated at /etc/sudoers.d/nos-dnsmasq."
    if [[ -n "$HUMAN_USER" ]]; then
        ok "  Added dnsmasq sudoers rules for user '$HUMAN_USER'."
    fi
else
    die "Sudoers file validation failed for /etc/sudoers.d/nos-dnsmasq"
fi

# ── 2h. sudoers rule — dhclient management ────────────────────────────────────
info "Installing sudoers rule for dhclient management…"

# Find the dhclient binary path
DHCLIENT_PATH=$(which dhclient)
if [[ -z "$DHCLIENT_PATH" ]]; then
    die "dhclient not found in PATH"
fi

# Build the sudoers content
read -r -d '' SUDOERS_DHCLIENT <<'SUDOERS_DHCLIENT_EOF' || true
# Allow the nos service account to manage dhclient processes.
# These operations are needed by DnsmasqDriver in nos/drivers/dhcp/dnsmasq.py.
nos ALL=(ALL) NOPASSWD: SUDOERS_DHCLIENT_PATH
nos ALL=(ALL) NOPASSWD: /bin/kill
SUDOERS_DHCLIENT_EOF

# Substitute the actual dhclient path
SUDOERS_DHCLIENT="${SUDOERS_DHCLIENT//SUDOERS_DHCLIENT_PATH/$DHCLIENT_PATH}"

# Add human user if present
HUMAN_USER="${SUDO_USER:-}"
if [[ "$HUMAN_USER" == "root" ]]; then
    HUMAN_USER=""
fi
if [[ -n "$HUMAN_USER" ]]; then
    SUDOERS_DHCLIENT+="
# Allow the human user to manage dhclient during development.
$HUMAN_USER ALL=(ALL) NOPASSWD: $DHCLIENT_PATH
$HUMAN_USER ALL=(ALL) NOPASSWD: /bin/kill"
fi

echo "$SUDOERS_DHCLIENT" > /etc/sudoers.d/nos-dhclient
chmod 0440 /etc/sudoers.d/nos-dhclient

# Validate the sudoers file
if visudo -c -f /etc/sudoers.d/nos-dhclient 2>/dev/null; then
    ok "Sudoers rule installed and validated at /etc/sudoers.d/nos-dhclient."
    ok "  dhclient path: $DHCLIENT_PATH"
    if [[ -n "$HUMAN_USER" ]]; then
        ok "  Added dhclient sudoers rules for user '$HUMAN_USER'."
    fi
else
    die "Sudoers file validation failed for /etc/sudoers.d/nos-dhclient"
fi

# ── 2i. sudoers rule — nft NAT table listing ───────────────────────────────────
info "Installing sudoers rule for nft NAT table listing…"

# Build the sudoers content
read -r -d '' SUDOERS_NFT <<'SUDOERS_NFT_EOF' || true
# Allow the nos service account to manage nftables for NAT.
# These are needed by NatDriver in nos/drivers/kernel/nat.py.
%nos ALL=(root) NOPASSWD: /usr/sbin/nft -f *
%nos ALL=(root) NOPASSWD: /usr/sbin/nft delete table *
nos ALL=(ALL) NOPASSWD: /usr/sbin/nft list table inet nos_nat
SUDOERS_NFT_EOF

# Add human user if present
HUMAN_USER="${SUDO_USER:-}"
if [[ "$HUMAN_USER" == "root" ]]; then
    HUMAN_USER=""
fi
if [[ -n "$HUMAN_USER" ]]; then
    SUDOERS_NFT+="
# Allow the human user to manage nftables during development.
$HUMAN_USER ALL=(root) NOPASSWD: /usr/sbin/nft -f *
$HUMAN_USER ALL=(root) NOPASSWD: /usr/sbin/nft delete table *
$HUMAN_USER ALL=(ALL) NOPASSWD: /usr/sbin/nft list table inet nos_nat"
fi

echo "$SUDOERS_NFT" > /etc/sudoers.d/nos-nft
chmod 0440 /etc/sudoers.d/nos-nft

# Validate the sudoers file
if visudo -c -f /etc/sudoers.d/nos-nft 2>/dev/null; then
    ok "Sudoers rule installed and validated at /etc/sudoers.d/nos-nft."
    if [[ -n "$HUMAN_USER" ]]; then
        ok "  Added nft sudoers rules for user '$HUMAN_USER'."
    fi
else
    die "Sudoers file validation failed for /etc/sudoers.d/nos-nft"
fi

# ── 2j. sudoers rule — user account management ───────────────────────────────
info "Installing sudoers rule for user account management…"

read -r -d '' SUDOERS_USERS <<'SUDOERS_USERS_EOF' || true
# Allow the nos group to manage system user accounts created by NOS configuration.
# These are needed by UserDriver in nos/drivers/kernel/users.py.
%nos ALL=(root) NOPASSWD: /usr/sbin/useradd *
%nos ALL=(root) NOPASSWD: /usr/sbin/usermod *
%nos ALL=(root) NOPASSWD: /usr/sbin/userdel *
%nos ALL=(root) NOPASSWD: /usr/sbin/chpasswd *
SUDOERS_USERS_EOF

# Add human user if present
HUMAN_USER="${SUDO_USER:-}"
if [[ "$HUMAN_USER" == "root" ]]; then
    HUMAN_USER=""
fi
if [[ -n "$HUMAN_USER" ]]; then
    SUDOERS_USERS+="
# Allow the human user to manage user accounts during development.
$HUMAN_USER ALL=(root) NOPASSWD: /usr/sbin/useradd *
$HUMAN_USER ALL=(root) NOPASSWD: /usr/sbin/usermod *
$HUMAN_USER ALL=(root) NOPASSWD: /usr/sbin/userdel *
$HUMAN_USER ALL=(root) NOPASSWD: /usr/sbin/chpasswd *"
fi

echo "$SUDOERS_USERS" > /etc/sudoers.d/nos-users
chmod 0440 /etc/sudoers.d/nos-users

if visudo -c -f /etc/sudoers.d/nos-users 2>/dev/null; then
    ok "Sudoers rule installed and validated at /etc/sudoers.d/nos-users."
    if [[ -n "$HUMAN_USER" ]]; then
        ok "  Added user management sudoers rules for user '$HUMAN_USER'."
    fi
else
    die "Sudoers file validation failed for /etc/sudoers.d/nos-users"
fi

# ── 2k. sudoers rule — SSH configuration management ────────────────────────────
info "Installing sudoers rule for SSH configuration management…"

read -r -d '' SUDOERS_SSH <<'SUDOERS_SSH_EOF' || true
# Allow the nos service account to manage SSH server configuration.
# These are needed by SshDriver in nos/drivers/kernel/ssh.py.
nos ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/ssh/sshd_config.d/nos.conf
nos ALL=(ALL) NOPASSWD: /bin/systemctl reload ssh
nos ALL=(ALL) NOPASSWD: /bin/systemctl reload sshd
SUDOERS_SSH_EOF

# Add human user if present
HUMAN_USER="${SUDO_USER:-}"
if [[ "$HUMAN_USER" == "root" ]]; then
    HUMAN_USER=""
fi
if [[ -n "$HUMAN_USER" ]]; then
    SUDOERS_SSH+="
# Allow the human user to manage SSH during development.
$HUMAN_USER ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/ssh/sshd_config.d/nos.conf
$HUMAN_USER ALL=(ALL) NOPASSWD: /bin/systemctl reload ssh
$HUMAN_USER ALL=(ALL) NOPASSWD: /bin/systemctl reload sshd"
fi

echo "$SUDOERS_SSH" > /etc/sudoers.d/nos-ssh
chmod 0440 /etc/sudoers.d/nos-ssh

if visudo -c -f /etc/sudoers.d/nos-ssh 2>/dev/null; then
    ok "Sudoers rule installed and validated at /etc/sudoers.d/nos-ssh."
    if [[ -n "$HUMAN_USER" ]]; then
        ok "  Added SSH sudoers rules for user '$HUMAN_USER'."
    fi
else
    die "Sudoers file validation failed for /etc/sudoers.d/nos-ssh"
fi

# ── 3. directories ─────────────────────────────────────────────────────────────
info "Creating runtime directories…"
install -d -m 0755 -o root    -g root         /opt/nos
chown root:"${NOS_USER}" /opt/nos
chmod 775 /opt/nos
install -d -m 0750 -o root    -g "${NOS_USER}" "${NOS_CONFDIR}"
install -d -m 0770 -o root    -g "${NOS_USER}" "${NOS_CONFDIR}/rollback"
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

# ── 6a. grant capabilities to Python 3.12 ────────────────────────────────────
# cap_net_admin: manage interfaces / addresses (pyroute2)
# cap_net_raw:   open raw/packet sockets (BFD, ARP probing)
# cap_sys_admin: mount namespaces, bpf() syscall for XDP attach
info "Granting capabilities to /usr/bin/python3.12…"
if [[ -x /usr/bin/python3.12 ]]; then
    setcap cap_net_admin,cap_net_raw,cap_sys_admin+eip /usr/bin/python3.12
    ok "Capabilities granted to /usr/bin/python3.12."
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

# ── 8a. managed addresses file ────────────────────────────────────────────────
info "Creating managed addresses file…"
managed_file="/opt/nos/managed_addresses.json"
if [[ ! -e "${managed_file}" ]]; then
    echo "{}" > "${managed_file}"
    chown root:"${NOS_USER}" "${managed_file}"
    chmod 0664 "${managed_file}"
    ok "  Created ${managed_file} with proper permissions."
else
    # Update permissions on existing file
    chown root:"${NOS_USER}" "${managed_file}"
    chmod 0664 "${managed_file}"
    ok "  Updated permissions on ${managed_file}."
fi

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
systemctl enable dnsmasq
ok "  Enabled dnsmasq."

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
