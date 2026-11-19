/*
 * Copyright 2026 Xiaomi Corporation
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * getrandom() replacement preloaded into the UML kernel process.
 *
 * UML seeds the guest's RNG once at boot with the host's getrandom(). This
 * returns a stream fixed by BSWORLD_UML_SEED instead, so every random value
 * the guest derives, such as private addresses and keys, repeats on every
 * run. Programs inside the guest reach the guest kernel's RNG, not this
 * function.
 */

#include <stdint.h>
#include <stdlib.h>
#include <sys/random.h>
#include <sys/types.h>

ssize_t getrandom(void *buf, size_t len, unsigned int flags)
{
	const char *seed = getenv("BSWORLD_UML_SEED");
	uint64_t state = seed ? strtoull(seed, NULL, 0) : 1;
	unsigned char *out = buf;
	size_t i;

	(void)flags;
	for (i = 0; i < len; i++) {
		/* splitmix64 */
		uint64_t z = (state += 0x9e3779b97f4a7c15ULL);

		z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
		z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
		out[i] = (unsigned char)(z ^ (z >> 31));
	}
	return (ssize_t)len;
}
