#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <signal.h>
#include <stdint.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define PORTAL_NAME "org.freedesktop.portal.Desktop"
#define PORTAL_PATH "/org/freedesktop/portal/desktop"
#define REQUEST_INTERFACE "org.freedesktop.portal.Request"
#define SESSION_INTERFACE "org.freedesktop.portal.Session"
#define FILE_MANAGER_NAME "org.freedesktop.FileManager1"
#define FILE_MANAGER_PATH "/org/freedesktop/FileManager1"
#define NOTIFICATIONS_NAME "org.freedesktop.Notifications"
#define NOTIFICATIONS_PATH "/org/freedesktop/Notifications"
#define SCREEN_SAVER_NAME "org.freedesktop.ScreenSaver"
#define SCREEN_SAVER_PATH "/org/freedesktop/ScreenSaver"
#define SCREEN_SAVER_LEGACY_PATH "/ScreenSaver"
#define POWER_NAME "org.freedesktop.PowerManagement"
#define POWER_PATH "/org/freedesktop/PowerManagement"
#define STATUS_WATCHER_NAME "org.kde.StatusNotifierWatcher"
#define STATUS_WATCHER_PATH "/StatusNotifierWatcher"
#define STATUS_WATCHER_INTERFACE "org.kde.StatusNotifierWatcher"
#define STATUS_ITEM_INTERFACE "org.kde.StatusNotifierItem"
#define DBUSMENU_INTERFACE "com.canonical.dbusmenu"
#define MPRIS_PREFIX "org.mpris.MediaPlayer2."
#define MPRIS_PATH "/org/mpris/MediaPlayer2"
#define RESTORE_DATA_VENDOR "Spaces"
#define RESTORE_DATA_VERSION 1
#define SECRET_SIZE 64
#define DBUS_NAME "org.freedesktop.DBus"
#define DBUS_PATH "/org/freedesktop/DBus"
#define DESKTOP_INTERFACE_PREFIX "org.freedesktop.portal."
#define INTEGRATION_PATH "/org/anatase/Spaces/Integration"
#define INTEGRATION_INTERFACE "org.anatase.Spaces.Integration1"
#ifndef PIDFD_THREAD
#define PIDFD_THREAD O_EXCL
#endif

/* Keep this list deliberately explicit.  Host introspection is metadata, not
 * authority to grow the guest API when a host adds a new portal. */
static const char *public_interfaces[] = {
    "Account", "Access", "Background", "Camera", "Clipboard",
    "DynamicLauncher", "Email", "FileChooser", "GameMode",
    "GlobalShortcuts", "Inhibit", "InputCapture", "Location",
    "NetworkMonitor", "Notification", "OpenURI", "PowerProfileMonitor",
    "Print", "ProxyResolver", "Realtime", "RemoteDesktop", "ScreenCast",
    "Screenshot", "Secret", "Settings", "Usb", "Wallpaper",
};

static const char lifecycle_xml[] =
    "<node>"
    " <interface name='" REQUEST_INTERFACE "'><method name='Close'/></interface>"
    " <interface name='" SESSION_INTERFACE "'><method name='Close'/>"
    "  <signal name='Closed'><arg type='a{sv}'/></signal>"
    " </interface>"
    "</node>";

static const char file_manager_xml[] =
    "<node><interface name='" FILE_MANAGER_NAME "'>"
    " <method name='ShowItems'><arg type='as' direction='in'/><arg type='s' direction='in'/></method>"
    " <method name='ShowFolders'><arg type='as' direction='in'/><arg type='s' direction='in'/></method>"
    " <method name='ShowItemProperties'><arg type='as' direction='in'/><arg type='s' direction='in'/></method>"
    "</interface></node>";

static const char notifications_xml[] =
    "<node><interface name='" NOTIFICATIONS_NAME "'>"
    " <method name='GetCapabilities'><arg type='as' direction='out'/></method>"
    " <method name='Notify'><arg type='s' direction='in'/><arg type='u' direction='in'/>"
    "  <arg type='s' direction='in'/><arg type='s' direction='in'/><arg type='s' direction='in'/>"
    "  <arg type='as' direction='in'/><arg type='a{sv}' direction='in'/><arg type='i' direction='in'/>"
    "  <arg type='u' direction='out'/></method>"
    " <method name='CloseNotification'><arg type='u' direction='in'/></method>"
    " <method name='GetServerInformation'><arg type='s' direction='out'/><arg type='s' direction='out'/>"
    "  <arg type='s' direction='out'/><arg type='s' direction='out'/></method>"
    " <signal name='NotificationClosed'><arg type='u'/><arg type='u'/></signal>"
    " <signal name='ActionInvoked'><arg type='u'/><arg type='s'/></signal>"
    " <signal name='ActivationToken'><arg type='u'/><arg type='s'/></signal>"
    "</interface></node>";

static const char screen_saver_xml[] =
    "<node><interface name='" SCREEN_SAVER_NAME "'>"
    " <method name='Lock'/><method name='SimulateUserActivity'/>"
    " <method name='GetActive'><arg type='b' direction='out'/></method>"
    " <method name='GetActiveTime'><arg type='u' direction='out'/></method>"
    " <method name='GetSessionIdleTime'><arg type='u' direction='out'/></method>"
    " <method name='SetActive'><arg type='b' direction='in'/><arg type='b' direction='out'/></method>"
    " <method name='Inhibit'><arg type='s' direction='in'/><arg type='s' direction='in'/><arg type='u' direction='out'/></method>"
    " <method name='UnInhibit'><arg type='u' direction='in'/></method>"
    " <method name='Throttle'><arg type='s' direction='in'/><arg type='s' direction='in'/><arg type='u' direction='out'/></method>"
    " <method name='UnThrottle'><arg type='u' direction='in'/></method>"
    " <signal name='ActiveChanged'><arg type='b'/></signal>"
    "</interface></node>";

static const char power_xml[] =
    "<node><interface name='" POWER_NAME "'>"
    " <method name='CanHibernate'><arg type='b' direction='out'/></method>"
    " <method name='CanHybridSuspend'><arg type='b' direction='out'/></method>"
    " <method name='CanSuspend'><arg type='b' direction='out'/></method>"
    " <method name='CanSuspendThenHibernate'><arg type='b' direction='out'/></method>"
    " <method name='GetPowerSaveStatus'><arg type='b' direction='out'/></method>"
    " <signal name='CanHibernateChanged'><arg type='b'/></signal>"
    " <signal name='CanHybridSuspendChanged'><arg type='b'/></signal>"
    " <signal name='CanSuspendChanged'><arg type='b'/></signal>"
    " <signal name='CanSuspendThenHibernateChanged'><arg type='b'/></signal>"
    " <signal name='PowerSaveStatusChanged'><arg type='b'/></signal>"
    "</interface></node>";

static const char status_watcher_xml[] =
    "<node><interface name='" STATUS_WATCHER_INTERFACE "'>"
    " <method name='RegisterStatusNotifierItem'><arg type='s' direction='in'/></method>"
    " <method name='RegisterStatusNotifierHost'><arg type='s' direction='in'/></method>"
    " <property name='RegisteredStatusNotifierItems' type='as' access='read'/>"
    " <property name='IsStatusNotifierHostRegistered' type='b' access='read'/>"
    " <property name='ProtocolVersion' type='i' access='read'/>"
    " <signal name='StatusNotifierItemRegistered'><arg type='s'/></signal>"
    " <signal name='StatusNotifierItemUnregistered'><arg type='s'/></signal>"
    " <signal name='StatusNotifierHostRegistered'/><signal name='StatusNotifierHostUnregistered'/>"
    "</interface></node>";

/* Some MPRIS players implement the standard root and player APIs while
 * returning an empty Introspect response.  Use the standardized ABI only as
 * a publication schema; calls and properties still go to the guest player. */
static const char mpris_fallback_xml[] =
    "<node>"
    " <interface name='org.mpris.MediaPlayer2'>"
    "  <method name='Raise'/><method name='Quit'/>"
    "  <property name='CanQuit' type='b' access='read'/>"
    "  <property name='CanRaise' type='b' access='read'/>"
    "  <property name='HasTrackList' type='b' access='read'/>"
    "  <property name='Identity' type='s' access='read'/>"
    "  <property name='DesktopEntry' type='s' access='read'/>"
    "  <property name='SupportedUriSchemes' type='as' access='read'/>"
    "  <property name='SupportedMimeTypes' type='as' access='read'/>"
    " </interface>"
    " <interface name='org.mpris.MediaPlayer2.Player'>"
    "  <method name='Next'/><method name='Previous'/><method name='Pause'/>"
    "  <method name='PlayPause'/><method name='Stop'/><method name='Play'/>"
    "  <method name='Seek'><arg type='x' direction='in'/></method>"
    "  <method name='SetPosition'><arg type='o' direction='in'/>"
    "   <arg type='x' direction='in'/></method>"
    "  <method name='OpenUri'><arg type='s' direction='in'/></method>"
    "  <property name='PlaybackStatus' type='s' access='read'/>"
    "  <property name='LoopStatus' type='s' access='readwrite'/>"
    "  <property name='Rate' type='d' access='readwrite'/>"
    "  <property name='Shuffle' type='b' access='readwrite'/>"
    "  <property name='Metadata' type='a{sv}' access='read'/>"
    "  <property name='Volume' type='d' access='readwrite'/>"
    "  <property name='Position' type='x' access='read'/>"
    "  <property name='MinimumRate' type='d' access='read'/>"
    "  <property name='MaximumRate' type='d' access='read'/>"
    "  <property name='CanGoNext' type='b' access='read'/>"
    "  <property name='CanGoPrevious' type='b' access='read'/>"
    "  <property name='CanPlay' type='b' access='read'/>"
    "  <property name='CanPause' type='b' access='read'/>"
    "  <property name='CanSeek' type='b' access='read'/>"
    "  <property name='CanControl' type='b' access='read'/>"
    "  <signal name='Seeked'><arg type='x'/></signal>"
    " </interface>"
    "</node>";

#if defined(__x86_64__) || defined(__aarch64__)
typedef int (*main_function)(int, char **, char **);
extern int spaces_old_libc_start_main(main_function, int, char **,
    void (*)(void), void (*)(void), void (*)(void), void *);
#if defined(__x86_64__)
__asm__(".symver spaces_old_libc_start_main,__libc_start_main@GLIBC_2.2.5");
#else
__asm__(".symver spaces_old_libc_start_main,__libc_start_main@GLIBC_2.17");
#endif
int __wrap___libc_start_main(main_function function, int argc, char **argv,
    void (*init)(void), void (*fini)(void), void (*rtld_fini)(void), void *stack_end)
{
    return spaces_old_libc_start_main(function, argc, argv, init, fini,
        rtld_fini, stack_end);
}
#endif

typedef struct Portal Portal;
typedef struct Mirror Mirror;
typedef struct {
    char *owner;
    guint cookie;
    gboolean throttle;
} Inhibitor;

typedef struct {
    Portal *portal;
    char *owner;
    char *guest_path;
    char *host_path;
    char *guest_session_path;
    guint registration;
    gboolean local;
    char *local_backend;
    char *flatpak_app_id;
    gboolean chooser_writable;
    gboolean persistent;
    gboolean cancelled;
} Request;

typedef struct {
    Portal *portal;
    char *owner;
    char *guest_path;
    char *host_path;
    guint registration;
} Session;

typedef struct {
    Portal *portal;
    GDBusMethodInvocation *invocation;
    GDBusMethodInfo *method;
    char *interface_name;
    char *method_name;
    char *owner;
    char *guest_request_path;
    char *guest_session_path;
    char *finish_guest_request;
    gboolean returns_request;
    gboolean returns_session;
} ForwardCall;

struct Portal {
    GMainLoop *loop;
    GDBusConnection *guest;
    GDBusConnection *host;
    GDBusNodeInfo *host_node;
    GDBusNodeInfo *lifecycle_node;
    GPtrArray *owned_nodes;
    GArray *registrations;
    GHashTable *requests_guest;
    GHashTable *requests_host;
    GHashTable *sessions_guest;
    GHashTable *sessions_host;
    GHashTable *mirrors;
    GHashTable *inhibitors;
    char *space_name;
    char *app_id;
    char *file_chooser_backend;
    char *screen_saver_host_path;
    gboolean notifications_owned;
    gboolean screen_saver_owned;
    gboolean power_owned;
    gboolean status_watcher_owned;
    guint mirror_serial;
    int exit_status;
};

struct Mirror {
    Portal *portal;
    char *key;
    char *guest_name;
    char *guest_owner;
    char *guest_path;
    char *host_name;
    gboolean status_item;
    guint host_owner;
    guint signal_subscription;
    GPtrArray *nodes;
    GPtrArray *paths;
    GPtrArray *artwork_uris;
    GArray *registrations;
};

static void bridge_done(GObject *source, GAsyncResult *result,
    gpointer user_data);

static GVariant *mirror_get_property(GDBusConnection *connection,
    const char *sender, const char *object_path, const char *interface_name,
    const char *property_name, GError **error, gpointer user_data);

static char *new_token(void)
{
    char *token = g_uuid_string_random();
    char *cursor;
    for (cursor = token; *cursor != '\0'; cursor++)
        if (*cursor == '-') *cursor = '_';
    return token;
}

gboolean portal_derive_secret(const guint8 *host_secret,
    gsize host_secret_size, const char *app_id, guint8 output[SECRET_SIZE])
{
    static const guint8 domain[] = "org.anatase.spaces.portal-secret.v1";
    GHmac *hmac;
    guint8 length[4];
    gsize app_id_size;
    gsize output_size = SECRET_SIZE;
    if (host_secret == NULL || host_secret_size == 0 || app_id == NULL
        || output == NULL) return FALSE;
    app_id_size = strlen(app_id);
    if (app_id_size > UINT32_MAX) return FALSE;
    length[0] = (guint8)(app_id_size >> 24);
    length[1] = (guint8)(app_id_size >> 16);
    length[2] = (guint8)(app_id_size >> 8);
    length[3] = (guint8)app_id_size;
    hmac = g_hmac_new(G_CHECKSUM_SHA512, host_secret, host_secret_size);
    if (hmac == NULL) return FALSE;
    g_hmac_update(hmac, domain, sizeof(domain));
    g_hmac_update(hmac, length, sizeof(length));
    g_hmac_update(hmac, (const guint8 *)app_id, app_id_size);
    g_hmac_get_digest(hmac, output, &output_size);
    g_hmac_unref(hmac);
    return output_size == SECRET_SIZE;
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
    g_variant_get(restore_data, "(&su@v)", &vendor, &version, &wrapped);
    private_data = g_variant_get_variant(wrapped);
    if (g_str_equal(vendor, RESTORE_DATA_VENDOR)
        && version == RESTORE_DATA_VERSION
        && g_variant_is_of_type(private_data, G_VARIANT_TYPE_STRING))
        token = g_variant_dup_string(private_data, NULL);
    g_variant_unref(private_data); g_variant_unref(wrapped);
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
    g_variant_dict_remove(&dictionary, "restore_data");
    g_variant_dict_remove(&dictionary, "restore_token");
    if (restore_token != NULL)
        g_variant_dict_insert(&dictionary, "restore_token", "s", restore_token);
    g_free(restore_token);
    return g_variant_dict_end(&dictionary);
}

GVariant *portal_restore_results_to_backend(GVariant *results)
{
    GVariantDict dictionary;
    const char *restore_token = NULL;
    g_variant_lookup(results, "restore_token", "&s", &restore_token);
    g_variant_dict_init(&dictionary, results);
    g_variant_dict_remove(&dictionary, "restore_token");
    if (restore_token != NULL) {
        GVariant *private_data = g_variant_new_variant(
            g_variant_new_string(restore_token));
        GVariant *restore_data = g_variant_new("(su@v)",
            RESTORE_DATA_VENDOR, RESTORE_DATA_VERSION, private_data);
        g_variant_dict_insert_value(&dictionary, "restore_data", restore_data);
    }
    return g_variant_dict_end(&dictionary);
}

static void inhibitor_free(gpointer data)
{
    Inhibitor *inhibitor = data;
    g_free(inhibitor->owner);
    g_free(inhibitor);
}

static char *inhibitor_key(gboolean throttle, guint cookie)
{
    return g_strdup_printf("%c:%u", throttle ? 't' : 'i', cookie);
}

