#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <signal.h>
#include <stdint.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "portal_interfaces.h"

#define DESKTOP_PATH "/org/freedesktop/portal/desktop"
#define HOST_NAME "org.freedesktop.portal.Desktop"
#define BACKEND_NAME "org.freedesktop.impl.portal.desktop.spaces"
#define FILE_MANAGER_NAME "org.freedesktop.FileManager1"
#define FILE_MANAGER_PATH "/org/freedesktop/FileManager1"
#define NOTIFICATIONS_NAME "org.freedesktop.Notifications"
#define NOTIFICATIONS_PATH "/org/freedesktop/Notifications"
#define SCREEN_SAVER_NAME "org.freedesktop.ScreenSaver"
#define SCREEN_SAVER_PATH "/org/freedesktop/ScreenSaver"
#define SCREEN_SAVER_LEGACY_PATH "/ScreenSaver"
#define DBUS_NAME "org.freedesktop.DBus"
#define DBUS_PATH "/org/freedesktop/DBus"
#define INTERFACE_DIRECTORY "/usr/share/dbus-1/interfaces"
#define RESTORE_DATA_VENDOR "Spaces"
#define RESTORE_DATA_VERSION 1
#define SECRET_MAXIMUM 4096
#define SECRET_SIZE 64

static const char app_chooser_xml[] =
    "<node>"
    " <interface name='org.freedesktop.impl.portal.AppChooser'>"
    "  <method name='ChooseApplication'>"
    "   <arg type='o' direction='in'/>"
    "   <arg type='s' direction='in'/>"
    "   <arg type='s' direction='in'/>"
    "   <arg type='as' direction='in'/>"
    "   <arg type='a{sv}' direction='in'/>"
    "   <arg type='u' direction='out'/>"
    "   <arg type='a{sv}' direction='out'/>"
    "  </method>"
    "  <method name='UpdateChoices'>"
    "   <arg type='o' direction='in'/>"
    "   <arg type='as' direction='in'/>"
    "  </method>"
    "  <property name='version' type='u' access='read'/>"
    " </interface>"
    "</node>";

static const char file_manager_xml[] =
    "<node>"
    " <interface name='" FILE_MANAGER_NAME "'>"
    "  <method name='ShowItems'>"
    "   <arg type='as' direction='in'/>"
    "   <arg type='s' direction='in'/>"
    "  </method>"
    "  <method name='ShowFolders'>"
    "   <arg type='as' direction='in'/>"
    "   <arg type='s' direction='in'/>"
    "  </method>"
    "  <method name='ShowItemProperties'>"
    "   <arg type='as' direction='in'/>"
    "   <arg type='s' direction='in'/>"
    "  </method>"
    " </interface>"
    "</node>";

static const char notifications_xml[] =
    "<node>"
    " <interface name='" NOTIFICATIONS_NAME "'>"
    "  <method name='GetCapabilities'>"
    "   <arg type='as' direction='out'/>"
    "  </method>"
    "  <method name='Notify'>"
    "   <arg type='s' direction='in'/>"
    "   <arg type='u' direction='in'/>"
    "   <arg type='s' direction='in'/>"
    "   <arg type='s' direction='in'/>"
    "   <arg type='s' direction='in'/>"
    "   <arg type='as' direction='in'/>"
    "   <arg type='a{sv}' direction='in'/>"
    "   <arg type='i' direction='in'/>"
    "   <arg type='u' direction='out'/>"
    "  </method>"
    "  <method name='CloseNotification'>"
    "   <arg type='u' direction='in'/>"
    "  </method>"
    "  <method name='GetServerInformation'>"
    "   <arg type='s' direction='out'/>"
    "   <arg type='s' direction='out'/>"
    "   <arg type='s' direction='out'/>"
    "   <arg type='s' direction='out'/>"
    "  </method>"
    "  <signal name='NotificationClosed'>"
    "   <arg type='u'/><arg type='u'/>"
    "  </signal>"
    "  <signal name='ActionInvoked'>"
    "   <arg type='u'/><arg type='s'/>"
    "  </signal>"
    "  <signal name='ActivationToken'>"
    "   <arg type='u'/><arg type='s'/>"
    "  </signal>"
    " </interface>"
    "</node>";

static const char screen_saver_xml[] =
    "<node>"
    " <interface name='" SCREEN_SAVER_NAME "'>"
    "  <method name='Lock'/>"
    "  <method name='SimulateUserActivity'/>"
    "  <method name='GetActive'>"
    "   <arg type='b' direction='out'/>"
    "  </method>"
    "  <method name='GetActiveTime'>"
    "   <arg type='u' direction='out'/>"
    "  </method>"
    "  <method name='GetSessionIdleTime'>"
    "   <arg type='u' direction='out'/>"
    "  </method>"
    "  <method name='SetActive'>"
    "   <arg type='b' direction='in'/>"
    "   <arg type='b' direction='out'/>"
    "  </method>"
    "  <method name='Inhibit'>"
    "   <arg type='s' direction='in'/>"
    "   <arg type='s' direction='in'/>"
    "   <arg type='u' direction='out'/>"
    "  </method>"
    "  <method name='UnInhibit'>"
    "   <arg type='u' direction='in'/>"
    "  </method>"
    "  <method name='Throttle'>"
    "   <arg type='s' direction='in'/>"
    "   <arg type='s' direction='in'/>"
    "   <arg type='u' direction='out'/>"
    "  </method>"
    "  <method name='UnThrottle'>"
    "   <arg type='u' direction='in'/>"
    "  </method>"
    "  <signal name='ActiveChanged'><arg type='b'/></signal>"
    " </interface>"
    "</node>";

#if defined(__x86_64__) || defined(__aarch64__)
typedef int (*main_function)(int, char **, char **);
extern int spaces_old_libc_start_main(
    main_function,
    int,
    char **,
    void (*)(void),
    void (*)(void),
    void (*)(void),
    void *
);
#if defined(__x86_64__)
__asm__(".symver spaces_old_libc_start_main,"
        "__libc_start_main@GLIBC_2.2.5");
#else
__asm__(".symver spaces_old_libc_start_main,"
        "__libc_start_main@GLIBC_2.17");
#endif
int __wrap___libc_start_main(
    main_function function,
    int argc,
    char **argv,
    void (*init)(void),
    void (*fini)(void),
    void (*rtld_fini)(void),
    void *stack_end
)
{
    return spaces_old_libc_start_main(
        function, argc, argv, init, fini, rtld_fini, stack_end
    );
}
#endif

static const char *backend_names[] = {
    "Account", "AppChooser", "Clipboard", "Email", "GlobalShortcuts", "Inhibit",
    "InputCapture", "Notification", "Print", "RemoteDesktop",
    "ScreenCast", "Screenshot", "Secret", "Settings", "Wallpaper",
};

static const guint backend_versions[] = {
    1, 2, 1, 4, 2, 3, 2, 2, 4, 2, 6, 3, 1, 1, 1,
};

typedef struct {
    GDBusMethodInvocation *invocation;
    char *guest_handle;
    char *guest_session;
    char *host_handle;
    char *interface_name;
    char *method_name;
    guint request_registration;
    gboolean cancelled;
    gboolean persistent;
    char *secret_app_id;
    int secret_guest_fd;
    int secret_host_fd;
} Pending;

typedef struct {
    char *host_path;
    guint registration;
} Session;

typedef struct {
    char *sender;
    guint cookie;
    gboolean throttle;
} Inhibitor;

typedef struct {
    GMainLoop *loop;
    GDBusConnection *guest;
    GDBusConnection *host;
    GPtrArray *node_infos;
    GArray *registrations;
    GHashTable *pending_by_guest;
    GHashTable *pending_by_host;
    GHashTable *sessions_by_guest;
    GHashTable *guest_by_host;
    GHashTable *inhibitors;
    GDBusInterfaceInfo *request_info;
    GDBusInterfaceInfo *session_info;
    char *app_id;
    char *bootstrap_portal_owner;
    char *screen_saver_path;
    gboolean notifications_owned;
    gboolean screen_saver_owned;
    int exit_status;
} Portal;

typedef struct {
    Portal *portal;
    Pending *pending;
    gboolean request;
    gboolean create_session;
    gboolean synthesize_response;
    gboolean persistent_request;
} HostCall;

typedef struct {
    Portal *portal;
    GDBusMethodInvocation *invocation;
    char *sender;
    char *inhibitor_key;
    gboolean add_inhibitor;
    gboolean throttle;
} NativeCall;

static GDBusInterfaceInfo *find_interface(Portal *portal, const char *name)
{
    guint index;

    for (index = 0; index < portal->node_infos->len; index++) {
        GDBusNodeInfo *node = g_ptr_array_index(portal->node_infos, index);
        GDBusInterfaceInfo *info = g_dbus_node_info_lookup_interface(node, name);
        if (info != NULL)
            return info;
    }
    return NULL;
}

static guint adapter_version(const char *interface_name)
{
    const char *suffix = interface_name
        + strlen("org.freedesktop.impl.portal.");
    guint index;

    for (index = 0; index < G_N_ELEMENTS(backend_names); index++) {
        if (g_str_equal(suffix, backend_names[index]))
            return backend_versions[index];
    }
    return 1;
}

static GDBusMethodInfo *find_method(
    GDBusInterfaceInfo *interface_info,
    const char *name
)
{
    GDBusMethodInfo **methods;

    for (methods = interface_info->methods; *methods != NULL; methods++) {
        if (g_str_equal((*methods)->name, name))
            return *methods;
    }
    return NULL;
}

static gboolean interface_has_signal(
    GDBusInterfaceInfo *interface_info,
    const char *name
)
{
    GDBusSignalInfo **signals;

    if (interface_info == NULL)
        return FALSE;
    for (signals = interface_info->signals;
         signals != NULL && *signals != NULL;
         signals++) {
        if (g_str_equal((*signals)->name, name))
            return TRUE;
    }
    return FALSE;
}

static void inhibitor_free(gpointer data)
{
    Inhibitor *inhibitor = data;

    g_free(inhibitor->sender);
    g_free(inhibitor);
}

static char *inhibitor_key(gboolean throttle, guint cookie)
{
    return g_strdup_printf("%c:%u", throttle ? 't' : 'i', cookie);
}

