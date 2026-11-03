/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Host-only desktop catalog and scoped application dispatch. */
static char *application_line(const char *value)
{
    char *text = g_utf8_make_valid(value ? value : "", -1);
    for (char *p = text; *p; p++) if (g_ascii_iscntrl(*p)) *p = ' ';
    return text;
}

static void application_quoted(GString *text, const char *value)
{
    JsonResult *string = yyjson_mut_strcpy(scu.doc, value);
    append_result_value(text, string);
}

static gint application_compare(gconstpointer a, gconstpointer b)
{
    return g_strcmp0(g_app_info_get_id(G_APP_INFO(a)), g_app_info_get_id(G_APP_INFO(b)));
}

static JsonResult *search_applications(JsonValue *args)
{
    const char *pattern = json_string(args, "pattern");
    GError *error = NULL;
    GRegex *regex = pattern && *pattern ? g_regex_new(pattern, G_REGEX_CASELESS, 0, &error) : NULL;
    if (error) {
        scu_fail("invalid_pattern", error->message);
        g_error_free(error);
        return NULL;
    }
    GAppInfo *defaults[] = {
        g_app_info_get_default_for_uri_scheme("http"),
        g_app_info_get_default_for_uri_scheme("https"),
        g_app_info_get_default_for_uri_scheme("mailto"),
        g_app_info_get_default_for_type("inode/directory", FALSE),
    };
    const char *roles[] = {"browser", "browser", "mail", "files"};
    GList *apps = g_list_sort(g_app_info_get_all(), application_compare);
    GString *rows = g_string_new(NULL);
    guint installed = 0, matched = 0;
    gboolean valid = TRUE;
    for (GList *node = apps; node; node = node->next) {
        if (!scu_tick()) { valid = FALSE; break; }
        GAppInfo *app = node->data;
        if (!G_IS_DESKTOP_APP_INFO(app) || !g_app_info_get_id(app) ||
            g_desktop_app_info_get_is_hidden(G_DESKTOP_APP_INFO(app)) || !g_app_info_should_show(app)) continue;
        installed++;
        GDesktopAppInfo *desktop_app = G_DESKTOP_APP_INFO(app);
        const char *const *actions = g_desktop_app_info_list_actions(desktop_app);
        const char *const *keywords = g_desktop_app_info_get_keywords(desktop_app);
        const char *const *types = g_app_info_get_supported_types(app);
        char *action_list = actions ? g_strjoinv(";", (char **)actions) : g_strdup("");
        char *keyword_list = keywords ? g_strjoinv(";", (char **)keywords) : g_strdup("");
        char *type_list = types ? g_strjoinv(";", (char **)types) : g_strdup("");
        GString *default_list = g_string_new(NULL);
        gboolean browser = FALSE;
        for (guint i = 0; i < G_N_ELEMENTS(defaults); i++) {
            if (defaults[i] && g_app_info_equal(app, defaults[i]) && !(i == 1 && browser)) {
                if (default_list->len) g_string_append_c(default_list, ',');
                g_string_append(default_list, roles[i]);
                if (i < 2) browser = TRUE;
            }
        }
        char *default_search = g_strdup(default_list->str);
        g_strdelimit(default_search, ",", ';');
        const char *fields[] = {g_app_info_get_name(app), g_desktop_app_info_get_generic_name(desktop_app),
            g_app_info_get_description(app), g_app_info_get_id(app), keyword_list,
            g_desktop_app_info_get_categories(desktop_app), type_list, g_app_info_get_executable(app),
            action_list, default_search};
        GString *line = g_string_new(NULL);
        for (guint i = 0; i < G_N_ELEMENTS(fields); i++) {
            if (i) g_string_append_c(line, ':');
            char *field = application_line(fields[i]);
            g_string_append(line, field);
            g_free(field);
        }
        if (!regex || g_regex_match(regex, line->str, 0, NULL)) {
            matched++;
            char *name = application_line(g_app_info_get_display_name(app));
            char *generic = application_line(g_desktop_app_info_get_generic_name(desktop_app));
            g_string_append(rows, "  ");
            application_quoted(rows, g_app_info_get_id(app));
            g_string_append_printf(rows, ": %s", name);
            if (*generic) g_string_append_printf(rows, " (%s)", generic);
            gboolean uris = g_app_info_supports_uris(app), files = g_app_info_supports_files(app);
            if (uris || files) g_string_append_printf(rows, " [args=%s%s%s]", uris ? "URI" : "",
                uris && files ? "|" : "", files ? "File" : "");
            if (default_list->len) g_string_append_printf(rows, " [default=%s]", default_list->str);
            if (actions && *actions) {
                g_string_append(rows, " [actions=");
                for (guint i = 0; actions[i]; i++) {
                    if (i) g_string_append_c(rows, ',');
                    application_quoted(rows, actions[i]);
                }
                g_string_append_c(rows, ']');
            }
            g_string_append_c(rows, '\n');
            g_free(name); g_free(generic);
        }
        g_string_free(line, TRUE); g_string_free(default_list, TRUE);
        g_free(action_list); g_free(keyword_list); g_free(type_list); g_free(default_search);
    }
    g_list_free_full(apps, g_object_unref);
    for (guint i = 0; i < G_N_ELEMENTS(defaults); i++) g_clear_object(&defaults[i]);
    if (regex) g_regex_unref(regex);
    JsonResult *result = NULL;
    if (valid) {
        result = result_object();
        char *text = g_strdup_printf("Applications (%u / %u):\n%s", matched, installed, rows->str);
        result_string(result, "text", text);
        g_free(text);
    }
    g_string_free(rows, TRUE);
    return result;
}