static void release_inhibitor(Portal *portal, Inhibitor *inhibitor)
{
    if (portal->screen_saver_host_path == NULL) return;
    g_dbus_connection_call(portal->host, SCREEN_SAVER_NAME,
        portal->screen_saver_host_path, SCREEN_SAVER_NAME,
        inhibitor->throttle ? "UnThrottle" : "UnInhibit",
        g_variant_new("(u)", inhibitor->cookie), NULL,
        G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
}

static char *sender_component(const char *sender)
{
    char *result = g_strdup(sender != NULL && sender[0] == ':' ? sender + 1 : sender);
    char *cursor;
    for (cursor = result; *cursor != '\0'; cursor++)
        if (!g_ascii_isalnum(*cursor) && *cursor != '_') *cursor = '_';
    return result;
}

static gboolean valid_token(const char *token)
{
    const unsigned char *cursor = (const unsigned char *)token;
    if (token == NULL || *token == '\0') return FALSE;
    for (; *cursor != '\0'; cursor++)
        if (!g_ascii_isalnum(*cursor) && *cursor != '_') return FALSE;
    return TRUE;
}

static char *public_object_path(const char *kind, const char *sender, const char *token)
{
    char *component = sender_component(sender);
    char *result = g_strdup_printf(PORTAL_PATH "/%s/%s/%s", kind, component, token);
    g_free(component);
    return result;
}

static gboolean interface_allowed(const char *name)
{
    guint index;
    if (!g_str_has_prefix(name, DESKTOP_INTERFACE_PREFIX)) return FALSE;
    name += strlen(DESKTOP_INTERFACE_PREFIX);
    for (index = 0; index < G_N_ELEMENTS(public_interfaces); index++)
        if (g_str_equal(name, public_interfaces[index])) return TRUE;
    return FALSE;
}

static GDBusMethodInfo *find_method(GDBusInterfaceInfo *info, const char *name)
{
    GDBusMethodInfo **cursor;
    for (cursor = info->methods; cursor != NULL && *cursor != NULL; cursor++)
        if (g_str_equal((*cursor)->name, name)) return *cursor;
    return NULL;
}

static GDBusInterfaceInfo *find_registered_interface(Portal *portal,
    const char *name)
{
    guint index;
    GDBusInterfaceInfo *info;
    if (portal->host_node != NULL) {
        info = g_dbus_node_info_lookup_interface(portal->host_node, name);
        if (info != NULL) return info;
    }
    for (index = 0; index < portal->owned_nodes->len; index++) {
        GDBusNodeInfo *node = g_ptr_array_index(portal->owned_nodes, index);
        info = g_dbus_node_info_lookup_interface(node, name);
        if (info != NULL) return info;
    }
    return NULL;
}

static GVariantType *method_output_type(GDBusMethodInfo *method)
{
    GString *signature = g_string_new("(");
    GDBusArgInfo **argument;
    for (argument = method->out_args; argument != NULL && *argument != NULL; argument++)
        g_string_append(signature, (*argument)->signature);
    g_string_append_c(signature, ')');
    GVariantType *type = g_variant_type_new(signature->str);
    g_string_free(signature, TRUE);
    return type;
}

static gboolean method_returns_request(GDBusMethodInfo *method)
{
    return method->out_args != NULL && method->out_args[0] != NULL
        && method->out_args[1] == NULL
        && g_str_equal(method->out_args[0]->signature, "o")
        && g_strcmp0(method->out_args[0]->name, "session_handle") != 0;
}

static gboolean method_returns_session(GDBusMethodInfo *method)
{
    return method->out_args != NULL && method->out_args[0] != NULL
        && method->out_args[1] == NULL
        && g_str_equal(method->out_args[0]->signature, "o")
        && g_strcmp0(method->out_args[0]->name, "session_handle") == 0;
}

static void request_free(gpointer data)
{
    Request *request = data;
    if (request->registration != 0)
        g_dbus_connection_unregister_object(request->portal->guest, request->registration);
    g_free(request->owner);
    g_free(request->guest_path);
    g_free(request->host_path);
    g_free(request->guest_session_path);
    g_free(request->local_backend);
    g_free(request->flatpak_app_id);
    g_free(request);
}

static void session_free(gpointer data)
{
    Session *session = data;
    if (session->registration != 0)
        g_dbus_connection_unregister_object(session->portal->guest, session->registration);
    g_free(session->owner);
    g_free(session->guest_path);
    g_free(session->host_path);
    g_free(session);
}

static void forward_call_free(ForwardCall *call)
{
    g_clear_object(&call->invocation);
    g_free(call->interface_name);
    g_free(call->method_name);
    g_free(call->owner);
    g_free(call->guest_request_path);
    g_free(call->guest_session_path);
    g_free(call->finish_guest_request);
    g_free(call);
}

static GVariant *translate_object_paths(Portal *portal, GVariant *value, gboolean to_host)
{
    const GVariantType *type = g_variant_get_type(value);
    if (g_variant_is_of_type(value, G_VARIANT_TYPE_OBJECT_PATH)) {
        const char *path = g_variant_get_string(value, NULL);
        gpointer mapped = NULL;
        if (to_host) {
            Request *request = g_hash_table_lookup(portal->requests_guest, path);
            Session *session = g_hash_table_lookup(portal->sessions_guest, path);
            mapped = request != NULL ? request->host_path : session != NULL ? session->host_path : NULL;
        } else {
            Request *request = g_hash_table_lookup(portal->requests_host, path);
            Session *session = g_hash_table_lookup(portal->sessions_host, path);
            mapped = request != NULL ? request->guest_path : session != NULL ? session->guest_path : NULL;
        }
        return g_variant_ref_sink(g_variant_new_object_path(mapped == NULL ? path : mapped));
    }
    if (!g_variant_is_container(value)) return g_variant_ref(value);
    if (g_variant_is_of_type(value, G_VARIANT_TYPE_VARIANT)) {
        GVariant *child = g_variant_get_variant(value);
        GVariant *mapped = translate_object_paths(portal, child, to_host);
        GVariant *result = g_variant_ref_sink(g_variant_new_variant(mapped));
        g_variant_unref(mapped);
        g_variant_unref(child);
        return result;
    }
    GVariantBuilder builder;
    gsize index, count = g_variant_n_children(value);
    g_variant_builder_init(&builder, type);
    for (index = 0; index < count; index++) {
        GVariant *child = g_variant_get_child_value(value, index);
        GVariant *mapped = translate_object_paths(portal, child, to_host);
        g_variant_builder_add_value(&builder, mapped);
        g_variant_unref(child);
    }
    return g_variant_ref_sink(g_variant_builder_end(&builder));
}

static GVariant *rewrite_options(ForwardCall *call, GVariant *options)
{
    GVariantDict dict;
    const char *guest_token = NULL;
    const char *guest_session_token = NULL;
    char *fallback = NULL;
    char *fallback_session = NULL;
    char *host_token = NULL;
    char *host_session_token = NULL;

    g_variant_lookup(options, "handle_token", "&s", &guest_token);
    if (!valid_token(guest_token)) guest_token = fallback = new_token();
    if (call->returns_request)
        call->guest_request_path = public_object_path("request", call->owner, guest_token);
    g_variant_lookup(options, "session_handle_token", "&s", &guest_session_token);
    if (!valid_token(guest_session_token)
        && (call->returns_session
            || g_str_equal(call->method_name, "CreateSession")))
        guest_session_token = fallback_session = new_token();
    if (valid_token(guest_session_token))
        call->guest_session_path = public_object_path("session", call->owner, guest_session_token);

    g_variant_dict_init(&dict, options);
    if (call->returns_request) {
        host_token = new_token();
        g_variant_dict_insert(&dict, "handle_token", "s", host_token);
    }
    if (call->guest_session_path != NULL) {
        host_session_token = new_token();
        g_variant_dict_insert(&dict, "session_handle_token", "s", host_session_token);
    }
    /* Host autostart entries must enter the Space instead of trying to run a
     * guest command on the host. */
    if (g_str_equal(call->interface_name, DESKTOP_INTERFACE_PREFIX "Background")
        && g_str_equal(call->method_name, "RequestBackground")) {
        GVariant *commandline = g_variant_lookup_value(options, "commandline", G_VARIANT_TYPE_STRING_ARRAY);
        if (commandline != NULL) {
            GVariantBuilder command;
            GVariantIter iterator;
            const char *item;
            g_variant_builder_init(&command, G_VARIANT_TYPE_STRING_ARRAY);
            g_variant_builder_add(&command, "s", "/usr/bin/spaces");
            g_variant_builder_add(&command, "s", "enter");
            g_variant_builder_add(&command, "s", "--graphical");
            g_variant_builder_add(&command, "s", call->portal->space_name);
            g_variant_builder_add(&command, "s", "--");
            g_variant_iter_init(&iterator, commandline);
            while (g_variant_iter_next(&iterator, "&s", &item))
                g_variant_builder_add(&command, "s", item);
            g_variant_dict_insert_value(&dict, "commandline", g_variant_builder_end(&command));
            g_variant_unref(commandline);
        }
    }
    g_free(host_session_token);
    g_free(host_token);
    g_free(fallback);
    g_free(fallback_session);
    return g_variant_ref_sink(g_variant_dict_end(&dict));
}

static char *dynamic_launcher_id(Portal *portal, const char *desktop_id)
{
    if (desktop_id == NULL || strchr(desktop_id, '/') != NULL
        || !g_str_has_suffix(desktop_id, ".desktop"))
        return NULL;
    if (g_str_has_prefix(desktop_id, portal->app_id)
        && desktop_id[strlen(portal->app_id)] == '.')
        return g_strdup(desktop_id);
    return g_strdup_printf("%s.%s", portal->app_id, desktop_id);
}

static char *wrapped_exec(Portal *portal, const char *command)
{
    if (command == NULL || *command == '\0') return NULL;
    char *quoted_name = g_shell_quote(portal->space_name);
    char *result = g_strdup_printf(
        "/usr/bin/spaces enter --graphical %s -- %s", quoted_name, command);
    g_free(quoted_name);
    return result;
}

static char *rewrite_desktop_entry(Portal *portal, const char *contents,
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
    if (!g_key_file_has_group(key, G_KEY_FILE_DESKTOP_GROUP)) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
            "The launcher has no Desktop Entry group");
        g_key_file_unref(key); return NULL;
    }
    groups = g_key_file_get_groups(key, &count);
    for (index = 0; index < count; index++) {
        char *command;
        char *wrapped;
        if (!g_str_equal(groups[index], G_KEY_FILE_DESKTOP_GROUP)
            && !g_str_has_prefix(groups[index], "Desktop Action "))
            continue;
        command = g_key_file_get_string(key, groups[index], "Exec", NULL);
        if (command == NULL) continue;
        wrapped = wrapped_exec(portal, command);
        g_key_file_set_string(key, groups[index], "Exec", wrapped);
        g_free(wrapped); g_free(command);
    }
    g_strfreev(groups);
    g_key_file_remove_key(key, G_KEY_FILE_DESKTOP_GROUP, "TryExec", NULL);
    g_key_file_remove_key(key, G_KEY_FILE_DESKTOP_GROUP, "Path", NULL);
    g_key_file_set_boolean(key, G_KEY_FILE_DESKTOP_GROUP, "DBusActivatable", FALSE);
    result = g_key_file_to_data(key, NULL, error);
    g_key_file_unref(key);
    return result;
}

static GVariant *prepare_parameters(ForwardCall *call, GVariant *parameters)
{
    GVariantBuilder tuple;
    GDBusArgInfo **argument;
    gsize index = 0;
    g_variant_builder_init(&tuple, G_VARIANT_TYPE_TUPLE);
    for (argument = call->method->in_args; argument != NULL && *argument != NULL; argument++, index++) {
        GVariant *child = g_variant_get_child_value(parameters, index);
        GVariant *mapped;
        if (g_str_equal(call->interface_name,
                DESKTOP_INTERFACE_PREFIX "DynamicLauncher")
            && g_str_equal((*argument)->name, "desktop_file_id")) {
            char *id = dynamic_launcher_id(call->portal,
                g_variant_get_string(child, NULL));
            if (id == NULL)
                mapped = g_variant_ref(child);
            else {
                mapped = g_variant_ref_sink(g_variant_new_string(id));
                g_free(id);
            }
        } else if (g_str_equal(call->interface_name,
                       DESKTOP_INTERFACE_PREFIX "DynamicLauncher")
            && g_str_equal(call->method_name, "Install")
            && g_str_equal((*argument)->name, "desktop_entry")) {
            GError *error = NULL;
            char *entry = rewrite_desktop_entry(call->portal,
                g_variant_get_string(child, NULL), &error);
            if (entry == NULL) {
                g_warning("Rejected dynamic launcher contents: %s",
                    error == NULL ? "invalid desktop entry" : error->message);
                g_clear_error(&error);
                mapped = g_variant_ref_sink(g_variant_new_string(""));
            } else {
                mapped = g_variant_ref_sink(g_variant_new_string(entry));
                g_free(entry);
            }
        } else if (g_str_equal((*argument)->name, "options")
            && g_variant_is_of_type(child, G_VARIANT_TYPE_VARDICT))
            mapped = rewrite_options(call, child);
        else
            mapped = translate_object_paths(call->portal, child, TRUE);
        if (g_str_equal(call->interface_name,
                DESKTOP_INTERFACE_PREFIX "Usb")
            && g_str_equal(call->method_name, "FinishAcquireDevices")
            && g_str_equal((*argument)->name, "handle"))
            call->finish_guest_request = g_variant_dup_string(child, NULL);
        g_variant_builder_add_value(&tuple, mapped);
        g_variant_unref(child);
    }
    return g_variant_ref_sink(g_variant_builder_end(&tuple));
}

static void close_host_object(Portal *portal, const char *path, const char *interface)
{
    if (path == NULL) return;
    g_dbus_connection_call(portal->host, PORTAL_NAME, path, interface, "Close",
        NULL, NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
}

static void lifecycle_call(GDBusConnection *connection, const char *sender,
    const char *object_path, const char *interface_name, const char *method_name,
    GVariant *parameters, GDBusMethodInvocation *invocation, gpointer user_data)
{
    Portal *portal = user_data;
    Request *request;
    Session *session;
    (void)connection; (void)parameters;
    if (!g_str_equal(method_name, "Close")) {
        g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
            G_DBUS_ERROR_UNKNOWN_METHOD, "Unknown lifecycle method");
        return;
    }
    if (g_str_equal(interface_name, REQUEST_INTERFACE)) {
        request = g_hash_table_lookup(portal->requests_guest, object_path);
        if (request == NULL || !g_str_equal(request->owner, sender)) goto denied;
        request->cancelled = TRUE;
        if (request->local && request->local_backend != NULL)
            g_dbus_connection_call(portal->guest, request->local_backend,
                object_path, "org.freedesktop.impl.portal.Request", "Close",
                NULL, NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
        else
            close_host_object(portal, request->host_path, REQUEST_INTERFACE);
    } else {
        session = g_hash_table_lookup(portal->sessions_guest, object_path);
        if (session == NULL || !g_str_equal(session->owner, sender)) goto denied;
        close_host_object(portal, session->host_path, SESSION_INTERFACE);
    }
    g_dbus_method_invocation_return_value(invocation, NULL);
    return;
denied:
    g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
        G_DBUS_ERROR_ACCESS_DENIED, "This portal object belongs to another caller");
}

static const GDBusInterfaceVTable lifecycle_vtable = { .method_call = lifecycle_call };

static gboolean register_request(Portal *portal, Request *request, GError **error)
{
    request->registration = g_dbus_connection_register_object(portal->guest,
        request->guest_path, portal->lifecycle_node->interfaces[0],
        &lifecycle_vtable, portal, NULL, error);
    if (request->registration == 0) return FALSE;
    g_hash_table_insert(portal->requests_guest, g_strdup(request->guest_path), request);
    if (request->host_path != NULL)
        g_hash_table_insert(portal->requests_host, g_strdup(request->host_path), request);
    return TRUE;
}

static gboolean register_session(Portal *portal, const char *owner,
    const char *guest_path, const char *host_path)
{
    Session *session;
    GError *error = NULL;
    if (guest_path == NULL || host_path == NULL) return FALSE;
    session = g_new0(Session, 1);
    session->portal = portal;
    session->owner = g_strdup(owner);
    session->guest_path = g_strdup(guest_path);
    session->host_path = g_strdup(host_path);
    session->registration = g_dbus_connection_register_object(portal->guest,
        guest_path, portal->lifecycle_node->interfaces[1], &lifecycle_vtable,
        portal, NULL, &error);
    if (session->registration == 0) {
        g_warning("Could not export portal session %s: %s", guest_path, error->message);
        g_clear_error(&error);
        session_free(session);
        return FALSE;
    }
    g_hash_table_insert(portal->sessions_guest, g_strdup(guest_path), session);
    g_hash_table_insert(portal->sessions_host, g_strdup(host_path), session);
    return TRUE;
}

static void forward_done(GObject *source, GAsyncResult *result, gpointer user_data)
{
    ForwardCall *call = user_data;
    GUnixFDList *fds = NULL;
    GError *error = NULL;
    GVariant *reply = g_dbus_connection_call_with_unix_fd_list_finish(
        G_DBUS_CONNECTION(source), &fds, result, &error);
    if (reply == NULL) {
        g_dbus_method_invocation_return_gerror(call->invocation, error);
        g_clear_error(&error);
        goto out;
    }
    if (call->returns_request) {
        const char *host_path;
        Request *request = g_new0(Request, 1);
        g_variant_get(reply, "(&o)", &host_path);
        request->portal = call->portal;
        request->owner = g_strdup(call->owner);
        request->guest_path = g_strdup(call->guest_request_path);
        request->host_path = g_strdup(host_path);
        request->guest_session_path = g_strdup(call->guest_session_path);
        request->persistent = g_str_equal(call->interface_name,
                DESKTOP_INTERFACE_PREFIX "Usb")
            && g_str_equal(call->method_name, "AcquireDevices");
        if (!register_request(call->portal, request, &error)) {
            close_host_object(call->portal, host_path, REQUEST_INTERFACE);
            request_free(request);
            g_dbus_method_invocation_return_gerror(call->invocation, error);
            g_clear_error(&error);
            g_variant_unref(reply);
            goto out;
        }
        g_variant_unref(reply);
        reply = g_variant_ref_sink(g_variant_new("(o)", request->guest_path));
    } else if (call->returns_session) {
        const char *host_path;
        g_variant_get(reply, "(&o)", &host_path);
        if (!register_session(call->portal, call->owner,
                call->guest_session_path, host_path)) {
            close_host_object(call->portal, host_path, SESSION_INTERFACE);
            g_dbus_method_invocation_return_error(call->invocation,
                G_DBUS_ERROR, G_DBUS_ERROR_FAILED,
                "Could not export the portal session");
            g_variant_unref(reply);
            goto out;
        }
        g_variant_unref(reply);
        reply = g_variant_ref_sink(g_variant_new("(o)",
            call->guest_session_path));
    }
    if (!call->returns_request && !call->returns_session
        && call->guest_session_path != NULL
        && g_variant_n_children(reply) > 0) {
        GVariant *results = g_variant_get_child_value(reply, 0);
        if (g_variant_is_of_type(results, G_VARIANT_TYPE_VARDICT)) {
            GVariant *session_value = g_variant_lookup_value(
                results, "session_handle", NULL);
            if (session_value != NULL
                && (g_variant_is_of_type(session_value,
                        G_VARIANT_TYPE_STRING)
                    || g_variant_is_of_type(session_value,
                        G_VARIANT_TYPE_OBJECT_PATH))) {
                const char *host_path = g_variant_get_string(
                    session_value, NULL);
                if (!g_variant_is_object_path(host_path)
                    || !register_session(call->portal, call->owner,
                        call->guest_session_path, host_path)) {
                    g_dbus_method_invocation_return_error(call->invocation,
                        G_DBUS_ERROR, G_DBUS_ERROR_FAILED,
                        "Could not export the portal session");
                    g_variant_unref(session_value);
                    g_variant_unref(results);
                    g_variant_unref(reply);
                    goto out;
                }
                GVariantDict dictionary;
                GVariantBuilder tuple;
                gsize index;
                g_variant_dict_init(&dictionary, results);
                g_variant_dict_insert(&dictionary, "session_handle",
                    g_variant_is_of_type(session_value,
                        G_VARIANT_TYPE_OBJECT_PATH) ? "o" : "s",
                    call->guest_session_path);
                g_variant_builder_init(&tuple, G_VARIANT_TYPE_TUPLE);
                g_variant_builder_add_value(&tuple,
                    g_variant_dict_end(&dictionary));
                for (index = 1; index < g_variant_n_children(reply); index++) {
                    GVariant *child = g_variant_get_child_value(reply, index);
                    g_variant_builder_add_value(&tuple, child);
                    g_variant_unref(child);
                }
                g_variant_unref(reply);
                reply = g_variant_ref_sink(g_variant_builder_end(&tuple));
            }
            g_clear_pointer(&session_value, g_variant_unref);
        }
        g_variant_unref(results);
    }
    if (call->finish_guest_request != NULL
        && g_variant_n_children(reply) == 2) {
        GVariant *finished_value = g_variant_get_child_value(reply, 1);
        gboolean finished = g_variant_is_of_type(finished_value,
            G_VARIANT_TYPE_BOOLEAN) && g_variant_get_boolean(finished_value);
        g_variant_unref(finished_value);
        if (finished) {
            Request *request = g_hash_table_lookup(
                call->portal->requests_guest, call->finish_guest_request);
            if (request != NULL) {
                g_hash_table_remove(call->portal->requests_host,
                    request->host_path);
                g_hash_table_remove(call->portal->requests_guest,
                    request->guest_path);
            }
        }
    }
    if (fds != NULL)
        g_dbus_method_invocation_return_value_with_unix_fd_list(call->invocation, reply, fds);
    else
        g_dbus_method_invocation_return_value(call->invocation, reply);
    g_variant_unref(reply);
out:
    g_clear_object(&fds);
    forward_call_free(call);
}

