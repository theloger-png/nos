/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef NOS_XDP_LOADER_H
#define NOS_XDP_LOADER_H

#include <stdint.h>
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

/* Insert or update an entry in port_vlan_map.
 * mode: 0 = access (XDP pushes a tag), 1 = trunk (pass through). */
int  xdp_loader_port_vlan_set(uint32_t ifindex, uint16_t vlan_id, uint8_t mode);

/* Remove an entry from port_vlan_map (ENOENT is silently ignored). */
int  xdp_loader_port_vlan_del(uint32_t ifindex);

#endif /* NOS_XDP_LOADER_H */
