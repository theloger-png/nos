/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef NOS_FIB_H
#define NOS_FIB_H

#include <stdint.h>

/* Route flags passed to fib_route_add(). */
#define FIB_F_GATEWAY   (1u << 0)   /* nexthop is an off-link gateway */
#define FIB_F_BLACKHOLE (1u << 1)   /* silently drop matching packets */

/* Aggregate of per-CPU stats_map values for one interface. */
struct fib_stats {
    uint64_t rx_packets;
    uint64_t rx_bytes;
    uint64_t tx_packets;
    uint64_t tx_bytes;
};

/* Lifecycle ---------------------------------------------------------------- */

/* Open / pin BPF maps.  Must be called before any other fib_* function. */
int  fib_init(void);

/* Unpin maps and release resources. */
void fib_destroy(void);

/* Routes ------------------------------------------------------------------- */

/* Add or replace a route.
 * prefix:  CIDR string, e.g. "10.0.0.0/24" or "2001:db8::/32".
 * nexthop: next-hop address string, or NULL for directly connected.
 * ifindex: output interface.
 * flags:   FIB_F_* bitmask. */
int fib_route_add(const char *prefix, const char *nexthop,
                  uint32_t ifindex, uint32_t flags);

/* Remove a route by prefix.  Returns -1 if the prefix was not found. */
int fib_route_del(const char *prefix);

/* Neighbors ---------------------------------------------------------------- */

/* Add or replace an ARP/ND entry.
 * ip:  resolved IP address string (v4 or v6).
 * mac: MAC address string, e.g. "aa:bb:cc:dd:ee:ff".
 * ifindex: egress interface. */
int fib_neigh_add(const char *ip, const char *mac, uint32_t ifindex);

/* Remove a neighbor entry by IP.  Returns -1 if not found. */
int fib_neigh_del(const char *ip);

/* VLANs -------------------------------------------------------------------- */

/* Map a VLAN ID [1..4094] to an egress interface. */
int fib_vlan_set(uint16_t vlan_id, uint32_t ifindex);

/* Remove a VLAN mapping.  Returns -1 if not found. */
int fib_vlan_del(uint16_t vlan_id);

/* Statistics --------------------------------------------------------------- */

/* Read aggregated counters for one interface into *out.
 * Sums across all CPUs from the PERCPU_HASH stats_map.
 * Returns -1 if ifindex is not present in the map. */
int fib_stats_get(uint32_t ifindex, struct fib_stats *out);

#endif /* NOS_FIB_H */
