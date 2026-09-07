/* Service policy shared by the unprivileged boundary and privileged broker.
 * Introspection describes the host API; it never expands this policy. */
#pragma once
#include <gio/gio.h>
#include <string.h>

#define CONTROL "org.anatase.Spaces.SystemBridge1"
#define CONTROL_PATH "/org/anatase/Spaces/SystemBridge"
#define PROPERTIES "org.freedesktop.DBus.Properties"
#define NM "org.freedesktop.NetworkManager"
#define RESOLVE "org.freedesktop.resolve1"
#define UPOWER "org.freedesktop.UPower"
static const char *const services[] = { RESOLVE, NM, UPOWER };

static const char *const roots[]
    = { "/org/freedesktop/resolve1", "/org/freedesktop/NetworkManager", "/org/freedesktop/UPower" };

static int service_index(const char *name)
{
    for (int index = 0; index < 3; index++) {
        if (g_strcmp0(name, services[index]) == 0) {
            return index;
        }
    }
    return -1;
}

static gboolean path_has_prefix(const char *value, const char *prefix, char separator)
{
    size_t n = strlen(prefix);
    return value && g_str_has_prefix(value, prefix) && (!value[n] || value[n] == separator);
}

/* NetworkManager publishes its ObjectManager one level above its objects. */
static gboolean service_path(int service, const char *path)
{
    return path_has_prefix(path, roots[service], '/')
        || (service == 1 && g_strcmp0(path, "/org/freedesktop") == 0);
}

static gboolean method_is_listed(const char *value, const char *list)
{
    if (!value) {
        return FALSE;
    }
    char **words = g_strsplit(list, " ", -1);
    gboolean found = g_strv_contains((const gchar *const *)words, value);
    g_strfreev(words);
    return found;
}

static gboolean service_interface(int service, const char *interface)
{
    return interface && path_has_prefix(interface, services[service], '.');
}

static gboolean read_method(int service, const char *interface, const char *method)
{
    if (service == 0) {
        if (g_str_equal(interface, RESOLVE ".Manager")) {
            return method_is_listed(
                method, "GetLink ResolveHostname ResolveAddress ResolveRecord ResolveService");
        }
        return FALSE;
    }
    if (service == 1) {
        if (g_str_equal(interface, NM)) {
            return method_is_listed(method,
                "GetDevices GetAllDevices GetDeviceByIpIface GetPermissions GetLogging state");
        }
        if (g_str_equal(interface, NM ".Settings")) {
            return method_is_listed(method, "ListConnections GetConnectionByUuid");
        }
        if (g_str_equal(interface, NM ".Settings.Connection")) {
            return g_str_equal(method, "GetSettings");
        }
        if (g_str_equal(interface, NM ".Device.Wireless")) {
            return method_is_listed(method, "GetAccessPoints GetAllAccessPoints");
        }
        if (g_str_equal(interface, NM ".Device.WifiP2P")) {
            return g_str_equal(method, "GetPeers");
        }
        if (g_str_equal(interface, NM ".Device")) {
            return g_str_equal(method, "GetAppliedConnection");
        }
        return FALSE;
    }
    if (g_str_equal(interface, UPOWER)) {
        return method_is_listed(
            method, "EnumerateDevices EnumerateKbdBacklights GetDisplayDevice GetCriticalAction");
    }
    if (g_str_equal(interface, UPOWER ".Device")) {
        return method_is_listed(method, "GetHistory GetStatistics Refresh");
    }
    if (g_str_equal(interface, UPOWER ".KbdBacklight")) {
        return method_is_listed(method, "GetBrightness GetMaxBrightness");
    }
    return FALSE;
}

static gboolean system_call_allowed(GDBusMessage *message, gboolean admin)
{
    int service = service_index(g_dbus_message_get_destination(message));
    const char *path = g_dbus_message_get_path(message);
    const char *interface = g_dbus_message_get_interface(message);
    const char *method = g_dbus_message_get_member(message);
    GVariant *body = g_dbus_message_get_body(message);
    if (service < 0 || !service_path(service, path) || !interface || !method) {
        return FALSE;
    }
    if (g_str_equal(interface, "org.freedesktop.DBus.Introspectable")) {
        return g_str_equal(method, "Introspect");
    }
    if (g_str_equal(interface, "org.freedesktop.DBus.Peer")) {
        return method_is_listed(method, "Ping GetMachineId");
    }
    if (g_str_equal(interface, "org.freedesktop.DBus.ObjectManager")) {
        return g_str_equal(method, "GetManagedObjects");
    }
    if (g_str_equal(interface, PROPERTIES)) {
        const char *target = NULL;
        if (!body || !g_variant_n_children(body)) {
            return FALSE;
        }
        GVariant *first = g_variant_get_child_value(body, 0);
        if (g_variant_is_of_type(first, G_VARIANT_TYPE_STRING)) {
            target = g_variant_get_string(first, NULL);
        }
        gboolean ok = target
            && (service_interface(service, target) || (!*target && g_str_equal(method, "GetAll")))
            && (method_is_listed(method, "Get GetAll")
                || (admin && service != 2 && g_str_equal(method, "Set")));
        g_variant_unref(first);
        return ok;
    }
    return service_interface(service, interface)
        && (read_method(service, interface, method) || (service != 2 && admin));
}
