/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Session ownership and headless output lifetime belong to the common host.
 * KDE helpers keep private Wayland protocols and Qt out of this executable. */
typedef struct {
    char *session, *path, *wayland, *output_name, *display_error;
    GDBusConnection *system;
    guint properties_watch, removed_watch, owner_watch, timer;
    gboolean locked, lock_known, valid, query_ready, helper_stopping;
    guint clients, outputs, physical_outputs;
    GSubprocess *query, *helper;
    gint64 query_started, queried, helper_started, helper_stop_started;
} DesktopHost;
static DesktopHost desktop;
#ifndef SCU_SCREEN_QUERY
#define SCU_SCREEN_QUERY "/usr/bin/kscreen-doctor"
#endif
#ifndef SCU_VIRTUAL_MONITOR
#define SCU_VIRTUAL_MONITOR "/usr/bin/krfb-virtualmonitor"
#endif
static void die_with_parent(void);
static void desktop_maintain(guint clients);

static gboolean desktop_timer(gpointer unused)
{
    (void)unused;
    desktop_maintain(desktop.clients);
    return G_SOURCE_CONTINUE;
}

static GVariant *desktop_call(const char *path, const char *interface, const char *method,
    GVariant *args, GError **error)
{
    return bounded_call(desktop.system, "org.freedesktop.login1", path, interface, method,
        args, NULL, NULL, error);
}

static void desktop_properties(GDBusConnection *bus, const char *sender, const char *path,
    const char *interface, const char *signal, GVariant *args, gpointer unused)
{
    (void)bus; (void)sender; (void)path; (void)interface; (void)signal; (void)unused;
    const char *changed_interface;
    GVariant *changed, *invalidated;
    g_variant_get(args, "(&s@a{sv}@as)", &changed_interface, &changed, &invalidated);
    if (!strcmp(changed_interface, "org.freedesktop.login1.Session")) {
        gboolean locked, active;
        if (g_variant_lookup(changed, "LockedHint", "b", &locked)) {
            desktop.locked = locked;
            desktop.lock_known = TRUE;
        }
        if (g_variant_lookup(changed, "Active", "b", &active) && !active) desktop.valid = FALSE;
        GVariantIter iterator;
        const char *name;
        g_variant_iter_init(&iterator, invalidated);
        while (g_variant_iter_next(&iterator, "&s", &name)) {
            if (!strcmp(name, "LockedHint")) desktop.lock_known = FALSE;
            if (!strcmp(name, "Active")) desktop.valid = FALSE;
        }
    }
    g_variant_unref(changed); g_variant_unref(invalidated);
}

static void desktop_session_removed(GDBusConnection *bus, const char *sender, const char *path,
    const char *interface, const char *signal, GVariant *args, gpointer unused)
{
    (void)bus; (void)sender; (void)path; (void)interface; (void)signal; (void)unused;
    const char *session, *object;
    g_variant_get(args, "(&s&o)", &session, &object);
    if (!g_strcmp0(session, desktop.session)) desktop.valid = FALSE;
}

static void desktop_owner_changed(GDBusConnection *bus, const char *sender, const char *path,
    const char *interface, const char *signal, GVariant *args, gpointer unused)
{
    (void)bus; (void)sender; (void)path; (void)interface; (void)signal; (void)unused;
    const char *name, *old_owner, *new_owner;
    g_variant_get(args, "(&s&s&s)", &name, &old_owner, &new_owner);
    if (*old_owner) desktop.valid = FALSE;
}

static gboolean desktop_read_session(GError **error)
{
    GVariant *reply = desktop_call(desktop.path, "org.freedesktop.DBus.Properties", "GetAll",
        g_variant_new("(s)", "org.freedesktop.login1.Session"), error);
    if (!reply) return FALSE;
    GVariant *properties;
    g_variant_get(reply, "(@a{sv})", &properties);
    guint uid = G_MAXUINT;
    const char *user_path, *type = NULL, *class = NULL;
    gboolean active = FALSE, remote = TRUE, locked = FALSE;
    gboolean valid = g_variant_lookup(properties, "User", "(u&o)", &uid, &user_path) &&
        g_variant_lookup(properties, "Active", "b", &active) &&
        g_variant_lookup(properties, "Remote", "b", &remote) &&
        g_variant_lookup(properties, "Type", "&s", &type) &&
        g_variant_lookup(properties, "Class", "&s", &class) &&
        g_variant_lookup(properties, "LockedHint", "b", &locked) &&
        uid == getuid() && active && !remote && !strcmp(type, "wayland") && !strcmp(class, "user");
    if (valid) {
        desktop.locked = locked;
        desktop.lock_known = TRUE;
        desktop.valid = TRUE;
    } else g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_PERMISSION_DENIED,
        "SCU requires the selected user's active local Wayland session.");
    g_variant_unref(properties); g_variant_unref(reply);
    return valid;
}