static void truncate_secret_fd(int descriptor)
{
    int result;

    do {
        result = ftruncate(descriptor, 0);
    } while (result < 0 && errno == EINTR);
}

static void pending_free(gpointer data)
{
    Pending *pending = data;

    if (pending->secret_host_fd >= 0) {
        truncate_secret_fd(pending->secret_host_fd);
        close(pending->secret_host_fd);
    }
    if (pending->secret_guest_fd >= 0)
        close(pending->secret_guest_fd);
    g_clear_object(&pending->invocation);
    g_free(pending->guest_handle);
    g_free(pending->guest_session);
    g_free(pending->host_handle);
    g_free(pending->interface_name);
    g_free(pending->method_name);
    g_free(pending->secret_app_id);
    g_free(pending);
}

static void secure_clear(void *data, gsize size)
{
    volatile unsigned char *cursor = data;

    while (size-- > 0)
        *cursor++ = 0;
}

gboolean portal_derive_secret(
    const guint8 *host_secret,
    gsize host_secret_size,
    const char *app_id,
    guint8 output[SECRET_SIZE]
)
{
    static const guint8 domain[] =
        "org.anatase.spaces.portal-secret.v1";
    GHmac *hmac;
    guint8 length[4];
    gsize app_id_size;
    gsize output_size = SECRET_SIZE;

    if (host_secret == NULL || host_secret_size == 0
        || app_id == NULL || output == NULL)
        return FALSE;
    app_id_size = strlen(app_id);
    if (app_id_size > UINT32_MAX)
        return FALSE;
    length[0] = (guint8)(app_id_size >> 24);
    length[1] = (guint8)(app_id_size >> 16);
    length[2] = (guint8)(app_id_size >> 8);
    length[3] = (guint8)app_id_size;

    hmac = g_hmac_new(
        G_CHECKSUM_SHA512, host_secret, host_secret_size
    );
    if (hmac == NULL)
        return FALSE;
    g_hmac_update(hmac, domain, sizeof(domain));
    g_hmac_update(hmac, length, sizeof(length));
    g_hmac_update(hmac, (const guint8 *)app_id, app_id_size);
    g_hmac_get_digest(hmac, output, &output_size);
    g_hmac_unref(hmac);
    return output_size == SECRET_SIZE;
}

static gboolean write_all(int descriptor, const guint8 *data, gsize size)
{
    while (size > 0) {
        ssize_t written = write(descriptor, data, size);

        if (written < 0 && errno == EINTR)
            continue;
        if (written <= 0)
            return FALSE;
        data += written;
        size -= (gsize)written;
    }
    return TRUE;
}

static gboolean finish_secret(Pending *pending)
{
    guint8 raw[SECRET_MAXIMUM + 1];
    guint8 derived[SECRET_SIZE];
    gsize size = 0;
    gboolean success = FALSE;

    if (pending->secret_host_fd < 0
        || pending->secret_guest_fd < 0
        || pending->secret_app_id == NULL
        || lseek(pending->secret_host_fd, 0, SEEK_SET) < 0)
        goto out;
    while (size < sizeof(raw)) {
        ssize_t count = read(
            pending->secret_host_fd, raw + size, sizeof(raw) - size
        );

        if (count < 0 && errno == EINTR)
            continue;
        if (count < 0)
            goto out;
        if (count == 0)
            break;
        size += (gsize)count;
    }
    if (size == 0 || size > SECRET_MAXIMUM)
        goto out;
    if (!portal_derive_secret(
            raw, size, pending->secret_app_id, derived
        ))
        goto out;
    success = write_all(
        pending->secret_guest_fd, derived, sizeof(derived)
    );

out:
    secure_clear(raw, sizeof(raw));
    secure_clear(derived, sizeof(derived));
    if (pending->secret_host_fd >= 0)
        truncate_secret_fd(pending->secret_host_fd);
    return success;
}

static int create_secret_memfd(void)
{
#ifdef SYS_memfd_create
    return (int)syscall(SYS_memfd_create, "spaces-portal-secret", 1U);
#else
    errno = ENOSYS;
    return -1;
#endif
}

static void session_free(gpointer data)
{
    Session *session = data;

    g_free(session->host_path);
    g_free(session);
}

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

static gboolean bootstrap_caller_is_portal(
    Portal *portal,
    const char *sender,
    GError **error
)
{
    GVariant *reply;
    guint process_id;
    char *process_link;
    char *executable;
    gboolean matches;

    reply = g_dbus_connection_call_sync(
        portal->guest, DBUS_NAME, DBUS_PATH, DBUS_NAME,
        "GetConnectionUnixProcessID",
        g_variant_new("(s)", sender), G_VARIANT_TYPE("(u)"),
        G_DBUS_CALL_FLAGS_NONE, 5000, NULL, error
    );
    if (reply == NULL)
        return FALSE;
    g_variant_get(reply, "(u)", &process_id);
    g_variant_unref(reply);

    process_link = g_strdup_printf("/proc/%u/exe", process_id);
    executable = g_file_read_link(process_link, error);
    g_free(process_link);
    if (executable == NULL)
        return FALSE;
    matches = g_str_equal(executable, "/usr/libexec/xdg-desktop-portal")
        || g_str_equal(executable, "/usr/lib/xdg-desktop-portal");
    g_free(executable);
    if (!matches)
        return FALSE;

    if (portal->bootstrap_portal_owner == NULL)
        portal->bootstrap_portal_owner = g_strdup(sender);
    return g_str_equal(portal->bootstrap_portal_owner, sender);
}

static gboolean caller_is_portal(
    Portal *portal,
    const char *sender,
    gboolean allow_bootstrap,
    GError **error
)
{
    GVariant *reply;
    GError *local_error = NULL;
    const char *owner;
    gboolean matches;

    reply = g_dbus_connection_call_sync(
        portal->guest, DBUS_NAME, DBUS_PATH, DBUS_NAME, "GetNameOwner",
        g_variant_new("(s)", HOST_NAME), G_VARIANT_TYPE("(s)"),
        G_DBUS_CALL_FLAGS_NONE, 5000, NULL, &local_error
    );
    if (reply == NULL) {
        if (allow_bootstrap
            && g_error_matches(
                local_error, G_DBUS_ERROR, G_DBUS_ERROR_NAME_HAS_NO_OWNER
            )) {
            g_clear_error(&local_error);
            return bootstrap_caller_is_portal(portal, sender, error);
        }
        if (error != NULL)
            g_propagate_error(error, local_error);
        else
            g_clear_error(&local_error);
        return FALSE;
    }
    g_variant_get(reply, "(&s)", &owner);
    matches = g_str_equal(owner, sender);
    g_variant_unref(reply);
    if (matches)
        g_clear_pointer(&portal->bootstrap_portal_owner, g_free);
    return matches;
}

static void return_access_denied(GDBusMethodInvocation *invocation)
{
    g_dbus_method_invocation_return_error(
        invocation, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED,
        "Only the guest portal frontend may call this backend"
    );
}

static GVariant *empty_dict(void)
{
    GVariantBuilder builder;

    g_variant_builder_init(&builder, G_VARIANT_TYPE_VARDICT);
    return g_variant_builder_end(&builder);
}

static const char *mapped_session(Portal *portal, const char *guest_path)
{
    Session *session = g_hash_table_lookup(
        portal->sessions_by_guest, guest_path
    );
    return session == NULL ? NULL : session->host_path;
}

static char *restore_token_from_data(GVariant *restore_data)
{
    const char *vendor;
    guint version;
    GVariant *wrapped;
    GVariant *private_data;
    char *token = NULL;

    if (!g_variant_is_of_type(restore_data, G_VARIANT_TYPE("(suv)")))
        return NULL;

    g_variant_get(
        restore_data, "(&su@v)", &vendor, &version, &wrapped
    );
    private_data = g_variant_get_variant(wrapped);
    if (g_str_equal(vendor, RESTORE_DATA_VENDOR)
        && version == RESTORE_DATA_VERSION
        && g_variant_is_of_type(private_data, G_VARIANT_TYPE_STRING))
        token = g_variant_dup_string(private_data, NULL);
    g_variant_unref(private_data);
    g_variant_unref(wrapped);
    return token;
}

GVariant *portal_restore_options_to_host(GVariant *options)
{
    GVariantDict dictionary;
    GVariant *restore_data;
    char *restore_token = NULL;

    restore_data = g_variant_lookup_value(options, "restore_data", NULL);
    if (restore_data != NULL) {
        restore_token = restore_token_from_data(restore_data);
        g_variant_unref(restore_data);
    }

    g_variant_dict_init(&dictionary, options);
    /*
     * restore_data is private to a backend. The host public frontend accepts
     * only its opaque public token, and data from another backend is ignored.
     */
    g_variant_dict_remove(&dictionary, "restore_data");
    g_variant_dict_remove(&dictionary, "restore_token");
    if (restore_token != NULL)
        g_variant_dict_insert(
            &dictionary, "restore_token", "s", restore_token
        );
    g_free(restore_token);
    return g_variant_dict_end(&dictionary);
}

GVariant *portal_restore_results_to_backend(GVariant *results)
{
    GVariantDict dictionary;
    const char *restore_token = NULL;

    g_variant_lookup(
        results, "restore_token", "&s", &restore_token
    );
    g_variant_dict_init(&dictionary, results);
    /*
     * The guest frontend will turn this backend-private value into its own
     * one-shot public restore token. Keeping the host token inside the value
     * makes the handoff stateless for this adapter.
     */
    g_variant_dict_remove(&dictionary, "restore_token");
    if (restore_token != NULL) {
        GVariant *private_data = g_variant_new_variant(
            g_variant_new_string(restore_token)
        );
        GVariant *restore_data = g_variant_new(
            "(su@v)",
            RESTORE_DATA_VENDOR,
            RESTORE_DATA_VERSION,
            private_data
        );

        g_variant_dict_insert_value(
            &dictionary, "restore_data", restore_data
        );
    }
    return g_variant_dict_end(&dictionary);
}

