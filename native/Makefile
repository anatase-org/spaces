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

all: pam_spaces.so spaces-pam-worker spaces-session-launcher spaces-portal \
	spaces-secret-helper

pam_spaces.so: pam_spaces.c protocol.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -shared -o $@ pam_spaces.c \
		$(LDFLAGS) -lpam

spaces-pam-worker: spaces_pam_worker.c protocol.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ spaces_pam_worker.c \
		$(LDFLAGS) -lpam

spaces-session-launcher: spaces_session_launcher.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ spaces_session_launcher.c \
		$(LDFLAGS) -Wl,--wrap=__libc_start_main

spaces-portal: spaces_portal.c portal_interfaces.h
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

check-guest-abi: all
	$(PYTHON) check_guest_abi.py --target $(TARGET_ARCH) \
		--glibc-max 2.17 \
		--guest pam_spaces.so spaces-session-launcher spaces-portal \
			spaces-secret-helper \
		--host spaces-pam-worker

install: check-guest-abi
	install -Dm755 pam_spaces.so \
		$(DESTDIR)$(GUESTDIR)/pam_spaces.so
	install -Dm755 spaces-pam-worker \
		$(DESTDIR)$(LIBEXECDIR)/spaces-pam-worker
	install -Dm755 spaces-session-launcher \
		$(DESTDIR)$(GUESTDIR)/spaces-session-launcher
	install -Dm755 spaces-portal \
		$(DESTDIR)$(GUESTDIR)/spaces-portal
	install -Dm755 spaces-secret-helper \
		$(DESTDIR)$(GUESTDIR)/spaces-secret-helper

clean:
	rm -f pam_spaces.so spaces-pam-worker spaces-session-launcher \
		spaces-portal spaces-secret-helper
