#pragma once
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
/* Copy only transport headers; bodies and descriptor indexes stay intact. */
static inline GDBusMessage *spaces_message_copy(GDBusMessage *source, const char *destination)
{
    GDBusMessage *copy = g_dbus_message_copy(source, NULL);
    g_dbus_message_set_sender(copy, NULL);
    g_dbus_message_set_destination(copy, destination);
    return copy;
}

static inline gboolean spaces_message_bounded(GDBusMessage *message)
{
    GVariant *body = g_dbus_message_get_body(message);
    GUnixFDList *fds = g_dbus_message_get_unix_fd_list(message);
    return (!body || g_variant_get_size(body) <= 8 * 1024 * 1024)
        && (!fds || g_unix_fd_list_get_length(fds) <= 64);
}

/* Both bridges open a distinct authenticated connection rather than sharing
 * GIO's process-global bus singleton across clients. */
static inline GDBusConnection *spaces_connect_bus(const char *address, GError **error)
{
    return g_dbus_connection_new_for_address_sync(address,
        G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT
            | G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
        NULL, NULL, error);
}