static gboolean method_accepts_restore_data(
    const char *interface_name,
    const char *method_name
)
{
    return (g_str_equal(
                interface_name,
                "org.freedesktop.impl.portal.ScreenCast"
            )
            && g_str_equal(method_name, "SelectSources"))
        || (g_str_equal(
                interface_name,
                "org.freedesktop.impl.portal.RemoteDesktop"
            )
            && g_str_equal(method_name, "SelectDevices"))
        || (g_str_equal(
                interface_name,
                "org.freedesktop.impl.portal.InputCapture"
            )
            && g_str_equal(method_name, "Start"));
}

static gboolean method_returns_restore_data(
    const char *interface_name,
    const char *method_name
)
{
    return g_str_equal(method_name, "Start")
        && (g_str_equal(
                interface_name,
                "org.freedesktop.impl.portal.ScreenCast"
            )
            || g_str_equal(
                interface_name,
                "org.freedesktop.impl.portal.RemoteDesktop"
            )
            || g_str_equal(
                interface_name,
                "org.freedesktop.impl.portal.InputCapture"
            ));
}

static GVariant *options_with_tokens(
    Pending *pending,
    GVariant *options,
    const char *request_token,
    const char *session_token
)
{
    GVariantDict dictionary;
    GVariant *translated = NULL;
    GVariant *updated;

    if (method_accepts_restore_data(
            pending->interface_name, pending->method_name
        ))
        translated = portal_restore_options_to_host(options);
    g_variant_dict_init(
        &dictionary, translated == NULL ? options : translated
    );
    if (request_token != NULL)
        g_variant_dict_insert(
            &dictionary, "handle_token", "s", request_token
        );
    if (session_token != NULL)
        g_variant_dict_insert(
            &dictionary, "session_handle_token", "s", session_token
        );
    updated = g_variant_dict_end(&dictionary);
    g_clear_pointer(&translated, g_variant_unref);
    return updated;
}

static gboolean method_returns_request(GDBusMethodInfo *method)
{
    return method->out_args != NULL
        && method->out_args[0] != NULL
        && method->out_args[1] == NULL
        && g_str_equal(method->out_args[0]->signature, "o");
}

static GVariantType *method_output_type(GDBusMethodInfo *method)
{
    GDBusArgInfo **argument;
    GString *signature = g_string_new("(");
    GVariantType *type;

    for (argument = method->out_args;
         argument != NULL && *argument != NULL;
         argument++)
        g_string_append(signature, (*argument)->signature);
    g_string_append_c(signature, ')');
    type = g_variant_type_new(signature->str);
    g_string_free(signature, TRUE);
    return type;
}

static gboolean host_path_has_screen_saver(
    Portal *portal,
    const char *path
)
{
    GVariant *reply;
    GDBusNodeInfo *node;
    const char *xml;
    GError *error = NULL;
    gboolean available = FALSE;

    reply = g_dbus_connection_call_sync(
        portal->host, SCREEN_SAVER_NAME, path,
        "org.freedesktop.DBus.Introspectable", "Introspect", NULL,
        G_VARIANT_TYPE("(s)"), G_DBUS_CALL_FLAGS_NONE, 3000, NULL,
        &error
    );
    if (reply == NULL) {
        g_clear_error(&error);
        return FALSE;
    }
    g_variant_get(reply, "(&s)", &xml);
    node = g_dbus_node_info_new_for_xml(xml, &error);
    if (node != NULL) {
        available = g_dbus_node_info_lookup_interface(
            node, SCREEN_SAVER_NAME
        ) != NULL;
        g_dbus_node_info_unref(node);
    }
    g_clear_error(&error);
    g_variant_unref(reply);
    return available;
}

static void discover_screen_saver_path(Portal *portal)
{
    const char *path = NULL;

    if (host_path_has_screen_saver(portal, SCREEN_SAVER_PATH))
        path = SCREEN_SAVER_PATH;
    else if (host_path_has_screen_saver(
                 portal, SCREEN_SAVER_LEGACY_PATH
             ))
        path = SCREEN_SAVER_LEGACY_PATH;
    g_free(portal->screen_saver_path);
    portal->screen_saver_path = g_strdup(path);
}

static void native_call_free(NativeCall *call)
{
    g_clear_object(&call->invocation);
    g_free(call->sender);
    g_free(call->inhibitor_key);
    g_free(call);
}

static gboolean guest_name_has_owner(Portal *portal, const char *name)
{
    GVariant *reply;
    gboolean owned = FALSE;

    reply = g_dbus_connection_call_sync(
        portal->guest,
        DBUS_NAME,
        DBUS_PATH,
        DBUS_NAME,
        "NameHasOwner",
        g_variant_new("(s)", name),
        G_VARIANT_TYPE("(b)"),
        G_DBUS_CALL_FLAGS_NONE,
        3000,
        NULL,
        NULL
    );
    if (reply != NULL) {
        g_variant_get(reply, "(b)", &owned);
        g_variant_unref(reply);
    }
    return owned;
}

static void release_host_inhibitor(
    Portal *portal,
    gboolean throttle,
    guint cookie
)
{
    if (portal->screen_saver_path == NULL)
        return;
    g_dbus_connection_call(
        portal->host,
        SCREEN_SAVER_NAME,
        portal->screen_saver_path,
        SCREEN_SAVER_NAME,
        throttle ? "UnThrottle" : "UnInhibit",
        g_variant_new("(u)", cookie),
        NULL,
        G_DBUS_CALL_FLAGS_NONE,
        -1,
        NULL,
        NULL,
        NULL
    );
}

static void native_call_done(
    GObject *source,
    GAsyncResult *result,
    gpointer user_data
)
{
    NativeCall *call = user_data;
    GVariant *reply;
    GError *error = NULL;

    reply = g_dbus_connection_call_finish(
        G_DBUS_CONNECTION(source), result, &error
    );
    if (reply == NULL) {
        g_dbus_method_invocation_return_gerror(call->invocation, error);
        g_clear_error(&error);
        native_call_free(call);
        return;
    }
    if (call->add_inhibitor) {
        guint cookie;
        Inhibitor *inhibitor;
        char *key;

        g_variant_get(reply, "(u)", &cookie);
        inhibitor = g_new0(Inhibitor, 1);
        inhibitor->sender = g_strdup(call->sender);
        inhibitor->cookie = cookie;
        inhibitor->throttle = call->throttle;
        if (guest_name_has_owner(call->portal, call->sender)) {
            key = inhibitor_key(call->throttle, cookie);
            g_hash_table_replace(
                call->portal->inhibitors, key, inhibitor
            );
        } else {
            release_host_inhibitor(
                call->portal, call->throttle, cookie
            );
            inhibitor_free(inhibitor);
        }
    } else if (call->inhibitor_key != NULL) {
        g_hash_table_remove(
            call->portal->inhibitors, call->inhibitor_key
        );
    }
    g_dbus_method_invocation_return_value(call->invocation, reply);
    g_variant_unref(reply);
    native_call_free(call);
}

static void native_method_call(
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
    Portal *portal = user_data;
    GDBusInterfaceInfo *interface_info;
    GDBusMethodInfo *method;
    GVariantType *reply_type;
    const char *host_path;
    NativeCall *call;

    (void)connection;
    (void)object_path;
    interface_info = find_interface(portal, interface_name);
    method = interface_info == NULL
        ? NULL : find_method(interface_info, method_name);
    if (method == NULL) {
        g_dbus_method_invocation_return_error(
            invocation, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD,
            "Unknown desktop integration method"
        );
        return;
    }
    if (g_str_equal(interface_name, NOTIFICATIONS_NAME)) {
        if (!portal->notifications_owned) {
            g_dbus_method_invocation_return_error(
                invocation,
                G_DBUS_ERROR,
                G_DBUS_ERROR_NAME_HAS_NO_OWNER,
                "The guest Notifications bridge name is unavailable"
            );
            return;
        }
        host_path = NOTIFICATIONS_PATH;
    } else {
        if (!portal->screen_saver_owned) {
            g_dbus_method_invocation_return_error(
                invocation,
                G_DBUS_ERROR,
                G_DBUS_ERROR_NAME_HAS_NO_OWNER,
                "The guest ScreenSaver bridge name is unavailable"
            );
            return;
        }
        host_path = portal->screen_saver_path;
        if (host_path == NULL) {
            g_dbus_method_invocation_return_error(
                invocation,
                G_DBUS_ERROR,
                G_DBUS_ERROR_NAME_HAS_NO_OWNER,
                "The host ScreenSaver service is unavailable"
            );
            return;
        }
    }

    call = g_new0(NativeCall, 1);
    call->portal = portal;
    call->invocation = g_object_ref(invocation);
    call->sender = g_strdup(sender);
    if (g_str_equal(interface_name, SCREEN_SAVER_NAME)) {
        if (g_str_equal(method_name, "Inhibit")
            || g_str_equal(method_name, "Throttle")) {
            call->add_inhibitor = TRUE;
            call->throttle = g_str_equal(method_name, "Throttle");
        } else if (g_str_equal(method_name, "UnInhibit")
                   || g_str_equal(method_name, "UnThrottle")) {
            gboolean throttle = g_str_equal(method_name, "UnThrottle");
            Inhibitor *inhibitor;
            guint cookie;

            g_variant_get(parameters, "(u)", &cookie);
            call->inhibitor_key = inhibitor_key(throttle, cookie);
            inhibitor = g_hash_table_lookup(
                portal->inhibitors, call->inhibitor_key
            );
            if (inhibitor == NULL) {
                g_dbus_method_invocation_return_error(
                    invocation,
                    G_DBUS_ERROR,
                    G_DBUS_ERROR_INVALID_ARGS,
                    "Unknown ScreenSaver inhibitor cookie"
                );
                native_call_free(call);
                return;
            }
            if (!g_str_equal(inhibitor->sender, sender)) {
                g_dbus_method_invocation_return_error(
                    invocation,
                    G_DBUS_ERROR,
                    G_DBUS_ERROR_ACCESS_DENIED,
                    "The ScreenSaver inhibitor belongs to another client"
                );
                native_call_free(call);
                return;
            }
        }
    }
    reply_type = method_output_type(method);
    g_dbus_connection_call(
        portal->host, interface_name, host_path, interface_name,
        method_name, parameters, reply_type, G_DBUS_CALL_FLAGS_NONE, -1,
        NULL, native_call_done, call
    );
    g_variant_type_free(reply_type);
}

static const GDBusInterfaceVTable native_vtable = {
    .method_call = native_method_call,
};

