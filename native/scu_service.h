/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* All callbacks and desktop work run on one GLib context. Callbacks only frame
 * and queue requests: nested dispatch during desktop calls never executes a
 * second action. Control sockets are host-only and never mounted into guests. */
#include <glib-unix.h>

typedef struct HostLease HostLease;
typedef struct {
    HostLease *lease;
    guint64 serial;
    int fd;
    guint source, writer, pending;
    GString *input, *output;
    gsize sent;
    gboolean closed, oversized;
} HostClient;

struct HostLease {
    int control, listener;
    guint source, accept_source;
    guint received_fds;
    GString *input;
    gboolean registered, revoked, acknowledged, used;
    gint64 idle_since;
    char *session, *wayland;
};

typedef enum { HOST_CALL, HOST_REGISTER, HOST_REVOKE } HostEventKind;
typedef struct {
    HostEventKind kind;
    HostClient *client;
    HostLease *lease;
    yyjson_doc *document;
} HostEvent;

static GPtrArray *host_clients, *host_leases;
static GQueue host_events = G_QUEUE_INIT;
static HostClient *host_current;
static guint64 host_serial;
static guint host_control_source;
static gboolean host_registered_once;
static const guint host_max_clients = 32, host_max_pending = 64;
static gboolean desktop_bind(const char *session, const char *wayland, GError **error);
static void desktop_maintain(guint clients);
static void desktop_close(void);

static char *client_request_key(HostClient *client, JsonValue *id)
{
    char *raw = request_id_key(id);
    char *key = g_strdup_printf("%" G_GUINT64_FORMAT ":%s", client ? client->serial : 0, raw ? raw : "null");
    free(raw);
    return key;
}

static char *host_request_key(JsonValue *id) { return client_request_key(host_current, id); }

static void host_client_close(HostClient *client)
{
    if (client->closed) return;
    client->closed = TRUE;
    if (client == host_current) g_atomic_int_set(&scu.cancelled, 1);
    if (client->source) g_source_remove(client->source);
    if (client->writer) g_source_remove(client->writer);
    client->source = client->writer = 0;
    shutdown(client->fd, SHUT_RDWR);
    close(client->fd);
    client->fd = -1;
    /* Cancellation entries cannot accumulate after a transport disappears. */
    char *prefix = g_strdup_printf("%" G_GUINT64_FORMAT ":", client->serial);
    GHashTableIter iterator;
    gpointer key;
    g_hash_table_iter_init(&iterator, cancelled_ids);
    while (g_hash_table_iter_next(&iterator, &key, NULL)) {
        if (g_str_has_prefix(key, prefix)) g_hash_table_iter_remove(&iterator);
    }
    g_free(prefix);
}

static gboolean host_write(gint fd, GIOCondition condition, gpointer data)
{
    HostClient *client = data;
    if (condition & (G_IO_ERR | G_IO_HUP | G_IO_NVAL)) {
        host_client_close(client);
        return G_SOURCE_REMOVE;
    }
    gsize remaining = client->output->len - client->sent;
    ssize_t count = send(fd, client->output->str + client->sent, MIN(remaining, 65536), MSG_NOSIGNAL);
    if (count > 0) client->sent += count;
    else if (count == 0 || (errno != EINTR && errno != EAGAIN)) {
        host_client_close(client);
        return G_SOURCE_REMOVE;
    }
    if (client->sent == client->output->len) {
        g_string_truncate(client->output, 0);
        client->sent = 0;
        client->writer = 0;
        return G_SOURCE_REMOVE;
    }
    return G_SOURCE_CONTINUE;
}

static void host_send_line(const char *line)
{
    HostClient *client = host_current;
    if (!client || client->closed) return;
    gsize length = strlen(line);
    if (client->output->len + length > 128u * 1024u * 1024u) {
        host_client_close(client);
        return;
    }
    g_string_append_len(client->output, line, length);
    g_string_append_c(client->output, '\n');
    if (!client->writer)
        client->writer = g_unix_fd_add(client->fd, G_IO_OUT | G_IO_HUP | G_IO_ERR, host_write, client);
}

