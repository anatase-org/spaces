#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <linux/openat2.h>
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define OPEN_PATH "/org/anatase/Spaces/Open"
#define OPEN_INTERFACE "org.anatase.Spaces.Open1"
#define PORTAL_NAME "org.freedesktop.portal.Desktop"
#define PORTAL_PATH "/org/freedesktop/portal/desktop"

static const char open_xml[] =
    "<node>"
    " <interface name='" OPEN_INTERFACE "'>"
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
    " </interface>"
    "</node>";

typedef struct {
    char *guest;
    int descriptor;
    struct stat metadata;
} Mapping;

typedef struct {
    GDBusConnection *bus;
    GMainLoop *loop;
    GPtrArray *mappings;
} Broker;

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
        G_VARIANT_TYPE("(o)"), G_DBUS_CALL_FLAGS_NONE, -1,
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
    return TRUE;
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
    int index;

    signal(SIGPIPE, SIG_IGN);
    broker.mappings = g_ptr_array_new_with_free_func(mapping_free);
    for (index = 1; index < argc; index++) {
        if (g_str_equal(argv[index], "--name") && index + 1 < argc)
            name = argv[++index];
        else if (g_str_equal(argv[index], "--ready-fd") && index + 1 < argc)
            ready_fd = atoi(argv[++index]);
        else if (g_str_equal(argv[index], "--map") && index + 2 < argc) {
            if (!add_mapping(
                    &broker, argv[index + 1], argv[index + 2]
                )) {
                g_printerr("spaces-open-broker: invalid mapping\n");
                return 2;
            }
            index += 2;
        } else {
            g_printerr("spaces-open-broker: invalid arguments\n");
            return 2;
        }
    }
    if (name == NULL || ready_fd < 0 || broker.mappings->len == 0)
        return 2;
    broker.bus = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &error);
    if (broker.bus == NULL)
        goto failed;
    node = g_dbus_node_info_new_for_xml(open_xml, &error);
    if (node == NULL)
        goto failed;
    registration = g_dbus_connection_register_object(
        broker.bus, OPEN_PATH, node->interfaces[0],
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
    g_main_loop_run(broker.loop);
    g_main_loop_unref(broker.loop);
    g_dbus_connection_unregister_object(broker.bus, registration);
    g_dbus_node_info_unref(node);
    g_object_unref(broker.bus);
    g_ptr_array_unref(broker.mappings);
    return 0;

failed:
    if (error != NULL) {
        g_printerr("spaces-open-broker: %s\n", error->message);
        g_clear_error(&error);
    }
    if (ready_fd >= 0)
        close(ready_fd);
    if (broker.bus != NULL)
        g_object_unref(broker.bus);
    g_ptr_array_unref(broker.mappings);
    return 1;
}
