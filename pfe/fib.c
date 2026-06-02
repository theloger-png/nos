/* SPDX-License-Identifier: GPL-2.0-only */

#include <arpa/inet.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <sys/stat.h>
#include <unistd.h>

#include <linux/neighbour.h>   /* NUD_REACHABLE */

#include <bpf/bpf.h>
#include <bpf/libbpf.h>

#include "fib.h"
#include "xdp/maps.h"

/* ── BPF filesystem pin paths ────────────────────────────────────────────── */

#define BPF_FS_DIR  "/sys/fs/bpf/nos"
#define PIN_FIB4    BPF_FS_DIR "/fib4_map"
#define PIN_FIB6    BPF_FS_DIR "/fib6_map"
#define PIN_NEIGH   BPF_FS_DIR "/neigh_map"
#define PIN_VLAN    BPF_FS_DIR "/vlan_map"
#define PIN_STATS   BPF_FS_DIR "/stats_map"

/* ── logging ─────────────────────────────────────────────────────────────── */

#define fib_err(fmt, ...)  syslog(LOG_ERR,     "fib: " fmt, ##__VA_ARGS__)
#define fib_warn(fmt, ...) syslog(LOG_WARNING,  "fib: " fmt, ##__VA_ARGS__)
#define fib_info(fmt, ...) syslog(LOG_INFO,     "fib: " fmt, ##__VA_ARGS__)

/* ── module state ────────────────────────────────────────────────────────── */

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;

static int g_fd_fib4  = -1;
static int g_fd_fib6  = -1;
static int g_fd_neigh = -1;
static int g_fd_vlan  = -1;
static int g_fd_stats = -1;

/* ── internal helpers ────────────────────────────────────────────────────── */

/*
 * Reuse a map already pinned to the BPF filesystem, or create a new one and
 * pin it.  This lets map state survive process restarts.
 */
static int open_or_create_map(const char *pin_path,
                               enum bpf_map_type type, const char *name,
                               __u32 key_size, __u32 value_size,
                               __u32 max_entries, __u32 map_flags)
{
    int fd = bpf_obj_get(pin_path);
    if (fd >= 0)
        return fd;

    struct bpf_map_create_opts opts = {
        .sz        = sizeof(struct bpf_map_create_opts),
        .map_flags = map_flags,
    };

    fd = bpf_map_create(type, name, key_size, value_size, max_entries, &opts);
    if (fd < 0) {
        fib_err("bpf_map_create %s: %s", name, strerror(errno));
        return -1;
    }

    if (bpf_obj_pin(fd, pin_path) < 0)
        fib_warn("bpf_obj_pin %s: %s — map will not survive restart",
                 pin_path, strerror(errno));

    return fd;
}

/*
 * Parse a CIDR string ("192.0.2.0/24" or "2001:db8::/32") into its address
 * family, binary address (addr_buf, 16 bytes), and prefix length.
 * Returns AF_INET, AF_INET6, or -1 on error.
 */
static int parse_prefix(const char *prefix, int *plen_out, uint8_t addr_buf[16])
{
    char buf[INET6_ADDRSTRLEN + 5];   /* addr + '/' + "128" + NUL */
    if (snprintf(buf, sizeof(buf), "%s", prefix) >= (int)sizeof(buf)) {
        fib_err("prefix string too long");
        return -1;
    }

    char *slash = strchr(buf, '/');
    if (!slash) {
        fib_err("prefix missing '/': %s", prefix);
        return -1;
    }
    *slash++ = '\0';
    int plen = atoi(slash);

    memset(addr_buf, 0, 16);

    if (inet_pton(AF_INET, buf, addr_buf) == 1) {
        if (plen < 0 || plen > 32) {
            fib_err("IPv4 prefix length %d out of range", plen);
            return -1;
        }
        *plen_out = plen;
        return AF_INET;
    }

    if (inet_pton(AF_INET6, buf, addr_buf) == 1) {
        if (plen < 0 || plen > 128) {
            fib_err("IPv6 prefix length %d out of range", plen);
            return -1;
        }
        *plen_out = plen;
        return AF_INET6;
    }

    fib_err("cannot parse prefix address: %s", buf);
    return -1;
}

