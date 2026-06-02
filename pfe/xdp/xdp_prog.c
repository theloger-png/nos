/* SPDX-License-Identifier: GPL-2.0-only */

/*
 * pfe/xdp/xdp_prog.c — XDP packet forwarding program (kernel space)
 *
 * Attached to each interface ingress via xdp_loader_attach().
 *
 * Packet path
 * -----------
 *  1.  Count rx stats on the ingress interface (every packet, including drops).
 *  2.  Parse Ethernet; drop if frame is too short.
 *  3.  802.1Q: extract VID → vlan_map lookup → redirect if ifindex present (L2 path).
 *      On vlan_map miss, continue with the inner ethertype for L3 processing.
 *  4.  IPv4: LPM lookup in fib4_map → neighbor lookup in neigh_map → dst MAC rewrite
 *      → bpf_redirect() to egress ifindex.
 *  5.  IPv6: same as above using fib6_map / 128-bit prefix key.
 *  6.  FIB miss or unresolved neighbor: XDP_PASS (kernel handles ARP/ND and slow path).
 *  7.  Malformed frame (bounds check failure): XDP_DROP.
 *
 * Notes
 * -----
 *  - Per-CPU PERCPU_HASH stats are incremented lock-free; userspace aggregates.
 *  - No dynamic allocation (BPF restriction); all temporaries live on the stack.
 *  - Compatible with XDP generic/SKB mode (virtio-net, vmxnet3, etc.).
 *  - Destination MAC is rewritten to the next-hop's resolved MAC from neigh_map.
 *    Source MAC is set to the old destination MAC (the router's ingress MAC), which
 *    is correct when all interfaces share one MAC.  A dedicated ifmac map would be
 *    required to use the egress interface's own MAC in multi-MAC topologies.
 */

#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/ipv6.h>

#include "maps.h"

/* Mirror AF_INET / AF_INET6 without pulling in userspace socket headers. */
#ifndef AF_INET
#define AF_INET  2
#endif
#ifndef AF_INET6
#define AF_INET6 10
#endif

/* ── inline 802.1Q header ────────────────────────────────────────────────── */

struct nos_vlanhdr {
    __be16 tci;    /* PCP(3) | DEI(1) | VID(12), network byte order */
    __be16 proto;  /* inner ethertype, network byte order */
};

/* ── per-CPU stats helpers ───────────────────────────────────────────────── */

static __always_inline void
stats_bump_rx(__u32 ifindex, __u32 bytes)
{
    struct stats_val *s = bpf_map_lookup_elem(&stats_map, &ifindex);
    if (!s)
        return;
    s->rx_packets++;
    s->rx_bytes += bytes;
}

static __always_inline void
stats_bump_tx(__u32 ifindex, __u32 bytes)
{
    struct stats_val *s = bpf_map_lookup_elem(&stats_map, &ifindex);
    if (!s)
        return;
    s->tx_packets++;
    s->tx_bytes += bytes;
}

/* ── XDP entry point ─────────────────────────────────────────────────────── */

