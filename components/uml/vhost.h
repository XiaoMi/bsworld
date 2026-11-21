/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright 2026 Xiaomi Corporation */

#ifndef BSWORLD_UML_VHOST_H
#define BSWORLD_UML_VHOST_H

#include <poll.h>
#include <stdbool.h>
#include <stddef.h>
#include <sys/types.h>

#define BSW_VHOST_API __attribute__((visibility("default")))

struct vhost;

struct bsw_vhost_state {
	bool interrupt_ready;
	bool interrupt_pending;
};

/* Open a Virtio Bluetooth (40) or console (3) device on a Unix socket. */
BSW_VHOST_API struct vhost *bsw_vhost_open(const char *path,
					   unsigned int device_id);
BSW_VHOST_API void bsw_vhost_close(struct vhost *vhost);

/* Passing NULL for fds updates only the state and returns zero. */
BSW_VHOST_API size_t bsw_vhost_poll(const struct vhost *vhost,
				    struct pollfd fds[3],
				    struct bsw_vhost_state *state);
/* Handle one message per ready socket with level-triggered polling.
 * Return 1 when the guest has announced output, 0 otherwise, or -1 on error.
 */
BSW_VHOST_API int bsw_vhost_service(struct vhost *vhost,
				    const struct pollfd *fds, size_t count);

/* EAGAIN means the queue has no available descriptor. On EMSGSIZE, read leaves
 * the descriptor in place and reports its length through required, if supplied.
 */
BSW_VHOST_API ssize_t bsw_vhost_read(struct vhost *vhost, void *data,
				     size_t capacity, size_t *required);
/* A Bluetooth packet must fit in one chain. Console input may be partial. */
BSW_VHOST_API ssize_t bsw_vhost_write(struct vhost *vhost, const void *data,
				      size_t length);

/* Queue notifications at the caller's simulated time. Service reads the ACKs. */
BSW_VHOST_API int bsw_vhost_deliver_interrupts(struct vhost *vhost);
BSW_VHOST_API const char *bsw_vhost_error(const struct vhost *vhost);

#endif
