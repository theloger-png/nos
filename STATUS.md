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
- `show bgp` (stub)
- `show isis` (stub)

### Backend Drivers
- Kernel: interfaces, bridge, routes, VRF (pyroute2, never iproute2 CLI directly)
- FRR: client, renderer, IS-IS, BGP
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

### Deployment
- Systemd services: `nos-pfe.service`, `nos-cli.service`, `nos-apply.service`
- Install script: `scripts/nos-install.sh` (Ubuntu 24.04)
  - `setcap cap_net_admin` automated post-install
  - `traceroute` added to apt install list
  - Package installed non-editable (not `pip install -e`)
- Managed addresses persisted across restarts: `/opt/nos/managed_addresses.json`
- `/run/nos` permissions fixed via systemd `RuntimeDirectoryMode`/`Group` and `CAP_CHOWN`
- `nos-apply` permissions: `UMask=0002`, `/opt/nos` group-writable

## Known Limitations / TODO
- Production mode: NOS full control of interfaces (disable netplan) — not yet

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
- Total: 1250 passing, 0 failing - 2026-06-04