static gboolean desktop_bind(const char *session, const char *wayland, GError **error)
{
    if (desktop.session) {
        if (!desktop.valid || strcmp(session, desktop.session) || strcmp(wayland, desktop.wayland)) {
            g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_FAILED, "The shared SCU host belongs to another desktop generation.");
            return FALSE;
        }
        return TRUE;
    }
    desktop.system = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, error);
    if (!desktop.system) return FALSE;
    GVariant *reply = desktop_call("/org/freedesktop/login1", "org.freedesktop.login1.Manager", "GetSession",
        g_variant_new("(s)", session), error);
    if (!reply) { g_clear_object(&desktop.system); return FALSE; }
    g_variant_get(reply, "(o)", &desktop.path);
    g_variant_unref(reply);
    if (!desktop_read_session(error)) {
        g_clear_pointer(&desktop.path, g_free); g_clear_object(&desktop.system);
        return FALSE;
    }
    desktop.session = g_strdup(session);
    desktop.wayland = g_strdup(wayland);
    desktop.output_name = g_strdup_printf("Spaces-SCU-%lu", (unsigned long)getpid());
    desktop.properties_watch = g_dbus_connection_signal_subscribe(desktop.system, "org.freedesktop.login1",
        "org.freedesktop.DBus.Properties", "PropertiesChanged", desktop.path, NULL,
        G_DBUS_SIGNAL_FLAGS_NONE, desktop_properties, NULL, NULL);
    desktop.removed_watch = g_dbus_connection_signal_subscribe(desktop.system, "org.freedesktop.login1",
        "org.freedesktop.login1.Manager", "SessionRemoved", "/org/freedesktop/login1", NULL,
        G_DBUS_SIGNAL_FLAGS_NONE, desktop_session_removed, NULL, NULL);
    desktop.owner_watch = g_dbus_connection_signal_subscribe(desktop.system, "org.freedesktop.DBus",
        "org.freedesktop.DBus", "NameOwnerChanged", "/org/freedesktop/DBus", "org.freedesktop.login1",
        G_DBUS_SIGNAL_FLAGS_NONE, desktop_owner_changed, NULL, NULL);
    desktop.timer = g_timeout_add(250, desktop_timer, NULL);
    return TRUE;
}

static void desktop_check_lock(void)
{
    if (!desktop.session) return; /* Direct private-fixture harness has no host login. */
    if (!desktop.valid || (desktop.system && g_dbus_connection_is_closed(desktop.system))) {
        g_atomic_int_set(&scu.stopping, 1);
        release_input();
    } else if (desktop.locked) release_input();
}

static gboolean desktop_input_allowed(void)
{
    if (desktop.session && desktop.locked) {
        scu_fail("desktop_locked", "The session locked during input. Inspect status before continuing; no action was replayed.");
        return FALSE;
    }
    return TRUE;
}

static void desktop_child_setup(gpointer unused)
{
    (void)unused;
    die_with_parent();
}

static GSubprocess *desktop_spawn(const char *const *argv, GSubprocessFlags flags, GError **error)
{
    GSubprocessLauncher *launcher = g_subprocess_launcher_new(flags);
    g_subprocess_launcher_setenv(launcher, "WAYLAND_DISPLAY", desktop.wayland, TRUE);
    g_subprocess_launcher_setenv(launcher, "QT_QPA_PLATFORM", "wayland", TRUE);
    g_subprocess_launcher_set_child_setup(launcher, desktop_child_setup, NULL, NULL);
    GSubprocess *process = g_subprocess_launcher_spawnv(launcher, argv, error);
    g_object_unref(launcher);
    return process;
}

