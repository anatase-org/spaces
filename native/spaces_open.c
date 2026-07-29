#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define OPEN_PATH "/org/anatase/Spaces/Open"
#define OPEN_INTERFACE "org.anatase.Spaces.Open1"
#define PORTAL_NAME "org.freedesktop.portal.Desktop"
#define PORTAL_PATH "/org/freedesktop/portal/desktop"

#if defined(__x86_64__) || defined(__aarch64__)
typedef int (*main_function)(int, char **, char **);
extern int spaces_old_libc_start_main(
    main_function, int, char **, void (*)(void), void (*)(void),
    void (*)(void), void *
);
#if defined(__x86_64__)
__asm__(".symver spaces_old_libc_start_main,"
        "__libc_start_main@GLIBC_2.2.5");
#else
__asm__(".symver spaces_old_libc_start_main,"
        "__libc_start_main@GLIBC_2.17");
#endif
int __wrap___libc_start_main(
    main_function function, int argc, char **argv, void (*init)(void),
    void (*fini)(void), void (*rtld_fini)(void), void *stack_end
)
{
    return spaces_old_libc_start_main(
        function, argc, argv, init, fini, rtld_fini, stack_end
    );
}
#endif

typedef struct {
    GMainLoop *loop;
    char *request_path;
    guint response;
    gboolean received;
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
    g_variant_unref(results);
    response->received = TRUE;
    g_main_loop_quit(response->loop);
}

static gboolean response_timeout(gpointer data)
{
    Response *response = data;

    g_main_loop_quit(response->loop);
    return G_SOURCE_REMOVE;
}

static GDBusConnection *host_connection(GError **error)
{
    char *address;
    const char *test_address = g_getenv("SPACES_PORTAL_TEST_ADDRESS");
    GDBusConnection *connection;

    if (test_address != NULL)
        address = g_strdup(test_address);
    else
        address = g_strdup_printf(
            "unix:path=/run/spaces/desktop/%u/portal/bus",
            (unsigned)getuid()
        );
    connection = g_dbus_connection_new_for_address_sync(
        address,
        G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT
            | G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
        NULL, NULL, error
    );
    g_free(address);
    return connection;
}

