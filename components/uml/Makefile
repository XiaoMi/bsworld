# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

OUT_DIR ?= build
CFLAGS ?= -O2

empty :=
space := $(empty) $(empty)
library := $(subst $(space),\ ,$(OUT_DIR))/libbsworld_vhost.so

.PHONY: all
all: $(library)

$(library): vhost.c vhost.h Makefile
	mkdir -p "$(OUT_DIR)"
	$(CC) $(CPPFLAGS) $(CFLAGS) -std=c11 -Wall -Wextra -fPIC -fvisibility=hidden \
		$(LDFLAGS) -shared -o "$@" $(filter %.c,$^) $(LDLIBS)
