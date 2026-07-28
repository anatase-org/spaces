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

all: pam_spaces.so spaces-pam-worker

pam_spaces.so: pam_spaces.c protocol.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -shared -o $@ pam_spaces.c \
		$(LDFLAGS) -lpam

spaces-pam-worker: spaces_pam_worker.c protocol.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ spaces_pam_worker.c \
		$(LDFLAGS) -lpam

check-guest-abi: all
	$(PYTHON) check_guest_abi.py --target $(TARGET_ARCH) \
		--glibc-max 2.17 \
		--guest pam_spaces.so \
		--host spaces-pam-worker

install: check-guest-abi
	install -Dm755 pam_spaces.so \
		$(DESTDIR)$(GUESTDIR)/pam_spaces.so
	install -Dm755 spaces-pam-worker \
		$(DESTDIR)$(LIBEXECDIR)/spaces-pam-worker

clean:
	rm -f pam_spaces.so spaces-pam-worker
