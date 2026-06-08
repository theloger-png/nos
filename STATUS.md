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
  - `setcap cap_net_admin` automated post-install
  - `traceroute` added to apt install list
  - Package installed non-editable (not `pip install -e`)
  - dnsmasq and isc-dhcp-client added to package list
  - `/etc/dnsmasq.d/` permissions fixed for nos group file writes
  - frr group membership added for human user (frr-reload.py access)
- Managed addresses persisted across restarts: `/opt/nos/managed_addresses.json`
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

## Known Limitations / TODO
- Production mode: NOS full control of interfaces (disable netplan) — not yet
- SNMP server: planned for future phase

## Phase 2 — Planned Features
- DHCP server: per-interface/IRB config (range, dns-server, lease-time, gateway auto-detected from IRB address)
- DHCP relay: forward to external server
- NAT: masquerade, SNAT, DNAT, nftables backend
- ACL / firewall filters: JunOS firewall filter syntax, applied per-interface inbound/outbound

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
- Total: 1658 passing, 0 failing - 2026-06-08

## Recent Changes (2026-06-08)
- Fixed: pipe character now works without spaces (`show config irb| display set`)
- Implemented: `show isis` — adjacency, database [detail], interface [name], summary via FRR isisd JSON
