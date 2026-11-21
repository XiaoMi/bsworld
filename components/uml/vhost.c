// SPDX-License-Identifier: Apache-2.0
/* Copyright 2026 Xiaomi Corporation */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#ifndef _FILE_OFFSET_BITS
#define _FILE_OFFSET_BITS 64
#endif

#include "vhost.h"

#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/virtio_bt.h>
#include <linux/virtio_config.h>
#include <linux/virtio_ids.h>
#include <linux/virtio_ring.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

enum {
	HEADER_SIZE = 12,
	STATE_SIZE = 8,
	CONFIG_SIZE = 12,
	MAX_PAYLOAD = 512,
	MAX_QUEUE_SIZE = 256,
	NUM_QUEUES = 2,

	VERSION = 1,
	REPLY = 1 << 2,
	NEED_REPLY = 1 << 3,
	BACKEND_VRING_CALL = 4,
	VHOST_USER_F_PROTOCOL_FEATURES = 30,
	VHOST_USER_PROTOCOL_F_REPLY_ACK = 3,
	VHOST_USER_PROTOCOL_F_BACKEND_REQ = 5,
	VHOST_USER_PROTOCOL_F_CONFIG = 9,
	VHOST_USER_PROTOCOL_F_INBAND_NOTIFICATIONS = 14,
};

#define FEATURE(bit) (UINT64_C(1) << (bit))

static const uint64_t features = FEATURE(VHOST_USER_F_PROTOCOL_FEATURES) |
				 FEATURE(VIRTIO_F_VERSION_1);
static const uint64_t notify_features =
	FEATURE(VHOST_USER_PROTOCOL_F_REPLY_ACK) |
	FEATURE(VHOST_USER_PROTOCOL_F_BACKEND_REQ) |
	FEATURE(VHOST_USER_PROTOCOL_F_INBAND_NOTIFICATIONS);

enum request {
	GET_FEATURES = 1,
	SET_FEATURES = 2,
	SET_OWNER = 3,
	SET_MEM_TABLE = 5,
	SET_VRING_NUM = 8,
	SET_VRING_ADDR = 9,
	SET_VRING_BASE = 10,
	GET_VRING_BASE = 11,
	GET_PROTOCOL_FEATURES = 15,
	SET_PROTOCOL_FEATURES = 16,
	SET_VRING_ENABLE = 18,
	SET_BACKEND_REQ_FD = 21,
	GET_CONFIG = 24,
	VRING_KICK = 35,
};

struct channel {
	int fd;

	uint8_t incoming[HEADER_SIZE + MAX_PAYLOAD];
	size_t received;
	int received_fd;

	uint8_t *outgoing;
	size_t queued;
	size_t capacity;
};

struct message {
	uint32_t request;
	uint32_t flags;
	const uint8_t *payload;
	size_t length;
	int fd;
};

struct memory {
	uint8_t *base;
	size_t length;
	uint64_t guest_base;
	uint64_t user_base;
};

struct queue {
	unsigned int size;
	uint16_t next_available;
	size_t desc;
	size_t avail;
	size_t used;
	bool mapped;
	bool enabled;
	bool started;
};

struct segment {
	size_t offset;
	size_t length;
};

struct chain {
	struct segment segments[MAX_QUEUE_SIZE];
	unsigned int count;
	uint16_t head;
	size_t length;
};

struct device_profile {
	unsigned int output_queue;
	unsigned int input_queue;
	const uint8_t *config;
	size_t config_size;
	bool notify_on_output;
	bool stream_input;
};

/* Primary Bluetooth Controller without vendor or Microsoft extensions. */
static const struct virtio_bt_config bluetooth_config = {
	.type = VIRTIO_BT_CONFIG_TYPE_PRIMARY,
	.vendor = VIRTIO_BT_CONFIG_VENDOR_NONE,
};

static const struct device_profile bluetooth = {
	.output_queue = 0,
	.input_queue = 1,
	.config = (const uint8_t *)&bluetooth_config,
	.config_size = sizeof(bluetooth_config),
	.notify_on_output = true,
};

