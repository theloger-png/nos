/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef NOS_XDP_LOADER_H
#define NOS_XDP_LOADER_H

#include <linux/if_link.h>   /* XDP_FLAGS_SKB_MODE, XDP_FLAGS_DRV_MODE */

/*
 * Loads a compiled XDP BPF object and manages program attachment across
 * interfaces.  All functions return 0 on success, -1 on error with errno set.
 */

/* Load the BPF object file.  Must be called before attach operations. */
int  xdp_loader_init(const char *obj_path);

/* Detach all programs and release the BPF object. */
void xdp_loader_cleanup(void);

/* Attach the XDP program to a single interface.
 * flags: XDP_FLAGS_SKB_MODE (generic) or XDP_FLAGS_DRV_MODE (native). */
int  xdp_loader_attach(int ifindex, unsigned int flags);

/* Detach the XDP program from a single interface. */
int  xdp_loader_detach(int ifindex);

/* Attach to every non-loopback interface currently visible in the netns. */
int  xdp_loader_attach_all(unsigned int flags);

/* Detach from every interface that has this program attached. */
int  xdp_loader_detach_all(void);

#endif /* NOS_XDP_LOADER_H */
