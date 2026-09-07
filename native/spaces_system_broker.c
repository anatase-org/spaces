#define _GNU_SOURCE
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <glib-unix.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>
#include "system_bus_policy.h"
#include "dbus_message.h"

#if defined(__x86_64__) || defined(__aarch64__)
typedef int (*main_function)(int, char **, char **);
extern int spaces_old_libc_start_main(
    main_function, int, char **, void (*)(void), void (*)(void), void (*)(void), void *);
#if defined(__x86_64__)
__asm__(".symver spaces_old_libc_start_main,__libc_start_main@GLIBC_2.2.5");
#else
__asm__(".symver spaces_old_libc_start_main,__libc_start_main@GLIBC_2.17");
#endif
int __wrap___libc_start_main(main_function function, int argc, char **argv, void (*init)(void),
    void (*fini)(void), void (*rtld_fini)(void), void *stack_end)
{
    return spaces_old_libc_start_main(function, argc, argv, init, fini, rtld_fini, stack_end);
}
#endif

typedef struct App App;
typedef struct Client Client;
struct App {
    gboolean broker, admin;
    char *address, *bus_address;
    GMainLoop *loop;
    GDBusConnection *bus;
    GHashTable *clients;
    guint names[3];
    gint queued;
};
struct Client {
    gint refs;
    App *app;
    char *name;
    GDBusConnection *peer, *host;
    gboolean opened, admin, observer, closed, agent;
    guint filters[2], watches[3], subscriptions[3];
    char *owners[3];
    guint outstanding;
};

typedef struct {
    Client *client;
    GDBusConnection *origin;
    GDBusMessage *request;
    gboolean permissions, register_agent, unregister_agent;
} Call;

typedef struct {
    App *app;
    Client *client;
    GDBusConnection *bus;
    GDBusMessage *message;
} Event;

/* Client lifetime */

static Client *client_ref(Client *client)
{
    g_atomic_int_inc(&client->refs);
    return client;
}

static void client_unref(gpointer data)
{
    Client *client = data;
    if (!g_atomic_int_dec_and_test(&client->refs)) {
        return;
    }
    g_clear_object(&client->peer);
    g_clear_object(&client->host);
    for (int index = 0; index < 3; index++) {
        g_free(client->owners[index]);
    }
    g_free(client->name);
    g_free(client);
}

static void client_close(Client *client)
{
    if (client->closed) {
        return;
    }
    client->closed = TRUE;
    for (int index = 0; index < 3; index++) {
        if (client->watches[index]) {
            g_bus_unwatch_name(client->watches[index]);
        }
        if (client->subscriptions[index]) {
            g_dbus_connection_signal_unsubscribe(client->host, client->subscriptions[index]);
        }
    }
    if (client->filters[0]) {
        g_dbus_connection_remove_filter(client->peer, client->filters[0]);
    }
    if (client->filters[1]) {
        g_dbus_connection_remove_filter(client->host, client->filters[1]);
    }
    if (client->peer) {
        g_signal_handlers_disconnect_by_data(client->peer, client);
        g_dbus_connection_close(client->peer, NULL, NULL, NULL);
    }
    if (client->host) {
        g_signal_handlers_disconnect_by_data(client->host, client);
        g_dbus_connection_close(client->host, NULL, NULL, NULL);
    }
}

static void remove_client(gpointer data)
{
    client_close(data);
    client_unref(data);
}

static void connection_closed(GDBusConnection *bus, gboolean remote, GError *error, gpointer data)
{
    (void)bus;
    (void)remote;
    (void)error;
    Client *client = client_ref(data);
    g_hash_table_remove(client->app->clients, client->name);
    client_unref(client);
}

/* Message forwarding */

static void send_message(GDBusConnection *bus, GDBusMessage *message)
{
    g_dbus_connection_send_message(bus, message, G_DBUS_SEND_MESSAGE_FLAGS_NONE, NULL, NULL);
    g_object_unref(message);
}