static const struct device_profile console = {
	/* hvc polls the used ring for output completions. */
	.output_queue = 1,
	.input_queue = 0,
	.stream_input = true,
};

struct vhost {
	int listener;
	char *path;
	bool bound;
	const struct device_profile *device;

	struct channel connection;
	struct channel calls;
	struct memory memory;
	struct queue queues[NUM_QUEUES];

	bool calling;
	unsigned int interrupts;
	unsigned int pending_calls[NUM_QUEUES];
	size_t pending_count;

	uint64_t protocol_features;
	const char *error;
};

static int fail(struct vhost *vhost, int code, const char *message)
{
	vhost->error = message;
	errno = code;
	return -1;
}

static bool notification_pending(const struct vhost *vhost)
{
	return vhost->calling || vhost->pending_count;
}

/* vhost-user fields use native byte order. Virtqueue fields are little-endian. */
static uint16_t load_le16(const uint8_t *data)
{
	uint16_t value;

	memcpy(&value, data, sizeof(value));
	return le16toh(value);
}

static uint32_t load32(const uint8_t *data)
{
	uint32_t value;

	memcpy(&value, data, sizeof(value));
	return value;
}

static uint64_t load64(const uint8_t *data)
{
	uint64_t value;

	memcpy(&value, data, sizeof(value));
	return value;
}

static void store32(uint8_t *data, uint32_t value)
{
	memcpy(data, &value, sizeof(value));
}

static void store64(uint8_t *data, uint64_t value)
{
	memcpy(data, &value, sizeof(value));
}

static void channel_close(struct channel *channel)
{
	if (channel->fd >= 0)
		close(channel->fd);
	if (channel->received_fd >= 0)
		close(channel->received_fd);
	free(channel->outgoing);
}

static int channel_read(struct vhost *vhost, struct channel *channel,
			size_t needed)
{
	struct iovec data = {
		.iov_base = channel->incoming + channel->received,
		.iov_len = needed - channel->received,
	};
	union {
		struct cmsghdr alignment;
		uint8_t bytes[CMSG_SPACE(sizeof(int))];
	} control;
	struct msghdr header = {
		.msg_iov = &data,
		.msg_iovlen = 1,
		.msg_control = control.bytes,
		.msg_controllen = sizeof(control.bytes),
	};
	ssize_t received;

	do {
		received = recvmsg(channel->fd, &header, MSG_CMSG_CLOEXEC);
	} while (received < 0 && errno == EINTR);
	if (received < 0)
		return errno == EAGAIN ? 0 : -1;
	if (!received)
		return fail(vhost, EPIPE,
			    "UML closed its vhost-user connection");

	bool invalid_fds = header.msg_flags & MSG_CTRUNC;

	for (struct cmsghdr *cmsg = CMSG_FIRSTHDR(&header); cmsg;
	     cmsg = CMSG_NXTHDR(&header, cmsg)) {
		if (cmsg->cmsg_level != SOL_SOCKET ||
		    cmsg->cmsg_type != SCM_RIGHTS)
			continue;
		size_t length = cmsg->cmsg_len - CMSG_LEN(0);
		const uint8_t *payload = CMSG_DATA(cmsg);

		for (size_t offset = 0; offset < length;
		     offset += sizeof(int)) {
			int fd;

			memcpy(&fd, payload + offset, sizeof(fd));
			/* Padding in the control buffer can hold an extra descriptor. */
			if (channel->received_fd >= 0) {
				close(fd);
				invalid_fds = true;
			} else {
				channel->received_fd = fd;
			}
		}
	}
	if (invalid_fds)
		return fail(vhost, EPROTO,
			    "unexpected vhost-user file descriptors");

	channel->received += (size_t)received;
	return 1;
}

/* Never read past one message boundary. SCM_RIGHTS can be sent with a partial
 * frame, so retain its descriptor until the payload arrives.
 */