/* Parse "aa:bb:cc:dd:ee:ff" into 6 bytes.  Returns 0 on success, -1 on error. */
static int parse_mac(const char *mac_str, uint8_t mac[ETH_ALEN])
{
    unsigned int b[ETH_ALEN];
    if (sscanf(mac_str, "%x:%x:%x:%x:%x:%x",
               &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != ETH_ALEN)
        return -1;
    for (int i = 0; i < ETH_ALEN; i++)
        mac[i] = (uint8_t)b[i];
    return 0;
}

/* ── lifecycle ───────────────────────────────────────────────────────────── */

int fib_init(void)
{
    /* Ensure the pin directory exists on the BPF filesystem. */
    if (mkdir(BPF_FS_DIR, 0700) < 0 && errno != EEXIST)
        fib_warn("mkdir %s: %s", BPF_FS_DIR, strerror(errno));

    g_fd_fib4 = open_or_create_map(PIN_FIB4,
                    BPF_MAP_TYPE_LPM_TRIE, "fib4_map",
                    sizeof(struct fib4_key), sizeof(struct fib4_val),
                    65536, BPF_F_NO_PREALLOC);
    if (g_fd_fib4 < 0) return -1;

    g_fd_fib6 = open_or_create_map(PIN_FIB6,
                    BPF_MAP_TYPE_LPM_TRIE, "fib6_map",
                    sizeof(struct fib6_key), sizeof(struct fib6_val),
                    65536, BPF_F_NO_PREALLOC);
    if (g_fd_fib6 < 0) goto err_fib6;

    g_fd_neigh = open_or_create_map(PIN_NEIGH,
                    BPF_MAP_TYPE_HASH, "neigh_map",
                    sizeof(struct neigh_key), sizeof(struct neigh_val),
                    16384, 0);
    if (g_fd_neigh < 0) goto err_neigh;

    g_fd_vlan = open_or_create_map(PIN_VLAN,
                    BPF_MAP_TYPE_ARRAY, "vlan_map",
                    sizeof(__u32), sizeof(struct vlan_val),
                    4095, 0);
    if (g_fd_vlan < 0) goto err_vlan;

    g_fd_stats = open_or_create_map(PIN_STATS,
                    BPF_MAP_TYPE_PERCPU_HASH, "stats_map",
                    sizeof(__u32), sizeof(struct stats_val),
                    1024, 0);
    if (g_fd_stats < 0) goto err_stats;

    fib_info("maps initialized (fib4=%d fib6=%d neigh=%d vlan=%d stats=%d)",
             g_fd_fib4, g_fd_fib6, g_fd_neigh, g_fd_vlan, g_fd_stats);
    return 0;

err_stats: close(g_fd_vlan);  g_fd_vlan  = -1;
err_vlan:  close(g_fd_neigh); g_fd_neigh = -1;
err_neigh: close(g_fd_fib6);  g_fd_fib6  = -1;
err_fib6:  close(g_fd_fib4);  g_fd_fib4  = -1;
    return -1;
}

void fib_destroy(void)
{
    pthread_mutex_lock(&g_lock);
    if (g_fd_fib4  >= 0) { close(g_fd_fib4);  g_fd_fib4  = -1; }
    if (g_fd_fib6  >= 0) { close(g_fd_fib6);  g_fd_fib6  = -1; }
    if (g_fd_neigh >= 0) { close(g_fd_neigh); g_fd_neigh = -1; }
    if (g_fd_vlan  >= 0) { close(g_fd_vlan);  g_fd_vlan  = -1; }
    if (g_fd_stats >= 0) { close(g_fd_stats); g_fd_stats = -1; }
    pthread_mutex_unlock(&g_lock);
    fib_info("maps closed");
}

/* ── routes ──────────────────────────────────────────────────────────────── */

int fib_route_add(const char *prefix, const char *nexthop,
                  uint32_t ifindex, uint32_t flags)
{
    uint8_t addr[16], nh[16] = {0};
    int plen, af;

    af = parse_prefix(prefix, &plen, addr);
    if (af < 0)
        return -1;

    pthread_mutex_lock(&g_lock);
    int rc = -1;

    if (af == AF_INET) {
        struct fib4_key k = { .prefixlen = (__u32)plen };
        memcpy(&k.addr, addr, 4);

        struct fib4_val v = { .ifindex = ifindex, .flags = flags };
        if (nexthop && inet_pton(AF_INET, nexthop, &v.nexthop) != 1) {
            fib_err("fib_route_add: bad IPv4 nexthop '%s'", nexthop);
            goto out;
        }
        if (bpf_map_update_elem(g_fd_fib4, &k, &v, BPF_ANY) < 0) {
            fib_err("fib4 update %s: %s", prefix, strerror(errno));
            goto out;
        }
    } else {
        struct fib6_key k = { .prefixlen = (__u32)plen };
        memcpy(k.addr, addr, 16);

        struct fib6_val v = { .ifindex = ifindex, .flags = flags };
        if (nexthop) {
            if (inet_pton(AF_INET6, nexthop, nh) != 1) {
                fib_err("fib_route_add: bad IPv6 nexthop '%s'", nexthop);
                goto out;
            }
            memcpy(v.nexthop, nh, 16);
        }
        if (bpf_map_update_elem(g_fd_fib6, &k, &v, BPF_ANY) < 0) {
            fib_err("fib6 update %s: %s", prefix, strerror(errno));
            goto out;
        }
    }
    rc = 0;
out:
    pthread_mutex_unlock(&g_lock);
    return rc;
}

int fib_route_del(const char *prefix)
{
    uint8_t addr[16];
    int plen, af;

    af = parse_prefix(prefix, &plen, addr);
    if (af < 0)
        return -1;

    pthread_mutex_lock(&g_lock);
    int rc;

    if (af == AF_INET) {
        struct fib4_key k = { .prefixlen = (__u32)plen };
        memcpy(&k.addr, addr, 4);
        rc = bpf_map_delete_elem(g_fd_fib4, &k);
    } else {
        struct fib6_key k = { .prefixlen = (__u32)plen };
        memcpy(k.addr, addr, 16);
        rc = bpf_map_delete_elem(g_fd_fib6, &k);
    }

    if (rc < 0 && errno != ENOENT)
        fib_err("fib_route_del %s: %s", prefix, strerror(errno));

    pthread_mutex_unlock(&g_lock);
    return rc < 0 ? -1 : 0;
}

/* ── neighbors ───────────────────────────────────────────────────────────── */

int fib_neigh_add(const char *ip, const char *mac, uint32_t ifindex)
{
    struct neigh_key k = {0};
    struct neigh_val v = {0};

    if (inet_pton(AF_INET, ip, &k.addr.v4) == 1) {
        k.af = AF_INET;
    } else if (inet_pton(AF_INET6, ip, k.addr.v6) == 1) {
        k.af = AF_INET6;
    } else {
        fib_err("fib_neigh_add: bad IP '%s'", ip);
        return -1;
    }

    if (parse_mac(mac, v.mac) < 0) {
        fib_err("fib_neigh_add: bad MAC '%s'", mac);
        return -1;
    }
    v.ifindex = ifindex;
    v.state   = NUD_REACHABLE;

    pthread_mutex_lock(&g_lock);
    int rc = bpf_map_update_elem(g_fd_neigh, &k, &v, BPF_ANY);
    if (rc < 0)
        fib_err("neigh update %s: %s", ip, strerror(errno));
    pthread_mutex_unlock(&g_lock);
    return rc < 0 ? -1 : 0;
}

int fib_neigh_del(const char *ip)
{
    struct neigh_key k = {0};

    if (inet_pton(AF_INET, ip, &k.addr.v4) == 1) {
        k.af = AF_INET;
    } else if (inet_pton(AF_INET6, ip, k.addr.v6) == 1) {
        k.af = AF_INET6;
    } else {
        fib_err("fib_neigh_del: bad IP '%s'", ip);
        return -1;
    }

    pthread_mutex_lock(&g_lock);
    int rc = bpf_map_delete_elem(g_fd_neigh, &k);
    if (rc < 0 && errno != ENOENT)
        fib_err("neigh delete %s: %s", ip, strerror(errno));
    pthread_mutex_unlock(&g_lock);
    return rc < 0 ? -1 : 0;
}

/* ── VLANs ───────────────────────────────────────────────────────────────── */

int fib_vlan_set(uint16_t vlan_id, uint32_t ifindex)
{
    if (vlan_id < 1 || vlan_id > 4094) {
        fib_err("fib_vlan_set: vlan_id %u out of range", (unsigned)vlan_id);
        return -1;
    }

    __u32 key = (__u32)vlan_id;
    struct vlan_val val = { .ifindex = ifindex };

    pthread_mutex_lock(&g_lock);
    int rc = bpf_map_update_elem(g_fd_vlan, &key, &val, BPF_ANY);
    if (rc < 0)
        fib_err("vlan set %u: %s", (unsigned)vlan_id, strerror(errno));
    pthread_mutex_unlock(&g_lock);
    return rc < 0 ? -1 : 0;
}

int fib_vlan_del(uint16_t vlan_id)
{
    if (vlan_id < 1 || vlan_id > 4094) {
        fib_err("fib_vlan_del: vlan_id %u out of range", (unsigned)vlan_id);
        return -1;
    }

    __u32 key = (__u32)vlan_id;
    struct vlan_val cur;

    pthread_mutex_lock(&g_lock);

    /* ARRAY maps do not support delete; read-check then zero as sentinel. */
    if (bpf_map_lookup_elem(g_fd_vlan, &key, &cur) < 0 || cur.ifindex == 0) {
        pthread_mutex_unlock(&g_lock);
        return -1;
    }

    struct vlan_val zero = {0};
    int rc = bpf_map_update_elem(g_fd_vlan, &key, &zero, BPF_EXIST);
    if (rc < 0)
        fib_err("vlan del %u: %s", (unsigned)vlan_id, strerror(errno));

    pthread_mutex_unlock(&g_lock);
    return rc < 0 ? -1 : 0;
}

/* ── statistics ──────────────────────────────────────────────────────────── */

int fib_stats_get(uint32_t ifindex, struct fib_stats *out)
{
    int ncpus = libbpf_num_possible_cpus();
    if (ncpus <= 0) {
        fib_err("libbpf_num_possible_cpus failed: %s", strerror(errno));
        return -1;
    }

    struct stats_val *percpu = calloc((size_t)ncpus, sizeof(*percpu));
    if (!percpu) {
        fib_err("fib_stats_get: out of memory");
        return -1;
    }

    pthread_mutex_lock(&g_lock);
    int rc = bpf_map_lookup_elem(g_fd_stats, &ifindex, percpu);
    pthread_mutex_unlock(&g_lock);

    if (rc < 0) {
        free(percpu);
        return -1;   /* ENOENT: interface not yet tracked */
    }

    memset(out, 0, sizeof(*out));
    for (int i = 0; i < ncpus; i++) {
        out->rx_packets += percpu[i].rx_packets;
        out->rx_bytes   += percpu[i].rx_bytes;
        out->tx_packets += percpu[i].tx_packets;
        out->tx_bytes   += percpu[i].tx_bytes;
    }

    free(percpu);
    return 0;
}
