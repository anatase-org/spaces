#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <glib-unix.h>
#include <linux/openat2.h>
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define INTEGRATION_PATH "/org/anatase/Spaces/Integration"
#define INTEGRATION_INTERFACE "org.anatase.Spaces.Integration1"
/* A lingering Space must not activate the host portal between desktop
 * logins, when its backend-selection environment is absent or stale. */
#define PORTAL_NAME "org.freedesktop.portal.Desktop"
#define PORTAL_PATH "/org/freedesktop/portal/desktop"
#define RTKIT_NAME "org.freedesktop.RealtimeKit1"
#define RTKIT_PATH "/org/freedesktop/RealtimeKit1"
#define SECRET_MAXIMUM (64 * 1024)
#define SECRET_SIZE 64
#define STAGED_FILE_MAXIMUM (32 * 1024 * 1024)

static const char open_xml[] =
    "<node>"
    " <interface name='" INTEGRATION_INTERFACE "'>"
    "  <method name='OpenFile'>"
    "   <arg type='s' direction='in' name='guest_path'/>"
    "   <arg type='h' direction='in' name='proof_fd'/>"
    "   <arg type='b' direction='in' name='writable'/>"
    "   <arg type='s' direction='in' name='activation_token'/>"
    "   <arg type='u' direction='out' name='response'/>"
    "  </method>"
    "  <method name='OpenDirectory'>"
    "   <arg type='s' direction='in' name='guest_path'/>"
    "   <arg type='h' direction='in' name='proof_fd'/>"
    "   <arg type='s' direction='in' name='activation_token'/>"
    "   <arg type='u' direction='out' name='response'/>"
    "  </method>"
    "  <method name='MakeRealtime'>"
    "   <arg type='h' direction='in' name='process_pidfd'/>"
    "   <arg type='h' direction='in' name='thread_pidfd'/>"
    "   <arg type='b' direction='in' name='high_priority'/>"
    "   <arg type='i' direction='in' name='priority'/>"
    "  </method>"
    "  <method name='MakeGameMode'>"
    "   <arg type='s' direction='in' name='method'/>"
    "   <arg type='h' direction='in' name='target_pidfd'/>"
    "   <arg type='h' direction='in' name='requester_pidfd'/>"
    "   <arg type='i' direction='out' name='result'/>"
    "  </method>"
    "  <method name='Screenshot'>"
    "   <arg type='s' direction='in' name='parent_window'/>"
    "   <arg type='a{sv}' direction='in' name='options'/>"
    "   <arg type='u' direction='out' name='response'/>"
    "   <arg type='h' direction='out' name='file'/>"
    "   <arg type='a{sv}' direction='out' name='results'/>"
    "  </method>"
    "  <method name='StageFile'>"
    "   <arg type='h' direction='in' name='source_fd'/>"
    "   <arg type='s' direction='out' name='host_uri'/>"
    "  </method>"
    "  <method name='ResolvePath'>"
    "   <arg type='s' direction='in' name='guest_path'/>"
    "   <arg type='h' direction='in' name='proof_fd'/>"
    "   <arg type='s' direction='out' name='host_uri'/>"
    "  </method>"
    "  <method name='RemoveStagedFile'>"
    "   <arg type='s' direction='in' name='host_uri'/>"
    "  </method>"
    "  <method name='PortalRequest'>"
    "   <arg type='s' direction='in' name='interface'/>"
    "   <arg type='s' direction='in' name='method'/>"
    "   <arg type='s' direction='in' name='app_id'/>"
    "   <arg type='v' direction='in' name='parameters'/>"
    "   <arg type='u' direction='out' name='response'/>"
    "   <arg type='a{sv}' direction='out' name='results'/>"
    "  </method>"
    "  <method name='DynamicLauncherCall'>"
    "   <arg type='s' direction='in' name='method'/>"
    "   <arg type='v' direction='in' name='parameters'/>"
    "   <arg type='v' direction='out' name='reply'/>"
    "  </method>"
    " </interface>"
    "</node>";

typedef struct {
    char *guest;
    int descriptor;
    struct stat metadata;
} Mapping;

typedef struct {
    GDBusConnection *bus;
    GDBusConnection *system_bus;
    GMainLoop *loop;
    GPtrArray *mappings;
    GPtrArray *staged_files;
    char *space_name;
    char *app_id;
} Broker;

typedef struct {
    GMainLoop *loop;
    char *request_path;
    guint response;
    gboolean received;
    GVariant *results;
} Response;

static char *new_token(void)
{
    char *token = g_uuid_string_random();
    char *cursor;

    for (cursor = token; *cursor != '\0'; cursor++) {
        if (*cursor == '-')
            *cursor = '_';
    }
    return token;
}

static gboolean stop_broker(gpointer data)
{
    g_main_loop_quit(data);
    return G_SOURCE_CONTINUE;
}

static void mapping_free(gpointer data)
{
    Mapping *mapping = data;

    close(mapping->descriptor);
    g_free(mapping->guest);
    g_free(mapping);
}

static gboolean valid_guest_path(const char *path)
{
    char **parts;
    guint index;
    gboolean valid = path != NULL && path[0] == '/';

    if (!valid)
        return FALSE;
    parts = g_strsplit(path, "/", -1);
    for (index = 0; parts[index] != NULL; index++) {
        if (g_str_equal(parts[index], "..")) {
            valid = FALSE;
            break;
        }
    }
    g_strfreev(parts);
    return valid;
}

static gboolean mapping_matches(const Mapping *mapping, const char *path)
{
    gsize size = strlen(mapping->guest);

    if (!g_str_has_prefix(path, mapping->guest))
        return FALSE;
    return size == 1 || path[size] == '\0' || path[size] == '/';
}

static Mapping *find_mapping(Broker *broker, const char *path)
{
    Mapping *best = NULL;
    guint index;

    for (index = 0; index < broker->mappings->len; index++) {
        Mapping *mapping = g_ptr_array_index(broker->mappings, index);

        if (mapping_matches(mapping, path)
            && (best == NULL
                || strlen(mapping->guest) > strlen(best->guest)))
            best = mapping;
    }
    return best;
}

static int secure_open(
    const Mapping *mapping,
    const char *guest_path,
    int flags
)
{
    const char *relative = guest_path + strlen(mapping->guest);
    struct open_how how = {
        .flags = (uint64_t)(flags | O_CLOEXEC),
        .resolve = RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS
            | RESOLVE_NO_XDEV,
    };

    while (*relative == '/')
        relative++;
    if (!S_ISDIR(mapping->metadata.st_mode)) {
        char descriptor_path[64];

        if (*relative != '\0')
            return errno = EXDEV, -1;
        if (g_snprintf(
                descriptor_path, sizeof(descriptor_path),
                "/proc/self/fd/%d", mapping->descriptor
            ) >= (int)sizeof(descriptor_path))
            return errno = ENAMETOOLONG, -1;
        return open(descriptor_path, flags | O_CLOEXEC);
    } else if (*relative == '\0') {
        relative = ".";
    }
#ifdef SYS_openat2
    return (int)syscall(
        SYS_openat2, mapping->descriptor, relative, &how, sizeof(how)
    );
#else
    (void)how;
    return errno = ENOSYS, -1;
#endif
}

static void response_signal(
    GDBusConnection *connection,
    const char *sender,
    const char *path,
    const char *interface,
    const char *signal_name,
    GVariant *parameters,
    gpointer user_data
)
{
    Response *response = user_data;
    GVariant *results;

    (void)connection;
    (void)sender;
    (void)interface;
    (void)signal_name;
    if (response->request_path != NULL
        && !g_str_equal(response->request_path, path))
        return;
    g_variant_get(parameters, "(u@a{sv})", &response->response, &results);
    g_clear_pointer(&response->results, g_variant_unref);
    response->results = results;
    response->received = TRUE;
    g_main_loop_quit(response->loop);
}

