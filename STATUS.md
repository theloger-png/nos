# NOS Project Status

## Completed - Phase 1

### Config Engine
- Store, schema, validator, diff, commit engine, serializer
- JunOS-style commit/rollback with 50 checkpoints
- Pydantic v2 models for all config sections
- Standardized config path to `/opt/nos/config/`

### CLI Engine
- Shell, parser, completer (operational + configure mode)
- Prefix matching: `sho int` expands to `show interfaces`
- Multi-line paste support
- `ens34.0` shorthand expands to unit subinterface notation
- Pipe support: `match`, `except`, `find`, `count`, `display set` (both modes)
- Pipe chaining: multiple `|` pipes in a single command
- Pipe completion after full pipe segments
- JunOS-style `ping` and `traceroute` options
- Space key: JunOS-style — completes unique prefix, shows options when ambiguous
- Ctrl+X: clears input line (optimized: cancel_completion + reset without history append)
- Interface aliasing: `set system interface-rename` maps physical interfaces to et0/et1 everywhere in nos-cli
- lo0 loopback: dummy interface support, units lo0.0/lo0.1/etc, excluded from interface-rename
- Autocompletion hints: dynamic hints for `<prefix>`, `<neighbor-ip>`, `<interface-name>`, `<vlan-name-or-id>`, `<ip-address>` in operational mode show commands and configure mode
- Multi-value set commands: JunOS-style `set interfaces et1 mtu 9000 unit 101 vlan-id 101` on a single line
- Tab completion for multi-value commands (navigates CONFIG_TREE correctly)
- FormattedText rendering bug fixed (completion hints show plain text, not rich markup)

### Show Commands
- `show interfaces` — live data via pyroute2, IPv6 addresses displayed
- `show interfaces terse` — IPv6 addresses, dotted subinterface names (irb.101)
- `show interfaces description`
- `show vlans` — live VLAN table with attached interfaces
- `show forwarding` — live data from PFE
- `show arp` — with interface and hostname filters
- `show ipv6 neighbors` — with interface filter
- `show ethernet-switching table` — with interface/vlan/summary filters
- `show ethernet-switching interface` — per-interface switching info
- `show ethernet-switching statistics` — per-interface packet counters
- `show ethernet-switching flood` — per-VLAN flood group membership
- `show configuration` — tree format + `| display set` pipe
- `show route` - JunOS format, IPv4/IPv6, brief/detail/terse/hidden, protocol filter, prefix filter
- `show bgp summary` - JunOS format with peer states and prefix counts
- `show bgp neighbor [<ip>]` - detailed neighbor information
- `show isis` — adjacency, database [detail], interface [name], summary (FRR isisd via vtysh JSON)
- `show isis interface` — JunOS format with L/CirID/Level 1 DR/Level 2 DR/L1/L2 Metric columns
- `show isis adjacency` — JunOS format, kernel→NOS name translation
- `show isis database` — JunOS format (level 1/2, Sequence, Lifetime, A/P/OL/AT flags)
- `show isis route` — JunOS format, text parsing (FRR 8.4 no JSON support)
- `show security nat static/source/destination/pool` — NAT configuration display
- `show security nat translations` — active NAT session translations
- `show system login` — local user configuration display
- `show system services ssh` — SSH server configuration display

### Backend Drivers
- Kernel: interfaces, bridge, routes, VRF (pyroute2, never iproute2 CLI directly)
- FRR: client, renderer, IS-IS, BGP
- FRR daemons auto-enabled/disabled based on configured protocols (bgpd, isisd, ospfd)
- Subinterface deletion for physical parents (ens34.101 deletable)
- Physical interface detached from bridge on delete
- `nos-br` bridge deleted when last port is detached

### PFE (Packet Forwarding Engine)
- C process: `main.c`, `fib.c`, `ipc.c`
- XDP program: `xdp_prog.c`, `xdp_loader.c`, `maps.h`
- Python PFE manager: `manager.py`, `fib.py`, `stats.py`, `ipc.py`
- XDP generic mode (virtio-net/vmxnet3 compatible), kernel fallback
- XDP VLAN tag push for access ports via `port_vlan_map`
- Fixed XDP tag-push MAC corruption (overlapping memcpy)
- Bridge MAC unique per VM (derived from physical port MAC)

### Integration / Config Apply
- ConfigApplier: integrated with CommitEngine, applies interfaces/VLANs/routing/protocols on every commit
- Unix socket JSON IPC between Python RE and C PFE
- `apply_svi`: IRB/SVI interfaces with IP addresses applied at commit
- `vlan_add_self` called automatically on SVI apply
- `nos-apply.service`: applies running config at boot
- Fixed: lo0.0 address lost after commit — `apply_interface` was calling `_sync_addresses` with no `family_inet`, wiping unit-managed addresses; now skipped when no address keys present at interface level

### Deployment
- Systemd services: `nos-pfe.service`, `nos-cli.service`, `nos-apply.service`
- Install script: `scripts/nos-install.sh` (Ubuntu 24.04)
  - `setcap cap_net_admin,cap_net_raw,cap_sys_admin+eip` automated post-install
  - `traceroute` added to apt install list
  - Package installed non-editable (not `pip install -e`)
  - dnsmasq and isc-dhcp-client added to package list
  - nftables package added (for NAT backend)
  - `/etc/dnsmasq.d/` permissions fixed for nos group file writes
  - frr group membership added for human user (frr-reload.py access)
  - Sudoers rules added: nos-nft (nftables), nos-users (user management), nos-ssh (SSH config)
  - Ubuntu 24.04 systemd socket activation for SSH disabled automatically
