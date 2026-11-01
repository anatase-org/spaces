/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* One host-user desktop engine; each Space holds a revocable registration. */
#define _GNU_SOURCE
#define SCU_HOST
#include <atspi/atspi.h>
#include <dlfcn.h>
#include <gdk-pixbuf/gdk-pixbuf.h>
#include <gio/gunixfdlist.h>
#include <glib-unix.h>
#include <libei.h>
#include <linux/input-event-codes.h>
#include <stdint.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <xkbcommon/xkbcommon-keysyms.h>
#include <xkbcommon/xkbcommon.h>
#include "scu_mcp.h"
static JsonResult *scu_fail(const char *code, const char *hint);
static JsonResult *error_payload(void);
static void clear_tool_error(void);
static JsonResult *scu_action(
    const char *route, gboolean confirmed, const char *summary);
static gboolean scu_tick(void);
static gboolean scu_pause(unsigned ms);
static void *scu_library(const char *soname);
static JsonResult *scu_status(void);
static JsonResult *canvas_status(void);
static JsonResult *kwin_status(void);
static gboolean require_computer_access(void);
static void stop_atspi_worker(void);
static int run_atspi_worker(guint base);
static JsonResult *atspi_worker_call(const char *tool, JsonValue *args);
static gboolean inside_atspi_worker, retire_atspi_worker;
static void scu_invalidate(void);
static JsonResult *scu_atspi(const char *tool, JsonValue *args);
static void scu_atspi_close(void);
static void clear_clipboard_selection(void);
static JsonResult *scu_visual(const char *tool, JsonValue *args);
static void scu_visual_close(void);
static void poll_backend(void);
static gboolean desktop_ready(void);
static void desktop_check_lock(void);
static gboolean desktop_input_allowed(void);

static const char *libs[] = {"libatspi.so.0", "libei.so.1",
    "libxkbcommon.so.0", "libgdk_pixbuf-2.0.so.0", NULL};
static GDBusConnection *session_bus;
static void disconnect_input(void);
static gboolean host_geometry(GError **error);
static const char *current_geometry(void);
static int connect_input(GError **error);
static GVariant *host_call(const char *name, const char *path, const char *interface,
    const char *method, GVariant *args, GUnixFDList *input, GUnixFDList **output,
    GError **error);

static void *scu_library(const char *name)
{
    static GHashTable *loaded;
    if (loaded == NULL) {
        loaded = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    }
    void *handle = g_hash_table_lookup(loaded, name);
    if (handle == NULL) {
        handle = dlopen(name, RTLD_NOW | RTLD_LOCAL);
        if (handle != NULL) {
            g_hash_table_insert(loaded, g_strdup(name), handle);
        }
    }
    return handle;
}

static void scu_invalidate(void)
{
    scu_visual_close();
    scu_atspi_close();
}

/* Long operations dispatch transport callbacks at safe boundaries. Those may
 * cancel work, but only this thread executes desktop operations. */
static gboolean scu_tick(void)
{
    poll_backend();
    while (g_main_context_iteration(NULL, FALSE)) {
    }
    desktop_check_lock();
    if (g_atomic_int_get(&scu.cancelled) || g_atomic_int_get(&scu.stopping)) {
        scu_fail("cancelled",
            "Operation cancelled; completed actions were not rolled back.");
        return FALSE;
    }
    if (scu.deadline && g_get_monotonic_time() > scu.deadline) {
        scu_fail("timeout",
            "Operation exceeded its time limit; inspect state before retrying.");
        return FALSE;
    }
    return TRUE;
}

static gboolean scu_pause(unsigned ms)
{
    gint64 end = g_get_monotonic_time() + (gint64)ms * 1000;
    do {
        if (!scu_tick()) {
            return FALSE;
        }
        g_usleep(1000);
    } while (g_get_monotonic_time() < end);
    return TRUE;
}

static JsonResult *scu_action(
    const char *route, gboolean confirmed, const char *summary)
{
    JsonResult *r = result_object();
    result_string(r, "route", route);
    /* Keep routine submissions terse; only readback evidence needs prose. */
    if (confirmed) {
        JsonResult *e = result_array(), *v = result_object();
        result_string(v, "kind", "value_readback");
        result_string(v, "detail", summary);
        result_append(e, v);
        result_member(r, "evidence", e);
    }
    return r;
}

/* Status is an observation: it must establish a real canvas, not merely
 * report cached dimensions. Discovery remains available through MCP itself. */
static JsonResult *scu_status(void)
{
    scu.deadline = MIN(scu.deadline, g_get_monotonic_time() + 30 * G_USEC_PER_SEC);
    return kwin_status();
}

/* Host accessibility: discovery, snapshots and semantic actions. */
typedef struct {
    __typeof__(&atspi_init) init;
    __typeof__(&atspi_exit) exit;
    __typeof__(&atspi_set_timeout) set_timeout;
    __typeof__(&atspi_get_desktop) get_desktop;
    __typeof__(&atspi_event_listener_new) event_listener_new;
    __typeof__(&atspi_event_listener_register) event_listener_register;
    __typeof__(&atspi_event_listener_deregister) event_listener_deregister;
    __typeof__(&atspi_event_get_type) event_get_type;
    __typeof__(&atspi_accessible_get_attributes) accessible_get_attributes;
    __typeof__(&atspi_accessible_clear_cache) accessible_clear_cache;
    __typeof__(&atspi_accessible_get_child_count) accessible_get_child_count;
    __typeof__(&atspi_accessible_get_child_at_index) accessible_get_child_at_index;
    __typeof__(&atspi_accessible_get_name) accessible_get_name;
    __typeof__(&atspi_accessible_get_role) accessible_get_role;
    __typeof__(&atspi_accessible_get_role_name) accessible_get_role_name;
    __typeof__(&atspi_accessible_get_state_set) accessible_get_state_set;
    __typeof__(&atspi_state_set_contains) state_set_contains;
    __typeof__(&atspi_accessible_get_process_id) accessible_get_process_id;
    __typeof__(&atspi_accessible_get_component_iface) accessible_get_component_iface;
    __typeof__(&atspi_component_get_extents) component_get_extents;
    __typeof__(&atspi_accessible_get_text_iface) accessible_get_text_iface;
    __typeof__(&atspi_text_get_text) text_get_text;
    __typeof__(&atspi_text_get_caret_offset) text_get_caret_offset;
    __typeof__(&atspi_text_get_character_count) text_get_character_count;
    __typeof__(&atspi_text_get_n_selections) text_get_n_selections;
    __typeof__(&atspi_text_get_selection) text_get_selection;
    __typeof__(&atspi_text_set_selection) text_set_selection;
    __typeof__(&atspi_text_add_selection) text_add_selection;
    __typeof__(&atspi_accessible_get_editable_text_iface)
        accessible_get_editable_text_iface;
    __typeof__(&atspi_editable_text_set_text_contents) editable_text_set_text_contents;
    __typeof__(&atspi_editable_text_insert_text) editable_text_insert_text;
    __typeof__(&atspi_accessible_get_action_iface) accessible_get_action_iface;
    __typeof__(&atspi_action_get_n_actions) action_get_n_actions;
    __typeof__(&atspi_action_get_action_name) action_get_action_name;
    __typeof__(&atspi_action_do_action) action_do_action;
    __typeof__(&atspi_accessible_get_value_iface) accessible_get_value_iface;
    __typeof__(&atspi_value_get_current_value) value_get_current_value;
    __typeof__(&atspi_value_set_current_value) value_set_current_value;
} AtspiLibrary;

static AtspiLibrary atspi;
static gboolean library_loaded, atspi_initialized;
static AtspiEventListener *atspi_listener;
static const char atspi_event[] = "object:children-changed";
typedef struct {
    guint id, pid;
    AtspiAccessible *accessible;
    char *key, *app;
    gboolean seen;
} Window;
typedef struct {
    AtspiAccessible *accessible;
    guint window, pid;
} Element;
static GPtrArray *windows;
static GHashTable *elements;
static guint next_window = 1, snapshot;
static void free_window(gpointer p)
{
    Window *w = p;
    if (elements != NULL) {
        GHashTableIter iterator;
        gpointer value;
        g_hash_table_iter_init(&iterator, elements);
        while (g_hash_table_iter_next(&iterator, NULL, &value)) {
            if (((Element *)value)->window == w->id) {
                g_hash_table_iter_remove(&iterator);
            }
        }
    }
    g_object_unref(w->accessible);
    g_free(w->key);
    g_free(w->app);
    g_free(w);
}

static void free_element(gpointer p)
{
    Element *e = p;
    g_object_unref(e->accessible);
    g_free(e);
}

/* Keep backend failures reportable as tool errors. Never unload libatspi:
 * GLib retains its registered GTypes. */
static gboolean load_atspi(void)
{
    void *library;

    if (library_loaded) {
        return TRUE;
    }
    library = scu_library("libatspi.so.0");
    if (library == NULL) {
        return FALSE;
    }

#define LOAD_ATSPI(name)                                                               \
    do {                                                                               \
        *(void **)(&atspi.name) = dlsym(library, "atspi_" #name);                      \
        if (atspi.name == NULL)                                                        \
            return FALSE;                                                              \
    } while (0)
    LOAD_ATSPI(init);
    LOAD_ATSPI(exit);
    LOAD_ATSPI(set_timeout);
    LOAD_ATSPI(get_desktop);
    LOAD_ATSPI(event_listener_new);
    LOAD_ATSPI(event_listener_register);
    LOAD_ATSPI(event_listener_deregister);
    LOAD_ATSPI(event_get_type);
    LOAD_ATSPI(accessible_get_attributes);
    LOAD_ATSPI(accessible_clear_cache);
    LOAD_ATSPI(accessible_get_child_count);
    LOAD_ATSPI(accessible_get_child_at_index);
    LOAD_ATSPI(accessible_get_name);
    LOAD_ATSPI(accessible_get_role);
    LOAD_ATSPI(accessible_get_role_name);
    LOAD_ATSPI(accessible_get_state_set);
    LOAD_ATSPI(state_set_contains);
    LOAD_ATSPI(accessible_get_process_id);
    LOAD_ATSPI(accessible_get_component_iface);
    LOAD_ATSPI(component_get_extents);
    LOAD_ATSPI(accessible_get_text_iface);
    LOAD_ATSPI(text_get_text);
    LOAD_ATSPI(text_get_caret_offset);
    LOAD_ATSPI(text_get_character_count);
    LOAD_ATSPI(text_get_n_selections);
    LOAD_ATSPI(text_get_selection);
    LOAD_ATSPI(text_set_selection);
    LOAD_ATSPI(text_add_selection);
    LOAD_ATSPI(accessible_get_editable_text_iface);
    LOAD_ATSPI(editable_text_set_text_contents);
    LOAD_ATSPI(editable_text_insert_text);
    LOAD_ATSPI(accessible_get_action_iface);
    LOAD_ATSPI(action_get_n_actions);
    LOAD_ATSPI(action_get_action_name);
    LOAD_ATSPI(action_do_action);
    LOAD_ATSPI(accessible_get_value_iface);
    LOAD_ATSPI(value_get_current_value);
    LOAD_ATSPI(value_set_current_value);
#undef LOAD_ATSPI

    library_loaded = TRUE;
    return TRUE;
}

static void scu_atspi_close(void)
{
    if (!inside_atspi_worker) {
        stop_atspi_worker();
        return;
    }
    retire_atspi_worker = TRUE;
    clear_clipboard_selection();
    if (elements) {
        g_hash_table_remove_all(elements);
    }
    if (windows) {
        g_ptr_array_set_size(windows, 0);
    }
    if (atspi_initialized) {
        if (atspi_listener) {
            atspi.event_listener_deregister(atspi_listener, atspi_event, NULL);
            g_clear_object(&atspi_listener);
        }
        atspi.exit();
        atspi_initialized = FALSE;
    }
}

/* Registering a consumer lets the host launcher enable accessibility. Events
 * also keep libatspi's cache current; snapshots remain the observation oracle. */
static void atspi_changed(AtspiEvent *event, void *unused)
{
    (void)unused;
    g_boxed_free(atspi.event_get_type(), event);
}

static gboolean ensure_atspi(void)
{
    if (!load_atspi()) {
        scu_fail("missing_dependency",
            "Install the host AT-SPI library: at-spi2-core (Fedora/Arch), "
            "libatspi2.0-0 (Ubuntu).");
        return FALSE;
    }
    if (atspi_initialized) {
        return TRUE;
    }
    /* Resolve from the selected host session, never an inherited guest address. */
    GError *error = NULL;
    GDBusConnection *bus = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &error);
    GVariant *reply = bus ? g_dbus_connection_call_sync(bus, "org.a11y.Bus",
        "/org/a11y/bus", "org.a11y.Bus", "GetAddress", NULL,
        G_VARIANT_TYPE("(s)"), G_DBUS_CALL_FLAGS_NONE, 1500, NULL, &error) : NULL;
    if (!reply) {
        scu_fail("accessibility_unavailable", error ? error->message : "The host accessibility bus is unavailable.");
        g_clear_error(&error);
        g_clear_object(&bus);
        return FALSE;
    }
    const char *address;
    g_variant_get(reply, "(&s)", &address);
    g_setenv("AT_SPI_BUS_ADDRESS", address, TRUE);
    g_variant_unref(reply);
    g_object_unref(bus);
    atspi.set_timeout(700, 1500);
    if (atspi.init() != 0) {
        /* A failed initialization can leave libatspi's private bus cached. */
        retire_atspi_worker = TRUE;
        scu_fail("accessibility_unavailable",
            "Could not initialize the host AT-SPI bus.");
        return FALSE;
    }
    atspi_initialized = TRUE;
    atspi_listener = atspi.event_listener_new(atspi_changed, NULL, NULL);
    if (!atspi_listener ||
        !atspi.event_listener_register(atspi_listener, atspi_event, &error) || error) {
        scu_fail("accessibility_unavailable", error ? error->message :
            "Could not register the host accessibility consumer.");
        g_clear_error(&error);
        scu_atspi_close();
        return FALSE;
    }
    if (!windows) {
        windows = g_ptr_array_new_with_free_func(free_window);
    }
    if (!elements) {
        elements = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, free_element);
    }
    return TRUE;
}

static char *object_key(AtspiAccessible *o)
{
    AtspiObject *base = (AtspiObject *)o;
    return g_strdup_printf(
        "%s:%s", base->app ? base->app->bus_name : "", base->path ? base->path : "");
}

static gboolean accessible_has_state(AtspiAccessible *o, AtspiStateType type)
{
    AtspiStateSet *s = atspi.accessible_get_state_set(o);
    gboolean b = s && atspi.state_set_contains(s, type);
    if (s) {
        g_object_unref(s);
    }
    return b;
}

static JsonResult *accessible_frame(AtspiAccessible *o)
{
    AtspiComponent *c = atspi.accessible_get_component_iface(o);
    if (!c) {
        return yyjson_mut_null(scu.doc);
    }
    AtspiRect *r = atspi.component_get_extents(c, ATSPI_COORD_TYPE_WINDOW, NULL);
    g_object_unref(c);
    if (!r) {
        return yyjson_mut_null(scu.doc);
    }
    JsonResult *f = result_object();
    result_integer(f, "x", r->x);
    result_integer(f, "y", r->y);
    result_integer(f, "width", r->width);
    result_integer(f, "height", r->height);
    g_free(r);
    return f;
}

/* The AT-SPI bus name and object path identify a window more reliably than
 * its title. A local numeric ID survives rediscovery while that object lives. */
static Window *register_window(
    AtspiAccessible *accessible, guint process_id, const char *application_name)
{
    char *key = object_key(accessible);
    for (guint index = 0; index < windows->len; index++) {
        Window *window = g_ptr_array_index(windows, index);
        if (strcmp(window->key, key) == 0) {
            window->seen = TRUE;
            g_free(key);
            return window;
        }
    }

    Window *window = g_new0(Window, 1);
    window->id = next_window++;
    window->pid = process_id;
    window->accessible = g_object_ref(accessible);
    window->key = key;
    window->app = g_strdup(application_name);
    window->seen = TRUE;
    g_ptr_array_add(windows, window);
    return window;
}

static void discover_application_windows(AtspiAccessible *application)
{
    char *name = atspi.accessible_get_name(application, NULL);
    guint process_id = atspi.accessible_get_process_id(application, NULL);
    gint count = MIN(atspi.accessible_get_child_count(application, NULL), 128);

    for (gint index = 0; index < count; index++) {
        if (!scu_tick()) {
            break;
        }
        AtspiAccessible *window =
            atspi.accessible_get_child_at_index(application, index, NULL);
        if (window != NULL) {
            register_window(window, process_id, name);
            g_object_unref(window);
        }
    }
    g_free(name);
}

static gboolean discover_windows(void)
{
    AtspiAccessible *desktop = atspi.get_desktop(0);
    if (desktop == NULL) {
        scu_fail(
            "accessibility_unavailable", "Host accessibility registry is unavailable.");
        return FALSE;
    }
    for (guint index = 0; index < windows->len; index++) {
        Window *window = g_ptr_array_index(windows, index);
        window->seen = FALSE;
    }

    /* Discovery is bounded separately from tree traversal: an unresponsive or
     * unusually large desktop must not block every future MCP request. */
    GError *error = NULL;
    gint count = atspi.accessible_get_child_count(desktop, &error);
    if (error != NULL || count < 0) {
        scu_fail("accessibility_unavailable",
            error != NULL ? error->message
                          : "Host accessibility registry disconnected.");
        g_clear_error(&error);
        g_object_unref(desktop);
        scu_atspi_close();
        return FALSE;
    }
    count = MIN(count, 256);
    for (gint index = 0; index < count; index++) {
        if (!scu_tick()) {
            g_object_unref(desktop);
            return FALSE;
        }
        AtspiAccessible *application =
            atspi.accessible_get_child_at_index(desktop, index, NULL);
        if (application != NULL) {
            discover_application_windows(application);
            g_object_unref(application);
        }
    }
    g_object_unref(desktop);

    /* Remove backwards because GPtrArray shifts later entries on removal. */
    for (guint index = windows->len; index > 0; index--) {
        Window *window = g_ptr_array_index(windows, index - 1);
        if (!window->seen) {
            g_ptr_array_remove_index(windows, index - 1);
        }
    }
    return scu.code == NULL;
}

/* Check the native registry without walking every application. */
static JsonResult *check_accessibility_ready(void)
{
    AtspiAccessible *desktop = atspi.get_desktop(0);
    GError *error = NULL;
    gint count = desktop ? atspi.accessible_get_child_count(desktop, &error) : -1;
    if (desktop) {
        g_object_unref(desktop);
    }
    if (error || count < 0) {
        scu_fail("accessibility_unavailable",
            error ? error->message : "Host accessibility registry is unavailable.");
        g_clear_error(&error);
        scu_atspi_close();
        return NULL;
    }
    return result_object();
}

static JsonResult *list_apps(void)
{
    if (!discover_windows()) {
        return NULL;
    }
    JsonResult *result = result_object();
    JsonResult *apps = result_array();
    GHashTable *by_application =
        g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    for (guint index = 0; index < windows->len; index++) {
        Window *window = g_ptr_array_index(windows, index);
        /* Keep distinct processes separate even when application names match.
         * Preserve discovery order while grouping multiple windows per app. */
        char *key = g_strdup_printf("%u:%s", window->pid, window->app);
        JsonResult *application = g_hash_table_lookup(by_application, key);
        if (application == NULL) {
            application = result_object();
            result_integer(application, "pid", window->pid);
            result_string(application, "app_name", window->app);
            result_member(application, "windows", result_array());
            result_append(apps, application);
            g_hash_table_insert(by_application, key, application);
        } else {
            g_free(key);
        }
        JsonResult *item = result_object();
        char *title = atspi.accessible_get_name(window->accessible, NULL);
        result_integer(item, "window_id", window->id);
        result_string(item, "title", title);
        result_member(item, "bounds", accessible_frame(window->accessible));
        result_boolean(item, "shown",
            accessible_has_state(window->accessible, ATSPI_STATE_SHOWING));
        result_boolean(item, "active",
            accessible_has_state(window->accessible, ATSPI_STATE_ACTIVE));
        result_append(yyjson_mut_obj_get(application, "windows"), item);
        g_free(title);
    }
    g_hash_table_unref(by_application);
    result_member(result, "apps", apps);
    return result;
}