static gboolean response_timeout(gpointer data)
{
    Response *response = data;

    g_main_loop_quit(response->loop);
    return G_SOURCE_REMOVE;
}

static gboolean call_host_portal(
    Broker *broker,
    const char *method,
    int descriptor,
    gboolean writable,
    const char *activation_token,
    guint *response,
    GError **error
)
{
    GUnixFDList *fd_list = g_unix_fd_list_new();
    GVariantBuilder options;
    GVariant *reply;
    const char *request_path;
    Response pending = {0};
    guint subscription;
    guint timeout;
    int handle;
    char *token;

    handle = g_unix_fd_list_append(fd_list, descriptor, error);
    if (handle < 0) {
        g_object_unref(fd_list);
        return FALSE;
    }
    g_variant_builder_init(&options, G_VARIANT_TYPE_VARDICT);
    token = new_token();
    g_variant_builder_add(
        &options, "{sv}", "handle_token",
        g_variant_new_string(token)
    );
    g_free(token);
    if (writable)
        g_variant_builder_add(
            &options, "{sv}", "writable", g_variant_new_boolean(TRUE)
        );
    if (activation_token != NULL && *activation_token != '\0')
        g_variant_builder_add(
            &options, "{sv}", "activation_token",
            g_variant_new_string(activation_token)
        );
    pending.loop = g_main_loop_new(NULL, FALSE);
    subscription = g_dbus_connection_signal_subscribe(
        broker->bus, PORTAL_NAME, "org.freedesktop.portal.Request",
        "Response", NULL, NULL, G_DBUS_SIGNAL_FLAGS_NONE,
        response_signal, &pending, NULL
    );
    reply = g_dbus_connection_call_with_unix_fd_list_sync(
        broker->bus, PORTAL_NAME, PORTAL_PATH,
        "org.freedesktop.portal.OpenURI", method,
        g_variant_new("(sh@a{sv})", "", handle,
                      g_variant_builder_end(&options)),
        G_VARIANT_TYPE("(o)"), G_DBUS_CALL_FLAGS_NO_AUTO_START, -1,
        fd_list, NULL, NULL, error
    );
    g_object_unref(fd_list);
    if (reply == NULL) {
        g_dbus_connection_signal_unsubscribe(
            broker->bus, subscription
        );
        g_main_loop_unref(pending.loop);
        return FALSE;
    }
    g_variant_get(reply, "(&o)", &request_path);
    pending.request_path = g_strdup(request_path);
    g_variant_unref(reply);
    timeout = g_timeout_add_seconds(120, response_timeout, &pending);
    g_main_loop_run(pending.loop);
    if (g_source_remove(timeout) == FALSE && !pending.received)
        timeout = 0;
    (void)timeout;
    g_dbus_connection_signal_unsubscribe(broker->bus, subscription);
    g_main_loop_unref(pending.loop);
    g_free(pending.request_path);
    if (!pending.received) {
        g_set_error(
            error, G_IO_ERROR, G_IO_ERROR_TIMED_OUT,
            "Timed out waiting for the host portal"
        );
        return FALSE;
    }
    *response = pending.response;
    g_clear_pointer(&pending.results, g_variant_unref);
    return TRUE;
}

static gboolean screenshot_request(
    Broker *broker,
    const char *parent_window,
    GVariant *options,
    guint *response,
    GVariant **results,
    GError **error
)
{
    GVariantDict dictionary;
    GVariant *updated;
    GVariant *reply;
    const char *request_path;
    Response pending = {0};
    guint subscription;
    guint timeout;
    char *token = new_token();

    g_variant_dict_init(&dictionary, options);
    g_variant_dict_insert(&dictionary, "handle_token", "s", token);
    g_free(token);
    updated = g_variant_dict_end(&dictionary);
    pending.loop = g_main_loop_new(NULL, FALSE);
    subscription = g_dbus_connection_signal_subscribe(
        broker->bus, PORTAL_NAME, "org.freedesktop.portal.Request",
        "Response", NULL, NULL, G_DBUS_SIGNAL_FLAGS_NONE,
        response_signal, &pending, NULL
    );
    reply = g_dbus_connection_call_sync(
        broker->bus, PORTAL_NAME, PORTAL_PATH,
        "org.freedesktop.portal.Screenshot", "Screenshot",
        g_variant_new("(s@a{sv})", parent_window,
            g_variant_ref_sink(updated)),
        G_VARIANT_TYPE("(o)"), G_DBUS_CALL_FLAGS_NO_AUTO_START, -1, NULL, error
    );
    if (reply == NULL) {
        g_dbus_connection_signal_unsubscribe(broker->bus, subscription);
        g_main_loop_unref(pending.loop);
        return FALSE;
    }
    g_variant_get(reply, "(&o)", &request_path);
    pending.request_path = g_strdup(request_path);
    g_variant_unref(reply);
    timeout = g_timeout_add_seconds(120, response_timeout, &pending);
    g_main_loop_run(pending.loop);
    if (g_source_remove(timeout) == FALSE && !pending.received)
        timeout = 0;
    (void)timeout;
    g_dbus_connection_signal_unsubscribe(broker->bus, subscription);
    g_main_loop_unref(pending.loop);
    g_free(pending.request_path);
    if (!pending.received) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_TIMED_OUT,
            "Timed out waiting for the host screenshot portal");
        g_clear_pointer(&pending.results, g_variant_unref);
        return FALSE;
    }
    *response = pending.response;
    *results = pending.results == NULL
        ? g_variant_ref_sink(g_variant_new_array(G_VARIANT_TYPE("{sv}"), NULL, 0))
        : pending.results;
    return TRUE;
}

static gboolean wait_portal_request(Broker *broker, const char *interface,
    const char *method, GVariant *parameters, guint *response,
    GVariant **results, GUnixFDList *fds, GError **error)
{
    GVariant *reply;
    const char *request_path;
    Response pending = {0};
    guint subscription;
    guint timeout;
    pending.loop = g_main_loop_new(NULL, FALSE);
    subscription = g_dbus_connection_signal_subscribe(broker->bus, PORTAL_NAME,
        "org.freedesktop.portal.Request", "Response", NULL, NULL,
        G_DBUS_SIGNAL_FLAGS_NONE, response_signal, &pending, NULL);
    reply = g_dbus_connection_call_with_unix_fd_list_sync(broker->bus,
        PORTAL_NAME, PORTAL_PATH, interface, method, parameters,
        G_VARIANT_TYPE("(o)"), G_DBUS_CALL_FLAGS_NO_AUTO_START, -1, fds,
        NULL, NULL, error);
    if (reply == NULL) {
        g_dbus_connection_signal_unsubscribe(broker->bus, subscription);
        g_main_loop_unref(pending.loop); return FALSE;
    }
    g_variant_get(reply, "(&o)", &request_path);
    pending.request_path = g_strdup(request_path); g_variant_unref(reply);
    timeout = g_timeout_add_seconds(120, response_timeout, &pending);
    g_main_loop_run(pending.loop);
    if (g_source_remove(timeout) == FALSE && !pending.received) timeout = 0;
    (void)timeout;
    g_dbus_connection_signal_unsubscribe(broker->bus, subscription);
    g_main_loop_unref(pending.loop); g_free(pending.request_path);
    if (!pending.received) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_TIMED_OUT,
            "Timed out waiting for the host portal request");
        g_clear_pointer(&pending.results, g_variant_unref); return FALSE;
    }
    *response = pending.response;
    *results = pending.results == NULL
        ? g_variant_ref_sink(g_variant_new_array(G_VARIANT_TYPE("{sv}"), NULL, 0))
        : pending.results;
    return TRUE;
}

