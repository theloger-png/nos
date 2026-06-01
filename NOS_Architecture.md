# NOS — Network Operating System
## Architecture Document v0.1
### Faza 1 — Core NOS cu CLI, PFE și Fastpath

---

## 1. Viziune și Scope

Un Network Operating System complet care rulează pe Linux, cu CLI identic JunOS, capabil să funcționeze ca:
- **L3 Switch** — VLANs, SVI/IRB, inter-VLAN routing, STP, EVPN/VXLAN (faza 2+)
- **Router** — IS-IS, BGP, OSPF, VRFs, MPLS/SR (faza 2+)
- **Nod de virtualizare KVM** — integrare pasivă cu libvirt prin port groups (faza 2+)

**Faza 1 scope:**
- CLI local complet (JunOS-like)
- Config Engine cu commit/rollback/compare/commit-confirmed
- Validare în două faze
- IS-IS, BGP de bază, routing static
- VLANs, SVI/IRB, inter-VLAN routing
- PFE cu Forwarding Abstraction Layer
- Fastpath XDP (cu fallback automat la kernel)
- Show commands esențiale
- Funcționează pe VM-uri KVM și VMware

---

## 2. Arhitectura Generală

```
┌─────────────────────────────────────────────────────────┐
│                    Management Plane                     │
│                                                         │
│              CLI Local (JunOS-like)                     │
│         prompt_toolkit, tab completion, ? help          │
└─────────────────────────┬───────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────┐
│                   Config Engine                         │
│                                                         │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ Config Store│  │  Validator   │  │ Commit Engine │  │
│  │ (candidate/ │  │  (phase 1+2) │  │ rollback/diff │  │
│  │  running)   │  │              │  │ confirmed     │  │
│  └─────────────┘  └──────────────┘  └───────────────┘  │
└─────────────────────────┬───────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────┐
│              Forwarding Abstraction Layer               │
│                                                         │
│  ┌────────────┐  ┌────────────┐  ┌──────────────────┐  │
│  │FIB Manager │  │ACL Manager │  │  Tunnel Manager  │  │
│  │            │  │            │  │  (faza 2+)       │  │
│  └────────────┘  └────────────┘  └──────────────────┘  │
│                                                         │
│  ┌────────────────────────────────────────────────────┐ │
│  │            Stats Collector                         │ │
│  └────────────────────────────────────────────────────┘ │
└──────────┬──────────────────────────┬───────────────────┘
           │                          │
┌──────────▼──────────┐  ┌────────────▼──────────────────┐
│   XDP/eBPF Driver   │  │      Kernel Driver            │
│   (fastpath)        │  │      (fallback)               │
│   XDP native/generic│  │      iproute2, bridge         │
└─────────────────────┘  └───────────────────────────────┘
           │                          │
┌──────────▼──────────────────────────▼───────────────────┐
│                  Linux Kernel                           │
│           Network Stack + FRR Daemons                  │
│        (IS-IS, BGP via FRR — control plane only)       │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Stack Tehnologic

| Componentă | Tehnologie | Motivare |
|---|---|---|
| CLI Engine | Python 3.11+ | Rapid de dezvoltat, prompt_toolkit excelent |
| Config Engine | Python 3.11+ | pyroute2, flexibil, ușor de întreținut |
| Validator | Python 3.11+ | Schema declarativă, ușor de extins |
| FRR Integration | Python (vtysh/socket) | FRR are bindings Python, API stabil |
| PFE Core | C | XDP/eBPF necesită C, performanță maximă |
| XDP Programs | C + eBPF | Singurul mod suportat pentru XDP |
| IPC RE↔PFE | Unix socket + JSON | Simplu, debuggable, suficient de rapid |
| Config pe disk | JSON (intern) + set commands (export) | JSON pentru parsing, set commands pentru human-readable |
| Dependințe kernel | iproute2, bridge-utils | Universale pe Linux |
| Routing daemons | FRR | Matur, suportă IS-IS/BGP/OSPF/EVPN/MPLS |

---

## 4. Structura Proiectului

```
nos/
├── nos/                        # Pachetul principal Python
│   ├── __init__.py
│   ├── cli/                    # CLI Engine
│   │   ├── __init__.py
│   │   ├── shell.py            # Main shell loop, prompt_toolkit setup
│   │   ├── completer.py        # Tab completion logic
│   │   ├── parser.py           # Command parser, ierarhie JunOS
│   │   ├── modes/
│   │   │   ├── operational.py  # Modul > (show, ping, traceroute)
│   │   │   └── configure.py    # Modul # (set, delete, edit, commit)
│   │   └── commands/
│   │       ├── show/           # Show commands
│   │       │   ├── interfaces.py
│   │       │   ├── route.py
│   │       │   ├── bgp.py
│   │       │   ├── isis.py
│   │       │   ├── vlan.py
│   │       │   └── system.py
│   │       └── configure/      # Configure commands
│   │           ├── interfaces.py
│   │           ├── protocols.py
│   │           ├── vlans.py
│   │           ├── routing_options.py
│   │           └── system.py
│   │
│   ├── config/                 # Config Engine
│   │   ├── __init__.py
│   │   ├── store.py            # Config Store (candidate + running)
│   │   ├── schema.py           # Schema declarativă de configurație
│   │   ├── validator.py        # Validare faza 1 (sintactic+semantic)
│   │   ├── commit.py           # Commit engine, rollback, compare, confirmed
│   │   ├── diff.py             # Config diff (compare)
│   │   └── serializer.py       # JSON ↔ set commands
│   │
│   ├── drivers/                # Backend Drivers
│   │   ├── __init__.py
│   │   ├── base.py             # Base driver interface
│   │   ├── kernel/             # Kernel driver
│   │   │   ├── __init__.py
│   │   │   ├── interfaces.py   # iproute2 interface management
│   │   │   ├── bridge.py       # bridge/VLAN management
│   │   │   ├── routes.py       # Route management
│   │   │   └── vrf.py          # VRF management
│   │   └── frr/                # FRR driver
│   │       ├── __init__.py
│   │       ├── client.py       # vtysh / FRR socket client
│   │       ├── isis.py         # IS-IS config generation
│   │       ├── bgp.py          # BGP config generation
│   │       └── renderer.py     # Config → FRR format renderer
│   │
│   ├── pfe/                    # Forwarding Abstraction Layer (Python side)
│   │   ├── __init__.py
│   │   ├── manager.py          # PFE manager, auto-detection
│   │   ├── fib.py              # FIB Manager
│   │   ├── acl.py              # ACL Manager
│   │   ├── stats.py            # Stats Collector
│   │   └── ipc.py              # IPC cu PFE C process
│   │
│   └── utils/
│       ├── __init__.py
│       ├── netutils.py         # IP/prefix utilities
│       └── logger.py           # Logging
│
├── pfe/                        # PFE în C
│   ├── main.c                  # PFE process entry point
│   ├── fib.c / fib.h           # FIB table management
│   ├── ipc.c / ipc.h           # Unix socket IPC cu Python
│   ├── xdp/
│   │   ├── xdp_prog.c          # XDP/eBPF forwarding program
│   │   ├── xdp_loader.c        # XDP program loader
│   │   └── maps.h              # eBPF map definitions
│   └── Makefile
│
├── config/
│   ├── running.json            # Running configuration
│   ├── candidate.json          # Candidate configuration
│   └── rollback/               # Rollback checkpoints
│       ├── rollback.0.json
│       ├── rollback.1.json
│       └── ...                 # până la rollback.49.json
│
├── tests/
│   ├── unit/
│   └── integration/
│
├── scripts/
│   └── nos-install.sh          # Install script
│
├── systemd/
│   ├── nos-cli.service
│   └── nos-pfe.service
│
├── requirements.txt
├── setup.py
└── README.md
```

---

## 5. CLI Engine

### 5.1 Moduri de Operare

**Modul Operational** — prompt `username@hostname>`
```
admin@nos01> show interfaces
admin@nos01> show route
admin@nos01> ping 10.0.0.1
admin@nos01> traceroute 10.0.0.1
admin@nos01> configure          ← intră în modul configurare
```

**Modul Configurare** — prompt `username@hostname#`
```
admin@nos01# set interfaces eth0 description "uplink"
admin@nos01# edit interfaces eth0
admin@nos01 (interfaces eth0)# set description "uplink"
admin@nos01 (interfaces eth0)# up
admin@nos01# commit
admin@nos01# exit               ← înapoi la operational
```