typedef struct {
    guint max, depth, count;
    gboolean truncated;
    Window *window;
    JsonResult *rows;
    GString *markdown;
    char snapshot[16];
} Walk;
/* Walk only a bounded subtree. Each token retains its accessible object, and
 * a new snapshot of the same window invalidates its previous tokens. */
static void append_accessible(
    Walk *state, AtspiAccessible *node, guint depth, gint parent)
{
    if (state->count >= state->max) {
        state->truncated = TRUE;
        return;
    }
    if (!scu_tick()) {
        return;
    }
    guint index = state->count++;
    JsonResult *row = result_object();
    char *name = atspi.accessible_get_name(node, NULL),
         *role = atspi.accessible_get_role_name(node, NULL);
    AtspiRole type = atspi.accessible_get_role(node, NULL);
    gboolean password = type == ATSPI_ROLE_PASSWORD_TEXT;
    char *token = g_strdup_printf("%s:%u", state->snapshot, index);
    Element *e = g_new0(Element, 1);
    e->accessible = g_object_ref(node);
    e->window = state->window->id;
    e->pid = state->window->pid;
    g_hash_table_insert(elements, g_strdup(token), e);
    result_integer(row, "element_index", index);
    result_string(row, "element_token", token);
    result_integer(row, "parent_index", parent);
    result_integer(row, "depth", depth);
    result_string(row, "role", role);
    result_string(row, "label", name);
    result_member(row, "frame", accessible_frame(node));
    result_boolean(row, "enabled", accessible_has_state(node, ATSPI_STATE_ENABLED));
    result_boolean(row, "focused", accessible_has_state(node, ATSPI_STATE_FOCUSED));
    result_boolean(row, "selected", accessible_has_state(node, ATSPI_STATE_SELECTED));
    result_boolean(row, "checked", accessible_has_state(node, ATSPI_STATE_CHECKED));
    AtspiText *t = password ? NULL : atspi.accessible_get_text_iface(node);
    char *value = NULL;
    if (t) {
        gint length = atspi.text_get_character_count(t, NULL);
        if (length >= 0) {
            value = atspi.text_get_text(t, 0, MIN(length, 4096), NULL);
        }
        g_object_unref(t);
    }
    if (!password) {
        result_string(row, "value", value);
    } else {
        result_boolean(row, "protected", TRUE);
    }
    JsonResult *actions = result_array();
    AtspiAction *ac = atspi.accessible_get_action_iface(node);
    if (ac) {
        gint n = atspi.action_get_n_actions(ac, NULL);
        for (gint i = 0; i < MIN(n, 32); i++) {
            char *s = atspi.action_get_action_name(ac, i, NULL);
            if (s) {
                result_append(actions, yyjson_mut_strcpy(scu.doc, s));
                g_free(s);
            }
        }
        g_object_unref(ac);
    }
    result_member(row, "actions", actions);
    g_string_append_printf(state->markdown, "%*s[%u] %s %s%s%s\n", (int)depth * 2, "",
        index, role ? role : "unknown", name ? name : "", value ? " = " : "",
        value ? value : "");
    result_append(state->rows, row);
    g_free(token);
    g_free(name);
    g_free(role);
    g_free(value);
    gint n = atspi.accessible_get_child_count(node, NULL);
    if (depth >= state->depth) {
        if (n > 0) {
            state->truncated = TRUE;
        }
        return;
    }
    for (gint i = 0; i < MIN(n, 2000); i++) {
        if (state->count >= state->max) {
            state->truncated = TRUE;
            break;
        }
        AtspiAccessible *child = atspi.accessible_get_child_at_index(node, i, NULL);
        if (child) {
            append_accessible(state, child, depth + 1, (gint)index);
            g_object_unref(child);
        }
        if (scu.code) {
            break;
        }
    }
}

/* Chromium enables its full tree when a consumer requests frame attributes.
 * Do this through the standard toolkit API, then allow asynchronous children
 * to arrive. A reported child count alone can describe null placeholders. */
static gboolean prepare_accessible_window(AtspiAccessible *window)
{
    atspi.accessible_clear_cache(window);
    GError *error = NULL;
    GHashTable *attributes = atspi.accessible_get_attributes(window, &error);
    gboolean supported = attributes != NULL && error == NULL;
    if (attributes) g_hash_table_unref(attributes);
    g_clear_error(&error);
    if (!supported) return scu_tick();

    gint64 until = g_get_monotonic_time() + 2 * G_USEC_PER_SEC;
    if (scu.deadline) until = MIN(until, scu.deadline);
    do {
        if (!scu_tick()) return FALSE;
        atspi.accessible_clear_cache(window);
        gint count = MIN(atspi.accessible_get_child_count(window, NULL), 128);
        for (gint i = 0; i < count && g_get_monotonic_time() < until; i++) {
            if (!scu_tick()) return FALSE;
            AtspiAccessible *child = atspi.accessible_get_child_at_index(window, i, NULL);
            if (child) {
                g_object_unref(child);
                return TRUE;
            }
        }
        gint64 remaining = until - g_get_monotonic_time();
        if (remaining <= 0) break;
        if (!scu_pause((unsigned)MIN(50, (remaining + 999) / 1000))) return FALSE;
    } while (g_get_monotonic_time() < until);
    return scu_tick();
}

/* Snapshot IDs distinguish row indices from different reads. Never resolve a
 * stale token by label: a similarly named control may have a different effect. */
static JsonResult *read_window_state(JsonValue *args)
{
    if (!discover_windows()) {
        return NULL;
    }
    Window *w = NULL;
    for (guint i = 0; i < windows->len; i++) {
        Window *v = g_ptr_array_index(windows, i);
        if (v->id == json_number(args, "window_id", -1) &&
            v->pid == json_number(args, "pid", -1)) {
            w = v;
            break;
        }
    }
    if (!w) {
        return scu_fail("window_unavailable",
            "Window is absent or belongs to a different process. Call status "
            "again.");
    }
    if (!prepare_accessible_window(w->accessible)) return NULL;
    GHashTableIter it;
    gpointer v;
    g_hash_table_iter_init(&it, elements);
    while (g_hash_table_iter_next(&it, NULL, &v)) {
        if (((Element *)v)->window == w->id) {
            g_hash_table_iter_remove(&it);
        }
    }
    Walk state = {.max = (guint)json_number(args, "max_elements", 250),
        .depth = (guint)json_number(args, "max_depth", 12),
        .window = w,
        .rows = result_array(),
        .markdown = g_string_new(NULL)};
    g_snprintf(state.snapshot, sizeof(state.snapshot), "s%08x", ++snapshot);
    append_accessible(&state, w->accessible, 0, -1);
    if (scu.code) {
        g_string_free(state.markdown, TRUE);
        return NULL;
    }
    JsonResult *r = result_object();
    result_string(r, "snapshot_id", state.snapshot);
    result_integer(r, "pid", w->pid);
    result_integer(r, "window_id", w->id);
    result_boolean(r, "truncated", state.truncated);
    const char *format = json_string(args, "tree_format");
    if (!format || strcmp(format, "elements")) {
        result_string(r, "tree_markdown", state.markdown->str);
    }
    if (format && strcmp(format, "markdown")) {
        result_member(r, "elements", state.rows);
    }
    g_string_free(state.markdown, TRUE);
    return scu.code ? NULL : r;
}

/* A semantic click invokes an advertised action. It must not silently fall
 * back to moving the pointer or raising a window when no such action exists. */
static JsonResult *invoke_accessible_action(
    AtspiAccessible *node, const char *tool, JsonValue *arguments)
{
    GError *error = NULL;
    gboolean completed = FALSE;
    gboolean context_menu = strcmp(tool, "right_click") == 0 ||
                            g_strcmp0(json_string(arguments, "button"), "right") == 0;

    if (json_number(arguments, "count", 1) != 1 ||
        g_strcmp0(json_string(arguments, "button"), "middle") == 0) {
        return scu_fail("unsupported_feature",
            "Repeated or middle clicks require desktop pixel coordinates.");
    }

    AtspiAction *action = atspi.accessible_get_action_iface(node);
    if (action != NULL) {
        gint count = MIN(atspi.action_get_n_actions(action, NULL), 32);
        for (gint index = 0; index < count; index++) {
            char *name = atspi.action_get_action_name(action, index, NULL);
            gboolean matches = FALSE;
            if (name != NULL) {
                matches =
                    context_menu
                        ? (strstr(name, "menu") != NULL || strstr(name, "Menu") != NULL)
                        : (g_ascii_strcasecmp(name, "click") == 0 ||
                              g_ascii_strcasecmp(name, "press") == 0 ||
                              g_ascii_strcasecmp(name, "activate") == 0 ||
                              g_ascii_strcasecmp(name, "toggle") == 0);
            }
            g_free(name);
            if (matches) {
                completed = atspi.action_do_action(action, index, &error);
                break;
            }
        }
        g_object_unref(action);
    }
    if (!completed) {
        scu_fail("accessible_action_failed",
            error != NULL ? error->message
                          : "The element does not expose this action.");
        g_clear_error(&error);
        return NULL;
    }
    return scu_action("accessibility", FALSE,
        "Accessible action submitted; inspect the resulting state.");
}

static JsonResult *set_accessible_number(AtspiAccessible *node, const char *text)
{
    GError *error = NULL;
    AtspiValue *value = atspi.accessible_get_value_iface(node);
    char *end = NULL;
    double number = g_ascii_strtod(text, &end);
    gboolean completed = FALSE;
    gboolean confirmed = FALSE;

    if (value != NULL && end != text && *end == '\0' && isfinite(number)) {
        completed = atspi.value_set_current_value(value, number, &error);
        confirmed = completed && atspi.value_get_current_value(value, NULL) == number;
    }
    if (value != NULL) {
        g_object_unref(value);
    }
    if (!completed) {
        scu_fail("accessible_action_failed",
            error != NULL ? error->message
                          : "The element does not accept the requested numeric value.");
        g_clear_error(&error);
        return NULL;
    }
    return scu_action("accessibility", confirmed,
        confirmed ? "Accessible numeric value read back as requested."
                  : "Accessible numeric value submitted; readback was unavailable.");
}

/* AT-SPI caret positions count Unicode characters, not UTF-8 bytes. Build the
 * expected complete value before inserting, so readback can confirm the edit. */
static char *expected_inserted_text(AtspiText *text, gint caret, const char *inserted)
{
    gint length = atspi.text_get_character_count(text, NULL);
    if (length < 0 || length > 65536 || caret < 0 || caret > length) {
        return NULL;
    }
    char *before = atspi.text_get_text(text, 0, length, NULL);
    if (before == NULL || !g_utf8_validate(before, -1, NULL) ||
        caret > g_utf8_strlen(before, -1)) {
        g_free(before);
        return NULL;
    }
    const char *suffix = g_utf8_offset_to_pointer(before, caret);
    char *expected =
        g_strdup_printf("%.*s%s%s", (int)(suffix - before), before, inserted, suffix);
    g_free(before);
    return expected;
}

static JsonResult *edit_accessible_text(
    AtspiAccessible *node, const char *tool, JsonValue *arguments)
{
    if (atspi.accessible_get_role(node, NULL) == ATSPI_ROLE_PASSWORD_TEXT) {
        return scu_fail("protected_element",
            "Password elements are excluded from semantic value operations.");
    }

    gboolean replace = strcmp(tool, "set_value") == 0;
    const char *contents = json_string(arguments, replace ? "value" : "text");
    AtspiEditableText *editable = atspi.accessible_get_editable_text_iface(node);
    AtspiText *text = atspi.accessible_get_text_iface(node);
    GError *error = NULL;
    char *expected = NULL;
    gboolean completed = FALSE;
    gboolean confirmed = FALSE;

    if (editable == NULL || text == NULL) {
        if (editable != NULL) {
            g_object_unref(editable);
        }
        if (text != NULL) {
            g_object_unref(text);
        }
        if (replace) {
            return set_accessible_number(node, contents);
        }
        return scu_fail(
            "accessible_action_failed", "The element does not expose editable text.");
    }

    if (replace) {
        expected = g_strdup(contents);
        completed = atspi.editable_text_set_text_contents(editable, contents, &error);
    } else if (atspi.text_get_n_selections(text, NULL) > 0) {
        scu_fail("unsupported_feature",
            "Semantic insertion with selected text is unsupported; "
            "use set_value or foreground typing.");
    } else {
        gint caret = atspi.text_get_caret_offset(text, NULL);
        if (caret >= 0) {
            expected = expected_inserted_text(text, caret, contents);
            completed = atspi.editable_text_insert_text(
                editable, caret, contents, (gint)g_utf8_strlen(contents, -1), &error);
        }
    }

    /* A successful setter only proves submission. Read the resulting value
     * separately, and bound that read to the expected text plus one character. */
    if (completed && expected != NULL) {
        gint expected_length = (gint)g_utf8_strlen(expected, -1);
        char *readback = atspi.text_get_text(text, 0, expected_length + 1, NULL);
        confirmed = g_strcmp0(readback, expected) == 0;
        g_free(readback);
    }
    g_free(expected);
    g_object_unref(editable);
    g_object_unref(text);

    if (!completed) {
        scu_fail("accessible_action_failed",
            error != NULL ? error->message
                          : "The accessible text edit could not be submitted.");
        g_clear_error(&error);
        return NULL;
    }
    return scu_action("accessibility", confirmed,
        confirmed
            ? "Accessible text value read back with the expected contents."
            : "Accessible text edit submitted; complete readback was unavailable.");
}

/* Tokens resolve only within this process and the snapshot that created them.
 * Retain the object while acting; a failed/defunct target is never retargeted. */
static JsonResult *operate_on_element(const char *tool, JsonValue *arguments)
{
    const char *token = json_string(arguments, "element_token");
    Element *element = token != NULL ? g_hash_table_lookup(elements, token) : NULL;
    if (element == NULL) {
        return scu_fail("stale_element",
            "Element token is absent or stale. Read a window tree with computer "
            "observe:{kind:window,pid,window_id} and use its new token.");
    }

    AtspiAccessible *node = g_object_ref(element->accessible);
    JsonResult *result;
    if (accessible_has_state(node, ATSPI_STATE_DEFUNCT)) {
        result = scu_fail("stale_element",
            "Accessible application or element disappeared. Read window state again.");
    } else if (strcmp(tool, "click") == 0 || strcmp(tool, "right_click") == 0) {
        result = invoke_accessible_action(node, tool, arguments);
    } else if (strcmp(tool, "set_value") == 0 || strcmp(tool, "type_text") == 0) {
        result = edit_accessible_text(node, tool, arguments);
    } else {
        result =
            scu_fail("unsupported_feature", "This action cannot use an accessibility "
                                            "token. Use desktop foreground input.");
    }
    g_object_unref(node);
    return result;
}

/* Klipper writes PRIMARY as well as CLIPBOARD. Preserve a focused text
 * selection through that ownership change, without modifying its contents. */
static AtspiAccessible *clipboard_selection_node;
static AtspiText *clipboard_selection_text;
static char *clipboard_selection_value;
static gint clipboard_selection_start, clipboard_selection_end;

static void clear_clipboard_selection(void)
{
    g_clear_object(&clipboard_selection_node);
    g_clear_object(&clipboard_selection_text);
    g_clear_pointer(&clipboard_selection_value, g_free);
}

static AtspiAccessible *find_focused_node(AtspiAccessible *node, guint depth, guint *remaining)
{
    if (*remaining == 0 || depth > 12 || !scu_tick()) {
        return NULL;
    }
    (*remaining)--;
    if (accessible_has_state(node, ATSPI_STATE_FOCUSED)) {
        return g_object_ref(node);
    }
    gint children = MIN(atspi.accessible_get_child_count(node, NULL), 250);
    for (gint i = 0; i < children && *remaining; i++) {
        AtspiAccessible *child = atspi.accessible_get_child_at_index(node, i, NULL);
        if (!child) {
            continue;
        }
        AtspiAccessible *focused = find_focused_node(child, depth + 1, remaining);
        g_object_unref(child);
        if (focused) {
            return focused;
        }
    }
    return NULL;
}

static JsonResult *clipboard_selection(const char *tool, JsonValue *args)
{
    JsonResult *result = result_object();
    if (!strcmp(tool, "save_selection")) {
        clear_clipboard_selection();
        if (!discover_windows()) {
            return NULL;
        }
        Window *selected = NULL;
        guint matches = 0;
        for (guint i = 0; i < windows->len; i++) {
            Window *window = g_ptr_array_index(windows, i);
            char *title = atspi.accessible_get_name(window->accessible, NULL);
            if (window->pid == json_number(args, "pid", -1) &&
                !g_strcmp0(title, json_string(args, "title"))) {
                selected = window;
                matches++;
            }
            g_free(title);
        }
        if (matches == 1) {
            guint remaining = 250;
            clipboard_selection_node = find_focused_node(selected->accessible, 0, &remaining);
        }
        if (clipboard_selection_node &&
            atspi.accessible_get_role(clipboard_selection_node, NULL) != ATSPI_ROLE_PASSWORD_TEXT) {
            clipboard_selection_text = atspi.accessible_get_text_iface(clipboard_selection_node);
        }
        gboolean saved = FALSE;
        if (clipboard_selection_text && atspi.text_get_n_selections(clipboard_selection_text, NULL) == 1) {
            gint length = atspi.text_get_character_count(clipboard_selection_text, NULL);
            AtspiRange *range = atspi.text_get_selection(clipboard_selection_text, 0, NULL);
            if (range && length >= 0 && length <= 65536 && range->start_offset >= 0 &&
                range->end_offset > range->start_offset && range->end_offset <= length) {
                clipboard_selection_start = range->start_offset;
                clipboard_selection_end = range->end_offset;
                clipboard_selection_value = atspi.text_get_text(clipboard_selection_text, 0, length, NULL);
                saved = clipboard_selection_value != NULL;
            }
            g_free(range);
        }
        if (!saved) {
            clear_clipboard_selection();
        }
        result_boolean(result, "saved", saved);
        return scu.code ? NULL : result;
    }
    if (!clipboard_selection_node || !clipboard_selection_text ||
        !accessible_has_state(clipboard_selection_node, ATSPI_STATE_FOCUSED)) {
        clear_clipboard_selection();
        return scu_fail("clipboard_selection_changed", "Clipboard/PRIMARY changed, but text focus changed before paste. No paste was sent.");
    }
    gint length = atspi.text_get_character_count(clipboard_selection_text, NULL);
    char *value = length >= 0 && length <= 65536
        ? atspi.text_get_text(clipboard_selection_text, 0, length, NULL) : NULL;
    gboolean same = value && !strcmp(value, clipboard_selection_value);
    g_free(value);
    gboolean restored = FALSE;
    gboolean verify = !strcmp(tool, "verify_selection");
    if (same && verify) {
        AtspiRange *range = atspi.text_get_n_selections(clipboard_selection_text, NULL) == 1
            ? atspi.text_get_selection(clipboard_selection_text, 0, NULL) : NULL;
        restored = range && range->start_offset == clipboard_selection_start &&
            range->end_offset == clipboard_selection_end;
        g_free(range);
    } else if (same) {
        restored = atspi.text_get_n_selections(clipboard_selection_text, NULL) > 0
            ? atspi.text_set_selection(clipboard_selection_text, 0,
                clipboard_selection_start, clipboard_selection_end, NULL)
            : atspi.text_add_selection(clipboard_selection_text,
                clipboard_selection_start, clipboard_selection_end, NULL);
    }
    if (verify || !restored) {
        clear_clipboard_selection();
    }
    if (!restored) {
        return scu_fail("clipboard_selection_changed", "Clipboard/PRIMARY changed, but the text selection could not be restored safely. No paste was sent. Use set_value for accessible text replacement.");
    }
    result_boolean(result, "restored", TRUE);
    return result;
}