static GVariant *broker_options(Broker *broker, GVariant *options,
    gboolean background)
{
    GVariantDict dictionary;
    char *token = new_token();
    g_variant_dict_init(&dictionary, options);
    g_variant_dict_insert(&dictionary, "handle_token", "s", token);
    g_free(token);
    if (background) {
        GVariant *commandline = g_variant_lookup_value(options, "commandline",
            G_VARIANT_TYPE_STRING_ARRAY);
        if (commandline != NULL) {
            GVariantBuilder command;
            GVariantIter iterator;
            const char *item;
            g_variant_builder_init(&command, G_VARIANT_TYPE_STRING_ARRAY);
            g_variant_builder_add(&command, "s", "/usr/bin/spaces");
            g_variant_builder_add(&command, "s", "enter");
            g_variant_builder_add(&command, "s", "--graphical");
            g_variant_builder_add(&command, "s", broker->space_name);
            g_variant_builder_add(&command, "s", "--");
            g_variant_iter_init(&iterator, commandline);
            while (g_variant_iter_next(&iterator, "&s", &item))
                g_variant_builder_add(&command, "s", item);
            g_variant_dict_insert_value(&dictionary, "commandline",
                g_variant_builder_end(&command));
            g_variant_unref(commandline);
        }
    }
    return g_variant_ref_sink(g_variant_dict_end(&dictionary));
}

static char *broker_launcher_id(Broker *broker, const char *desktop_id)
{
    if (desktop_id == NULL || strchr(desktop_id, '/') != NULL
        || !g_str_has_suffix(desktop_id, ".desktop")) return NULL;
    if (g_str_has_prefix(desktop_id, broker->app_id)
        && desktop_id[strlen(broker->app_id)] == '.') return g_strdup(desktop_id);
    return g_strdup_printf("%s.%s", broker->app_id, desktop_id);
}

static int invocation_fd(GDBusMethodInvocation *invocation, int handle,
    GError **error);

static gboolean copy_staged_fd(int source, int output, const char *kind,
    GError **error)
{
    struct stat metadata;
    guint8 buffer[64 * 1024];
    gsize total = 0;
    off_t source_offset = 0;

    if (fstat(source, &metadata) < 0) {
        g_set_error(error, G_IO_ERROR, g_io_error_from_errno(errno),
            "Could not inspect %s input: %s", kind, g_strerror(errno));
        return FALSE;
    }
    if (!S_ISREG(metadata.st_mode) || metadata.st_size < 0
        || metadata.st_size > STAGED_FILE_MAXIMUM) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
            "%s input must be a regular file no larger than 32 MiB", kind);
        return FALSE;
    }
    for (;;) {
        ssize_t count = pread(source, buffer, sizeof(buffer), source_offset);
        gsize output_offset = 0;
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) {
            g_set_error(error, G_IO_ERROR, g_io_error_from_errno(errno),
                "Could not read %s input: %s", kind, g_strerror(errno));
            return FALSE;
        }
        if (count == 0) return TRUE;
        total += (gsize)count;
        if (total > STAGED_FILE_MAXIMUM) {
            g_set_error(error, G_IO_ERROR, G_IO_ERROR_NO_SPACE,
                "%s input exceeds the 32 MiB staging limit", kind);
            return FALSE;
        }
        while (output_offset < (gsize)count) {
            ssize_t written = write(output, buffer + output_offset,
                (gsize)count - output_offset);
            if (written < 0 && errno == EINTR) continue;
            if (written <= 0) {
                g_set_error(error, G_IO_ERROR,
                    written < 0 ? g_io_error_from_errno(errno)
                                : G_IO_ERROR_FAILED,
                    "Could not write staged %s: %s", kind,
                    written < 0 ? g_strerror(errno) : "short write");
                return FALSE;
            }
            output_offset += (gsize)written;
        }
        source_offset += count;
    }
}

static int stage_wallpaper_fd(GDBusMethodInvocation *invocation, gint handle,
    char **staged_path, GError **error)
{
    int source = -1;
    int output = -1;
    int staged = -1;
    char *directory = NULL;
    char *token = NULL;

    source = invocation_fd(invocation, handle, error);
    if (source < 0) goto failed;
    directory = g_build_filename(g_get_user_cache_dir(), "spaces",
        "wallpaper", NULL);
    if (g_mkdir_with_parents(directory, 0700) < 0) goto io_failed;
    token = new_token();
    *staged_path = g_build_filename(directory, token, NULL);
    output = open(*staged_path,
        O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (output < 0) goto io_failed;
    if (!copy_staged_fd(source, output, "Wallpaper", error)) goto failed;
    if (close(output) < 0) { output = -1; goto io_failed; }
    output = -1;
    staged = open(*staged_path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (staged < 0) goto io_failed;
    close(source); g_free(token); g_free(directory);
    return staged;

io_failed:
    if (*error == NULL)
        g_set_error(error, G_IO_ERROR, g_io_error_from_errno(errno),
            "Could not stage guest wallpaper: %s", g_strerror(errno));
failed:
    if (output >= 0) close(output);
    if (staged >= 0) close(staged);
    if (source >= 0) close(source);
    if (*staged_path != NULL) {
        unlink(*staged_path);
        g_clear_pointer(staged_path, g_free);
    }
    g_free(token); g_free(directory);
    return -1;
}

static void secure_clear(void *data, gsize size)
{
    volatile guint8 *cursor = data;
    while (size-- > 0) *cursor++ = 0;
}

static gboolean write_all(int descriptor, const guint8 *data, gsize size)
{
    while (size > 0) {
        ssize_t written = write(descriptor, data, size);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) return FALSE;
        data += written;
        size -= (gsize)written;
    }
    return TRUE;
}

static gboolean finish_app_secret(const char *app_id, int host_descriptor,
    int guest_descriptor)
{
    static const guint8 domain[] = "org.anatase.spaces.portal-secret.v1";
    guint8 raw[SECRET_MAXIMUM + 1];
    guint8 derived[SECRET_SIZE];
    guint8 length[4];
    gsize size = 0;
    gsize app_id_size = strlen(app_id);
    gsize derived_size = sizeof(derived);
    GHmac *hmac = NULL;
    gboolean success = FALSE;

    if (app_id_size > UINT32_MAX || lseek(host_descriptor, 0, SEEK_SET) < 0)
        goto out;
    while (size < sizeof(raw)) {
        ssize_t count = read(host_descriptor, raw + size,
            sizeof(raw) - size);
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) goto out;
        if (count == 0) break;
        size += (gsize)count;
    }
    if (size == 0 || size > SECRET_MAXIMUM) goto out;
    length[0] = (guint8)(app_id_size >> 24);
    length[1] = (guint8)(app_id_size >> 16);
    length[2] = (guint8)(app_id_size >> 8);
    length[3] = (guint8)app_id_size;
    hmac = g_hmac_new(G_CHECKSUM_SHA512, raw, size);
    if (hmac == NULL) goto out;
    g_hmac_update(hmac, domain, sizeof(domain));
    g_hmac_update(hmac, length, sizeof(length));
    g_hmac_update(hmac, (const guint8 *)app_id, app_id_size);
    g_hmac_get_digest(hmac, derived, &derived_size);
    if (derived_size != sizeof(derived)) goto out;
    success = write_all(guest_descriptor, derived, sizeof(derived));
out:
    if (hmac != NULL) g_hmac_unref(hmac);
    secure_clear(raw, sizeof(raw));
    secure_clear(derived, sizeof(derived));
    while (ftruncate(host_descriptor, 0) < 0 && errno == EINTR) {}
    return success;
}