- Managed addresses persisted across restarts: `/opt/nos/managed_addresses.json`
- Managed users persisted across restarts: `/opt/nos/managed_users.json`
- `/run/nos` permissions fixed via systemd `RuntimeDirectoryMode`/`Group` and `CAP_CHOWN`
- `nos-apply` permissions: `UMask=0002`, `/opt/nos` group-writable
- Rollback directory permissions: 770 (group-writable for commit/rollback)

### Interface Statistics
- Per-interface counters collected every 30 seconds by IfaceStatsWriter background thread (nos/pfe/stats.py)
- Stats written atomically to /run/nos/stats.json (mode 0664, nos group) using IF-MIB naming for future SNMP compatibility
- Interface names in stats.json match NOS-cli names (et0/et1 or hardware name if no alias)
- bps/pps calculated as 30-second moving averages, last_flap tracking
- `show interfaces`: Traffic statistics section with bytes, packets, bps, pps, errors, drops
- `show interfaces extensive`: adds last flap timestamp and moving average annotation
- `show interfaces <name>`: filters output to a single interface
- `show interfaces <name> extensive/terse/detail/description`: all variants work correctly
- Tab completion for format keywords after interface name

### DHCP Server and Client
- DHCP server via dnsmasq: per-interface pool configuration with range, gateway, optional dns-server
- dnsmasq config files generated in /etc/dnsmasq.d/nos-<iface>-<pool>.conf
- Interface name translation: NOS names (et1.101) translated to kernel names (ens34.101) in dnsmasq config
- dnsmasq DNS listener disabled (port=0) to avoid conflict with systemd-resolved
- DHCP client: `set interfaces <name> family inet dhcp` starts dhclient via sudo
- Duplicate dhclient prevention via pgrep fallback check
- `show dhcp server leases`, `show dhcp server statistics`, `show dhcp client leases`
- sudoers rules for dnsmasq and dhclient management without password prompt

### NAT (Network Address Translation)
- Static NAT (1:1): `set security nat static rule <name> source <prefix> translated <ip>`
- Source NAT with pool (many-to-few): `set security nat pool <name> address <prefix>`
- Destination NAT (port forwarding): `set security nat destination rule <name> destination port <port> forward-to <ip> port <port>`
- nftables backend via sudo (no password prompt)
- Config serializer: round-trip support for all NAT types
- `show security nat static/source/destination/pool` — display NAT configurations
- `show security nat translations` — active session tracking

### User Management
- Local users: `set system login user <name> class [super-user|operator|read-only]`
- Password hashing: SHA512, never stores plaintext
- Linux user creation/deletion via sudo: useradd/usermod/userdel/chpasswd (no password prompt)
- Managed users tracked in `/opt/nos/managed_users.json` for persistence across restarts
- User class enforcement in CLI permissions (super-user=full, operator=limited, read-only=show only)
- `show system login` — display local user configuration

### SSH Server
- Configuration: `set system services ssh port <1-65535>`, `set system services ssh protocol-version v2`, `set system services ssh root-login [allow|deny|deny-password]`
- SSH config written to `/etc/ssh/sshd_config.d/nos.conf` via sudo
- Ubuntu 24.04 systemd socket activation disabled automatically (prevents conflicts)
- `sshd` reloaded after config changes
- `show system services ssh` — display SSH server configuration

## Known Limitations / TODO
- Production mode: NOS full control of interfaces (disable netplan) — not yet
- SNMP server: planned for future phase

## Known Bugs
- `frr-reload.py` fails with rc=1 when all protocols are deleted (frr.conf retains stale config)
- `test_nat.py` has 2 pre-existing failures (sudo vs bare `nft` command in NatDriver)

## Phase 2 — In Progress

## Phase 2 — Planned Features
- DHCP relay: forward to external server
- NAT masquerade: remaining masquerade feature implementation
- ACL / firewall filters: JunOS firewall filter syntax, applied per-interface inbound/outbound
- TACACS+ authentication: set system tacacs-server, authentication-order
- REST API: HTTP API for automation and integration
- OSPF routing protocol
- LAG/LACP: link aggregation

## Architecture Decisions
- JunOS-like CLI identical syntax
- Python 3.12 control plane, C for PFE/XDP
- FRR as routing engine (zebra, isisd, bgpd, staticd)
- XDP generic for VMs (virtio-net, vmxnet3), fallback to kernel
- Unix socket JSON IPC between Python RE and C PFE
- Pydantic v2 for config schema validation
- pyroute2 for all kernel operations (never iproute2 CLI directly)
- commit/rollback stateful JunOS-style (50 checkpoints)

## Test Count
- Total: 1847 tests collected - 2026-06-09

## Recent Changes (2026-06-09)
- Implemented: NAT engine — static, source, destination with nftables backend and show commands
- Implemented: User management — local users with class-based access control (super-user/operator/read-only)
- Implemented: SSH server configuration (`set system services ssh`) with automatic socket activation disable on Ubuntu 24.04
- Implemented: Enhanced IS-IS show commands — interface, adjacency, database, route with JunOS format and text parsing
- Implemented: Multi-value set commands — JunOS-style `set interfaces et1 mtu 9000 unit 101 vlan-id 101` on single line
- Fixed: FormattedText rendering bug (completion hints now show plain text)
- Fixed: Tab completion for multi-value commands (correct CONFIG_TREE navigation)
- Updated: Install script — setcap extended (cap_net_raw, cap_sys_admin), nftables package, sudoers rules for NAT/users/SSH