static JsonResult *scu_atspi(const char *tool, JsonValue *args)
{
    if (!scu_tick()) {
        return NULL;
    }
    if (!inside_atspi_worker) {
        if (!require_computer_access()) {
            return NULL;
        }
        return atspi_worker_call(tool, args);
    }
    if (!ensure_atspi()) {
        return NULL;
    }
    if (!strcmp(tool, "check_ready")) {
        return check_accessibility_ready();
    }
    if (!strcmp(tool, "list_apps")) {
        return list_apps();
    }
    if (!strcmp(tool, "save_selection") || !strcmp(tool, "restore_selection") ||
        !strcmp(tool, "verify_selection")) {
        return clipboard_selection(tool, args);
    }
    if (!strcmp(tool, "window_state")) {
        return read_window_state(args);
    }
    return operate_on_element(tool, args);
}

/* libatspi documents atspi_exit() as an exit-only operation: its process
 * globals retain disposed applications across reinitialization. Keep it in a
 * private child of this executable so revocation/reconnection discards those
 * caches without restarting the MCP transport or the visual session.
 *
 * The parent owns cancellation, deadlines and permission checks. The child has
 * no additional authority and inherits no host-service/input descriptors. Its private
 * pipe protocol carries semantic requests/results only, never host shell work. */
static GPid atspi_worker_pid;
static int atspi_worker_input = -1, atspi_worker_output = -1;
static guint atspi_worker_base = 1;

static void stop_atspi_worker(void)
{
    if (atspi_worker_input >= 0) {
        close(atspi_worker_input);
        atspi_worker_input = -1;
    }
    if (atspi_worker_output >= 0) {
        close(atspi_worker_output);
        atspi_worker_output = -1;
    }
    if (atspi_worker_pid != 0) {
        /* SIGTERM has its default disposition in the semantic child. */
        kill(atspi_worker_pid, SIGTERM);
        waitpid(atspi_worker_pid, NULL, 0);
        g_spawn_close_pid(atspi_worker_pid);
        atspi_worker_pid = 0;
    }
}

static gboolean start_atspi_worker(void)
{
    if (atspi_worker_pid != 0) {
        return TRUE;
    }
    if (atspi_worker_base > G_MAXUINT - 0x100000) {
        scu_fail("handle_limit", "Restart scu to allocate a fresh window ID range.");
        return FALSE;
    }
    char *base = g_strdup_printf("%u", atspi_worker_base);
    char *argv[] = {"/proc/self/exe", "--atspi-worker", base, NULL};
    GError *error = NULL;
    gboolean ok = g_spawn_async_with_pipes(NULL, argv, NULL, G_SPAWN_DO_NOT_REAP_CHILD,
        NULL, NULL, &atspi_worker_pid, &atspi_worker_input, &atspi_worker_output, NULL,
        &error);
    g_free(base);
    atspi_worker_base += 0x100000;
    if (!ok) {
        scu_fail("accessibility_unavailable",
            error != NULL ? error->message : "Could not start the semantic adapter.");
        g_clear_error(&error);
        return FALSE;
    }
    fcntl(atspi_worker_input, F_SETFL, O_NONBLOCK);
    fcntl(atspi_worker_output, F_SETFL, O_NONBLOCK);
    return TRUE;
}

static JsonResult *atspi_worker_call(const char *tool, JsonValue *args)
{
    if (!start_atspi_worker()) {
        return NULL;
    }
    JsonResult *request = result_object();
    result_string(request, "tool", tool);
    result_member(request, "args",
        args != NULL ? yyjson_val_mut_copy(scu.doc, args) : result_object());
    char *serialized = yyjson_mut_val_write(request, 0, NULL);
    char *line = g_strconcat(serialized, "\n", NULL);
    free(serialized);
    gsize length = strlen(line), offset = 0;
    GString *response = g_string_new(NULL);
    gboolean complete = FALSE;
    while (!complete) {
        if (!scu_tick() || !require_computer_access()) {
            break;
        }
        if (offset < length) {
            ssize_t count = write(atspi_worker_input, line + offset, length - offset);
            if (count > 0) {
                offset += count;
            } else if (count < 0 && errno != EAGAIN && errno != EINTR) {
                break;
            }
        }
        char buffer[8192];
        ssize_t count = read(atspi_worker_output, buffer, sizeof(buffer));
        if (count > 0) {
            g_string_append_len(response, buffer, count);
            complete = response->str[response->len - 1] == '\n';
            if (response->len > 32 * 1024 * 1024) {
                break;
            }
        } else if (count == 0 || (errno != EAGAIN && errno != EINTR)) {
            break;
        }
        if (!complete) {
            g_usleep(1000);
        }
    }
    g_free(line);
    yyjson_doc *document =
        complete ? yyjson_read(response->str, response->len, 0) : NULL;
    JsonValue *root = document != NULL ? yyjson_doc_get_root(document) : NULL;
    JsonValue *output = json_member(root, "output");
    JsonResult *result = output != NULL ? yyjson_val_mut_copy(scu.doc, output) : NULL;
    if (document == NULL) {
        scu_fail("accessibility_unavailable",
            "Semantic adapter disconnected; no action was retried.");
        stop_atspi_worker();
    } else {
        JsonValue *error = json_member(root, "error");
        if (error != NULL) {
            scu_fail(json_string(error, "code"), json_string(error, "hint"));
        }
        if (json_boolean(root, "retire")) {
            stop_atspi_worker();
        }
    }
    yyjson_doc_free(document);
    g_string_free(response, TRUE);
    return result;
}

typedef struct {
    GString *input;
    GQueue lines;
    guint source;
    gboolean closed;
} AtspiInput;

/* Nested libatspi calls dispatch this context too. Only frame requests here;
 * the outer worker loop alone executes them. The parent sends one at a time. */
static gboolean atspi_read_input(gint fd, GIOCondition condition, gpointer data)
{
    AtspiInput *input = data;
    char buffer[8192];
    for (guint turn = 0; turn < 16; turn++) {
        ssize_t count = read(fd, buffer, sizeof(buffer));
        if (count < 0 && errno == EINTR) continue;
        if (count < 0 && errno == EAGAIN) break;
        if (count <= 0) { input->closed = TRUE; break; }
        for (ssize_t i = 0; i < count; i++) {
            if (buffer[i] == '\n') {
                if (g_queue_get_length(&input->lines) >= 4) {
                    input->closed = TRUE;
                    break;
                }
                g_queue_push_tail(&input->lines, g_strdup(input->input->str));
                g_string_truncate(input->input, 0);
            } else if (input->input->len < 2u * 1024u * 1024u) {
                g_string_append_c(input->input, buffer[i]);
            } else {
                input->closed = TRUE;
                break;
            }
        }
        if (input->closed) break;
    }
    if (condition & (G_IO_ERR | G_IO_NVAL)) input->closed = TRUE;
    if (!input->closed) return G_SOURCE_CONTINUE;
    input->source = 0;
    return G_SOURCE_REMOVE;
}

static int run_atspi_worker(guint base)
{
    inside_atspi_worker = TRUE;
    next_window = snapshot = base;
    signal(SIGPIPE, SIG_IGN);
    AtspiInput input = {.input = g_string_new(NULL), .lines = G_QUEUE_INIT};
    fcntl(STDIN_FILENO, F_SETFL, fcntl(STDIN_FILENO, F_GETFL) | O_NONBLOCK);
    input.source = g_unix_fd_add(STDIN_FILENO, G_IO_IN | G_IO_HUP | G_IO_ERR,
        atspi_read_input, &input);
    for (;;) {
        while (g_queue_is_empty(&input.lines) && !input.closed)
            g_main_context_iteration(NULL, TRUE);
        char *line = g_queue_pop_head(&input.lines);
        if (!line) break;
        yyjson_doc *document = yyjson_read(line, strlen(line), 0);
        g_free(line);
        JsonValue *request = document != NULL ? yyjson_doc_get_root(document) : NULL;
        const char *tool = json_string(request, "tool");
        if (tool == NULL) {
            yyjson_doc_free(document);
            break;
        }
        scu.doc = yyjson_mut_doc_new(NULL);
        scu.deadline = g_get_monotonic_time() + 60 * G_USEC_PER_SEC;
        g_clear_pointer(&scu.code, g_free);
        g_clear_pointer(&scu.hint, g_free);
        JsonResult *result = result_object();
        JsonResult *output =
            scu_tick() ? scu_atspi(tool, json_member(request, "args")) : NULL;
        if (output != NULL) {
            result_member(result, "output", output);
        } else {
            JsonResult *error = result_object();
            result_string(error, "code",
                scu.code != NULL ? scu.code : "accessibility_unavailable");
            result_string(error, "hint",
                scu.hint != NULL ? scu.hint : "Semantic operation failed.");
            result_member(result, "error", error);
        }
        result_boolean(result, "retire", retire_atspi_worker);
        yyjson_mut_doc_set_root(scu.doc, result);
        char *reply = yyjson_mut_write(scu.doc, 0, NULL);
        puts(reply);
        fflush(stdout);
        free(reply);
        yyjson_mut_doc_free(scu.doc);
        scu.doc = NULL;
        yyjson_doc_free(document);
        if (retire_atspi_worker) {
            break;
        }
    }
    if (input.source) g_source_remove(input.source);
    g_queue_clear_full(&input.lines, g_free);
    g_string_free(input.input, TRUE);
    scu_atspi_close();
    return 0;
}

/* KWin visual access. The private host service is the authority boundary;
 * the guest never receives a general connection to the host session bus. */
#define KWIN_NAME "org.kde.KWin"
#define KWIN_EIS_PATH "/org/kde/KWin/EIS/RemoteDesktop"
#define KWIN_EIS_INTERFACE "org.kde.KWin.EIS.RemoteDesktop"
#define MAX_DISPLAYS 8
#define MAX_FRAME_BYTES (256u * 1024u * 1024u)

typedef struct {
    __typeof__(&ei_new_sender) new_sender;
    __typeof__(&ei_configure_name) configure_name;
    __typeof__(&ei_setup_backend_fd) setup_backend_fd;
    __typeof__(&ei_unref) unref;
    __typeof__(&ei_dispatch) dispatch;
    __typeof__(&ei_get_event) get_event;
    __typeof__(&ei_event_get_type) event_get_type;
    __typeof__(&ei_event_get_device) event_get_device;
    __typeof__(&ei_event_get_seat) event_get_seat;
    __typeof__(&ei_event_keyboard_get_xkb_mods_depressed)
        event_keyboard_get_xkb_mods_depressed;
    __typeof__(&ei_event_keyboard_get_xkb_mods_latched)
        event_keyboard_get_xkb_mods_latched;
    __typeof__(&ei_event_keyboard_get_xkb_mods_locked)
        event_keyboard_get_xkb_mods_locked;
    __typeof__(&ei_event_keyboard_get_xkb_group) event_keyboard_get_xkb_group;
    __typeof__(&ei_event_unref) event_unref;
    __typeof__(&ei_seat_bind_capabilities) seat_bind_capabilities;
    __typeof__(&ei_device_ref) device_ref;
    __typeof__(&ei_device_unref) device_unref;
    __typeof__(&ei_device_has_capability) device_has_capability;
    __typeof__(&ei_device_get_region) device_get_region;
    __typeof__(&ei_device_get_seat) device_get_seat;
    __typeof__(&ei_region_get_mapping_id) region_get_mapping_id;
    __typeof__(&ei_region_get_x) region_get_x;
    __typeof__(&ei_region_get_y) region_get_y;
    __typeof__(&ei_region_get_width) region_get_width;
    __typeof__(&ei_region_get_height) region_get_height;
    __typeof__(&ei_device_start_emulating) device_start_emulating;
    __typeof__(&ei_device_stop_emulating) device_stop_emulating;
    __typeof__(&ei_device_frame) device_frame;
    __typeof__(&ei_device_pointer_motion_absolute) device_pointer_motion_absolute;
    __typeof__(&ei_device_button_button) device_button_button;
    __typeof__(&ei_device_scroll_discrete) device_scroll_discrete;
    __typeof__(&ei_device_keyboard_key) device_keyboard_key;
    __typeof__(&ei_device_keyboard_get_keymap) device_keyboard_get_keymap;
    __typeof__(&ei_keymap_get_fd) keymap_get_fd;
    __typeof__(&ei_keymap_get_size) keymap_get_size;
    __typeof__(&ei_keymap_get_type) keymap_get_type;
    /* Text capabilities were added in libei 1.6; older libraries still work. */
    __typeof__(&ei_device_text_utf8) device_text_utf8;
    /* Round trips appeared in 1.4. Older libraries retain capture support. */
    __typeof__(&ei_new_ping) new_ping;
    __typeof__(&ei_ping) ping;
    __typeof__(&ei_ping_unref) ping_unref;
    __typeof__(&ei_event_pong_get_ping) event_pong_get_ping;
} EiLibrary;

typedef struct {
    __typeof__(&xkb_context_new) context_new;
    __typeof__(&xkb_context_unref) context_unref;
    __typeof__(&xkb_keymap_new_from_string) keymap_new_from_string;
    __typeof__(&xkb_keymap_unref) keymap_unref;
    __typeof__(&xkb_state_new) state_new;
    __typeof__(&xkb_state_unref) state_unref;
    __typeof__(&xkb_state_update_mask) state_update_mask;
    __typeof__(&xkb_state_key_get_one_sym) state_key_get_one_sym;
    __typeof__(&xkb_keymap_mod_get_index) keymap_mod_get_index;
    __typeof__(&xkb_keymap_min_keycode) keymap_min_keycode;
    __typeof__(&xkb_keymap_max_keycode) keymap_max_keycode;
    __typeof__(&xkb_keymap_num_layouts_for_key) keymap_num_layouts_for_key;
    __typeof__(&xkb_keymap_num_levels_for_key) keymap_num_levels_for_key;
    __typeof__(&xkb_keymap_key_get_syms_by_level) keymap_key_get_syms_by_level;
    __typeof__(&xkb_keysym_from_name) keysym_from_name;
    __typeof__(&xkb_utf32_to_keysym) utf32_to_keysym;
} XkbLibrary;

typedef struct {
    __typeof__(&gdk_pixbuf_new_from_bytes) new_from_bytes;
    __typeof__(&gdk_pixbuf_save_to_buffer) save_to_buffer;
} PixbufLibrary;

static EiLibrary ei_api;
static XkbLibrary xkb;
static PixbufLibrary pixbuf;
static gboolean visual_libraries_loaded;

typedef struct {
    struct ei_device *device;
    gboolean resumed;
    guint depressed, latched, locked, group;
} InputDevice;

typedef struct {
    char *mapping_id;
    guint width, height;
    gint logical_x, logical_y, logical_width, logical_height;
    gboolean has_geometry;
    guint64 revision;
    gboolean failed;
} Display;

/* Output rectangles remain private. The agent sees only the overview's pixel
 * grid; a zoom is an observation of that grid, never a new coordinate system. */
typedef struct {
    double x, y, width, height;
} CanvasRect;

typedef struct {
    CanvasRect tiles[MAX_DISPLAYS];
    guint native_widths[MAX_DISPLAYS], native_heights[MAX_DISPLAYS];
    guint count;
    double x, y, width, height;
} CanvasLayout;

typedef struct {
    guint64 id; /* Positive JSON-safe integer; zero means no overview. */
    char *key;
    guint width, height;
    CanvasLayout layout;
} CanvasSnapshot;

typedef struct {
    GDBusConnection *bus;
    gboolean broken;
    yyjson_doc *geometry;
    char *owner;
    struct ei *input;
    GPtrArray *devices;
    Display displays[MAX_DISPLAYS];
    guint display_count, emulation_sequence;
    guint64 mapping_revision, observed_input_sequence;
    CanvasSnapshot canvas;
    guint64 input_sequence;
    gint64 last_input_time;
    struct ei_ping *pending_ping; /* Borrowed from the observation wait. */
    gboolean ping_done;
    struct ei_device *held_device;
    guint held_button;
    struct ei_device *keyboard_device;
    guint held_keys[8], held_key_count;
} VisualSession;

static VisualSession visual;
static guint64 next_capture;

