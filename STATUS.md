# NOS Project Status

## Completed - Phase 1

### Config Engine
- Store, schema, validator, diff, commit engine, serializer
- JunOS-style commit/rollback with 50 checkpoints
- Pydantic v2 models for all config sections

### CLI Engine
- Shell, parser, completer (operational + configure mode)
- Prefix matching: `sho int` expands to `show interfaces`
- Multi-line paste support
- `ens34.0` shorthand expands to unit subinterface notation
- Pipe support: `match`, `except`, `find`, `count`, `display set` (both modes)
- JunOS-style `ping` and `traceroute` options

### Show Commands
- `show interfaces` — live data via pyroute2
- `show interfaces terse`
- `show interfaces description`
- `show vlans` — live VLAN table
- `show forwarding` — live data from PFE
- `show arp` — with interface and hostname filters
- `show ipv6 neighbors` — with interface filter
- `show ethernet-switching table` — with interface/vlan/summary filters
- `show configuration` — tree format + `| display set` pipe
- `show route` (stub)
- `show bgp` (stub)
- `show isis` (stub)

### Backend Drivers
- Kernel: interfaces, bridge, routes, VRF (pyroute2, never iproute2 CLI)
- FRR: client, renderer, IS-IS, BGP

### PFE (Packet Forwarding Engine)
- C process: `main.c`, `fib.c`, `ipc.c`
- XDP program: `xdp_prog.c`, `xdp_loader.c`, `maps.h`
- Python PFE manager: `manager.py`, `fib.py`, `stats.py`, `ipc.py`
- XDP generic mode (virtio-net/vmxnet3 compatible), kernel fallback

### Integration
- ConfigApplier: integrated with CommitEngine, applies interfaces/VLANs/routing/protocols on every commit
- Unix socket JSON IPC between Python RE and C PFE

### Deployment
- Systemd services: `nos-pfe.service`, `nos-cli.service`
- Install script: `scripts/nos-install.sh` (Ubuntu 24.04)

## Known Limitations / TODO
- `show vlans`: does not display attached interfaces
- `nos-cli` permissions: `setcap` must be rerun manually after reinstall
- Pipe chaining not yet implemented
- `show ethernet-switching interface/statistics/flood`: not implemented
- Production mode: NOS full control of interfaces (disable netplan) — not yet
- IPv6 neighbors: implemented but not tested live

## Next Steps
1. End-to-end testing on a real VM
2. Fix `show vlans` attached-interface display
3. Automate `setcap` in install script

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
- Total: 1003 tests, all passing (2026-06-02)
