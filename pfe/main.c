/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE

/*
 * pfe/main.c — PFE (Packet Forwarding Engine) process entry point.
 *
 * Responsibilities:
 *   - Write PID file, open syslog.
 *   - Load XDP programs onto all interfaces (generic/SKB mode by default).
 *   - Start the Unix-socket IPC server for JSON messages from the Python RE.
 *   - epoll event loop: signal fd, accept, per-client reads.
 *   - Dispatch JSON messages to fib.c / xdp_loader handlers.
 *   - Clean shutdown on SIGTERM/SIGINT; SIGHUP triggers XDP reload.
 *
 * Message framing: newline-delimited JSON (\n terminator, one reply per request).
 */

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <unistd.h>
#include <sys/epoll.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <linux/if_link.h>
#include <cjson/cJSON.h>
#include <systemd/sd-daemon.h>

#include "fib.h"
#include "ipc.h"
#include "xdp/xdp_loader.h"

/* ── tunables ───────────────────────────────────────────────────────────── */

#define PFE_SOCK_PATH     "/run/nos/pfe.sock"
#define PFE_PID_FILE      "/run/nos/pfe.pid"
#define PFE_XDP_OBJ       "/usr/lib/nos/xdp_prog.o"
#define PFE_RUN_DIR       "/run/nos"

#define MAX_EPOLL_EVENTS  64
#define MAX_CLIENTS       16
#define CLIENT_BUF_SIZE   (64u * 1024u)  /* 64 KiB: ample for any control message */

/* ── logging ────────────────────────────────────────────────────────────── */