static gboolean load_visual_libraries(void)
{
    if (visual_libraries_loaded) {
        return TRUE;
    }
    void *ei_library = scu_library("libei.so.1");
    void *xkb_library = scu_library("libxkbcommon.so.0");
    void *pixbuf_library = scu_library("libgdk_pixbuf-2.0.so.0");
    if (ei_library == NULL || xkb_library == NULL ||
        pixbuf_library == NULL) {
        return FALSE;
    }
#define LOAD_SYMBOL(library, table, prefix, name)                                      \
    do {                                                                               \
        *(void **)(&(table).name) = dlsym(library, prefix #name);                      \
        if ((table).name == NULL) {                                                    \
            return FALSE;                                                              \
        }                                                                              \
    } while (0)
#define LOAD_EI(name) LOAD_SYMBOL(ei_library, ei_api, "ei_", name)
    LOAD_EI(new_sender);
    LOAD_EI(configure_name);
    LOAD_EI(setup_backend_fd);
    LOAD_EI(unref);
    LOAD_EI(dispatch);
    LOAD_EI(get_event);
    LOAD_EI(event_get_type);
    LOAD_EI(event_get_device);
    LOAD_EI(event_get_seat);
    LOAD_EI(event_keyboard_get_xkb_mods_depressed);
    LOAD_EI(event_keyboard_get_xkb_mods_latched);
    LOAD_EI(event_keyboard_get_xkb_mods_locked);
    LOAD_EI(event_keyboard_get_xkb_group);
    LOAD_EI(event_unref);
    LOAD_EI(seat_bind_capabilities);
    LOAD_EI(device_ref);
    LOAD_EI(device_unref);
    LOAD_EI(device_has_capability);
    LOAD_EI(device_get_region);
    LOAD_EI(device_get_seat);
    LOAD_EI(region_get_mapping_id);
    LOAD_EI(region_get_x);
    LOAD_EI(region_get_y);
    LOAD_EI(region_get_width);
    LOAD_EI(region_get_height);
    LOAD_EI(device_start_emulating);
    LOAD_EI(device_stop_emulating);
    LOAD_EI(device_frame);
    LOAD_EI(device_pointer_motion_absolute);
    LOAD_EI(device_button_button);
    LOAD_EI(device_scroll_discrete);
    LOAD_EI(device_keyboard_key);
    LOAD_EI(device_keyboard_get_keymap);
    LOAD_EI(keymap_get_fd);
    LOAD_EI(keymap_get_size);
    LOAD_EI(keymap_get_type);
#undef LOAD_EI
    *(void **)(&ei_api.device_text_utf8) = dlsym(ei_library, "ei_device_text_utf8");
    *(void **)(&ei_api.new_ping) = dlsym(ei_library, "ei_new_ping");
    *(void **)(&ei_api.ping) = dlsym(ei_library, "ei_ping");
    *(void **)(&ei_api.ping_unref) = dlsym(ei_library, "ei_ping_unref");
    *(void **)(&ei_api.event_pong_get_ping) =
        dlsym(ei_library, "ei_event_pong_get_ping");
#define LOAD_XKB(name) LOAD_SYMBOL(xkb_library, xkb, "xkb_", name)
    LOAD_XKB(context_new);
    LOAD_XKB(context_unref);
    LOAD_XKB(keymap_new_from_string);
    LOAD_XKB(keymap_unref);
    LOAD_XKB(state_new);
    LOAD_XKB(state_unref);
    LOAD_XKB(state_update_mask);
    LOAD_XKB(state_key_get_one_sym);
    LOAD_XKB(keymap_mod_get_index);
    LOAD_XKB(keymap_min_keycode);
    LOAD_XKB(keymap_max_keycode);
    LOAD_XKB(keymap_num_layouts_for_key);
    LOAD_XKB(keymap_num_levels_for_key);
    LOAD_XKB(keymap_key_get_syms_by_level);
    LOAD_XKB(keysym_from_name);
    LOAD_XKB(utf32_to_keysym);
#undef LOAD_XKB
#define LOAD_PIXBUF(name) LOAD_SYMBOL(pixbuf_library, pixbuf, "gdk_pixbuf_", name)
    LOAD_PIXBUF(new_from_bytes);
    LOAD_PIXBUF(save_to_buffer);
#undef LOAD_PIXBUF
#undef LOAD_SYMBOL
    visual_libraries_loaded = TRUE;
    return TRUE;
}

static void input_frame(struct ei_device *device)
{
    ei_api.device_frame(device, (uint64_t)g_get_monotonic_time());
    visual.input_sequence++;
    visual.last_input_time = g_get_monotonic_time();
}

/* Release our own input state only. The compositor also neutralizes devices
 * when a client disconnects, including when the process is killed abruptly. */
static void release_input(void)
{
    if (visual.held_device != NULL) {
        ei_api.device_button_button(visual.held_device, visual.held_button, false);
        input_frame(visual.held_device);
        visual.held_device = NULL;
    }
    if (visual.keyboard_device != NULL) {
        while (visual.held_key_count > 0) {
            ei_api.device_keyboard_key(visual.keyboard_device,
                visual.held_keys[--visual.held_key_count], false);
            input_frame(visual.keyboard_device);
        }
        visual.keyboard_device = NULL;
    }
}

static void free_input_device(gpointer pointer)
{
    InputDevice *device = pointer;
    ei_api.device_unref(device->device);
    g_free(device);
}

static void scu_visual_close(void)
{
    release_input();
    g_clear_pointer(&visual.devices, g_ptr_array_unref);
    if (visual.input) {
        ei_api.unref(visual.input);
    }
    for (guint i = 0; i < visual.display_count; i++) {
        g_free(visual.displays[i].mapping_id);
    }
    if (visual.bus) {
        g_object_unref(visual.bus);
    }
    disconnect_input();
    yyjson_doc_free(visual.geometry);
    g_free(visual.owner);
    g_free(visual.canvas.key);
    memset(&visual, 0, sizeof(visual));
}

static void poll_backend(void)
{
    if (visual.bus && g_dbus_connection_is_closed(visual.bus)) {
        visual.broken = TRUE;
    }
    if (visual.input == NULL || visual.devices == NULL) {
        return;
    }
    ei_api.dispatch(visual.input);
    struct ei_event *event;
    while ((event = ei_api.get_event(visual.input)) != NULL) {
        enum ei_event_type type = ei_api.event_get_type(event);
        struct ei_device *raw_device = ei_api.event_get_device(event);
        if (type == EI_EVENT_PONG && ei_api.event_pong_get_ping != NULL) {
            if (ei_api.event_pong_get_ping(event) == visual.pending_ping) {
                visual.ping_done = TRUE;
            }
        } else if (type == EI_EVENT_SEAT_ADDED) {
            ei_api.seat_bind_capabilities(ei_api.event_get_seat(event),
                EI_DEVICE_CAP_POINTER_ABSOLUTE, EI_DEVICE_CAP_BUTTON,
                EI_DEVICE_CAP_SCROLL, EI_DEVICE_CAP_KEYBOARD,
                ei_api.device_text_utf8 != NULL ? EI_DEVICE_CAP_TEXT : 0, NULL);
        } else if (type == EI_EVENT_DEVICE_ADDED) {
            visual.mapping_revision++;
            InputDevice *device = g_new0(InputDevice, 1);
            device->device = ei_api.device_ref(raw_device);
            g_ptr_array_add(visual.devices, device);
        } else if (type == EI_EVENT_DISCONNECT) {
            visual.broken = TRUE;
        } else {
            for (guint index = 0; index < visual.devices->len; index++) {
                InputDevice *device = g_ptr_array_index(visual.devices, index);
                if (device->device != raw_device) {
                    continue;
                }
                if (type == EI_EVENT_KEYBOARD_MODIFIERS) {
                    device->depressed =
                        ei_api.event_keyboard_get_xkb_mods_depressed(event);
                    device->latched = ei_api.event_keyboard_get_xkb_mods_latched(event);
                    device->locked = ei_api.event_keyboard_get_xkb_mods_locked(event);
                    device->group = ei_api.event_keyboard_get_xkb_group(event);
                } else if (type == EI_EVENT_DEVICE_RESUMED) {
                    visual.mapping_revision++;
                    device->resumed = TRUE;
                    ei_api.device_start_emulating(
                        raw_device, ++visual.emulation_sequence);
                } else if (type == EI_EVENT_DEVICE_PAUSED ||
                           type == EI_EVENT_DEVICE_REMOVED) {
                    visual.mapping_revision++;
                    device->resumed = FALSE;
                    if (visual.held_device == raw_device) {
                        visual.held_device = NULL;
                    }
                    if (visual.keyboard_device == raw_device) {
                        visual.keyboard_device = NULL;
                        visual.held_key_count = 0;
                    }
                    if (type == EI_EVENT_DEVICE_REMOVED) {
                        g_ptr_array_remove_index(visual.devices, index);
                    }
                }
                break;
            }
        }
        ei_api.event_unref(event);
    }
}

/* Calls stay asynchronous even though desktop operations are serialized. The
 * main context must continue dispatching cancellation and peer disconnects. */
typedef struct {
    gboolean done;
    GVariant *reply;
    GUnixFDList *fds;
    GError *error;
} BusReply;

static void bus_reply_done(GObject *source, GAsyncResult *result, gpointer data)
{
    BusReply *reply = data;
    reply->reply = g_dbus_connection_call_with_unix_fd_list_finish(
        G_DBUS_CONNECTION(source), &reply->fds, result, &reply->error);
    reply->done = TRUE;
}

static GVariant *bounded_call(GDBusConnection *connection, const char *destination,
    const char *path, const char *interface, const char *method, GVariant *arguments,
    GUnixFDList *input_fds, GUnixFDList **output_fds, GError **error)
{
    BusReply reply = {0};
    GCancellable *cancel = g_cancellable_new();
    gint64 deadline = g_get_monotonic_time() + 30 * G_USEC_PER_SEC;
    if (scu.deadline) {
        deadline = MIN(deadline, scu.deadline);
    }
    gint milliseconds = MAX(1, (deadline - g_get_monotonic_time()) / 1000);
    g_dbus_connection_call_with_unix_fd_list(connection, destination, path, interface,
        method, arguments, NULL, G_DBUS_CALL_FLAGS_NO_AUTO_START, milliseconds,
        input_fds, cancel, bus_reply_done, &reply);
    while (!reply.done) {
        if (g_atomic_int_get(&scu.stopping) ||
            g_get_monotonic_time() >= deadline ||
            !scu_tick()) {
            g_cancellable_cancel(cancel);
        }
        while (g_main_context_iteration(NULL, FALSE)) {
        }
        if (!reply.done) {
            g_usleep(1000);
        }
    }
    g_object_unref(cancel);
    if (output_fds) {
        *output_fds = reply.fds;
    } else {
        g_clear_object(&reply.fds);
    }
    if (reply.error) {
        g_propagate_error(error, reply.error);
    }
    return reply.reply;
}

/* Only host workers acquire the session bus. No bus or device descriptors
 * cross the Space boundary. */
static gboolean require_computer_access(void)
{
    if (visual.broken) {
        scu_invalidate();
        scu_fail("computer_use_disconnected", "KWin computer use disconnected. Inspect status before continuing; no action was replayed.");
        return FALSE;
    }
    if (!session_bus || g_dbus_connection_is_closed(session_bus)) {
        scu_fail("computer_use_unavailable", "The host session bus is unavailable.");
        return FALSE;
    }
    if (!visual.bus) {
        visual.bus = g_object_ref(session_bus);
    }
    return scu_tick();
}

static gboolean read_rect(JsonValue *value, CanvasRect *rect)
{
    const char *fields[] = {"x", "y", "width", "height"};
    double parts[4];
    for (guint i = 0; i < 4; i++) {
        JsonValue *part = json_member(value, fields[i]);
        parts[i] = yyjson_get_num(part);
        if (!yyjson_is_num(part) || !isfinite(parts[i]) || fabs(parts[i]) > G_MAXINT / 4) {
            return FALSE;
        }
    }
    *rect = (CanvasRect){parts[0], parts[1], parts[2], parts[3]};
    return rect->width > 0 && rect->height > 0;
}

/* Metadata comes only from the fixed host-side script. Windows are deliberately
 * not part of the canvas identity: moving a window does not move the desktop. */
static gboolean refresh_geometry(void)
{
    GError *error = NULL;
    if (!require_computer_access() || !host_geometry(&error)) {
        scu_fail(g_error_matches(error, G_IO_ERROR, G_IO_ERROR_TIMED_OUT) ||
                g_error_matches(error, G_IO_ERROR, G_IO_ERROR_CANCELLED) ? "timeout" : "kwin_geometry_unavailable",
            error ? error->message : "KWin geometry is unavailable.");
        g_clear_error(&error);
        return FALSE;
    }
    const char *json = current_geometry();
    yyjson_doc *document = strlen(json) <= 1024 * 1024 ? yyjson_read(json, strlen(json), 0) : NULL;
    JsonValue *root = document ? yyjson_doc_get_root(document) : NULL;
    JsonValue *outputs = json_member(root, "outputs");
    const char *owner = json_string(root, "owner");
    guint count = yyjson_arr_size(outputs);
    if (!owner || !yyjson_is_arr(outputs) || count == 0 || count > MAX_DISPLAYS ||
        !yyjson_is_arr(json_member(root, "windows"))) {
        yyjson_doc_free(document);
        scu_fail("kwin_geometry_unavailable", "KWin returned invalid desktop geometry.");
        return FALSE;
    }
    if (visual.owner && strcmp(visual.owner, owner)) {
        yyjson_doc_free(document);
        visual.broken = TRUE;
        scu_atspi_close();
        scu_fail("computer_use_disconnected", "KWin restarted. Call status again; no action was replayed.");
        return FALSE;
    }
    Display next[MAX_DISPLAYS] = {0};
    for (guint i = 0; i < count; i++) {
        JsonValue *output = yyjson_arr_get(outputs, i);
        CanvasRect rect;
        const char *name = json_string(output, "name");
        double scale = json_number(output, "scale", 0);
        if (!name || !*name || !read_rect(json_member(output, "geometry"), &rect) ||
            !isfinite(scale) || scale <= 0 || scale > 16 ||
            rect.width * scale > G_MAXINT / 3 || rect.height * scale > G_MAXINT) {
            for (guint j = 0; j < i; j++) {
                g_free(next[j].mapping_id);
            }
            yyjson_doc_free(document);
            scu_fail("kwin_geometry_unavailable", "KWin output geometry or scale is invalid.");
            return FALSE;
        }
        next[i] = (Display){.mapping_id = g_strdup(name), .has_geometry = TRUE,
            .logical_x = (gint)rect.x, .logical_y = (gint)rect.y,
            .logical_width = (gint)rect.width, .logical_height = (gint)rect.height,
            .width = MAX(1, (guint)(rect.width * scale)),
            .height = MAX(1, (guint)(rect.height * scale))};
    }
    for (guint i = 0; i < visual.display_count; i++) {
        g_free(visual.displays[i].mapping_id);
    }
    memcpy(visual.displays, next, sizeof(next));
    visual.display_count = count;
    g_free(visual.owner);
    visual.owner = g_strdup(owner);
    yyjson_doc_free(visual.geometry);
    visual.geometry = document;
    return TRUE;
}

static gboolean ensure_visual_session(void)
{
    if (!require_computer_access() || !desktop_ready() || !refresh_geometry()) {
        return FALSE;
    }
    if (visual.input) {
        return TRUE;
    }
    for (guint i = 1; libs[i]; i++) {
        if (!scu_library(libs[i])) {
            char *hint = g_strdup_printf("Missing library %s. Install the package for it.", libs[i]);
            scu_fail("missing_dependency", hint);
            g_free(hint);
            return FALSE;
        }
    }
    if (!load_visual_libraries()) {
        scu_fail("missing_dependency", "Install host libei, libxkbcommon and GdkPixbuf libraries.");
        return FALSE;
    }
    GError *error = NULL;
    int fd = connect_input(&error);
    if (fd < 0) {
        scu_fail("input_unavailable", error ? error->message : "KWin did not provide an EIS connection.");
        g_clear_error(&error);
        return FALSE;
    }
    visual.input = ei_api.new_sender(NULL);
    visual.devices = g_ptr_array_new_with_free_func(free_input_device);
    ei_api.configure_name(visual.input, "Spaces computer use");
    if (ei_api.setup_backend_fd(visual.input, fd) < 0) {
        close(fd);
        scu_fail("input_unavailable", "Could not connect to KWin EIS.");
        return FALSE;
    }
    gint64 deadline = g_get_monotonic_time() + 3 * G_USEC_PER_SEC;
    while (g_get_monotonic_time() < deadline) {
        if (!scu_pause(5)) {
            return FALSE;
        }
        gboolean resumed = FALSE;
        for (guint i = 0; i < visual.devices->len; i++) {
            InputDevice *device = g_ptr_array_index(visual.devices, i);
            resumed |= device->resumed && ei_api.device_has_capability(
                device->device, EI_DEVICE_CAP_POINTER_ABSOLUTE);
        }
        if (resumed) {
            /* Consume announcements for the rest of this compositor seat. */
            return scu_pause(20);
        }
    }
    scu_fail("input_unavailable", "KWin EIS did not resume a pointer device.");
    return FALSE;
}

static gboolean visual_step(void)
{
    if (!scu_tick() || !require_computer_access()) {
        return FALSE;
    }
    return !visual.broken && desktop_input_allowed();
}


typedef struct {
    struct ei_device *device;
    struct ei_region *region;
} RegionBinding;

static gboolean bind_stream_region(Display *display, RegionBinding *binding)
{
    guint matches = 0, total = 0;
    RegionBinding only = {0};
    memset(binding, 0, sizeof(*binding));
    for (guint index = 0; visual.devices && index < visual.devices->len; index++) {
        InputDevice *device = g_ptr_array_index(visual.devices, index);
        if (!device->resumed || !ei_api.device_has_capability(
                device->device, EI_DEVICE_CAP_POINTER_ABSOLUTE)) {
            continue;
        }
        for (guint n = 0;; n++) {
            struct ei_region *region = ei_api.device_get_region(device->device, n);
            if (region == NULL) {
                break;
            }
            only = (RegionBinding){device->device, region};
            total++;
            const char *mapping = ei_api.region_get_mapping_id(region);
            if (display->mapping_id && mapping && !strcmp(display->mapping_id, mapping)) {
                *binding = only;
                matches++;
            }
        }
    }
    if (!matches && !display->mapping_id && visual.display_count == 1 && total == 1) {
        *binding = only;
        matches = 1;
    }
    return matches == 1 && ei_api.region_get_width(binding->region) > 0 &&
           ei_api.region_get_height(binding->region) > 0;
}

static gboolean rect_contains(const CanvasRect *rect, double x, double y)
{
    return x >= rect->x && y >= rect->y && x < rect->x + rect->width &&
           y < rect->y + rect->height;
}

/* KWin output geometry is authoritative. EIS regions route input within it;
 * they never invent a second desktop layout. Capture IDs cover both mappings. */
static gboolean canvas_layout(CanvasLayout *layout, char **key)
{
    memset(layout, 0, sizeof(*layout));
    if (visual.display_count == 0) {
        scu_fail("capture_unavailable", "KWin has no active desktop outputs.");
        return FALSE;
    }
    for (guint i = 0; i < visual.display_count; i++) {
        Display *display = &visual.displays[i];
        if (!display->has_geometry || display->logical_width <= 0 || display->logical_height <= 0) {
            scu_fail("canvas_layout_unavailable", "KWin did not provide complete output geometry.");
            return FALSE;
        }
    }
    layout->count = visual.display_count;
    GString *identity = g_string_new(NULL);
    g_string_append_printf(identity, "%u:%" G_GUINT64_FORMAT ";",
        visual.display_count, visual.mapping_revision);
    double right = 0, bottom = 0;
    for (guint i = 0; i < layout->count; i++) {
        Display *display = &visual.displays[i];
        CanvasRect *rect = &layout->tiles[i];
        layout->native_widths[i] = display->width;
        layout->native_heights[i] = display->height;
        if (display->failed || visual.broken) {
            g_string_free(identity, TRUE);
            scu_fail("capture_unavailable", "KWin desktop access disconnected.");
            return FALSE;
        }
        *rect = (CanvasRect){display->logical_x, display->logical_y,
            display->logical_width, display->logical_height};
        if (display->width == 0 || display->height == 0 || rect->width <= 0 ||
            rect->height <= 0) {
            g_string_free(identity, TRUE);
            scu_fail("capture_unavailable", "A KWin output has no valid dimensions.");
            return FALSE;
        }
        for (guint j = 0; j < i; j++) {
            CanvasRect *other = &layout->tiles[j];
            if (rect->x < other->x + other->width && other->x < rect->x + rect->width &&
                rect->y < other->y + other->height && other->y < rect->y + rect->height) {
                g_string_free(identity, TRUE);
                scu_fail("ambiguous_canvas_layout",
                    "Granted capture regions overlap; a unique canvas cannot be established.");
                return FALSE;
            }
        }
        if (i == 0) {
            layout->x = rect->x;
            layout->y = rect->y;
            right = rect->x + rect->width;
            bottom = rect->y + rect->height;
        } else {
            layout->x = MIN(layout->x, rect->x);
            layout->y = MIN(layout->y, rect->y);
            right = MAX(right, rect->x + rect->width);
            bottom = MAX(bottom, rect->y + rect->height);
        }
        g_string_append_printf(identity,
            "%u:%s:%s:%u:%u:%" G_GUINT64_FORMAT ":%.17g,%.17g,%.17g,%.17g;",
            i, visual.owner ? visual.owner : "",
            display->mapping_id ? display->mapping_id : "", display->width,
            display->height, display->revision, rect->x, rect->y, rect->width, rect->height);
    }
    /* Include all pointer regions, including duplicate mapping IDs during a
     * replacement. Observation may still work, but old input routes are stale. */
    for (guint i = 0; visual.devices && i < visual.devices->len; i++) {
        InputDevice *device = g_ptr_array_index(visual.devices, i);
        if (!ei_api.device_has_capability(device->device, EI_DEVICE_CAP_POINTER_ABSOLUTE)) {
            continue;
        }
        g_string_append_printf(identity, "%p:%d;", (void *)device->device, device->resumed);
        for (guint n = 0;; n++) {
            struct ei_region *region = ei_api.device_get_region(device->device, n);
            if (!region) {
                break;
            }
            const char *mapping = ei_api.region_get_mapping_id(region);
            g_string_append_printf(identity, "%s:%u:%u:%u:%u;", mapping ? mapping : "",
                ei_api.region_get_x(region), ei_api.region_get_y(region),
                ei_api.region_get_width(region), ei_api.region_get_height(region));
        }
    }
    layout->width = right - layout->x;
    layout->height = bottom - layout->y;
    *key = g_string_free(identity, FALSE);
    return TRUE;
}

static gboolean validate_canvas(guint64 id)
{
    CanvasSnapshot *canvas = &visual.canvas;
    if (canvas->id == 0 || (id && id != canvas->id)) {
        scu_fail("stale_capture", "Take a full desktop overview before zooming or sending pixel input.");
        return FALSE;
    }
    CanvasLayout current;
    char *key = NULL;
    if (!canvas_layout(&current, &key)) {
        return FALSE;
    }
    gboolean valid = strcmp(key, canvas->key) == 0;
    g_free(key);
    if (!valid) {
        scu_fail("stale_capture", "Desktop geometry or input mapping changed. Take a new full overview.");
    }
    return valid;
}

/* Describe the full overview's coordinate frame and capture guard. */
static JsonResult *canvas_status(void)
{
    if (visual.canvas.id == 0) {
        return yyjson_mut_null(scu.doc);
    }
    char *code = scu.code, *hint = scu.hint;
    scu.code = scu.hint = NULL;
    gboolean valid = validate_canvas(0);
    g_free(scu.code);
    g_free(scu.hint);
    scu.code = code;
    scu.hint = hint;
    if (!valid) {
        return yyjson_mut_null(scu.doc);
    }
    CanvasSnapshot *canvas = &visual.canvas;
    JsonResult *result = result_object();
    result_integer(result, "width", canvas->width);
    result_integer(result, "height", canvas->height);
    result_integer(result, "id", canvas->id);
    return result;
}

static gboolean validate_zoom(JsonValue *arguments, guint64 id)
{
    JsonValue *zoom = json_member(arguments, "zoom");
    if (!zoom) {
        return TRUE;
    }
    if (!yyjson_is_arr(zoom) || yyjson_arr_size(zoom) != 4) {
        scu_fail("invalid_arguments", "zoom must be [left,top,right,bottom] in overview coordinates.");
        return FALSE;
    }
    double corners[4];
    for (guint i = 0; i < 4; i++) {
        JsonValue *value = yyjson_arr_get(zoom, i);
        corners[i] = yyjson_get_num(value);
        if (!yyjson_is_num(value) || !isfinite(corners[i])) {
            scu_fail("invalid_arguments", "Zoom coordinates must be finite numbers.");
            return FALSE;
        }
    }
    if (corners[0] < 0 || corners[1] < 0 || corners[2] <= corners[0] ||
        corners[3] <= corners[1]) {
        scu_fail("invalid_arguments", "Zoom must describe a positive rectangle inside the overview.");
        return FALSE;
    }
    if (!validate_canvas(id)) {
        return FALSE;
    }
    if (corners[2] > visual.canvas.width || corners[3] > visual.canvas.height) {
        scu_fail("invalid_arguments", "Zoom rectangle lies outside the full desktop overview.");
        return FALSE;
    }
    return TRUE;
}

/* A compositor capture is fresh by construction. Synchronize submitted input
 * before requesting pixels, but never claim that the application has finished. */
static gboolean prepare_capture(void)
{
    if (!desktop_ready() || !visual_step()) {
        return FALSE;
    }
    if (!visual.input_sequence || visual.observed_input_sequence == visual.input_sequence) {
        return TRUE;
    }
    if (ei_api.new_ping && ei_api.ping && ei_api.ping_unref && ei_api.event_pong_get_ping) {
        struct ei_ping *ping = ei_api.new_ping(visual.input);
        visual.pending_ping = ping;
        visual.ping_done = FALSE;
        ei_api.ping(ping);
        gint64 until = g_get_monotonic_time() + 250000;
        while (!visual.ping_done && g_get_monotonic_time() < until) {
            if (!scu_pause(2)) {
                visual.pending_ping = NULL;
                ei_api.ping_unref(ping);
                return FALSE;
            }
        }
        visual.pending_ping = NULL;
        ei_api.ping_unref(ping);
    }
    gint64 remaining = visual.last_input_time + 100000 - g_get_monotonic_time();
    if (remaining > 0 && !scu_pause((remaining + 999) / 1000)) {
        return FALSE;
    }
    return TRUE;
}

static GBytes *render_canvas(const CanvasLayout *layout, const CanvasRect *view,
    guint width, guint height, GBytes **frames)
{
    guchar *output = g_try_malloc0((gsize)width * height * 3);
    if (!output) {
        scu_fail("capture_failed", "Could not allocate the desktop image.");
        return NULL;
    }
    const guchar *sources[MAX_DISPLAYS];
    for (guint i = 0; i < layout->count; i++) {
        sources[i] = g_bytes_get_data(frames[i], NULL);
    }
    for (guint y = 0; y < height; y++) {
        if ((y % 32) == 0 && !scu_tick()) {
            g_free(output);
            return NULL;
        }
        double logical_y = view->y + (y + 0.5) * view->height / height;
        for (guint x = 0; x < width; x++) {
            double logical_x = view->x + (x + 0.5) * view->width / width;
            for (guint i = 0; i < layout->count; i++) {
                const CanvasRect *tile = &layout->tiles[i];
                if (!rect_contains(tile, logical_x, logical_y)) {
                    continue;
                }
                guint native_width = layout->native_widths[i];
                guint native_height = layout->native_heights[i];
                double px = CLAMP((logical_x - tile->x) * native_width / tile->width - 0.5,
                    0.0, native_width - 1.0);
                double py = CLAMP((logical_y - tile->y) * native_height / tile->height - 0.5,
                    0.0, native_height - 1.0);
                guint x0 = (guint)px, y0 = (guint)py;
                guint x1 = MIN(x0 + 1, native_width - 1);
                guint y1 = MIN(y0 + 1, native_height - 1);
                double fx = px - x0, fy = py - y0;
                guchar *pixel = output + ((gsize)y * width + x) * 3;
                for (guint channel = 0; channel < 3; channel++) {
                    const guchar *data = sources[i];
                    double upper = data[((gsize)y0 * native_width + x0) * 3 + channel] * (1 - fx) +
                                   data[((gsize)y0 * native_width + x1) * 3 + channel] * fx;
                    double lower = data[((gsize)y1 * native_width + x0) * 3 + channel] * (1 - fx) +
                                   data[((gsize)y1 * native_width + x1) * 3 + channel] * fx;
                    pixel[channel] = (guchar)(upper * (1 - fy) + lower * fy + 0.5);
                }
                break;
            }
        }
    }
    return g_bytes_new_take(output, (gsize)width * height * 3);
}



static int lower_integer(double value)
{
    int integer = (int)value;
    return value < integer ? integer - 1 : integer;
}

static GBytes *host_capture(GVariant *args, const char *window_key,
    guint *width, guint *height, GError **error);

static GBytes *capture_pixels(const char *method, GVariant *arguments,
    guint *width, guint *height)
{
    const char *window_key = NULL;
    if (!strcmp(method, "CaptureWindow")) {
        g_variant_get(arguments, "(&s)", &window_key);
    }
    GError *error = NULL;
    GVariant *owned = g_variant_ref_sink(arguments);
    GBytes *pixels = host_capture(arguments, window_key, width, height, &error);
    g_variant_unref(owned);
    if (!pixels) {
        scu_fail("capture_failed", error ? error->message : "KWin capture failed.");
        g_clear_error(&error);
    }
    return pixels;
}

static GBytes *capture_area(const CanvasRect *view, gboolean native,
    guint *width, guint *height, CanvasRect *captured)
{
    gint left = lower_integer(view->x), top = lower_integer(view->y);
    gint right = -lower_integer(-(view->x + view->width));
    gint bottom = -lower_integer(-(view->y + view->height));
    *captured = (CanvasRect){left, top, right - left, bottom - top};
    return capture_pixels("Capture",
        g_variant_new("(iiiib)", left, top, right - left, bottom - top, native), width, height);
}

static gboolean append_image(GBytes *pixels, guint width, guint height)
{
    GdkPixbuf *image = pixbuf.new_from_bytes(pixels, GDK_COLORSPACE_RGB, FALSE,
        8, width, height, width * 3);
    char *png = NULL;
    gsize length = 0;
    GError *error = NULL;
    gboolean saved = image && pixbuf.save_to_buffer(image, &png, &length, "png", &error, NULL);
    g_clear_object(&image);
    if (!saved || length > 32u * 1024u * 1024u) {
        scu_fail("capture_failed", error ? error->message : "PNG exceeds the 32 MiB image limit.");
        g_clear_error(&error);
        g_free(png);
        return FALSE;
    }
    char *base64 = g_base64_encode((guchar *)png, length);
    g_free(png);
    JsonResult *block = result_object();
    result_string(block, "type", "image");
    result_string(block, "mimeType", "image/png");
    result_string(block, "data", base64);
    result_append(scu.images, block);
    g_free(base64);
    return TRUE;
}

static JsonResult *capture_desktop(JsonValue *arguments)
{
    gboolean zoomed = json_member(arguments, "zoom") != NULL;
    if (zoomed && !validate_zoom(arguments, 0)) {
        return NULL;
    }
    if (!zoomed && next_capture == MAX_CAPTURE_ID) {
        return scu_fail("capture_limit", "Capture IDs exhausted. Reconnect the MCP client.");
    }
    CanvasLayout layout;
    char *key = NULL;
    if (!prepare_capture() || !canvas_layout(&layout, &key)) {
        return NULL;
    }
    CanvasRect view = {layout.x, layout.y, layout.width, layout.height};
    if (zoomed) {
        JsonValue *zoom = json_member(arguments, "zoom");
        double left = yyjson_get_num(yyjson_arr_get(zoom, 0));
        double top = yyjson_get_num(yyjson_arr_get(zoom, 1));
        double right = yyjson_get_num(yyjson_arr_get(zoom, 2));
        double bottom = yyjson_get_num(yyjson_arr_get(zoom, 3));
        view = (CanvasRect){layout.x + left * layout.width / visual.canvas.width,
            layout.y + top * layout.height / visual.canvas.height,
            (right - left) * layout.width / visual.canvas.width,
            (bottom - top) * layout.height / visual.canvas.height};
    }
    guint maximum = (guint)json_number(arguments, "max_image_dimension", 1600);
    /* Overviews need orientation pixels, while zoom always requests native
     * detail. Avoid a huge native workspace allocation for ordinary overviews. */
    gboolean native = zoomed || maximum == 0 || maximum > MAX(view.width, view.height);
    guint source_width = 0, source_height = 0;
    CanvasRect captured;
    GBytes *source = capture_area(&view, native, &source_width, &source_height, &captured);
    if (!source) {
        g_free(key);
        return NULL;
    }
    double width = view.width * source_width / captured.width;
    double height = view.height * source_height / captured.height;
    if (maximum && MAX(width, height) > maximum) {
        double scale = maximum / MAX(width, height);
        width *= scale;
        height *= scale;
    }
    guint output_width = MAX(1, (guint)width), output_height = MAX(1, (guint)height);
    if (width > G_MAXINT / 3 || height > G_MAXINT ||
        (guint64)output_width * output_height * 4 > MAX_FRAME_BYTES) {
        g_bytes_unref(source);
        g_free(key);
        return scu_fail("capture_failed", "Canvas exceeds the image memory limits; reduce max_image_dimension.");
    }
    CanvasLayout sampling = {.count = 1};
    sampling.tiles[0] = captured;
    sampling.native_widths[0] = source_width;
    sampling.native_heights[0] = source_height;
    GBytes *pixels = render_canvas(&sampling, &view, output_width, output_height, &source);
    g_bytes_unref(source);
    if (!pixels) {
        g_free(key);
        return NULL;
    }
    CanvasLayout current;
    char *current_key = NULL;
    gboolean unchanged = refresh_geometry() && canvas_layout(&current, &current_key) &&
        !strcmp(key, current_key);
    g_free(current_key);
    if (!unchanged) {
        g_bytes_unref(pixels);
        g_free(key);
        return scu_fail("stale_capture", "KWin geometry changed during capture. Request a new overview.");
    }
    gboolean saved = append_image(pixels, output_width, output_height);
    g_bytes_unref(pixels);
    if (!saved) {
        g_free(key);
        return NULL;
    }
    if (!zoomed) {
        g_free(visual.canvas.key);
        visual.canvas = (CanvasSnapshot){.id = ++next_capture,
            .key = key, .width = output_width, .height = output_height,
            .layout = layout};
    } else {
        g_free(key);
    }
    JsonResult *result = result_object();
    result_integer(result, "id", visual.canvas.id);
    result_integer(result, "width", output_width);
    result_integer(result, "height", output_height);
    if (zoomed) {
        result_member(result, "zoom", yyjson_val_mut_copy(scu.doc, json_member(arguments, "zoom")));
    }
    visual.observed_input_sequence = visual.input_sequence;
    return result;
}

/* Public window IDs name KWin windows, including windows with no AT-SPI tree. */
static GHashTable *kwin_window_ids;
static guint next_kwin_window = 1;
static guint kwin_window_id(const char *key)
{
    if (!kwin_window_ids) {
        kwin_window_ids = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    }
    guint id = GPOINTER_TO_UINT(g_hash_table_lookup(kwin_window_ids, key));
    if (!id) {
        id = next_kwin_window++;
        g_hash_table_insert(kwin_window_ids, g_strdup(key), GUINT_TO_POINTER(id));
    }
    return id;
}

static JsonResult *overview_rect(const CanvasRect *rect)
{
    CanvasSnapshot *canvas = &visual.canvas;
    JsonResult *result = result_object();
    result_number(result, "x", (rect->x - canvas->layout.x) * canvas->width / canvas->layout.width);
    result_number(result, "y", (rect->y - canvas->layout.y) * canvas->height / canvas->layout.height);
    result_number(result, "width", rect->width * canvas->width / canvas->layout.width);
    result_number(result, "height", rect->height * canvas->height / canvas->layout.height);
    return result;
}

static JsonResult *kwin_apps(void)
{
    JsonResult *apps = result_array();
    GHashTable *grouped = g_hash_table_new(g_direct_hash, g_direct_equal);
    JsonValue *windows_json = json_member(yyjson_doc_get_root(visual.geometry), "windows");
    size_t i, n;
    JsonValue *window;
    yyjson_arr_foreach(windows_json, i, n, window) {
        const char *key = json_string(window, "key");
        guint pid = json_number(window, "pid", 0);
        CanvasRect frame;
        if (!key || !read_rect(json_member(window, "frame"), &frame)) {
            continue;
        }
        JsonResult *app = g_hash_table_lookup(grouped, GUINT_TO_POINTER(pid));
        if (!app) {
            app = result_object();
            result_integer(app, "pid", pid);
            result_string(app, "app_name", json_string(window, "app_name"));
            result_member(app, "windows", result_array());
            result_append(apps, app);
            g_hash_table_insert(grouped, GUINT_TO_POINTER(pid), app);
        }
        JsonResult *item = result_object();
        result_integer(item, "window_id", kwin_window_id(key));
        result_string(item, "title", json_string(window, "title"));
        result_member(item, "bounds", overview_rect(&frame));
        result_boolean(item, "shown", json_boolean(window, "shown"));
        result_boolean(item, "active", json_boolean(window, "active"));
        result_append(yyjson_mut_obj_get(app, "windows"), item);
    }
    g_hash_table_unref(grouped);
    return apps;
}

/* Validate both adapters before capturing or starting a batch. Discovery of
 * MCP schemas remains available, but there is no reduced-functionality mode. */
static gboolean require_computer_ready(void)
{
    if (!require_computer_access()) {
        return FALSE;
    }
    for (guint i = 0; libs[i]; i++) {
        if (!scu_library(libs[i])) {
            char *hint = g_strdup_printf("Missing library %s. Install the package for it.", libs[i]);
            scu_fail("missing_dependency", hint);
            g_free(hint);
            return FALSE;
        }
    }
    if (!load_atspi() || !load_visual_libraries()) {
        scu_fail("missing_dependency", "The installed computer-use libraries are missing required functions. Update the host libraries.");
        return FALSE;
    }
    return atspi_worker_call("check_ready", NULL) != NULL;
}

static JsonResult *kwin_status(void)
{
    if (!require_computer_ready() || !ensure_visual_session()) {
        return NULL;
    }
    if (!capture_desktop(NULL)) {
        return NULL;
    }
    JsonResult *result = result_object();
    JsonResult *canvas = canvas_status();
    if (yyjson_mut_is_null(canvas)) {
        return scu_fail("stale_capture", "Desktop geometry changed during status. Take a new full overview.");
    }
    result_member(result, "canvas", canvas);
    result_member(result, "apps", kwin_apps());
    return result;
}

/* A toolkit may expose its client, buffer (including invisible margins), or
 * decorated frame. Use only compositor-reported rectangles and desktop scales;
 * never stretch an accessibility tree to an approximately sized window. */
static gboolean accessibility_geometry(JsonValue *window, JsonValue *candidate,
    CanvasRect *anchor, CanvasRect *origin, double *ratio)
{
    const char *title = json_string(candidate, "title");
    if (!title || !*title || g_strcmp0(title, json_string(window, "title")) ||
        !read_rect(json_member(candidate, "bounds"), anchor)) {
        return FALSE;
    }
    const char *rectangles[] = {"client", "buffer", "frame"};
    for (guint r = 0; r < G_N_ELEMENTS(rectangles); r++) {
        CanvasRect rect;
        if (!read_rect(json_member(window, rectangles[r]), &rect)) {
            continue;
        }
        for (guint k = 0; k <= visual.display_count; k++) {
            double scale = k == 0 ? 1 : (double)visual.displays[k - 1].width /
                visual.displays[k - 1].logical_width;
            if (fabs(anchor->width - rect.width * scale) <= 2 &&
                fabs(anchor->height - rect.height * scale) <= 2) {
                *origin = rect;
                *ratio = 1 / scale;
                return TRUE;
            }
        }
    }
    return FALSE;
}

/* Semantic rectangles are private window-relative coordinates until a unique
 * KWin association anchors them. Raw rectangles never cross the MCP boundary. */
static JsonResult *window_tree(JsonValue *args, JsonValue *window)
{
    CanvasRect client = {0};
    JsonValue *candidate;
    size_t i, n;
    JsonResult *discovery = atspi_worker_call("list_apps", NULL);
    if (!discovery) {
        return NULL;
    }
    yyjson_doc *listing = yyjson_mut_val_imut_copy(discovery, NULL);
    JsonValue *match = NULL;
    CanvasRect anchor = {0};
    guint matches = 0, best = 0, match_pid = 0;
    double ratio = 1;
    JsonValue *app;
    yyjson_arr_foreach(json_member(yyjson_doc_get_root(listing), "apps"), i, n, app) {
        guint score = json_number(app, "pid", -1) == json_number(args, "pid", -2) ? 2 : 1;
        size_t j, m;
        yyjson_arr_foreach(json_member(app, "windows"), j, m, candidate) {
            CanvasRect root, origin;
            double scale;
            if (score < best || !accessibility_geometry(window, candidate, &root, &origin, &scale)) {
                continue;
            }
            /* Bus credentials and compositor PIDs can identify different
             * processes of a sandboxed application. A PID-independent fallback
             * must be unique in both inventories, not merely in the AT-SPI list. */
            if (score == 1) {
                guint alternatives = 0;
                size_t w, total;
                JsonValue *other;
                yyjson_arr_foreach(json_member(yyjson_doc_get_root(visual.geometry), "windows"), w, total, other) {
                    CanvasRect unused_anchor, unused_origin;
                    double unused_ratio;
                    if (accessibility_geometry(other, candidate, &unused_anchor, &unused_origin, &unused_ratio)) {
                        alternatives++;
                    }
                }
                if (alternatives != 1) {
                    continue;
                }
            }
            if (score > best) {
                best = score;
                matches = 0;
            }
            match = candidate;
            match_pid = json_number(app, "pid", 0);
            anchor = root;
            client = origin;
            ratio = scale;
            matches++;
        }
    }
    if (matches != 1) {
        yyjson_doc_free(listing);
        return scu_fail("accessibility_window_unavailable", "No unique accessibility window matches this KWin window and client geometry. Use screenshot-based input.");
    }
    yyjson_mut_doc *request = yyjson_mut_doc_new(NULL);
    JsonResult *copy = yyjson_val_mut_copy(request, args);
    yyjson_mut_obj_put(copy, yyjson_mut_str(request, "window_id"),
        yyjson_mut_uint(request, json_number(match, "window_id", 0)));
    /* The worker validates its own process identity, not the KWin PID. */
    yyjson_mut_obj_put(copy, yyjson_mut_str(request, "pid"), yyjson_mut_uint(request, match_pid));
    yyjson_mut_obj_put(copy, yyjson_mut_str(request, "tree_format"), yyjson_mut_str(request, "both"));
    yyjson_doc *parameters = yyjson_mut_val_imut_copy(copy, NULL);
    yyjson_mut_doc_free(request);
    yyjson_doc_free(listing);
    JsonResult *tree = atspi_worker_call("window_state", yyjson_doc_get_root(parameters));
    yyjson_doc_free(parameters);
    if (!tree) {
        return NULL;
    }
    result_boolean(tree, "pid_matched", best == 2);
    JsonResult *rows = yyjson_mut_obj_get(tree, "elements"), *row;
    yyjson_mut_arr_foreach(rows, i, n, row) {
        yyjson_doc *raw = yyjson_mut_val_imut_copy(yyjson_mut_obj_get(row, "frame"), NULL);
        CanvasRect rect;
        if (raw && read_rect(yyjson_doc_get_root(raw), &rect)) {
            rect.x = client.x + (rect.x - anchor.x) * ratio;
            rect.y = client.y + (rect.y - anchor.y) * ratio;
            rect.width *= ratio;
            rect.height *= ratio;
            result_member(row, "frame", overview_rect(&rect));
        } else {
            result_member(row, "frame", yyjson_mut_null(scu.doc));
        }
        yyjson_doc_free(raw);
    }
    const char *format = json_string(args, "tree_format");
    if (!format || !strcmp(format, "markdown")) {
        yyjson_mut_obj_remove_str(tree, "elements");
    } else if (!strcmp(format, "elements")) {
        yyjson_mut_obj_remove_str(tree, "tree_markdown");
    }
    return tree;
}

static JsonValue *requested_kwin_window(JsonValue *args)
{
    size_t i, n;
    JsonValue *window;
    yyjson_arr_foreach(json_member(yyjson_doc_get_root(visual.geometry), "windows"), i, n, window) {
        if (json_string(window, "key") && kwin_window_id(json_string(window, "key")) ==
                json_number(args, "window_id", -1) && json_number(window, "pid", -1) ==
                json_number(args, "pid", -2)) {
            return window;
        }
    }
    return NULL;
}

static JsonResult *window_image(JsonValue *args, JsonValue *window)
{
    CanvasRect frame;
    if (!read_rect(json_member(window, "frame"), &frame) || !prepare_capture()) {
        return NULL;
    }
    guint width = 0, height = 0;
    GBytes *source = capture_pixels("CaptureWindow",
        g_variant_new("(s)", json_string(window, "key")), &width, &height);
    if (!source) {
        return NULL;
    }
    guint maximum = json_number(args, "max_image_dimension", 1600);
    double scale = maximum && MAX(width, height) > maximum ? (double)maximum / MAX(width, height) : 1;
    guint output_width = MAX(1, (guint)(width * scale));
    guint output_height = MAX(1, (guint)(height * scale));
    CanvasLayout sampling = {.count = 1};
    sampling.tiles[0] = frame;
    sampling.native_widths[0] = width;
    sampling.native_heights[0] = height;
    GBytes *pixels = render_canvas(&sampling, &frame, output_width, output_height, &source);
    g_bytes_unref(source);
    if (!pixels) {
        return NULL;
    }
    gboolean saved = append_image(pixels, output_width, output_height);
    g_bytes_unref(pixels);
    if (!saved) {
        return NULL;
    }
    JsonResult *screenshot = result_object();
    result_integer(screenshot, "width", output_width);
    result_integer(screenshot, "height", output_height);
    result_member(screenshot, "bounds", overview_rect(&frame));
    JsonResult *result = result_object();
    result_member(result, "screenshot", screenshot);
    visual.observed_input_sequence = visual.input_sequence;
    return result;
}

static JsonResult *observe_window(JsonValue *args)
{
    if (!visual.canvas.id || !validate_canvas(0)) {
        clear_tool_error();
        guint images = yyjson_mut_arr_size(scu.images);
        if (!capture_desktop(NULL)) {
            return NULL;
        }
        yyjson_mut_arr_remove_range(scu.images, images, yyjson_mut_arr_size(scu.images) - images);
    }
    JsonValue *window = requested_kwin_window(args);
    if (!window) {
        return scu_fail("window_unavailable", "KWin window is unavailable. Call status again.");
    }
    char *signature = yyjson_val_write(window, 0, NULL);
    guint images = yyjson_mut_arr_size(scu.images);
    JsonResult *result = NULL, *tree_error = NULL;
    JsonResult *tree = window_tree(args, window);
    if (!tree) {
        if (g_strcmp0(scu.code, "accessibility_window_unavailable") &&
            g_strcmp0(scu.code, "accessibility_unavailable")) {
            goto done;
        }
        tree_error = error_payload();
        clear_tool_error();
    }
    JsonResult *image = window_image(args, window);
    if (!image) {
        if (!tree || !scu_tick() || !require_computer_access()) {
            goto done;
        }
        result_member(tree, "screenshot_error", error_payload());
        clear_tool_error();
    }
    if (!refresh_geometry() || !validate_canvas(0)) {
        goto done;
    }
    window = requested_kwin_window(args);
    char *current = window ? yyjson_val_write(window, 0, NULL) : NULL;
    gboolean same = current && !strcmp(signature, current);
    free(current);
    if (!same) {
        scu_fail("stale_window_geometry", "Window changed during observation. Observe again.");
        goto done;
    }
    result = tree ? tree : image;
    if (tree && image) {
        result_member(tree, "screenshot", yyjson_mut_obj_get(image, "screenshot"));
    } else if (tree_error) {
        result_member(image, "accessibility_error", tree_error);
    }
    result_integer(result, "pid", json_number(args, "pid", 0));
    result_integer(result, "window_id", json_number(args, "window_id", 0));
    result_integer(result, "id", visual.canvas.id);
done:
    free(signature);
    if (!result) {
        yyjson_mut_arr_remove_range(scu.images, images, yyjson_mut_arr_size(scu.images) - images);
    }
    return result;
}


static InputDevice *capable_device(enum ei_device_capability capability)
{
    if (visual.devices == NULL) {
        return NULL;
    }
    for (guint index = 0; index < visual.devices->len; index++) {
        InputDevice *device = g_ptr_array_index(visual.devices, index);
        if (device->resumed &&
            ei_api.device_has_capability(device->device, capability)) {
            return device;
        }
    }
    return NULL;
}

static InputDevice *wait_for_device(enum ei_device_capability capability)
{
    gint64 deadline = g_get_monotonic_time() + 3 * G_USEC_PER_SEC;
    InputDevice *device;
    while ((device = capable_device(capability)) == NULL &&
           g_get_monotonic_time() < deadline) {
        if (!visual_step() || !scu_pause(5)) {
            return NULL;
        }
    }
    if (device == NULL) {
        scu_fail("input_unavailable",
            "The granted EIS session has no resumed device for this input operation.");
    }
    return device;
}

/* Resolve overview coordinates without ever interpreting them as zoom-local.
 * A gap is allowed only for intermediate samples of a held-button drag. */
static gboolean pointer_target(JsonValue *arguments, double x, double y,
    gboolean allow_gap, struct ei_device **device_out, double *screen_x, double *screen_y)
{
    *device_out = NULL;
    if (!validate_canvas(yyjson_get_uint(json_member(arguments, "id")))) {
        return FALSE;
    }
    CanvasSnapshot *canvas = &visual.canvas;
    if (!isfinite(x) || !isfinite(y) || x < 0 || y < 0 ||
        x >= canvas->width || y >= canvas->height) {
        scu_fail("invalid_coordinates", "Coordinates lie outside the full desktop overview.");
        return FALSE;
    }
    double logical_x = canvas->layout.x + x * canvas->layout.width / canvas->width;
    double logical_y = canvas->layout.y + y * canvas->layout.height / canvas->height;
    for (guint i = 0; i < canvas->layout.count; i++) {
        CanvasRect *tile = &canvas->layout.tiles[i];
        if (!rect_contains(tile, logical_x, logical_y)) {
            continue;
        }
        RegionBinding binding;
        if (!bind_stream_region(&visual.displays[i], &binding)) {
            scu_fail("ambiguous_display_mapping",
                "The image and input region cannot be paired uniquely. No pointer input was sent.");
            return FALSE;
        }
        *device_out = binding.device;
        *screen_x = (int32_t)ei_api.region_get_x(binding.region) +
                    (logical_x - tile->x) * ei_api.region_get_width(binding.region) / tile->width;
        *screen_y = (int32_t)ei_api.region_get_y(binding.region) +
                    (logical_y - tile->y) * ei_api.region_get_height(binding.region) / tile->height;
        return TRUE;
    }
    if (allow_gap) {
        return TRUE;
    }
    scu_fail("invalid_coordinates", "Coordinates fall in a gap between granted desktop regions.");
    return FALSE;
}

static guint button_code(const char *button)
{
    if (g_strcmp0(button, "right") == 0) {
        return BTN_RIGHT;
    }
    if (g_strcmp0(button, "middle") == 0) {
        return BTN_MIDDLE;
    }
    return BTN_LEFT;
}

static gboolean device_resumed(struct ei_device *raw);

static InputDevice *seat_device(struct ei_device *pointer, enum ei_device_capability capability)
{
    /* KWin exposes both relative and absolute pointer devices. Keep motion,
     * buttons and scrolling on the same device whenever it supports them;
     * use another device on the seat only when a capability is split out. */
    for (guint i = 0; visual.devices && i < visual.devices->len; i++) {
        InputDevice *device = g_ptr_array_index(visual.devices, i);
        if (device->device == pointer && device->resumed &&
            ei_api.device_has_capability(pointer, capability)) {
            return device;
        }
    }
    struct ei_seat *seat = ei_api.device_get_seat(pointer);
    for (guint i = 0; visual.devices && i < visual.devices->len; i++) {
        InputDevice *device = g_ptr_array_index(visual.devices, i);
        if (device->resumed && ei_api.device_get_seat(device->device) == seat &&
            ei_api.device_has_capability(device->device, capability)) {
            return device;
        }
    }
    scu_fail("input_unavailable", "The pointer's input seat has no compatible resumed device.");
    return NULL;
}

static gboolean click_pointer(struct ei_device *button_device, guint button, guint count)
{
    for (guint i = 0; i < count; i++) {
        if (!visual_step() || !device_resumed(button_device)) {
            return FALSE;
        }
        ei_api.device_button_button(button_device, button, true);
        input_frame(button_device);
        visual.held_device = button_device;
        visual.held_button = button;
        if (!scu_pause(20)) {
            release_input();
            return FALSE;
        }
        release_input();
        if (i + 1 < count && !scu_pause(60)) {
            return FALSE;
        }
    }
    return TRUE;
}

/* Slab intersection catches even narrow regions between timed drag samples.
 * Check every occupied segment's route before any movement or button press. */
static gboolean drag_crosses_tile(const CanvasRect *tile, double x, double y,
    double to_x, double to_y)
{
    double enter = 0, leave = 1;
    double origins[] = {x, y}, deltas[] = {to_x - x, to_y - y};
    double minimum[] = {tile->x, tile->y};
    double maximum[] = {tile->x + tile->width, tile->y + tile->height};
    for (guint axis = 0; axis < 2; axis++) {
        if (deltas[axis] == 0) {
            if (origins[axis] < minimum[axis] || origins[axis] >= maximum[axis]) {
                return FALSE;
            }
            continue;
        }
        double a = (minimum[axis] - origins[axis]) / deltas[axis];
        double b = (maximum[axis] - origins[axis]) / deltas[axis];
        enter = MAX(enter, MIN(a, b));
        leave = MIN(leave, MAX(a, b));
    }
    return enter < leave;
}

typedef struct {
    struct ei_device *device; /* Owned until this operation finishes. */
    double x, y;
} PointerSample;

static JsonResult *pointer_action(const char *tool, JsonValue *arguments)
{
    gboolean drag = strcmp(tool, "drag") == 0;
    double x = json_number(arguments, drag ? "from_x" : "x", -1);
    double y = json_number(arguments, drag ? "from_y" : "y", -1);
    double to_x = drag ? json_number(arguments, "to_x", -1) : x;
    double to_y = drag ? json_number(arguments, "to_y", -1) : y;
    guint duration = (guint)json_number(arguments, "duration_ms", 500);
    guint count = drag ? MAX(1, duration / 16) : 0;
    PointerSample samples[626] = {0};
    struct ei_device *buttons = NULL, *scroll = NULL;
    JsonResult *result = NULL;
    if (wait_for_device(EI_DEVICE_CAP_POINTER_ABSOLUTE) == NULL) {
        return NULL;
    }
    for (guint i = 0; i <= count; i++) {
        double fraction = count ? (double)i / count : 0;
        struct ei_device *raw = NULL;
        if (!pointer_target(arguments, x + (to_x - x) * fraction,
                y + (to_y - y) * fraction, drag && i > 0 && i < count,
                &raw, &samples[i].x, &samples[i].y)) {
            goto done;
        }
        if (raw) {
            samples[i].device = ei_api.device_ref(raw);
        }
    }
    struct ei_device *pointer = samples[0].device;
    struct ei_seat *seat = ei_api.device_get_seat(pointer);
    if (drag) {
        CanvasSnapshot *canvas = &visual.canvas;
        double logical_x = canvas->layout.x + x * canvas->layout.width / canvas->width;
        double logical_y = canvas->layout.y + y * canvas->layout.height / canvas->height;
        double logical_to_x = canvas->layout.x + to_x * canvas->layout.width / canvas->width;
        double logical_to_y = canvas->layout.y + to_y * canvas->layout.height / canvas->height;
        for (guint i = 0; i < canvas->layout.count; i++) {
            if (!drag_crosses_tile(&canvas->layout.tiles[i], logical_x, logical_y,
                    logical_to_x, logical_to_y)) {
                continue;
            }
            RegionBinding binding;
            if (!bind_stream_region(&visual.displays[i], &binding) ||
                ei_api.device_get_seat(binding.device) != seat) {
                scu_fail("input_unavailable",
                    "The drag crosses an ambiguous region or incompatible input seat. No input was sent.");
                goto done;
            }
        }
    }
    gboolean click = strcmp(tool, "move_cursor") != 0 && strcmp(tool, "scroll") != 0;
    if (click) {
        InputDevice *device = seat_device(pointer, EI_DEVICE_CAP_BUTTON);
        if (!device) {
            goto done;
        }
        buttons = ei_api.device_ref(device->device);
    }
    if (!strcmp(tool, "scroll")) {
        InputDevice *device = seat_device(pointer, EI_DEVICE_CAP_SCROLL);
        if (!device) {
            goto done;
        }
        scroll = ei_api.device_ref(device->device);
    }
    /* References keep removed devices alive; resumed checks prevent submitting
     * to them. A mapping change during a drag aborts rather than retargeting. */
    for (guint i = 0; i <= count; i++) {
        if ((i && !scu_pause(duration / count)) || !visual_step() ||
            !validate_canvas(yyjson_get_uint(json_member(arguments, "id"))) ||
            (buttons && !device_resumed(buttons)) || (scroll && !device_resumed(scroll))) {
            goto done;
        }
        struct ei_device *raw = samples[i].device;
        if (raw) {
            if (!device_resumed(raw)) {
                goto done;
            }
            ei_api.device_pointer_motion_absolute(raw, samples[i].x, samples[i].y);
            input_frame(raw);
        }
        if (i == 0 && drag) {
            guint code = button_code(json_string(arguments, "button"));
            ei_api.device_button_button(buttons, code, true);
            input_frame(buttons);
            visual.held_device = buttons;
            visual.held_button = code;
        }
    }
    if (drag) {
        release_input();
    } else if (scroll) {
        const char *direction = json_string(arguments, "direction");
        gint amount = (gint)json_number(arguments, "amount", 1) * 120;
        gint dx = !strcmp(direction, "left") ? -amount : !strcmp(direction, "right") ? amount : 0;
        gint dy = !strcmp(direction, "up") ? -amount : !strcmp(direction, "down") ? amount : 0;
        ei_api.device_scroll_discrete(scroll, dx, dy);
        input_frame(scroll);
    } else if (buttons) {
        guint repeats = !strcmp(tool, "double_click") ? 2 : (guint)json_number(arguments, "count", 1);
        guint code = !strcmp(tool, "right_click") ? BTN_RIGHT : button_code(json_string(arguments, "button"));
        if (!click_pointer(buttons, code, repeats)) {
            goto done;
        }
    }
    result = scu_action("global_input", FALSE, "Pointer input submitted; inspect the resulting state.");
done:
    release_input();
    for (guint i = 0; i <= count; i++) {
        if (samples[i].device) {
            ei_api.device_unref(samples[i].device);
        }
    }
    if (buttons) {
        ei_api.device_unref(buttons);
    }
    if (scroll) {
        ei_api.device_unref(scroll);
    }
    return result;
}

typedef struct {
    struct xkb_context *context;
    struct xkb_keymap *keymap;
    struct xkb_state *state;
    guint depressed, latched, locked, group;
} KeyboardMap;

typedef struct {
    guint code;
    gboolean shift;
} KeyStroke;

static void free_keyboard_map(KeyboardMap *map)
{
    if (map->state != NULL) {
        xkb.state_unref(map->state);
    }
    if (map->keymap != NULL) {
        xkb.keymap_unref(map->keymap);
    }
    if (map->context != NULL) {
        xkb.context_unref(map->context);
    }
}

static gboolean load_keyboard_map(struct ei_device *device, KeyboardMap *map)
{
    struct ei_keymap *keymap = ei_api.device_keyboard_get_keymap(device);
    if (keymap == NULL || ei_api.keymap_get_type(keymap) != EI_KEYMAP_TYPE_XKB) {
        return FALSE;
    }
    size_t length = ei_api.keymap_get_size(keymap);
    if (length == 0 || length > 4 * 1024 * 1024) {
        return FALSE;
    }
    void *memory =
        mmap(NULL, length, PROT_READ, MAP_PRIVATE, ei_api.keymap_get_fd(keymap), 0);
    if (memory == MAP_FAILED) {
        return FALSE;
    }
    char *contents = g_strndup(memory, length);
    munmap(memory, length);
    map->context = xkb.context_new(XKB_CONTEXT_NO_FLAGS);
    map->keymap = map->context != NULL
                      ? xkb.keymap_new_from_string(map->context, contents,
                            XKB_KEYMAP_FORMAT_TEXT_V1, XKB_KEYMAP_COMPILE_NO_FLAGS)
                      : NULL;
    g_free(contents);
    if (map->keymap == NULL || (map->state = xkb.state_new(map->keymap)) == NULL) {
        return FALSE;
    }
    for (guint index = 0; index < visual.devices->len; index++) {
        InputDevice *input = g_ptr_array_index(visual.devices, index);
        if (input->device == device) {
            map->depressed = input->depressed;
            map->latched = input->latched;
            map->locked = input->locked;
            map->group = input->group;
            break;
        }
    }
    return TRUE;
}

/* Resolve against the compositor-provided keymap, never a hard-coded US keycode
 * table. Only base/Shift levels are synthesized; other text uses EI text or paste. */
static gboolean lookup_keysym(KeyboardMap *map, xkb_keysym_t symbol, KeyStroke *stroke)
{
    xkb_mod_index_t shift = xkb.keymap_mod_get_index(map->keymap, XKB_MOD_NAME_SHIFT);
    xkb_keycode_t maximum = MIN(xkb.keymap_max_keycode(map->keymap), 767);
    for (guint level = 0; level < 2; level++) {
        if (level != 0 && shift == XKB_MOD_INVALID) {
            break;
        }
        xkb_mod_mask_t modifiers = map->depressed | (level != 0 ? 1u << shift : 0);
        xkb.state_update_mask(
            map->state, modifiers, map->latched, map->locked, 0, 0, map->group);
        for (xkb_keycode_t code = MAX(8, xkb.keymap_min_keycode(map->keymap));
            code <= maximum; code++) {
            if (xkb.state_key_get_one_sym(map->state, code) == symbol) {
                stroke->code = code - 8;
                stroke->shift = level != 0;
                return TRUE;
            }
        }
    }
    return FALSE;
}

static xkb_keysym_t named_keysym(const char *name)
{
    static const struct {
        const char *name, *symbol;
    } aliases[] = {{"enter", "Return"}, {"return", "Return"}, {"esc", "Escape"},
        {"escape", "Escape"}, {"ctrl", "Control_L"}, {"control", "Control_L"},
        {"shift", "Shift_L"}, {"alt", "Alt_L"}, {"super", "Super_L"},
        {"meta", "Super_L"}, {"win", "Super_L"}, {"pageup", "Prior"},
        {"pagedown", "Next"}, {"space", "space"}, {NULL, NULL}};
    for (guint index = 0; aliases[index].name != NULL; index++) {
        if (g_ascii_strcasecmp(name, aliases[index].name) == 0) {
            return xkb.keysym_from_name(aliases[index].symbol, XKB_KEYSYM_NO_FLAGS);
        }
    }
    return xkb.keysym_from_name(name, XKB_KEYSYM_CASE_INSENSITIVE);
}

static gboolean device_resumed(struct ei_device *raw)
{
    if (visual.devices == NULL) {
        return FALSE;
    }
    for (guint index = 0; index < visual.devices->len; index++) {
        InputDevice *device = g_ptr_array_index(visual.devices, index);
        if (device->device == raw && device->resumed) {
            return TRUE;
        }
    }
    scu_fail("input_unavailable",
        "The input device was removed or paused during the action.");
    return FALSE;
}

static gboolean press_codes(struct ei_device *device, const guint *codes, guint count)
{
    if (count == 0 || count > G_N_ELEMENTS(visual.held_keys) || !visual_step() ||
        !device_resumed(device)) {
        return FALSE;
    }
    visual.keyboard_device = device;
    for (guint index = 0; index < count; index++) {
        ei_api.device_keyboard_key(device, codes[index], true);
        input_frame(device);
        visual.held_keys[visual.held_key_count++] = codes[index];
    }
    gboolean ok = scu_pause(10);
    release_input();
    return ok;
}

static JsonResult *keyboard_action(const char *tool, JsonValue *arguments)
{
    InputDevice *input = wait_for_device(EI_DEVICE_CAP_KEYBOARD);
    if (input == NULL) {
        return NULL;
    }
    struct ei_device *device = input->device;
    KeyboardMap map = {0};
    if (!load_keyboard_map(device, &map)) {
        free_keyboard_map(&map);
        return scu_fail("keyboard_mapping_unavailable",
            "The compositor did not supply a usable XKB keyboard map.");
    }
    const char *names[6] = {0};
    guint count = 0;
    JsonValue *array =
        json_member(arguments, strcmp(tool, "hotkey") == 0 ? "keys" : "modifiers");
    size_t index, size;
    JsonValue *item;
    yyjson_arr_foreach(array, index, size, item) names[count++] = yyjson_get_str(item);
    if (strcmp(tool, "press_key") == 0) {
        names[count++] = json_string(arguments, "key");
    }
    guint codes[8], code_count = 0;
    for (guint position = 0; position < count; position++) {
        KeyStroke stroke;
        xkb_keysym_t symbol = named_keysym(names[position]);
        if (symbol == XKB_KEY_NoSymbol || !lookup_keysym(&map, symbol, &stroke)) {
            free_keyboard_map(&map);
            return scu_fail("unsupported_key",
                "A requested key is absent from the compositor's "
                "keyboard map. No key was pressed.");
        }
        guint candidates[2] = {stroke.code, 0};
        guint candidate_count = 1;
        if (stroke.shift) {
            KeyStroke shift;
            if (!lookup_keysym(&map, XKB_KEY_Shift_L, &shift)) {
                free_keyboard_map(&map);
                return scu_fail(
                    "unsupported_key", "Shift is unavailable in the keyboard map.");
            }
            candidates[0] = shift.code;
            candidates[1] = stroke.code;
            candidate_count = 2;
        }
        /* A chord may imply Shift for several symbols. Press each physical key
         * once, and check capacity before every write to the held-key array. */
        for (guint candidate = 0; candidate < candidate_count; candidate++) {
            gboolean present = FALSE;
            for (guint existing = 0; existing < code_count; existing++) {
                present |= codes[existing] == candidates[candidate];
            }
            if (present) {
                continue;
            }
            if (code_count >= G_N_ELEMENTS(codes)) {
                free_keyboard_map(&map);
                return scu_fail("invalid_arguments", "Too many keys in this chord.");
            }
            codes[code_count++] = candidates[candidate];
        }
    }
    free_keyboard_map(&map);
    if (!press_codes(device, codes, code_count)) {
        return NULL;
    }
    return scu_action("global_input", FALSE,
        "Foreground key chord submitted; inspect the resulting state.");
}

static gboolean paste_text(struct ei_device *device, KeyboardMap *map, const char *text)
{
    KeyStroke control, paste;
    if (!lookup_keysym(map, XKB_KEY_Control_L, &control) ||
        !lookup_keysym(map, XKB_KEY_v, &paste)) {
        scu_fail("unsupported_key",
            "Clipboard paste shortcut is unavailable in the keyboard map.");
        return FALSE;
    }
    gboolean saved_selection = FALSE;
    JsonValue *active = NULL, *window;
    size_t index, count;
    yyjson_arr_foreach(json_member(yyjson_doc_get_root(visual.geometry), "windows"), index, count, window) {
        if (json_boolean(window, "active")) {
            active = window;
            break;
        }
    }
    if (active && scu_library("libatspi.so.0") && scu_tick()) {
        JsonResult *target = result_object();
        result_integer(target, "pid", json_number(active, "pid", 0));
        result_string(target, "title", json_string(active, "title"));
        yyjson_doc *parameters = yyjson_mut_val_imut_copy(target, NULL);
        JsonResult *guard = atspi_worker_call("save_selection", yyjson_doc_get_root(parameters));
        yyjson_doc_free(parameters);
        if (!guard) {
            return FALSE;
        }
        saved_selection = yyjson_mut_is_true(yyjson_mut_obj_get(guard, "saved"));
    } else {
        /* A missing AT-SPI bridge does not forbid foreground paste. The
         * clipboard/PRIMARY replacement warning still describes its effects. */
        g_clear_pointer(&scu.code, g_free);
        g_clear_pointer(&scu.hint, g_free);
    }
    GError *error = NULL;
    GVariant *reply = host_call("org.kde.klipper", "/klipper", "org.kde.klipper.klipper",
        "setClipboardContents", g_variant_new("(s)", text), NULL, NULL, &error);
    if (!reply) {
        scu_fail("clipboard_unavailable", error ? error->message : "Host clipboard is unavailable.");
    }
    g_clear_error(&error);
    if (!reply) {
        return FALSE;
    }
    g_variant_unref(reply);
    if (saved_selection) {
        if (!scu_pause(50) || !atspi_worker_call("restore_selection", NULL) ||
            !scu_pause(50) || !atspi_worker_call("verify_selection", NULL)) {
            return FALSE;
        }
    }
    /* Ctrl+V addresses the regular clipboard. Klipper's setter also replaces
     * PRIMARY, which can clear a GTK selection; report that material effect. */
    guint codes[] = {control.code, paste.code};
    if (!press_codes(device, codes, G_N_ELEMENTS(codes))) {
        return FALSE;
    }
    /* Allow the receiving application to consume the new clipboard selection. */
    return scu_pause(100);
}

static JsonResult *type_foreground_text(JsonValue *arguments)
{
    const char *text = json_string(arguments, "text");
    if (*text == '\0') {
        return scu_action(
            "global_input", FALSE, "Empty text: no input sent.");
    }
    InputDevice *text_device = capable_device(EI_DEVICE_CAP_TEXT);
    if (text_device != NULL && ei_api.device_text_utf8 != NULL) {
        struct ei_device *device = text_device->device;
        /* Keep chunks under the protocol string bound, without splitting UTF-8. */
        for (const char *cursor = text; *cursor != '\0';) {
            const char *end = cursor;
            while (*end != '\0' && g_utf8_next_char(end) - cursor <= 240) {
                end = g_utf8_next_char(end);
            }
            if (!visual_step() || !device_resumed(device)) {
                return NULL;
            }
            char *chunk = g_strndup(cursor, end - cursor);
            ei_api.device_text_utf8(device, chunk);
            input_frame(device);
            g_free(chunk);
            cursor = end;
        }
        return scu_action("global_input", FALSE,
            "UTF-8 input submitted through EIS text; verify the visible result.");
    }
    InputDevice *input = wait_for_device(EI_DEVICE_CAP_KEYBOARD);
    if (input == NULL) {
        return NULL;
    }
    struct ei_device *device = input->device;
    KeyboardMap map = {0};
    if (!load_keyboard_map(device, &map)) {
        free_keyboard_map(&map);
        return scu_fail("keyboard_mapping_unavailable",
            "No compositor XKB map is available for text input.");
    }
    GArray *strokes = g_array_new(FALSE, FALSE, sizeof(KeyStroke));
    gboolean representable = TRUE;
    for (const char *cursor = text; *cursor != '\0';
        cursor = g_utf8_next_char(cursor)) {
        KeyStroke stroke;
        if (!lookup_keysym(
                &map, xkb.utf32_to_keysym(g_utf8_get_char(cursor)), &stroke)) {
            representable = FALSE;
            break;
        }
        g_array_append_val(strokes, stroke);
    }
    KeyStroke shift;
    if (representable && !lookup_keysym(&map, XKB_KEY_Shift_L, &shift)) {
        representable = FALSE;
    }
    if (!representable) {
        gboolean ok = paste_text(device, &map, text);
        g_array_unref(strokes);
        free_keyboard_map(&map);
        if (!ok) {
            return NULL;
        }
        JsonResult *result = scu_action("global_input", FALSE, NULL);
        result_string(result, "route", "clipboard_paste");
        result_string(result, "warning", "Clipboard and primary selection text replaced.");
        return result;
    }
    free_keyboard_map(&map);
    for (guint index = 0; index < strokes->len; index++) {
        KeyStroke stroke = g_array_index(strokes, KeyStroke, index);
        guint codes[] = {shift.code, stroke.code};
        if (!press_codes(
                device, stroke.shift ? codes : &codes[1], stroke.shift ? 2 : 1)) {
            g_array_unref(strokes);
            return NULL;
        }
    }
    g_array_unref(strokes);
    return scu_action("global_input", FALSE,
        "Text submitted using the compositor's keyboard map; verify the visible "
        "result.");
}

static JsonResult *scu_visual(const char *tool, JsonValue *arguments)
{
    if (!ensure_visual_session() || !visual_step()) {
        return NULL;
    }
    if (strcmp(tool, "press_key") == 0 || strcmp(tool, "hotkey") == 0) {
        return keyboard_action(tool, arguments);
    }
    if (strcmp(tool, "type_text") == 0) {
        return type_foreground_text(arguments);
    }
    JsonResult *result = pointer_action(tool, arguments);
    if (result == NULL) {
        release_input();
    }
    return result;
}

/*
 * Tool argument definitions are data, not embedded JSON strings. The same
 * definitions build tools/list schemas and validate direct and batched calls.
 * Optional upstream arguments are absent until their behavior is implemented.
 */
/* The only shared action default is a pixel capture. Semantic tokens carry
 * their own identity; window observations always name their window explicitly. */
static yyjson_doc *prepare_arguments(JsonValue *args, guint64 id,
    gboolean observation)
{
    yyjson_mut_doc *document = yyjson_mut_doc_new(NULL);
    JsonResult *copy = yyjson_val_mut_copy(document, args);
    yyjson_mut_doc_set_root(document, copy);
    if (observation) {
        yyjson_mut_obj_remove_str(copy, "kind");
    } else if (id && !json_string(args, "element_token") &&
        (json_member(args, "x") || json_member(args, "from_x")) &&
        !json_member(args, "id")) {
        yyjson_mut_obj_add_uint(document, copy, "id", id);
    }
    yyjson_doc *result = yyjson_mut_doc_imut_copy(document, NULL);
    yyjson_mut_doc_free(document);
    return result;
}

static void clear_tool_error(void)
{
    g_clear_pointer(&scu.code, g_free);
    g_clear_pointer(&scu.hint, g_free);
}

/* Both explicit and automatic pauses remain cancellable and notice revoked
 * access even when no visual session has been opened. */
static gboolean wait_for_delay(unsigned ms)
{
    gint64 end = g_get_monotonic_time() + (gint64)ms * 1000;
    while (g_get_monotonic_time() < end) {
        gint64 remaining = end - g_get_monotonic_time();
        if (!scu_pause((unsigned)MIN(10, MAX(0, (remaining + 999) / 1000))) ||
            !require_computer_access()) {
            return FALSE;
        }
    }
    return TRUE;
}

static JsonResult *batch_failure(guint index, const Tool *tool)
{
    JsonResult *failure = error_payload();
    result_integer(failure, "index", index);
    result_string(failure, "tool", tool->name);
    return failure;
}

static JsonResult *compact_observation(JsonResult *observation)
{
    JsonResult *result = result_object();
    const char *fields[] = {"id", "width", "height", "zoom", NULL};
    for (unsigned index = 0; fields[index]; index++) {
        JsonResult *value = yyjson_mut_obj_get(observation, fields[index]);
        if (value != NULL) {
            result_member(result, fields[index], yyjson_mut_val_mut_copy(scu.doc, value));
        }
    }
    return result;
}

/* Preflight structure and supported options for the entire batch before effects.
 * Runtime failure keeps completed counts and one error, never a replayable dump
 * of successful steps. Observation is independent of action confirmation. */
static JsonResult *computer(JsonValue *args)
{
    yyjson_doc *steps[64] = {0};
    const Tool *actions[64] = {0};
    JsonValue *array = json_member(args, "steps");
    size_t count = yyjson_arr_size(array), index, length;
    JsonValue *step;
    yyjson_doc *observation = NULL;
    const Tool *observer = NULL;
    JsonValue *observe = json_member(args, "observe");
    gboolean automatic = observe == NULL, has_foreground = FALSE, has_actions = FALSE;
    gboolean valid = FALSE, previous_delay = FALSE;
    JsonResult *result = result_object(), *warnings = result_array();

    yyjson_arr_foreach(array, index, length, step) {
        const char *name = json_string(step, "tool");
        JsonValue *arguments = json_member(step, "args");
        const Tool *action = actions[index] = find_action(name);
        if (!action || yyjson_obj_size(step) != 2 || !yyjson_is_obj(arguments)) {
            goto invalid;
        }
        steps[index] = prepare_arguments(arguments, yyjson_get_uint(json_member(args, "id")), FALSE);
        JsonValue *merged = yyjson_doc_get_root(steps[index]);
        if (!validate_arguments(action, merged)) {
            goto done;
        }
        if (strcmp(action->name, "delay_ms")) {
            has_actions = TRUE;
            has_foreground |= json_string(merged, "element_token") == NULL;
        }
    }
    /* Delays are neutral: semantic-only sequences stay semantic. With no
     * input actions, they provide a delayed observation of ongoing loading. */
    if (automatic && (!has_actions || has_foreground)) {
        const char *defaults = "{\"max_image_dimension\":1600}";
        observation = yyjson_read(defaults, strlen(defaults), 0);
        observer = &desktop_schema;
    } else if (yyjson_is_obj(observe)) {
        gboolean desktop = g_strcmp0(json_string(observe, "kind"), "desktop") == 0;
        observation = prepare_arguments(observe, 0, TRUE);
        observer = desktop ? &desktop_schema : &window_schema;
    }
    if (observer && !validate_arguments(observer, yyjson_doc_get_root(observation))) {
        goto done;
    }
    if (observer == &desktop_schema &&
        json_member(yyjson_doc_get_root(observation), "zoom") && visual.canvas.id &&
        !refresh_geometry()) {
        goto done;
    }
    if (observer == &desktop_schema &&
        !validate_zoom(yyjson_doc_get_root(observation), yyjson_get_uint(json_member(args, "id")))) {
        goto done;
    }
    if (!require_computer_ready()) {
        goto done;
    }
    valid = TRUE;
    guint completed = 0;
    gboolean failed = FALSE, foreground_attempted = FALSE;
    for (index = 0; index < count; index++) {
        JsonValue *merged = yyjson_doc_get_root(steps[index]);
        gboolean delay = !strcmp(actions[index]->name, "delay_ms");
        foreground_attempted |= !delay &&
            json_string(merged, "element_token") == NULL;
        /* A delay step supplies its own gap. Other action boundaries retain
         * their default wait, including after an earlier explicit delay. */
        JsonResult *value = NULL;
        if (!index || delay || previous_delay || wait_for_delay(150)) {
            value = execute_tool(actions[index], merged);
        }
        previous_delay = value != NULL && delay;
        if (value == NULL) {
            release_input();
            result_member(result, "failed", batch_failure(index, actions[index]));
            failed = TRUE;
            clear_tool_error();
            break;
        }
        completed++;
        JsonResult *warning = yyjson_mut_obj_get(value, "warning");
        if (warning) {
            JsonResult *item = result_object();
            result_integer(item, "index", index);
            result_string(item, "tool", actions[index]->name);
            result_member(item, "warning", yyjson_mut_val_mut_copy(scu.doc, warning));
            result_append(warnings, item);
        }
    }
    result_integer(result, "completed", completed);
    if (yyjson_mut_arr_size(warnings) > 0) {
        result_member(result, "warnings", warnings);
    }
    if (observer && (!automatic || !has_actions || foreground_attempted) &&
        !g_atomic_int_get(&scu.cancelled) && !g_atomic_int_get(&scu.stopping)) {
        /* A broken visual session is not reconnected inside the interrupted
         * batch. Only a subsequent request may establish another grant. */
        JsonResult *value = NULL;
        /* Only a completed trailing delay replaces the final rendering wait. */
        if (!previous_delay && !wait_for_delay(300)) {
            release_input();
        } else if (visual.broken || (failed && foreground_attempted &&
                visual.bus == NULL)) {
            scu_fail("visual_session_closed", "Visual session disconnected during the batch.");
        } else {
            if (ensure_visual_session()) {
                value = observer == &desktop_schema
                    ? capture_desktop(yyjson_doc_get_root(observation))
                    : observe_window(yyjson_doc_get_root(observation));
            }
        }
        if (value) {
            result_member(result, "observation",
                observer == &desktop_schema ? compact_observation(value) : value);
        } else {
            result_member(result, "observation_error", error_payload());
            failed = TRUE;
            clear_tool_error();
        }
    }
    result_string(result, "status", failed ? "failed" : "ok");
    goto done;
invalid:
    scu_fail("invalid_arguments", "Each step must be {tool,args} naming a supported computer action.");
done:
    for (index = 0; index < count; index++) {
        yyjson_doc_free(steps[index]);
    }
    yyjson_doc_free(observation);
    return valid ? result : NULL;
}

static JsonResult *execute_tool(const Tool *t, JsonValue *args)
{
    if (!scu_tick()) {
        return NULL;
    }
    if (!strcmp(t->name, "status")) {
        return scu_status();
    }
    if (!strcmp(t->name, "computer")) {
        return computer(args);
    }
    if (!require_computer_access()) {
        return NULL;
    }
    if (!strcmp(t->name, "delay_ms")) {
        if (!wait_for_delay((unsigned)json_number(args, "ms", 0))) {
            return NULL;
        }
        return result_object();
    }
    if (!strcmp(t->name, "set_value") || json_string(args, "element_token")) {
        return scu_atspi(t->name, args);
    }
    return scu_visual(t->name, args);
}

/* Host-user service. The controller starts this only for an accessibility-tier
 * Space and mounts only its private peer socket into that Space. The host bus,
 * KWin scripting interface and capture authorization never reach the guest. */
#define GEOMETRY_PATH "/org/anatase/Spaces/Geometry"
#define GEOMETRY_INTERFACE "org.anatase.Spaces.Geometry1"
static const char geometry_xml[] =
    "<node><interface name='" GEOMETRY_INTERFACE "'>"
    "<method name='Publish'><arg type='s' direction='in'/><arg type='s' direction='in'/></method>"
    "</interface></node>";

typedef struct {
    GDBusConnection *bus;
    char *name, *owner, *token, *geometry, *script_path;
    int script_id, input_cookie;
} ComputerHost;
static ComputerHost host = {.script_id = -1, .input_cookie = -1};

static const char *current_geometry(void) { return host.geometry; }

static GVariant *host_call(const char *name, const char *path, const char *interface,
    const char *method, GVariant *args, GUnixFDList *input, GUnixFDList **output,
    GError **error)
{
    return bounded_call(host.bus, name, path, interface, method, args, input, output, error);
}

static char *kwin_owner(GError **error)
{
    GVariant *reply = host_call("org.freedesktop.DBus", "/org/freedesktop/DBus",
        "org.freedesktop.DBus", "GetNameOwner", g_variant_new("(s)", KWIN_NAME),
        NULL, NULL, error);
    char *owner = NULL;
    if (reply) {
        g_variant_get(reply, "(s)", &owner);
        g_variant_unref(reply);
    }
    return owner;
}

static void host_publish(GDBusConnection *connection, const char *sender,
    const char *path, const char *interface, const char *method, GVariant *args,
    GDBusMethodInvocation *invocation, gpointer data)
{
    (void)connection; (void)path; (void)interface; (void)method; (void)data;
    const char *token, *json;
    g_variant_get(args, "(&s&s)", &token, &json);
    if (g_strcmp0(sender, host.owner) || g_strcmp0(token, host.token) || strlen(json) > 1024 * 1024) {
        g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR,
            G_DBUS_ERROR_ACCESS_DENIED, "Invalid geometry publisher");
        return;
    }
    g_free(host.geometry);
    host.geometry = g_strdup(json);
    g_dbus_method_invocation_return_value(invocation, NULL);
}