static int channel_receive(struct vhost *vhost, struct channel *channel,
			   struct message *message)
{
	while (true) {
		size_t needed = HEADER_SIZE;

		if (channel->received >= HEADER_SIZE) {
			size_t length = load32(channel->incoming + 8);

			if (length > MAX_PAYLOAD)
				return fail(vhost, EPROTO,
					    "vhost-user message is too large");
			needed += length;
			if (channel->received == needed) {
				*message = (struct message){
					.request = load32(channel->incoming),
					.flags = load32(channel->incoming + 4),
					.payload =
						channel->incoming + HEADER_SIZE,
					.length = length,
					.fd = channel->received_fd,
				};
				channel->received = 0;
				channel->received_fd = -1;
				return 1;
			}
		}

		int result = channel_read(vhost, channel, needed);

		if (result <= 0)
			return result;
	}
}

static int channel_send(struct channel *channel, uint32_t request,
			uint32_t flags, const void *payload, size_t length)
{
	size_t size = HEADER_SIZE + length;

	if (channel->queued > SIZE_MAX - size) {
		errno = ENOMEM;
		return -1;
	}

	size_t total = channel->queued + size;

	if (total > channel->capacity) {
		size_t capacity = total < 256 ? 256 : total;
		uint8_t *outgoing = realloc(channel->outgoing, capacity);

		if (!outgoing)
			return -1;
		channel->outgoing = outgoing;
		channel->capacity = capacity;
	}

	uint8_t *header = channel->outgoing + channel->queued;

	store32(header, request);
	store32(header + 4, flags);
	store32(header + 8, (uint32_t)length);
	memcpy(header + HEADER_SIZE, payload, length);
	channel->queued = total;
	return 0;
}

static int channel_flush(struct channel *channel)
{
	if (!channel->queued)
		return 0;

	ssize_t sent;

	do {
		sent = send(channel->fd, channel->outgoing, channel->queued,
			    MSG_NOSIGNAL);
	} while (sent < 0 && errno == EINTR);
	if (sent < 0)
		return errno == EAGAIN ? 0 : -1;
	if (!sent) {
		errno = EPIPE;
		return -1;
	}

	channel->queued -= (size_t)sent;
	if (channel->queued)
		memmove(channel->outgoing, channel->outgoing + sent,
			channel->queued);
	return 0;
}

static bool translate(const struct memory *memory, uint64_t address,
		      uint64_t length, bool user, size_t *offset)
{
	uint64_t base = user ? memory->user_base : memory->guest_base;

	if (address < base || address - base > memory->length ||
	    length > memory->length - (address - base))
		return false;

	*offset = (size_t)(address - base);
	return true;
}

static int set_memory(struct vhost *vhost, const struct message *message)
{
	const uint8_t *payload = message->payload;

	if (message->length != STATE_SIZE + 4 * sizeof(uint64_t) ||
	    load32(payload) != 1 || notification_pending(vhost) ||
	    vhost->queues[0].enabled || vhost->queues[1].enabled)
		return fail(
			vhost, EPROTO,
			"expected one UML memory region before enabling queues");

	uint64_t length = load64(payload + 16);
	uint64_t offset = load64(payload + 32);
	struct stat info;

	if (fstat(message->fd, &info) < 0)
		return -1;
	if (!length || length > SIZE_MAX || info.st_size < 0 ||
	    offset > (uint64_t)info.st_size ||
	    length > (uint64_t)info.st_size - offset)
		return fail(vhost, EPROTO, "invalid UML memory region size");

	void *map = mmap(NULL, (size_t)length, PROT_READ | PROT_WRITE,
			 MAP_SHARED, message->fd, (off_t)offset);
	if (map == MAP_FAILED)
		return -1;

	if (vhost->memory.length) {
		munmap(vhost->memory.base, vhost->memory.length);
		memset(vhost->queues, 0, sizeof(vhost->queues));
		vhost->interrupts = 0;
	}
	vhost->memory = (struct memory){
		.base = map,
		.length = (size_t)length,
		.guest_base = load64(payload + 8),
		.user_base = load64(payload + 24),
	};
	return 0;
}

