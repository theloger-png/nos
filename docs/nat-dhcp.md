# NAT and DHCP

## 1. NAT

NAT rules are applied to the kernel via nftables (`nos_nat` table, `inet` family) on every `commit`. All three NAT types — static, source pool, and destination — can coexist.

---

### 1.1 Static NAT (1:1)

Maps one internal IP to one public IP bidirectionally. Outbound traffic from the internal IP is SNATed to the public IP; inbound traffic to the public IP is DNATed back to the internal IP.

**Config:**

```
set security nat static rule R1 source 10.0.0.10/32
set security nat static rule R1 translated 1.2.3.4
commit
```

`source` is a prefix (`/32` for a single host). `translated` is a host address.

**Verify:**

```
show security nat static
```
```
Static NAT rules:
Rule            Source                 Translated
-------------------------------------------------------
R1              10.0.0.10/32           1.2.3.4
```

```
show security nat translations
```
Dumps the live `inet nos_nat` nftables table. Expect a `postrouting` chain with:
```
iifname != "lo" ip saddr 10.0.0.10/32 snat to 1.2.3.4
```

---

### 1.2 Source NAT with Pool (many-to-few)

Maps many internal IPs to a smaller set of public IPs. Outbound connections are SNATed to an address drawn from the named pool.

**Pool addressing:** network and broadcast addresses are excluded. A `/30` pool (`203.0.113.0/30`) yields the range `203.0.113.1–203.0.113.2`. A `/32` is treated as a single host.

**Config — 10 LAN hosts sharing 4 public IPs:**

```
set security nat pool PUB4 address 198.51.100.0/30
set security nat source rule OUTBOUND match source 192.168.10.0/24
set security nat source rule OUTBOUND then pool PUB4
set security nat source rule OUTBOUND interface et1
commit
```

`interface` names the outgoing interface in NOS alias form (`et0`, `et1`, `irb.100`). The rule only fires on packets leaving that interface.

**Verify:**

```
show security nat pool
```
```
NAT Pools:
Pool            Address
----------------------------------------
PUB4            198.51.100.0/30
```

```
show security nat source
```
```
Source NAT rules:
Rule            Match Source           Pool             Interface
-----------------------------------------------------------------------
OUTBOUND        192.168.10.0/24        PUB4             et1
```

```
show security nat translations
```
Expect a `postrouting` chain entry:
```
oifname "ens4" ip saddr 192.168.10.0/24 snat to 198.51.100.1-198.51.100.2
```
(kernel name shown, not alias)

---

### 1.3 Destination NAT (port forwarding)

Rewrites the destination IP (and optionally port) on inbound packets — typically used to forward a public port to an internal server.

#### Port forwarding (with port mapping)

Forward TCP/UDP port 80 on the public IP `203.0.113.1` to an internal web server at `10.0.0.5:8080`:

```
set security nat destination rule WEB match destination 203.0.113.1
set security nat destination rule WEB match destination-port 80
set security nat destination rule WEB then destination 10.0.0.5
set security nat destination rule WEB then destination-port 8080
commit
```

When `destination-port` is set, the rule is installed for both TCP and UDP.

#### Without port remapping

Forward all traffic destined to a public IP to an internal IP (no port translation):

```
set security nat destination rule DMZ match destination 203.0.113.2
set security nat destination rule DMZ then destination 10.0.0.20
commit
```

**Verify:**

```
show security nat destination
```
```
Destination NAT rules:
Rule            Match Dest             Match Port  Then Dest          Then Port
------------------------------------------------------------------------------------
DMZ             203.0.113.2            -           10.0.0.20          -
WEB             203.0.113.1            80          10.0.0.5           8080
```

```
show security nat translations
```
Expect `prerouting` chain entries:
```
tcp dport 80 ip daddr 203.0.113.1 dnat to 10.0.0.5:8080
udp dport 80 ip daddr 203.0.113.1 dnat to 10.0.0.5:8080
ip daddr 203.0.113.2 dnat to 10.0.0.20
```

---

### 1.4 Combining NAT types

A common setup: source NAT pool for outbound traffic on `et1`, plus destination NAT to forward inbound port 443 to an internal HTTPS server.

```
# Outbound pool
set security nat pool MYPOOL address 203.0.113.0/29
set security nat source rule OUT match source 10.0.0.0/8
set security nat source rule OUT then pool MYPOOL
set security nat source rule OUT interface et1

# Inbound port forward
set security nat destination rule HTTPS match destination 203.0.113.1
set security nat destination rule HTTPS match destination-port 443
set security nat destination rule HTTPS then destination 10.0.0.100
set security nat destination rule HTTPS then destination-port 443

commit
```

Both rules compile into the same `nos_nat` table — source rules into `postrouting`, destination rules into `prerouting`.

---

### 1.5 Removing NAT rules

```
delete security nat static rule R1
delete security nat pool PUB4
delete security nat source rule OUTBOUND
delete security nat destination rule WEB
commit
```

Deleting a pool while a source rule still references it causes the source rule to produce no nftables entry on the next commit (the driver silently skips rules with missing pools). Remove or update both together.

---

## 2. DHCP Server

