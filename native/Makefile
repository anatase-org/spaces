CC ?= cc
PYTHON ?= python3
CPPFLAGS ?=
CFLAGS ?= -O2 -std=gnu11 -fstack-protector-strong -fPIC -Wall -Wextra -Werror
LDFLAGS ?= -Wl,-z,relro,-z,now
PREFIX ?= /usr
LIBEXECDIR ?= $(PREFIX)/lib/spaces
GUESTDIR ?= $(LIBEXECDIR)/guest
DESTDIR ?=
TARGET_ARCH ?= $(shell uname -m)

.PHONY: all check-guest-abi clean install

POLKIT_CFLAGS := $(shell pkg-config --cflags polkit-agent-1)
POLKIT_LIBS := $(shell pkg-config --libs polkit-agent-1)

all: pam_spaces.so spaces-pam-worker spaces-polkit-worker \
	spaces-polkit-agent

pam_spaces.so: pam_spaces.c protocol.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -shared -o $@ pam_spaces.c \
		$(LDFLAGS) -lpam

spaces-pam-worker: spaces_pam_worker.c protocol.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ spaces_pam_worker.c \
		$(LDFLAGS) -lpam

spaces-polkit-worker: spaces_polkit_worker.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< $(LDFLAGS)

spaces-polkit-agent: spaces_polkit_agent.c glibc_compat.c
	$(CC) $(CPPFLAGS) $(CFLAGS) $(POLKIT_CFLAGS) -o $@ $^ \
		$(LDFLAGS) -Wl,--wrap=__libc_start_main $(POLKIT_LIBS)

check-guest-abi: all
	$(PYTHON) check_guest_abi.py --target $(TARGET_ARCH) \
		--glibc-max 2.17 \
		--guest pam_spaces.so spaces-polkit-agent \
		--host spaces-pam-worker spaces-polkit-worker

install: check-guest-abi
	install -Dm755 pam_spaces.so \
		$(DESTDIR)$(GUESTDIR)/pam_spaces.so
	install -Dm755 spaces-pam-worker \
		$(DESTDIR)$(LIBEXECDIR)/spaces-pam-worker
	install -Dm755 spaces-polkit-worker \
		$(DESTDIR)$(LIBEXECDIR)/spaces-polkit-worker
	install -Dm755 spaces-polkit-agent \
		$(DESTDIR)$(GUESTDIR)/spaces-polkit-agent

clean:
	rm -f pam_spaces.so spaces-pam-worker spaces-polkit-worker \
		spaces-polkit-agent