static void host_queue(HostEventKind kind, HostClient *client, HostLease *lease, yyjson_doc *document)
{
    HostEvent *event = g_new0(HostEvent, 1);
    *event = (HostEvent){kind, client, lease, document};
    if (client) client->pending++;
    g_queue_push_tail(&host_events, event);
}

static void host_line(HostClient *client)
{
    yyjson_doc *doc = client->oversized ? NULL : yyjson_read(client->input->str, client->input->len, 0);
    JsonValue *root = doc ? yyjson_doc_get_root(doc) : NULL;
    if (!g_strcmp0(json_string(root, "method"), "notifications/cancelled")) {
        char *key = client_request_key(client, json_member(json_member(root, "params"), "requestId"));
        if (g_hash_table_size(cancelled_ids) < 256) g_hash_table_add(cancelled_ids, g_strdup(key));
        if (client == host_current && !g_strcmp0(key, active_id)) g_atomic_int_set(&scu.cancelled, 1);
        g_free(key);
        yyjson_doc_free(doc);
    } else if (g_queue_get_length(&host_events) < host_max_pending) {
        host_queue(HOST_CALL, client, NULL, doc);
    } else {
        yyjson_doc_free(doc);
        host_client_close(client);
    }
    g_string_truncate(client->input, 0);
    client->oversized = FALSE;
}

static gboolean host_read(gint fd, GIOCondition condition, gpointer data)
{
    HostClient *client = data;
    char buffer[65536];
    /* Bound each callback so one busy peer cannot starve cancellation. */
    for (guint turn = 0; turn < 16 && !client->closed; turn++) {
        ssize_t count = recv(fd, buffer, sizeof(buffer), MSG_DONTWAIT);
        if (count <= 0) {
            if (!count || (errno != EINTR && errno != EAGAIN)) host_client_close(client);
            break;
        }
        for (ssize_t i = 0; i < count && !client->closed; i++) {
            if (buffer[i] == '\n') host_line(client);
            else if (client->input->len < 1024u * 1024u) g_string_append_c(client->input, buffer[i]);
            else client->oversized = TRUE;
        }
    }
    if (!client->closed && condition & (G_IO_ERR | G_IO_NVAL)) host_client_close(client);
    return client->closed ? G_SOURCE_REMOVE : G_SOURCE_CONTINUE;
}

static guint host_client_count(void)
{
    guint count = 0;
    for (guint i = 0; i < host_clients->len; i++)
        count += !((HostClient *)g_ptr_array_index(host_clients, i))->closed;
    return count;
}

static gboolean host_accept(gint fd, GIOCondition condition, gpointer data)
{
    (void)condition;
    HostLease *lease = data;
    int peer;
    while ((peer = accept4(fd, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK)) >= 0) {
        struct ucred credentials;
        socklen_t size = sizeof(credentials);
        if (lease->revoked || host_client_count() >= host_max_clients ||
            getsockopt(peer, SOL_SOCKET, SO_PEERCRED, &credentials, &size) || credentials.uid != getuid()) {
            close(peer);
            continue;
        }
        HostClient *client = g_new0(HostClient, 1);
        *client = (HostClient){.lease = lease, .serial = ++host_serial, .fd = peer,
            .input = g_string_new(NULL), .output = g_string_new(NULL)};
        g_ptr_array_add(host_clients, client);
        lease->used = TRUE;
        client->source = g_unix_fd_add(peer, G_IO_IN | G_IO_HUP | G_IO_ERR, host_read, client);
    }
    return G_SOURCE_CONTINUE;
}

static void host_revoke(HostLease *lease)
{
    if (lease->revoked) return;
    lease->revoked = TRUE;
    if (lease->accept_source) g_source_remove(lease->accept_source);
    lease->accept_source = 0;
    if (lease->listener >= 0) close(lease->listener);
    lease->listener = -1;
    if (lease->source) g_source_remove(lease->source);
    lease->source = 0;
    for (guint i = 0; i < host_clients->len; i++) {
        HostClient *client = g_ptr_array_index(host_clients, i);
        if (client->lease == lease) host_client_close(client);
    }
    host_queue(HOST_REVOKE, NULL, lease, NULL);
}