/* Validate before creating any process. URI strings are data, never argv flags. */
static GDesktopAppInfo *application_resolve(JsonValue *args, char **uri, const char **action)
{
    const char *id = json_string(args, "application"), *arg = json_string(args, "arg");
    if (!id || !*id || strchr(id, '/') || !g_str_has_suffix(id, ".desktop")) {
        scu_fail("invalid_application", "Use an installed desktop ID including .desktop.");
        return NULL;
    }
    GDesktopAppInfo *app = g_desktop_app_info_new(id);
    if (!app || g_desktop_app_info_get_is_hidden(app)) {
        g_clear_object(&app);
        scu_fail("application_not_found", "The host desktop application is missing or Hidden.");
        return NULL;
    }
    if (!arg) return app;
    if (!*arg) goto invalid;
    if (g_str_has_prefix(arg, "action://")) {
        const char *const *actions = g_desktop_app_info_list_actions(app);
        for (guint i = 0; actions && actions[i]; i++) {
            if (!strcmp(actions[i], arg + 9)) { *action = arg + 9; return app; }
        }
        scu_fail("unknown_application_action", "Use an action ID returned by search_applications.");
        g_object_unref(app);
        return NULL;
    }
    GFile *file = NULL;
    if (g_path_is_absolute(arg)) file = g_file_new_for_path(arg);
    else {
        GUri *parsed = g_uri_parse(arg, G_URI_FLAGS_NONE, NULL);
        if (!parsed || !g_uri_get_scheme(parsed)) {
            if (parsed) g_uri_unref(parsed);
            goto invalid;
        }
        g_uri_unref(parsed);
        file = g_file_new_for_uri(arg);
    }
    gboolean supported = g_app_info_supports_uris(G_APP_INFO(app)) ||
        (g_app_info_supports_files(G_APP_INFO(app)) && g_file_is_native(file));
    if (!supported) {
        g_object_unref(file); g_object_unref(app);
        scu_fail("unsupported_application_argument", "The application does not declare support for this File/URI argument.");
        return NULL;
    }
    *uri = g_file_get_uri(file);
    g_object_unref(file);
    return app;
invalid:
    g_object_unref(app);
    scu_fail("invalid_arguments", "arg must be an absolute host path, an absolute URI, or action://<action-id>.");
    return NULL;
}

/* The worker waits on fd 3 until its independent scope is active. Applications
 * inherit that scope, not SCU's KillMode=control-group service. */
