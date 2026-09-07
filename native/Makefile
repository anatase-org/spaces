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

all: pam_spaces.so spaces-pam spaces spaces-portal \
	spaces-secret-helper spaces-open spaces-broker spaces-system-broker

pam_spaces.so: pam_spaces.c protocol.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -shared -o $@ pam_spaces.c \
		$(LDFLAGS) -lpam

spaces-pam: spaces_pam.c protocol.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ spaces_pam.c \
		$(LDFLAGS) -lpam

spaces: spaces.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ spaces.c \
		$(LDFLAGS) -Wl,--wrap=__libc_start_main

spaces-portal: spaces_portal.c dbus_message.h
	$(CC) $(CPPFLAGS) $(CFLAGS) \
		$$(pkg-config --cflags gio-unix-2.0) \
		-o $@ spaces_portal.c $(LDFLAGS) \
		-Wl,--wrap=__libc_start_main \
		$$(pkg-config --libs gio-unix-2.0)

spaces-secret-helper: spaces_secret_helper.c
	$(CC) $(CPPFLAGS) $(CFLAGS) \
		$$(pkg-config --cflags gio-unix-2.0) \
		-o $@ spaces_secret_helper.c $(LDFLAGS) \
		-Wl,--wrap=__libc_start_main \
		$$(pkg-config --libs gio-unix-2.0)

spaces-open: spaces_open.c
	$(CC) $(CPPFLAGS) $(CFLAGS) \
		$$(pkg-config --cflags gio-unix-2.0) \
		-o $@ spaces_open.c $(LDFLAGS) \
		-Wl,--wrap=__libc_start_main \
		$$(pkg-config --libs gio-unix-2.0)

spaces-broker: spaces_broker.c
	$(CC) $(CPPFLAGS) $(CFLAGS) \
		$$(pkg-config --cflags gio-unix-2.0) \
		-o $@ spaces_broker.c $(LDFLAGS) \
		$$(pkg-config --libs gio-unix-2.0)

spaces-system-broker: spaces_system_broker.c system_bus_policy.h dbus_message.h
	$(CC) $(CPPFLAGS) $(CFLAGS) \
		$$(pkg-config --cflags gio-unix-2.0) \
		-o $@ spaces_system_broker.c $(LDFLAGS) \
		-Wl,--wrap=__libc_start_main \
		$$(pkg-config --libs gio-unix-2.0)

check-guest-abi: all
	$(PYTHON) check_guest_abi.py --target $(TARGET_ARCH) \
		--glibc-max 2.17 \
		--guest pam_spaces.so spaces spaces-portal \
			spaces-secret-helper spaces-open spaces-system-broker \
		--host spaces-pam spaces-broker

install: check-guest-abi
	install -Dm755 spaces-system-broker $(DESTDIR)$(GUESTDIR)/spaces-system-broker
	install -Dm755 spaces-system-broker $(DESTDIR)$(LIBEXECDIR)/spaces-system-broker
	install -Dm755 pam_spaces.so \
		$(DESTDIR)$(GUESTDIR)/pam_spaces.so
	install -Dm755 spaces-pam \
		$(DESTDIR)$(LIBEXECDIR)/spaces-pam
	install -Dm755 spaces \
		$(DESTDIR)$(GUESTDIR)/spaces
	install -Dm755 spaces-portal \
		$(DESTDIR)$(GUESTDIR)/spaces-portal
	install -Dm755 spaces-secret-helper \
		$(DESTDIR)$(GUESTDIR)/spaces-secret-helper
	install -Dm755 spaces-open \
		$(DESTDIR)$(GUESTDIR)/spaces-open
	install -Dm755 spaces-broker \
		$(DESTDIR)$(LIBEXECDIR)/spaces-broker

clean:
	rm -f pam_spaces.so spaces-pam spaces \
		spaces-portal spaces-secret-helper spaces-open spaces-broker spaces-system-broker