static gboolean method_creates_session(
    const char *interface_name,
    const char *method_name
)
{
    return g_str_equal(method_name, "CreateSession")
        || g_str_equal(method_name, "CreateSession2")
        || (g_str_equal(interface_name, "Inhibit")
            && g_str_equal(method_name, "CreateMonitor"));
}

static GVariant *build_host_parameters(
    Portal *portal,
    GDBusMethodInfo *backend_method,
    GVariant *parameters,
    gboolean request,
    gboolean create_session,
    Pending *pending,
    GError **error
)
{
    GVariantBuilder tuple;
    GDBusArgInfo **arguments;
    gsize index = 0;
    gboolean had_options = FALSE;
    char *request_token = request ? new_token() : NULL;
    char *session_token = create_session ? new_token() : NULL;

    g_variant_builder_init(&tuple, G_VARIANT_TYPE_TUPLE);
    for (arguments = backend_method->in_args;
         arguments != NULL && *arguments != NULL;
         arguments++, index++) {
        GDBusArgInfo *argument = *arguments;
        GVariant *child = g_variant_get_child_value(parameters, index);

        if (g_str_equal(argument->name, "handle")) {
            pending->guest_handle = g_variant_dup_string(child, NULL);
            g_variant_unref(child);
            continue;
        }
        if (g_str_equal(argument->name, "app_id")) {
            if (g_str_equal(
                    pending->interface_name,
                    "org.freedesktop.impl.portal.Secret"
                ))
                pending->secret_app_id = g_variant_dup_string(
                    child, NULL
                );
            g_variant_unref(child);
            continue;
        }
        if (g_str_equal(argument->name, "session_handle")) {
            const char *guest_path = g_variant_get_string(child, NULL);
            if (pending->guest_session == NULL)
                pending->guest_session = g_strdup(guest_path);
            if (create_session) {
                g_variant_unref(child);
                continue;
            }
            const char *host_path = mapped_session(portal, guest_path);
            if (host_path == NULL) {
                g_set_error(
                    error, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS,
                    "Unknown portal session"
                );
                g_variant_unref(child);
                g_free(request_token);
                g_free(session_token);
                return NULL;
            }
            g_variant_unref(child);
            child = g_variant_new_object_path(host_path);
        } else if (g_str_equal(argument->name, "options")) {
            GVariant *updated = options_with_tokens(
                pending, child, request_token, session_token
            );
            g_variant_unref(child);
            child = updated;
            had_options = TRUE;
        }
        g_variant_builder_add_value(&tuple, child);
    }

    if (!had_options
        && ((g_str_equal(pending->interface_name, "GlobalShortcuts")
             && g_str_equal(pending->method_name, "ListShortcuts"))
            || (g_str_equal(pending->interface_name, "Inhibit")
                && g_str_equal(pending->method_name, "CreateMonitor")))) {
        g_variant_builder_add_value(
            &tuple,
            options_with_tokens(
                pending, empty_dict(), request_token, session_token
            )
        );
    }
    g_free(request_token);
    g_free(session_token);
    return g_variant_builder_end(&tuple);
}

static GVariant *replace_session_result(
    Portal *portal,
    Pending *pending,
    GVariant *results
);

static void close_host_object(
    Portal *portal,
    const char *path,
    const char *interface_name
)
{
    if (path == NULL)
        return;
    g_dbus_connection_call(
        portal->host, HOST_NAME, path, interface_name, "Close", NULL, NULL,
        G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL
    );
}

static void dynamic_method_call(
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
    Portal *portal = user_data;
    GError *error = NULL;

    (void)connection;
    (void)parameters;
    if (!caller_is_portal(portal, sender, FALSE, &error)) {
        g_clear_error(&error);
        return_access_denied(invocation);
        return;
    }
    if (!g_str_equal(method_name, "Close")) {
        g_dbus_method_invocation_return_error(
            invocation, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD,
            "Unknown lifecycle method"
        );
        return;
    }
    if (g_str_equal(interface_name, "org.freedesktop.impl.portal.Request")) {
        Pending *pending = g_hash_table_lookup(
            portal->pending_by_guest, object_path
        );
        if (pending != NULL) {
            pending->cancelled = TRUE;
            close_host_object(
                portal, pending->host_handle,
                "org.freedesktop.portal.Request"
            );
            if (pending->persistent) {
                if (pending->request_registration != 0)
                    g_dbus_connection_unregister_object(
                        portal->guest, pending->request_registration
                    );
                if (pending->host_handle != NULL)
                    g_hash_table_remove(
                        portal->pending_by_host, pending->host_handle
                    );
                g_hash_table_remove(
                    portal->pending_by_guest, object_path
                );
            }
        }
    } else {
        Session *session = g_hash_table_lookup(
            portal->sessions_by_guest, object_path
        );
        if (session != NULL)
            close_host_object(
                portal, session->host_path,
                "org.freedesktop.portal.Session"
            );
    }
    g_dbus_method_invocation_return_value(invocation, NULL);
}

static const GDBusInterfaceVTable dynamic_vtable = {
    .method_call = dynamic_method_call,
};

static void register_request(Portal *portal, Pending *pending)
{
    GError *error = NULL;

    if (pending->guest_handle == NULL)
        return;
    pending->request_registration = g_dbus_connection_register_object(
        portal->guest, pending->guest_handle,
        portal->request_info, &dynamic_vtable,
        portal, NULL, &error
    );
    if (pending->request_registration == 0) {
        g_warning("Could not export request %s: %s",
                  pending->guest_handle, error->message);
        g_clear_error(&error);
    }
}

static void register_session(
    Portal *portal,
    const char *guest_path,
    const char *host_path
)
{
    Session *session;
    GError *error = NULL;

    if (guest_path == NULL || host_path == NULL)
        return;
    session = g_new0(Session, 1);
    session->host_path = g_strdup(host_path);
    session->registration = g_dbus_connection_register_object(
        portal->guest, guest_path, portal->session_info,
        &dynamic_vtable, portal, NULL, &error
    );
    if (session->registration == 0) {
        g_warning("Could not export session %s: %s",
                  guest_path, error->message);
        g_clear_error(&error);
        session_free(session);
        return;
    }
    g_hash_table_replace(
        portal->guest_by_host, g_strdup(host_path), g_strdup(guest_path)
    );
    g_hash_table_replace(
        portal->sessions_by_guest, g_strdup(guest_path), session
    );
}

static void finish_pending(
    Portal *portal,
    Pending *pending,
    guint response,
    GVariant *results
)
{
    GDBusInterfaceInfo *interface_info;
    GDBusMethodInfo *method;
    guint outputs = 0;

    if (response == 0
        && pending->secret_host_fd >= 0
        && (pending->cancelled || !finish_secret(pending))) {
        response = pending->cancelled ? 1 : 2;
        g_variant_unref(results);
        results = empty_dict();
    }
    if (response == 0) {
        if (method_returns_restore_data(
                pending->interface_name, pending->method_name
            )) {
            GVariant *translated =
                portal_restore_results_to_backend(results);

            g_variant_unref(results);
            results = translated;
        }
        GVariant *mapped = replace_session_result(
            portal, pending, results
        );

        g_variant_unref(results);
        results = mapped;
    }
    interface_info = find_interface(portal, pending->interface_name);
    method = find_method(interface_info, pending->method_name);
    if (method->out_args != NULL) {
        while (method->out_args[outputs] != NULL)
            outputs++;
    }
    if (outputs == 2) {
        g_dbus_method_invocation_return_value(
            pending->invocation,
            g_variant_new("(u@a{sv})", response, g_variant_ref(results))
        );
    } else if (outputs == 1) {
        g_dbus_method_invocation_return_value(
            pending->invocation, g_variant_new("(u)", response)
        );
    } else {
        g_dbus_method_invocation_return_value(pending->invocation, NULL);
    }
    if (pending->request_registration != 0)
        g_dbus_connection_unregister_object(
            portal->guest, pending->request_registration
        );
    if (pending->host_handle != NULL)
        g_hash_table_remove(portal->pending_by_host, pending->host_handle);
    if (pending->guest_handle != NULL)
        g_hash_table_remove(portal->pending_by_guest, pending->guest_handle);
    g_variant_unref(results);
}

static GVariant *replace_session_result(
    Portal *portal,
    Pending *pending,
    GVariant *results
)
{
    GVariant *value;
    const char *host_path = NULL;
    GVariantDict dictionary;

    value = g_variant_lookup_value(results, "session_handle", NULL);
    if (value == NULL)
        return g_variant_ref(results);
    if (g_variant_is_of_type(value, G_VARIANT_TYPE_STRING)
        || g_variant_is_of_type(value, G_VARIANT_TYPE_OBJECT_PATH))
        host_path = g_variant_get_string(value, NULL);
    if (host_path == NULL || pending->guest_session == NULL) {
        g_variant_unref(value);
        return g_variant_ref(results);
    }
    register_session(
        portal, pending->guest_session, host_path
    );
    g_variant_dict_init(&dictionary, results);
    if (g_variant_is_of_type(value, G_VARIANT_TYPE_OBJECT_PATH))
        g_variant_dict_insert(
            &dictionary, "session_handle", "o", pending->guest_session
        );
    else
        g_variant_dict_insert(
            &dictionary, "session_handle", "s", pending->guest_session
        );
    g_variant_unref(value);
    return g_variant_dict_end(&dictionary);
}

static void host_call_free(HostCall *call)
{
    g_free(call);
}