The DHCP server is implemented by dnsmasq. NOS writes per-pool config files under `/etc/dnsmasq.d/` and reloads dnsmasq on each commit. The DHCP client uses `dhclient`.

---

### 2.1 Basic DHCP server on an IRB/SVI interface

**Use case:** serve DHCP on VLAN 100 via `irb.100`.

Gateway is set explicitly — NOS does not auto-detect it from the interface IP.

```
# Define the pool
set system services dhcp-local-server pool VLAN100 range low 192.168.100.10
set system services dhcp-local-server pool VLAN100 range high 192.168.100.200
set system services dhcp-local-server pool VLAN100 gateway 192.168.100.1

# Bind the pool to the interface
set system services dhcp-local-server interface irb.100 pool VLAN100

commit
```

The interface must already have `192.168.100.1/24` configured:

```
set interfaces irb.100 family inet address 192.168.100.1/24
```

**Verify:**

```
show dhcp server leases
```
```
Expiry      MAC Address       IP Address      Hostname            Client-ID
2026-06-09 10:00  aa:bb:cc:dd:ee:ff  192.168.100.15  myhost              *
```

```
show dhcp server statistics
```
```
Pool                Interface     Range Low       Range High      Active
VLAN100             irb.100       192.168.100.10  192.168.100.200 1
```

Filter leases by interface:
```
show dhcp server leases interface irb.100
```

---

### 2.2 DHCP server with custom DNS

Add a `dns-server` to an existing pool:

```
set system services dhcp-local-server pool VLAN100 dns-server 8.8.8.8
commit
```

Only one DNS server per pool is supported. To change it, set the new value and commit again.

---

### 2.3 Multiple DHCP pools on different interfaces

Each interface gets its own pool. Pool names must be unique.

```
# VLAN 100 — management
set system services dhcp-local-server pool MGMT range low 10.100.0.10
set system services dhcp-local-server pool MGMT range high 10.100.0.50
set system services dhcp-local-server pool MGMT gateway 10.100.0.1
set system services dhcp-local-server pool MGMT dns-server 10.100.0.1
set system services dhcp-local-server interface irb.100 pool MGMT

# VLAN 200 — users
set system services dhcp-local-server pool USERS range low 10.200.0.10
set system services dhcp-local-server pool USERS range high 10.200.0.250
set system services dhcp-local-server pool USERS gateway 10.200.0.1
set system services dhcp-local-server pool USERS dns-server 1.1.1.1
set system services dhcp-local-server interface irb.200 pool USERS

commit
```

Each pool generates a separate `/etc/dnsmasq.d/nos-<interface>-<pool>.conf` file.

---

### 2.4 DHCP client

Enable DHCP client mode on an interface. This starts `dhclient` for that interface.

```
set interfaces et0 family inet dhcp
commit
```

`dhcp` and a static `address` are mutually exclusive on the same interface. The commit will be rejected if both are set.

**Verify:**

```
show dhcp client leases
```
```
Interface     IP Address      Subnet Mask     Gateway         Expiry
et0           192.0.2.50      255.255.255.0   192.0.2.1       2026/06/09 12:00:00
```

---

### 2.5 Removing DHCP config

Remove the pool from an interface, then delete the pool:

```
delete system services dhcp-local-server interface irb.100 pool VLAN100
delete system services dhcp-local-server pool VLAN100
commit
```

Remove the entire DHCP server config for an interface:

```
delete system services dhcp-local-server interface irb.100
commit
```

Disable DHCP client on an interface:

```
delete interfaces et0 family inet dhcp
commit
```

---

## 3. Troubleshooting

### NAT not working

**Check the nftables table:**
```
show security nat translations
```
- `NAT table not active (no rules applied)` — no rules committed, or commit did not apply. Re-run `commit` from configure mode.
- Empty output from `nft list table` — rules were flushed; check logs for nft errors.

**Common causes:**
- Wrong interface name in a source NAT rule. The rule uses the NOS alias (`et1`). Verify with `show interfaces terse` that `et1` exists and maps to the expected kernel interface.
- Pool name in the source rule does not match any defined pool (`show security nat pool`). The driver silently skips the rule if the pool is missing.
- Missing `match source` or `match destination` on a rule — the rule is incomplete and will not be rendered.
- `nft` not installed or not in sudo path — check `/var/log/syslog` or `journalctl -u nos` for `nft not found` errors.

### DHCP leases not being issued

**Check dnsmasq is running:**
```
show dhcp server statistics
```
If it returns `No DHCP pools configured`, the config was not committed or the pool was not bound to an interface.

**Check from the OS:**
```
! systemctl status dnsmasq
```

**Common causes:**
- Interface name in the pool binding does not match a configured interface — dnsmasq binds by kernel interface name. NOS translates aliases to kernel names when writing config files, but the interface must be up.
- Interface is down — confirm with `show interfaces terse` that the interface admin/link state is `up/up`.
- Pool range is outside the subnet assigned to the interface — clients will send requests but dnsmasq will not respond.
- `dns-server` misconfigured — does not prevent leases but clients will have broken DNS. Verify with `show dhcp server leases` that IPs are being assigned.
- Multiple pools accidentally assigned the same range — dnsmasq will log conflicts to syslog.
