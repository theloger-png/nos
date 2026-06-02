/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef NOS_IPC_H
#define NOS_IPC_H

#include <stddef.h>

/*
 * Unix-socket IPC server — socket lifecycle helpers used by main.c.
 *
 * Protocol: newline-delimited JSON (\n terminator).
 * Each request gets exactly one response line.
 */

/* Create, bind and listen on a SOCK_STREAM Unix socket at sock_path.
 * An existing socket file is removed before binding.
 * Returns the listening fd (O_NONBLOCK | O_CLOEXEC), or -1 on error. */
int ipc_server_create(const char *sock_path);

/* Close the server fd and unlink sock_path. */
void ipc_server_close(int fd, const char *sock_path);

/* Write a newline-terminated reply to client_fd.
 * json must be a valid JSON string (no embedded newlines).
 * Retries on EINTR; returns 0 on success, -1 on error. */
int ipc_send_reply(int client_fd, const char *json);

#endif /* NOS_IPC_H */
