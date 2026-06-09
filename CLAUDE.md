# NOS — Network Operating System

## Project Overview
NOS is a JunOS-like CLI network operating system running on Linux. Always read `NOS_Architecture.md` for full architectural details before implementing any new module.

**Current phase:** Phase 1 — CLI, Config Engine, PFE, basic routing (IS-IS, BGP, static)

## Stack
- **Control plane:** Python 3.12
- **PFE/data plane:** C with XDP/eBPF
- **Routing engine:** FRR (Free Range Routing)

## Code Style
- Python: PEP8, type hints everywhere, pydantic v2 for data models
- Docstrings on all public methods
- **Language:** All code, comments, and documentation (`.md` files) must be in English, regardless of conversation language

## Testing
- Framework: pytest
- All new code must have unit tests in `tests/unit/`
- Run tests before every commit

## Git
- Commit after each logical unit of work
- Use conventional commits: `feat:`, `fix:`, `test:`, `docs:`

## Hard Rules
- **Config:** Never modify `running.json` or `candidate.json` directly — always go through `ConfigStore`
- **FRR:** Never call `vtysh` directly — always use `nos/drivers/frr/client.py`
- **XDP/eBPF:** C code only in `pfe/` directory; Python-side bindings only in `nos/pfe/`

## Do Not Implement Yet
The following are planned for future phases — do not add them now:
- EVPN/VXLAN
- MPLS
- REST API
- Central Controller
- Web UI
- libvirt integration

Also read TODO.md