static int set_queue_addresses(struct vhost *vhost,
			       const struct message *message)
{
	const uint8_t *payload = message->payload;

	if (message->length != STATE_SIZE + 4 * sizeof(uint64_t) ||
	    load32(payload) >= NUM_QUEUES || load32(payload + 4) ||
	    !vhost->memory.length)
		return fail(vhost, EPROTO, "invalid virtqueue addresses");

	struct queue *queue = &vhost->queues[load32(payload)];
	size_t desc, used, avail;

	if (!queue->size ||
	    !translate(&vhost->memory, load64(payload + 8),
		       sizeof(struct vring_desc) * queue->size, true, &desc) ||
	    !translate(&vhost->memory, load64(payload + 16),
		       offsetof(struct vring_used, ring) +
			       sizeof(struct vring_used_elem) * queue->size,
		       true, &used) ||
	    !translate(&vhost->memory, load64(payload + 24),
		       offsetof(struct vring_avail, ring) +
			       sizeof(uint16_t) * queue->size,
		       true, &avail) ||
	    desc % VRING_DESC_ALIGN_SIZE || avail % VRING_AVAIL_ALIGN_SIZE ||
	    used % VRING_USED_ALIGN_SIZE)
		return fail(vhost, EPROTO, "invalid virtqueue ring layout");

	queue->desc = desc;
	queue->avail = avail;
	queue->used = used;
	queue->mapped = true;
	return 0;
}

static int queue_state(struct vhost *vhost, const struct message *message,
		       uint8_t *reply, size_t *reply_len, unsigned int *kicks)
{
	if (message->length != STATE_SIZE ||
	    load32(message->payload) >= NUM_QUEUES)
		return fail(vhost, EPROTO,
			    "invalid virtqueue index or state length");

	unsigned int index = load32(message->payload);
	uint32_t value = load32(message->payload + 4);
	struct queue *queue = &vhost->queues[index];

	switch (message->request) {
	case SET_VRING_NUM:
		if (!value || value > MAX_QUEUE_SIZE || (value & (value - 1)) ||
		    (queue->enabled && value != queue->size))
			return fail(vhost, EPROTO, "invalid virtqueue size");
		if (value != queue->size) {
			queue->mapped = false;
			queue->started = false;
			queue->size = value;
		}
		break;
	case SET_VRING_BASE:
		if (value > UINT16_MAX)
			return fail(vhost, EPROTO, "invalid virtqueue base");
		queue->next_available = (uint16_t)value;
		break;
	case GET_VRING_BASE:
		queue->started = false;
		store32(reply, index);
		store32(reply + 4, queue->next_available);
		*reply_len = STATE_SIZE;
		break;
	case SET_VRING_ENABLE:
		if (value > 1 || (value && !queue->mapped))
			return fail(vhost, EPROTO,
				    "invalid virtqueue enable state");
		queue->enabled = value;
		break;
	case VRING_KICK:
		if (value || !queue->enabled)
			return fail(vhost, EPROTO, "invalid virtqueue kick");
		queue->started = true;
		*kicks |= 1u << index;
		break;
	default:
		return fail(vhost, EPROTO, "unsupported vhost-user request");
	}
	return 0;
}

static int set_call_channel(struct vhost *vhost, struct message *message)
{
	if (message->length || vhost->calls.fd >= 0)
		return fail(vhost, EPROTO,
			    "invalid vhost-user request channel setup");

	int type;
	socklen_t size = sizeof(type);

	if (getsockopt(message->fd, SOL_SOCKET, SO_TYPE, &type, &size) < 0)
		return -1;
	if (type != SOCK_STREAM)
		return fail(vhost, EPROTO,
			    "vhost-user request channel is not a stream");

	int flags = fcntl(message->fd, F_GETFL);

	if (flags < 0 || fcntl(message->fd, F_SETFL, flags | O_NONBLOCK) < 0)
		return -1;
	vhost->calls.fd = message->fd;
	message->fd = -1;
	return 0;
}

