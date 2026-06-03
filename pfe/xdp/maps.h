/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef NOS_XDP_MAPS_H
#define NOS_XDP_MAPS_H

/*
 * BPF map definitions for NOS PFE.
 *
 * Struct definitions are unconditional so xdp_loader.c (userspace) can use
 * the same key/value types when calling bpf_map_update_elem / bpf_map_lookup_elem.
 * Map object declarations are guarded by __bpf__ (set by clang -target bpf).
 */

#include <linux/types.h>
#include <linux/if_ether.h>   /* ETH_ALEN */

#ifdef __bpf__
#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>
#endif

/* ── FIB (Forwarding Information Base) ──────────────────────────────────────
 *
 * IPv4 and IPv6 are split into fib4_map / fib6_map because LPM_TRIE requires a
 * fixed key size per map and the two address families have different widths.
 * The first field of every LPM_TRIE key must be `prefixlen` (__u32).
 */

struct fib4_key {
    __u32  prefixlen;   /* number of significant bits [0, 32] */
    __be32 addr;        /* IPv4 prefix, network byte order */
};

struct fib4_val {
    __be32 nexthop;     /* next-hop IPv4; 0 = directly connected */
    __u32  ifindex;     /* output interface */
    __u32  flags;       /* RTF_GATEWAY etc. */
};

struct fib6_key {
    __u32 prefixlen;    /* number of significant bits [0, 128] */
    __u8  addr[16];     /* IPv6 prefix */
};

struct fib6_val {
    __u8  nexthop[16];  /* next-hop IPv6; all-zeros = directly connected */
    __u32 ifindex;
    __u32 flags;
};

/* ── Neighbor cache (ARP / ND) ───────────────────────────────────────────────
 *
 * Maps a resolved IP address to its layer-2 reachability (MAC + egress port).
 * The union carries either a 4-byte v4 address or a 16-byte v6 address; the
 * af field disambiguates and also ensures the union is always zeroed uniformly.
 */

struct neigh_key {
    __u32 af;           /* AF_INET = 2, AF_INET6 = 10 */
    union {
        __be32 v4;      /* IPv4: occupy first 4 bytes, remaining 12 zeroed */
        __u8   v6[16];  /* IPv6: full 128-bit address */
    } addr;             /* total 16 bytes → struct size = 20 bytes */
};

struct neigh_val {
    __u8  mac[ETH_ALEN]; /* resolved destination MAC */
    __u8  _pad[2];
    __u32 ifindex;       /* egress interface */
    __u32 state;         /* NUD_REACHABLE, NUD_STALE, NUD_FAILED, … */
};

/* ── VLAN table ──────────────────────────────────────────────────────────────
 *
 * Maps a 802.1Q VLAN ID to its logical egress ifindex.  Implemented as an
 * ARRAY so lookup is O(1) with no hashing; index == vlan_id (0 unused).
 */

struct vlan_val {
    __u32 ifindex;      /* logical interface for this VLAN */
    __u32 _pad;
};

/* ── Port VLAN map ───────────────────────────────────────────────────────────
 *
 * Keyed by ingress ifindex.  access mode (0): untagged frames receive a VLAN
 * tag push in the XDP program before XDP_PASS.  trunk mode (1): frames are
 * already tagged; the XDP program skips tag insertion and continues normally.
 */

struct port_vlan_val {
    __u16 vlan_id;  /* 802.1Q VID [1, 4094] */
    __u8  mode;     /* 0 = access (push tag), 1 = trunk (pass through) */
    __u8  _pad;
};

/* ── Interface statistics ────────────────────────────────────────────────────
 *
 * Per-CPU counters keyed by ifindex.  PERCPU_HASH eliminates atomic ops in
 * the XDP fast path; userspace aggregates per-CPU values when reading.
 */

struct stats_val {
    __u64 rx_packets;
    __u64 rx_bytes;
    __u64 tx_packets;
    __u64 tx_bytes;
};

/* ── Local address sets ──────────────────────────────────────────────────────
 *
 * Populated by the userspace loader with the host's own interface addresses.
 * Keyed by address (network byte order); value is a non-zero presence flag.
 * Packets whose destination matches an entry are XDP_PASS'd immediately so
 * the kernel delivers them locally rather than forwarding through the FIB.
 *
 * This is necessary in XDP generic mode, which intercepts packets before the
 * kernel's local-delivery path: without this check, a connected-subnet entry
 * in fib4_map would match the host's own address and bpf_redirect() it.
 */

/* ── Map declarations (BPF / kernel space only) ──────────────────────────── */
#ifdef __bpf__

/* IPv4 LPM forwarding table — up to 64 K prefixes */
struct {
    __uint(type,        BPF_MAP_TYPE_LPM_TRIE);
    __type(key,         struct fib4_key);
    __type(value,       struct fib4_val);
    __uint(max_entries, 65536);
    __uint(map_flags,   BPF_F_NO_PREALLOC);
} fib4_map SEC(".maps");

/* IPv6 LPM forwarding table — up to 64 K prefixes */
struct {
    __uint(type,        BPF_MAP_TYPE_LPM_TRIE);
    __type(key,         struct fib6_key);
    __type(value,       struct fib6_val);
    __uint(max_entries, 65536);
    __uint(map_flags,   BPF_F_NO_PREALLOC);
} fib6_map SEC(".maps");

/* ARP / ND resolution cache */
struct {
    __uint(type,        BPF_MAP_TYPE_HASH);
    __type(key,         struct neigh_key);
    __type(value,       struct neigh_val);
    __uint(max_entries, 16384);
} neigh_map SEC(".maps");

/* VLAN ID → egress interface; array index == vlan_id; VIDs 1–4094 */
struct {
    __uint(type,        BPF_MAP_TYPE_ARRAY);
    __uint(key_size,    sizeof(__u32));
    __type(value,       struct vlan_val);
    __uint(max_entries, 4095);
} vlan_map SEC(".maps");

/* Per-interface, per-CPU packet and byte counters; key = ifindex */
struct {
    __uint(type,        BPF_MAP_TYPE_PERCPU_HASH);
    __uint(key_size,    sizeof(__u32));
    __type(value,       struct stats_val);
    __uint(max_entries, 1024);
} stats_map SEC(".maps");

/* Local IPv4 addresses — key: __be32 (4 bytes, network byte order) */
struct {
    __uint(type,        BPF_MAP_TYPE_HASH);
    __uint(key_size,    sizeof(__be32));
    __uint(value_size,  sizeof(__u32));
    __uint(max_entries, 256);
} local_ip4_map SEC(".maps");

/* Local IPv6 addresses — key: 16-byte address (network byte order) */
struct {
    __uint(type,        BPF_MAP_TYPE_HASH);
    __uint(key_size,    16);
    __uint(value_size,  sizeof(__u32));
    __uint(max_entries, 256);
} local_ip6_map SEC(".maps");

/* Per-ingress-port VLAN mode and ID; key = ifindex (__u32) */
struct {
    __uint(type,        BPF_MAP_TYPE_HASH);
    __uint(key_size,    sizeof(__u32));
    __type(value,       struct port_vlan_val);
    __uint(max_entries, 1024);
} port_vlan_map SEC(".maps");

#endif /* __bpf__ */

#endif /* NOS_XDP_MAPS_H */