static int application_worker(void)
{
    signal(SIGPIPE, SIG_IGN);
    fcntl(3, F_SETFD, FD_CLOEXEC);
    GString *input = g_string_new(NULL);
    char buffer[4096];
    while (!strchr(input->str, '\n') && input->len < 1024 * 1024) {
        ssize_t size = read(3, buffer, sizeof(buffer));
        if (size < 0 && errno == EINTR) continue;
        if (size <= 0) { g_string_free(input, TRUE); return 1; }
        g_string_append_len(input, buffer, size);
    }
    yyjson_doc *request = yyjson_read(input->str, input->len, 0);
    g_string_free(input, TRUE);
    scu.doc = yyjson_mut_doc_new(NULL);
    JsonResult *result = result_object();
    char *uri = NULL;
    const char *action = NULL;
    GDesktopAppInfo *app = request ? application_resolve(yyjson_doc_get_root(request), &uri, &action) : NULL;
    gboolean ok = FALSE;
    GError *error = NULL;
    if (app) {
        GAppLaunchContext *context = g_app_launch_context_new();
        if (action) {
            g_desktop_app_info_launch_action(app, action, context);
            ok = TRUE;
        } else {
            GList *uris = uri ? g_list_append(NULL, uri) : NULL;
            ok = g_app_info_launch_uris(G_APP_INFO(app), uris, context, &error);
            g_list_free(uris);
        }
        /* Both APIs can queue D-Bus activation. Flush before this short-lived
         * worker exits. Action dispatch itself has no failure result. */
        GDBusConnection *bus = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, NULL);
        if (bus) { g_dbus_connection_flush_sync(bus, NULL, NULL); g_object_unref(bus); }
        g_object_unref(context); g_object_unref(app);
    }
    result_boolean(result, "ok", ok);
    if (!ok) {
        result_string(result, "code", scu.code ? scu.code : "application_launch_failed");
        result_string(result, "hint", error ? error->message : scu.hint ? scu.hint : "Invalid launch request.");
    }
    char *output = yyjson_mut_val_write(result, 0, NULL);
    if (output) {
        /* The response is small; handle short writes and interrupted syscalls. */
        size_t length = strlen(output), offset = 0;
        while (offset < length) {
            ssize_t sent = write(3, output + offset, length - offset);
            if (sent < 0 && errno == EINTR) continue;
            if (sent <= 0) break;
            offset += sent;
        }
    }
    free(output); g_free(uri); g_clear_error(&error);
    yyjson_doc_free(request); yyjson_mut_doc_free(scu.doc);
    g_free(scu.code); g_free(scu.hint);
    return ok ? 0 : 1;
}