static void return_not_available(GDBusMethodInvocation *invocation, const char *message)
{
    g_dbus_method_invocation_return_dbus_error(invocation,
        "org.freedesktop.portal.Error.NotAvailable", message);
}

typedef struct { Portal *portal; Request *request; } ChooserCall;

static char *flatpak_app_id(Portal *portal, const char *sender)
{
    GVariant *reply;
    guint pid;
    char *path;
    GKeyFile *info;
    char *app_id = NULL;
    reply = g_dbus_connection_call_sync(portal->guest, DBUS_NAME, DBUS_PATH,
        DBUS_NAME, "GetConnectionUnixProcessID", g_variant_new("(s)", sender),
        G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, 3000, NULL, NULL);
    if (reply == NULL) return NULL;
    g_variant_get(reply, "(u)", &pid); g_variant_unref(reply);
    path = g_strdup_printf("/proc/%u/root/.flatpak-info", pid);
    info = g_key_file_new();
    if (g_key_file_load_from_file(info, path, G_KEY_FILE_NONE, NULL))
        app_id = g_key_file_get_string(info, "Application", "name", NULL);
    if (app_id != NULL && (!g_dbus_is_name(app_id)
            || g_str_has_prefix(app_id, ":")))
        g_clear_pointer(&app_id, g_free);
    g_key_file_unref(info); g_free(path);
    return app_id;
}

static GVariant *export_flatpak_uris(Request *request, GVariant *results)
{
    GVariant *uris = g_variant_lookup_value(results, "uris",
        G_VARIANT_TYPE_STRING_ARRAY);
    GVariantIter iterator;
    const char *uri;
    GPtrArray *filenames;
    GUnixFDList *fds;
    GVariantBuilder handles;
    GVariantBuilder permissions;
    GVariant *reply;
    GError *error = NULL;
    if (uris == NULL || request->flatpak_app_id == NULL) return g_variant_ref(results);
    filenames = g_ptr_array_new_with_free_func(g_free);
    fds = g_unix_fd_list_new();
    g_variant_builder_init(&handles, G_VARIANT_TYPE("ah"));
    g_variant_iter_init(&iterator, uris);
    while (g_variant_iter_next(&iterator, "&s", &uri)) {
        char *filename = g_filename_from_uri(uri, NULL, NULL);
        int descriptor = filename == NULL ? -1
            : open(filename, O_PATH | O_CLOEXEC | O_NOFOLLOW);
        gint handle = descriptor < 0 ? -1
            : g_unix_fd_list_append(fds, descriptor, &error);
        if (descriptor >= 0) close(descriptor);
        if (handle < 0) {
            g_clear_error(&error); g_free(filename);
            g_object_unref(fds); g_ptr_array_unref(filenames);
            g_variant_unref(uris); return g_variant_ref(results);
        }
        g_variant_builder_add(&handles, "h", handle);
        g_ptr_array_add(filenames, filename);
    }
    g_variant_builder_init(&permissions, G_VARIANT_TYPE_STRING_ARRAY);
    g_variant_builder_add(&permissions, "s", "read");
    if (request->chooser_writable) g_variant_builder_add(&permissions, "s", "write");
    reply = g_dbus_connection_call_with_unix_fd_list_sync(request->portal->guest,
        "org.freedesktop.portal.Documents", "/org/freedesktop/portal/documents",
        "org.freedesktop.portal.Documents", "AddFull",
        g_variant_new("(@ahus@as)", g_variant_builder_end(&handles), 3U,
            request->flatpak_app_id, g_variant_builder_end(&permissions)),
        G_VARIANT_TYPE("(asa{sv})"), G_DBUS_CALL_FLAGS_NONE, 5000,
        fds, NULL, NULL, NULL);
    g_object_unref(fds);
    if (reply != NULL) {
        GVariant *doc_ids; GVariant *extra;
        GVariant *mount_value;
        GVariantBuilder exported;
        GVariantDict dictionary;
        const guint8 *mount_bytes;
        gsize mount_size;
        guint index;
        g_variant_get(reply, "(@as@a{sv})", &doc_ids, &extra);
        mount_value = g_variant_lookup_value(extra, "mountpoint",
            G_VARIANT_TYPE_BYTESTRING);
        g_variant_builder_init(&exported, G_VARIANT_TYPE_STRING_ARRAY);
        if (mount_value != NULL) {
            mount_bytes = g_variant_get_fixed_array(mount_value, &mount_size,
                sizeof(guint8));
            for (index = 0; index < filenames->len; index++) {
                GVariant *doc = g_variant_get_child_value(doc_ids, index);
                const char *doc_id = g_variant_get_string(doc, NULL);
                char *mount = g_strndup((const char *)mount_bytes, mount_size);
                char *basename = g_path_get_basename(
                    g_ptr_array_index(filenames, index));
                char *path = g_build_filename(mount, doc_id, basename, NULL);
                char *exported_uri = *doc_id == '\0' ? g_filename_to_uri(
                    g_ptr_array_index(filenames, index), NULL, NULL)
                    : g_filename_to_uri(path, NULL, NULL);
                g_variant_builder_add(&exported, "s", exported_uri);
                g_free(exported_uri); g_free(path); g_free(basename);
                g_free(mount); g_variant_unref(doc);
            }
            g_variant_dict_init(&dictionary, results);
            g_variant_dict_insert_value(&dictionary, "uris",
                g_variant_builder_end(&exported));
            GVariant *updated = g_variant_ref_sink(g_variant_dict_end(&dictionary));
            g_variant_unref(mount_value); g_variant_unref(doc_ids);
            g_variant_unref(extra); g_variant_unref(reply); g_variant_unref(uris);
            g_ptr_array_unref(filenames); return updated;
        }
        g_variant_unref(doc_ids); g_variant_unref(extra); g_variant_unref(reply);
    }
    g_variant_unref(uris); g_ptr_array_unref(filenames);
    return g_variant_ref(results);
}

static void chooser_done(GObject *source, GAsyncResult *result, gpointer user_data)
{
    ChooserCall *call = user_data;
    GError *error = NULL;
    GVariant *reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(source), result, &error);
    guint response = 2;
    GVariant *results = NULL;
    if (reply != NULL) {
        g_variant_get(reply, "(u@a{sv})", &response, &results);
        g_variant_unref(reply);
    } else {
        g_clear_error(&error);
        results = g_variant_ref_sink(g_variant_new_array(G_VARIANT_TYPE("{sv}"), NULL, 0));
    }
    if (call->request->cancelled) {
        g_variant_unref(results);
        g_hash_table_remove(call->portal->requests_guest,
            call->request->guest_path);
        g_free(call);
        return;
    }
    if (response == 0 && call->request->flatpak_app_id != NULL) {
        GVariant *exported = export_flatpak_uris(call->request, results);
        g_variant_unref(results); results = exported;
    }
    g_dbus_connection_emit_signal(call->portal->guest, call->request->owner,
        call->request->guest_path, REQUEST_INTERFACE, "Response",
        g_variant_new("(u@a{sv})", response, results), NULL);
    g_hash_table_remove(call->portal->requests_guest, call->request->guest_path);
    g_free(call);
}

static void file_chooser_call(Portal *portal, const char *sender,
    const char *method_name, GVariant *parameters, GDBusMethodInvocation *invocation)
{
    const char *parent, *title, *token = NULL;
    GVariant *options;
    char *fallback = NULL;
    Request *request;
    ChooserCall *call;
    GError *error = NULL;
    if (portal->file_chooser_backend == NULL) {
        return_not_available(invocation, "No FileChooser backend is installed in this Space");
        return;
    }
    g_variant_get(parameters, "(&s&s@a{sv})", &parent, &title, &options);
    const struct { const char *name; const char *type; } option_types[] = {
        { "handle_token", "s" }, { "accept_label", "s" },
        { "modal", "b" }, { "multiple", "b" }, { "directory", "b" },
        { "filters", "a(sa(us))" }, { "current_filter", "(sa(us))" },
        { "choices", "a(ssa(ss)s)" }, { "current_folder", "ay" },
        { "current_name", "s" }, { "current_file", "ay" },
        { "files", "aay" },
    };
    guint option_index;
    for (option_index = 0; option_index < G_N_ELEMENTS(option_types);
         option_index++) {
        GVariant *option = g_variant_lookup_value(options,
            option_types[option_index].name, NULL);
        if (option != NULL && !g_variant_is_of_type(option,
                G_VARIANT_TYPE(option_types[option_index].type))) {
            g_variant_unref(option); g_variant_unref(options);
            g_dbus_method_invocation_return_dbus_error(invocation,
                "org.freedesktop.portal.Error.InvalidArgument",
                "A FileChooser option has an invalid type");
            return;
        }
        g_clear_pointer(&option, g_variant_unref);
    }
    g_variant_lookup(options, "handle_token", "&s", &token);
    if (!valid_token(token)) token = fallback = new_token();
    request = g_new0(Request, 1);
    request->portal = portal;
    request->owner = g_strdup(sender);
    request->guest_path = public_object_path("request", sender, token);
    request->local = TRUE;
    request->local_backend = g_strdup(portal->file_chooser_backend);
    request->flatpak_app_id = flatpak_app_id(portal, sender);
    request->chooser_writable = !g_str_equal(method_name, "OpenFile");
    if (!register_request(portal, request, &error)) {
        g_dbus_method_invocation_return_gerror(invocation, error);
        g_clear_error(&error);
        request_free(request);
        g_variant_unref(options);
        g_free(fallback);
        return;
    }
    call = g_new0(ChooserCall, 1);
    call->portal = portal;
    call->request = request;
    g_dbus_connection_call(portal->guest, portal->file_chooser_backend,
        PORTAL_PATH, "org.freedesktop.impl.portal.FileChooser", method_name,
        g_variant_new("(osss@a{sv})", request->guest_path, portal->app_id,
            parent, title, options), G_VARIANT_TYPE("(ua{sv})"),
        G_DBUS_CALL_FLAGS_NONE, -1, NULL, chooser_done, call);
    g_dbus_method_invocation_return_value(invocation, g_variant_new("(o)", request->guest_path));
    g_free(fallback);
}

static int pidfd_open_compat(pid_t pid, unsigned int flags)
{
#ifdef SYS_pidfd_open
    return (int)syscall(SYS_pidfd_open, pid, flags);
#else
    (void)pid; (void)flags; errno = ENOSYS; return -1;
#endif
}

static void game_mode_call(Portal *portal, const char *sender,
    const char *method_name, GVariant *parameters, GDBusMethodInvocation *invocation)
{
    const char *broker_name = g_getenv("SPACES_INTEGRATION_BROKER");
    gint target, requester;
    pid_t caller_pid;
    GVariant *pid_reply;
    int target_fd = -1, requester_fd = -1;
    const char *host_method = NULL;
    GUnixFDList *fds = NULL;
    GError *error = NULL;
    GVariant *reply;
    gint th, rh;
    gboolean own_fds = FALSE;
    if (broker_name == NULL || *broker_name == '\0') {
        return_not_available(invocation,
            "The host GameMode translation broker is unavailable");
        return;
    }
    if (g_str_has_suffix(method_name, "ByPIDFd")) {
        g_variant_get(parameters, "(hh)", &th, &rh);
        fds = g_dbus_message_get_unix_fd_list(
            g_dbus_method_invocation_get_message(invocation));
        host_method = method_name;
    } else {
        if (g_str_has_suffix(method_name, "ByPid"))
            g_variant_get(parameters, "(ii)", &target, &requester);
        else {
            g_variant_get(parameters, "(i)", &target);
            pid_reply = g_dbus_connection_call_sync(portal->guest, DBUS_NAME,
                DBUS_PATH, DBUS_NAME, "GetConnectionUnixProcessID",
                g_variant_new("(s)", sender), G_VARIANT_TYPE("(u)"),
                G_DBUS_CALL_FLAGS_NONE, 5000, NULL, &error);
            if (pid_reply == NULL) goto failed;
            g_variant_get(pid_reply, "(u)", &caller_pid);
            g_variant_unref(pid_reply);
            requester = (gint)caller_pid;
        }
        if (g_str_has_prefix(method_name, "Query"))
            host_method = "QueryStatusByPIDFd";
        else if (g_str_has_prefix(method_name, "Register"))
            host_method = "RegisterGameByPIDFd";
        else if (g_str_has_prefix(method_name, "Unregister"))
            host_method = "UnregisterGameByPIDFd";
        if (host_method == NULL) { errno = EINVAL; goto syscall_failed; }
        target_fd = pidfd_open_compat((pid_t)target, 0);
        requester_fd = pidfd_open_compat((pid_t)requester, 0);
        if (target_fd < 0 || requester_fd < 0) goto syscall_failed;
        fds = g_unix_fd_list_new();
        own_fds = TRUE;
        th = g_unix_fd_list_append(fds, target_fd, &error);
        rh = g_unix_fd_list_append(fds, requester_fd, &error);
        if (th < 0 || rh < 0) goto failed;
    }
    reply = g_dbus_connection_call_with_unix_fd_list_sync(portal->host,
        broker_name, INTEGRATION_PATH, INTEGRATION_INTERFACE, "MakeGameMode",
        g_variant_new("(shh)", host_method, th, rh), G_VARIANT_TYPE("(i)"),
        G_DBUS_CALL_FLAGS_NONE, -1, fds, NULL, NULL, &error);
    if (own_fds) { g_object_unref(fds); fds = NULL; }
    if (reply == NULL) goto failed;
    g_dbus_method_invocation_return_value(invocation, reply);
    g_variant_unref(reply);
    close(target_fd); close(requester_fd);
    return;
syscall_failed:
    g_set_error(&error, G_IO_ERROR, g_io_error_from_errno(errno),
        "Could not translate the guest process to a pidfd: %s", g_strerror(errno));
failed:
    if (own_fds && fds != NULL) g_object_unref(fds);
    if (target_fd >= 0) close(target_fd);
    if (requester_fd >= 0) close(requester_fd);
    if (error != NULL) {
        g_dbus_method_invocation_return_gerror(invocation, error);
        g_clear_error(&error);
    } else return_not_available(invocation, "The host GameMode portal does not support pidfds");
}

static void realtime_call(Portal *portal, const char *method_name,
    GVariant *parameters, GDBusMethodInvocation *invocation)
{
    const char *broker_name = g_getenv("SPACES_INTEGRATION_BROKER");
    guint64 process, thread;
    gboolean high_priority = g_str_equal(method_name, "MakeThreadHighPriorityWithPID");
    gint priority;
    int process_fd = -1, thread_fd = -1;
    GUnixFDList *fds = NULL;
    GError *error = NULL;
    gint process_handle, thread_handle;

    if (broker_name == NULL || *broker_name == '\0') {
        return_not_available(invocation, "The host PID translation broker is unavailable");
        return;
    }
    if (high_priority)
        g_variant_get(parameters, "(tti)", &process, &thread, &priority);
    else {
        guint realtime_priority;
        g_variant_get(parameters, "(ttu)", &process, &thread, &realtime_priority);
        if (realtime_priority > G_MAXINT) {
            g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
                G_DBUS_ERROR_INVALID_ARGS, "Realtime priority is out of range");
            return;
        }
        priority = (gint)realtime_priority;
    }
    if (process > G_MAXINT || thread > G_MAXINT) {
        g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
            G_DBUS_ERROR_INVALID_ARGS, "Process or thread ID is out of range");
        return;
    }
    process_fd = pidfd_open_compat((pid_t)process, 0);
    thread_fd = pidfd_open_compat((pid_t)thread, PIDFD_THREAD);
    if (process_fd < 0 || thread_fd < 0) {
        if (errno == EINVAL || errno == ENOSYS)
            return_not_available(invocation, "The kernel does not support thread pidfds");
        else
            g_dbus_method_invocation_return_error(invocation, G_IO_ERROR,
                g_io_error_from_errno(errno), "Could not open process pidfds: %s",
                g_strerror(errno));
        if (process_fd >= 0) close(process_fd);
        if (thread_fd >= 0) close(thread_fd);
        return;
    }
    fds = g_unix_fd_list_new();
    process_handle = g_unix_fd_list_append(fds, process_fd, &error);
    thread_handle = g_unix_fd_list_append(fds, thread_fd, &error);
    close(process_fd); close(thread_fd);
    if (process_handle < 0 || thread_handle < 0) {
        g_dbus_method_invocation_return_gerror(invocation, error);
        g_clear_error(&error); g_object_unref(fds); return;
    }
    g_dbus_connection_call_with_unix_fd_list(portal->host, broker_name,
        INTEGRATION_PATH, INTEGRATION_INTERFACE, "MakeRealtime",
        g_variant_new("(hhbi)", process_handle, thread_handle, high_priority, priority),
        G_VARIANT_TYPE_UNIT, G_DBUS_CALL_FLAGS_NONE, -1, fds, NULL,
        bridge_done, g_object_ref(invocation));
    g_object_unref(fds);
}