/* Only these fixed expressions run inside KWin. No public method accepts script
 * code, a script path, a D-Bus destination or a window-management command. */
static const char geometry_script[] =
    "function rect(r){return {x:r.x,y:r.y,width:r.width,height:r.height};}\n"
    "var result={outputs:[],windows:[]};\n"
    "for(var i=0;i<workspace.screens.length;i++){var o=workspace.screens[i];"
    "result.outputs.push({name:o.name,geometry:rect(o.geometry),scale:o.devicePixelRatio===undefined?o.scale:o.devicePixelRatio});}\n"
    "var windows=workspace.windowList();\n"
    "for(var i=0;i<windows.length;i++){var w=windows[i];"
    "if(w.deleted||w.desktopWindow||w.dock)continue;"
    "var shown=!w.minimized&&(!w.desktops.length||w.desktops.indexOf(workspace.currentDesktop)>=0)"
    "&&(!w.activities.length||w.activities.indexOf(workspace.currentActivity)>=0);"
    "result.windows.push({key:String(w.internalId),pid:w.pid,app_name:w.resourceClass||w.resourceName,"
    "title:w.caption,frame:rect(w.frameGeometry),client:rect(w.clientGeometry),buffer:rect(w.bufferGeometry),shown:shown,active:w===workspace.activeWindow});}\n"
    "callDBus('%s','" GEOMETRY_PATH "','" GEOMETRY_INTERFACE "','Publish','%s',JSON.stringify(result));\n";