static int get_config(struct vhost *vhost, const struct message *message,
		      uint8_t *reply, size_t *reply_len)
{
	const struct device_profile *device = vhost->device;

	if (message->length < CONFIG_SIZE || !device->config)
		return fail(vhost, EPROTO, "invalid virtio device config read");

	uint32_t offset = load32(message->payload);
	uint32_t size = load32(message->payload + 4);

	if (offset > device->config_size ||
	    size > device->config_size - offset ||
	    message->length - CONFIG_SIZE != size)
		return fail(vhost, EPROTO, "invalid virtio device config read");

	memcpy(reply, message->payload, CONFIG_SIZE);
	memcpy(reply + CONFIG_SIZE, device->config + offset, size);
	*reply_len = CONFIG_SIZE + size;
	return 0;
}

static int dispatch(struct vhost *vhost, struct message *message,
		    uint8_t *reply, size_t *reply_len, unsigned int *kicks)
{
	size_t length = message->length;
	uint32_t request = message->request;

	switch (request) {
	case GET_FEATURES:
	case GET_PROTOCOL_FEATURES:
	case SET_OWNER:
		if (length)
			return fail(vhost, EPROTO,
				    "unexpected vhost-user payload");
		if (request != SET_OWNER) {
			uint64_t value = request == GET_FEATURES ?
						 features :
						 vhost->protocol_features;
			store64(reply, value);
			*reply_len = sizeof(value);
		}
		return 0;
	case SET_FEATURES:
	case SET_PROTOCOL_FEATURES:
		if (length != sizeof(uint64_t) ||
		    load64(message->payload) !=
			    (request == SET_FEATURES ?
				     features :
				     vhost->protocol_features))
			return fail(
				vhost, EPROTO,
				"UML did not negotiate the supported features");
		return 0;
	case SET_MEM_TABLE:
		return set_memory(vhost, message);
	case SET_BACKEND_REQ_FD:
		return set_call_channel(vhost, message);
	case GET_CONFIG:
		return get_config(vhost, message, reply, reply_len);
	case SET_VRING_ADDR:
		return set_queue_addresses(vhost, message);
	default:
		return queue_state(vhost, message, reply, reply_len, kicks);
	}
}

static int request(struct vhost *vhost, struct message *message,
		   unsigned int *kicks)
{
	if ((message->flags & ~(uint32_t)NEED_REPLY) != VERSION)
		return fail(vhost, EPROTO,
			    "invalid UML vhost-user message flags");

	bool expects_fd = message->request == SET_MEM_TABLE ||
			  message->request == SET_BACKEND_REQ_FD;
	if ((message->fd >= 0) != expects_fd)
		return fail(vhost, EPROTO,
			    "invalid UML vhost-user file descriptor count");

	uint8_t reply[MAX_PAYLOAD];
	size_t length = 0;

	if (dispatch(vhost, message, reply, &length, kicks) < 0)
		return -1;
	if (!length) {
		if (!(message->flags & NEED_REPLY))
			return 0;
		store64(reply, 0);
		length = sizeof(uint64_t);
	}
	return channel_send(&vhost->connection, message->request,
			    VERSION | REPLY, reply, length);
}

static int send_call(struct vhost *vhost)
{
	if (vhost->calling || !vhost->pending_count)
		return 0;

	uint8_t payload[STATE_SIZE];

	store32(payload, vhost->pending_calls[0]);
	store32(payload + 4, 0);
	if (channel_send(&vhost->calls, BACKEND_VRING_CALL,
			 VERSION | NEED_REPLY, payload, sizeof(payload)) < 0)
		return -1;

	vhost->calling = true;
	--vhost->pending_count;
	if (vhost->pending_count)
		vhost->pending_calls[0] = vhost->pending_calls[1];
	return 0;
}

static int ack_call(struct vhost *vhost, const struct message *message)
{
	if (!vhost->calling || message->request != BACKEND_VRING_CALL ||
	    message->flags != (VERSION | REPLY) || message->fd >= 0 ||
	    message->length != sizeof(uint64_t) || load64(message->payload))
		return fail(vhost, EPROTO,
			    "invalid UML vhost-user interrupt acknowledgment");

	vhost->calling = false;
	return send_call(vhost);
}