static void host_call_done(GObject *source, GAsyncResult *result, gpointer data)
{
    HostCall *call = data;
    Portal *portal = call->portal;
    Pending *pending = call->pending;
    GUnixFDList *fd_list = NULL;
    GError *error = NULL;
    GVariant *reply;

    reply = g_dbus_connection_call_with_unix_fd_list_finish(
        G_DBUS_CONNECTION(source), &fd_list, result, &error
    );
    if (reply == NULL) {
        g_dbus_method_invocation_return_gerror(pending->invocation, error);
        g_clear_error(&error);
        if (pending->request_registration != 0)
            g_dbus_connection_unregister_object(
                portal->guest, pending->request_registration
            );
        if (pending->guest_handle != NULL)
            g_hash_table_remove(
                portal->pending_by_guest, pending->guest_handle
            );
        else
            pending_free(pending);
        host_call_free(call);
        return;
    }

    if (call->request || call->persistent_request) {
        const char *host_handle;
        g_variant_get(reply, "(&o)", &host_handle);
        pending->host_handle = g_strdup(host_handle);
        g_hash_table_insert(
            portal->pending_by_host, pending->host_handle, pending
        );
        if (pending->cancelled)
            close_host_object(
                portal, pending->host_handle,
                "org.freedesktop.portal.Request"
            );
        if (call->persistent_request) {
            pending->persistent = TRUE;
            g_dbus_method_invocation_return_value(
                pending->invocation, NULL
            );
            g_clear_object(&pending->invocation);
        }
    } else if (call->synthesize_response) {
        GVariant *results = empty_dict();
        finish_pending(portal, pending, 0, results);
    } else {
        if (call->create_session
            && g_variant_n_children(reply) == 1) {
            GVariant *child = g_variant_get_child_value(reply, 0);
            if (g_variant_is_of_type(child, G_VARIANT_TYPE_VARDICT)) {
                GVariant *mapped = replace_session_result(
                    portal, pending, child
                );
                g_variant_unref(child);
                g_variant_unref(reply);
                reply = g_variant_new_tuple(&mapped, 1);
                g_variant_ref_sink(reply);
                g_variant_unref(mapped);
            } else {
                g_variant_unref(child);
            }
        }
        if (fd_list != NULL)
            g_dbus_method_invocation_return_value_with_unix_fd_list(
                pending->invocation, reply, fd_list
            );
        else
            g_dbus_method_invocation_return_value(
                pending->invocation, reply
            );
        pending_free(pending);
    }
    g_clear_object(&fd_list);
    g_variant_unref(reply);
    host_call_free(call);
}

static GVariant *backend_property(
    GDBusConnection *connection,
    const char *sender,
    const char *object_path,
    const char *interface_name,
    const char *property_name,
    GError **error,
    gpointer user_data
)
{
    Portal *portal = user_data;
    const char *suffix;
    char *host_interface;
    GVariant *reply;
    GVariant *value;

    (void)connection;
    (void)object_path;
    if (!caller_is_portal(portal, sender, TRUE, error)) {
        if (error != NULL && *error == NULL) {
            g_set_error(
                error,
                G_DBUS_ERROR,
                G_DBUS_ERROR_ACCESS_DENIED,
                "Only the guest portal frontend may read this property"
            );
        }
        return NULL;
    }
    if (g_str_equal(
            interface_name, "org.freedesktop.impl.portal.AppChooser"
        )
        && g_str_equal(property_name, "version"))
        return g_variant_ref_sink(g_variant_new_uint32(2));
    suffix = interface_name + strlen("org.freedesktop.impl.portal.");
    host_interface = g_strconcat("org.freedesktop.portal.", suffix, NULL);
    reply = g_dbus_connection_call_sync(
        portal->host, HOST_NAME, DESKTOP_PATH,
        "org.freedesktop.DBus.Properties", "Get",
        g_variant_new("(ss)", host_interface, property_name),
        G_VARIANT_TYPE("(v)"), G_DBUS_CALL_FLAGS_NONE, 5000, NULL, error
    );
    g_free(host_interface);
    if (reply == NULL)
        return NULL;
    g_variant_get(reply, "(v)", &value);
    g_variant_unref(reply);
    if (g_str_equal(property_name, "version")
        && g_variant_is_of_type(value, G_VARIANT_TYPE_UINT32)) {
        guint host_version = g_variant_get_uint32(value);
        guint maximum = adapter_version(interface_name);

        g_variant_unref(value);
        value = g_variant_ref_sink(
            g_variant_new_uint32(MIN(host_version, maximum))
        );
    }
    return value;
}

static char *chooser_id(const char *desktop_id)
{
    gsize size;

    if (desktop_id == NULL)
        return NULL;
    size = strlen(desktop_id);
    if (size > strlen(".desktop")
        && g_str_has_suffix(desktop_id, ".desktop"))
        return g_strndup(desktop_id, size - strlen(".desktop"));
    return g_strdup(desktop_id);
}

static gboolean choice_is_available(GVariant *choices, const char *choice)
{
    GVariantIter iterator;
    const char *available;

    g_variant_iter_init(&iterator, choices);
    while (g_variant_iter_next(&iterator, "&s", &available)) {
        char *normalized = chooser_id(available);
        gboolean matches = g_str_equal(available, choice)
            || g_str_equal(normalized, choice);

        g_free(normalized);
        if (matches)
            return TRUE;
    }
    return FALSE;
}

static gboolean delegate_app_chooser(
    Portal *portal,
    const char *method_name,
    GVariant *parameters,
    GDBusMethodInvocation *invocation
)
{
    GVariant *reply;
    GError *error = NULL;
    const GVariantType *reply_type = g_str_equal(
        method_name, "ChooseApplication"
    )
        ? G_VARIANT_TYPE("(ua{sv})") : G_VARIANT_TYPE_UNIT;

    reply = g_dbus_connection_call_sync(
        portal->guest, "org.freedesktop.impl.portal.desktop.kde",
        DESKTOP_PATH, "org.freedesktop.impl.portal.AppChooser",
        method_name, parameters, reply_type, G_DBUS_CALL_FLAGS_NONE,
        -1, NULL, &error
    );
    if (reply == NULL) {
        g_clear_error(&error);
        return FALSE;
    }
    g_dbus_method_invocation_return_value(invocation, reply);
    g_variant_unref(reply);
    return TRUE;
}

static void app_chooser_call(
    Portal *portal,
    const char *sender,
    const char *method_name,
    GVariant *parameters,
    GDBusMethodInvocation *invocation
)
{
    GError *error = NULL;
    GVariant *choices;
    GVariant *options;
    GVariantBuilder results;
    GVariantIter iterator;
    GAppInfo *application = NULL;
    const char *content_type = NULL;
    const char *uri = NULL;
    const char *activation_token = NULL;
    const char *first = NULL;
    char *choice = NULL;

    if (!caller_is_portal(portal, sender, FALSE, &error)) {
        g_clear_error(&error);
        return_access_denied(invocation);
        return;
    }
    if (g_str_equal(method_name, "UpdateChoices")) {
        if (!delegate_app_chooser(
                portal, method_name, parameters, invocation
            ))
            g_dbus_method_invocation_return_value(invocation, NULL);
        return;
    }
    choices = g_variant_get_child_value(parameters, 3);
    options = g_variant_get_child_value(parameters, 4);
    g_variant_lookup(options, "content_type", "&s", &content_type);
    g_variant_lookup(options, "uri", "&s", &uri);
    g_variant_lookup(options, "activation_token", "&s", &activation_token);
    if (content_type != NULL)
        application = g_app_info_get_default_for_type(
            content_type, FALSE
        );
    if (application == NULL && uri != NULL) {
        GUri *parsed = g_uri_parse(
            uri, G_URI_FLAGS_PARSE_RELAXED, NULL
        );

        if (parsed != NULL && g_uri_get_scheme(parsed) != NULL)
            application = g_app_info_get_default_for_uri_scheme(
                g_uri_get_scheme(parsed)
            );
        if (parsed != NULL)
            g_uri_unref(parsed);
    }
    if (application != NULL)
        choice = chooser_id(g_app_info_get_id(application));
    if (choice != NULL && !choice_is_available(choices, choice))
        g_clear_pointer(&choice, g_free);
    if (choice == NULL
        && delegate_app_chooser(
            portal, method_name, parameters, invocation
        ))
        goto out;
    if (choice == NULL) {
        g_variant_iter_init(&iterator, choices);
        if (g_variant_iter_next(&iterator, "&s", &first))
            choice = chooser_id(first);
    }
    g_variant_builder_init(&results, G_VARIANT_TYPE_VARDICT);
    if (choice != NULL) {
        g_variant_builder_add(
            &results, "{sv}", "choice", g_variant_new_string(choice)
        );
        if (activation_token != NULL)
            g_variant_builder_add(
                &results, "{sv}", "activation_token",
                g_variant_new_string(activation_token)
            );
        g_dbus_method_invocation_return_value(
            invocation,
            g_variant_new("(u@a{sv})", 0U, g_variant_builder_end(&results))
        );
    } else {
        g_dbus_method_invocation_return_value(
            invocation,
            g_variant_new("(u@a{sv})", 2U, g_variant_builder_end(&results))
        );
    }
out:
    g_clear_object(&application);
    g_free(choice);
    g_variant_unref(options);
    g_variant_unref(choices);
}

