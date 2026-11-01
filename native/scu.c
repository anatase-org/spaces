/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Space-side MCP frontend. Only protocol handling and a private host connection
 * live here; the host executes complete tool calls and produces all images. */
#define _GNU_SOURCE
#include <sys/un.h>
#include "scu_mcp.h"

/* Guest ABI compatibility and request lifecycle. */
#if defined(__x86_64__)
#define OLD_LIBC "GLIBC_2.2.5"
#elif defined(__aarch64__)
#define OLD_LIBC "GLIBC_2.17"
#else
#error Unsupported guest architecture
#endif
extern int old_start(int (*)(int, char **, char **), int, char **, void (*)(void),
    void (*)(void), void (*)(void), void *);
__asm__(".symver old_start,__libc_start_main@" OLD_LIBC);
int __wrap___libc_start_main(int (*f)(int, char **, char **), int n, char **v,
    void (*i)(void), void (*e)(void), void (*r)(void), void *s)
{
    return old_start(f, n, v, i, e, r, s);
}

/* Recent glibc headers bind stat to GLIBC_2.33; use the stable kernel ABI. */
static int endpoint_stat(const char *path, struct stat *metadata)
{
#ifdef SYS_newfstatat
    return syscall(SYS_newfstatat, AT_FDCWD, path, metadata, AT_SYMLINK_NOFOLLOW);
#else
    return syscall(SYS_fstatat, AT_FDCWD, path, metadata, AT_SYMLINK_NOFOLLOW);
#endif
}

static int host_fd = -1;
static dev_t endpoint_device;
static ino_t endpoint_inode;
static gboolean disconnected;

static void scu_invalidate(void)
{
    if (host_fd >= 0) close(host_fd);
    host_fd = -1;
}

static gboolean scu_tick(void)
{
    if (g_atomic_int_get(&scu.cancelled) || g_atomic_int_get(&scu.stopping)) {
        scu_fail("cancelled", "Operation cancelled; completed actions were not rolled back.");
        return FALSE;
    }
    if (scu.deadline && g_get_monotonic_time() >= scu.deadline) {
        scu_fail("timeout", "Operation exceeded its time limit; inspect state before retrying.");
        return FALSE;
    }
    return TRUE;
}

static char *computer_socket_path(void)
{
#ifdef SCU_COMPUTER_SOCKET
    return g_strdup(SCU_COMPUTER_SOCKET);
#else
    return g_strdup_printf("/run/spaces/desktop/%lu/computer/bus", (unsigned long)getuid());
#endif
}

static gboolean endpoint_available(struct stat *st)
{
    char *path = computer_socket_path();
    gboolean safe = !endpoint_stat(path, st) && S_ISSOCK(st->st_mode) &&
        st->st_uid == getuid() && !(st->st_mode & 077);
    g_free(path);
    return safe;
}

static void poll_backend(void)
{
    if (host_fd < 0) return;
    struct stat st;
    char byte;
    ssize_t n = recv(host_fd, &byte, 1, MSG_PEEK | MSG_DONTWAIT);
    if (!endpoint_available(&st) || st.st_dev != endpoint_device ||
        st.st_ino != endpoint_inode || n == 0 ||
        (n < 0 && errno != EAGAIN && errno != EINTR)) {
        scu_invalidate();
        disconnected = TRUE;
    }
}

static gboolean wait_host(short events)
{
    while (scu_tick()) {
        poll_backend();
        if (host_fd < 0) break;
        struct pollfd p = {.fd = host_fd, .events = events};
        int n = poll(&p, 1, 10);
        if (n > 0) return TRUE;
        if (n < 0 && errno != EINTR) break;
    }
    return FALSE;
}