static gboolean host_control_read(gint fd, GIOCondition condition, gpointer data)
{
    (void)condition;
    HostLease *lease = data;
    if (lease->registered) {
        char byte;
        ssize_t count = recv(fd, &byte, 1, MSG_DONTWAIT);
        if (count >= 0 || (errno != EINTR && errno != EAGAIN)) host_revoke(lease);
        return lease->revoked ? G_SOURCE_REMOVE : G_SOURCE_CONTINUE;
    }
    char buffer[8192], ancillary[CMSG_SPACE(sizeof(int) * 4)];
    struct iovec iov = {.iov_base = buffer, .iov_len = sizeof(buffer)};
    struct msghdr message = {.msg_iov = &iov, .msg_iovlen = 1,
        .msg_control = ancillary, .msg_controllen = sizeof(ancillary)};
    ssize_t length = recvmsg(fd, &message, MSG_DONTWAIT | MSG_CMSG_CLOEXEC);
    if (length < 0 && (errno == EAGAIN || errno == EINTR)) return G_SOURCE_CONTINUE;
    for (struct cmsghdr *c = CMSG_FIRSTHDR(&message); length > 0 && c; c = CMSG_NXTHDR(&message, c)) {
        if (c->cmsg_level != SOL_SOCKET || c->cmsg_type != SCM_RIGHTS) continue;
        guint count = (c->cmsg_len - CMSG_LEN(0)) / sizeof(int);
        for (guint i = 0; i < count; i++) {
            int received = ((int *)CMSG_DATA(c))[i];
            if (!lease->received_fds++) lease->listener = received;
            else close(received);
        }
    }
    if (length <= 0 || message.msg_flags & (MSG_TRUNC | MSG_CTRUNC) || lease->input->len + length > 8192) {
        host_revoke(lease);
        return G_SOURCE_REMOVE;
    }
    g_string_append_len(lease->input, buffer, length);
    if (!memchr(lease->input->str, '\n', lease->input->len)) return G_SOURCE_CONTINUE;
    yyjson_doc *doc = yyjson_read(lease->input->str, lease->input->len, 0);
    JsonValue *root = doc ? yyjson_doc_get_root(doc) : NULL;
    const char *session = json_string(root, "session"), *wayland = json_string(root, "wayland");
    int accepting = 0;
    socklen_t size = sizeof(accepting);
    struct stat st;
    gboolean valid = lease->received_fds == 1 && session && *session && wayland && *wayland &&
        !fstat(lease->listener, &st) && S_ISSOCK(st.st_mode) && st.st_uid == getuid() &&
        !getsockopt(lease->listener, SOL_SOCKET, SO_ACCEPTCONN, &accepting, &size) && accepting;
    if (valid) {
        fcntl(lease->listener, F_SETFL, fcntl(lease->listener, F_GETFL) | O_NONBLOCK);
        lease->session = g_strdup(session);
        lease->wayland = g_strdup(wayland);
        lease->registered = TRUE;
        host_queue(HOST_REGISTER, NULL, lease, NULL);
    } else host_revoke(lease);
    yyjson_doc_free(doc);
    return lease->revoked ? G_SOURCE_REMOVE : G_SOURCE_CONTINUE;
}

static gboolean host_control_accept(gint fd, GIOCondition condition, gpointer unused)
{
    (void)condition; (void)unused;
    int peer;
    while ((peer = accept4(fd, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK)) >= 0) {
        struct ucred credentials;
        socklen_t size = sizeof(credentials);
        if (host_leases->len >= 64 || getsockopt(peer, SOL_SOCKET, SO_PEERCRED, &credentials, &size) ||
            (credentials.uid != 0 && credentials.uid != getuid())) {
            close(peer);
            continue;
        }
        HostLease *lease = g_new0(HostLease, 1);
        *lease = (HostLease){.control = peer, .listener = -1, .input = g_string_new(NULL)};
        g_ptr_array_add(host_leases, lease);
        lease->source = g_unix_fd_add(peer, G_IO_IN | G_IO_HUP | G_IO_ERR, host_control_read, lease);
    }
    return G_SOURCE_CONTINUE;
}