typedef struct {
    Portal *portal;
    char *guest_path;
} ScreenshotCall;

static gboolean copy_screenshot(int source, char **uri)
{
    char *directory = g_build_filename(g_get_user_cache_dir(), "spaces",
        "screenshots", NULL);
    char *token = new_token();
    char *filename = g_strdup_printf("%s/%s.png", directory, token);
    guint8 buffer[64 * 1024];
    gsize total = 0;
    int output = -1;
    gboolean success = FALSE;
    g_free(token);
    if (g_mkdir_with_parents(directory, 0700) < 0) goto out;
    output = open(filename, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (output < 0) goto out;
    for (;;) {
        ssize_t count = read(source, buffer, sizeof(buffer));
        gsize offset = 0;
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) goto out;
        if (count == 0) break;
        total += (gsize)count;
        if (total > 32 * 1024 * 1024) goto out;
        while (offset < (gsize)count) {
            ssize_t written = write(output, buffer + offset,
                (gsize)count - offset);
            if (written < 0 && errno == EINTR) continue;
            if (written <= 0) goto out;
            offset += (gsize)written;
        }
    }
    if (close(output) < 0) { output = -1; goto out; }
    output = -1;
    *uri = g_filename_to_uri(filename, NULL, NULL);
    success = *uri != NULL;
out:
    if (output >= 0) close(output);
    if (!success) unlink(filename);
    g_free(filename); g_free(directory);
    return success;
}

static void screenshot_done(GObject *source, GAsyncResult *result,
    gpointer user_data)
{
    ScreenshotCall *call = user_data;
    Request *request = g_hash_table_lookup(call->portal->requests_guest,
        call->guest_path);
    GUnixFDList *fds = NULL;
    GError *error = NULL;
    GVariant *reply = g_dbus_connection_call_with_unix_fd_list_finish(
        G_DBUS_CONNECTION(source), &fds, result, &error);
    guint response = 2;
    gint handle = -1;
    GVariant *results = NULL;
    int descriptor = -1;
    char *uri = NULL;
    if (request == NULL) goto out;
    if (request->cancelled) {
        g_hash_table_remove(call->portal->requests_guest,
            request->guest_path);
        goto out;
    }
    if (reply != NULL) {
        g_variant_get(reply, "(uh@a{sv})", &response, &handle, &results);
        if (response == 0 && fds != NULL)
            descriptor = g_unix_fd_list_get(fds, handle, &error);
        if (response == 0 && (descriptor < 0
                || !copy_screenshot(descriptor, &uri)))
            response = 2;
    }
    if (results == NULL)
        results = g_variant_ref_sink(g_variant_new_array(
            G_VARIANT_TYPE("{sv}"), NULL, 0));
    if (response == 0 && uri != NULL) {
        GVariantDict dictionary;
        g_variant_dict_init(&dictionary, results);
        g_variant_dict_insert(&dictionary, "uri", "s", uri);
        g_variant_unref(results);
        results = g_variant_ref_sink(g_variant_dict_end(&dictionary));
        if (request->flatpak_app_id != NULL) {
            GVariantBuilder uri_list;
            GVariantDict export_dict;
            GVariant *export_input;
            GVariant *exported;
            GVariant *exported_uris;
            g_variant_builder_init(&uri_list, G_VARIANT_TYPE_STRING_ARRAY);
            g_variant_builder_add(&uri_list, "s", uri);
            g_variant_dict_init(&export_dict, NULL);
            g_variant_dict_insert_value(&export_dict, "uris",
                g_variant_builder_end(&uri_list));
            export_input = g_variant_ref_sink(g_variant_dict_end(&export_dict));
            exported = export_flatpak_uris(request, export_input);
            exported_uris = g_variant_lookup_value(exported, "uris",
                G_VARIANT_TYPE_STRING_ARRAY);
            if (exported_uris != NULL && g_variant_n_children(exported_uris) == 1) {
                GVariant *first = g_variant_get_child_value(exported_uris, 0);
                GVariantDict result_dict;
                g_variant_dict_init(&result_dict, results);
                g_variant_dict_insert(&result_dict, "uri", "s",
                    g_variant_get_string(first, NULL));
                g_variant_unref(results);
                results = g_variant_ref_sink(g_variant_dict_end(&result_dict));
                g_variant_unref(first);
            }
            g_clear_pointer(&exported_uris, g_variant_unref);
            g_variant_unref(exported); g_variant_unref(export_input);
        }
    }
    g_dbus_connection_emit_signal(call->portal->guest, request->owner,
        request->guest_path, REQUEST_INTERFACE, "Response",
        g_variant_new("(u@a{sv})", response, results), NULL);
    g_hash_table_remove(call->portal->requests_guest, request->guest_path);
out:
    if (descriptor >= 0) close(descriptor);
    g_free(uri); g_clear_error(&error); g_clear_object(&fds);
    g_clear_pointer(&reply, g_variant_unref);
    g_free(call->guest_path); g_free(call);
}

static void screenshot_call(Portal *portal, const char *sender,
    GVariant *parameters, GDBusMethodInvocation *invocation)
{
    const char *broker = g_getenv("SPACES_INTEGRATION_BROKER");
    const char *parent, *token = NULL;
    GVariant *options;
    char *fallback = NULL;
    Request *request;
    ScreenshotCall *call;
    GError *error = NULL;
    if (broker == NULL || *broker == '\0') {
        return_not_available(invocation, "The host file staging broker is unavailable");
        return;
    }
    g_variant_get(parameters, "(&s@a{sv})", &parent, &options);
    g_variant_lookup(options, "handle_token", "&s", &token);
    if (!valid_token(token)) token = fallback = new_token();
    request = g_new0(Request, 1);
    request->portal = portal; request->owner = g_strdup(sender);
    request->guest_path = public_object_path("request", sender, token);
    request->local = TRUE;
    request->flatpak_app_id = flatpak_app_id(portal, sender);
    if (!register_request(portal, request, &error)) {
        g_dbus_method_invocation_return_gerror(invocation, error);
        g_clear_error(&error); request_free(request); g_variant_unref(options);
        g_free(fallback); return;
    }
    call = g_new0(ScreenshotCall, 1);
    call->portal = portal; call->guest_path = g_strdup(request->guest_path);
    g_dbus_connection_call_with_unix_fd_list(portal->host, broker, INTEGRATION_PATH,
        INTEGRATION_INTERFACE, "Screenshot", g_variant_new("(s@a{sv})", parent, options),
        G_VARIANT_TYPE("(uha{sv})"), G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL,
        screenshot_done, call);
    g_dbus_method_invocation_return_value(invocation,
        g_variant_new("(o)", request->guest_path));
    g_free(fallback);
}

typedef struct {
    Portal *portal;
    char *guest_path;
} BrokerRequestCall;

static void broker_request_done(GObject *source, GAsyncResult *result,
    gpointer user_data)
{
    BrokerRequestCall *call = user_data;
    Request *request = g_hash_table_lookup(call->portal->requests_guest,
        call->guest_path);
    GError *error = NULL;
    GUnixFDList *fds = NULL;
    GVariant *reply = g_dbus_connection_call_with_unix_fd_list_finish(
        G_DBUS_CONNECTION(source), &fds, result, &error);
    guint response = 2;
    if (request != NULL) {
        if (request->cancelled) {
            g_hash_table_remove(call->portal->requests_guest,
                request->guest_path);
            goto done;
        }
        if (reply != NULL) g_variant_get(reply, "(u)", &response);
        g_dbus_connection_emit_signal(call->portal->guest, request->owner,
            request->guest_path, REQUEST_INTERFACE, "Response",
            g_variant_new("(u@a{sv})", response,
                g_variant_new_array(G_VARIANT_TYPE("{sv}"), NULL, 0)), NULL);
        g_hash_table_remove(call->portal->requests_guest, request->guest_path);
    }
done:
    g_clear_object(&fds); g_clear_pointer(&reply, g_variant_unref);
    g_clear_error(&error);
    g_free(call->guest_path); g_free(call);
}

static void open_file_call(Portal *portal, const char *sender,
    const char *method_name, GVariant *parameters,
    GDBusMethodInvocation *invocation)
{
    const char *broker = g_getenv("SPACES_INTEGRATION_BROKER");
    const char *parent, *token = NULL, *activation = "";
    gint handle;
    GVariant *options;
    GUnixFDList *incoming;
    int descriptor = -1;
    char descriptor_path[64];
    char *guest_path = NULL;
    gboolean writable = FALSE;
    char *fallback = NULL;
    Request *request;
    BrokerRequestCall *call;
    GUnixFDList *outgoing;
    gint outgoing_handle;
    GError *error = NULL;
    if (broker == NULL || *broker == '\0') {
        return_not_available(invocation, "The host path broker is unavailable");
        return;
    }
    g_variant_get(parameters, "(&sh@a{sv})", &parent, &handle, &options);
    (void)parent;
    incoming = g_dbus_message_get_unix_fd_list(
        g_dbus_method_invocation_get_message(invocation));
    if (incoming != NULL) descriptor = g_unix_fd_list_get(incoming, handle, &error);
    if (descriptor < 0) goto failed;
    g_snprintf(descriptor_path, sizeof(descriptor_path), "/proc/self/fd/%d",
        descriptor);
    guest_path = g_file_read_link(descriptor_path, &error);
    if (guest_path == NULL || guest_path[0] != '/'
        || g_str_has_suffix(guest_path, " (deleted)")) goto failed;
    g_variant_lookup(options, "handle_token", "&s", &token);
    g_variant_lookup(options, "activation_token", "&s", &activation);
    if (g_str_equal(method_name, "OpenFile"))
        g_variant_lookup(options, "writable", "b", &writable);
    if (!valid_token(token)) token = fallback = new_token();
    request = g_new0(Request, 1); request->portal = portal;
    request->owner = g_strdup(sender);
    request->guest_path = public_object_path("request", sender, token);
    request->local = TRUE;
    if (!register_request(portal, request, &error)) { request_free(request); goto failed; }
    outgoing = g_unix_fd_list_new();
    outgoing_handle = g_unix_fd_list_append(outgoing, descriptor, &error);
    if (outgoing_handle < 0) {
        g_hash_table_remove(portal->requests_guest, request->guest_path);
        g_object_unref(outgoing); goto failed;
    }
    call = g_new0(BrokerRequestCall, 1); call->portal = portal;
    call->guest_path = g_strdup(request->guest_path);
    g_dbus_connection_call_with_unix_fd_list(portal->host, broker, INTEGRATION_PATH,
        INTEGRATION_INTERFACE, method_name,
        g_str_equal(method_name, "OpenFile")
            ? g_variant_new("(shbs)", guest_path, outgoing_handle, writable,
                activation == NULL ? "" : activation)
            : g_variant_new("(shs)", guest_path, outgoing_handle,
                activation == NULL ? "" : activation),
        G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, -1, outgoing, NULL,
        broker_request_done, call);
    g_object_unref(outgoing);
    g_dbus_method_invocation_return_value(invocation,
        g_variant_new("(o)", request->guest_path));
    close(descriptor); g_variant_unref(options); g_free(guest_path);
    g_free(fallback); return;
failed:
    if (descriptor >= 0) close(descriptor);
    g_variant_unref(options); g_free(guest_path); g_free(fallback);
    if (error == NULL)
        g_set_error(&error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
            "The file descriptor has no stable guest path");
    g_dbus_method_invocation_return_gerror(invocation, error);
    g_clear_error(&error);
}

typedef struct {
    Portal *portal;
    char *guest_path;
} TransformedRequestCall;

static void transformed_request_done(GObject *source, GAsyncResult *result,
    gpointer user_data)
{
    TransformedRequestCall *call = user_data;
    Request *request = g_hash_table_lookup(call->portal->requests_guest,
        call->guest_path);
    GError *error = NULL;
    GUnixFDList *fds = NULL;
    GVariant *reply = g_dbus_connection_call_with_unix_fd_list_finish(
        G_DBUS_CONNECTION(source), &fds, result, &error);
    guint response = 2;
    GVariant *results = NULL;
    if (request != NULL) {
        if (request->cancelled) {
            g_hash_table_remove(call->portal->requests_guest,
                request->guest_path);
            goto done;
        }
        if (reply != NULL)
            g_variant_get(reply, "(u@a{sv})", &response, &results);
        if (results == NULL)
            results = g_variant_ref_sink(g_variant_new_array(
                G_VARIANT_TYPE("{sv}"), NULL, 0));
        g_dbus_connection_emit_signal(call->portal->guest, request->owner,
            request->guest_path, REQUEST_INTERFACE, "Response",
            g_variant_new("(u@a{sv})", response, results), NULL);
        g_hash_table_remove(call->portal->requests_guest, request->guest_path);
    }
done:
    g_clear_object(&fds); g_clear_pointer(&reply, g_variant_unref);
    g_clear_error(&error);
    g_free(call->guest_path); g_free(call);
}

static void transformed_request_call(Portal *portal, const char *sender,
    const char *interface_name, const char *method_name, GVariant *parameters,
    GDBusMethodInvocation *invocation)
{
    const char *broker = g_getenv("SPACES_INTEGRATION_BROKER");
    GVariant *options = g_variant_get_child_value(parameters,
        g_variant_n_children(parameters) - 1);
    const char *token = NULL;
    char *fallback = NULL;
    char *app_id = NULL;
    Request *request;
    TransformedRequestCall *call;
    GUnixFDList *fds;
    GError *error = NULL;
    if (broker == NULL || *broker == '\0') {
        g_variant_unref(options);
        return_not_available(invocation, "The host integration broker is unavailable");
        return;
    }
    g_variant_lookup(options, "handle_token", "&s", &token);
    if (!valid_token(token)) token = fallback = new_token();
    request = g_new0(Request, 1); request->portal = portal;
    request->owner = g_strdup(sender);
    request->guest_path = public_object_path("request", sender, token);
    request->local = TRUE;
    if (!register_request(portal, request, &error)) {
        request_free(request); g_variant_unref(options); g_free(fallback);
        g_dbus_method_invocation_return_gerror(invocation, error);
        g_clear_error(&error); return;
    }
    call = g_new0(TransformedRequestCall, 1); call->portal = portal;
    call->guest_path = g_strdup(request->guest_path);
    fds = g_dbus_message_get_unix_fd_list(
        g_dbus_method_invocation_get_message(invocation));
    if (g_str_equal(interface_name, DESKTOP_INTERFACE_PREFIX "Secret"))
        app_id = flatpak_app_id(portal, sender);
    g_dbus_connection_call_with_unix_fd_list(portal->host, broker, INTEGRATION_PATH,
        INTEGRATION_INTERFACE,
        "PortalRequest", g_variant_new("(sssv)", interface_name, method_name,
            app_id == NULL ? "" : app_id, parameters),
        G_VARIANT_TYPE("(ua{sv})"), G_DBUS_CALL_FLAGS_NONE,
        -1, fds, NULL, transformed_request_done, call);
    g_dbus_method_invocation_return_value(invocation,
        g_variant_new("(o)", request->guest_path));
    g_variant_unref(options); g_free(fallback); g_free(app_id);
}

typedef struct { GDBusMethodInvocation *invocation; } DynamicCall;
static void dynamic_done(GObject *source, GAsyncResult *result,
    gpointer user_data)
{
    DynamicCall *call = user_data;
    GError *error = NULL;
    GVariant *reply = g_dbus_connection_call_finish(
        G_DBUS_CONNECTION(source), result, &error);
    if (reply == NULL) {
        g_dbus_method_invocation_return_gerror(call->invocation, error);
        g_clear_error(&error);
    } else {
        GVariant *wrapped;
        GVariant *inner;
        g_variant_get(reply, "(@v)", &wrapped);
        inner = g_variant_get_variant(wrapped);
        g_dbus_method_invocation_return_value(call->invocation, inner);
        g_variant_unref(inner); g_variant_unref(wrapped); g_variant_unref(reply);
    }
    g_object_unref(call->invocation); g_free(call);
}

static void dynamic_call(Portal *portal, const char *method_name,
    GVariant *parameters, GDBusMethodInvocation *invocation)
{
    const char *broker = g_getenv("SPACES_INTEGRATION_BROKER");
    DynamicCall *call;
    if (broker == NULL || *broker == '\0') {
        return_not_available(invocation, "The launcher integration broker is unavailable");
        return;
    }
    call = g_new0(DynamicCall, 1);
    call->invocation = g_object_ref(invocation);
    g_dbus_connection_call(portal->host, broker, INTEGRATION_PATH, INTEGRATION_INTERFACE,
        "DynamicLauncherCall", g_variant_new("(sv)", method_name, parameters),
        G_VARIANT_TYPE("(v)"), G_DBUS_CALL_FLAGS_NONE, -1, NULL,
        dynamic_done, call);
}