static char *broker_desktop_entry(Broker *broker, const char *contents,
    GError **error)
{
    GKeyFile *key = g_key_file_new();
    char **groups;
    gsize count, index;
    char *result;
    if (contents == NULL || strlen(contents) > 1024 * 1024
        || !g_key_file_load_from_data(key, contents, -1,
            G_KEY_FILE_KEEP_COMMENTS | G_KEY_FILE_KEEP_TRANSLATIONS, error)) {
        g_key_file_unref(key); return NULL;
    }
    groups = g_key_file_get_groups(key, &count);
    for (index = 0; index < count; index++) {
        char *command;
        char *wrapped;
        if (!g_str_equal(groups[index], G_KEY_FILE_DESKTOP_GROUP)
            && !g_str_has_prefix(groups[index], "Desktop Action ")) continue;
        command = g_key_file_get_string(key, groups[index], "Exec", NULL);
        if (command == NULL) continue;
        wrapped = g_strdup_printf(
            "/usr/bin/spaces enter --graphical %s -- %s",
            broker->space_name, command);
        g_key_file_set_string(key, groups[index], "Exec", wrapped);
        g_free(wrapped); g_free(command);
    }
    g_strfreev(groups);
    g_key_file_remove_key(key, G_KEY_FILE_DESKTOP_GROUP, "TryExec", NULL);
    g_key_file_remove_key(key, G_KEY_FILE_DESKTOP_GROUP, "Path", NULL);
    g_key_file_set_boolean(key, G_KEY_FILE_DESKTOP_GROUP,
        "DBusActivatable", FALSE);
    result = g_key_file_to_data(key, NULL, error);
    g_key_file_unref(key); return result;
}

static void portal_request_method(Broker *broker, GVariant *parameters,
    GDBusMethodInvocation *invocation)
{
    const char *interface;
    const char *method;
    const char *app_id;
    GVariant *wrapped;
    GVariant *input;
    GVariant *host_parameters = NULL;
    GVariant *options;
    guint response;
    GVariant *results = NULL;
    GUnixFDList *fds = g_dbus_message_get_unix_fd_list(
        g_dbus_method_invocation_get_message(invocation));
    GUnixFDList *outgoing_fds = NULL;
    char *staged_path = NULL;
    int secret_guest_fd = -1;
    int secret_host_fd = -1;
    GError *error = NULL;
    g_variant_get(parameters, "(&s&s&s@v)", &interface, &method, &app_id,
        &wrapped);
    input = g_variant_get_variant(wrapped); g_variant_unref(wrapped);
    if (g_str_equal(interface, "org.freedesktop.portal.Background")
        && g_str_equal(method, "RequestBackground")) {
        const char *parent;
        g_variant_get(input, "(&s@a{sv})", &parent, &options);
        GVariant *updated = broker_options(broker, options, TRUE);
        host_parameters = g_variant_ref_sink(g_variant_new("(s@a{sv})",
            parent, updated));
        g_variant_unref(options);
    } else if (g_str_equal(interface,
                   "org.freedesktop.portal.DynamicLauncher")
        && g_str_equal(method, "PrepareInstall")) {
        const char *parent, *name;
        GVariant *icon;
        g_variant_get(input, "(&s&s@v@a{sv})", &parent, &name, &icon,
            &options);
        GVariant *updated = broker_options(broker, options, FALSE);
        host_parameters = g_variant_ref_sink(g_variant_new("(ss@v@a{sv})",
            parent, name, icon, updated));
        g_variant_unref(options);
    } else if (g_str_equal(interface, "org.freedesktop.portal.Secret")
        && g_str_equal(method, "RetrieveSecret")) {
        gint handle;
        gint host_handle;
        if (strlen(app_id) > 255) {
            g_set_error(&error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
                "The portal application ID is invalid");
            goto failed;
        }
        g_variant_get(input, "(h@a{sv})", &handle, &options);
        secret_guest_fd = invocation_fd(invocation, handle, &error);
        if (secret_guest_fd < 0) { g_variant_unref(options); goto failed; }
#ifdef SYS_memfd_create
        secret_host_fd = (int)syscall(SYS_memfd_create,
            "spaces-portal-secret", 1U);
#else
        errno = ENOSYS;
        secret_host_fd = -1;
#endif
        if (secret_host_fd < 0) {
            g_variant_unref(options);
            g_set_error(&error, G_IO_ERROR, g_io_error_from_errno(errno),
                "Could not create the broker secret buffer: %s",
                g_strerror(errno));
            goto failed;
        }
        outgoing_fds = g_unix_fd_list_new();
        host_handle = g_unix_fd_list_append(outgoing_fds, secret_host_fd,
            &error);
        if (host_handle < 0) { g_variant_unref(options); goto failed; }
        GVariant *updated = broker_options(broker, options, FALSE);
        host_parameters = g_variant_ref_sink(g_variant_new("(h@a{sv})",
            host_handle, updated));
        g_variant_unref(options);
    } else if (g_str_equal(interface,
                   "org.freedesktop.portal.Wallpaper")
        && g_str_equal(method, "SetWallpaperFile")) {
        const char *parent;
        gint handle;
        gint host_handle;
        int staged;
        g_variant_get(input, "(&sh@a{sv})", &parent, &handle, &options);
        staged = stage_wallpaper_fd(invocation, handle, &staged_path, &error);
        if (staged < 0) { g_variant_unref(options); goto failed; }
        outgoing_fds = g_unix_fd_list_new();
        host_handle = g_unix_fd_list_append(outgoing_fds, staged, &error);
        close(staged);
        if (host_handle < 0) { g_variant_unref(options); goto failed; }
        GVariant *updated = broker_options(broker, options, FALSE);
        host_parameters = g_variant_ref_sink(g_variant_new("(sh@a{sv})",
            parent, host_handle, updated));
        g_variant_unref(options);
    } else if (g_str_equal(interface,
                   "org.freedesktop.portal.Wallpaper")
        && g_str_equal(method, "SetWallpaperURI")) {
        const char *parent, *uri;
        g_variant_get(input, "(&s&s@a{sv})", &parent, &uri, &options);
        if (g_str_has_prefix(uri, "file:")) {
            g_variant_unref(options);
            g_set_error(&error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
                "Use SetWallpaperFile for a guest-local wallpaper");
            goto failed;
        }
        GVariant *updated = broker_options(broker, options, FALSE);
        host_parameters = g_variant_ref_sink(g_variant_new("(ss@a{sv})",
            parent, uri, updated));
        g_variant_unref(options);
    } else {
        g_set_error(&error, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED,
            "This host portal request is not available through the broker");
        goto failed;
    }
    if (!wait_portal_request(broker, interface, method, host_parameters,
            &response, &results,
            outgoing_fds == NULL ? fds : outgoing_fds, &error)) goto failed;
    if (response == 0 && secret_host_fd >= 0
        && !finish_app_secret(app_id, secret_host_fd, secret_guest_fd)) {
        response = 2;
        g_clear_pointer(&results, g_variant_unref);
        results = g_variant_ref_sink(g_variant_new_array(
            G_VARIANT_TYPE("{sv}"), NULL, 0));
    }
    g_dbus_method_invocation_return_value(invocation,
        g_variant_new("(u@a{sv})", response, results));
    g_variant_unref(host_parameters); g_variant_unref(input);
    g_clear_object(&outgoing_fds);
    if (secret_host_fd >= 0) close(secret_host_fd);
    if (secret_guest_fd >= 0) close(secret_guest_fd);
    /* Desktop implementations may retain the path behind the wallpaper FD
     * after returning success. Keep accepted wallpaper data persistent. */
    if (staged_path != NULL && response != 0) unlink(staged_path);
    g_free(staged_path); return;
failed:
    g_clear_pointer(&host_parameters, g_variant_unref);
    g_clear_object(&outgoing_fds);
    if (secret_host_fd >= 0) {
        while (ftruncate(secret_host_fd, 0) < 0 && errno == EINTR) {}
        close(secret_host_fd);
    }
    if (secret_guest_fd >= 0) close(secret_guest_fd);
    if (staged_path != NULL) unlink(staged_path);
    g_free(staged_path);
    g_variant_unref(input);
    g_dbus_method_invocation_return_gerror(invocation, error);
    g_clear_error(&error);
}