SEC("xdp")
int nos_xdp_fwd(struct xdp_md *ctx)
{
    void *data     = (void *)(long)ctx->data;
    void *data_end = (void *)(long)ctx->data_end;
    __u32 ingress  = ctx->ingress_ifindex;
    __u32 pktlen   = (__u32)(data_end - data);

    /* ── rx stats: every packet, before any drop/pass decision ── */
    stats_bump_rx(ingress, pktlen);

    /* ── Ethernet header ── */
    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end)
        return XDP_DROP;  /* frame too short to hold Ethernet header */

    __u16 proto = bpf_ntohs(eth->h_proto);
    void *l3    = (void *)(eth + 1);

    /* ── 802.1Q VLAN (L2 forwarding path) ── */
    if (proto == ETH_P_8021Q) {
        struct nos_vlanhdr *vh = l3;
        if ((void *)(vh + 1) > data_end)
            return XDP_DROP;

        __u16 vid  = bpf_ntohs(vh->tci) & 0x0FFFu;  /* 12-bit VID */
        __u32 vkey = (__u32)vid;

        struct vlan_val *vv = bpf_map_lookup_elem(&vlan_map, &vkey);
        if (vv && vv->ifindex != 0) {
            /* VLAN map hit: redirect at L2, no MAC rewrite needed */
            stats_bump_tx(vv->ifindex, pktlen);
            return bpf_redirect(vv->ifindex, 0);
        }

        /* No VLAN map entry — fall through to L3 using inner ethertype */
        proto = bpf_ntohs(vh->proto);
        l3    = (void *)(vh + 1);
    }

    /* ── IPv4 forwarding ── */
    if (proto == ETH_P_IP) {
        struct iphdr *ip = l3;
        if ((void *)(ip + 1) > data_end)
            return XDP_DROP;
        if (ip->ihl < 5)           /* minimum header length: 5 × 4 = 20 bytes */
            return XDP_DROP;

        /* LPM lookup: set prefixlen=32 so the trie finds the longest match */
        struct fib4_key fk = {
            .prefixlen = 32,
            .addr      = ip->daddr,
        };
        struct fib4_val *fv = bpf_map_lookup_elem(&fib4_map, &fk);
        if (!fv)
            return XDP_PASS;  /* FIB miss: let kernel route it */

        /* Neighbor resolution: use gateway address, or dst IP if directly connected */
        struct neigh_key nk = { .af = AF_INET };
        nk.addr.v4 = fv->nexthop ? fv->nexthop : ip->daddr;

        struct neigh_val *nv = bpf_map_lookup_elem(&neigh_map, &nk);
        if (!nv)
            return XDP_PASS;  /* neighbor not yet resolved; kernel will ARP */

        /* Rewrite dst MAC to next-hop's resolved MAC; use old dst as new src */
        __builtin_memcpy(eth->h_source, eth->h_dest,  ETH_ALEN);
        __builtin_memcpy(eth->h_dest,   nv->mac,      ETH_ALEN);

        stats_bump_tx(nv->ifindex, pktlen);
        return bpf_redirect(nv->ifindex, 0);
    }

    /* ── IPv6 forwarding ── */
    if (proto == ETH_P_IPV6) {
        struct ipv6hdr *ip6 = l3;
        if ((void *)(ip6 + 1) > data_end)
            return XDP_DROP;

        /* Build 128-bit LPM key from the destination address */
        struct fib6_key fk = { .prefixlen = 128 };
        __builtin_memcpy(fk.addr, &ip6->daddr, 16);

        struct fib6_val *fv = bpf_map_lookup_elem(&fib6_map, &fk);
        if (!fv)
            return XDP_PASS;

        struct neigh_key nk = { .af = AF_INET6 };

        /* Check for all-zeros nexthop (directly connected route) */
        __u64 nh_hi, nh_lo;
        __builtin_memcpy(&nh_hi, fv->nexthop,      8);
        __builtin_memcpy(&nh_lo, fv->nexthop + 8,  8);
        if (nh_hi == 0 && nh_lo == 0)
            __builtin_memcpy(nk.addr.v6, &ip6->daddr, 16);
        else
            __builtin_memcpy(nk.addr.v6, fv->nexthop,  16);

        struct neigh_val *nv = bpf_map_lookup_elem(&neigh_map, &nk);
        if (!nv)
            return XDP_PASS;

        __builtin_memcpy(eth->h_source, eth->h_dest, ETH_ALEN);
        __builtin_memcpy(eth->h_dest,   nv->mac,     ETH_ALEN);

        stats_bump_tx(nv->ifindex, pktlen);
        return bpf_redirect(nv->ifindex, 0);
    }

    /* Unknown ethertype (ARP, MPLS, etc.) — pass to kernel */
    return XDP_PASS;
}

char _license[] SEC("license") = "GPL";