static void reply_error(
    GDBusConnection *bus, GDBusMessage *request, const char *name, const char *text)
{
    if (g_str_equal(name, "org.freedesktop.DBus.Error.AccessDenied")) {
        g_debug("Denied system call: destination=%s path=%s interface=%s member=%s",
            g_dbus_message_get_destination(request), g_dbus_message_get_path(request),
            g_dbus_message_get_interface(request), g_dbus_message_get_member(request));
    }
    send_message(bus, g_dbus_message_new_method_error_literal(request, name, text));
}

static void reply_success(GDBusConnection *bus, GDBusMessage *request, GVariant *body)
{
    GDBusMessage *reply = g_dbus_message_new_method_reply(request);
    g_dbus_message_set_body(reply, body);
    send_message(bus, reply);
}

static GDBusConnection *connect_bus(App *app, GError **error)
{
    return g_dbus_connection_new_for_address_sync(app->bus_address,
        G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT
            | G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
        NULL, NULL, error);
}

static void forward_finished(GObject *source, GAsyncResult *result, gpointer data)
{
    Call *call = data;
    Client *client = call->client;
    GError *error = NULL;
    GDBusMessage *reply = g_dbus_connection_send_message_with_reply_finish(
        G_DBUS_CONNECTION(source), result, &error);
    if (!reply || !spaces_message_bounded(reply)) {
        reply_error(call->origin, call->request, "org.freedesktop.DBus.Error.NoReply",
            "System service unavailable or reply limit exceeded");
    } else {
        GDBusMessage *copy = spaces_message_copy(reply, g_dbus_message_get_sender(call->request));
        g_dbus_message_set_reply_serial(copy, g_dbus_message_get_serial(call->request));
        if (g_dbus_message_get_message_type(reply) == G_DBUS_MESSAGE_TYPE_METHOD_RETURN) {
            if (call->register_agent) {
                client->agent = TRUE;
            }
            if (call->unregister_agent) {
                client->agent = FALSE;
            }
            if (call->permissions && !client->admin) {
                GVariant *body = g_dbus_message_get_body(reply);
                if (body && g_variant_is_of_type(body, G_VARIANT_TYPE("(a{ss})"))) {
                    GVariantIter *iter;
                    const char *key, *value;
                    GVariantBuilder builder;
                    g_variant_builder_init(&builder, G_VARIANT_TYPE("a{ss}"));
                    g_variant_get(body, "(a{ss})", &iter);
                    while (g_variant_iter_loop(iter, "{&s&s}", &key, &value)) {
                        g_variant_builder_add(&builder, "{ss}", key, "no");
                    }
                    g_variant_iter_free(iter);
                    g_dbus_message_set_body(copy, g_variant_new("(a{ss})", &builder));
                }
            }
        }
        send_message(call->origin, copy);
    }
    g_clear_object(&reply);
    g_clear_error(&error);
    client->outstanding--;
    g_object_unref(call->origin);
    g_object_unref(call->request);
    client_unref(client);
    g_free(call);
}

static void forward(Client *client, GDBusConnection *origin, GDBusConnection *target,
    GDBusMessage *request, const char *destination)
{
    if (client->outstanding >= 64) {
        reply_error(origin, request, "org.freedesktop.DBus.Error.LimitsExceeded",
            "Too many outstanding calls");
        return;
    }
    Call *call = g_new0(Call, 1);
    call->client = client_ref(client);
    call->origin = g_object_ref(origin);
    call->request = g_object_ref(request);
    call->permissions = g_strcmp0(g_dbus_message_get_interface(request), NM) == 0
        && g_strcmp0(g_dbus_message_get_member(request), "GetPermissions") == 0;
    gboolean agent = client->app->broker && origin == client->peer
        && g_strcmp0(g_dbus_message_get_interface(request), NM ".AgentManager") == 0;
    call->register_agent = agent
        && method_is_listed(
            g_dbus_message_get_member(request), "Register RegisterWithCapabilities");
    call->unregister_agent
        = agent && g_strcmp0(g_dbus_message_get_member(request), "Unregister") == 0;
    GDBusMessage *copy = spaces_message_copy(request, destination);
    /* Even NO_REPLY_EXPECTED requests are tracked and bounded internally. */
    g_dbus_message_set_flags(
        copy, g_dbus_message_get_flags(copy) & ~G_DBUS_MESSAGE_FLAGS_NO_REPLY_EXPECTED);
    client->outstanding++;
    g_dbus_connection_send_message_with_reply(
        target, copy, G_DBUS_SEND_MESSAGE_FLAGS_NONE, 120000, NULL, NULL, forward_finished, call);
    g_object_unref(copy);
}