static gboolean application_scope(GSubprocess *worker, GError **error)
{
    char *uuid = g_uuid_string_random();
    char *name = g_strdup_printf("app-spaces-scu-%s.scope", uuid);
    g_free(uuid);
    guint32 pid = (guint32)g_ascii_strtoull(g_subprocess_get_identifier(worker), NULL, 10);
    GVariantBuilder properties, pids, auxiliary;
    g_variant_builder_init(&properties, G_VARIANT_TYPE("a(sv)"));
    g_variant_builder_init(&pids, G_VARIANT_TYPE("au"));
    g_variant_builder_add(&pids, "u", pid);
    g_variant_builder_add(&properties, "(sv)", "PIDs", g_variant_builder_end(&pids));
    g_variant_builder_add(&properties, "(sv)", "Slice", g_variant_new_string("app.slice"));
    g_variant_builder_add(&properties, "(sv)", "Description", g_variant_new_string("SCU application launch"));
    g_variant_builder_add(&properties, "(sv)", "CollectMode", g_variant_new_string("inactive-or-failed"));
    g_variant_builder_init(&auxiliary, G_VARIANT_TYPE("a(sa(sv))"));
    GVariant *reply = bounded_call(session_bus, "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager", "StartTransientUnit",
        g_variant_new("(ssa(sv)a(sa(sv)))", name, "fail", &properties, &auxiliary), NULL, NULL, error);
    gboolean active = FALSE;
    if (reply) {
        g_variant_unref(reply);
        reply = bounded_call(session_bus, "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "GetUnit", g_variant_new("(s)", name), NULL, NULL, error);
        if (reply) {
            char *path = NULL;
            g_variant_get(reply, "(o)", &path);
            g_variant_unref(reply);
            while (scu_tick()) {
                reply = bounded_call(session_bus, "org.freedesktop.systemd1", path,
                    "org.freedesktop.DBus.Properties", "Get", g_variant_new("(ss)",
                        "org.freedesktop.systemd1.Unit", "ActiveState"), NULL, NULL, error);
                if (!reply) break;
                GVariant *value;
                g_variant_get(reply, "(v)", &value);
                const char *state = g_variant_get_string(value, NULL);
                active = !strcmp(state, "active");
                gboolean starting = !strcmp(state, "activating");
                g_variant_unref(value); g_variant_unref(reply);
                if (active || !starting || !scu_pause(10)) break;
            }
            g_free(path);
        }
    }
    g_free(name);
    return active;
}

static JsonResult *launch_application(JsonValue *args)
{
    char *uri = NULL;
    const char *action = NULL;
    GDesktopAppInfo *app = application_resolve(args, &uri, &action);
    if (!app) return NULL;
    g_object_unref(app); g_free(uri);
    char *request = yyjson_val_write(args, 0, NULL);
    if (!request || strlen(request) >= 1024 * 1024 - 1) {
        free(request);
        return scu_fail("invalid_arguments", "Application launch request exceeds its size limit.");
    }
    int sockets[2];
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sockets) < 0) {
        free(request);
        return scu_fail("application_launch_failed", "Could not create the application worker channel.");
    }
    GError *error = NULL;
    GSubprocessLauncher *launcher = g_subprocess_launcher_new(G_SUBPROCESS_FLAGS_STDOUT_SILENCE);
    if (desktop.wayland) g_subprocess_launcher_setenv(launcher, "WAYLAND_DISPLAY", desktop.wayland, TRUE);
    if (desktop.session) g_subprocess_launcher_setenv(launcher, "XDG_SESSION_ID", desktop.session, TRUE);
    g_subprocess_launcher_take_fd(launcher, sockets[1], 3);
    GSubprocess *worker = g_subprocess_launcher_spawn(launcher, &error, "/proc/self/exe", "--application-worker", NULL);
    g_object_unref(launcher);
    gboolean ready = worker && application_scope(worker, &error);
    if (ready) ready = require_computer_access() && desktop_refresh_lock();
    GString *response = g_string_new(NULL);
    if (ready) {
        fcntl(sockets[0], F_SETFL, O_NONBLOCK);
        GString *message = g_string_new(request);
        g_string_append_c(message, '\n');
        size_t sent = 0;
        while (sent < message->len && scu_tick()) {
            ssize_t size = send(sockets[0], message->str + sent, message->len - sent, MSG_NOSIGNAL);
            if (size > 0) sent += size;
            else if (size < 0 && (errno == EAGAIN || errno == EINTR)) {
                while (g_main_context_iteration(NULL, FALSE)) {}
                g_usleep(1000);
            } else break;
        }
        ready = sent == message->len;
        g_string_free(message, TRUE);
        while (ready && scu_tick()) {
            char buffer[4096];
            ssize_t size = read(sockets[0], buffer, sizeof(buffer));
            if (size > 0) {
                g_string_append_len(response, buffer, size);
                if (response->len > 65536) { ready = FALSE; break; }
            } else if (!size) break;
            else if (errno != EAGAIN && errno != EINTR) { ready = FALSE; break; }
            while (g_main_context_iteration(NULL, FALSE)) {}
            if (size < 0) g_usleep(1000);
        }
    }
    close(sockets[0]); free(request);
    yyjson_doc *reply = ready ? yyjson_read(response->str, response->len, 0) : NULL;
    JsonValue *value = reply ? yyjson_doc_get_root(reply) : NULL;
    JsonResult *result = NULL;
    if (value && json_boolean(value, "ok") && scu_tick()) {
        GString *text = g_string_new("Launch requested: ");
        application_quoted(text, json_string(args, "application"));
        if (json_string(args, "arg")) {
            g_string_append(text, " arg=");
            application_quoted(text, json_string(args, "arg"));
        }
        g_string_append_c(text, '\n');
        result = result_object(); result_string(result, "text", text->str);
        g_string_free(text, TRUE);
    } else if (!scu.code) {
        scu_fail(value && json_string(value, "code") ? json_string(value, "code") : "application_launch_failed",
            value && json_string(value, "hint") ? json_string(value, "hint") : error ? error->message :
            "The application worker or its independent user scope failed. Inspect state before retrying.");
    }
    if (worker) {
        if (!reply) g_subprocess_force_exit(worker);
        g_object_unref(worker);
    }
    yyjson_doc_free(reply); g_string_free(response, TRUE); g_clear_error(&error);
    return result;
}