static gboolean connect_host(void)
{
    poll_backend();
    if (disconnected) {
        disconnected = FALSE;
        scu_fail("computer_use_disconnected", "The host computer-use service disconnected. Call status again; no action was replayed.");
        return FALSE;
    }
    if (host_fd >= 0) return TRUE;
    struct stat st;
    if (!endpoint_available(&st)) {
        scu_fail("accessibility_permission_required", "Computer use is not forwarded. On the host, run `spaces configure <space>` and enable Plus Accessibility and Agent computer use. If enabled, check the graphical login and Spaces service log.");
        return FALSE;
    }
    char *path = computer_socket_path();
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(path) >= sizeof(address.sun_path)) {
        g_free(path);
        scu_fail("computer_use_unavailable", "The host socket path is too long.");
        return FALSE;
    }
    strcpy(address.sun_path, path);
    g_free(path);
    host_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    endpoint_device = st.st_dev;
    endpoint_inode = st.st_ino;
    int rc = host_fd < 0 ? -1 : connect(host_fd, (struct sockaddr *)&address, sizeof(address));
    if (rc < 0 && errno == EINPROGRESS && wait_host(POLLOUT)) {
        int error = 0;
        socklen_t size = sizeof(error);
        rc = getsockopt(host_fd, SOL_SOCKET, SO_ERROR, &error, &size);
        if (!rc && error) { errno = error; rc = -1; }
    }
    struct ucred credentials;
    socklen_t size = sizeof(credentials);
    if (rc < 0 || getsockopt(host_fd, SOL_SOCKET, SO_PEERCRED, &credentials, &size) ||
        credentials.uid != getuid()) {
        scu_fail("computer_use_unavailable", "Cannot connect to the host computer-use service.");
        scu_invalidate();
        disconnected = FALSE;
        return FALSE;
    }
    return TRUE;
}

static JsonResult *forward_tool(JsonValue *id, JsonValue *parameters)
{
    if (!connect_host()) return NULL;
    yyjson_mut_doc *request = yyjson_mut_doc_new(NULL);
    JsonResult *root = yyjson_mut_obj(request);
    yyjson_mut_obj_add_str(request, root, "jsonrpc", "2.0");
    yyjson_mut_obj_add_str(request, root, "method", "tools/call");
    yyjson_mut_obj_add_val(request, root, "id", yyjson_val_mut_copy(request, id));
    yyjson_mut_obj_add_val(request, root, "params", yyjson_val_mut_copy(request, parameters));
    yyjson_mut_doc_set_root(request, root);
    size_t length = 0, offset = 0;
    char *line = yyjson_mut_write(request, YYJSON_WRITE_NEWLINE_AT_END, &length);
    yyjson_mut_doc_free(request);
    GString *response = g_string_new(NULL);
    gboolean complete = FALSE;
    while (scu_tick()) {
        if (offset < length) {
            if (!wait_host(POLLOUT)) break;
            ssize_t n = send(host_fd, line + offset, length - offset, MSG_NOSIGNAL);
            if (n > 0) offset += n;
            else if (n == 0 || (errno != EAGAIN && errno != EINTR)) break;
            continue;
        }
        if (!wait_host(POLLIN)) break;
        char buffer[65536];
        ssize_t n = read(host_fd, buffer, sizeof(buffer));
        if (n > 0) {
            if (response->len + n > 512u * 1024u * 1024u) break;
            g_string_append_len(response, buffer, n);
            if (response->str[response->len - 1] == '\n') { complete = TRUE; break; }
        } else if (n == 0 || (errno != EAGAIN && errno != EINTR)) break;
    }
    free(line);
    yyjson_doc *reply = complete ? yyjson_read(response->str, response->len, 0) : NULL;
    JsonValue *value = reply ? yyjson_doc_get_root(reply) : NULL;
    JsonValue *result = json_member(value, "result");
    JsonResult *output = NULL;
    if (yyjson_equals(json_member(value, "id"), id) && yyjson_is_obj(result)) {
        output = yyjson_val_mut_copy(scu.doc, result);
    } else {
        scu_fail("computer_use_disconnected", "The host computer-use service disconnected. Call status again; no action was replayed.");
        scu_invalidate();
        disconnected = FALSE; /* This request has already reported the loss. */
    }
    yyjson_doc_free(reply);
    g_string_free(response, TRUE);
    return output;
}

int main(int argc, char **argv)
{
    if (argc == 1 || (argc == 2 && (!strcmp(argv[1], "--help") || !strcmp(argv[1], "-h")))) {
        print_help(stdout);
        return 0;
    }
    if ((argc != 2 && !(argc == 3 && !strcmp(argv[2], "--no-vision"))) || strcmp(argv[1], "mcp")) {
        print_help(stderr);
        return 2;
    }
    no_vision = argc == 3;
    return serve_mcp();
}