static void dynamic_launcher_method(Broker *broker, GVariant *parameters,
    GDBusMethodInvocation *invocation)
{
    const char *method;
    GVariant *wrapped;
    GVariant *input;
    GVariant *host_parameters = NULL;
    const GVariantType *reply_type = G_VARIANT_TYPE_UNIT;
    GVariant *reply;
    GError *error = NULL;
    g_variant_get(parameters, "(&s@v)", &method, &wrapped);
    input = g_variant_get_variant(wrapped); g_variant_unref(wrapped);
    if (g_str_equal(method, "Install")) {
        const char *token, *desktop_id, *entry;
        GVariant *options;
        char *host_id, *host_entry;
        g_variant_get(input, "(&s&s&s@a{sv})", &token, &desktop_id, &entry,
            &options);
        host_id = broker_launcher_id(broker, desktop_id);
        host_entry = broker_desktop_entry(broker, entry, &error);
        if (host_id == NULL || host_entry == NULL) {
            g_free(host_id); g_free(host_entry); g_variant_unref(options);
            if (error == NULL) g_set_error(&error, G_DBUS_ERROR,
                G_DBUS_ERROR_INVALID_ARGS, "Invalid dynamic launcher");
            goto failed;
        }
        host_parameters = g_variant_ref_sink(g_variant_new("(sss@a{sv})",
            token, host_id, host_entry, options));
        g_free(host_id); g_free(host_entry);
    } else if (g_str_equal(method, "RequestInstallToken")) {
        host_parameters = g_variant_ref(input); reply_type = G_VARIANT_TYPE("(s)");
    } else if (g_str_equal(method, "Uninstall")
            || g_str_equal(method, "Launch")) {
        const char *desktop_id; GVariant *options; char *host_id;
        g_variant_get(input, "(&s@a{sv})", &desktop_id, &options);
        host_id = broker_launcher_id(broker, desktop_id);
        if (host_id == NULL) { g_variant_unref(options); goto invalid; }
        host_parameters = g_variant_ref_sink(g_variant_new("(s@a{sv})",
            host_id, options)); g_free(host_id);
    } else if (g_str_equal(method, "GetDesktopEntry")
            || g_str_equal(method, "GetIcon")) {
        const char *desktop_id; char *host_id;
        g_variant_get(input, "(&s)", &desktop_id);
        host_id = broker_launcher_id(broker, desktop_id);
        if (host_id == NULL) goto invalid;
        host_parameters = g_variant_ref_sink(g_variant_new("(s)", host_id));
        g_free(host_id);
        reply_type = g_str_equal(method, "GetDesktopEntry")
            ? G_VARIANT_TYPE("(s)") : G_VARIANT_TYPE("(vsu)");
    } else goto invalid;
    reply = g_dbus_connection_call_sync(broker->bus, PORTAL_NAME, PORTAL_PATH,
        "org.freedesktop.portal.DynamicLauncher", method, host_parameters,
        reply_type, G_DBUS_CALL_FLAGS_NO_AUTO_START, -1, NULL, &error);
    if (reply == NULL) goto failed;
    g_dbus_method_invocation_return_value(invocation,
        g_variant_new("(v)", reply));
    g_variant_unref(reply); g_variant_unref(host_parameters);
    g_variant_unref(input); return;
invalid:
    g_set_error(&error, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED,
        "This DynamicLauncher method is unavailable or invalid");
failed:
    g_clear_pointer(&host_parameters, g_variant_unref);
    g_variant_unref(input);
    g_dbus_method_invocation_return_gerror(invocation, error);
    g_clear_error(&error);
}

static void screenshot_method(Broker *broker, GVariant *parameters,
    GDBusMethodInvocation *invocation)
{
    const char *parent;
    const char *uri = NULL;
    GVariant *options;
    GVariant *results = NULL;
    guint response;
    char *filename = NULL;
    int descriptor = -1;
    int empty_descriptor = -1;
    struct stat metadata;
    GUnixFDList *fds = NULL;
    gint handle;
    GError *error = NULL;

    g_variant_get(parameters, "(&s@a{sv})", &parent, &options);
    if (!screenshot_request(broker, parent, options, &response, &results,
            &error)) {
        g_variant_unref(options);
        goto failed;
    }
    g_variant_unref(options);
    if (response == 0)
        g_variant_lookup(results, "uri", "&s", &uri);
    if (response == 0 && uri != NULL)
        filename = g_filename_from_uri(uri, NULL, &error);
    if (response == 0 && filename != NULL)
        descriptor = open(filename, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (response == 0 && (descriptor < 0 || fstat(descriptor, &metadata) < 0
            || !S_ISREG(metadata.st_mode) || metadata.st_size < 0
            || metadata.st_size > STAGED_FILE_MAXIMUM)) {
        g_clear_error(&error);
        g_set_error(&error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
            "The host screenshot result is not a bounded regular file");
        goto failed;
    }
    if (descriptor < 0) {
#ifdef SYS_memfd_create
        empty_descriptor = (int)syscall(SYS_memfd_create,
            "spaces-empty-screenshot", 1U);
#endif
        if (empty_descriptor < 0) goto failed;
        descriptor = empty_descriptor;
    }
    fds = g_unix_fd_list_new();
    handle = g_unix_fd_list_append(fds, descriptor, &error);
    if (handle < 0) goto failed;
    if (filename != NULL) unlink(filename);
    g_dbus_method_invocation_return_value_with_unix_fd_list(invocation,
        g_variant_new("(uh@a{sv})", response, handle, results), fds);
    g_object_unref(fds); close(descriptor); g_free(filename);
    return;
failed:
    if (fds != NULL) g_object_unref(fds);
    if (descriptor >= 0) close(descriptor);
    g_free(filename);
    g_clear_pointer(&results, g_variant_unref);
    if (error == NULL)
        g_set_error(&error, G_IO_ERROR, G_IO_ERROR_FAILED,
            "Could not import the host screenshot");
    g_dbus_method_invocation_return_gerror(invocation, error);
    g_clear_error(&error);
}

static int invocation_fd(
    GDBusMethodInvocation *invocation,
    int handle,
    GError **error
)
{
    GDBusMessage *message = g_dbus_method_invocation_get_message(invocation);
    GUnixFDList *fd_list = g_dbus_message_get_unix_fd_list(message);

    if (fd_list == NULL) {
        g_set_error(
            error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
            "The proof descriptor is missing"
        );
        return -1;
    }
    return g_unix_fd_list_get(fd_list, handle, error);
}

static gboolean pidfd_id(int descriptor, guint64 *identifier, GError **error)
{
    char path[64];
    char *contents = NULL;
    char **lines;
    guint index;
    gboolean found = FALSE;

    if (g_snprintf(path, sizeof(path), "/proc/self/fdinfo/%d", descriptor)
        >= (int)sizeof(path)
        || !g_file_get_contents(path, &contents, NULL, error))
        return FALSE;
    lines = g_strsplit(contents, "\n", -1);
    for (index = 0; lines[index] != NULL; index++) {
        char *end = NULL;
        guint64 value;

        if (!g_str_has_prefix(lines[index], "Pid:"))
            continue;
        errno = 0;
        value = g_ascii_strtoull(lines[index] + 4, &end, 10);
        while (end != NULL && g_ascii_isspace(*end))
            end++;
        if (errno == 0 && end != lines[index] + 4
            && end != NULL && *end == '\0' && value > 0) {
            *identifier = value;
            found = TRUE;
        }
        break;
    }
    g_strfreev(lines);
    g_free(contents);
    if (!found)
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
            "The descriptor is not a live pidfd");
    return found;
}