static void unload_geometry_script(void)
{
    if (host.script_id >= 0 && host.bus && !g_dbus_connection_is_closed(host.bus)) {
        char *path = g_strdup_printf("/Scripting/Script%d", host.script_id);
        GVariant *reply = g_dbus_connection_call_sync(host.bus, KWIN_NAME, path,
            "org.kde.kwin.Script", "stop", NULL, NULL,
            G_DBUS_CALL_FLAGS_NO_AUTO_START, 500, NULL, NULL);
        g_clear_pointer(&reply, g_variant_unref);
        g_free(path);
        /* unloadScript takes the unique plugin name, not a guest-supplied ID. */
        reply = g_dbus_connection_call_sync(host.bus, KWIN_NAME, "/Scripting",
            "org.kde.kwin.Scripting", "unloadScript", g_variant_new("(s)", host.token),
            NULL, G_DBUS_CALL_FLAGS_NO_AUTO_START, 500, NULL, NULL);
        g_clear_pointer(&reply, g_variant_unref);
    }
    host.script_id = -1;
    if (host.script_path) {
        unlink(host.script_path);
        g_clear_pointer(&host.script_path, g_free);
    }
}

static gboolean host_geometry(GError **error)
{
    char *owner = kwin_owner(error);
    if (!owner) {
        return FALSE;
    }
    g_free(host.owner);
    host.owner = owner;
    g_clear_pointer(&host.geometry, g_free);
    g_free(host.token);
    host.token = g_uuid_string_random();
    char *script = g_strdup_printf(geometry_script, host.name, host.token);
    int fd = g_file_open_tmp("spaces-scu-geometry-XXXXXX", &host.script_path, error);
    if (fd < 0) {
        g_free(script);
        return FALSE;
    }
    gsize length = strlen(script), offset = 0;
    while (offset < length) {
        ssize_t count = write(fd, script + offset, length - offset);
        if (count > 0) {
            offset += count;
        } else if (errno != EINTR) {
            break;
        }
    }
    close(fd);
    g_free(script);
    if (offset != length) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_FAILED, "Could not write the fixed KWin geometry script");
        unload_geometry_script();
        return FALSE;
    }
    GVariant *reply = host_call(KWIN_NAME, "/Scripting", "org.kde.kwin.Scripting",
        "loadScript", g_variant_new("(ss)", host.script_path, host.token), NULL, NULL, error);
    if (reply) {
        g_variant_get(reply, "(i)", &host.script_id);
        g_variant_unref(reply);
    }
    if (host.script_id < 0) {
        if (error && !*error) {
            g_set_error(error, G_IO_ERROR, G_IO_ERROR_FAILED, "KWin refused its fixed geometry script");
        }
        unload_geometry_script();
        return FALSE;
    }
    char *path = g_strdup_printf("/Scripting/Script%d", host.script_id);
    reply = host_call(KWIN_NAME, path, "org.kde.kwin.Script", "run", NULL, NULL, NULL, error);
    g_free(path);
    gboolean ran = reply != NULL;
    g_clear_pointer(&reply, g_variant_unref);
    while (ran && !host.geometry && g_get_monotonic_time() < scu.deadline &&
           scu_tick()) {
        while (g_main_context_iteration(NULL, FALSE)) {
        }
        g_usleep(1000);
    }
    unload_geometry_script();
    if (!host.geometry) {
        if (error && !*error) {
            g_set_error(error, G_IO_ERROR, G_IO_ERROR_TIMED_OUT, "Timed out reading KWin geometry");
        }
        return FALSE;
    }
    yyjson_doc *document = yyjson_read(host.geometry, strlen(host.geometry), 0);
    if (!document || !yyjson_is_obj(yyjson_doc_get_root(document))) {
        yyjson_doc_free(document);
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA, "KWin returned invalid geometry JSON");
        return FALSE;
    }
    yyjson_mut_doc *output = yyjson_doc_mut_copy(document, NULL);
    yyjson_mut_obj_add_strcpy(output, yyjson_mut_doc_get_root(output), "owner", host.owner);
    g_free(host.geometry);
    host.geometry = yyjson_mut_write(output, 0, NULL);
    yyjson_mut_doc_free(output);
    yyjson_doc_free(document);
    return TRUE;
}