/* Host service discovery */

static void ignore_signal(GDBusConnection *bus, const gchar *sender, const gchar *path,
    const gchar *interface, const gchar *signal, GVariant *body, gpointer data)
{
    (void)bus;
    (void)sender;
    (void)path;
    (void)interface;
    (void)signal;
    (void)body;
    (void)data;
}

static void appeared(GDBusConnection *bus, const gchar *name, const gchar *owner, gpointer data)
{
    (void)bus;
    Client *client = data;
    int index = service_index(name);
    g_free(client->owners[index]);
    client->owners[index] = g_strdup(owner);
    if (client->observer) {
        g_dbus_connection_emit_signal(
            client->peer, NULL, CONTROL_PATH, CONTROL, "Changed", g_variant_new("(s)", name), NULL);
    }
}

static void vanished(GDBusConnection *bus, const gchar *name, gpointer data)
{
    (void)bus;
    Client *client = data;
    int index = service_index(name);
    g_clear_pointer(&client->owners[index], g_free);
    if (index == 1) {
        client->agent = FALSE;
    }
    if (client->observer) {
        g_dbus_connection_emit_signal(
            client->peer, NULL, CONTROL_PATH, CONTROL, "Changed", g_variant_new("(s)", name), NULL);
    }
}

static GDBusMessage *client_filter(GDBusConnection *, GDBusMessage *, gboolean, gpointer);
static void initialize_host(Client *client)
{
    client->filters[1] = g_dbus_connection_add_filter(
        client->host, client_filter, client_ref(client), client_unref);
    g_signal_connect(client->host, "closed", G_CALLBACK(connection_closed), client);
    for (int index = 0; index < 3; index++) {
        client->watches[index] = g_bus_watch_name_on_connection(client->host, services[index],
            G_BUS_NAME_WATCHER_FLAGS_AUTO_START, appeared, vanished, client, NULL);
        if (client->observer) {
            client->subscriptions[index]
                = g_dbus_connection_signal_subscribe(client->host, services[index], NULL, NULL,
                    NULL, NULL, G_DBUS_SIGNAL_FLAGS_NONE, ignore_signal, NULL, NULL);
        }
    }
}

static void open_client(Client *client, GDBusMessage *request)
{
    GVariant *body = g_dbus_message_get_body(request);
    if (client->opened || !body || !g_variant_is_of_type(body, G_VARIANT_TYPE("(ub)"))) {
        reply_error(client->peer, request, "org.freedesktop.DBus.Error.InvalidArgs",
            "Invalid client handshake");
        return;
    }
    guint32 uid;
    g_variant_get(body, "(ub)", &uid, &client->observer);
    GError *error = NULL;
    client->host = connect_bus(client->app, &error);
    if (!client->host) {
        g_clear_error(&error);
        reply_error(client->peer, request, "org.freedesktop.DBus.Error.NoServer",
            "Host system bus unavailable");
        return;
    }
    client->admin = client->app->admin && uid == 0 && !client->observer;
    client->opened = TRUE;
    initialize_host(client);
    reply_success(client->peer, request, g_variant_new("(b)", client->admin));
}