static void host_collect(void)
{
    for (guint i = host_clients->len; i > 0; i--) {
        HostClient *client = g_ptr_array_index(host_clients, i - 1);
        if (client->closed && !client->pending && client != host_current) {
            g_string_free(client->input, TRUE);
            g_string_free(client->output, TRUE);
            g_free(client);
            g_ptr_array_remove_index_fast(host_clients, i - 1);
        }
    }
    for (guint i = host_leases->len; i > 0; i--) {
        HostLease *lease = g_ptr_array_index(host_leases, i - 1);
        gboolean referenced = FALSE;
        for (guint j = 0; j < host_clients->len; j++)
            referenced |= ((HostClient *)g_ptr_array_index(host_clients, j))->lease == lease;
        if (lease->acknowledged && !referenced) {
            if (lease->control >= 0) close(lease->control);
            g_free(lease->session); g_free(lease->wayland);
            if (lease->input) g_string_free(lease->input, TRUE);
            g_free(lease);
            g_ptr_array_remove_index_fast(host_leases, i - 1);
        }
    }
}

static int host_listener(const char *path)
{
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    char *parent = g_path_get_dirname(path);
    struct stat st;
    gboolean safe = path[0] == '/' && strlen(path) < sizeof(address.sun_path) &&
        !lstat(parent, &st) && S_ISDIR(st.st_mode) && st.st_uid == getuid() && !(st.st_mode & 077);
    g_free(parent);
    if (!safe) return -1;
    strcpy(address.sun_path, path);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    mode_t mask = umask(0077);
    int result = fd >= 0 ? bind(fd, (struct sockaddr *)&address, sizeof(address)) : -1;
    umask(mask);
    if (result || chmod(path, 0600) || listen(fd, 32)) { if (fd >= 0) close(fd); return -1; }
    return fd;
}