static void backend_method_call(
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
    Portal *portal = user_data;
    GDBusInterfaceInfo *backend_interface;
    GDBusInterfaceInfo *public_interface;
    GDBusMethodInfo *backend_method;
    GDBusMethodInfo *public_method;
    const char *suffix;
    char *host_interface;
    Pending *pending;
    GVariant *host_parameters;
    GUnixFDList *fd_list;
    GUnixFDList *host_fd_list = NULL;
    GError *error = NULL;
    gboolean request;
    gboolean create_session;
    gboolean synthesize = FALSE;
    HostCall *call;
    GVariantType *reply_type;

    (void)connection;
    (void)object_path;
    if (g_str_equal(
            interface_name, "org.freedesktop.impl.portal.AppChooser"
        )) {
        app_chooser_call(
            portal, sender, method_name, parameters, invocation
        );
        return;
    }
    if (!caller_is_portal(portal, sender, FALSE, &error)) {
        g_clear_error(&error);
        return_access_denied(invocation);
        return;
    }
    backend_interface = find_interface(portal, interface_name);
    backend_method = find_method(backend_interface, method_name);
    suffix = interface_name + strlen("org.freedesktop.impl.portal.");
    host_interface = g_strconcat("org.freedesktop.portal.", suffix, NULL);
    public_interface = find_interface(portal, host_interface);
    public_method = public_interface == NULL
        ? NULL : find_method(public_interface, method_name);
    if (backend_method == NULL || public_method == NULL) {
        g_dbus_method_invocation_return_error(
            invocation, G_DBUS_ERROR, G_DBUS_ERROR_NOT_SUPPORTED,
            "The host portal does not expose this method"
        );
        g_free(host_interface);
        return;
    }

    pending = g_new0(Pending, 1);
    pending->secret_guest_fd = -1;
    pending->secret_host_fd = -1;
    pending->invocation = g_object_ref(invocation);
    pending->interface_name = g_strdup(interface_name);
    pending->method_name = g_strdup(method_name);
    request = method_returns_request(public_method);
    create_session = method_creates_session(suffix, method_name);
    host_parameters = build_host_parameters(
        portal, backend_method, parameters, request, create_session,
        pending, &error
    );
    if (host_parameters == NULL) {
        g_dbus_method_invocation_return_gerror(invocation, error);
        g_clear_error(&error);
        pending_free(pending);
        g_free(host_interface);
        return;
    }
    if (request && pending->guest_handle != NULL) {
        register_request(portal, pending);
        g_hash_table_insert(
            portal->pending_by_guest,
            g_strdup(pending->guest_handle),
            pending
        );
    }
    if (request
        && g_str_equal(suffix, "Inhibit")
        && g_str_equal(method_name, "Inhibit")) {
        synthesize = TRUE;
    }
    if (!request
        && g_str_equal(suffix, "InputCapture")
        && (g_str_equal(method_name, "Enable")
            || g_str_equal(method_name, "Disable")
            || g_str_equal(method_name, "Release")))
        synthesize = TRUE;

    fd_list = g_dbus_message_get_unix_fd_list(
        g_dbus_method_invocation_get_message(invocation)
    );
    if (g_str_equal(suffix, "Secret")
        && g_str_equal(method_name, "RetrieveSecret")) {
        GVariant *fd_value = g_variant_get_child_value(parameters, 2);
        gint fd_index = g_variant_get_handle(fd_value);
        gint count = fd_list == NULL ? 0 : g_unix_fd_list_get_length(fd_list);
        gint index;

        g_variant_unref(fd_value);
        if (fd_index < 0 || fd_index >= count) {
            g_set_error(
                &error, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS,
                "Secret output descriptor is unavailable"
            );
            goto secret_setup_failed;
        }
        pending->secret_guest_fd = g_unix_fd_list_get(
            fd_list, fd_index, &error
        );
        if (pending->secret_guest_fd < 0)
            goto secret_setup_failed;
        pending->secret_host_fd = create_secret_memfd();
        if (pending->secret_host_fd < 0) {
            g_set_error(
                &error, G_IO_ERROR, g_io_error_from_errno(errno),
                "Could not create the secret transport: %s",
                g_strerror(errno)
            );
            goto secret_setup_failed;
        }
        host_fd_list = g_unix_fd_list_new();
        for (index = 0; index < count; index++) {
            int descriptor = index == fd_index
                ? pending->secret_host_fd
                : g_unix_fd_list_get(fd_list, index, &error);
            int appended;

            if (descriptor < 0)
                goto secret_setup_failed;
            appended = g_unix_fd_list_append(
                host_fd_list, descriptor, &error
            );
            if (index != fd_index)
                close(descriptor);
            if (appended < 0)
                goto secret_setup_failed;
        }
        fd_list = host_fd_list;
    }
    call = g_new0(HostCall, 1);
    call->portal = portal;
    call->pending = pending;
    call->request = request && !synthesize;
    call->create_session = create_session;
    call->persistent_request = request && synthesize;
    call->synthesize_response = synthesize && !request;
    reply_type = method_output_type(public_method);
    g_dbus_connection_call_with_unix_fd_list(
        portal->host, HOST_NAME, DESKTOP_PATH,
        host_interface, method_name, host_parameters, reply_type,
        G_DBUS_CALL_FLAGS_NONE, -1, fd_list, NULL,
        host_call_done, call
    );
    g_clear_object(&host_fd_list);
    g_variant_type_free(reply_type);
    g_free(host_interface);
    return;

secret_setup_failed:
    g_dbus_method_invocation_return_gerror(invocation, error);
    g_clear_error(&error);
    g_clear_object(&host_fd_list);
    g_variant_unref(host_parameters);
    if (pending->request_registration != 0)
        g_dbus_connection_unregister_object(
            portal->guest, pending->request_registration
        );
    if (pending->guest_handle != NULL)
        g_hash_table_remove(
            portal->pending_by_guest, pending->guest_handle
        );
    else
        pending_free(pending);
    g_free(host_interface);
}

static const GDBusInterfaceVTable backend_vtable = {
    .method_call = backend_method_call,
    .get_property = backend_property,
};

static void host_signal(
    GDBusConnection *connection,
    const char *sender_name,
    const char *object_path,
    const char *interface_name,
    const char *signal_name,
    GVariant *parameters,
    gpointer user_data
)
{
    Portal *portal = user_data;
    GError *error = NULL;

    (void)connection;
    (void)sender_name;
    if (g_str_equal(interface_name, "org.freedesktop.portal.Request")
        && g_str_equal(signal_name, "Response")) {
        Pending *pending = g_hash_table_lookup(
            portal->pending_by_host, object_path
        );
        guint response;
        GVariant *results;
        if (pending == NULL)
            return;
        g_variant_get(parameters, "(u@a{sv})", &response, &results);
        finish_pending(portal, pending, response, results);
        return;
    }
    if (g_str_equal(interface_name, "org.freedesktop.portal.Session")
        && g_str_equal(signal_name, "Closed")) {
        char *guest_path = g_hash_table_lookup(
            portal->guest_by_host, object_path
        );
        Session *session;
        if (guest_path == NULL)
            return;
        session = g_hash_table_lookup(
            portal->sessions_by_guest, guest_path
        );
        g_dbus_connection_emit_signal(
            portal->guest, NULL, guest_path,
            "org.freedesktop.impl.portal.Session", "Closed",
            NULL, &error
        );
        if (error != NULL) {
            g_warning("Could not relay session closure: %s", error->message);
            g_clear_error(&error);
        }
        if (session != NULL && session->registration != 0)
            g_dbus_connection_unregister_object(
                portal->guest, session->registration
            );
        g_hash_table_remove(portal->sessions_by_guest, guest_path);
        g_hash_table_remove(portal->guest_by_host, object_path);
        return;
    }
    if (!g_str_has_prefix(
            interface_name, "org.freedesktop.portal."))
        return;

    const char *suffix = interface_name
        + strlen("org.freedesktop.portal.");
    char *backend_interface = g_strconcat(
        "org.freedesktop.impl.portal.", suffix, NULL
    );
    GVariant *relayed = NULL;
    if (g_str_equal(suffix, "Notification")
        && g_str_equal(signal_name, "ActionInvoked")) {
        const char *id;
        const char *action;
        GVariant *parameter;
        g_variant_get(parameters, "(&s&s@av)", &id, &action, &parameter);
        relayed = g_variant_new(
            "(sss@av)", portal->app_id, id, action, parameter
        );
    } else if (g_variant_n_children(parameters) > 0) {
        GVariant *first = g_variant_get_child_value(parameters, 0);
        if (g_variant_is_of_type(first, G_VARIANT_TYPE_OBJECT_PATH)) {
            const char *host_path = g_variant_get_string(first, NULL);
            const char *guest_path = g_hash_table_lookup(
                portal->guest_by_host, host_path
            );
            if (guest_path != NULL) {
                GVariantBuilder tuple;
                gsize count = g_variant_n_children(parameters);
                gsize index;
                g_variant_builder_init(&tuple, G_VARIANT_TYPE_TUPLE);
                g_variant_builder_add_value(
                    &tuple, g_variant_new_object_path(guest_path)
                );
                for (index = 1; index < count; index++)
                    g_variant_builder_add_value(
                        &tuple,
                        g_variant_get_child_value(parameters, index)
                    );
                relayed = g_variant_builder_end(&tuple);
            }
        }
        g_variant_unref(first);
    }
    if (relayed == NULL)
        relayed = g_variant_ref(parameters);
    g_variant_take_ref(relayed);
    g_dbus_connection_emit_signal(
        portal->guest, NULL, DESKTOP_PATH,
        backend_interface, signal_name, relayed, &error
    );
    if (error != NULL) {
        g_warning("Could not relay %s.%s: %s",
                  backend_interface, signal_name, error->message);
        g_clear_error(&error);
    }
    g_variant_unref(relayed);
    g_free(backend_interface);
}

static void native_host_signal(
    GDBusConnection *connection,
    const char *sender_name,
    const char *object_path,
    const char *interface_name,
    const char *signal_name,
    GVariant *parameters,
    gpointer user_data
)
{
    Portal *portal = user_data;
    GDBusInterfaceInfo *interface_info = find_interface(
        portal, interface_name
    );
    GError *error = NULL;

    (void)connection;
    (void)sender_name;
    (void)object_path;
    if (!interface_has_signal(interface_info, signal_name))
        return;
    if (g_str_equal(interface_name, NOTIFICATIONS_NAME)) {
        if (!portal->notifications_owned)
            return;
        g_dbus_connection_emit_signal(
            portal->guest, NULL, NOTIFICATIONS_PATH, interface_name,
            signal_name, parameters, &error
        );
    } else if (g_str_equal(interface_name, SCREEN_SAVER_NAME)) {
        const char *paths[] = {
            SCREEN_SAVER_PATH,
            SCREEN_SAVER_LEGACY_PATH,
        };
        guint index;

        if (!portal->screen_saver_owned)
            return;
        for (index = 0; index < G_N_ELEMENTS(paths); index++) {
            g_dbus_connection_emit_signal(
                portal->guest, NULL, paths[index], interface_name,
                signal_name, parameters, &error
            );
            if (error != NULL)
                break;
        }
    }
    if (error != NULL) {
        g_warning(
            "Could not relay %s.%s: %s",
            interface_name,
            signal_name,
            error->message
        );
        g_clear_error(&error);
    }
}

static void guest_name_owner_changed(
    GDBusConnection *connection,
    const char *sender_name,
    const char *object_path,
    const char *interface_name,
    const char *signal_name,
    GVariant *parameters,
    gpointer user_data
)
{
    Portal *portal = user_data;
    GHashTableIter iterator;
    gpointer value;
    const char *name;
    const char *old_owner;
    const char *new_owner;

    (void)connection;
    (void)sender_name;
    (void)object_path;
    (void)interface_name;
    (void)signal_name;
    g_variant_get(
        parameters, "(&s&s&s)", &name, &old_owner, &new_owner
    );
    if (*old_owner == '\0' || *new_owner != '\0')
        return;

    g_hash_table_iter_init(&iterator, portal->inhibitors);
    while (g_hash_table_iter_next(&iterator, NULL, &value)) {
        Inhibitor *inhibitor = value;
        if (!g_str_equal(inhibitor->sender, name))
            continue;
        release_host_inhibitor(
            portal, inhibitor->throttle, inhibitor->cookie
        );
        g_hash_table_iter_remove(&iterator);
    }
}