### 5.2 Comenzi de bază (identice JunOS)

| Comandă | Descriere |
|---|---|
| `set <path> <value>` | Setează un parametru |
| `delete <path>` | Șterge un parametru sau ramură |
| `edit <path>` | Navighează în ierarhie |
| `up` | Urcă un nivel în ierarhie |
| `top` | Urcă la nivelul root |
| `show` | Afișează configurația candidat curentă |
| `show \| compare` | Diff între candidat și running |
| `commit` | Aplică configurația candidat |
| `commit confirmed <minutes>` | Commit cu rollback automat dacă nu confirmi |
| `commit check` | Validează fără să aplice |
| `rollback <0-49>` | Revert la un checkpoint anterior |
| `discard` | Abandonează modificările din candidat |
| `run <operational command>` | Rulează o comandă operațională din modul configurare |

### 5.3 Pipe Commands
```
show interfaces | match ge-
show route | except 0.0.0.0
show bgp summary | no-more
show interfaces | count
```

### 5.4 Tab Completion și ? Help
- `<Tab>` — completează comanda curentă sau afișează opțiunile
- `?` — afișează help contextual pentru poziția curentă
- `set interfaces ?` — afișează toate interfețele disponibile
- `set interfaces eth0 ?` — afișează toți parametrii disponibili pentru eth0