static gboolean host_output_bounds(CanvasRect *bounds)
{
    yyjson_doc *doc = host.geometry ? yyjson_read(host.geometry, strlen(host.geometry), 0) : NULL;
    JsonValue *outputs = json_member(doc ? yyjson_doc_get_root(doc) : NULL, "outputs");
    guint count = yyjson_arr_size(outputs);
    gboolean valid = count > 0 && count <= MAX_DISPLAYS;
    for (guint i = 0; valid && i < count; i++) {
        CanvasRect rect;
        valid = read_rect(json_member(yyjson_arr_get(outputs, i), "geometry"), &rect);
        if (!valid) {
            break;
        }
        if (i == 0) {
            *bounds = rect;
        } else {
            double right = MAX(bounds->x + bounds->width, rect.x + rect.width);
            double bottom = MAX(bounds->y + bounds->height, rect.y + rect.height);
            bounds->x = MIN(bounds->x, rect.x);
            bounds->y = MIN(bounds->y, rect.y);
            bounds->width = right - bounds->x;
            bounds->height = bottom - bounds->y;
        }
    }
    yyjson_doc_free(doc);
    return valid;
}

/* Normalize the documented QImage raw formats to immutable RGB8. Bounds and
 * stride are validated before allocation or indexing; alpha is painted black. */