static void portal_method_call(GDBusConnection *connection, const char *sender,
    const char *object_path, const char *interface_name, const char *method_name,
    GVariant *parameters, GDBusMethodInvocation *invocation, gpointer user_data)
{
    Portal *portal = user_data;
    GDBusInterfaceInfo *interface_info;
    GDBusMethodInfo *method;
    GVariant *mapped;
    GVariantType *reply_type;
    GUnixFDList *fds;
    ForwardCall *call;
    (void)connection; (void)object_path;
    interface_info = find_registered_interface(portal, interface_name);
    method = interface_info == NULL ? NULL : find_method(interface_info, method_name);
    if (method == NULL) {
        g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
            G_DBUS_ERROR_UNKNOWN_METHOD, "The host does not provide this portal method");
        return;
    }
    if (g_str_equal(interface_name, DESKTOP_INTERFACE_PREFIX "FileChooser")) {
        file_chooser_call(portal, sender, method_name, parameters, invocation);
        return;
    }
    if (g_str_equal(interface_name, DESKTOP_INTERFACE_PREFIX "Screenshot")
        && g_str_equal(method_name, "Screenshot")) {
        screenshot_call(portal, sender, parameters, invocation);
        return;
    }
    if (g_str_equal(interface_name, DESKTOP_INTERFACE_PREFIX "OpenURI")
        && (g_str_equal(method_name, "OpenFile")
            || g_str_equal(method_name, "OpenDirectory"))) {
        open_file_call(portal, sender, method_name, parameters, invocation);
        return;
    }
    if (g_str_equal(interface_name,
            DESKTOP_INTERFACE_PREFIX "Wallpaper")) {
        transformed_request_call(portal, sender, interface_name, method_name,
            parameters, invocation);
        return;
    }
    if (g_str_equal(interface_name,
            DESKTOP_INTERFACE_PREFIX "Background")
        && g_str_equal(method_name, "RequestBackground")) {
        transformed_request_call(portal, sender, interface_name, method_name,
            parameters, invocation);
        return;
    }
    if (g_str_equal(interface_name, DESKTOP_INTERFACE_PREFIX "Secret")
        && g_str_equal(method_name, "RetrieveSecret")) {
        transformed_request_call(portal, sender, interface_name, method_name,
            parameters, invocation);
        return;
    }
    if (g_str_equal(interface_name,
            DESKTOP_INTERFACE_PREFIX "DynamicLauncher")) {
        if (g_str_equal(method_name, "PrepareInstall"))
            transformed_request_call(portal, sender, interface_name,
                method_name, parameters, invocation);
        else
            dynamic_call(portal, method_name, parameters, invocation);
        return;
    }
    if (g_str_equal(interface_name, DESKTOP_INTERFACE_PREFIX "GameMode")) {
        game_mode_call(portal, sender, method_name, parameters, invocation);
        return;
    }
    if (g_str_equal(interface_name, DESKTOP_INTERFACE_PREFIX "Realtime")
        && (g_str_equal(method_name, "MakeThreadRealtimeWithPID")
            || g_str_equal(method_name, "MakeThreadHighPriorityWithPID"))) {
        realtime_call(portal, method_name, parameters, invocation);
        return;
    }
    call = g_new0(ForwardCall, 1);
    call->portal = portal;
    call->invocation = g_object_ref(invocation);
    call->method = method;
    call->interface_name = g_strdup(interface_name);
    call->method_name = g_strdup(method_name);
    call->owner = g_strdup(sender);
    call->returns_request = method_returns_request(method);
    call->returns_session = method_returns_session(method);
    mapped = prepare_parameters(call, parameters);
    reply_type = method_output_type(method);
    fds = g_dbus_message_get_unix_fd_list(g_dbus_method_invocation_get_message(invocation));
    g_dbus_connection_call_with_unix_fd_list(portal->host, PORTAL_NAME,
        PORTAL_PATH, interface_name, method_name, mapped, reply_type,
        G_DBUS_CALL_FLAGS_NONE, -1, fds, NULL, forward_done, call);
    g_variant_type_free(reply_type);
    g_variant_unref(mapped);
}

static void clear_mirror_artwork(Mirror *mirror)
{
    const char *broker = g_getenv("SPACES_INTEGRATION_BROKER");
    guint index;
    if (mirror->artwork_uris == NULL) return;
    if (broker != NULL) {
        for (index = 0; index < mirror->artwork_uris->len; index++)
            g_dbus_connection_call(mirror->portal->host, broker,
                INTEGRATION_PATH, INTEGRATION_INTERFACE,
                "RemoveStagedFile", g_variant_new("(s)",
                    (const char *)g_ptr_array_index(
                        mirror->artwork_uris, index)),
                NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
    }
    g_ptr_array_set_size(mirror->artwork_uris, 0);
}

static GVariant *stage_artwork(Mirror *mirror, GVariant *value)
{
    const char *art_uri = NULL;
    const char *broker = g_getenv("SPACES_INTEGRATION_BROKER");
    if (broker != NULL
        && g_variant_is_of_type(value, G_VARIANT_TYPE_VARDICT)
        && g_variant_lookup(value, "mpris:artUrl", "&s", &art_uri)
        && g_str_has_prefix(art_uri, "file:")) {
        char *filename = g_filename_from_uri(art_uri, NULL, NULL);
        int descriptor = filename == NULL ? -1
            : open(filename, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        if (descriptor >= 0) {
            GUnixFDList *fds = g_unix_fd_list_new();
            gint handle = g_unix_fd_list_append(fds, descriptor, NULL);
            GError *error = NULL;
            GVariant *staged = handle < 0 ? NULL
                : g_dbus_connection_call_with_unix_fd_list_sync(
                    mirror->portal->host, broker, INTEGRATION_PATH,
                    INTEGRATION_INTERFACE,
                    "StageFile", g_variant_new("(sh)", filename, handle),
                    G_VARIANT_TYPE("(s)"), G_DBUS_CALL_FLAGS_NONE,
                    5000, fds, NULL, NULL, &error);
            if (staged != NULL) {
                const char *host_uri;
                GVariantDict dictionary;
                GVariant *updated;
                g_variant_get(staged, "(&s)", &host_uri);
                clear_mirror_artwork(mirror);
                g_ptr_array_add(mirror->artwork_uris,
                    g_strdup(host_uri));
                g_variant_dict_init(&dictionary, value);
                g_variant_dict_insert(&dictionary, "mpris:artUrl", "s",
                    host_uri);
                updated = g_variant_ref_sink(g_variant_dict_end(&dictionary));
                g_variant_unref(value); value = updated;
                g_variant_unref(staged);
            } else if (error != NULL) {
                g_warning("Could not stage MPRIS artwork: %s",
                    error->message);
            }
            g_clear_error(&error);
            g_object_unref(fds); close(descriptor);
        }
        g_free(filename);
    }
    return value;
}

static GVariant *portal_get_property(GDBusConnection *connection, const char *sender,
    const char *object_path, const char *interface_name, const char *property_name,
    GError **error, gpointer user_data)
{
    Portal *portal = user_data;
    GVariant *reply, *value;
    (void)connection; (void)sender; (void)object_path;
    if (g_str_equal(interface_name,
            DESKTOP_INTERFACE_PREFIX "FileChooser")
        && g_str_equal(property_name, "version")) {
        guint version = 4;
        if (portal->file_chooser_backend != NULL) {
            reply = g_dbus_connection_call_sync(portal->guest,
                portal->file_chooser_backend, PORTAL_PATH,
                "org.freedesktop.DBus.Properties", "Get",
                g_variant_new("(ss)",
                    "org.freedesktop.impl.portal.FileChooser", "version"),
                G_VARIANT_TYPE("(v)"), G_DBUS_CALL_FLAGS_NONE, 3000,
                NULL, NULL);
            if (reply != NULL) {
                g_variant_get(reply, "(v)", &value);
                if (g_variant_is_of_type(value, G_VARIANT_TYPE_UINT32))
                    version = MIN(version, g_variant_get_uint32(value));
                g_variant_unref(value); g_variant_unref(reply);
            }
        }
        return g_variant_ref_sink(g_variant_new_uint32(version));
    }
    reply = g_dbus_connection_call_sync(portal->host, PORTAL_NAME, PORTAL_PATH,
        "org.freedesktop.DBus.Properties", "Get",
        g_variant_new("(ss)", interface_name, property_name), G_VARIANT_TYPE("(v)"),
        G_DBUS_CALL_FLAGS_NONE, 5000, NULL, error);
    if (reply == NULL) return NULL;
    g_variant_get(reply, "(v)", &value);
    g_variant_unref(reply);
    return value;
}

static const GDBusInterfaceVTable portal_vtable = {
    .method_call = portal_method_call,
    .get_property = portal_get_property,
};

static GVariant *map_response(Portal *portal, Request *request, GVariant *parameters)
{
    guint response;
    GVariant *results, *session_value;
    GVariantDict dict;
    g_variant_get(parameters, "(u@a{sv})", &response, &results);
    session_value = g_variant_lookup_value(results, "session_handle", NULL);
    if (session_value != NULL && request->guest_session_path != NULL
        && response == 0
        && (g_variant_is_of_type(session_value, G_VARIANT_TYPE_STRING)
            || g_variant_is_of_type(session_value,
                G_VARIANT_TYPE_OBJECT_PATH))) {
        const char *host_session = g_variant_get_string(session_value, NULL);
        if (g_variant_is_object_path(host_session)
            && register_session(portal, request->owner,
                request->guest_session_path, host_session)) {
            g_variant_dict_init(&dict, results);
            g_variant_dict_insert(&dict, "session_handle",
                g_variant_is_of_type(session_value,
                    G_VARIANT_TYPE_OBJECT_PATH) ? "o" : "s",
                request->guest_session_path);
            g_variant_unref(results);
            results = g_variant_ref_sink(g_variant_dict_end(&dict));
        }
    }
    g_clear_pointer(&session_value, g_variant_unref);
    GVariant *mapped = translate_object_paths(portal, results, FALSE);
    g_variant_unref(results);
    return g_variant_ref_sink(g_variant_new("(u@a{sv})", response, mapped));
}

static void host_portal_signal(GDBusConnection *connection, const char *sender,
    const char *object_path, const char *interface_name, const char *signal_name,
    GVariant *parameters, gpointer user_data)
{
    Portal *portal = user_data;
    Request *request = g_hash_table_lookup(portal->requests_host, object_path);
    Session *session = g_hash_table_lookup(portal->sessions_host, object_path);
    GVariant *mapped;
    const char *destination = NULL;
    const char *guest_path = object_path;
    (void)connection; (void)sender;
    if (request != NULL) {
        destination = request->owner;
        guest_path = request->guest_path;
    } else if (session != NULL) {
        destination = session->owner;
        guest_path = session->guest_path;
    } else if (!g_str_equal(object_path, PORTAL_PATH)) {
        return;
    }
    if (request != NULL && request->cancelled
        && g_str_equal(interface_name, REQUEST_INTERFACE)
        && g_str_equal(signal_name, "Response")) {
        g_hash_table_remove(portal->requests_host, request->host_path);
        g_hash_table_remove(portal->requests_guest, request->guest_path);
        return;
    }
    if (request != NULL && g_str_equal(interface_name, REQUEST_INTERFACE)
        && g_str_equal(signal_name, "Response"))
        mapped = map_response(portal, request, parameters);
    else
        mapped = translate_object_paths(portal, parameters, FALSE);
    g_dbus_connection_emit_signal(portal->guest, destination, guest_path,
        interface_name, signal_name, mapped, NULL);
    g_variant_unref(mapped);
    if (request != NULL && !request->persistent
        && g_str_equal(interface_name, REQUEST_INTERFACE)
        && g_str_equal(signal_name, "Response")) {
        g_hash_table_remove(portal->requests_host, request->host_path);
        g_hash_table_remove(portal->requests_guest, request->guest_path);
    } else if (session != NULL && g_str_equal(interface_name, SESSION_INTERFACE)
        && g_str_equal(signal_name, "Closed")) {
        g_hash_table_remove(portal->sessions_host, session->host_path);
        g_hash_table_remove(portal->sessions_guest, session->guest_path);
    }
}

static void bridge_done(GObject *source, GAsyncResult *result, gpointer user_data)
{
    GDBusMethodInvocation *invocation = user_data;
    GUnixFDList *fds = NULL;
    GError *error = NULL;
    GVariant *reply = g_dbus_connection_call_with_unix_fd_list_finish(
        G_DBUS_CONNECTION(source), &fds, result, &error);
    if (reply == NULL) {
        g_dbus_method_invocation_return_gerror(invocation, error);
        g_clear_error(&error);
    } else if (fds != NULL) {
        g_dbus_method_invocation_return_value_with_unix_fd_list(invocation, reply, fds);
        g_variant_unref(reply);
    } else {
        g_dbus_method_invocation_return_value(invocation, reply);
        g_variant_unref(reply);
    }
    g_clear_object(&fds);
    g_object_unref(invocation);
}

static char *resolve_host_file_uri(Portal *portal, const char *uri,
    GError **error)
{
    const char *broker = g_getenv("SPACES_INTEGRATION_BROKER");
    char *filename;
    char *host_uri = NULL;
    GUnixFDList *fds;
    GVariant *reply;
    int descriptor;
    gint handle;

    if (broker == NULL || *broker == '\0') {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_NOT_CONNECTED,
            "The Spaces integration broker is unavailable");
        return NULL;
    }
    filename = g_filename_from_uri(uri, NULL, error);
    if (filename == NULL) return NULL;
    descriptor = open(filename, O_PATH | O_CLOEXEC);
    if (descriptor < 0) {
        g_set_error(error, G_IO_ERROR, g_io_error_from_errno(errno),
            "Could not open the guest notification path: %s",
            g_strerror(errno));
        g_free(filename);
        return NULL;
    }
    fds = g_unix_fd_list_new();
    handle = g_unix_fd_list_append(fds, descriptor, error);
    reply = handle < 0 ? NULL
        : g_dbus_connection_call_with_unix_fd_list_sync(
            portal->host, broker, INTEGRATION_PATH, INTEGRATION_INTERFACE,
            "ResolvePath", g_variant_new("(sh)", filename, handle),
            G_VARIANT_TYPE("(s)"), G_DBUS_CALL_FLAGS_NONE,
            5000, fds, NULL, NULL, error);
    if (reply != NULL) {
        const char *value;
        g_variant_get(reply, "(&s)", &value);
        host_uri = g_strdup(value);
        g_variant_unref(reply);
    }
    g_object_unref(fds);
    close(descriptor);
    g_free(filename);
    return host_uri;
}

static GVariant *rewrite_notification_paths(Portal *portal,
    GVariant *parameters)
{
    const char *app_name, *app_icon, *summary, *body;
    guint replaces;
    gint timeout;
    GVariant *actions, *hints, *urls;
    GVariantIter iterator;
    GVariantBuilder mapped_urls;
    GVariantDict dictionary;
    const char *uri;
    gboolean changed = FALSE;

    g_variant_get(parameters, "(&su&s&s&s@as@a{sv}i)", &app_name,
        &replaces, &app_icon, &summary, &body, &actions, &hints, &timeout);
    urls = g_variant_lookup_value(hints, "x-kde-urls",
        G_VARIANT_TYPE_STRING_ARRAY);
    if (urls == NULL) {
        g_variant_unref(actions);
        g_variant_unref(hints);
        return g_variant_ref(parameters);
    }
    g_variant_builder_init(&mapped_urls, G_VARIANT_TYPE_STRING_ARRAY);
    g_variant_iter_init(&iterator, urls);
    while (g_variant_iter_next(&iterator, "&s", &uri)) {
        char *host_uri = NULL;
        if (g_str_has_prefix(uri, "file:")) {
            GError *error = NULL;
            changed = TRUE;
            host_uri = resolve_host_file_uri(portal, uri, &error);
            if (host_uri == NULL) {
                g_warning("Could not map notification file URI: %s",
                    error == NULL ? "unknown error" : error->message);
            }
            g_clear_error(&error);
        }
        if (host_uri != NULL || !g_str_has_prefix(uri, "file:"))
            g_variant_builder_add(&mapped_urls, "s",
                host_uri == NULL ? uri : host_uri);
        g_free(host_uri);
    }
    g_variant_unref(urls);
    if (!changed) {
        g_variant_builder_clear(&mapped_urls);
        g_variant_unref(actions);
        g_variant_unref(hints);
        return g_variant_ref(parameters);
    }
    g_variant_dict_init(&dictionary, hints);
    g_variant_dict_insert_value(&dictionary, "x-kde-urls",
        g_variant_builder_end(&mapped_urls));
    g_variant_unref(hints);
    return g_variant_ref_sink(g_variant_new("(susss@as@a{sv}i)",
        app_name, replaces, app_icon, summary, body, actions,
        g_variant_dict_end(&dictionary), timeout));
}

typedef struct {
    Portal *portal;
    GDBusMethodInvocation *invocation;
    char *owner;
    char *key;
    gboolean add;
    gboolean throttle;
} ScreenCall;

static void screen_call_free(ScreenCall *call)
{
    g_object_unref(call->invocation);
    g_free(call->owner); g_free(call->key); g_free(call);
}

static void screen_call_done(GObject *source, GAsyncResult *result,
    gpointer user_data)
{
    ScreenCall *call = user_data;
    GError *error = NULL;
    GVariant *reply = g_dbus_connection_call_finish(
        G_DBUS_CONNECTION(source), result, &error);
    if (reply == NULL) {
        g_dbus_method_invocation_return_gerror(call->invocation, error);
        g_clear_error(&error); screen_call_free(call); return;
    }
    if (call->add) {
        guint cookie;
        Inhibitor *inhibitor = g_new0(Inhibitor, 1);
        g_variant_get(reply, "(u)", &cookie);
        inhibitor->owner = g_strdup(call->owner);
        inhibitor->cookie = cookie;
        inhibitor->throttle = call->throttle;
        g_hash_table_replace(call->portal->inhibitors,
            inhibitor_key(call->throttle, cookie), inhibitor);
    } else if (call->key != NULL) {
        g_hash_table_remove(call->portal->inhibitors, call->key);
    }
    g_dbus_method_invocation_return_value(call->invocation, reply);
    g_variant_unref(reply); screen_call_free(call);
}

static void simple_bridge_call(GDBusConnection *connection, const char *sender,
    const char *object_path, const char *interface_name, const char *method_name,
    GVariant *parameters, GDBusMethodInvocation *invocation, gpointer user_data)
{
    Portal *portal = user_data;
    const char *host_path = object_path;
    const char *host_name = interface_name;
    GDBusInterfaceInfo *info = find_registered_interface(portal, interface_name);
    GDBusMethodInfo *method = info == NULL ? NULL : find_method(info, method_name);
    GVariantType *reply_type;
    GVariant *mapped = NULL;
    GUnixFDList *fds;
    (void)connection; (void)sender;
    if (g_str_equal(interface_name, SCREEN_SAVER_NAME)) {
        host_path = portal->screen_saver_host_path;
        if (host_path == NULL) {
            g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
                G_DBUS_ERROR_NAME_HAS_NO_OWNER,
                "The host ScreenSaver service is unavailable");
            return;
        }
        if (g_str_equal(method_name, "Inhibit")
            || g_str_equal(method_name, "Throttle")) {
            ScreenCall *call = g_new0(ScreenCall, 1);
            call->portal = portal; call->invocation = g_object_ref(invocation);
            call->owner = g_strdup(sender); call->add = TRUE;
            call->throttle = g_str_equal(method_name, "Throttle");
            g_dbus_connection_call(portal->host, SCREEN_SAVER_NAME, host_path,
                SCREEN_SAVER_NAME, method_name, parameters,
                G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, -1, NULL,
                screen_call_done, call);
            return;
        }
        if (g_str_equal(method_name, "UnInhibit")
            || g_str_equal(method_name, "UnThrottle")) {
            guint cookie; gboolean throttle = g_str_equal(method_name, "UnThrottle");
            char *key; Inhibitor *inhibitor; ScreenCall *call;
            g_variant_get(parameters, "(u)", &cookie);
            key = inhibitor_key(throttle, cookie);
            inhibitor = g_hash_table_lookup(portal->inhibitors, key);
            if (inhibitor == NULL || !g_str_equal(inhibitor->owner, sender)) {
                g_free(key);
                g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
                    inhibitor == NULL ? G_DBUS_ERROR_INVALID_ARGS
                                      : G_DBUS_ERROR_ACCESS_DENIED,
                    inhibitor == NULL ? "Unknown ScreenSaver inhibitor cookie"
                                      : "The inhibitor belongs to another caller");
                return;
            }
            call = g_new0(ScreenCall, 1); call->portal = portal;
            call->invocation = g_object_ref(invocation); call->owner = g_strdup(sender);
            call->key = key;
            g_dbus_connection_call(portal->host, SCREEN_SAVER_NAME, host_path,
                SCREEN_SAVER_NAME, method_name, parameters,
                G_VARIANT_TYPE_UNIT, G_DBUS_CALL_FLAGS_NONE, -1, NULL,
                screen_call_done, call);
            return;
        }
    } else if (g_str_equal(interface_name, NOTIFICATIONS_NAME)) {
        host_path = NOTIFICATIONS_PATH;
    } else if (g_str_equal(interface_name, POWER_NAME)) {
        static const char *readonly[] = { "CanHibernate", "CanHybridSuspend", "CanSuspend",
            "CanSuspendThenHibernate", "GetPowerSaveStatus" };
        gboolean allowed = FALSE;
        guint index;
        for (index = 0; index < G_N_ELEMENTS(readonly); index++)
            if (g_str_equal(method_name, readonly[index])) allowed = TRUE;
        if (!allowed) {
            g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
                G_DBUS_ERROR_ACCESS_DENIED, "Host power actions are not exposed to Spaces");
            return;
        }
        host_path = POWER_PATH;
    }
    if (method == NULL) {
        g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
            G_DBUS_ERROR_UNKNOWN_METHOD, "Unknown desktop integration method");
        return;
    }
    if (g_str_equal(interface_name, NOTIFICATIONS_NAME)
        && g_str_equal(method_name, "Notify"))
        mapped = rewrite_notification_paths(portal, parameters);
    reply_type = method_output_type(method);
    fds = g_dbus_message_get_unix_fd_list(g_dbus_method_invocation_get_message(invocation));
    g_dbus_connection_call_with_unix_fd_list(portal->host, host_name, host_path,
        interface_name, method_name, mapped == NULL ? parameters : mapped,
        reply_type, G_DBUS_CALL_FLAGS_NONE,
        -1, fds, NULL, bridge_done, g_object_ref(invocation));
    g_variant_type_free(reply_type);
    g_clear_pointer(&mapped, g_variant_unref);
}