---

## 6. Config Engine

### 6.1 Config Store

Două configurații în memorie și pe disk:
- **candidate** — modificările curente, nesalvate
- **running** — configurația activă pe sistem

La pornire, running se încarcă din `config/running.json` și se aplică pe sistem.

### 6.2 Rollback Checkpoints

La fiecare commit reușit:
1. running curent → `config/rollback/rollback.0.json`
2. rollback.0 → rollback.1, rollback.1 → rollback.2, etc.
3. Se păstrează maximum 50 de checkpoints (rollback.0 - rollback.49)
4. Noul running = fostul candidat

```
rollback 0    ← configurația de dinaintea ultimului commit
rollback 1    ← cu două commit-uri în urmă
...
rollback 49   ← cel mai vechi checkpoint
```

### 6.3 Commit Confirmed

```
admin@nos01# commit confirmed 5
commit confirmed — will rollback in 5 minutes
commit complete

admin@nos01# commit        ← confirmă înainte de 5 minute
```

Dacă nu se confirmă în intervalul specificat, sistemul face automat `rollback 0`.
Implementat cu un timer în background thread.

### 6.4 Validare în Două Faze

**Faza 1 — Validare locală (instant)**
- Tipuri de date corecte (IP valid, range VLAN 1-4094, AS number 1-4294967295)
- Câmpuri obligatorii prezente
- Constrângeri de coexistență (switchport XOR routed port)
- Referințe valide (VRF există, VLAN există, etc.)

**Faza 2 — Dry run (commit check)**
- Încearcă să aplice în kernel fără să comite
- Verifică că interfețele există
- Verifică că FRR acceptă configurația generată
- Rollback automat dacă dry run eșuează

---

## 7. Schema de Configurație

### 7.1 Ierarhia Principală (JunOS-like)

```
system
    host-name <string>
    domain-name <string>
    name-server <ip>
    ntp server <ip>
    login
        user <name>
            class [super-user | operator | read-only]
            authentication
                plain-text-password <string>
                ssh-rsa <key>
    syslog
        file <name>
            any <level>

interfaces
    <name> (eth0, eth1, bond0, irb, lo, etc.)
        description <string>
        mtu <256-9192>
        speed [auto | 10m | 100m | 1g | 10g | 25g | 40g | 100g]
        duplex [auto | half | full]
        disable                         ← shutdown
        
        # Routed port
        family inet
            address <ip/prefix>
                primary                 ← primary address
        family inet6
            address <ipv6/prefix>
        
        # Switch port
        unit <0>
            family ethernet-switching
                interface-mode [access | trunk]
                vlan
                    members [<vlan-name> | <vlan-id>]    ← access
                    members [<vlan-name> | all]          ← trunk
        
        # MPLS (faza 2+)
        family mpls
        
        # LAG (faza 2+)
        aggregated-ether-options
            lacp
                active
                periodic [slow | fast]

vlans
    <name>
        vlan-id <1-4094>
        description <string>
        l3-interface irb.<vlan-id>      ← SVI pentru L3

routing-options
    static
        route <prefix>
            next-hop <ip>
            discard
            reject
    router-id <ip>
    autonomous-system <asn>

protocols
    isis
        interface <name>
            point-to-point
            level 1 disable             ← only L2
            level 2 disable             ← only L1
            hello-interval <seconds>
            hold-time <seconds>
        level 1
            wide-metrics-only
        level 2
            wide-metrics-only
        
    bgp
        group <name>
            type [internal | external]
            local-as <asn>
            peer-as <asn>              ← pentru eBGP
            local-address <ip>
            neighbor <ip>
                description <string>
                authentication-key <string>
                hold-time <seconds>
            export <policy-name>
            import <policy-name>
            family inet
                unicast
            family inet6
                unicast
            family evpn                 ← faza 2+
                signaling
    
    ospf                                ← faza 2+
        area <id>
            interface <name>
    
    mpls                                ← faza 2+
        interface <name>
    
    ldp                                 ← faza 2+
        interface <name>

policy-options
    prefix-list <name>
        <prefix>
    policy-statement <name>
        term <name>
            from
                prefix-list <name>
                protocol [bgp | isis | ospf | static | direct]
                route-filter <prefix> [exact | longer | orlonger]
            then
                accept
                reject
                next-hop <ip>
                local-preference <0-4294967295>
                metric <value>
                community add <community>

firewall                                ← faza 2+
    filter <name>
        term <name>
            from
                source-address <prefix>
                destination-address <prefix>
                protocol [tcp | udp | icmp]
            then
                accept
                discard
                count <name>

routing-instances
    <name>
        instance-type [vrf | virtual-router]
        interface <name>
        route-distinguisher <rd>
        vrf-target <rt>
        routing-options
            static
                route <prefix> next-hop <ip>
        protocols
            bgp
                group <name>
                    ...
```