static int run_shared_host(const char *test_path, int ready_fd)
{
    signal(SIGPIPE, SIG_IGN);
    signal(SIGTERM, stop_signal);
    signal(SIGINT, stop_signal);
    host_clients = g_ptr_array_new();
    host_leases = g_ptr_array_new();
    cancelled_ids = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    if (!initialize_host()) return 1;
    int control = -1;
    if (test_path) {
        HostLease *lease = g_new0(HostLease, 1);
        *lease = (HostLease){.control = -1, .listener = host_listener(test_path), .registered = TRUE};
        if (lease->listener < 0) { g_free(lease); return 1; }
        g_ptr_array_add(host_leases, lease);
        lease->accept_source = g_unix_fd_add(lease->listener, G_IO_IN, host_accept, lease);
    } else {
        const char *pid = g_getenv("LISTEN_PID"), *fds = g_getenv("LISTEN_FDS");
        if (!pid || !fds || g_ascii_strtoull(pid, NULL, 10) != (guint64)getpid() || strcmp(fds, "1")) return 1;
        control = 3;
        fcntl(control, F_SETFD, FD_CLOEXEC);
        fcntl(control, F_SETFL, O_NONBLOCK);
        host_control_source = g_unix_fd_add(control, G_IO_IN, host_control_accept, NULL);
    }
    if (ready_fd >= 0) {
        int result = write(ready_fd, "1", 1);
        close(ready_fd);
        if (result != 1) g_atomic_int_set(&scu.stopping, 1);
    }
    gint64 startup_deadline = g_get_monotonic_time() + 10 * G_USEC_PER_SEC;
    guint previous_clients = 0;
    while (!g_atomic_int_get(&scu.stopping)) {
        while (g_main_context_iteration(NULL, FALSE)) {}
        HostEvent *event = g_queue_pop_head(&host_events);
        if (event) {
            if (event->kind == HOST_CALL) {
                host_current = event->client;
                if (!host_current->closed) {
                    if (event->document) handle_request(event->document);
                    else {
                        scu.doc = yyjson_mut_doc_new(NULL);
                        send_response(NULL, NULL, -32700, "Malformed or oversized JSON message");
                        yyjson_mut_doc_free(scu.doc); scu.doc = NULL;
                    }
                }
                host_current->pending--;
                host_current = NULL;
                yyjson_doc_free(event->document);
            } else if (event->kind == HOST_REGISTER && !event->lease->revoked) {
                HostLease *lease = event->lease;
                GError *error = NULL;
                g_atomic_int_set(&scu.cancelled, 0);
                if (desktop_bind(lease->session, lease->wayland, &error)) {
                    struct stat st;
                    if (stat("/proc/self/exe", &st)) memset(&st, 0, sizeof(st));
                    char *reply = g_strdup_printf("{\"ready\":true,\"pid\":%lu,\"device\":%lu,\"inode\":%lu}\n",
                        (unsigned long)getpid(), (unsigned long)st.st_dev, (unsigned long)st.st_ino);
                    if (send(lease->control, reply, strlen(reply), MSG_NOSIGNAL) < 0) host_revoke(lease);
                    g_free(reply);
                    if (!lease->revoked) {
                        host_registered_once = TRUE;
                        lease->accept_source = g_unix_fd_add(lease->listener, G_IO_IN, host_accept, lease);
                    }
                } else {
                    g_printerr("scu: registration failed: %s\n", error ? error->message : "invalid session");
                    host_revoke(lease);
                }
                g_clear_error(&error);
            } else if (event->kind == HOST_REVOKE) {
                /* The active call has returned and queued calls from this lease
                 * are already dead. No input can follow this acknowledgement. */
                release_input();
                if (event->lease->control >= 0)
                    (void)send(event->lease->control, "{\"released\":true}\n", 18, MSG_NOSIGNAL);
                event->lease->acknowledged = TRUE;
            }
            g_free(event);
        }
        guint clients = host_client_count();
        if (previous_clients && !clients) scu_invalidate();
        previous_clients = clients;
        desktop_maintain(clients);
        poll_backend();
        /* Controllers retain the listening FD. Returning an idle lease leaves
         * its socket mounted and ready to activate us on the next connection. */
        for (guint i = 0; i < host_leases->len; i++) {
            HostLease *lease = g_ptr_array_index(host_leases, i);
            if (lease->control < 0 || !lease->used || lease->revoked) continue;
            gboolean connected = FALSE;
            for (guint j = 0; j < host_clients->len; j++) {
                HostClient *client = g_ptr_array_index(host_clients, j);
                connected |= client->lease == lease && !client->closed;
            }
            if (connected) lease->idle_since = 0;
            else if (!lease->idle_since) lease->idle_since = g_get_monotonic_time();
            else if (g_get_monotonic_time() - lease->idle_since >= 2 * G_USEC_PER_SEC) host_revoke(lease);
        }
        host_collect();
        if (!test_path && !host_leases->len &&
            (host_registered_once || g_get_monotonic_time() >= startup_deadline)) break;
        if (!event) g_usleep(1000);
    }
    for (guint i = 0; i < host_clients->len; i++) host_client_close(g_ptr_array_index(host_clients, i));
    scu_invalidate();
    desktop_close();
    unload_geometry_script();
    if (host_control_source) g_source_remove(host_control_source);
    if (control >= 0) close(control);
    for (guint i = 0; i < host_leases->len; i++) {
        HostLease *lease = g_ptr_array_index(host_leases, i);
        if (lease->source) g_source_remove(lease->source);
        if (lease->accept_source) g_source_remove(lease->accept_source);
        if (lease->listener >= 0) close(lease->listener);
        if (lease->control >= 0) close(lease->control);
    }
    if (test_path) unlink(test_path);
    g_dbus_connection_flush_sync(host.bus, NULL, NULL);
    g_object_unref(host.bus);
    return 0;
}