static GDBusNodeInfo *load_public_xml(const char *name)
{
    char *filename;
    char *contents = NULL;
    GDBusNodeInfo *node = NULL;
    GError *error = NULL;

    filename = g_strdup_printf(
        INTERFACE_DIRECTORY "/org.freedesktop.portal.%s.xml", name
    );

    if (!g_file_get_contents(filename, &contents, NULL, &error)) {
        g_clear_error(&error);
    } else {
        node = g_dbus_node_info_new_for_xml(contents, &error);
        if (node == NULL) {
            g_printerr("spaces-portal: %s\n", error->message);
            g_clear_error(&error);
        }
    }
    g_free(contents);
    g_free(filename);
    return node;
}

static GDBusNodeInfo *introspect_host(Portal *portal)
{
    GVariant *reply;
    const char *xml;
    GDBusNodeInfo *node;
    GError *error = NULL;

    reply = g_dbus_connection_call_sync(
        portal->host,
        HOST_NAME,
        DESKTOP_PATH,
        "org.freedesktop.DBus.Introspectable",
        "Introspect",
        NULL,
        G_VARIANT_TYPE("(s)"),
        G_DBUS_CALL_FLAGS_NONE,
        5000,
        NULL,
        &error
    );
    if (reply == NULL) {
        g_clear_error(&error);
        return NULL;
    }
    g_variant_get(reply, "(&s)", &xml);
    node = g_dbus_node_info_new_for_xml(xml, &error);
    g_variant_unref(reply);
    if (node == NULL) {
        g_clear_error(&error);
        return NULL;
    }
    return node;
}

typedef struct {
    GDBusMethodInvocation *invocation;
    guint pending;
    GError *error;
} FileManagerCall;

static void finish_file_manager_call(FileManagerCall *call)
{
    if (call->pending != 0)
        return;
    if (call->error != NULL)
        g_dbus_method_invocation_return_gerror(
            call->invocation, call->error
        );
    else
        g_dbus_method_invocation_return_value(call->invocation, NULL);
    g_clear_error(&call->error);
    g_object_unref(call->invocation);
    g_free(call);
}

static void file_manager_open_done(
    GObject *source,
    GAsyncResult *result,
    gpointer user_data
)
{
    FileManagerCall *call = user_data;
    GError *error = NULL;

    if (!g_subprocess_wait_check_finish(
            G_SUBPROCESS(source), result, &error
        )) {
        if (call->error == NULL)
            call->error = error;
        else
            g_clear_error(&error);
    }
    call->pending--;
    finish_file_manager_call(call);
}

static void file_manager_call(
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
    GVariant *uris;
    GVariantIter iterator;
    const char *uri;
    const char *mode;
    const char *startup_id;
    GError *error = NULL;
    FileManagerCall *call;
    GSubprocessLauncher *launcher;

    (void)connection;
    (void)sender;
    (void)object_path;
    (void)interface_name;
    (void)user_data;
    if (g_str_equal(method_name, "ShowItemProperties")) {
        g_dbus_method_invocation_return_error(
            invocation, G_DBUS_ERROR, G_DBUS_ERROR_NOT_SUPPORTED,
            "File property dialogs are not supported"
        );
        return;
    }
    mode = g_str_equal(method_name, "ShowFolders")
        ? "--folder" : "--directory";
    g_variant_get_child(parameters, 1, "&s", &startup_id);
    launcher = g_subprocess_launcher_new(
        G_SUBPROCESS_FLAGS_STDOUT_SILENCE
            | G_SUBPROCESS_FLAGS_STDERR_SILENCE
    );
    if (startup_id != NULL && *startup_id != '\0')
        g_subprocess_launcher_setenv(
            launcher, "XDG_ACTIVATION_TOKEN", startup_id, TRUE
        );
    call = g_new0(FileManagerCall, 1);
    call->invocation = g_object_ref(invocation);
    uris = g_variant_get_child_value(parameters, 0);
    g_variant_iter_init(&iterator, uris);
    while (g_variant_iter_next(&iterator, "&s", &uri)) {
        const char *arguments[] = {
            "/run/spaces-host/bin/spaces-open",
            mode,
            uri,
            NULL,
        };
        GSubprocess *process = g_subprocess_launcher_spawnv(
            launcher, arguments, &error
        );

        if (process == NULL) {
            if (call->error == NULL)
                call->error = error;
            else
                g_clear_error(&error);
            continue;
        }
        call->pending++;
        g_subprocess_wait_check_async(
            process, NULL, file_manager_open_done, call
        );
        g_object_unref(process);
    }
    g_object_unref(launcher);
    g_variant_unref(uris);
    finish_file_manager_call(call);
}

static const GDBusInterfaceVTable file_manager_vtable = {
    .method_call = file_manager_call,
};

static gboolean register_interfaces(Portal *portal)
{
    GDBusNodeInfo *backend;
    GDBusNodeInfo *chooser;
    GDBusNodeInfo *file_manager;
    GDBusNodeInfo *notifications;
    GDBusNodeInfo *screen_saver;
    GDBusNodeInfo *public;
    GError *error = NULL;
    guint index;
    guint count = 0;
    guint registration;

    backend = g_dbus_node_info_new_for_xml(portal_backend_xml, &error);
    if (backend == NULL) {
        g_printerr("spaces-portal: %s\n", error->message);
        g_clear_error(&error);
        return FALSE;
    }
    g_ptr_array_add(portal->node_infos, backend);
    chooser = g_dbus_node_info_new_for_xml(app_chooser_xml, &error);
    if (chooser == NULL) {
        g_printerr("spaces-portal: %s\n", error->message);
        g_clear_error(&error);
        return FALSE;
    }
    g_ptr_array_add(portal->node_infos, chooser);
    file_manager = g_dbus_node_info_new_for_xml(file_manager_xml, &error);
    if (file_manager == NULL) {
        g_printerr("spaces-portal: %s\n", error->message);
        g_clear_error(&error);
        return FALSE;
    }
    g_ptr_array_add(portal->node_infos, file_manager);
    notifications = g_dbus_node_info_new_for_xml(
        notifications_xml, &error
    );
    if (notifications == NULL) {
        g_printerr("spaces-portal: %s\n", error->message);
        g_clear_error(&error);
        return FALSE;
    }
    g_ptr_array_add(portal->node_infos, notifications);
    screen_saver = g_dbus_node_info_new_for_xml(
        screen_saver_xml, &error
    );
    if (screen_saver == NULL) {
        g_printerr("spaces-portal: %s\n", error->message);
        g_clear_error(&error);
        return FALSE;
    }
    g_ptr_array_add(portal->node_infos, screen_saver);

    public = introspect_host(portal);
    if (public != NULL) {
        g_ptr_array_add(portal->node_infos, public);
    } else {
        /*
         * Keep private-bus tests and older hosts diagnosable. Production
         * guests do not need these files: the restricted host portal is the
         * authoritative source of its public interface metadata.
         */
        for (index = 0; index < G_N_ELEMENTS(backend_names); index++) {
            public = load_public_xml(backend_names[index]);
            if (public != NULL)
                g_ptr_array_add(portal->node_infos, public);
        }
    }

    portal->request_info = g_dbus_node_info_lookup_interface(
        backend, "org.freedesktop.impl.portal.Request"
    );
    portal->session_info = g_dbus_node_info_lookup_interface(
        backend, "org.freedesktop.impl.portal.Session"
    );
    if (portal->request_info == NULL || portal->session_info == NULL)
        return FALSE;

    for (index = 0; index < G_N_ELEMENTS(backend_names); index++) {
        GDBusInterfaceInfo *backend_interface;
        GDBusInterfaceInfo *public_interface;
        char *backend_name;
        char *public_name;
        backend_name = g_strconcat(
            "org.freedesktop.impl.portal.", backend_names[index], NULL
        );
        public_name = g_strconcat(
            "org.freedesktop.portal.", backend_names[index], NULL
        );
        backend_interface = g_str_equal(
            backend_names[index], "AppChooser"
        )
            ? g_dbus_node_info_lookup_interface(chooser, backend_name)
            : g_dbus_node_info_lookup_interface(backend, backend_name);
        public_interface = find_interface(portal, public_name);
        g_free(backend_name);
        g_free(public_name);
        if (backend_interface == NULL
            || (public_interface == NULL
                && !g_str_equal(backend_names[index], "AppChooser")))
            continue;
        registration = g_dbus_connection_register_object(
            portal->guest, DESKTOP_PATH, backend_interface,
            &backend_vtable, portal, NULL, &error
        );
        if (registration == 0) {
            g_printerr("spaces-portal: %s\n", error->message);
            g_clear_error(&error);
            return FALSE;
        }
        g_array_append_val(portal->registrations, registration);
        count++;
    }
    registration = g_dbus_connection_register_object(
        portal->guest, FILE_MANAGER_PATH, file_manager->interfaces[0],
        &file_manager_vtable, portal, NULL, &error
    );
    if (registration == 0) {
        g_printerr("spaces-portal: %s\n", error->message);
        g_clear_error(&error);
        return FALSE;
    }
    g_array_append_val(portal->registrations, registration);
    registration = g_dbus_connection_register_object(
        portal->guest, NOTIFICATIONS_PATH, notifications->interfaces[0],
        &native_vtable, portal, NULL, &error
    );
    if (registration == 0) {
        g_printerr("spaces-portal: %s\n", error->message);
        g_clear_error(&error);
        return FALSE;
    }
    g_array_append_val(portal->registrations, registration);
    const char *screen_saver_paths[] = {
        SCREEN_SAVER_PATH,
        SCREEN_SAVER_LEGACY_PATH,
    };
    for (index = 0; index < G_N_ELEMENTS(screen_saver_paths); index++) {
        registration = g_dbus_connection_register_object(
            portal->guest,
            screen_saver_paths[index],
            screen_saver->interfaces[0],
            &native_vtable,
            portal,
            NULL,
            &error
        );
        if (registration == 0) {
            g_printerr("spaces-portal: %s\n", error->message);
            g_clear_error(&error);
            return FALSE;
        }
        g_array_append_val(portal->registrations, registration);
    }
    return count > 0;
}