### 7.2 Exemple de Configurație

**Switch L3 — configurație simplă:**
```
set system host-name sw01
set interfaces eth0 description "uplink-to-router"
set interfaces eth0 family inet address 10.0.0.1/30
set interfaces eth1 unit 0 family ethernet-switching interface-mode trunk
set interfaces eth1 unit 0 family ethernet-switching vlan members all
set interfaces eth2 unit 0 family ethernet-switching interface-mode access
set interfaces eth2 unit 0 family ethernet-switching vlan members vlan100
set vlans vlan100 vlan-id 100
set vlans vlan100 l3-interface irb.100
set interfaces irb unit 100 family inet address 192.168.100.1/24
set vlans vlan200 vlan-id 200
set vlans vlan200 l3-interface irb.200
set interfaces irb unit 200 family inet address 192.168.200.1/24
set routing-options static route 0.0.0.0/0 next-hop 10.0.0.2
```

**Router — IS-IS + BGP:**
```
set system host-name rtr01
set interfaces eth0 description "to-rtr02"
set interfaces eth0 family inet address 10.1.1.1/30
set interfaces lo0 family inet address 1.1.1.1/32
set routing-options router-id 1.1.1.1
set routing-options autonomous-system 65000
set protocols isis interface eth0 point-to-point
set protocols isis interface lo0
set protocols bgp group IBGP type internal
set protocols bgp group IBGP local-address 1.1.1.1
set protocols bgp group IBGP neighbor 2.2.2.2
```

---

## 8. PFE — Packet Forwarding Engine

### 8.1 Arhitectura PFE

```
┌─────────────────────────────────────────────────┐
│              Python — RE Side                   │
│                                                 │
│  nos/pfe/manager.py                             │
│  ┌──────────────┐  ┌──────────────┐             │
│  │  FIB Manager │  │  ACL Manager │             │
│  └──────┬───────┘  └──────┬───────┘             │
│         │                 │                     │
│  nos/pfe/ipc.py ──────────┘                     │
│  Unix socket client                             │
└─────────────────────┬───────────────────────────┘
                      │ Unix socket
                      │ JSON messages
┌─────────────────────▼───────────────────────────┐
│              C — PFE Process                    │
│                                                 │
│  pfe/main.c                                     │
│  pfe/ipc.c  ← primește comenzi de la RE         │
│  pfe/fib.c  ← menține FIB table                 │
│                                                 │
│  ┌──────────────────────────────────────────┐   │
│  │         XDP Loader (pfe/xdp_loader.c)   │   │
│  │  încarcă programul eBPF pe interfețe    │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│              eBPF Maps (kernel space)           │
│                                                 │
│  fib_map        ← IPv4/IPv6 forwarding table    │
│  neigh_map      ← ARP/ND table (MAC lookup)     │
│  vlan_map       ← VLAN → VNI/interface mapping  │
│  stats_map      ← per-interface counters        │
└─────────────────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│              XDP Program (pfe/xdp/xdp_prog.c)  │
│              rulează în kernel pentru fiecare   │
│              pachet primit                      │
└─────────────────────────────────────────────────┘
```