static const GDBusInterfaceVTable simple_bridge_vtable = { .method_call = simple_bridge_call };

typedef struct { GDBusMethodInvocation *invocation; guint pending; GError *error; } FileManagerCall;
static void finish_file_manager(FileManagerCall *call)
{
    if (call->pending != 0) return;
    if (call->error != NULL) g_dbus_method_invocation_return_gerror(call->invocation, call->error);
    else g_dbus_method_invocation_return_value(call->invocation, NULL);
    g_clear_error(&call->error); g_object_unref(call->invocation); g_free(call);
}
static void file_manager_done(GObject *source, GAsyncResult *result, gpointer user_data)
{
    FileManagerCall *call = user_data; GError *error = NULL;
    if (!g_subprocess_wait_check_finish(G_SUBPROCESS(source), result, &error)) {
        if (call->error == NULL) call->error = error; else g_clear_error(&error);
    }
    call->pending--; finish_file_manager(call);
}
static void file_manager_call(GDBusConnection *connection, const char *sender,
    const char *object_path, const char *interface_name, const char *method_name,
    GVariant *parameters, GDBusMethodInvocation *invocation, gpointer user_data)
{
    GVariant *uris; GVariantIter iterator; const char *uri; const char *startup;
    const char *mode; GError *error = NULL; GSubprocessLauncher *launcher;
    FileManagerCall *call;
    (void)connection; (void)sender; (void)object_path; (void)interface_name; (void)user_data;
    if (g_str_equal(method_name, "ShowItemProperties")) {
        g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
            G_DBUS_ERROR_NOT_SUPPORTED, "File property dialogs are not supported"); return;
    }
    mode = g_str_equal(method_name, "ShowFolders") ? "--folder" : "--directory";
    g_variant_get_child(parameters, 1, "&s", &startup);
    launcher = g_subprocess_launcher_new(G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE);
    if (*startup != '\0') g_subprocess_launcher_setenv(launcher, "XDG_ACTIVATION_TOKEN", startup, TRUE);
    call = g_new0(FileManagerCall, 1); call->invocation = g_object_ref(invocation);
    uris = g_variant_get_child_value(parameters, 0); g_variant_iter_init(&iterator, uris);
    while (g_variant_iter_next(&iterator, "&s", &uri)) {
        const char *args[] = { "/run/spaces-host/bin/spaces-open", mode, uri, NULL };
        GSubprocess *process = g_subprocess_launcher_spawnv(launcher, args, &error);
        if (process == NULL) { if (call->error == NULL) call->error = error; else g_clear_error(&error); continue; }
        call->pending++; g_subprocess_wait_check_async(process, NULL, file_manager_done, call); g_object_unref(process);
    }
    g_variant_unref(uris); g_object_unref(launcher); finish_file_manager(call);
}
static const GDBusInterfaceVTable file_manager_vtable = { .method_call = file_manager_call };

static void native_host_signal(GDBusConnection *connection, const char *sender,
    const char *path, const char *interface, const char *signal_name,
    GVariant *parameters, gpointer user_data)
{
    Portal *portal = user_data; const char *guest_path = path;
    (void)connection; (void)sender;
    if (g_str_equal(interface, SCREEN_SAVER_NAME)) guest_path = SCREEN_SAVER_PATH;
    g_dbus_connection_emit_signal(portal->guest, NULL, guest_path, interface,
        signal_name, parameters, NULL);
    if (g_str_equal(interface, SCREEN_SAVER_NAME))
        g_dbus_connection_emit_signal(portal->guest, NULL, SCREEN_SAVER_LEGACY_PATH,
            interface, signal_name, parameters, NULL);
}

typedef struct {
    GDBusMethodInvocation *invocation;
} MirrorCall;

static GDBusInterfaceInfo *mirror_interface(Mirror *mirror, const char *name)
{
    guint index;
    for (index = 0; index < mirror->nodes->len; index++) {
        GDBusNodeInfo *node = g_ptr_array_index(mirror->nodes, index);
        GDBusInterfaceInfo *info = g_dbus_node_info_lookup_interface(node, name);
        if (info != NULL) return info;
    }
    return NULL;
}

static void mirror_call_done(GObject *source, GAsyncResult *result, gpointer user_data)
{
    MirrorCall *call = user_data;
    GUnixFDList *fds = NULL;
    GError *error = NULL;
    GVariant *reply = g_dbus_connection_call_with_unix_fd_list_finish(
        G_DBUS_CONNECTION(source), &fds, result, &error);
    if (reply == NULL) {
        g_dbus_method_invocation_return_gerror(call->invocation, error);
        g_clear_error(&error);
    } else if (fds != NULL) {
        g_dbus_method_invocation_return_value_with_unix_fd_list(call->invocation, reply, fds);
        g_variant_unref(reply);
    } else {
        g_dbus_method_invocation_return_value(call->invocation, reply);
        g_variant_unref(reply);
    }
    g_clear_object(&fds);
    g_object_unref(call->invocation);
    g_free(call);
}

static void mirror_method_call(GDBusConnection *connection, const char *sender,
    const char *object_path, const char *interface_name, const char *method_name,
    GVariant *parameters, GDBusMethodInvocation *invocation, gpointer user_data)
{
    Mirror *mirror = user_data;
    GDBusInterfaceInfo *info = mirror_interface(mirror, interface_name);
    GDBusMethodInfo *method = info == NULL ? NULL : find_method(info, method_name);
    GVariantType *reply_type;
    GUnixFDList *fds;
    MirrorCall *call;
    (void)connection; (void)sender;
    if (method == NULL) {
        g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
            G_DBUS_ERROR_UNKNOWN_METHOD, "The guest object does not provide this method");
        return;
    }
    reply_type = method_output_type(method);
    fds = g_dbus_message_get_unix_fd_list(g_dbus_method_invocation_get_message(invocation));
    call = g_new0(MirrorCall, 1);
    call->invocation = g_object_ref(invocation);
    g_dbus_connection_call_with_unix_fd_list(mirror->portal->guest,
        mirror->guest_name, object_path, interface_name, method_name, parameters,
        reply_type, G_DBUS_CALL_FLAGS_NONE, -1, fds, NULL, mirror_call_done, call);
    g_variant_type_free(reply_type);
}

static GVariant *mirror_get_property(GDBusConnection *connection,
    const char *sender, const char *object_path, const char *interface_name,
    const char *property_name, GError **error, gpointer user_data)
{
    Mirror *mirror = user_data;
    GVariant *reply, *value;
    (void)connection; (void)sender;
    reply = g_dbus_connection_call_sync(mirror->portal->guest, mirror->guest_name,
        object_path, "org.freedesktop.DBus.Properties", "Get",
        g_variant_new("(ss)", interface_name, property_name), G_VARIANT_TYPE("(v)"),
        G_DBUS_CALL_FLAGS_NONE, 5000, NULL, error);
    if (reply == NULL) return NULL;
    g_variant_get(reply, "(v)", &value);
    g_variant_unref(reply);
    if (g_str_equal(interface_name, "org.mpris.MediaPlayer2.Player")
        && g_str_equal(property_name, "Metadata"))
        value = stage_artwork(mirror, value);
    return value;
}

static gboolean mirror_set_property(GDBusConnection *connection,
    const char *sender, const char *object_path, const char *interface_name,
    const char *property_name, GVariant *value, GError **error, gpointer user_data)
{
    Mirror *mirror = user_data;
    GVariant *reply;
    (void)connection; (void)sender;
    reply = g_dbus_connection_call_sync(mirror->portal->guest, mirror->guest_name,
        object_path, "org.freedesktop.DBus.Properties", "Set",
        g_variant_new("(ssv)", interface_name, property_name, value),
        G_VARIANT_TYPE_UNIT, G_DBUS_CALL_FLAGS_NONE, 5000, NULL, error);
    if (reply == NULL) return FALSE;
    g_variant_unref(reply);
    return TRUE;
}

static const GDBusInterfaceVTable mirror_vtable = {
    .method_call = mirror_method_call,
    .get_property = mirror_get_property,
    .set_property = mirror_set_property,
};

static void mirror_guest_signal(GDBusConnection *connection, const char *sender,
    const char *object_path, const char *interface_name, const char *signal_name,
    GVariant *parameters, gpointer user_data)
{
    Mirror *mirror = user_data;
    GVariant *forwarded = parameters;
    guint index;
    gboolean known_path = FALSE;
    (void)connection;
    if (!g_str_equal(sender, mirror->guest_owner)) return;
    for (index = 0; index < mirror->paths->len; index++) {
        if (g_str_equal(object_path,
                g_ptr_array_index(mirror->paths, index))) {
            known_path = TRUE;
            break;
        }
    }
    if (!known_path) return;
    if (g_str_equal(interface_name, "org.freedesktop.DBus.Properties")
        && g_str_equal(signal_name, "PropertiesChanged")) {
        const char *changed_interface;
        GVariant *changed;
        GVariant *invalidated;
        g_variant_get(parameters, "(&s@a{sv}@as)", &changed_interface,
            &changed, &invalidated);
        if (g_str_equal(changed_interface, "org.mpris.MediaPlayer2.Player")) {
            GVariant *metadata = g_variant_lookup_value(changed, "Metadata",
                G_VARIANT_TYPE_VARDICT);
            if (metadata != NULL) {
                GVariantDict dictionary;
                GVariant *staged = stage_artwork(mirror, metadata);
                g_variant_dict_init(&dictionary, changed);
                g_variant_dict_insert_value(&dictionary, "Metadata", staged);
                g_variant_unref(staged);
                g_variant_unref(changed);
                changed = g_variant_ref_sink(g_variant_dict_end(&dictionary));
            }
        }
        forwarded = g_variant_ref_sink(g_variant_new("(s@a{sv}@as)",
            changed_interface, changed, invalidated));
    }
    g_dbus_connection_emit_signal(mirror->portal->host, NULL, object_path,
        interface_name, signal_name, forwarded, NULL);
    if (forwarded != parameters) g_variant_unref(forwarded);
}

static gboolean mirror_add_path(Mirror *mirror, const char *path, GError **error)
{
    GVariant *reply;
    const char *xml;
    GDBusNodeInfo *node;
    guint index;
    guint registrations_before = mirror->registrations->len;
    reply = g_dbus_connection_call_sync(mirror->portal->guest,
        mirror->guest_name, path, "org.freedesktop.DBus.Introspectable",
        "Introspect", NULL, G_VARIANT_TYPE("(s)"), G_DBUS_CALL_FLAGS_NONE,
        5000, NULL, error);
    if (reply == NULL) return FALSE;
    g_variant_get(reply, "(&s)", &xml);
    node = g_dbus_node_info_new_for_xml(xml, error);
    g_variant_unref(reply);
    if (node == NULL) return FALSE;
    if (!mirror->status_item && g_str_equal(path, MPRIS_PATH)) {
        gboolean mirrorable = FALSE;
        for (index = 0; node->interfaces[index] != NULL; index++) {
            if (!g_str_has_prefix(node->interfaces[index]->name,
                    "org.freedesktop.DBus.")) {
                mirrorable = TRUE;
                break;
            }
        }
        if (!mirrorable) {
            g_dbus_node_info_unref(node);
            node = g_dbus_node_info_new_for_xml(mpris_fallback_xml, error);
            if (node == NULL) return FALSE;
        }
    }
    g_ptr_array_add(mirror->nodes, node);
    g_ptr_array_add(mirror->paths, g_strdup(path));
    for (index = 0; node->interfaces[index] != NULL; index++) {
        GDBusInterfaceInfo *info = node->interfaces[index];
        guint registration;
        if (g_str_has_prefix(info->name, "org.freedesktop.DBus.")) continue;
        registration = g_dbus_connection_register_object(mirror->portal->host,
            path, info, &mirror_vtable, mirror, NULL, error);
        if (registration == 0) return FALSE;
        g_array_append_val(mirror->registrations, registration);
    }
    if (mirror->registrations->len == registrations_before) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_NOT_SUPPORTED,
            "The guest object does not export a mirrorable interface");
        return FALSE;
    }
    return TRUE;
}