static void desktop_helper_exited(GObject *source, GAsyncResult *result, gpointer unused)
{
    (void)unused;
    g_subprocess_wait_finish(G_SUBPROCESS(source), result, NULL);
    if (desktop.helper == G_SUBPROCESS(source)) {
        gboolean expected = desktop.helper_stopping;
        g_clear_object(&desktop.helper);
        desktop.helper_stopping = FALSE;
        desktop.queried = 0;
        if (!expected) {
            g_free(desktop.display_error);
            desktop.display_error = g_strdup("krfb-virtualmonitor exited before a usable fallback appeared.");
        }
    }
}

static void desktop_stop_helper(void)
{
    if (desktop.helper && !desktop.helper_stopping) {
        desktop.helper_stopping = TRUE;
        desktop.helper_stop_started = g_get_monotonic_time();
        g_subprocess_send_signal(desktop.helper, SIGTERM);
    }
}

static void desktop_query_done(GObject *source, GAsyncResult *result, gpointer unused)
{
    (void)unused;
    char *output = NULL;
    GError *error = NULL;
    gboolean success = g_subprocess_communicate_utf8_finish(G_SUBPROCESS(source), result, &output, NULL, &error) &&
        g_subprocess_get_successful(G_SUBPROCESS(source));
    yyjson_doc *doc = success && output && strlen(output) <= 1024u * 1024u ? yyjson_read(output, strlen(output), 0) : NULL;
    JsonValue *outputs = json_member(doc ? yyjson_doc_get_root(doc) : NULL, "outputs");
    desktop.query_ready = yyjson_is_arr(outputs);
    desktop.outputs = desktop.physical_outputs = 0;
    char *owned_name = g_strconcat("Virtual-", desktop.output_name, NULL);
    if (desktop.query_ready) {
        size_t i, n;
        JsonValue *item;
        yyjson_arr_foreach(outputs, i, n, item) {
            const char *name = json_string(item, "name");
            if (!name || !json_boolean(item, "connected")) continue;
            /* KScreen omits enabled for outputs without a disable capability
             * (including current KWin virtual outputs). Those are always on. */
            gboolean enabled = !json_member(item, "enabled") || json_boolean(item, "enabled");
            if (!enabled) continue;
            desktop.outputs++;
            if (strcmp(name, owned_name)) desktop.physical_outputs++;
        }
        if (desktop.outputs) g_clear_pointer(&desktop.display_error, g_free);
    } else {
        g_free(desktop.display_error);
        desktop.display_error = g_strdup(error ? error->message : "kscreen-doctor did not return a valid output inventory.");
    }
    g_free(owned_name);
    yyjson_doc_free(doc); g_free(output); g_clear_error(&error);
    g_clear_object(&desktop.query);
    desktop.queried = g_get_monotonic_time();
}

static void desktop_maintain(guint clients)
{
    desktop.clients = clients;
    if (!desktop.session) return;
    desktop_check_lock();
    gint64 now = g_get_monotonic_time();
    if (desktop.helper_stopping && now - desktop.helper_stop_started > G_USEC_PER_SEC)
        g_subprocess_force_exit(desktop.helper);
    if (desktop.query && now - desktop.query_started > 3 * G_USEC_PER_SEC)
        g_subprocess_force_exit(desktop.query);
    if (!clients || !desktop.valid) {
        desktop_stop_helper();
        if (desktop.query) g_subprocess_force_exit(desktop.query);
        desktop.query_ready = FALSE;
        desktop.queried = 0;
        return;
    }
    if (!desktop.query && now - desktop.queried > G_USEC_PER_SEC) {
        const char *argv[] = {SCU_SCREEN_QUERY, "-j", NULL};
        GError *error = NULL;
        desktop.query = desktop_spawn(argv, G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error);
        desktop.query_started = now;
        if (desktop.query) g_subprocess_communicate_utf8_async(desktop.query, NULL, NULL, desktop_query_done, NULL);
        else {
            g_free(desktop.display_error); desktop.display_error = g_strdup(error->message);
            desktop.queried = now;
        }
        g_clear_error(&error);
    }
    if (!desktop.query_ready) return;
    if (desktop.physical_outputs) {
        desktop_stop_helper();
    } else if (!desktop.outputs && !desktop.helper && now - desktop.helper_started > G_USEC_PER_SEC) {
        /* Without --password KRfb initializes the output but does not call
         * rfbInitServer(), so there is no VNC TCP listener. */
        const char *argv[] = {SCU_VIRTUAL_MONITOR, "--name", desktop.output_name,
            "--resolution", "1280x720", "--scale", "1", NULL};
        GError *error = NULL;
        desktop.helper = desktop_spawn(argv, G_SUBPROCESS_FLAGS_STDOUT_SILENCE, &error);
        desktop.helper_started = now;
        if (desktop.helper) g_subprocess_wait_async(desktop.helper, NULL, desktop_helper_exited, NULL);
        else { g_free(desktop.display_error); desktop.display_error = g_strdup(error->message); }
        g_clear_error(&error);
    }
}