### 8.2 Auto-Detection Fastpath

La pornire, PFE încearcă în ordine:

1. **XDP native** — cel mai rapid, suportat pe NIC-uri fizice cu drivere moderne
2. **XDP generic** — funcționează pe orice interfață inclusiv virtio-net (KVM), vmxnet3 (VMware)
3. **Kernel fallback** — funcționează întotdeauna, folosit când XDP nu e disponibil

```python
# nos/pfe/manager.py
def detect_forwarding_mode(interface: str) -> ForwardingMode:
    if try_xdp_native(interface):
        return ForwardingMode.XDP_NATIVE
    elif try_xdp_generic(interface):
        return ForwardingMode.XDP_GENERIC
    else:
        return ForwardingMode.KERNEL
```

Output în CLI:
```
admin@nos01> show system forwarding
Interface    Mode          Status
eth0         xdp-generic   active    ← VM cu virtio-net
eth1         xdp-generic   active
lo           kernel        active    ← fallback normal pentru loopback
```

### 8.3 IPC Protocol (RE ↔ PFE)

Mesaje JSON pe Unix socket `/run/nos/pfe.sock`:

```json
// Adaugă rută
{"type": "fib_add", "prefix": "10.0.0.0/24", "nexthop": "10.0.1.1", "ifindex": 2}

// Șterge rută
{"type": "fib_del", "prefix": "10.0.0.0/24"}

// Adaugă entry ARP/ND
{"type": "neigh_add", "ip": "10.0.1.1", "mac": "aa:bb:cc:dd:ee:ff", "ifindex": 2}

// Încarcă XDP pe interfață
{"type": "xdp_attach", "ifindex": 2, "mode": "generic"}

// Citește stats
{"type": "stats_get", "ifindex": 2}
```

---

## 9. FRR Integration

### 9.1 Cum NOS-ul Vorbește cu FRR

NOS-ul nu înlocuiește FRR — îl configurează și îl folosește ca routing engine:

```
Config Engine
      │
      │ la commit, generează fișiere de config FRR
      ▼
/etc/frr/frr.conf (rendered din configurația NOS)
      │
      │ reload sau vtysh
      ▼
FRR Daemons (bgpd, isisd, ospfd, etc.)
      │
      │ instalează rute în kernel via Netlink
      ▼
Kernel FIB
      │
      │ PFE citește FIB via Netlink sau direct din kernel
      ▼
XDP maps (sync)
```

### 9.2 FRR Daemons Folosiți în Faza 1

| Daemon | Scop |
|---|---|
| `zebra` | Obligatoriu, coordonează routing între daemons |
| `isisd` | IS-IS routing |
| `bgpd` | BGP |
| `staticd` | Rute statice |

### 9.3 Netlink Listener

NOS-ul ascultă Netlink events pentru a sincroniza starea cu PFE:

```python
# nos/drivers/kernel/netlink_listener.py
# Ascultă: RTM_NEWROUTE, RTM_DELROUTE, RTM_NEWNEIGH, RTM_DELNEIGH
# La fiecare event, trimite update la PFE prin IPC
```

---

## 10. Show Commands — Output Format

### show interfaces
```
admin@nos01> show interfaces
Physical interface: eth0, Enabled, Physical link is Up
  Description: uplink-to-router
  Link-level type: Ethernet, MTU: 1500, Speed: 1Gbps, Duplex: Full
  Device flags   : Present Running
  Interface flags: SNMP-Traps
  Forwarding mode: xdp-generic

  Logical interface eth0.0
    Flags: Up SNMP-Traps
    Inet  10.0.0.1/30

Physical interface: eth1, Enabled, Physical link is Up
  Link-level type: Ethernet, MTU: 1500
  Forwarding mode: xdp-generic
  
  Logical interface eth1.0 (VLAN trunk)
    Allowed VLANs: 100, 200
```

### show route
```
admin@nos01> show route

inet.0: 5 destinations, 5 routes (5 active, 0 holddown, 0 hidden)

+ = Active Route, - = Last Active, * = Both

10.0.0.0/30         *[Direct/0] 00:10:23
                    > via eth0
10.0.0.1/32         *[Local/0] 00:10:23
                      Local via eth0
192.168.100.0/24    *[Direct/0] 00:05:11
                    > via irb.100
0.0.0.0/0           *[Static/5] 00:10:23
                    > to 10.0.0.2 via eth0
```