static void name_lost(
    GDBusConnection *connection,
    const char *name,
    gpointer user_data
)
{
    Portal *portal = user_data;
    (void)connection;
    (void)name;
    g_main_loop_quit(portal->loop);
}

static void optional_name_lost(
    GDBusConnection *connection,
    const char *name,
    gpointer user_data
)
{
    Portal *portal = user_data;

    (void)connection;
    if (g_str_equal(name, NOTIFICATIONS_NAME))
        portal->notifications_owned = FALSE;
    else if (g_str_equal(name, SCREEN_SAVER_NAME))
        portal->screen_saver_owned = FALSE;
    g_warning(
        "Could not own optional desktop integration name %s; continuing",
        name
    );
}

static void optional_name_acquired(
    GDBusConnection *connection,
    const char *name,
    gpointer user_data
)
{
    Portal *portal = user_data;

    (void)connection;
    if (g_str_equal(name, NOTIFICATIONS_NAME))
        portal->notifications_owned = TRUE;
    else if (g_str_equal(name, SCREEN_SAVER_NAME))
        portal->screen_saver_owned = TRUE;
}

static void host_closed(
    GDBusConnection *connection,
    gboolean remote_peer_vanished,
    GError *error,
    gpointer user_data
)
{
    Portal *portal = user_data;
    (void)connection;
    (void)remote_peer_vanished;
    (void)error;
    portal->exit_status = 1;
    g_main_loop_quit(portal->loop);
}

static void host_name_appeared(
    GDBusConnection *connection,
    const char *name,
    const char *owner,
    gpointer user_data
)
{
    (void)connection;
    (void)name;
    (void)owner;
    (void)user_data;
}

static void screen_saver_appeared(
    GDBusConnection *connection,
    const char *name,
    const char *owner,
    gpointer user_data
)
{
    Portal *portal = user_data;

    (void)connection;
    (void)name;
    (void)owner;
    discover_screen_saver_path(portal);
}

static void screen_saver_vanished(
    GDBusConnection *connection,
    const char *name,
    gpointer user_data
)
{
    Portal *portal = user_data;

    (void)connection;
    (void)name;
    g_clear_pointer(&portal->screen_saver_path, g_free);
    g_hash_table_remove_all(portal->inhibitors);
}

static void host_name_vanished(
    GDBusConnection *connection,
    const char *name,
    gpointer user_data
)
{
    Portal *portal = user_data;
    GHashTableIter iterator;
    gpointer value;

    (void)connection;
    (void)name;
    g_hash_table_iter_init(&iterator, portal->pending_by_guest);
    while (g_hash_table_iter_next(&iterator, NULL, &value)) {
        Pending *pending = value;

        if (pending->invocation != NULL) {
            g_dbus_method_invocation_return_error(
                pending->invocation,
                G_DBUS_ERROR,
                G_DBUS_ERROR_NAME_HAS_NO_OWNER,
                "The host portal disappeared"
            );
            g_clear_object(&pending->invocation);
        }
    }
    portal->exit_status = 1;
    g_main_loop_quit(portal->loop);
}

int main(void)
{
    Portal portal = {0};
    GError *error = NULL;
    char *address;
    const char *test_address;
    const char *space_name;
    guint host_watcher;
    guint screen_saver_watcher;
    guint owner;
    guint file_manager_owner;
    guint notifications_owner;
    guint screen_saver_owner;
    guint notification_subscription;
    guint screen_saver_subscription;
    guint guest_name_subscription;

    signal(SIGPIPE, SIG_IGN);
    space_name = g_getenv("SPACES_NAME");
    if (space_name == NULL || *space_name == '\0') {
        g_printerr("spaces-portal: SPACES_NAME is unavailable\n");
        return 1;
    }
    portal.app_id = g_strconcat(
        "org.anatase.spaces.", space_name, NULL
    );
    test_address = g_getenv("SPACES_PORTAL_TEST_ADDRESS");
    if (test_address != NULL)
        address = g_strdup(test_address);
    else
        address = g_strdup_printf(
            "unix:path=/run/spaces/desktop/%u/portal/bus",
            (unsigned)getuid()
        );
    portal.guest = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &error);
    if (portal.guest == NULL) {
        g_printerr("spaces-portal: %s\n", error->message);
        g_clear_error(&error);
        g_free(address);
        g_free(portal.app_id);
        return 1;
    }
    portal.host = g_dbus_connection_new_for_address_sync(
        address,
        G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT
            | G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
        NULL, NULL, &error
    );
    g_free(address);
    if (portal.host == NULL) {
        g_printerr("spaces-portal: %s\n", error->message);
        g_clear_error(&error);
        g_object_unref(portal.guest);
        g_free(portal.app_id);
        return 1;
    }
    portal.loop = g_main_loop_new(NULL, FALSE);
    portal.node_infos = g_ptr_array_new_with_free_func(
        (GDestroyNotify)g_dbus_node_info_unref
    );
    portal.registrations = g_array_new(FALSE, FALSE, sizeof(guint));
    portal.pending_by_guest = g_hash_table_new_full(
        g_str_hash, g_str_equal, g_free, pending_free
    );
    portal.pending_by_host = g_hash_table_new(g_str_hash, g_str_equal);
    portal.sessions_by_guest = g_hash_table_new_full(
        g_str_hash, g_str_equal, g_free, session_free
    );
    portal.guest_by_host = g_hash_table_new_full(
        g_str_hash, g_str_equal, g_free, g_free
    );
    portal.inhibitors = g_hash_table_new_full(
        g_str_hash, g_str_equal, g_free, inhibitor_free
    );
    if (!register_interfaces(&portal))
        return 1;
    g_dbus_connection_signal_subscribe(
        portal.host, HOST_NAME, NULL, NULL, NULL, NULL,
        G_DBUS_SIGNAL_FLAGS_NONE, host_signal, &portal, NULL
    );
    notification_subscription = g_dbus_connection_signal_subscribe(
        portal.host,
        NOTIFICATIONS_NAME,
        NOTIFICATIONS_NAME,
        NULL,
        NOTIFICATIONS_PATH,
        NULL,
        G_DBUS_SIGNAL_FLAGS_NONE,
        native_host_signal,
        &portal,
        NULL
    );
    screen_saver_subscription = g_dbus_connection_signal_subscribe(
        portal.host,
        SCREEN_SAVER_NAME,
        SCREEN_SAVER_NAME,
        NULL,
        NULL,
        NULL,
        G_DBUS_SIGNAL_FLAGS_NONE,
        native_host_signal,
        &portal,
        NULL
    );
    guest_name_subscription = g_dbus_connection_signal_subscribe(
        portal.guest,
        DBUS_NAME,
        DBUS_NAME,
        "NameOwnerChanged",
        DBUS_PATH,
        NULL,
        G_DBUS_SIGNAL_FLAGS_NONE,
        guest_name_owner_changed,
        &portal,
        NULL
    );
    g_signal_connect(portal.host, "closed", G_CALLBACK(host_closed), &portal);
    host_watcher = g_bus_watch_name_on_connection(
        portal.host,
        HOST_NAME,
        G_BUS_NAME_WATCHER_FLAGS_AUTO_START,
        host_name_appeared,
        host_name_vanished,
        &portal,
        NULL
    );
    screen_saver_watcher = g_bus_watch_name_on_connection(
        portal.host,
        SCREEN_SAVER_NAME,
        G_BUS_NAME_WATCHER_FLAGS_AUTO_START,
        screen_saver_appeared,
        screen_saver_vanished,
        &portal,
        NULL
    );
    owner = g_bus_own_name_on_connection(
        portal.guest, BACKEND_NAME,
        G_BUS_NAME_OWNER_FLAGS_ALLOW_REPLACEMENT
            | G_BUS_NAME_OWNER_FLAGS_REPLACE,
        NULL, name_lost, &portal, NULL
    );
    file_manager_owner = g_bus_own_name_on_connection(
        portal.guest, FILE_MANAGER_NAME,
        G_BUS_NAME_OWNER_FLAGS_ALLOW_REPLACEMENT
            | G_BUS_NAME_OWNER_FLAGS_REPLACE,
        NULL, name_lost, &portal, NULL
    );
    notifications_owner = g_bus_own_name_on_connection(
        portal.guest,
        NOTIFICATIONS_NAME,
        G_BUS_NAME_OWNER_FLAGS_NONE,
        optional_name_acquired,
        optional_name_lost,
        &portal,
        NULL
    );
    screen_saver_owner = g_bus_own_name_on_connection(
        portal.guest,
        SCREEN_SAVER_NAME,
        G_BUS_NAME_OWNER_FLAGS_NONE,
        optional_name_acquired,
        optional_name_lost,
        &portal,
        NULL
    );
    g_main_loop_run(portal.loop);
    g_bus_unown_name(screen_saver_owner);
    g_bus_unown_name(notifications_owner);
    g_bus_unown_name(file_manager_owner);
    g_bus_unown_name(owner);
    g_bus_unwatch_name(screen_saver_watcher);
    g_bus_unwatch_name(host_watcher);
    g_dbus_connection_signal_unsubscribe(
        portal.guest, guest_name_subscription
    );
    g_dbus_connection_signal_unsubscribe(
        portal.host, screen_saver_subscription
    );
    g_dbus_connection_signal_unsubscribe(
        portal.host, notification_subscription
    );
    g_main_loop_unref(portal.loop);
    g_hash_table_unref(portal.inhibitors);
    g_hash_table_unref(portal.guest_by_host);
    g_hash_table_unref(portal.sessions_by_guest);
    g_hash_table_unref(portal.pending_by_host);
    g_hash_table_unref(portal.pending_by_guest);
    g_array_unref(portal.registrations);
    g_ptr_array_unref(portal.node_infos);
    g_object_unref(portal.host);
    g_object_unref(portal.guest);
    g_free(portal.bootstrap_portal_owner);
    g_free(portal.screen_saver_path);
    g_free(portal.app_id);
    return portal.exit_status;
}