/* Guest callers and broker connections */

static Client *relay_client(App *app, const char *sender, gboolean observer)
{
    Client *client = g_hash_table_lookup(app->clients, sender);
    if (client) {
        return client;
    }
    if (g_hash_table_size(app->clients) >= 128) {
        return NULL;
    }
    GError *error = NULL;
    guint32 uid = 0;
    if (!observer) {
        GVariant *reply
            = g_dbus_connection_call_sync(app->bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "GetConnectionUnixUser", g_variant_new("(s)", sender),
                G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, 5000, NULL, &error);
        if (!reply) {
            g_clear_error(&error);
            return NULL;
        }
        g_variant_get(reply, "(u)", &uid);
        g_variant_unref(reply);
    }
    GDBusConnection *peer = g_dbus_connection_new_for_address_sync(
        app->address, G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT, NULL, NULL, &error);
    if (!peer) {
        g_clear_error(&error);
        return NULL;
    }
    GVariant *reply = g_dbus_connection_call_sync(peer, NULL, CONTROL_PATH, CONTROL, "Open",
        g_variant_new("(ub)", uid, observer), G_VARIANT_TYPE("(b)"), G_DBUS_CALL_FLAGS_NONE, 5000,
        NULL, &error);
    if (!reply) {
        g_clear_error(&error);
        g_dbus_connection_close_sync(peer, NULL, NULL);
        g_object_unref(peer);
        return NULL;
    }
    client = g_new0(Client, 1);
    client->refs = 1;
    client->app = app;
    client->name = g_strdup(sender);
    client->peer = peer;
    client->opened = TRUE;
    client->observer = observer;
    g_variant_get(reply, "(b)", &client->admin);
    g_variant_unref(reply);
    /* Broker authority can never turn a non-root guest caller into an admin. */
    client->admin = client->admin && uid == 0;
    client->filters[0]
        = g_dbus_connection_add_filter(peer, client_filter, client_ref(client), client_unref);
    g_signal_connect(peer, "closed", G_CALLBACK(connection_closed), client);
    g_hash_table_insert(app->clients, g_strdup(sender), client);
    return client;
}

/* Routing across the system bus boundary */

static void handle_guest_call(Event *event)
{
    App *app = event->app;
    GDBusMessage *message = event->message;
    Client *client = relay_client(app, g_dbus_message_get_sender(message), FALSE);

    if (!client) {
        reply_error(event->bus, message, "org.freedesktop.DBus.Error.NoServer",
            "System bridge unavailable");
        return;
    }

    /* Libraries address the unique owner after resolving a service name. */
    const char *destination = g_dbus_message_get_destination(message);
    if (g_strcmp0(destination, g_dbus_connection_get_unique_name(app->bus)) == 0) {
        GDBusMessage *copy = g_dbus_message_copy(message, NULL);
        for (int index = 0; index < 3; index++) {
            if (service_path(index, g_dbus_message_get_path(message))) {
                g_dbus_message_set_destination(copy, services[index]);
                break;
            }
        }
        g_object_unref(message);
        event->message = message = copy;
    }

    if (!system_call_allowed(message, client->admin)) {
        reply_error(event->bus, message, "org.freedesktop.DBus.Error.AccessDenied",
            "Spaces system service permission denied");
        return;
    }

    forward(client, event->bus, client->peer, message, g_dbus_message_get_destination(message));
}

static void handle_broker_call(Event *event)
{
    Client *client = event->client;
    GDBusMessage *message = event->message;
    const char *interface = g_dbus_message_get_interface(message);
    const char *member = g_dbus_message_get_member(message);
    const char *path = g_dbus_message_get_path(message);

    if (g_strcmp0(interface, CONTROL) == 0 && g_strcmp0(member, "Open") == 0
        && g_strcmp0(path, CONTROL_PATH) == 0) {
        open_client(client, message);
        return;
    }

    if (!client->opened || client->observer || !system_call_allowed(message, client->admin)) {
        reply_error(event->bus, message, "org.freedesktop.DBus.Error.AccessDenied",
            "Spaces system service permission denied");
        return;
    }

    forward(client, client->peer, client->host, message, g_dbus_message_get_destination(message));
}