static short ready(int fd, const struct pollfd *fds, size_t count)
{
	if (fd < 0)
		return 0;

	short events = 0;

	for (size_t i = 0; i < count; ++i) {
		if (fds[i].fd == fd)
			events |= fds[i].revents;
	}
	return events;
}

static int service_channel(struct vhost *vhost, struct channel *channel,
			   short events, unsigned int *kicks)
{
	if ((events & POLLOUT) && channel_flush(channel) < 0)
		return -1;
	if (!(events & (POLLIN | POLLHUP | POLLERR | POLLNVAL)))
		return 0;

	/* The caller uses level-triggered polling, so unread requests stay ready. */
	struct message message;
	int status = channel_receive(vhost, channel, &message);

	if (status <= 0)
		return status;

	bool control = channel == &vhost->connection;
	int result = control ? request(vhost, &message, kicks) :
			       ack_call(vhost, &message);
	if (message.fd >= 0) {
		int saved = errno;

		close(message.fd);
		errno = saved;
	}
	/* A kick or queue enable can leave virtqueue work for the caller. */
	if (result < 0 || !control || *kicks ||
	    message.request == SET_VRING_ENABLE)
		return result;
	return channel_flush(channel);
}

static int peek_chain(struct vhost *vhost, unsigned int index, bool writable,
		      struct chain *chain)
{
	if (index >= NUM_QUEUES)
		return fail(vhost, EINVAL, "invalid virtqueue index");

	const struct memory *memory = &vhost->memory;
	const struct queue *queue = &vhost->queues[index];

	if (!queue->enabled || !queue->started || !queue->mapped)
		return fail(vhost, EAGAIN, NULL);

	const uint8_t *avail = memory->base + queue->avail;
	const uint16_t *avail_index =
		(const uint16_t *)(avail + offsetof(struct vring_avail, idx));
	/* Acquire the descriptors published by the guest. Ring setup checks the
	 * alignment of the shared index before using atomic loads and stores.
	 */
	uint16_t available =
		le16toh(__atomic_load_n(avail_index, __ATOMIC_ACQUIRE));
	uint16_t count = (uint16_t)(available - queue->next_available);

	if (!count)
		return fail(vhost, EAGAIN, NULL);
	if (count > queue->size)
		return fail(vhost, EPROTO,
			    "virtqueue available index exceeds its size");

	unsigned int slot = queue->next_available % queue->size;
	uint16_t descriptor =
		load_le16(avail + offsetof(struct vring_avail, ring) +
			  sizeof(uint16_t) * slot);
	chain->head = descriptor;
	chain->count = 0;
	chain->length = 0;

	while (true) {
		if (descriptor >= queue->size || chain->count == queue->size)
			return fail(vhost, EPROTO,
				    "invalid virtqueue descriptor chain");

		struct vring_desc entry;

		memcpy(&entry,
		       memory->base + queue->desc + sizeof(entry) * descriptor,
		       sizeof(entry));
		uint64_t address = le64toh(entry.addr);
		uint32_t length = le32toh(entry.len);
		uint16_t flags = le16toh(entry.flags);

		descriptor = le16toh(entry.next);

		if ((flags & ~(VRING_DESC_F_NEXT | VRING_DESC_F_WRITE)) ||
		    (bool)(flags & VRING_DESC_F_WRITE) != writable)
			return fail(vhost, EPROTO,
				    "unsupported virtqueue descriptor flags");

		size_t offset;

		if (!translate(memory, address, length, false, &offset))
			return fail(vhost, EPROTO,
				    "virtqueue address outside guest memory");
		if (length > (size_t)SSIZE_MAX - chain->length)
			return fail(vhost, EOVERFLOW,
				    "virtqueue descriptor chain is too large");
		chain->segments[chain->count++] =
			(struct segment){ offset, length };
		chain->length += length;
		if (!(flags & VRING_DESC_F_NEXT))
			return 0;
	}
}