static gboolean desktop_ready(void)
{
    if (!desktop.session) return TRUE;
    if (!desktop.valid) { scu_fail("desktop_session_unavailable", "The selected graphical login is no longer active."); return FALSE; }
    GError *error = NULL;
    if (g_get_monotonic_time() - desktop.queried > 200000) {
        desktop.query_ready = FALSE;
        desktop.queried = 0;
    }
    /* Refresh at the effect boundary as well as monitoring notifications. */
    if (!desktop_read_session(&error)) goto unavailable;
    GVariant *reply = bounded_call(session_bus, "org.kde.screensaver", "/ScreenSaver",
        "org.freedesktop.ScreenSaver", "GetActive", NULL, NULL, NULL, &error);
    gboolean screen_locked = desktop.locked;
    if (reply) { g_variant_get(reply, "(b)", &screen_locked); g_variant_unref(reply); }
    if (error) goto unavailable;
    if (desktop.locked || screen_locked) {
        gint64 saved_deadline = scu.deadline;
        scu.deadline = MIN(saved_deadline, g_get_monotonic_time() + 5 * G_USEC_PER_SEC);
        reply = desktop_call(desktop.path, "org.freedesktop.login1.Session", "Unlock", NULL, &error);
        if (reply) g_variant_unref(reply);
        while (reply && scu_tick()) {
            if (!desktop_read_session(&error)) break;
            GVariant *state = bounded_call(session_bus, "org.kde.screensaver", "/ScreenSaver",
                "org.freedesktop.ScreenSaver", "GetActive", NULL, NULL, NULL, &error);
            if (!state) break;
            g_variant_get(state, "(b)", &screen_locked); g_variant_unref(state);
            if (!desktop.locked && !screen_locked) break;
            if (!scu_pause(10)) break;
        }
        gboolean unlocked = reply && !error && !desktop.locked && !screen_locked && scu_tick();
        scu.deadline = saved_deadline;
        if (!unlocked) {
            scu_fail("desktop_unlock_failed", error ? error->message : "The session did not unlock within five seconds.");
            g_clear_error(&error);
            return FALSE;
        }
    }
    gint64 until = MIN(scu.deadline, g_get_monotonic_time() + 5 * G_USEC_PER_SEC);
    do {
        desktop_maintain(MAX(1, desktop.clients));
        if (desktop.query_ready && desktop.outputs && !desktop.helper_stopping) return TRUE;
        if (!scu_pause(10)) return FALSE;
    } while (g_get_monotonic_time() < until);
    scu_fail("virtual_display_unavailable", desktop.display_error ? desktop.display_error : "No usable desktop output became available.");
    return FALSE;
unavailable:
    scu_fail("desktop_session_unavailable", error ? error->message : "Cannot inspect the host session lock.");
    g_clear_error(&error);
    return FALSE;
}

static void desktop_close(void)
{
    if (desktop.timer) g_source_remove(desktop.timer);
    if (desktop.helper) {
        g_subprocess_force_exit(desktop.helper);
        g_subprocess_wait(desktop.helper, NULL, NULL);
    }
    if (desktop.query) {
        g_subprocess_force_exit(desktop.query);
        g_subprocess_wait(desktop.query, NULL, NULL);
    }
    if (desktop.system) {
        if (desktop.properties_watch) g_dbus_connection_signal_unsubscribe(desktop.system, desktop.properties_watch);
        if (desktop.removed_watch) g_dbus_connection_signal_unsubscribe(desktop.system, desktop.removed_watch);
        if (desktop.owner_watch) g_dbus_connection_signal_unsubscribe(desktop.system, desktop.owner_watch);
    }
}
