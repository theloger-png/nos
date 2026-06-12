/* SPDX-License-Identifier: GPL-2.0-only */

/*
 * pfe/xdp/xdp_loader.c — Load and attach the XDP forwarding program.
 *
 * Map ownership: fib.c creates and pins all BPF maps under /sys/fs/bpf/nos/.
 * This module reuses those maps via bpf_map__reuse_fd() before loading the
 * BPF object, so the XDP fast path and the fib.c control path share state.
 * On restart, existing pinned maps are reused automatically; route state
 * survives as long as fib.c does not unpin them.
 *
 * Attach strategy per interface (three tiers):
 *   1. bpf_program__attach_xdp()             — BPF_LINK_CREATE, link-based native.
 *      The kernel manages lifetime; the program is auto-detached when the link fd
 *      is closed.  Preferred because it is the cleanest ownership model.
 *   2. bpf_xdp_attach(XDP_FLAGS_DRV_MODE)   — legacy netlink native (same path as
 *      "ip link set ... xdpdrv").  Used when tier 1 fails; works on i40e and other
 *      drivers that support XDP in driver mode but not the newer link-based API.
 *   3. bpf_xdp_attach(XDP_FLAGS_SKB_MODE)   — generic / software XDP.  Last resort;
 *      always works but runs in the kernel's skb fast-path, not the driver.
 * Tiers 1 and 2 are skipped when the caller passes XDP_FLAGS_SKB_MODE.
 */

#include <errno.h>
#include <net/if.h>
#include <stdarg.h>
#include <string.h>
#include <syslog.h>
#include <unistd.h>
#include <sys/stat.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <linux/if_link.h>

#include "xdp_loader.h"
#include "maps.h"

/* ── tunables ───────────────────────────────────────────────────────────── */

#define BPF_FS_DIR   "/sys/fs/bpf/nos"
#define MAX_IFACES   64

/* ── logging ────────────────────────────────────────────────────────────── */

#define xdp_info(fmt, ...)  syslog(LOG_INFO,    "xdp: " fmt, ##__VA_ARGS__)
#define xdp_warn(fmt, ...)  syslog(LOG_WARNING, "xdp: " fmt, ##__VA_ARGS__)
#define xdp_err(fmt, ...)   syslog(LOG_ERR,     "xdp: " fmt, ##__VA_ARGS__)

/* ── map pin table ──────────────────────────────────────────────────────── */

static const struct {
    const char *name;   /* matches the C variable name in xdp_prog.c */
    const char *pin;    /* path under BPF FS created by fib.c         */
} g_map_pins[] = {
    { "fib4_map",       BPF_FS_DIR "/fib4_map"       },
    { "fib6_map",       BPF_FS_DIR "/fib6_map"       },
    { "neigh_map",      BPF_FS_DIR "/neigh_map"       },
    { "vlan_map",       BPF_FS_DIR "/vlan_map"        },
    { "stats_map",      BPF_FS_DIR "/stats_map"       },
    { "local_ip4_map",  BPF_FS_DIR "/local_ip4_map"   },
    { "local_ip6_map",  BPF_FS_DIR "/local_ip6_map"   },
    { "port_vlan_map",  BPF_FS_DIR "/port_vlan_map"   },
};

#define N_MAPS  ((int)(sizeof(g_map_pins) / sizeof(g_map_pins[0])))

/* ── per-interface attachment record ────────────────────────────────────── */

struct iface_entry {
    int              ifindex;
    struct bpf_link *link;    /* non-NULL → link-based; destroy via bpf_link__destroy() */
    __u32            mode;    /* XDP_FLAGS_DRV_MODE or XDP_FLAGS_SKB_MODE                */
};

/* ── module state ───────────────────────────────────────────────────────── */

static struct bpf_object  *g_obj              = NULL;
static struct bpf_program *g_prog             = NULL;
static struct iface_entry  g_ifaces[MAX_IFACES];
static int                 g_nifaces          = 0;
static int                 g_port_vlan_map_fd = -1;

/* ── libbpf print redirect ──────────────────────────────────────────────── */

static int libbpf_log_cb(enum libbpf_print_level level,
                          const char *fmt, va_list args)
{
    if (level == LIBBPF_DEBUG)
        return 0;   /* suppress: too verbose for a daemon */

    char buf[256];
    vsnprintf(buf, sizeof(buf), fmt, args);
    /* strip trailing newline libbpf adds */
    int n = (int)strlen(buf);
    if (n > 0 && buf[n - 1] == '\n')
        buf[n - 1] = '\0';

    syslog(level == LIBBPF_WARN ? LOG_WARNING : LOG_INFO,
           "libbpf: %s", buf);
    return 0;
}