static void forward_host_signal(Event *event)
{
    Client *client = event->client;
    GDBusMessage *message = event->message;
    const char *sender = g_dbus_message_get_sender(message);

    for (int index = 0; index < 3; index++) {
        if (!client->owners[index] || g_strcmp0(sender, client->owners[index]) != 0
            || !service_path(index, g_dbus_message_get_path(message))) {
            continue;
        }

        /* One observer forwards broadcasts. Caller connections forward only
         * signals explicitly addressed to that caller. */
        gboolean unicast = g_dbus_message_get_destination(message) != NULL;
        if (unicast == client->observer) {
            return;
        }

        GDBusMessage *copy = spaces_message_copy(message, NULL);
        g_dbus_message_set_sender(copy, services[index]);
        send_message(client->peer, copy);
        return;
    }
}

static void forward_peer_signal(Event *event)
{
    App *app = event->app;
    Client *client = event->client;
    GDBusMessage *message = event->message;
    const char *interface = g_dbus_message_get_interface(message);
    const char *member = g_dbus_message_get_member(message);

    if (g_strcmp0(interface, CONTROL) == 0 && g_strcmp0(member, "Changed") == 0) {
        /* Make client libraries rediscover objects after host service restarts. */
        GVariant *body = g_dbus_message_get_body(message);
        if (!client->observer || !body || !g_variant_is_of_type(body, G_VARIANT_TYPE("(s)"))) {
            return;
        }

        const char *name;
        g_variant_get(body, "(&s)", &name);
        int index = service_index(name);
        if (index >= 0) {
            if (app->names[index]) {
                g_bus_unown_name(app->names[index]);
            }
            app->names[index] = g_bus_own_name_on_connection(
                app->bus, services[index], G_BUS_NAME_OWNER_FLAGS_NONE, NULL, NULL, NULL, NULL);
        }
        return;
    }

    int index = service_index(g_dbus_message_get_sender(message));
    if (index >= 0 && service_path(index, g_dbus_message_get_path(message))) {
        const char *destination = client->observer ? NULL : client->name;
        send_message(app->bus, spaces_message_copy(message, destination));
    }
}

static void handle_agent_callback(Event *event)
{
    App *app = event->app;
    Client *client = event->client;
    GDBusMessage *message = event->message;
    const char *interface = g_dbus_message_get_interface(message);
    const char *path = g_dbus_message_get_path(message);
    const char *member = g_dbus_message_get_member(message);

    gboolean callback = g_strcmp0(interface, NM ".SecretAgent") == 0
        && g_strcmp0(path, "/org/freedesktop/NetworkManager/SecretAgent") == 0
        && method_is_listed(member, "GetSecrets CancelGetSecrets SaveSecrets DeleteSecrets");

    if (app->broker) {
        callback = callback && client->agent && client->owners[1]
            && g_strcmp0(g_dbus_message_get_sender(message), client->owners[1]) == 0;
    } else {
        callback = callback && client->admin && !client->observer;
    }

    if (!callback) {
        reply_error(event->bus, message, "org.freedesktop.DBus.Error.AccessDenied",
            "Callback not permitted");
        return;
    }

    forward(client, event->bus, app->broker ? client->peer : app->bus, message,
        app->broker ? NULL : client->name);
}

