/* SPDX-License-Identifier: GPL-2.0-only */

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include "ipc.h"

#define ipc_err(fmt, ...)  syslog(LOG_ERR,     "ipc: " fmt, ##__VA_ARGS__)
#define ipc_warn(fmt, ...) syslog(LOG_WARNING,  "ipc: " fmt, ##__VA_ARGS__)
#define ipc_info(fmt, ...) syslog(LOG_INFO,     "ipc: " fmt, ##__VA_ARGS__)

int ipc_server_create(const char *sock_path)
{
    /* Remove a stale socket left from a previous crash. */
    if (unlink(sock_path) < 0 && errno != ENOENT)
        ipc_warn("unlink %s: %s", sock_path, strerror(errno));

    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        ipc_err("socket: %s", strerror(errno));
        return -1;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", sock_path)
            >= (int)sizeof(addr.sun_path)) {
        ipc_err("sock_path too long: %s", sock_path);
        close(fd);
        return -1;
    }

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        ipc_err("bind %s: %s", sock_path, strerror(errno));
        close(fd);
        return -1;
    }

    /* rw-rw---- so the 'nos' group can connect without root. */
    if (chmod(sock_path, 0660) < 0)
        ipc_warn("chmod %s: %s", sock_path, strerror(errno));

    if (listen(fd, 8) < 0) {
        ipc_err("listen %s: %s", sock_path, strerror(errno));
        unlink(sock_path);
        close(fd);
        return -1;
    }

    ipc_info("listening on %s fd=%d", sock_path, fd);
    return fd;
}

void ipc_server_close(int fd, const char *sock_path)
{
    if (fd >= 0)
        close(fd);
    if (sock_path)
        unlink(sock_path);
}

int ipc_send_reply(int client_fd, const char *json)
{
    size_t jlen  = strlen(json);
    size_t total = jlen + 1;   /* +1 for the '\n' framing byte */

    /* Avoid a heap allocation for the common case. */
    char  stack_buf[4096];
    char *buf = (total <= sizeof(stack_buf)) ? stack_buf : malloc(total);
    if (!buf) {
        ipc_err("ipc_send_reply: out of memory");
        return -1;
    }

    memcpy(buf, json, jlen);
    buf[jlen] = '\n';

    size_t sent = 0;
    int    rc   = 0;
    while (sent < total) {
        ssize_t n = write(client_fd, buf + sent, total - sent);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            ipc_err("write fd=%d: %s", client_fd, strerror(errno));
            rc = -1;
            break;
        }
        sent += (size_t)n;
    }

    if (buf != stack_buf)
        free(buf);
    return rc;
}