/* ── iface table helpers ────────────────────────────────────────────────── */

static struct iface_entry *iface_find(int ifindex)
{
    for (int i = 0; i < g_nifaces; i++)
        if (g_ifaces[i].ifindex == ifindex)
            return &g_ifaces[i];
    return NULL;
}

static struct iface_entry *iface_alloc(int ifindex)
{
    if (g_nifaces >= MAX_IFACES)
        return NULL;
    struct iface_entry *e = &g_ifaces[g_nifaces++];
    e->ifindex = ifindex;
    e->link    = NULL;
    e->mode    = 0;
    return e;
}

/* Remove entry at index idx; keeps array dense by swapping with tail. */
static void iface_remove_idx(int idx)
{
    g_ifaces[idx] = g_ifaces[--g_nifaces];
}

/* ── attach / detach helpers ────────────────────────────────────────────── */

/*
 * Attach the loaded XDP program to one interface using the three-tier strategy
 * described in the file header.  Caller can force generic-only by passing
 * XDP_FLAGS_SKB_MODE (skips tiers 1 and 2).
 */
static int xdp_attach_iface(int ifindex, unsigned int flags)
{
    char name[IF_NAMESIZE] = "?";
    if_indextoname((unsigned)ifindex, name);

    if (iface_find(ifindex)) {
        xdp_warn("ifindex %d (%s): already attached, skipping", ifindex, name);
        return 0;
    }

    struct iface_entry *e = iface_alloc(ifindex);
    if (!e) {
        xdp_err("attach table full (max %d interfaces)", MAX_IFACES);
        errno = ENOBUFS;
        return -1;
    }

    int alloc_idx = g_nifaces - 1;   /* saved so we can undo on total failure */
    int prog_fd   = bpf_program__fd(g_prog);
    int rc;

    if (!(flags & XDP_FLAGS_SKB_MODE)) {
        /* ── tier 1: link-based native XDP (BPF_LINK_CREATE) ── */
        xdp_info("%s (ifindex %d): trying tier-1 native XDP (BPF_LINK_CREATE)",
                 name, ifindex);
        struct bpf_link *link = bpf_program__attach_xdp(g_prog, ifindex);
        if (link) {
            xdp_info("%s (ifindex %d): attached — native mode (link-based)",
                     name, ifindex);
            e->link = link;
            e->mode = XDP_FLAGS_DRV_MODE;
            return 0;
        }
        long lerr = libbpf_get_error(link);
        xdp_warn("%s (ifindex %d): tier-1 BPF_LINK_CREATE failed (%s); "
                 "trying tier-2 legacy DRV_MODE",
                 name, ifindex, strerror((int)-lerr));

        /* ── tier 2: legacy netlink-based native XDP (XDP_FLAGS_DRV_MODE) ──
         *
         * This is the path used by "ip link set ... xdpdrv".  It goes through
         * RTM_NEWLINK / IFLA_XDP rather than BPF_LINK_CREATE, which avoids
         * any kernel-side restrictions specific to the link-based API.
         * i40e (Intel X710) and similar drivers that support native XDP but
         * return EOPNOTSUPP from BPF_LINK_CREATE land here.
         */
        xdp_info("%s (ifindex %d): trying tier-2 native XDP (bpf_xdp_attach DRV_MODE)",
                 name, ifindex);
        rc = bpf_xdp_attach(ifindex, prog_fd, XDP_FLAGS_DRV_MODE, NULL);
        if (rc == 0) {
            xdp_info("%s (ifindex %d): attached — native mode (legacy DRV_MODE)",
                     name, ifindex);
            e->link = NULL;
            e->mode = XDP_FLAGS_DRV_MODE;
            return 0;
        }
        xdp_warn("%s (ifindex %d): tier-2 DRV_MODE failed (%s); "
                 "falling back to tier-3 generic",
                 name, ifindex, strerror(-rc));
    }

    /* ── tier 3: generic (SKB / software) XDP ── */
    xdp_info("%s (ifindex %d): trying tier-3 generic XDP (bpf_xdp_attach SKB_MODE)",
             name, ifindex);
    rc = bpf_xdp_attach(ifindex, prog_fd, XDP_FLAGS_SKB_MODE, NULL);
    if (rc < 0) {
        xdp_err("%s (ifindex %d): all XDP attach tiers failed; last error: %s",
                name, ifindex, strerror(-rc));
        iface_remove_idx(alloc_idx);
        errno = -rc;
        return -1;
    }

    xdp_info("%s (ifindex %d): attached — generic mode (SKB_MODE)", name, ifindex);
    e->link = NULL;
    e->mode = XDP_FLAGS_SKB_MODE;
    return 0;
}