static gboolean process_event(gpointer data)
{
    Event *event = data;
    App *app = event->app;
    Client *client = event->client;
    GDBusMessageType type = g_dbus_message_get_message_type(event->message);

    if (client && client->closed) {
        goto out;
    }
    if (!spaces_message_bounded(event->message)) {
        if (type == G_DBUS_MESSAGE_TYPE_METHOD_CALL) {
            reply_error(event->bus, event->message, "org.freedesktop.DBus.Error.LimitsExceeded",
                "Message limit exceeded");
        }
        goto out;
    }

    if (!app->broker && !client) {
        handle_guest_call(event);
    } else if (app->broker && event->bus == client->peer) {
        handle_broker_call(event);
    } else if (type == G_DBUS_MESSAGE_TYPE_SIGNAL) {
        if (app->broker) {
            forward_host_signal(event);
        } else {
            forward_peer_signal(event);
        }
    } else if (type == G_DBUS_MESSAGE_TYPE_METHOD_CALL) {
        handle_agent_callback(event);
    }

out:
    g_atomic_int_add(&app->queued, -1);
    if (client) {
        client_unref(client);
    }
    g_object_unref(event->bus);
    g_object_unref(event->message);
    g_free(event);
    return G_SOURCE_REMOVE;
}

static GDBusMessage *queue_message(
    App *app, Client *client, GDBusConnection *bus, GDBusMessage *message)
{
    if (g_atomic_int_add(&app->queued, 1) >= 1024) {
        g_atomic_int_add(&app->queued, -1);
        if (g_dbus_message_get_message_type(message) == G_DBUS_MESSAGE_TYPE_METHOD_CALL) {
            reply_error(bus, message, "org.freedesktop.DBus.Error.LimitsExceeded",
                "System bridge queue full");
        }
        g_object_unref(message);
        return NULL;
    }
    Event *event = g_new0(Event, 1);
    event->app = app;
    event->client = client ? client_ref(client) : NULL;
    event->bus = g_object_ref(bus);
    event->message = message;
    g_idle_add(process_event, event);
    return NULL;
}

static GDBusMessage *client_filter(
    GDBusConnection *bus, GDBusMessage *message, gboolean incoming, gpointer data)
{
    Client *client = data;
    GDBusMessageType type = g_dbus_message_get_message_type(message);
    if (incoming && type == G_DBUS_MESSAGE_TYPE_METHOD_CALL) {
        return queue_message(client->app, client, bus, message);
    }
    if (incoming && type == G_DBUS_MESSAGE_TYPE_SIGNAL) {
        queue_message(client->app, client, bus, g_object_ref(message));
    }
    return message;
}

static GDBusMessage *guest_filter(
    GDBusConnection *bus, GDBusMessage *message, gboolean incoming, gpointer data)
{
    if (incoming && g_dbus_message_get_message_type(message) == G_DBUS_MESSAGE_TYPE_METHOD_CALL) {
        return queue_message(data, NULL, bus, message);
    }
    return message;
}

static void guest_names(GDBusConnection *bus, const gchar *sender, const gchar *path,
    const gchar *interface, const gchar *signal, GVariant *body, gpointer data)
{
    (void)bus;
    (void)sender;
    (void)path;
    (void)interface;
    (void)signal;
    App *app = data;
    const char *name, *old, *now;
    g_variant_get(body, "(&s&s&s)", &name, &old, &now);
    if (name[0] == ':' && !*now) {
        g_hash_table_remove(app->clients, name);
    }
}

/* Broker authentication and startup */

static gboolean auth_peer(
    GDBusAuthObserver *observer, GIOStream *stream, GCredentials *credentials, gpointer data)
{
    (void)observer;
    (void)stream;
    (void)data;
    return credentials && g_credentials_get_unix_user(credentials, NULL) == 0;
}

static gboolean auth_mechanism(GDBusAuthObserver *observer, const char *mechanism, gpointer data)
{
    (void)observer;
    (void)data;
    return g_str_equal(mechanism, "EXTERNAL");
}