static void complete_chain(struct vhost *vhost, unsigned int queue_index,
			   const struct chain *chain, uint32_t length)
{
	struct queue *queue = &vhost->queues[queue_index];
	uint8_t *used = vhost->memory.base + queue->used;
	uint16_t *used_index =
		(uint16_t *)(used + offsetof(struct vring_used, idx));
	uint16_t index = le16toh(__atomic_load_n(used_index, __ATOMIC_RELAXED));
	const struct vring_used_elem entry = {
		.id = htole32(chain->head),
		.len = htole32(length),
	};
	memcpy(used + offsetof(struct vring_used, ring) +
		       (index % queue->size) * sizeof(entry),
	       &entry, sizeof(entry));
	__atomic_store_n(used_index, htole16((uint16_t)(index + 1)),
			 __ATOMIC_RELEASE);
	++queue->next_available;
}

struct vhost *bsw_vhost_open(const char *path, unsigned int device_id)
{
	struct sockaddr_un address = { .sun_family = AF_UNIX };
	int saved;
	size_t length = strlen(path);

	if (!length || length >= sizeof(address.sun_path)) {
		errno = length ? ENAMETOOLONG : EINVAL;
		return NULL;
	}
	const struct device_profile *device;

	switch (device_id) {
	case VIRTIO_ID_BT:
		device = &bluetooth;
		break;
	case VIRTIO_ID_CONSOLE:
		device = &console;
		break;
	default:
		errno = EINVAL;
		return NULL;
	}

	struct vhost *vhost = calloc(1, sizeof(*vhost));

	if (!vhost)
		return NULL;
	vhost->listener = -1;
	vhost->device = device;
	vhost->connection.fd = vhost->connection.received_fd = -1;
	vhost->calls.fd = vhost->calls.received_fd = -1;
	vhost->protocol_features = notify_features;
	if (device->config)
		vhost->protocol_features |=
			FEATURE(VHOST_USER_PROTOCOL_F_CONFIG);

	vhost->path = strdup(path);
	if (!vhost->path)
		goto error;
	if (unlink(path) < 0 && errno != ENOENT)
		goto error;

	vhost->listener =
		socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
	if (vhost->listener < 0)
		goto error;
	memcpy(address.sun_path, path, length + 1);
	socklen_t size = (socklen_t)(offsetof(struct sockaddr_un, sun_path) +
				     length + 1);
	if (bind(vhost->listener, (struct sockaddr *)&address, size) < 0)
		goto error;
	vhost->bound = true;
	if (listen(vhost->listener, 1) < 0)
		goto error;
	return vhost;

error:
	saved = errno;
	bsw_vhost_close(vhost);
	errno = saved;
	return NULL;
}

void bsw_vhost_close(struct vhost *vhost)
{
	if (!vhost)
		return;
	if (vhost->listener >= 0)
		close(vhost->listener);
	channel_close(&vhost->connection);
	channel_close(&vhost->calls);
	if (vhost->memory.length)
		munmap(vhost->memory.base, vhost->memory.length);
	if (vhost->bound)
		unlink(vhost->path);
	free(vhost->path);
	free(vhost);
}

size_t bsw_vhost_poll(const struct vhost *vhost, struct pollfd fds[3],
		      struct bsw_vhost_state *state)
{
	const struct channel *control = &vhost->connection;
	const struct channel *calls = &vhost->calls;

	*state = (struct bsw_vhost_state){
		.interrupt_ready = vhost->interrupts != 0,
		.interrupt_pending = notification_pending(vhost),
	};
	if (!fds)
		return 0;

	fds[0] = (struct pollfd){
		.fd = control->fd < 0 ? vhost->listener : control->fd,
		.events = POLLIN | (control->queued ? POLLOUT : 0),
	};
	if (calls->fd < 0)
		return 1;

	fds[1] = (struct pollfd){
		.fd = calls->fd,
		.events = POLLIN | (calls->queued ? POLLOUT : 0),
	};
	return 2;
}