static void mirror_name_acquired(GDBusConnection *connection, const char *name,
    gpointer user_data)
{
    Mirror *mirror = user_data;
    (void)connection; (void)name;
    if (mirror->status_item)
        g_dbus_connection_call(mirror->portal->host, STATUS_WATCHER_NAME,
            STATUS_WATCHER_PATH, STATUS_WATCHER_INTERFACE,
            "RegisterStatusNotifierItem", g_variant_new("(s)", mirror->host_name),
            NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
}

static void mirror_name_lost(GDBusConnection *connection, const char *name,
    gpointer user_data)
{
    Mirror *mirror = user_data;
    (void)connection;
    g_warning("Could not publish guest integration object as %s", name);
    if (mirror->portal->mirrors != NULL)
        g_hash_table_remove(mirror->portal->mirrors, mirror->key);
}

static void mirror_free(gpointer data)
{
    Mirror *mirror = data;
    guint index;
    clear_mirror_artwork(mirror);
    if (mirror->signal_subscription != 0)
        g_dbus_connection_signal_unsubscribe(mirror->portal->guest,
            mirror->signal_subscription);
    if (mirror->host_owner != 0) g_bus_unown_name(mirror->host_owner);
    for (index = 0; index < mirror->registrations->len; index++)
        g_dbus_connection_unregister_object(mirror->portal->host,
            g_array_index(mirror->registrations, guint, index));
    g_array_unref(mirror->registrations);
    g_ptr_array_unref(mirror->nodes);
    g_ptr_array_unref(mirror->paths);
    g_ptr_array_unref(mirror->artwork_uris);
    g_free(mirror->host_name); g_free(mirror->guest_path);
    g_free(mirror->guest_owner); g_free(mirror->guest_name); g_free(mirror->key);
    g_free(mirror);
}

static char *mirror_app_component(const char *value)
{
    GString *component = g_string_sized_new(32);
    const unsigned char *cursor;
    gboolean separator = FALSE;
    for (cursor = (const unsigned char *)value;
         *cursor != '\0' && component->len < 63; cursor++) {
        if (g_ascii_isalnum(*cursor) || *cursor == '_' || *cursor == '-') {
            g_string_append_c(component, g_ascii_tolower(*cursor));
            separator = FALSE;
        } else if (!separator && component->len != 0) {
            g_string_append_c(component, '-');
            separator = TRUE;
        }
    }
    while (component->len != 0
        && component->str[component->len - 1] == '-')
        g_string_truncate(component, component->len - 1);
    if (component->len == 0) g_string_append(component, "app");
    return g_string_free(component, FALSE);
}

static char *status_item_app(Mirror *mirror)
{
    GVariant *reply = g_dbus_connection_call_sync(mirror->portal->guest,
        mirror->guest_name, mirror->guest_path,
        "org.freedesktop.DBus.Properties", "Get",
        g_variant_new("(ss)", STATUS_ITEM_INTERFACE, "Id"),
        G_VARIANT_TYPE("(v)"), G_DBUS_CALL_FLAGS_NONE, 3000, NULL, NULL);
    char *app = NULL;
    if (reply != NULL) {
        GVariant *value;
        g_variant_get(reply, "(v)", &value);
        if (g_variant_is_of_type(value, G_VARIANT_TYPE_STRING))
            app = mirror_app_component(g_variant_get_string(value, NULL));
        g_variant_unref(value);
        g_variant_unref(reply);
    }
    if (app == NULL) {
        const char *last = strrchr(mirror->guest_name, '.');
        app = mirror_app_component(last == NULL
            ? mirror->guest_name : last + 1);
    }
    return app;
}

static char *mpris_app(const char *guest_name)
{
    const char *start = g_str_has_prefix(guest_name, MPRIS_PREFIX)
        ? guest_name + strlen(MPRIS_PREFIX) : guest_name;
    const char *end = strchr(start, '.');
    char *value = end == NULL ? g_strdup(start) : g_strndup(start, end - start);
    char *app = mirror_app_component(value);
    g_free(value);
    return app;
}

static char *mirror_host_name(Portal *portal, const char *prefix,
    const char *app, gboolean status_item)
{
    char *space_app = g_strdup_printf("%s-%s", portal->space_name, app);
    char *name = g_strdup_printf("%s.spaces.space.%s%s.%s.i%u", prefix,
        g_ascii_isdigit(space_app[0]) ? "s" : "", space_app,
        status_item ? "item" : "player", ++portal->mirror_serial);
    g_free(space_app);
    return name;
}

static gboolean name_owner(Portal *portal, const char *name, char **owner,
    GError **error)
{
    GVariant *reply = g_dbus_connection_call_sync(portal->guest, DBUS_NAME,
        DBUS_PATH, DBUS_NAME, "GetNameOwner", g_variant_new("(s)", name),
        G_VARIANT_TYPE("(s)"), G_DBUS_CALL_FLAGS_NONE, 5000, NULL, error);
    const char *value;
    if (reply == NULL) return FALSE;
    g_variant_get(reply, "(&s)", &value);
    *owner = g_strdup(value);
    g_variant_unref(reply);
    return TRUE;
}

static gboolean connection_process(Portal *portal, const char *name,
    guint32 *process, GError **error)
{
    GVariant *reply = g_dbus_connection_call_sync(portal->guest, DBUS_NAME,
        DBUS_PATH, DBUS_NAME, "GetConnectionUnixProcessID",
        g_variant_new("(s)", name), G_VARIANT_TYPE("(u)"),
        G_DBUS_CALL_FLAGS_NONE, 5000, NULL, error);
    if (reply == NULL) return FALSE;
    g_variant_get(reply, "(u)", process);
    g_variant_unref(reply);
    return TRUE;
}

static gboolean same_connection_process(Portal *portal, const char *first,
    const char *second, GError **error)
{
    guint32 first_process, second_process;
    if (g_str_equal(first, second)) return TRUE;
    return connection_process(portal, first, &first_process, error)
        && connection_process(portal, second, &second_process, error)
        && first_process == second_process;
}

static gboolean create_mirror(Portal *portal, const char *key,
    const char *guest_name, const char *guest_path, gboolean status_item,
    GError **error)
{
    Mirror *mirror;
    char *app;
    GVariant *menu_reply = NULL, *menu_value = NULL;
    const char *menu_path = NULL;
    if (g_hash_table_contains(portal->mirrors, key)) return TRUE;
    mirror = g_new0(Mirror, 1);
    mirror->portal = portal;
    mirror->key = g_strdup(key);
    mirror->guest_name = g_strdup(guest_name);
    mirror->guest_path = g_strdup(guest_path);
    mirror->status_item = status_item;
    mirror->nodes = g_ptr_array_new_with_free_func((GDestroyNotify)g_dbus_node_info_unref);
    mirror->paths = g_ptr_array_new_with_free_func(g_free);
    mirror->artwork_uris = g_ptr_array_new_with_free_func(g_free);
    mirror->registrations = g_array_new(FALSE, FALSE, sizeof(guint));
    if (!name_owner(portal, guest_name, &mirror->guest_owner, error)) {
        mirror_free(mirror); return FALSE;
    }
    app = status_item ? status_item_app(mirror) : mpris_app(guest_name);
    mirror->host_name = mirror_host_name(portal,
        status_item ? STATUS_ITEM_INTERFACE : "org.mpris.MediaPlayer2",
        app, status_item);
    g_free(app);
    if (!mirror_add_path(mirror, guest_path, error)) {
        mirror_free(mirror); return FALSE;
    }
    if (status_item) {
        GError *menu_error = NULL;
        menu_reply = g_dbus_connection_call_sync(portal->guest, guest_name,
            guest_path, "org.freedesktop.DBus.Properties", "Get",
            g_variant_new("(ss)", STATUS_ITEM_INTERFACE, "Menu"),
            G_VARIANT_TYPE("(v)"), G_DBUS_CALL_FLAGS_NONE, 3000, NULL, NULL);
        if (menu_reply != NULL) {
            g_variant_get(menu_reply, "(v)", &menu_value);
            if (g_variant_is_of_type(menu_value, G_VARIANT_TYPE_OBJECT_PATH))
                menu_path = g_variant_get_string(menu_value, NULL);
            if (menu_path != NULL && !g_str_equal(menu_path, "/")
                && !mirror_add_path(mirror, menu_path, &menu_error)) {
                g_warning("Could not mirror status notifier menu %s: %s",
                    menu_path, menu_error->message);
                g_clear_error(&menu_error);
            }
            g_variant_unref(menu_value); g_variant_unref(menu_reply);
        }
    }
    mirror->signal_subscription = g_dbus_connection_signal_subscribe(
        portal->guest, mirror->guest_owner, NULL, NULL, NULL, NULL,
        G_DBUS_SIGNAL_FLAGS_NONE, mirror_guest_signal, mirror, NULL);
    g_hash_table_insert(portal->mirrors, g_strdup(key), mirror);
    mirror->host_owner = g_bus_own_name_on_connection(portal->host,
        mirror->host_name, G_BUS_NAME_OWNER_FLAGS_NONE, mirror_name_acquired,
        mirror_name_lost, mirror, NULL);
    return TRUE;
}

static void status_watcher_call(GDBusConnection *connection, const char *sender,
    const char *object_path, const char *interface_name, const char *method_name,
    GVariant *parameters, GDBusMethodInvocation *invocation, gpointer user_data)
{
    Portal *portal = user_data;
    const char *argument;
    char *service = NULL, *owner = NULL, *key = NULL;
    const char *path;
    GError *error = NULL;
    (void)connection; (void)object_path; (void)interface_name;
    g_variant_get(parameters, "(&s)", &argument);
    if (g_str_equal(method_name, "RegisterStatusNotifierHost")) {
        g_dbus_method_invocation_return_value(invocation, NULL); return;
    }
    if (!g_str_equal(method_name, "RegisterStatusNotifierItem")) {
        g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
            G_DBUS_ERROR_UNKNOWN_METHOD, "Unknown StatusNotifierWatcher method"); return;
    }
    if (g_variant_is_object_path(argument)) { service = g_strdup(sender); path = argument; }
    else { service = g_strdup(argument); path = "/StatusNotifierItem"; }
    if (!name_owner(portal, service, &owner, &error)
        || !same_connection_process(portal, owner, sender, &error)) {
        g_clear_error(&error); g_free(owner); g_free(service);
        g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
            G_DBUS_ERROR_ACCESS_DENIED,
            "The notifier item name belongs to another process"); return;
    }
    key = g_strdup_printf("sni:%s:%s", service, path);
    if (!create_mirror(portal, key, service, path, TRUE, &error)) {
        g_dbus_method_invocation_return_gerror(invocation, error); g_clear_error(&error);
    } else {
        g_dbus_method_invocation_return_value(invocation, NULL);
        g_dbus_connection_emit_signal(portal->guest, NULL, STATUS_WATCHER_PATH,
            STATUS_WATCHER_INTERFACE, "StatusNotifierItemRegistered",
            g_variant_new("(s)", service), NULL);
    }
    g_free(key); g_free(owner); g_free(service);
}

static GVariant *status_watcher_property(GDBusConnection *connection,
    const char *sender, const char *object_path, const char *interface_name,
    const char *property_name, GError **error, gpointer user_data)
{
    Portal *portal = user_data;
    (void)connection; (void)sender; (void)object_path; (void)interface_name; (void)error;
    if (g_str_equal(property_name, "IsStatusNotifierHostRegistered"))
        return g_variant_ref_sink(g_variant_new_boolean(TRUE));
    if (g_str_equal(property_name, "ProtocolVersion"))
        return g_variant_ref_sink(g_variant_new_int32(0));
    if (g_str_equal(property_name, "RegisteredStatusNotifierItems")) {
        GVariantBuilder items; GHashTableIter iterator; gpointer value;
        g_variant_builder_init(&items, G_VARIANT_TYPE_STRING_ARRAY);
        g_hash_table_iter_init(&iterator, portal->mirrors);
        while (g_hash_table_iter_next(&iterator, NULL, &value)) {
            Mirror *mirror = value;
            if (mirror->status_item) g_variant_builder_add(&items, "s", mirror->guest_name);
        }
        return g_variant_ref_sink(g_variant_builder_end(&items));
    }
    return NULL;
}

static const GDBusInterfaceVTable status_watcher_vtable = {
    .method_call = status_watcher_call,
    .get_property = status_watcher_property,
};

typedef struct {
    Portal *portal;
    char *name;
    guint remaining;
} MirrorRetry;

static gboolean retry_mpris(gpointer user_data)
{
    MirrorRetry *retry = user_data;
    GError *error = NULL;
    char *key = g_strconcat("mpris:", retry->name, NULL);
    gboolean created = create_mirror(retry->portal, key, retry->name,
        MPRIS_PATH, FALSE, &error);
    g_free(key);
    if (created) return G_SOURCE_REMOVE;
    if (--retry->remaining == 0) {
        g_warning("Could not mirror MPRIS player %s: %s", retry->name,
            error == NULL ? "unknown error" : error->message);
        g_clear_error(&error);
        return G_SOURCE_REMOVE;
    }
    g_clear_error(&error);
    return G_SOURCE_CONTINUE;
}

static void mirror_retry_free(gpointer data)
{
    MirrorRetry *retry = data;
    g_free(retry->name);
    g_free(retry);
}

static void maybe_add_mpris(Portal *portal, const char *name)
{
    MirrorRetry *retry;
    if (!g_str_has_prefix(name, MPRIS_PREFIX)) return;
    retry = g_new0(MirrorRetry, 1);
    retry->portal = portal;
    retry->name = g_strdup(name);
    retry->remaining = 20;
    g_timeout_add_full(G_PRIORITY_DEFAULT, 100, retry_mpris, retry,
        mirror_retry_free);
}

static void discover_mpris(Portal *portal)
{
    GVariant *reply = g_dbus_connection_call_sync(portal->guest, DBUS_NAME,
        DBUS_PATH, DBUS_NAME, "ListNames", NULL, G_VARIANT_TYPE("(as)"),
        G_DBUS_CALL_FLAGS_NONE, 5000, NULL, NULL);
    GVariantIter *names; const char *name;
    if (reply == NULL) return;
    g_variant_get(reply, "(as)", &names);
    while (g_variant_iter_next(names, "&s", &name)) maybe_add_mpris(portal, name);
    g_variant_iter_free(names); g_variant_unref(reply);
}

static char *file_chooser_preference(char **desktops)
{
    GPtrArray *directories = g_ptr_array_new_with_free_func(g_free);
    const char * const *system;
    guint index, directory_index;
    char *preference = NULL;
    g_ptr_array_add(directories, g_build_filename(g_get_user_config_dir(),
        "xdg-desktop-portal", NULL));
    for (system = g_get_system_config_dirs(); *system != NULL; system++)
        g_ptr_array_add(directories, g_build_filename(*system,
            "xdg-desktop-portal", NULL));
    g_ptr_array_add(directories,
        g_strdup("/usr/share/xdg-desktop-portal"));
    for (index = 0; desktops[index] != NULL && preference == NULL; index++) {
        char *desktop = g_ascii_strdown(desktops[index], -1);
        char *filename;
        if (g_str_equal(desktop, "spaces") || *desktop == '\0') {
            g_free(desktop); continue;
        }
        filename = g_strconcat(desktop, "-portals.conf", NULL);
        for (directory_index = 0; directory_index < directories->len;
             directory_index++) {
            char *path = g_build_filename(
                g_ptr_array_index(directories, directory_index), filename, NULL);
            GKeyFile *config = g_key_file_new();
            if (g_key_file_load_from_file(config, path, G_KEY_FILE_NONE, NULL))
                preference = g_key_file_get_string(config, "preferred",
                    "org.freedesktop.impl.portal.FileChooser", NULL);
            g_key_file_unref(config); g_free(path);
            if (preference != NULL) break;
        }
        g_free(filename); g_free(desktop);
    }
    if (preference == NULL) {
        for (directory_index = 0; directory_index < directories->len;
             directory_index++) {
            char *path = g_build_filename(
                g_ptr_array_index(directories, directory_index),
                "portals.conf", NULL);
            GKeyFile *config = g_key_file_new();
            if (g_key_file_load_from_file(config, path, G_KEY_FILE_NONE, NULL))
                preference = g_key_file_get_string(config, "preferred",
                    "org.freedesktop.impl.portal.FileChooser", NULL);
            g_key_file_unref(config); g_free(path);
            if (preference != NULL) break;
        }
    }
    g_ptr_array_unref(directories);
    if (preference != NULL) {
        char **choices = g_strsplit(preference, ";", 2);
        char *first = choices[0] != NULL && !g_str_equal(choices[0], "none")
            ? g_strdup(choices[0]) : NULL;
        g_strfreev(choices); g_free(preference); preference = first;
    }
    return preference;
}

static char *choose_file_chooser_backend(void)
{
    const char *directories[] = { "/usr/local/share/xdg-desktop-portal/portals",
        "/usr/share/xdg-desktop-portal/portals" };
    const char *desktop = g_getenv("XDG_CURRENT_DESKTOP");
    char **desktops = g_strsplit(desktop == NULL ? "" : desktop, ":", -1);
    char *preference = file_chooser_preference(desktops);
    char *fallback = NULL;
    guint d;
    for (d = 0; d < G_N_ELEMENTS(directories); d++) {
        GDir *directory = g_dir_open(directories[d], 0, NULL);
        const char *entry;
        if (directory == NULL) continue;
        while ((entry = g_dir_read_name(directory)) != NULL) {
            char *path; GKeyFile *key; char *interfaces, *name, *use_in;
            char *backend_id;
            guint index;
            gboolean matched = FALSE;
            if (!g_str_has_suffix(entry, ".portal")) continue;
            path = g_build_filename(directories[d], entry, NULL);
            key = g_key_file_new();
            if (!g_key_file_load_from_file(key, path, G_KEY_FILE_NONE, NULL)) { g_key_file_unref(key); g_free(path); continue; }
            interfaces = g_key_file_get_string(key, "portal", "Interfaces", NULL);
            name = g_key_file_get_string(key, "portal", "DBusName", NULL);
            use_in = g_key_file_get_string(key, "portal", "UseIn", NULL);
            backend_id = g_strndup(entry, strlen(entry) - strlen(".portal"));
            if (name != NULL && interfaces != NULL
                && strstr(interfaces, "org.freedesktop.impl.portal.FileChooser") != NULL) {
                if (fallback == NULL) fallback = g_strdup(name);
                if (preference != NULL && g_str_equal(preference, backend_id)) {
                    g_free(fallback); fallback = g_strdup(name); matched = TRUE;
                }
                for (index = 0; desktops[index] != NULL; index++)
                    if (!g_ascii_strcasecmp(desktops[index], "Spaces")) continue;
                    else if (use_in != NULL && strstr(use_in, desktops[index]) != NULL) {
                        g_free(fallback); fallback = g_strdup(name); matched = TRUE; break;
                    }
            }
            g_free(backend_id); g_free(use_in); g_free(name); g_free(interfaces); g_key_file_unref(key); g_free(path);
            if (matched) { g_dir_close(directory); g_strfreev(desktops); g_free(preference); return fallback; }
        }
        g_dir_close(directory);
    }
    g_strfreev(desktops);
    g_free(preference);
    return fallback;
}

static gboolean register_node_interface(Portal *portal, const char *path,
    GDBusInterfaceInfo *info, const GDBusInterfaceVTable *vtable, GError **error)
{
    guint registration = g_dbus_connection_register_object(portal->guest, path,
        info, vtable, portal, NULL, error);
    if (registration == 0) return FALSE;
    g_array_append_val(portal->registrations, registration);
    return TRUE;
}