/*
 * Detach one entry and log the result.  Does NOT remove the entry from the
 * array; callers must call iface_remove_idx() afterward.
 */
static void detach_entry(struct iface_entry *e)
{
    char name[IF_NAMESIZE] = "?";
    if_indextoname((unsigned)e->ifindex, name);

    if (e->link) {
        /* Link-based attachment (native mode): destroy releases the kernel ref. */
        if (bpf_link__destroy(e->link) < 0)
            xdp_warn("bpf_link__destroy ifindex %d (%s): %s",
                     e->ifindex, name, strerror(errno));
        e->link = NULL;
    } else {
        /* Legacy fd-based attachment (DRV_MODE tier-2 or SKB_MODE tier-3). */
        int rc = bpf_xdp_detach(e->ifindex, e->mode, NULL);
        if (rc < 0)
            xdp_warn("bpf_xdp_detach ifindex %d (%s): %s",
                     e->ifindex, name, strerror(-rc));
    }

    xdp_info("detached from %s (ifindex %d)", name, e->ifindex);
}

/* ── public API ─────────────────────────────────────────────────────────── */

int xdp_loader_init(const char *obj_path)
{
    if (g_obj) {
        xdp_warn("xdp_loader_init: already initialized — call xdp_loader_cleanup first");
        return -1;
    }

    libbpf_set_print(libbpf_log_cb);

    /* Ensure the BPF FS directory exists (fib.c does this too; EEXIST is fine). */
    if (mkdir(BPF_FS_DIR, 0700) < 0 && errno != EEXIST)
        xdp_warn("mkdir %s: %s", BPF_FS_DIR, strerror(errno));

    /* Open the compiled BPF object (parses ELF, does not load into kernel yet). */
    struct bpf_object *obj = bpf_object__open(obj_path);
    if (!obj) {
        xdp_err("bpf_object__open(%s): %s", obj_path, strerror(errno));
        return -1;
    }

    /*
     * Wire up pinned maps before loading.  fib.c runs first (main.c guarantees
     * this) and has already created + pinned all five maps.  bpf_map__reuse_fd()
     * makes the BPF object reference those existing maps instead of creating
     * fresh ones, so the XDP fast path and the fib control path share state.
     */
    for (int i = 0; i < N_MAPS; i++) {
        struct bpf_map *map = bpf_object__find_map_by_name(obj, g_map_pins[i].name);
        if (!map) {
            xdp_warn("BPF object has no map named '%s'", g_map_pins[i].name);
            continue;
        }

        int pinned_fd = bpf_obj_get(g_map_pins[i].pin);
        if (pinned_fd < 0) {
            /* fib.c hasn't run yet or the pin is missing — create on load below. */
            xdp_warn("pin '%s' not found (%s): map will be created fresh",
                     g_map_pins[i].pin, strerror(errno));
            continue;
        }

        int rc = bpf_map__reuse_fd(map, pinned_fd);
        close(pinned_fd);   /* libbpf dup()'d it; we must release our copy */
        if (rc < 0)
            xdp_warn("bpf_map__reuse_fd '%s': %s — map will be recreated",
                     g_map_pins[i].name, strerror(errno));
        else
            xdp_info("reusing pinned map '%s'", g_map_pins[i].name);
    }

    /* Run the BPF verifier and finalise maps (create any that were not reused). */
    if (bpf_object__load(obj) < 0) {
        xdp_err("bpf_object__load(%s): %s", obj_path, strerror(errno));
        bpf_object__close(obj);
        return -1;
    }

    /*
     * Pin any maps that were just created (not reused above).  EEXIST means
     * fib.c already pinned them, which is the normal case; any other error is
     * worth logging but not fatal.
     */
    for (int i = 0; i < N_MAPS; i++) {
        struct bpf_map *map = bpf_object__find_map_by_name(obj, g_map_pins[i].name);
        if (!map)
            continue;
        int rc = bpf_obj_pin(bpf_map__fd(map), g_map_pins[i].pin);
        if (rc < 0 && errno != EEXIST)
            xdp_warn("bpf_obj_pin '%s': %s — map state will not survive restart",
                     g_map_pins[i].pin, strerror(errno));
    }

    /* Save port_vlan_map fd for runtime updates from userspace. */
    {
        struct bpf_map *pvm =
            bpf_object__find_map_by_name(obj, "port_vlan_map");
        if (pvm)
            g_port_vlan_map_fd = bpf_map__fd(pvm);
        else
            xdp_warn("port_vlan_map not found — port VLAN tagging unavailable");
    }

    /* Locate the forwarding program by its C function name. */
    struct bpf_program *prog = bpf_object__find_program_by_name(obj, "nos_xdp_fwd");
    if (!prog) {
        xdp_err("program 'nos_xdp_fwd' not found in %s", obj_path);
        bpf_object__close(obj);
        return -1;
    }

    g_obj     = obj;
    g_prog    = prog;
    g_nifaces = 0;

    xdp_info("initialized from %s", obj_path);
    return 0;
}