int bsw_vhost_service(struct vhost *vhost, const struct pollfd *fds,
		      size_t count)
{
	vhost->error = NULL;
	unsigned int kicks = 0;

	if (vhost->connection.fd < 0 && ready(vhost->listener, fds, count)) {
		int fd;

		do {
			fd = accept4(vhost->listener, NULL, NULL,
				     SOCK_NONBLOCK | SOCK_CLOEXEC);
		} while (fd < 0 && errno == EINTR);
		if (fd < 0 && errno != EAGAIN)
			return -1;
		vhost->connection.fd = fd;
	}

	if (service_channel(vhost, &vhost->connection,
			    ready(vhost->connection.fd, fds, count),
			    &kicks) < 0 ||
	    service_channel(vhost, &vhost->calls,
			    ready(vhost->calls.fd, fds, count), &kicks) < 0)
		return -1;
	return (kicks & (1u << vhost->device->output_queue)) != 0;
}

ssize_t bsw_vhost_read(struct vhost *vhost, void *data, size_t capacity,
		       size_t *required)
{
	vhost->error = NULL;
	unsigned int queue = vhost->device->output_queue;
	struct chain chain;

	if (peek_chain(vhost, queue, false, &chain) < 0)
		return -1;
	if (chain.length > capacity) {
		if (required)
			*required = chain.length;
		return fail(vhost, EMSGSIZE,
			    "virtqueue read buffer is too small");
	}

	uint8_t *output = data;

	for (unsigned int i = 0; i < chain.count; ++i) {
		const struct segment *segment = &chain.segments[i];

		if (segment->length) {
			memcpy(output, vhost->memory.base + segment->offset,
			       segment->length);
			output += segment->length;
		}
	}
	complete_chain(vhost, queue, &chain, 0);
	if (vhost->device->notify_on_output)
		vhost->interrupts |= 1u << queue;
	return (ssize_t)chain.length;
}

ssize_t bsw_vhost_write(struct vhost *vhost, const void *data, size_t length)
{
	vhost->error = NULL;
	if (!length)
		return 0;

	unsigned int queue = vhost->device->input_queue;
	struct chain chain;

	if (peek_chain(vhost, queue, true, &chain) < 0)
		return -1;
	if (vhost->device->stream_input) {
		if (!chain.length)
			return fail(vhost, EINVAL,
				    "Virtio console has an empty input buffer");
		if (length > chain.length)
			length = chain.length;
	}
	if (length > chain.length || length > UINT32_MAX)
		return fail(vhost, EMSGSIZE,
			    "data does not fit in the guest receive buffer");

	const uint8_t *input = data;
	size_t remaining = length;

	for (unsigned int i = 0; i < chain.count && remaining; ++i) {
		const struct segment *segment = &chain.segments[i];
		size_t size = segment->length < remaining ? segment->length :
							    remaining;
		if (size) {
			memcpy(vhost->memory.base + segment->offset, input,
			       size);
			input += size;
			remaining -= size;
		}
	}
	complete_chain(vhost, queue, &chain, (uint32_t)length);
	vhost->interrupts |= 1u << queue;
	return (ssize_t)length;
}

static int notify(struct vhost *vhost, unsigned int queue)
{
	if (vhost->calls.fd < 0)
		return fail(
			vhost, ENOTCONN,
			"UML has not established a vhost-user request channel");

	for (size_t i = 0; i < vhost->pending_count; ++i) {
		if (vhost->pending_calls[i] == queue)
			return send_call(vhost);
	}
	vhost->pending_calls[vhost->pending_count++] = queue;
	return send_call(vhost);
}

int bsw_vhost_deliver_interrupts(struct vhost *vhost)
{
	vhost->error = NULL;
	for (unsigned int queue = 0; queue < NUM_QUEUES; ++queue) {
		unsigned int mask = 1u << queue;

		if (!(vhost->interrupts & mask))
			continue;
		if (notify(vhost, queue) < 0)
			return -1;
		vhost->interrupts &= ~mask;
	}
	return 0;
}

const char *bsw_vhost_error(const struct vhost *vhost)
{
	return vhost->error;
}