static void realtime_method(
    Broker *broker,
    GVariant *parameters,
    GDBusMethodInvocation *invocation
)
{
    int process_handle;
    int thread_handle;
    gboolean high_priority;
    gint priority;
    int process_fd = -1;
    int thread_fd = -1;
    guint64 process;
    guint64 thread;
    char task_path[96];
    struct stat metadata;
    struct rlimit realtime_limit;
    GVariant *limit_reply = NULL;
    GVariant *limit_value = NULL;
    GVariant *reply;
    GError *error = NULL;

    g_variant_get(parameters, "(hhbi)", &process_handle, &thread_handle,
        &high_priority, &priority);
    process_fd = invocation_fd(invocation, process_handle, &error);
    if (process_fd < 0)
        goto failed;
    thread_fd = invocation_fd(invocation, thread_handle, &error);
    if (thread_fd < 0)
        goto failed;
    if (!pidfd_id(process_fd, &process, &error)
        || !pidfd_id(thread_fd, &thread, &error))
        goto failed;
    if (g_snprintf(task_path, sizeof(task_path), "/proc/%" G_GUINT64_FORMAT
            "/task/%" G_GUINT64_FORMAT, process, thread)
        >= (int)sizeof(task_path) || stat(task_path, &metadata) < 0) {
        g_set_error(&error, G_IO_ERROR, G_IO_ERROR_PERMISSION_DENIED,
            "The thread does not belong to the supplied process");
        goto failed;
    }
    limit_reply = g_dbus_connection_call_sync(
        broker->system_bus, RTKIT_NAME, RTKIT_PATH,
        "org.freedesktop.DBus.Properties", "Get",
        g_variant_new("(ss)", RTKIT_NAME, "RTTimeUSecMax"),
        G_VARIANT_TYPE("(v)"), G_DBUS_CALL_FLAGS_NONE, 5000, NULL, &error
    );
    if (limit_reply == NULL)
        goto failed;
    g_variant_get(limit_reply, "(v)", &limit_value);
    if (!g_variant_is_of_type(limit_value, G_VARIANT_TYPE_INT64)
        || g_variant_get_int64(limit_value) <= 0) {
        g_set_error(&error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
            "RealtimeKit returned an invalid realtime timeout limit");
        goto failed;
    }
    realtime_limit.rlim_cur = (rlim_t)g_variant_get_int64(limit_value);
    realtime_limit.rlim_max = realtime_limit.rlim_cur;
    if (prlimit((pid_t)process, RLIMIT_RTTIME, &realtime_limit, NULL) < 0) {
        g_set_error(&error, G_IO_ERROR, g_io_error_from_errno(errno),
            "Could not constrain the target realtime timeout: %s",
            g_strerror(errno));
        goto failed;
    }
    /* The public Realtime portal maps IDs through the D-Bus sender's PID
     * namespace. A host broker cannot submit a process in an nspawn child
     * namespace through that API after resolving its pidfds. Invoke the same
     * RealtimeKit operation only after the pidfd and thread-membership checks
     * above; RealtimeKit still applies the host's scheduling limits. */
    reply = g_dbus_connection_call_sync(
        broker->system_bus, RTKIT_NAME, RTKIT_PATH, RTKIT_NAME,
        high_priority ? "MakeThreadHighPriorityWithPID"
                      : "MakeThreadRealtimeWithPID",
        high_priority
            ? g_variant_new("(tti)", process, thread, priority)
            : g_variant_new("(ttu)", process, thread, (guint)priority),
        G_VARIANT_TYPE_UNIT, G_DBUS_CALL_FLAGS_NONE, -1, NULL, &error
    );
    if (reply == NULL)
        goto failed;
    g_dbus_method_invocation_return_value(invocation, reply);
    g_variant_unref(reply);
    g_variant_unref(limit_value);
    g_variant_unref(limit_reply);
    close(thread_fd);
    close(process_fd);
    return;

failed:
    g_clear_pointer(&limit_value, g_variant_unref);
    g_clear_pointer(&limit_reply, g_variant_unref);
    if (thread_fd >= 0)
        close(thread_fd);
    if (process_fd >= 0)
        close(process_fd);
    g_dbus_method_invocation_return_gerror(invocation, error);
    g_clear_error(&error);
}

static void game_mode_method(Broker *broker, GVariant *parameters,
    GDBusMethodInvocation *invocation)
{
    const char *method;
    gint target_handle, requester_handle;
    int target_fd = -1, requester_fd = -1;
    GUnixFDList *fds = g_dbus_message_get_unix_fd_list(
        g_dbus_method_invocation_get_message(invocation));
    GVariant *reply;
    GError *error = NULL;

    g_variant_get(parameters, "(&shh)", &method, &target_handle,
        &requester_handle);
    if (!g_str_equal(method, "QueryStatusByPIDFd")
        && !g_str_equal(method, "RegisterGameByPIDFd")
        && !g_str_equal(method, "UnregisterGameByPIDFd")) {
        g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
            G_DBUS_ERROR_ACCESS_DENIED, "Unsupported GameMode operation");
        return;
    }
    if (fds != NULL) {
        target_fd = g_unix_fd_list_get(fds, target_handle, &error);
        if (target_fd >= 0)
            requester_fd = g_unix_fd_list_get(fds, requester_handle, &error);
    }
    if (target_fd < 0 || requester_fd < 0) goto failed;
    close(target_fd); target_fd = -1;
    close(requester_fd); requester_fd = -1;
    reply = g_dbus_connection_call_with_unix_fd_list_sync(broker->bus,
        PORTAL_NAME, PORTAL_PATH, "org.freedesktop.portal.GameMode", method,
        g_variant_new("(hh)", target_handle, requester_handle),
        G_VARIANT_TYPE("(i)"), G_DBUS_CALL_FLAGS_NO_AUTO_START, -1, fds, NULL, NULL,
        &error);
    if (reply == NULL) goto failed;
    g_dbus_method_invocation_return_value(invocation, reply);
    g_variant_unref(reply);
    return;
failed:
    if (target_fd >= 0) close(target_fd);
    if (requester_fd >= 0) close(requester_fd);
    if (error == NULL)
        g_set_error(&error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
            "GameMode pidfds are missing");
    g_dbus_method_invocation_return_gerror(invocation, error);
    g_clear_error(&error);
}

static gboolean same_object(int left, int right)
{
    struct stat left_metadata;
    struct stat right_metadata;
    return fstat(left, &left_metadata) == 0
        && fstat(right, &right_metadata) == 0
        && left_metadata.st_dev == right_metadata.st_dev
        && left_metadata.st_ino == right_metadata.st_ino
        && (left_metadata.st_mode & S_IFMT)
            == (right_metadata.st_mode & S_IFMT);
}