static guint open_uri(
    GDBusConnection *connection,
    const char *uri,
    const char *activation_token,
    GError **error
)
{
    GVariantBuilder options;
    GVariant *reply;
    guint response = 2;
    char *token = new_token();
    const char *request_path;
    Response pending = {0};
    guint subscription;
    guint timeout;

    pending.loop = g_main_loop_new(NULL, FALSE);
    subscription = g_dbus_connection_signal_subscribe(
        connection, PORTAL_NAME, "org.freedesktop.portal.Request",
        "Response", NULL, NULL, G_DBUS_SIGNAL_FLAGS_NONE,
        response_signal, &pending, NULL
    );
    g_variant_builder_init(&options, G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add(
        &options, "{sv}", "handle_token", g_variant_new_string(token)
    );
    if (activation_token != NULL && *activation_token != '\0')
        g_variant_builder_add(
            &options, "{sv}", "activation_token",
            g_variant_new_string(activation_token)
        );
    g_free(token);
    reply = g_dbus_connection_call_sync(
        connection, PORTAL_NAME, PORTAL_PATH,
        "org.freedesktop.portal.OpenURI", "OpenURI",
        g_variant_new("(ss@a{sv})", "", uri,
                      g_variant_builder_end(&options)),
        G_VARIANT_TYPE("(o)"), G_DBUS_CALL_FLAGS_NONE, -1, NULL, error
    );
    if (reply == NULL) {
        g_dbus_connection_signal_unsubscribe(connection, subscription);
        g_main_loop_unref(pending.loop);
        return response;
    }
    g_variant_get(reply, "(&o)", &request_path);
    pending.request_path = g_strdup(request_path);
    g_variant_unref(reply);
    timeout = g_timeout_add_seconds(120, response_timeout, &pending);
    g_main_loop_run(pending.loop);
    if (pending.received)
        g_source_remove(timeout);
    g_dbus_connection_signal_unsubscribe(connection, subscription);
    g_main_loop_unref(pending.loop);
    g_free(pending.request_path);
    if (!pending.received)
        g_set_error(
            error, G_IO_ERROR, G_IO_ERROR_TIMED_OUT,
            "Timed out waiting for the host portal"
        );
    else
        response = pending.response;
    return response;
}

static guint open_path(
    GDBusConnection *connection,
    const char *path,
    gboolean force_directory,
    gboolean folder,
    const char *activation_token,
    GError **error
)
{
    const char *broker_name = g_getenv("SPACES_OPEN_BROKER");
    GUnixFDList *fd_list;
    GVariant *reply;
    struct stat metadata;
    gboolean writable = FALSE;
    const char *method;
    guint response = 2;
    int descriptor;
    int handle;

    if (broker_name == NULL || *broker_name == '\0') {
        g_set_error(
            error, G_IO_ERROR, G_IO_ERROR_NOT_CONNECTED,
            "The Spaces open broker is unavailable"
        );
        return response;
    }
    descriptor = open(path, O_RDWR | O_CLOEXEC);
    if (descriptor >= 0)
        writable = TRUE;
    else
        descriptor = open(path, O_RDONLY | O_CLOEXEC);
    if (descriptor < 0) {
        g_set_error(
            error, G_IO_ERROR, g_io_error_from_errno(errno),
            "Could not open %s: %s", path, g_strerror(errno)
        );
        return response;
    }
    if (syscall(SYS_fstat, descriptor, &metadata) < 0) {
        g_set_error(
            error, G_IO_ERROR, g_io_error_from_errno(errno),
            "Could not inspect %s: %s", path, g_strerror(errno)
        );
        close(descriptor);
        return response;
    }
    if (force_directory) {
        writable = FALSE;
        method = "OpenDirectory";
    } else if (S_ISDIR(metadata.st_mode)) {
        writable = FALSE;
        method = folder ? "OpenFile" : "OpenDirectory";
    } else if (S_ISREG(metadata.st_mode)) {
        method = "OpenFile";
    } else {
        g_set_error(
            error, G_IO_ERROR, G_IO_ERROR_NOT_SUPPORTED,
            "Only regular files and directories can be opened on the host"
        );
        close(descriptor);
        return response;
    }
    fd_list = g_unix_fd_list_new();
    handle = g_unix_fd_list_append(fd_list, descriptor, error);
    close(descriptor);
    if (handle < 0) {
        g_object_unref(fd_list);
        return response;
    }
    if (g_str_equal(method, "OpenDirectory"))
        reply = g_dbus_connection_call_with_unix_fd_list_sync(
            connection, broker_name, OPEN_PATH, OPEN_INTERFACE, method,
            g_variant_new(
                "(shs)", path, handle,
                activation_token == NULL ? "" : activation_token
            ),
            G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, -1,
            fd_list, NULL, NULL, error
        );
    else
        reply = g_dbus_connection_call_with_unix_fd_list_sync(
            connection, broker_name, OPEN_PATH, OPEN_INTERFACE, method,
            g_variant_new(
                "(shbs)", path, handle, writable,
                activation_token == NULL ? "" : activation_token
            ),
            G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, -1,
            fd_list, NULL, NULL, error
        );
    g_object_unref(fd_list);
    if (reply != NULL) {
        g_variant_get(reply, "(u)", &response);
        g_variant_unref(reply);
    }
    return response;
}

int main(int argc, char **argv)
{
    gboolean force_directory = FALSE;
    gboolean folder = FALSE;
    const char *argument;
    const char *activation_token;
    GDBusConnection *connection;
    GUri *parsed;
    const char *scheme;
    char *path = NULL;
    char *absolute = NULL;
    char *canonical = NULL;
    guint response;
    GError *error = NULL;
    int index = 1;

    signal(SIGPIPE, SIG_IGN);
    if (index < argc && g_str_equal(argv[index], "--folder")) {
        folder = TRUE;
        index++;
    } else if (index < argc && g_str_equal(argv[index], "--directory")) {
        force_directory = TRUE;
        index++;
    }
    if (index + 1 != argc) {
        g_printerr(
            "Usage: spaces-open [--folder|--directory] URI-OR-PATH\n"
        );
        return 2;
    }
    argument = argv[index];
    activation_token = g_getenv("XDG_ACTIVATION_TOKEN");
    connection = host_connection(&error);
    if (connection == NULL)
        goto failed;
    parsed = g_uri_parse(argument, G_URI_FLAGS_PARSE_RELAXED, NULL);
    scheme = parsed == NULL ? NULL : g_uri_get_scheme(parsed);
    if (scheme != NULL && !g_ascii_strcasecmp(scheme, "file")) {
        path = g_filename_from_uri(argument, NULL, &error);
        if (path == NULL) {
            if (parsed != NULL)
                g_uri_unref(parsed);
            goto failed_connection;
        }
    } else if (scheme != NULL) {
        response = open_uri(
            connection, argument, activation_token, &error
        );
        if (parsed != NULL)
            g_uri_unref(parsed);
        g_object_unref(connection);
        if (error != NULL)
            goto failed;
        return response == 0 ? 0 : 1;
    } else {
        path = g_strdup(argument);
    }
    if (parsed != NULL)
        g_uri_unref(parsed);
    if (!g_path_is_absolute(path)) {
        char *working = g_get_current_dir();
        absolute = g_build_filename(working, path, NULL);
        g_free(working);
    } else {
        absolute = g_strdup(path);
    }
    canonical = realpath(absolute, NULL);
    if (canonical == NULL) {
        g_set_error(
            &error, G_IO_ERROR, g_io_error_from_errno(errno),
            "Could not resolve %s: %s", absolute, g_strerror(errno)
        );
        goto failed_connection;
    }
    response = open_path(
        connection, canonical, force_directory, folder,
        activation_token, &error
    );
    free(canonical);
    g_free(absolute);
    g_free(path);
    g_object_unref(connection);
    if (error != NULL)
        goto failed;
    return response == 0 ? 0 : 1;

failed_connection:
    g_clear_pointer(&absolute, g_free);
    g_clear_pointer(&path, g_free);
    g_object_unref(connection);
failed:
    g_printerr(
        "spaces-open: %s\n",
        error == NULL ? "opening failed" : error->message
    );
    g_clear_error(&error);
    return 1;
}