static gboolean new_peer(GDBusServer *server, GDBusConnection *peer, gpointer data)
{
    (void)server;
    App *app = data;
    if (g_hash_table_size(app->clients) >= 129) {
        return FALSE;
    }
    Client *client = g_new0(Client, 1);
    client->refs = 1;
    client->app = app;
    client->name = g_strdup_printf("%p", (void *)peer);
    client->peer = g_object_ref(peer);
    client->filters[0]
        = g_dbus_connection_add_filter(peer, client_filter, client_ref(client), client_unref);
    g_signal_connect(peer, "closed", G_CALLBACK(connection_closed), client);
    g_hash_table_insert(app->clients, g_strdup(client->name), client);
    return TRUE;
}

static gboolean observer_tick(gpointer data)
{
    relay_client(data, "observer", TRUE);
    return G_SOURCE_CONTINUE;
}

static gboolean stop(gpointer data)
{
    g_main_loop_quit(((App *)data)->loop);
    return G_SOURCE_REMOVE;
}

int main(int argc, char **argv)
{
    App app = { 0 };
    GError *error = NULL;
    GDBusServer *server = NULL;
    if (argc < 3 || argc > 4
        || (!g_str_equal(argv[1], "--broker") && !g_str_equal(argv[1], "--relay"))
        || (argc == 4 && (!g_str_equal(argv[1], "--broker") || !g_str_equal(argv[3], "--admin")))
        || geteuid() != 0) {
        g_printerr("Usage (root): spaces-system-broker --broker|--relay SOCKET [--admin]\n");
        return 2;
    }
    app.broker = g_str_equal(argv[1], "--broker");
    app.admin = argc == 4;
    char *escaped = g_dbus_address_escape_value(argv[2]);
    app.address = g_strconcat("unix:path=", escaped, NULL);
    g_free(escaped);
    app.bus_address = g_dbus_address_get_for_bus_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
    app.clients = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, remove_client);
    app.loop = g_main_loop_new(NULL, FALSE);
    if (!app.bus_address) {
        goto failed;
    }
    if (app.broker) {
        GDBusAuthObserver *observer = g_dbus_auth_observer_new();
        g_signal_connect(observer, "authorize-authenticated-peer", G_CALLBACK(auth_peer), NULL);
        g_signal_connect(observer, "allow-mechanism", G_CALLBACK(auth_mechanism), NULL);
        char *guid = g_dbus_generate_guid();
        mode_t old = umask(0077);
        server = g_dbus_server_new_sync(
            app.address, G_DBUS_SERVER_FLAGS_NONE, guid, observer, NULL, &error);
        umask(old);
        g_free(guid);
        g_object_unref(observer);
        if (!server) {
            goto failed;
        }
        g_signal_connect(server, "new-connection", G_CALLBACK(new_peer), &app);
        g_dbus_server_start(server);
    } else {
        app.bus = connect_bus(&app, &error);
        if (!app.bus) {
            goto failed;
        }
        g_dbus_connection_set_exit_on_close(app.bus, TRUE);
        g_dbus_connection_add_filter(app.bus, guest_filter, &app, NULL);
        g_dbus_connection_signal_subscribe(app.bus, "org.freedesktop.DBus", "org.freedesktop.DBus",
            "NameOwnerChanged", "/org/freedesktop/DBus", NULL, G_DBUS_SIGNAL_FLAGS_NONE,
            guest_names, &app, NULL);
        observer_tick(&app);
        for (int index = 0; index < 3; index++) {
            app.names[index] = g_bus_own_name_on_connection(
                app.bus, services[index], G_BUS_NAME_OWNER_FLAGS_NONE, NULL, NULL, NULL, NULL);
        }
        g_timeout_add_seconds(1, observer_tick, &app);
    }
    g_unix_signal_add(SIGTERM, stop, &app);
    g_unix_signal_add(SIGINT, stop, &app);
    g_main_loop_run(app.loop);
    /* Process teardown closes outstanding calls, registrations and descriptors. */
    if (server) {
        g_dbus_server_stop(server);
    }
    return 0;
failed:
    g_printerr("spaces-system-broker: %s\n", error ? error->message : "startup failed");
    return 1;
}