static void resolve_path_method(Broker *broker, GVariant *parameters,
    GDBusMethodInvocation *invocation)
{
    const char *guest_path;
    char descriptor_path[64];
    char *host_path = NULL;
    char *host_uri = NULL;
    int handle;
    int proof = -1;
    int mapped = -1;
    Mapping *mapping;
    GError *error = NULL;

    g_variant_get(parameters, "(&sh)", &guest_path, &handle);
    if (!valid_guest_path(guest_path)
        || (mapping = find_mapping(broker, guest_path)) == NULL) {
        g_set_error(&error, G_IO_ERROR, G_IO_ERROR_PERMISSION_DENIED,
            "The guest path has no active host mapping");
        goto failed;
    }
    proof = invocation_fd(invocation, handle, &error);
    if (proof < 0) goto failed;
    mapped = secure_open(mapping, guest_path, O_PATH);
    if (mapped < 0 || !same_object(proof, mapped)) {
        g_set_error(&error, G_IO_ERROR, G_IO_ERROR_PERMISSION_DENIED,
            "The proof does not identify the mapped host path");
        goto failed;
    }
    if (g_snprintf(descriptor_path, sizeof(descriptor_path),
            "/proc/self/fd/%d", mapped) >= (int)sizeof(descriptor_path)) {
        g_set_error(&error, G_IO_ERROR, G_IO_ERROR_FILENAME_TOO_LONG,
            "The mapped descriptor path is too long");
        goto failed;
    }
    host_path = g_file_read_link(descriptor_path, &error);
    if (host_path == NULL) goto failed;
    if (!g_path_is_absolute(host_path)
        || g_str_has_suffix(host_path, " (deleted)")) {
        g_set_error(&error, G_IO_ERROR, G_IO_ERROR_INVALID_FILENAME,
            "The mapped descriptor has no stable host path");
        goto failed;
    }
    host_uri = g_filename_to_uri(host_path, NULL, &error);
    if (host_uri == NULL) goto failed;
    g_dbus_method_invocation_return_value(invocation,
        g_variant_new("(s)", host_uri));
    close(mapped); close(proof); g_free(host_uri); g_free(host_path);
    return;

failed:
    if (mapped >= 0) close(mapped);
    if (proof >= 0) close(proof);
    g_free(host_uri); g_free(host_path);
    if (error == NULL)
        g_set_error(&error, G_IO_ERROR, G_IO_ERROR_FAILED,
            "Could not resolve the mapped host path");
    g_dbus_method_invocation_return_gerror(invocation, error);
    g_clear_error(&error);
}