#define log_info(fmt, ...) \
    do { syslog(LOG_INFO,    "pfe: " fmt, ##__VA_ARGS__); \
         fprintf(stderr, "INFO  pfe: " fmt "\n", ##__VA_ARGS__); } while (0)

#define log_warn(fmt, ...) \
    do { syslog(LOG_WARNING, "pfe: " fmt, ##__VA_ARGS__); \
         fprintf(stderr, "WARN  pfe: " fmt "\n", ##__VA_ARGS__); } while (0)

#define log_err(fmt, ...) \
    do { syslog(LOG_ERR,     "pfe: " fmt, ##__VA_ARGS__); \
         fprintf(stderr, "ERROR pfe: " fmt "\n", ##__VA_ARGS__); } while (0)

/* ── client state ───────────────────────────────────────────────────────── */

struct client {
    int    fd;
    char   buf[CLIENT_BUF_SIZE];
    size_t len;   /* bytes currently buffered */
};

/* ── globals ────────────────────────────────────────────────────────────── */

static int g_epfd      = -1;
static int g_server_fd = -1;
static int g_sigfd     = -1;
static int g_running   =  1;
static int g_reload    =  0;

static struct client g_clients[MAX_CLIENTS];

/* ── PID file ───────────────────────────────────────────────────────────── */

static int write_pid_file(void)
{
    if (mkdir(PFE_RUN_DIR, 0755) < 0 && errno != EEXIST) {
        log_warn("mkdir %s: %s", PFE_RUN_DIR, strerror(errno));
        /* non-fatal */
    }

    FILE *f = fopen(PFE_PID_FILE, "w");
    if (!f) {
        log_err("open %s: %s", PFE_PID_FILE, strerror(errno));
        return -1;
    }
    fprintf(f, "%d\n", getpid());
    fclose(f);
    return 0;
}

static void remove_pid_file(void)
{
    unlink(PFE_PID_FILE);
}

/* ── fd helpers ─────────────────────────────────────────────────────────── */

static int epoll_add(int epfd, int fd, uint32_t events)
{
    struct epoll_event ev = { .events = events, .data.fd = fd };
    return epoll_ctl(epfd, EPOLL_CTL_ADD, fd, &ev);
}

static int epoll_del(int epfd, int fd)
{
    return epoll_ctl(epfd, EPOLL_CTL_DEL, fd, NULL);
}

/* ── client table ───────────────────────────────────────────────────────── */

static struct client *client_alloc(int fd)
{
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (g_clients[i].fd < 0) {
            g_clients[i].fd  = fd;
            g_clients[i].len = 0;
            return &g_clients[i];
        }
    }
    return NULL;  /* table full */
}

static struct client *client_find(int fd)
{
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (g_clients[i].fd == fd)
            return &g_clients[i];
    }
    return NULL;
}

static void client_close(struct client *c)
{
    if (c->fd >= 0) {
        epoll_del(g_epfd, c->fd);
        close(c->fd);
        c->fd  = -1;
        c->len = 0;
    }
}

static void close_all_clients(void)
{
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (g_clients[i].fd >= 0)
            client_close(&g_clients[i]);
    }
}

/* ── reply helpers ──────────────────────────────────────────────────────── */

static int reply_ok(int fd)
{
    return ipc_send_reply(fd, "{\"status\":\"ok\"}");
}

static int reply_err(int fd, const char *message)
{
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "status",  "error");
    cJSON_AddStringToObject(r, "message", message);
    char *s = cJSON_PrintUnformatted(r);
    int rc = ipc_send_reply(fd, s);
    free(s);
    cJSON_Delete(r);
    return rc;
}

/* Takes ownership of data; caller must NOT cJSON_Delete it afterwards. */
static int reply_ok_data(int fd, cJSON *data)
{
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "status", "ok");
    cJSON_AddItemToObject(r, "data", data);   /* ownership transferred */
    char *s = cJSON_PrintUnformatted(r);
    int rc = ipc_send_reply(fd, s);
    free(s);
    cJSON_Delete(r);  /* also frees data */
    return rc;
}

/* ── message handlers ───────────────────────────────────────────────────── */

static void handle_fib_add(int fd, cJSON *msg)
{
    cJSON *jprefix  = cJSON_GetObjectItemCaseSensitive(msg, "prefix");
    cJSON *jnexthop = cJSON_GetObjectItemCaseSensitive(msg, "nexthop");
    cJSON *jifindex = cJSON_GetObjectItemCaseSensitive(msg, "ifindex");

    if (!cJSON_IsString(jprefix) || !cJSON_IsNumber(jifindex)) {
        reply_err(fd, "fib_add: required fields: prefix (string), ifindex (int)");
        return;
    }

    const char *nexthop = cJSON_IsString(jnexthop) ? jnexthop->valuestring : NULL;
    uint32_t    flags   = nexthop ? FIB_F_GATEWAY : 0;
    uint32_t    ifindex = (uint32_t)jifindex->valuedouble;

    if (fib_route_add(jprefix->valuestring, nexthop, ifindex, flags) < 0) {
        reply_err(fd, "fib_add: failed to insert route");
        return;
    }

    log_info("fib_add prefix=%s nexthop=%s ifindex=%u",
             jprefix->valuestring, nexthop ? nexthop : "direct", ifindex);
    reply_ok(fd);
}

static void handle_fib_del(int fd, cJSON *msg)
{
    cJSON *jprefix = cJSON_GetObjectItemCaseSensitive(msg, "prefix");

    if (!cJSON_IsString(jprefix)) {
        reply_err(fd, "fib_del: required field: prefix (string)");
        return;
    }

    if (fib_route_del(jprefix->valuestring) < 0) {
        reply_err(fd, "fib_del: prefix not found");
        return;
    }

    log_info("fib_del prefix=%s", jprefix->valuestring);
    reply_ok(fd);
}

static void handle_neigh_add(int fd, cJSON *msg)
{
    cJSON *jip      = cJSON_GetObjectItemCaseSensitive(msg, "ip");
    cJSON *jmac     = cJSON_GetObjectItemCaseSensitive(msg, "mac");
    cJSON *jifindex = cJSON_GetObjectItemCaseSensitive(msg, "ifindex");

    if (!cJSON_IsString(jip) || !cJSON_IsString(jmac) || !cJSON_IsNumber(jifindex)) {
        reply_err(fd, "neigh_add: required fields: ip, mac (strings), ifindex (int)");
        return;
    }

    uint32_t ifindex = (uint32_t)jifindex->valuedouble;
    if (fib_neigh_add(jip->valuestring, jmac->valuestring, ifindex) < 0) {
        reply_err(fd, "neigh_add: failed to insert neighbor");
        return;
    }

    log_info("neigh_add ip=%s mac=%s ifindex=%u",
             jip->valuestring, jmac->valuestring, ifindex);
    reply_ok(fd);
}

static void handle_neigh_del(int fd, cJSON *msg)
{
    cJSON *jip = cJSON_GetObjectItemCaseSensitive(msg, "ip");

    if (!cJSON_IsString(jip)) {
        reply_err(fd, "neigh_del: required field: ip (string)");
        return;
    }

    if (fib_neigh_del(jip->valuestring) < 0) {
        reply_err(fd, "neigh_del: entry not found");
        return;
    }

    log_info("neigh_del ip=%s", jip->valuestring);
    reply_ok(fd);
}

static void handle_vlan_set(int fd, cJSON *msg)
{
    cJSON *jvlan    = cJSON_GetObjectItemCaseSensitive(msg, "vlan_id");
    cJSON *jifindex = cJSON_GetObjectItemCaseSensitive(msg, "ifindex");

    if (!cJSON_IsNumber(jvlan) || !cJSON_IsNumber(jifindex)) {
        reply_err(fd, "vlan_set: required fields: vlan_id, ifindex (ints)");
        return;
    }

    uint16_t vlan_id = (uint16_t)jvlan->valuedouble;
    if (vlan_id < 1 || vlan_id > 4094) {
        reply_err(fd, "vlan_set: vlan_id out of range [1, 4094]");
        return;
    }

    if (fib_vlan_set(vlan_id, (uint32_t)jifindex->valuedouble) < 0) {
        reply_err(fd, "vlan_set: failed to set VLAN mapping");
        return;
    }

    reply_ok(fd);
}

static void handle_vlan_del(int fd, cJSON *msg)
{
    cJSON *jvlan = cJSON_GetObjectItemCaseSensitive(msg, "vlan_id");

    if (!cJSON_IsNumber(jvlan)) {
        reply_err(fd, "vlan_del: required field: vlan_id (int)");
        return;
    }

    if (fib_vlan_del((uint16_t)jvlan->valuedouble) < 0) {
        reply_err(fd, "vlan_del: entry not found");
        return;
    }

    reply_ok(fd);
}

static void handle_port_vlan_set(int fd, cJSON *msg)
{
    cJSON *jifindex = cJSON_GetObjectItemCaseSensitive(msg, "ifindex");
    cJSON *jvlan    = cJSON_GetObjectItemCaseSensitive(msg, "vlan_id");
    cJSON *jmode    = cJSON_GetObjectItemCaseSensitive(msg, "mode");

    if (!cJSON_IsNumber(jifindex) || !cJSON_IsNumber(jvlan) ||
        !cJSON_IsNumber(jmode)) {
        reply_err(fd, "port_vlan_set: required fields: ifindex, vlan_id, mode (ints)");
        return;
    }

    uint16_t vlan_id = (uint16_t)jvlan->valuedouble;
    if (vlan_id < 1 || vlan_id > 4094) {
        reply_err(fd, "port_vlan_set: vlan_id out of range [1, 4094]");
        return;
    }

    uint8_t mode = (uint8_t)jmode->valuedouble;
    if (mode > 1) {
        reply_err(fd, "port_vlan_set: mode must be 0 (access) or 1 (trunk)");
        return;
    }

    uint32_t ifindex = (uint32_t)jifindex->valuedouble;
    if (xdp_loader_port_vlan_set(ifindex, vlan_id, mode) < 0) {
        reply_err(fd, "port_vlan_set: failed to update port_vlan_map");
        return;
    }

    log_info("port_vlan_set ifindex=%u vlan_id=%u mode=%u", ifindex, vlan_id, mode);
    reply_ok(fd);
}

static void handle_xdp_attach(int fd, cJSON *msg)
{
    cJSON *jifindex = cJSON_GetObjectItemCaseSensitive(msg, "ifindex");
    cJSON *jmode    = cJSON_GetObjectItemCaseSensitive(msg, "mode");

    if (!cJSON_IsNumber(jifindex)) {
        reply_err(fd, "xdp_attach: required field: ifindex (int)");
        return;
    }

    int ifindex = (int)jifindex->valuedouble;

    /* mode: "native" → driver mode; anything else (incl. "generic") → SKB mode */
    unsigned int flags = XDP_FLAGS_SKB_MODE;
    if (cJSON_IsString(jmode) && strcmp(jmode->valuestring, "native") == 0)
        flags = XDP_FLAGS_DRV_MODE;

    if (xdp_loader_attach(ifindex, flags) < 0) {
        reply_err(fd, "xdp_attach: failed to attach XDP program");
        return;
    }

    log_info("xdp_attach ifindex=%d mode=%s", ifindex,
             flags == XDP_FLAGS_DRV_MODE ? "native" : "generic");
    reply_ok(fd);
}

static void handle_xdp_detach(int fd, cJSON *msg)
{
    cJSON *jifindex = cJSON_GetObjectItemCaseSensitive(msg, "ifindex");

    if (!cJSON_IsNumber(jifindex)) {
        reply_err(fd, "xdp_detach: required field: ifindex (int)");
        return;
    }

    if (xdp_loader_detach((int)jifindex->valuedouble) < 0) {
        reply_err(fd, "xdp_detach: failed to detach XDP program");
        return;
    }

    reply_ok(fd);
}

static void handle_stats_get(int fd, cJSON *msg)
{
    cJSON *jifindex = cJSON_GetObjectItemCaseSensitive(msg, "ifindex");

    if (!cJSON_IsNumber(jifindex)) {
        reply_err(fd, "stats_get: required field: ifindex (int)");
        return;
    }

    struct fib_stats s;
    if (fib_stats_get((uint32_t)jifindex->valuedouble, &s) < 0) {
        reply_err(fd, "stats_get: interface not found");
        return;
    }

    cJSON *data = cJSON_CreateObject();
    cJSON_AddNumberToObject(data, "rx_packets", (double)s.rx_packets);
    cJSON_AddNumberToObject(data, "rx_bytes",   (double)s.rx_bytes);
    cJSON_AddNumberToObject(data, "tx_packets", (double)s.tx_packets);
    cJSON_AddNumberToObject(data, "tx_bytes",   (double)s.tx_bytes);
    reply_ok_data(fd, data);  /* data ownership transferred */
}

/* ── dispatch ───────────────────────────────────────────────────────────── */

static void dispatch(int fd, const char *raw, size_t len)
{
    cJSON *msg = cJSON_ParseWithLength(raw, len);
    if (!msg) {
        log_warn("fd=%d: JSON parse error in: %.80s", fd, raw);
        reply_err(fd, "invalid JSON");
        return;
    }

    cJSON *jtype = cJSON_GetObjectItemCaseSensitive(msg, "type");
    if (!cJSON_IsString(jtype)) {
        reply_err(fd, "missing or non-string field: type");
        cJSON_Delete(msg);
        return;
    }

    const char *t = jtype->valuestring;

    if      (strcmp(t, "fib_add")    == 0) handle_fib_add(fd, msg);
    else if (strcmp(t, "fib_del")    == 0) handle_fib_del(fd, msg);
    else if (strcmp(t, "neigh_add")  == 0) handle_neigh_add(fd, msg);
    else if (strcmp(t, "neigh_del")  == 0) handle_neigh_del(fd, msg);
    else if (strcmp(t, "vlan_set")   == 0) handle_vlan_set(fd, msg);
    else if (strcmp(t, "vlan_del")   == 0) handle_vlan_del(fd, msg);
    else if (strcmp(t, "port_vlan_set") == 0) handle_port_vlan_set(fd, msg);
    else if (strcmp(t, "xdp_attach") == 0) handle_xdp_attach(fd, msg);
    else if (strcmp(t, "xdp_detach") == 0) handle_xdp_detach(fd, msg);
    else if (strcmp(t, "stats_get")  == 0) handle_stats_get(fd, msg);
    else if (strcmp(t, "ping")       == 0) reply_ok(fd);
    else {
        log_warn("fd=%d: unknown message type: %s", fd, t);
        reply_err(fd, "unknown message type");
    }

    cJSON_Delete(msg);
}

/* ── I/O event handlers ─────────────────────────────────────────────────── */

static void handle_accept(void)
{
    /* Drain all pending connections (edge-triggered). */
    for (;;) {
        int cfd = accept4(g_server_fd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (cfd < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK)
                return;
            log_warn("accept4: %s", strerror(errno));
            return;
        }

        struct client *c = client_alloc(cfd);
        if (!c) {
            log_warn("client table full, rejecting fd=%d", cfd);
            close(cfd);
            continue;
        }

        if (epoll_add(g_epfd, cfd, EPOLLIN | EPOLLET) < 0) {
            log_err("epoll_add client fd=%d: %s", cfd, strerror(errno));
            client_close(c);
            continue;
        }

        log_info("client connected fd=%d", cfd);
    }
}

/*
 * Read and process all available data from an edge-triggered client fd.
 * Returns 0 normally, -1 when the client should be closed.
 */
static int handle_client(struct client *c)
{
    for (;;) {
        size_t space = CLIENT_BUF_SIZE - c->len - 1;  /* -1 keeps room for NUL */
        if (space == 0) {
            /* Buffer full without a newline — malformed oversized message. */
            log_warn("fd=%d: read buffer overflow, disconnecting", c->fd);
            return -1;
        }

        ssize_t n = read(c->fd, c->buf + c->len, space);
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK)
                break;   /* exhausted for now */
            log_warn("fd=%d: read: %s", c->fd, strerror(errno));
            return -1;
        }
        if (n == 0)
            return -1;   /* clean disconnect */

        c->len += (size_t)n;

        /* Process every complete (newline-terminated) message in the buffer. */
        char *start = c->buf;
        char *nl;
        while ((nl = memchr(start, '\n',
                            c->len - (size_t)(start - c->buf))) != NULL) {
            *nl = '\0';
            size_t mlen = (size_t)(nl - start);
            if (mlen > 0)
                dispatch(c->fd, start, mlen);
            start = nl + 1;
        }

        /* Shift any partial message to the front. */
        size_t remaining = c->len - (size_t)(start - c->buf);
        if (remaining > 0 && start != c->buf)
            memmove(c->buf, start, remaining);
        c->len = remaining;
    }
    return 0;
}

/* ── signal handling ────────────────────────────────────────────────────── */

static void handle_signal_fd(void)
{
    struct signalfd_siginfo si;
    ssize_t n = read(g_sigfd, &si, sizeof(si));
    if (n != (ssize_t)sizeof(si))
        return;

    switch ((int)si.ssi_signo) {
    case SIGTERM:
    case SIGINT:
        log_info("received signal %d — stopping", si.ssi_signo);
        g_running = 0;
        break;
    case SIGHUP:
        log_info("received SIGHUP — scheduling XDP reload");
        g_reload = 1;
        break;
    }
}

/* ── reload ─────────────────────────────────────────────────────────────── */

static void do_reload(const char *xdp_obj)
{
    log_info("reload: detaching XDP programs");
    xdp_loader_detach_all();
    xdp_loader_cleanup();

    if (xdp_loader_init(xdp_obj) < 0) {
        log_err("reload: xdp_loader_init failed — XDP offline until next reload");
        return;
    }
    if (xdp_loader_attach_all(XDP_FLAGS_SKB_MODE) < 0)
        log_warn("reload: one or more interfaces failed XDP attach");

    log_info("reload complete");
}

/* ── setup ──────────────────────────────────────────────────────────────── */

static int setup_signal_fd(void)
{
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGTERM);
    sigaddset(&mask, SIGINT);
    sigaddset(&mask, SIGHUP);

    /* Block the signals so they arrive only via the signalfd. */
    if (sigprocmask(SIG_BLOCK, &mask, NULL) < 0) {
        log_err("sigprocmask: %s", strerror(errno));
        return -1;
    }

    int sfd = signalfd(-1, &mask, SFD_NONBLOCK | SFD_CLOEXEC);
    if (sfd < 0) {
        log_err("signalfd: %s", strerror(errno));
        return -1;
    }
    return sfd;
}

/* ── main ───────────────────────────────────────────────────────────────── */

int main(int argc, char *argv[])
{
    const char *xdp_obj = PFE_XDP_OBJ;

    /* Allow overriding the XDP object path for development/testing. */
    if (argc == 3 && strcmp(argv[1], "--xdp-obj") == 0)
        xdp_obj = argv[2];

    openlog("nos-pfe", LOG_PID | LOG_NDELAY, LOG_DAEMON);
    log_info("starting (pid %d)", getpid());

    /* Initialise client table. */
    for (int i = 0; i < MAX_CLIENTS; i++)
        g_clients[i].fd = -1;

    /* Track which subsystems started successfully so cleanup is correct. */
    int fib_up    = 0;
    int xdp_up    = 0;
    int exit_code = EXIT_SUCCESS;

    /* ── PID file ── */
    if (write_pid_file() < 0)
        log_warn("could not write PID file — continuing");

    /* ── FIB / BPF maps ── */
    if (fib_init() < 0) {
        log_err("fib_init failed — aborting");
        exit_code = EXIT_FAILURE;
        goto cleanup_pid;
    }
    fib_up = 1;

    /* ── XDP programs (non-fatal: VM environments may not support XDP) ── */
    if (xdp_loader_init(xdp_obj) < 0) {
        log_warn("xdp_loader_init failed — XDP unavailable, kernel forwarding active");
    } else {
        if (xdp_loader_attach_all(XDP_FLAGS_SKB_MODE) < 0)
            log_warn("one or more interfaces failed XDP attach");
        xdp_up = 1;
    }

    /* ── IPC server socket ── */
    g_server_fd = ipc_server_create(PFE_SOCK_PATH);
    if (g_server_fd < 0) {
        exit_code = EXIT_FAILURE;
        goto cleanup_xdp;
    }

    /* ── signalfd ── */
    g_sigfd = setup_signal_fd();
    if (g_sigfd < 0) {
        exit_code = EXIT_FAILURE;
        goto cleanup_sock;
    }

    /* ── epoll ── */
    g_epfd = epoll_create1(EPOLL_CLOEXEC);
    if (g_epfd < 0) {
        log_err("epoll_create1: %s", strerror(errno));
        exit_code = EXIT_FAILURE;
        goto cleanup_sigfd;
    }

    if (epoll_add(g_epfd, g_sigfd, EPOLLIN) < 0 ||
        epoll_add(g_epfd, g_server_fd, EPOLLIN | EPOLLET) < 0) {
        log_err("epoll_add: %s", strerror(errno));
        exit_code = EXIT_FAILURE;
        goto cleanup_epfd;
    }

    log_info("ready — listening on %s", PFE_SOCK_PATH);
    sd_notify(0, "READY=1");

    /* ── event loop ── */

    struct epoll_event events[MAX_EPOLL_EVENTS];

    while (g_running) {
        if (g_reload) {
            g_reload = 0;
            do_reload(xdp_obj);
        }

        int nfds = epoll_wait(g_epfd, events, MAX_EPOLL_EVENTS, 1000 /* ms */);
        if (nfds < 0) {
            if (errno == EINTR)
                continue;
            log_err("epoll_wait: %s", strerror(errno));
            break;
        }

        for (int i = 0; i < nfds; i++) {
            int fd = events[i].data.fd;

            if (fd == g_sigfd) {
                handle_signal_fd();
                continue;
            }

            if (fd == g_server_fd) {
                handle_accept();
                continue;
            }

            struct client *c = client_find(fd);
            if (!c) {
                log_warn("epoll event on unknown fd=%d", fd);
                continue;
            }

            if ((events[i].events & (EPOLLHUP | EPOLLERR)) ||
                handle_client(c) < 0) {
                log_info("client disconnected fd=%d", c->fd);
                client_close(c);
            }
        }
    }

    /* ── clean shutdown ── */

    log_info("shutting down");
    close_all_clients();

cleanup_epfd:
    close(g_epfd);
    g_epfd = -1;
cleanup_sigfd:
    close(g_sigfd);
    g_sigfd = -1;
cleanup_sock:
    ipc_server_close(g_server_fd, PFE_SOCK_PATH);
    g_server_fd = -1;
cleanup_xdp:
    if (xdp_up) {
        xdp_loader_detach_all();
        xdp_loader_cleanup();
    }
    if (fib_up)
        fib_destroy();
cleanup_pid:
    remove_pid_file();
    closelog();

    return exit_code;
}