void xdp_loader_cleanup(void)
{
    if (!g_obj)
        return;

    xdp_loader_detach_all();

    /*
     * Close the BPF object.  This closes the program fd and any map fds the
     * object holds.  Pinned maps on the BPF FS are NOT unlinked here because
     * fib.c owns them and they should survive for restart re-use.
     */
    bpf_object__close(g_obj);
    g_obj              = NULL;
    g_prog             = NULL;
    g_nifaces          = 0;
    g_port_vlan_map_fd = -1;

    xdp_info("cleaned up");
}

int xdp_loader_attach(int ifindex, unsigned int flags)
{
    if (!g_prog) {
        xdp_err("xdp_loader_attach: not initialized");
        errno = EAGAIN;
        return -1;
    }
    return xdp_attach_iface(ifindex, flags);
}

int xdp_loader_detach(int ifindex)
{
    for (int i = 0; i < g_nifaces; i++) {
        if (g_ifaces[i].ifindex != ifindex)
            continue;
        detach_entry(&g_ifaces[i]);
        iface_remove_idx(i);
        return 0;
    }
    xdp_warn("xdp_loader_detach: ifindex %d not found in attach table", ifindex);
    errno = ENOENT;
    return -1;
}

int xdp_loader_attach_all(unsigned int flags)
{
    if (!g_prog) {
        xdp_err("xdp_loader_attach_all: not initialized");
        errno = EAGAIN;
        return -1;
    }

    struct if_nameindex *ifs = if_nameindex();
    if (!ifs) {
        xdp_err("if_nameindex: %s", strerror(errno));
        return -1;
    }

    int n_ok = 0, n_fail = 0;

    for (struct if_nameindex *it = ifs; it->if_index != 0; it++) {
        if (strcmp(it->if_name, "lo") == 0)
            continue;   /* never attach XDP to the loopback interface */

        if (xdp_attach_iface((int)it->if_index, flags) < 0)
            n_fail++;
        else
            n_ok++;
    }

    if_freenameindex(ifs);

    xdp_info("attach_all: %d attached, %d failed", n_ok, n_fail);
    return n_fail ? -1 : 0;
}

int xdp_loader_detach_all(void)
{
    /* Detach from index 0 each time; iface_remove_idx(0) compacts the array. */
    while (g_nifaces > 0) {
        detach_entry(&g_ifaces[0]);
        iface_remove_idx(0);
    }
    return 0;
}

int xdp_loader_port_vlan_set(__u32 ifindex, __u16 vlan_id, __u8 mode)
{
    if (g_port_vlan_map_fd < 0) {
        xdp_err("port_vlan_set: port_vlan_map not available");
        errno = EAGAIN;
        return -1;
    }
    struct port_vlan_val val = { .vlan_id = vlan_id, .mode = mode, ._pad = 0 };
    int rc = bpf_map_update_elem(g_port_vlan_map_fd, &ifindex, &val, BPF_ANY);
    if (rc < 0)
        xdp_err("port_vlan_set ifindex=%u: %s", ifindex, strerror(errno));
    return rc;
}

int xdp_loader_port_vlan_del(__u32 ifindex)
{
    if (g_port_vlan_map_fd < 0) {
        xdp_err("port_vlan_del: port_vlan_map not available");
        errno = EAGAIN;
        return -1;
    }
    int rc = bpf_map_delete_elem(g_port_vlan_map_fd, &ifindex);
    if (rc < 0 && errno != ENOENT)
        xdp_err("port_vlan_del ifindex=%u: %s", ifindex, strerror(errno));
    return rc;
}