static void stage_file_method(Broker *broker, GVariant *parameters,
    GDBusMethodInvocation *invocation)
{
    int handle;
    int source = -1;
    int output = -1;
    char *directory = NULL;
    char *token = NULL;
    char *filename = NULL;
    char *uri = NULL;
    GError *error = NULL;

    g_variant_get(parameters, "(h)", &handle);
    source = invocation_fd(invocation, handle, &error);
    if (source < 0) goto failed;
    directory = g_build_filename(g_get_user_cache_dir(), "spaces",
        "artwork", NULL);
    if (g_mkdir_with_parents(directory, 0700) < 0) goto io_failed;
    token = new_token();
    filename = g_build_filename(directory, token, NULL);
    output = open(filename, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (output < 0) goto io_failed;
    if (!copy_staged_fd(source, output, "Artwork", &error)) goto failed;
    if (close(output) < 0) { output = -1; goto io_failed; }
    output = -1;
    uri = g_filename_to_uri(filename, NULL, &error);
    if (uri == NULL) goto failed;
    g_dbus_method_invocation_return_value(invocation,
        g_variant_new("(s)", uri));
    g_ptr_array_add(broker->staged_files, g_strdup(filename));
    close(source); g_free(uri); g_free(filename);
    g_free(token); g_free(directory); return;
io_failed:
    if (error == NULL)
        g_set_error(&error, G_IO_ERROR, g_io_error_from_errno(errno),
            "Could not stage guest artwork: %s", g_strerror(errno));
failed:
    if (output >= 0) close(output);
    if (filename != NULL) unlink(filename);
    if (source >= 0) close(source);
    g_free(uri); g_free(filename); g_free(token); g_free(directory);
    if (error == NULL)
        g_set_error(&error, G_IO_ERROR, G_IO_ERROR_FAILED,
            "Could not stage guest artwork");
    g_dbus_method_invocation_return_gerror(invocation, error);
    g_clear_error(&error);
}

static void remove_staged_file_method(Broker *broker, GVariant *parameters,
    GDBusMethodInvocation *invocation)
{
    const char *uri;
    char *filename;
    guint index;
    g_variant_get(parameters, "(&s)", &uri);
    filename = g_filename_from_uri(uri, NULL, NULL);
    if (filename == NULL) {
        g_dbus_method_invocation_return_error(invocation, G_IO_ERROR,
            G_IO_ERROR_INVALID_ARGUMENT, "Invalid staged file URI");
        return;
    }
    for (index = 0; index < broker->staged_files->len; index++) {
        const char *staged = g_ptr_array_index(broker->staged_files, index);
        if (!g_str_equal(staged, filename)) continue;
        unlink(staged);
        g_ptr_array_remove_index(broker->staged_files, index);
        g_free(filename);
        g_dbus_method_invocation_return_value(invocation, NULL);
        return;
    }
    g_free(filename);
    g_dbus_method_invocation_return_error(invocation, G_IO_ERROR,
        G_IO_ERROR_NOT_FOUND, "The staged file is not owned by this broker");
}

static void open_method(
    GDBusConnection *connection,
    const char *sender,
    const char *object_path,
    const char *interface_name,
    const char *method_name,
    GVariant *parameters,
    GDBusMethodInvocation *invocation,
    gpointer user_data
)
{
    Broker *broker = user_data;
    const char *guest_path;
    const char *activation_token;
    gboolean requested_writable = FALSE;
    gboolean guest_writable = FALSE;
    gboolean writable = FALSE;
    int handle;
    int proof = -1;
    int mapped = -1;
    int flags;
    Mapping *mapping;
    struct stat proof_metadata;
    struct stat mapped_metadata;
    guint response;
    GError *error = NULL;

    (void)connection;
    (void)sender;
    (void)object_path;
    (void)interface_name;
    if (g_str_equal(method_name, "MakeRealtime")) {
        realtime_method(broker, parameters, invocation);
        return;
    }
    if (g_str_equal(method_name, "MakeGameMode")) {
        game_mode_method(broker, parameters, invocation);
        return;
    }
    if (g_str_equal(method_name, "Screenshot")) {
        screenshot_method(broker, parameters, invocation);
        return;
    }
    if (g_str_equal(method_name, "StageFile")) {
        stage_file_method(broker, parameters, invocation);
        return;
    }
    if (g_str_equal(method_name, "ResolvePath")) {
        resolve_path_method(broker, parameters, invocation);
        return;
    }
    if (g_str_equal(method_name, "RemoveStagedFile")) {
        remove_staged_file_method(broker, parameters, invocation);
        return;
    }
    if (g_str_equal(method_name, "PortalRequest")) {
        portal_request_method(broker, parameters, invocation);
        return;
    }
    if (g_str_equal(method_name, "DynamicLauncherCall")) {
        dynamic_launcher_method(broker, parameters, invocation);
        return;
    }
    if (g_str_equal(method_name, "OpenFile"))
        g_variant_get(
            parameters, "(&shb&s)", &guest_path, &handle,
            &requested_writable, &activation_token
        );
    else
        g_variant_get(
            parameters, "(&sh&s)", &guest_path, &handle, &activation_token
        );
    if (!valid_guest_path(guest_path)
        || (mapping = find_mapping(broker, guest_path)) == NULL) {
        g_dbus_method_invocation_return_error(
            invocation, G_IO_ERROR, G_IO_ERROR_PERMISSION_DENIED,
            "The guest path has no active host mapping"
        );
        return;
    }
    proof = invocation_fd(invocation, handle, &error);
    if (proof < 0)
        goto failed;
    if (fstat(proof, &proof_metadata) < 0) {
        g_set_error(
            &error, G_IO_ERROR, g_io_error_from_errno(errno),
            "Could not inspect the guest proof descriptor: %s",
            g_strerror(errno)
        );
        goto failed;
    }
    if (requested_writable) {
        int proof_flags = fcntl(proof, F_GETFL);

        guest_writable = proof_flags >= 0
            && (proof_flags & O_ACCMODE) == O_RDWR;
    }
    flags = O_RDONLY;
    if (guest_writable && g_str_equal(method_name, "OpenFile")) {
        mapped = secure_open(mapping, guest_path, O_RDWR);
        if (mapped >= 0)
            writable = TRUE;
    }
    if (mapped < 0)
        mapped = secure_open(mapping, guest_path, flags);
    if (mapped < 0) {
        g_set_error(
            &error, G_IO_ERROR, g_io_error_from_errno(errno),
            "Could not securely reopen the mapped path: %s",
            g_strerror(errno)
        );
        goto failed;
    }
    if (fstat(mapped, &mapped_metadata) < 0) {
        g_set_error(
            &error, G_IO_ERROR, g_io_error_from_errno(errno),
            "Could not inspect the mapped descriptor: %s",
            g_strerror(errno)
        );
        goto failed;
    }
    if (proof_metadata.st_dev != mapped_metadata.st_dev
        || proof_metadata.st_ino != mapped_metadata.st_ino
        || (proof_metadata.st_mode & S_IFMT)
            != (mapped_metadata.st_mode & S_IFMT)) {
        g_set_error(
            &error, G_IO_ERROR, G_IO_ERROR_PERMISSION_DENIED,
            "The guest proof does not identify the mapped object"
        );
        goto failed;
    }
    if (!call_host_portal(
            broker, method_name, mapped, writable, activation_token,
            &response, &error
        ))
        goto failed;
    g_dbus_method_invocation_return_value(
        invocation, g_variant_new("(u)", response)
    );
    close(mapped);
    close(proof);
    return;

failed:
    if (mapped >= 0)
        close(mapped);
    if (proof >= 0)
        close(proof);
    g_dbus_method_invocation_return_gerror(invocation, error);
    g_clear_error(&error);
}

static const GDBusInterfaceVTable open_vtable = {
    .method_call = open_method,
};

static gboolean add_mapping(
    Broker *broker,
    const char *guest,
    const char *descriptor_text
)
{
    char *end = NULL;
    long value;
    Mapping *mapping;

    errno = 0;
    value = strtol(descriptor_text, &end, 10);
    if (errno != 0 || end == descriptor_text || *end != '\0'
        || value < 0 || value > INT32_MAX || !valid_guest_path(guest))
        return FALSE;
    mapping = g_new0(Mapping, 1);
    mapping->guest = g_strdup(guest);
    mapping->descriptor = (int)value;
    if (fstat(mapping->descriptor, &mapping->metadata) < 0) {
        mapping_free(mapping);
        return FALSE;
    }
    g_ptr_array_add(broker->mappings, mapping);
    return TRUE;
}

int main(int argc, char **argv)
{
    Broker broker = {0};
    GDBusNodeInfo *node;
    GVariant *reply;
    GError *error = NULL;
    const char *name = NULL;
    int ready_fd = -1;
    guint registration;
    guint request_name_result;
    guint sigterm_source = 0;
    guint sigint_source = 0;
    int index;

    signal(SIGPIPE, SIG_IGN);
    broker.mappings = g_ptr_array_new_with_free_func(mapping_free);
    broker.staged_files = g_ptr_array_new_with_free_func(g_free);
    for (index = 1; index < argc; index++) {
        if (g_str_equal(argv[index], "--name") && index + 1 < argc)
            name = argv[++index];
        else if (g_str_equal(argv[index], "--space") && index + 1 < argc)
            broker.space_name = g_strdup(argv[++index]);
        else if (g_str_equal(argv[index], "--app-id") && index + 1 < argc)
            broker.app_id = g_strdup(argv[++index]);
        else if (g_str_equal(argv[index], "--ready-fd") && index + 1 < argc)
            ready_fd = atoi(argv[++index]);
        else if (g_str_equal(argv[index], "--map") && index + 2 < argc) {
            if (!add_mapping(
                    &broker, argv[index + 1], argv[index + 2]
                )) {
                g_printerr("spaces-broker: invalid mapping\n");
                return 2;
            }
            index += 2;
        } else {
            g_printerr("spaces-broker: invalid arguments\n");
            return 2;
        }
    }
    if (name == NULL || ready_fd < 0 || broker.mappings->len == 0
        || broker.space_name == NULL || broker.app_id == NULL)
        return 2;
    broker.bus = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &error);
    if (broker.bus == NULL)
        goto failed;
    broker.system_bus = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
    if (broker.system_bus == NULL)
        goto failed;
    reply = g_dbus_connection_call_sync(broker.bus, PORTAL_NAME, PORTAL_PATH,
        "org.freedesktop.host.portal.Registry", "Register",
        g_variant_new("(s@a{sv})", broker.app_id,
            g_variant_new_array(G_VARIANT_TYPE("{sv}"), NULL, 0)),
        G_VARIANT_TYPE_UNIT, G_DBUS_CALL_FLAGS_NO_AUTO_START, 5000, NULL, NULL);
    if (reply != NULL) g_variant_unref(reply);
    node = g_dbus_node_info_new_for_xml(open_xml, &error);
    if (node == NULL)
        goto failed;
    registration = g_dbus_connection_register_object(
        broker.bus, INTEGRATION_PATH, node->interfaces[0],
        &open_vtable, &broker, NULL, &error
    );
    if (registration == 0) {
        g_dbus_node_info_unref(node);
        goto failed;
    }
    reply = g_dbus_connection_call_sync(
        broker.bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
        "org.freedesktop.DBus", "RequestName",
        g_variant_new("(su)", name, 4U), G_VARIANT_TYPE("(u)"),
        G_DBUS_CALL_FLAGS_NONE, 5000, NULL, &error
    );
    if (reply == NULL) {
        g_dbus_connection_unregister_object(broker.bus, registration);
        g_dbus_node_info_unref(node);
        goto failed;
    }
    g_variant_get(reply, "(u)", &request_name_result);
    g_variant_unref(reply);
    if (request_name_result != 1U) {
        g_set_error(
            &error, G_IO_ERROR, G_IO_ERROR_ADDRESS_IN_USE,
            "The broker bus name is already owned"
        );
        g_dbus_connection_unregister_object(broker.bus, registration);
        g_dbus_node_info_unref(node);
        goto failed;
    }
    if (write(ready_fd, "1", 1) != 1) {
        g_dbus_connection_unregister_object(broker.bus, registration);
        g_dbus_node_info_unref(node);
        goto failed;
    }
    close(ready_fd);
    broker.loop = g_main_loop_new(NULL, FALSE);
    sigterm_source = g_unix_signal_add(SIGTERM, stop_broker, broker.loop);
    sigint_source = g_unix_signal_add(SIGINT, stop_broker, broker.loop);
    g_main_loop_run(broker.loop);
    g_source_remove(sigterm_source);
    g_source_remove(sigint_source);
    g_main_loop_unref(broker.loop);
    g_dbus_connection_unregister_object(broker.bus, registration);
    g_dbus_node_info_unref(node);
    g_object_unref(broker.system_bus);
    g_object_unref(broker.bus);
    for (index = 0; index < (int)broker.staged_files->len; index++)
        unlink(g_ptr_array_index(broker.staged_files, index));
    g_ptr_array_unref(broker.staged_files);
    g_ptr_array_unref(broker.mappings);
    g_free(broker.app_id); g_free(broker.space_name);
    return 0;

failed:
    if (error != NULL) {
        g_printerr("spaces-broker: %s\n", error->message);
        g_clear_error(&error);
    }
    if (ready_fd >= 0)
        close(ready_fd);
    if (broker.bus != NULL)
        g_object_unref(broker.bus);
    if (broker.system_bus != NULL)
        g_object_unref(broker.system_bus);
    for (index = 0; index < (int)broker.staged_files->len; index++)
        unlink(g_ptr_array_index(broker.staged_files, index));
    g_ptr_array_unref(broker.staged_files);
    g_ptr_array_unref(broker.mappings);
    g_free(broker.app_id); g_free(broker.space_name);
    return 1;
}