static gboolean register_interfaces(Portal *portal, GError **error)
{
    GVariant *reply; const char *xml; guint index;
    GError *introspection_error = NULL;
    gboolean host_metadata = TRUE;
    reply = g_dbus_connection_call_sync(portal->host, PORTAL_NAME, PORTAL_PATH,
        "org.freedesktop.DBus.Introspectable", "Introspect", NULL,
        G_VARIANT_TYPE("(s)"), G_DBUS_CALL_FLAGS_NONE, 5000, NULL,
        &introspection_error);
    if (reply != NULL) {
        g_variant_get(reply, "(&s)", &xml);
        portal->host_node = g_dbus_node_info_new_for_xml(xml,
            &introspection_error);
        g_variant_unref(reply);
    }
    if (portal->host_node == NULL) {
        if (g_getenv("SPACES_PORTAL_TEST_ADDRESS") == NULL) {
            if (introspection_error != NULL)
                g_propagate_error(error, introspection_error);
            else
                g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
                    "The host portal returned no introspection metadata");
            return FALSE;
        }
        host_metadata = FALSE;
        g_warning("Host portal introspection is unavailable: %s",
            introspection_error == NULL ? "unknown error"
                                        : introspection_error->message);
        g_clear_error(&introspection_error);
        portal->host_node = g_dbus_node_info_new_for_xml("<node/>", error);
        if (portal->host_node == NULL) return FALSE;
    }
    portal->lifecycle_node = g_dbus_node_info_new_for_xml(lifecycle_xml, error);
    if (portal->lifecycle_node == NULL) return FALSE;
    for (index = 0; portal->host_node->interfaces[index] != NULL; index++) {
        GDBusInterfaceInfo *info = portal->host_node->interfaces[index];
        if (!interface_allowed(info->name)) continue;
        if (!register_node_interface(portal, PORTAL_PATH, info, &portal_vtable, error)) return FALSE;
    }
    if (!host_metadata) {
        for (index = 0; index < G_N_ELEMENTS(public_interfaces); index++) {
            char *filename = g_strdup_printf(
                "/usr/share/dbus-1/interfaces/org.freedesktop.portal.%s.xml",
                public_interfaces[index]);
            char *contents = NULL;
            GDBusNodeInfo *node;
            GDBusInterfaceInfo *info;
            char *name;
            if (!g_file_get_contents(filename, &contents, NULL, NULL)) {
                g_free(filename); continue;
            }
            node = g_dbus_node_info_new_for_xml(contents, NULL);
            g_free(contents); g_free(filename);
            if (node == NULL) continue;
            g_ptr_array_add(portal->owned_nodes, node);
            name = g_strconcat(DESKTOP_INTERFACE_PREFIX,
                public_interfaces[index], NULL);
            info = g_dbus_node_info_lookup_interface(node, name);
            g_free(name);
            if (info != NULL && !register_node_interface(portal, PORTAL_PATH,
                    info, &portal_vtable, error)) return FALSE;
        }
    }
    if (g_dbus_node_info_lookup_interface(portal->host_node,
            DESKTOP_INTERFACE_PREFIX "FileChooser") == NULL
        && host_metadata) {
        char *contents = NULL;
        if (g_file_get_contents(
                "/usr/share/dbus-1/interfaces/org.freedesktop.portal.FileChooser.xml",
                &contents, NULL, NULL)) {
            GDBusNodeInfo *node = g_dbus_node_info_new_for_xml(contents, NULL);
            g_free(contents);
            if (node != NULL) {
                GDBusInterfaceInfo *info = g_dbus_node_info_lookup_interface(
                    node, DESKTOP_INTERFACE_PREFIX "FileChooser");
                g_ptr_array_add(portal->owned_nodes, node);
                if (info != NULL && !register_node_interface(portal,
                        PORTAL_PATH, info, &portal_vtable, error))
                    return FALSE;
            }
        }
    }
    const struct { const char *xml; const char *path; const GDBusInterfaceVTable *vtable; } extras[] = {
        { file_manager_xml, FILE_MANAGER_PATH, &file_manager_vtable },
        { notifications_xml, NOTIFICATIONS_PATH, &simple_bridge_vtable },
        { screen_saver_xml, SCREEN_SAVER_PATH, &simple_bridge_vtable },
        { screen_saver_xml, SCREEN_SAVER_LEGACY_PATH, &simple_bridge_vtable },
        { power_xml, POWER_PATH, &simple_bridge_vtable },
        { status_watcher_xml, STATUS_WATCHER_PATH, &status_watcher_vtable },
    };
    for (index = 0; index < G_N_ELEMENTS(extras); index++) {
        GDBusNodeInfo *node = g_dbus_node_info_new_for_xml(extras[index].xml, error);
        if (node == NULL) return FALSE;
        g_ptr_array_add(portal->owned_nodes, node);
        if (!register_node_interface(portal, extras[index].path, node->interfaces[0], extras[index].vtable, error)) return FALSE;
    }
    return TRUE;
}

static gboolean host_path_has_interface(Portal *portal, const char *name,
    const char *path, const char *interface)
{
    GVariant *reply = g_dbus_connection_call_sync(portal->host, name, path,
        "org.freedesktop.DBus.Introspectable", "Introspect", NULL,
        G_VARIANT_TYPE("(s)"), G_DBUS_CALL_FLAGS_NONE, 3000, NULL, NULL);
    const char *xml;
    GDBusNodeInfo *node;
    gboolean found = FALSE;
    if (reply == NULL) return FALSE;
    g_variant_get(reply, "(&s)", &xml);
    node = g_dbus_node_info_new_for_xml(xml, NULL);
    if (node != NULL) {
        found = g_dbus_node_info_lookup_interface(node, interface) != NULL;
        g_dbus_node_info_unref(node);
    }
    g_variant_unref(reply);
    return found;
}

static void discover_screen_saver(Portal *portal)
{
    g_clear_pointer(&portal->screen_saver_host_path, g_free);
    if (host_path_has_interface(portal, SCREEN_SAVER_NAME, SCREEN_SAVER_PATH,
            SCREEN_SAVER_NAME))
        portal->screen_saver_host_path = g_strdup(SCREEN_SAVER_PATH);
    else if (host_path_has_interface(portal, SCREEN_SAVER_NAME,
                 SCREEN_SAVER_LEGACY_PATH, SCREEN_SAVER_NAME))
        portal->screen_saver_host_path = g_strdup(SCREEN_SAVER_LEGACY_PATH);
}

static void screen_saver_appeared(GDBusConnection *connection,
    const char *name, const char *owner, gpointer user_data)
{
    Portal *portal = user_data;
    (void)connection; (void)name; (void)owner;
    discover_screen_saver(portal);
}

static void screen_saver_vanished(GDBusConnection *connection,
    const char *name, gpointer user_data)
{
    Portal *portal = user_data;
    (void)connection; (void)name;
    g_clear_pointer(&portal->screen_saver_host_path, g_free);
    g_hash_table_remove_all(portal->inhibitors);
}

static gboolean register_host_identity(Portal *portal, GError **error)
{
    GVariant *reply = g_dbus_connection_call_sync(portal->host, PORTAL_NAME,
        PORTAL_PATH, "org.freedesktop.host.portal.Registry", "Register",
        g_variant_new("(s@a{sv})", portal->app_id,
            g_variant_new_array(G_VARIANT_TYPE("{sv}"), NULL, 0)),
        G_VARIANT_TYPE_UNIT, G_DBUS_CALL_FLAGS_NONE, 5000, NULL, error);
    if (reply == NULL) return FALSE;
    g_variant_unref(reply);
    return TRUE;
}

static void guest_name_changed(GDBusConnection *connection, const char *sender,
    const char *path, const char *interface, const char *signal_name,
    GVariant *parameters, gpointer user_data)
{
    Portal *portal = user_data; const char *name, *old_owner, *new_owner;
    GHashTableIter iterator; gpointer value; GPtrArray *remove;
    guint index;
    (void)connection; (void)sender; (void)path; (void)interface; (void)signal_name;
    g_variant_get(parameters, "(&s&s&s)", &name, &old_owner, &new_owner);
    if (*new_owner != '\0') {
        if (*old_owner != '\0') {
            g_hash_table_iter_init(&iterator, portal->mirrors);
            while (g_hash_table_iter_next(&iterator, NULL, &value)) {
                Mirror *mirror = value;
                if (g_str_equal(mirror->guest_owner, old_owner))
                    g_hash_table_iter_remove(&iterator);
            }
        }
        maybe_add_mpris(portal, name);
        return;
    }
    if (*old_owner == '\0') return;
    remove = g_ptr_array_new_with_free_func(g_free);
    g_hash_table_iter_init(&iterator, portal->requests_guest);
    while (g_hash_table_iter_next(&iterator, NULL, &value)) {
        Request *request = value;
        if (g_str_equal(request->owner, name)) { close_host_object(portal, request->host_path, REQUEST_INTERFACE); g_ptr_array_add(remove, g_strdup(request->guest_path)); }
    }
    for (index = 0; index < remove->len; index++) {
        Request *request = g_hash_table_lookup(portal->requests_guest, g_ptr_array_index(remove, index));
        if (request != NULL && request->host_path != NULL) g_hash_table_remove(portal->requests_host, request->host_path);
        g_hash_table_remove(portal->requests_guest, g_ptr_array_index(remove, index));
    }
    g_ptr_array_set_size(remove, 0);
    g_hash_table_iter_init(&iterator, portal->sessions_guest);
    while (g_hash_table_iter_next(&iterator, NULL, &value)) {
        Session *session = value;
        if (g_str_equal(session->owner, name)) { close_host_object(portal, session->host_path, SESSION_INTERFACE); g_ptr_array_add(remove, g_strdup(session->guest_path)); }
    }
    for (index = 0; index < remove->len; index++) {
        Session *session = g_hash_table_lookup(portal->sessions_guest, g_ptr_array_index(remove, index));
        if (session != NULL) g_hash_table_remove(portal->sessions_host, session->host_path);
        g_hash_table_remove(portal->sessions_guest, g_ptr_array_index(remove, index));
    }
    g_ptr_array_set_size(remove, 0);
    g_hash_table_iter_init(&iterator, portal->mirrors);
    while (g_hash_table_iter_next(&iterator, NULL, &value)) {
        Mirror *mirror = value;
        if (g_str_equal(mirror->guest_owner, old_owner)) {
            if (mirror->status_item)
                g_dbus_connection_emit_signal(portal->guest, NULL,
                    STATUS_WATCHER_PATH, STATUS_WATCHER_INTERFACE,
                    "StatusNotifierItemUnregistered",
                    g_variant_new("(s)", mirror->guest_name), NULL);
            g_ptr_array_add(remove, g_strdup(mirror->key));
        }
    }
    for (index = 0; index < remove->len; index++)
        g_hash_table_remove(portal->mirrors, g_ptr_array_index(remove, index));
    g_hash_table_iter_init(&iterator, portal->inhibitors);
    while (g_hash_table_iter_next(&iterator, NULL, &value)) {
        Inhibitor *inhibitor = value;
        if (g_str_equal(inhibitor->owner, name)) {
            release_inhibitor(portal, inhibitor);
            g_hash_table_iter_remove(&iterator);
        }
    }
    g_ptr_array_unref(remove);
}

static void name_lost(GDBusConnection *connection, const char *name, gpointer user_data)
{ Portal *portal = user_data; (void)connection; g_warning("Lost required bus name %s", name); portal->exit_status = 1; g_main_loop_quit(portal->loop); }
static void optional_acquired(GDBusConnection *connection, const char *name, gpointer user_data)
{ Portal *portal = user_data; (void)connection; if (g_str_equal(name, NOTIFICATIONS_NAME)) portal->notifications_owned = TRUE; else if (g_str_equal(name, SCREEN_SAVER_NAME)) portal->screen_saver_owned = TRUE; else if (g_str_equal(name, POWER_NAME)) portal->power_owned = TRUE; }
static void optional_lost(GDBusConnection *connection, const char *name, gpointer user_data)
{ Portal *portal = user_data; (void)connection; if (g_str_equal(name, NOTIFICATIONS_NAME)) portal->notifications_owned = FALSE; else if (g_str_equal(name, SCREEN_SAVER_NAME)) portal->screen_saver_owned = FALSE; else if (g_str_equal(name, POWER_NAME)) portal->power_owned = FALSE; g_warning("Optional integration name %s is already owned", name); }
static void host_closed(GDBusConnection *connection, gboolean vanished, GError *error, gpointer user_data)
{
    Portal *portal = user_data;
    (void)connection;
    g_warning("Host portal bus connection closed (remote=%s): %s",
        vanished ? "yes" : "no",
        error == NULL ? "no error reported" : error->message);
    portal->exit_status = 1;
    g_main_loop_quit(portal->loop);
}
static void host_portal_appeared(GDBusConnection *connection, const char *name,
    const char *owner, gpointer user_data)
{ (void)connection; (void)name; (void)owner; (void)user_data; }
static void host_portal_vanished(GDBusConnection *connection, const char *name,
    gpointer user_data)
{
    Portal *portal = user_data;
    (void)connection;
    g_warning("Host portal bus name %s vanished", name);
    portal->exit_status = 1;
    g_main_loop_quit(portal->loop);
}

int main(void)
{
    Portal portal = {0}; GError *error = NULL; char *address; const char *test_address;
    guint portal_owner, file_owner, notification_owner, saver_owner, power_owner;
    guint status_owner;
    guint guest_names, screen_saver_watcher, host_portal_watcher;
    signal(SIGPIPE, SIG_IGN);
    portal.space_name = g_strdup(g_getenv("SPACES_NAME"));
    if (portal.space_name == NULL || *portal.space_name == '\0') { g_printerr("spaces-portal: SPACES_NAME is unavailable\n"); return 1; }
    portal.app_id = g_strconcat("org.anatase.Spaces.", portal.space_name, NULL);
    test_address = g_getenv("SPACES_PORTAL_TEST_ADDRESS");
    address = test_address != NULL ? g_strdup(test_address) : g_strdup_printf("unix:path=/run/spaces/desktop/%u/portal/bus", (unsigned)getuid());
    portal.guest = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &error);
    if (portal.guest == NULL) goto failed;
    portal.host = g_dbus_connection_new_for_address_sync(address,
        G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT | G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
        NULL, NULL, &error);
    if (portal.host == NULL) goto failed;
    portal.loop = g_main_loop_new(NULL, FALSE);
    portal.owned_nodes = g_ptr_array_new_with_free_func((GDestroyNotify)g_dbus_node_info_unref);
    portal.registrations = g_array_new(FALSE, FALSE, sizeof(guint));
    portal.requests_guest = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, request_free);
    portal.requests_host = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    portal.sessions_guest = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, session_free);
    portal.sessions_host = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    portal.mirrors = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, mirror_free);
    portal.inhibitors = g_hash_table_new_full(g_str_hash, g_str_equal, g_free,
        inhibitor_free);
    portal.file_chooser_backend = choose_file_chooser_backend();
    if (!register_host_identity(&portal, &error)) {
        g_warning("Could not bind the host portal connection to %s: %s",
            portal.app_id, error->message);
        g_clear_error(&error);
    }
    if (!register_interfaces(&portal, &error)) goto failed;
    g_dbus_connection_signal_subscribe(portal.host, PORTAL_NAME, NULL, NULL, NULL,
        NULL, G_DBUS_SIGNAL_FLAGS_NONE, host_portal_signal, &portal, NULL);
    g_dbus_connection_signal_subscribe(portal.host, NOTIFICATIONS_NAME, NOTIFICATIONS_NAME,
        NULL, NOTIFICATIONS_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, native_host_signal, &portal, NULL);
    g_dbus_connection_signal_subscribe(portal.host, SCREEN_SAVER_NAME, SCREEN_SAVER_NAME,
        NULL, NULL, NULL, G_DBUS_SIGNAL_FLAGS_NONE, native_host_signal, &portal, NULL);
    g_dbus_connection_signal_subscribe(portal.host, POWER_NAME, POWER_NAME,
        NULL, POWER_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, native_host_signal, &portal, NULL);
    guest_names = g_dbus_connection_signal_subscribe(portal.guest, DBUS_NAME, DBUS_NAME,
        "NameOwnerChanged", DBUS_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, guest_name_changed, &portal, NULL);
    g_signal_connect(portal.host, "closed", G_CALLBACK(host_closed), &portal);
    screen_saver_watcher = g_bus_watch_name_on_connection(portal.host,
        SCREEN_SAVER_NAME, G_BUS_NAME_WATCHER_FLAGS_AUTO_START,
        screen_saver_appeared, screen_saver_vanished, &portal, NULL);
    host_portal_watcher = g_bus_watch_name_on_connection(portal.host,
        PORTAL_NAME, G_BUS_NAME_WATCHER_FLAGS_AUTO_START,
        host_portal_appeared, host_portal_vanished, &portal, NULL);
    portal_owner = g_bus_own_name_on_connection(portal.guest, PORTAL_NAME,
        G_BUS_NAME_OWNER_FLAGS_ALLOW_REPLACEMENT | G_BUS_NAME_OWNER_FLAGS_REPLACE,
        NULL, name_lost, &portal, NULL);
    file_owner = g_bus_own_name_on_connection(portal.guest, FILE_MANAGER_NAME,
        G_BUS_NAME_OWNER_FLAGS_ALLOW_REPLACEMENT | G_BUS_NAME_OWNER_FLAGS_REPLACE,
        NULL, name_lost, &portal, NULL);
    notification_owner = g_bus_own_name_on_connection(portal.guest, NOTIFICATIONS_NAME,
        G_BUS_NAME_OWNER_FLAGS_NONE, optional_acquired, optional_lost, &portal, NULL);
    saver_owner = g_bus_own_name_on_connection(portal.guest, SCREEN_SAVER_NAME,
        G_BUS_NAME_OWNER_FLAGS_NONE, optional_acquired, optional_lost, &portal, NULL);
    power_owner = g_bus_own_name_on_connection(portal.guest, POWER_NAME,
        G_BUS_NAME_OWNER_FLAGS_NONE, optional_acquired, optional_lost, &portal, NULL);
    status_owner = g_bus_own_name_on_connection(portal.guest, STATUS_WATCHER_NAME,
        G_BUS_NAME_OWNER_FLAGS_ALLOW_REPLACEMENT | G_BUS_NAME_OWNER_FLAGS_REPLACE,
        NULL, name_lost, &portal, NULL);
    discover_mpris(&portal);
    g_main_loop_run(portal.loop);
    g_bus_unown_name(status_owner);
    g_bus_unown_name(power_owner); g_bus_unown_name(saver_owner); g_bus_unown_name(notification_owner);
    g_bus_unown_name(file_owner); g_bus_unown_name(portal_owner);
    g_bus_unwatch_name(screen_saver_watcher);
    g_bus_unwatch_name(host_portal_watcher);
    g_dbus_connection_signal_unsubscribe(portal.guest, guest_names);
    g_hash_table_unref(portal.inhibitors);
    g_hash_table_unref(portal.mirrors);
    g_hash_table_unref(portal.sessions_host); g_hash_table_unref(portal.sessions_guest);
    g_hash_table_unref(portal.requests_host); g_hash_table_unref(portal.requests_guest);
    g_array_unref(portal.registrations); g_ptr_array_unref(portal.owned_nodes);
    g_dbus_node_info_unref(portal.lifecycle_node); g_dbus_node_info_unref(portal.host_node);
    g_main_loop_unref(portal.loop); g_object_unref(portal.host); g_object_unref(portal.guest);
    g_free(portal.screen_saver_host_path); g_free(portal.file_chooser_backend); g_free(portal.app_id); g_free(portal.space_name); g_free(address);
    return portal.exit_status;
failed:
    if (error != NULL) { g_printerr("spaces-portal: %s\n", error->message); g_clear_error(&error); }
    if (portal.host != NULL) g_object_unref(portal.host);
    if (portal.guest != NULL) g_object_unref(portal.guest);
    g_free(portal.screen_saver_host_path); g_free(portal.file_chooser_backend); g_free(portal.app_id); g_free(portal.space_name); g_free(address);
    return 1;
}