### show bgp summary
```
admin@nos01> show bgp summary
BGP summary information for VRF default
Router identifier 1.1.1.1, local AS number 65000

Neighbor        V    AS    MsgRcvd  MsgSent  InQ  OutQ  Up/Down   State/PfxRcd
2.2.2.2         4  65000       145      147    0     0  02:10:05   12
```

### show isis adjacency
```
admin@nos01> show isis adjacency
IS-IS instance: default

Interface   System ID      State  Hold  SNPA
eth0        rtr02.00       Up     27    aabb.ccdd.eeff
```

---

## 11. Faze de Dezvoltare

### Faza 1 — Core NOS (CURRENT)
- CLI engine complet (JunOS-like)
- Config Engine (commit/rollback/compare/confirmed)
- Validare în două faze
- Interfețe (routed + switchport)
- VLANs, SVI/IRB
- Routing static
- IS-IS
- BGP de bază (iBGP, eBGP)
- PFE cu XDP generic + kernel fallback
- Show commands esențiale
- Funcționează pe KVM și VMware

### Faza 2 — Overlay + MPLS
- EVPN/VXLAN (symmetric IRB)
- OSPF
- MPLS + LDP
- SR-MPLS
- ACL / firewall filters
- REST API + WebSocket
- LAG/LACP
- Port channels

### Faza 3 — Advanced Features
- MPLS L3VPN
- BGP route reflector
- QoS
- MSTP
- Libvirt integration (port groups)
- Telemetry

### Faza 4 — Platform
- Central Controller
- Web UI (network management)
- Web UI (VM management)
- Distributed storage integration
- HA

---

## 12. Dependințe Sistem (Ubuntu)

### Python packages
```
prompt_toolkit >= 3.0
pyroute2 >= 0.7
pyyaml >= 6.0
click >= 8.0
rich >= 13.0          ← pentru output colorat în show commands
jsonschema >= 4.0
```

### System packages
```
frr                   ← FRR routing suite
frr-pythontools       ← Python tools pentru FRR
iproute2              ← ip, bridge commands
bridge-utils          ← brctl (compat)
linux-headers         ← pentru compilare eBPF
clang                 ← compilare XDP/eBPF programs
llvm                  ← backend pentru clang eBPF
libbpf-dev            ← librărie BPF
bpftool               ← debugging XDP maps
libmnl-dev            ← Netlink library pentru C
```

### Kernel requirements
```
Linux kernel >= 5.4   ← XDP generic support stabil
CONFIG_BPF=y
CONFIG_BPF_SYSCALL=y
CONFIG_XDP_SOCKETS=y
CONFIG_NET_SCH_INGRESS=y
```

Ubuntu 22.04 LTS sau 24.04 LTS — ambele satisfac toate cerințele.

---

## 13. Future Features (TODO)

- [ ] Central Controller (multi-node management)
- [ ] Web UI pentru network management (switch/router topology, config)
- [ ] Web UI pentru VM management (create/delete/migrate/console)
- [ ] Distributed storage integration (Ceph)
- [ ] VM templates și provisioning
- [ ] HA pentru VM-uri
- [ ] Resource scheduling (pe ce nod pornește o VM)
- [ ] Console access din browser
- [ ] Libvirt integration mai strânsă (opțional)

---

## 14. Decizii Arhitecturale și Motivații

| Decizie | Alternativă considerată | Motivul alegerii |
|---|---|---|
| Python pentru control plane | Go, Rust | Viteză de dezvoltare, ecosistem networking bogat |
| C pentru PFE/XDP | Rust | XDP necesită C, fără alternativă reală |
| FRR ca routing engine | BIRD, custom | Cel mai matur, suportă tot (IS-IS/BGP/EVPN/MPLS) |
| JunOS-like CLI | IOS-like, propriu | Cel mai elegant, familiar pentru network engineers |
| XDP cu fallback kernel | DPDK, VPP | Funcționează pe VM-uri fără hardware special |
| JSON config pe disk | YAML, text propriu | Ușor de parsat, diff-abil, tooling bogat |
| Unix socket IPC | gRPC, shared memory | Simplu, debuggable, suficient de rapid pentru faza 1 |
| Commit/rollback stateful | Stateless (IOS style) | Siguranță, recovery ușor, experiență JunOS |
| IS-IS underlay | OSPF | Scalează mai bine în fabric, standard în datacenter modern |

---

*Document versiunea 0.1 — Faza 1*
*Actualizat pe măsură ce arhitectura evoluează*