static GBytes *decode_kwin_pixels(const guchar *raw, gsize length, guint width,
    guint height, guint stride, guint format, GError **error)
{
    guint channels = format == 13 ? 3 : 4;
    gboolean supported = format == 4 || format == 5 || format == 6 || format == 13 ||
        format == 16 || format == 17 || format == 18;
    if (!supported || !width || !height || (guint64)width * height * 4 > MAX_FRAME_BYTES ||
        (guint64)width * channels > stride || (guint64)stride * height != length) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA, "Unsupported or malformed KWin image format/stride");
        return NULL;
    }
    guchar *pixels = g_try_malloc((gsize)width * height * 3);
    if (!pixels) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_NO_SPACE, "Cannot allocate KWin image");
        return NULL;
    }
    for (guint y = 0; y < height; y++) {
        for (guint x = 0; x < width; x++) {
            const guchar *in = raw + (gsize)y * stride + x * channels;
            guchar *out = pixels + ((gsize)y * width + x) * 3;
            guint alpha = 255;
            if (format >= 4 && format <= 6) {
                uint32_t argb;
                memcpy(&argb, in, 4);
                out[0] = argb >> 16; out[1] = argb >> 8; out[2] = argb;
                if (format == 5) {
                    alpha = argb >> 24;
                }
            } else {
                memcpy(out, in, 3);
                if (format == 17) {
                    alpha = in[3];
                }
            }
            for (guint c = 0; c < 3; c++) {
                out[c] = out[c] * alpha / 255;
            }
        }
    }
    return g_bytes_new_take(pixels, (gsize)width * height * 3);
}

static gboolean host_read_pixels(int fd, guchar *pixels, gsize length, GError **error)
{
    gsize offset = 0;
    while (offset < length && scu_tick()) {
        ssize_t count = read(fd, pixels + offset, MIN(length - offset, 1024u * 1024u));
        if (count > 0) {
            offset += count;
        } else if (count == 0 || (errno != EAGAIN && errno != EINTR)) {
            break;
        } else {
            while (g_main_context_iteration(NULL, FALSE)) {
            }
            struct pollfd pollfd = {.fd = fd, .events = POLLIN};
            poll(&pollfd, 1, 5);
        }
    }
    if (offset != length) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_TIMED_OUT, "KWin capture was truncated, disconnected or timed out");
        return FALSE;
    }
    return TRUE;
}

static GBytes *host_capture(GVariant *args, const char *window_key,
    guint *width, guint *height, GError **error)
{
    gint x = 0, y = 0, w = 0, h = 0;
    gboolean native = TRUE;
    if (!window_key) {
        g_variant_get(args, "(iiiib)", &x, &y, &w, &h, &native);
    }
    CanvasRect bounds = {0};
    if (!host_geometry(error)) {
        return NULL;
    }
    if (window_key) {
        yyjson_doc *geometry = yyjson_read(host.geometry, strlen(host.geometry), 0);
        size_t i, count;
        JsonValue *window;
        CanvasRect frame = {0};
        yyjson_arr_foreach(json_member(yyjson_doc_get_root(geometry), "windows"), i, count, window) {
            if (!g_strcmp0(json_string(window, "key"), window_key)) {
                read_rect(json_member(window, "frame"), &frame);
                break;
            }
        }
        yyjson_doc_free(geometry);
        if (frame.width <= 0 || frame.height <= 0) {
            g_set_error(error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND, "KWin window is unavailable");
            return NULL;
        }
        w = -lower_integer(-frame.width);
        h = -lower_integer(-frame.height);
    } else if (!host_output_bounds(&bounds) || w <= 0 || h <= 0 || x < bounds.x || y < bounds.y ||
        (gint64)x + w > bounds.x + bounds.width || (gint64)y + h > bounds.y + bounds.height) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT, "Capture is outside the KWin desktop");
        return NULL;
    }
    /* Refuse excessive work before asking the compositor to allocate an image,
     * rather than relying only on validating the metadata it returns later. */
    double density = 1;
    if (native) {
        yyjson_doc *geometry = yyjson_read(host.geometry, strlen(host.geometry), 0);
        JsonValue *outputs = json_member(yyjson_doc_get_root(geometry), "outputs");
        size_t i, count;
        JsonValue *output;
        yyjson_arr_foreach(outputs, i, count, output) {
            density = MAX(density, json_number(output, "scale", 1));
        }
        yyjson_doc_free(geometry);
    }
    if ((double)w * density * h * density * 4 > MAX_FRAME_BYTES) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_NO_SPACE, "Capture exceeds the 256 MiB image limit; request a smaller region");
        return NULL;
    }
    int pipes[2];
    if (pipe2(pipes, O_CLOEXEC | O_NONBLOCK) < 0) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_FAILED, "Cannot create capture pipe");
        return NULL;
    }
    GUnixFDList *fds = g_unix_fd_list_new();
    gint handle = g_unix_fd_list_append(fds, pipes[1], error);
    close(pipes[1]);
    GVariantBuilder options;
    g_variant_builder_init(&options, G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add(&options, "{sv}", "native-resolution", g_variant_new_boolean(native));
    g_variant_builder_add(&options, "{sv}", "include-cursor", g_variant_new_boolean(FALSE));
    g_variant_builder_add(&options, "{sv}", "hide-caller-windows", g_variant_new_boolean(FALSE));
    g_variant_builder_add(&options, "{sv}", "include-decoration", g_variant_new_boolean(TRUE));
    g_variant_builder_add(&options, "{sv}", "include-shadow", g_variant_new_boolean(FALSE));
    gboolean full = !window_key && x == bounds.x && y == bounds.y && w == bounds.width && h == bounds.height;
    GVariant *params = window_key ? g_variant_new("(sa{sv}h)", window_key, &options, handle) :
        full ? g_variant_new("(a{sv}h)", &options, handle) :
        g_variant_new("(iiuua{sv}h)", x, y, (guint)w, (guint)h, &options, handle);
    GVariant *reply = handle >= 0 ? host_call("org.kde.KWin.ScreenShot2",
        "/org/kde/KWin/ScreenShot2", "org.kde.KWin.ScreenShot2",
        window_key ? "CaptureWindow" : full ? "CaptureWorkspace" : "CaptureArea", params, fds, NULL, error) : NULL;
    g_object_unref(fds);
    GVariant *metadata = NULL;
    guint stride = 0, format = 0;
    if (reply && g_variant_is_of_type(reply, G_VARIANT_TYPE("(a{sv})"))) {
        g_variant_get(reply, "(@a{sv})", &metadata);
        g_variant_lookup(metadata, "width", "u", width);
        g_variant_lookup(metadata, "height", "u", height);
        g_variant_lookup(metadata, "stride", "u", &stride);
        g_variant_lookup(metadata, "format", "u", &format);
    }
    guint64 length = (guint64)stride * *height;
    GBytes *result = NULL;
    if (reply && *width > 0 && *height > 0 && length > 0 && length <= MAX_FRAME_BYTES &&
        (guint64)*width * *height * 4 <= MAX_FRAME_BYTES) {
        guchar *raw = g_try_malloc(length);
        if (raw && host_read_pixels(pipes[0], raw, length, error)) {
            result = decode_kwin_pixels(raw, length, *width, *height, stride, format, error);
        } else if (error && !*error) {
            g_set_error(error, G_IO_ERROR, G_IO_ERROR_NO_SPACE, "Capture exceeds memory limits");
        }
        g_free(raw);
    } else if (error && !*error) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA, "Invalid or oversized KWin capture metadata");
    }
    close(pipes[0]);
    g_clear_pointer(&metadata, g_variant_unref);
    g_clear_pointer(&reply, g_variant_unref);
    return result;
}

static void disconnect_input(void)
{
    if (host.input_cookie >= 0 && host.bus && !g_dbus_connection_is_closed(host.bus)) {
        GVariant *reply = g_dbus_connection_call_sync(host.bus, KWIN_NAME,
            KWIN_EIS_PATH, KWIN_EIS_INTERFACE, "disconnect",
            g_variant_new("(i)", host.input_cookie), NULL,
            G_DBUS_CALL_FLAGS_NO_AUTO_START, 500, NULL, NULL);
        g_clear_pointer(&reply, g_variant_unref);
    }
    host.input_cookie = -1;
}

static int connect_input(GError **error)
{
    disconnect_input();
    GUnixFDList *fds = NULL;
    GVariant *reply = host_call(KWIN_NAME, KWIN_EIS_PATH, KWIN_EIS_INTERFACE,
        "connectToEIS", g_variant_new("(i)", 3), NULL, &fds, error);
    int fd = -1;
    if (reply && g_variant_is_of_type(reply, G_VARIANT_TYPE("(hi)"))) {
        gint handle;
        g_variant_get(reply, "(hi)", &handle, &host.input_cookie);
        if (fds) {
            fd = g_unix_fd_list_get(fds, handle, error);
        }
    }
    g_clear_pointer(&reply, g_variant_unref);
    g_clear_object(&fds);
    if (fd < 0) {
        disconnect_input();
    }
    return fd;
}

static gboolean initialize_host(void)
{
    GError *error = NULL;
    host.bus = session_bus = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &error);
    host.name = g_strdup_printf("org.anatase.Spaces.Computer.p%lu.r%08x", (unsigned long)getpid(), g_random_int());
    if (!host.bus) {
        goto failed;
    }
    GVariant *reply = host_call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "RequestName", g_variant_new("(su)", host.name, 4), NULL, NULL, &error);
    if (!reply) {
        goto failed;
    }
    g_variant_unref(reply);
    GDBusNodeInfo *publisher = g_dbus_node_info_new_for_xml(geometry_xml, NULL);
    static const GDBusInterfaceVTable publish_table = {.method_call = host_publish};
    g_dbus_connection_register_object(host.bus, GEOMETRY_PATH, publisher->interfaces[0],
        &publish_table, NULL, NULL, &error);
    g_dbus_node_info_unref(publisher);
    if (error) {
        goto failed;
    }
    /* Fresh namespaces prevent old handles from naming a new worker's state. */
    next_capture = (guint64)g_random_int() << 20;
    next_kwin_window = g_random_int_range(1, G_MAXINT);
    atspi_worker_base = (guint)g_random_int_range(1, 1024) << 20;
    initialized = TRUE; /* The guest owns public MCP negotiation. */
    return TRUE;
failed:
    g_printerr("scu: cannot start host: %s\n", error ? error->message : "initialization failed");
    g_clear_error(&error);
    return FALSE;
}

/* The accessibility helper retains its existing parent-death behavior. */
static void die_with_parent(void)
{
    pid_t parent = getppid();
    if (prctl(PR_SET_PDEATHSIG, SIGTERM) < 0 || parent == 1 || getppid() != parent) {
        _exit(1);
    }
}

#include "scu_desktop.h"
#include "scu_service.h"

int main(int argc, char **argv)
{
    if (argc == 3 && !strcmp(argv[1], "--listen"))
        return listen((int)g_ascii_strtoll(argv[2], NULL, 10), 32) < 0;
    if (argc == 4 && !strcmp(argv[1], "--host-service"))
        return run_shared_host(argv[2], (int)g_ascii_strtoll(argv[3], NULL, 10));
    if (argc == 2 && !strcmp(argv[1], "--user-service"))
        return run_shared_host(NULL, -1);
    if (argc == 3 && !strcmp(argv[1], "--atspi-worker")) {
        die_with_parent();
        return run_atspi_worker((guint)g_ascii_strtoull(argv[2], NULL, 10));
    }
    print_help(stderr);
    return 2;
}
