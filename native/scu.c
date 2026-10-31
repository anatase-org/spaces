/* SPDX-License-Identifier: AGPL-3.0-or-later */
/*
 * scu: Spaces computer use over MCP stdio.
 *
 * This file owns the MCP transport, ordered action execution and desktop
 * adapters. It uses the existing guest-visible portal and accessibility buses;
 * it does not publish host permissions or change Spaces configuration.
 *
 * JSON input documents belong to the reader/request queue. Each response gets
 * one mutable document, which owns all result objects until it is written.
 * Accessible objects outlive requests only through explicit GObject references.
 */
#define _GNU_SOURCE

#include "vendor/yyjson.h"

#include <atspi/atspi.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <gdk-pixbuf/gdk-pixbuf.h>
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <libei.h>
#include <linux/input-event-codes.h>
#include <math.h>
#include <pipewire/pipewire.h>
#include <signal.h>
#include <spa/param/video/format-utils.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>
#include <xkbcommon/xkbcommon-keysyms.h>
#include <xkbcommon/xkbcommon.h>

/* Shared response state and JSON construction helpers. */
typedef yyjson_val JsonValue;
typedef yyjson_mut_val JsonResult;
typedef struct {
    yyjson_mut_doc *doc;
    JsonResult *images;
    char *code, *hint, *generation;
    gint cancelled, stopping;
    gint64 deadline;
} Scu;
static Scu scu;
static inline JsonValue *json_member(JsonValue *o, const char *k)
{
    return yyjson_obj_get(o, k);
}

static inline const char *json_string(JsonValue *o, const char *k)
{
    return yyjson_get_str(json_member(o, k));
}

static inline double json_number(JsonValue *o, const char *k, double fallback)
{
    JsonValue *v = json_member(o, k);
    return yyjson_is_num(v) ? yyjson_get_num(v) : fallback;
}

static inline gboolean json_boolean(JsonValue *o, const char *k)
{
    return yyjson_is_true(json_member(o, k));
}

static inline JsonResult *result_object(void)
{
    return yyjson_mut_obj(scu.doc);
}

static inline JsonResult *result_array(void)
{
    return yyjson_mut_arr(scu.doc);
}

static inline void result_member(JsonResult *o, const char *k, JsonResult *v)
{
    yyjson_mut_obj_put(o, yyjson_mut_strcpy(scu.doc, k), v);
}

static inline void result_string(JsonResult *o, const char *k, const char *s)
{
    result_member(o, k, s ? yyjson_mut_strcpy(scu.doc, s) : yyjson_mut_null(scu.doc));
}

static inline void result_integer(JsonResult *o, const char *k, int64_t n)
{
    result_member(o, k, yyjson_mut_sint(scu.doc, n));
}

static inline void result_number(JsonResult *o, const char *k, double n)
{
    result_member(o, k, yyjson_mut_real(scu.doc, n));
}

static inline void result_boolean(JsonResult *o, const char *k, gboolean b)
{
    result_member(o, k, yyjson_mut_bool(scu.doc, b));
}

static inline void result_append(JsonResult *a, JsonResult *v)
{
    yyjson_mut_arr_add_val(a, v);
}

static JsonResult *scu_fail(const char *code, const char *hint);
static JsonResult *scu_action(
    const char *route, const char *mode, gboolean confirmed, const char *summary);
static gboolean scu_gate(void);
static gboolean scu_tick(void);
static gboolean scu_pause(unsigned ms);
static void *scu_library(const char *soname);
static JsonResult *scu_health(void);
static gboolean probe_accessibility(void);
static void stop_atspi_worker(void);
static int run_atspi_worker(guint base);
static JsonResult *atspi_worker_call(const char *tool, JsonValue *args);
static gboolean inside_atspi_worker, retire_atspi_worker;
static void scu_invalidate(void);
static JsonResult *scu_atspi(const char *tool, JsonValue *args);
static void scu_atspi_close(void);
static JsonResult *scu_visual(const char *tool, JsonValue *args);
static void scu_visual_close(void);
static const char *scu_visual_state(void);
static void poll_visual_events(void);

/* Guest ABI compatibility and request lifecycle. */
#if defined(__x86_64__)
#define OLD_LIBC "GLIBC_2.2.5"
#elif defined(__aarch64__)
#define OLD_LIBC "GLIBC_2.17"
#else
#error Unsupported guest architecture
#endif
extern int old_start(int (*)(int, char **, char **), int, char **, void (*)(void),
    void (*)(void), void (*)(void), void *);
__asm__(".symver old_start,__libc_start_main@" OLD_LIBC);
int __wrap___libc_start_main(int (*f)(int, char **, char **), int n, char **v,
    void (*i)(void), void (*e)(void), void (*r)(void), void *s)
{
    return old_start(f, n, v, i, e, r, s);
}

/* glibc 2.34 merged libdl; retain the older public ABI for injected binaries. */
extern void *old_dlopen(const char *, int);
extern void *old_dlsym(void *, const char *);
__asm__(".symver old_dlopen,dlopen@" OLD_LIBC);
__asm__(".symver old_dlsym,dlsym@" OLD_LIBC);
void *__wrap_dlopen(const char *s, int f)
{
    return old_dlopen(s, f);
}
void *__wrap_dlsym(void *h, const char *s)
{
    return old_dlsym(h, s);
}

/* Recent glibc headers bind stat wrappers to GLIBC_2.33. The kernel's
 * native 64-bit stat ABI is stable on both supported guest architectures. */
static int scu_lstat(const char *path, struct stat *metadata)
{
#ifdef SYS_newfstatat
    return syscall(SYS_newfstatat, AT_FDCWD, path, metadata, AT_SYMLINK_NOFOLLOW);
#else
    return syscall(SYS_fstatat, AT_FDCWD, path, metadata, AT_SYMLINK_NOFOLLOW);
#endif
}

static int scu_fstat(int fd, struct stat *metadata)
{
    return syscall(SYS_fstat, fd, metadata);
}

static GAsyncQueue *requests;
static GMutex active_lock;
static char *active_id;
static gboolean modern, initialized;
static GHashTable *cancelled_ids;
static const char *libs[] = {"libatspi.so.0", "libpipewire-0.3.so.0", "libei.so.1",
    "libxkbcommon.so.0", "libgdk_pixbuf-2.0.so.0", NULL};

static JsonResult *scu_fail(const char *code, const char *hint)
{
    if (!scu.code) {
        scu.code = g_strdup(code);
        scu.hint = g_strdup(hint);
    }
    return NULL;
}

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

/* The forwarded socket is the existing grant signal. Its absence cannot tell
 * disabled accessibility from forwarding that is still starting, so the hint
 * covers both. No host-side status file or new permission API is involved. */
static gboolean scu_gate(void)
{
    char path[256];
    struct stat st;
#ifndef SCU_ATSPI_SOCKET
    g_snprintf(path, sizeof(path), "/run/spaces/desktop/%lu/a11y/bus",
        (unsigned long)getuid());
#else
    g_snprintf(path, sizeof(path), "%s", SCU_ATSPI_SOCKET);
#endif
    if (scu_lstat(path, &st) || !S_ISSOCK(st.st_mode) || st.st_uid != getuid()) {
        scu_fail("accessibility_permission_required",
            "Host accessibility is not forwarded. On the host, run `spaces configure "
            "<space>` and enable Plus Accessibility and Agent computer use. If already "
            "enabled, check the host graphical login and Spaces service log.");
        scu_invalidate();
        return FALSE;
    }
    char *generation =
        g_strdup_printf("%lu:%lu", (unsigned long)st.st_dev, (unsigned long)st.st_ino);
    if (scu.generation && strcmp(scu.generation, generation)) {
        scu_invalidate();
    }
    g_free(scu.generation);
    scu.generation = generation;
    return TRUE;
}

/* Long operations call this at safe boundaries. Cancellation flags are shared
 * with the stdin thread; desktop objects are touched only by this thread. */
static gboolean scu_tick(void)
{
    poll_visual_events();
    while (g_main_context_iteration(NULL, FALSE)) {
    }
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
    return scu_gate();
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
    const char *route, const char *mode, gboolean confirmed, const char *summary)
{
    JsonResult *r = result_object(), *delivery = result_object();
    result_string(r, "effect", confirmed ? "confirmed" : "unverifiable");
    result_string(r, "route", route);
    result_string(delivery, "mode", mode);
    result_member(r, "delivery", delivery);
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

static JsonResult *scu_health(void)
{
    JsonResult *r = result_object(), *deps = result_object();
    gboolean forwarded = scu_gate();
    gboolean ready = forwarded && probe_accessibility();
    result_boolean(r, "forwarded", forwarded);
    result_boolean(r, "ready", ready);
    if (!ready) {
        result_string(r, "code", scu.code);
        result_string(r, "hint", scu.hint);
    }
    result_string(r, "visual_session", scu_visual_state());
    for (unsigned i = 0; libs[i]; i++) {
        void *h = scu_library(libs[i]);
        result_boolean(deps, libs[i], h != NULL); /* Keep optional libraries loaded:
                                              GTypes retain class pointers. */
    }
    result_member(r, "libraries", deps);
    g_clear_pointer(&scu.code, g_free);
    g_clear_pointer(&scu.hint, g_free);
    return r;
}

/* Host accessibility: discovery, snapshots and semantic actions. */
typedef struct {
    __typeof__(&atspi_init) init;
    __typeof__(&atspi_exit) exit;
    __typeof__(&atspi_set_timeout) set_timeout;
    __typeof__(&atspi_get_desktop) get_desktop;
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

/* Load through a function table so MCP discovery and diagnostics can start
 * even on guests without libatspi. Never unload it: GLib retains its GTypes. */
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
    if (elements) {
        g_hash_table_remove_all(elements);
    }
    if (windows) {
        g_ptr_array_set_size(windows, 0);
    }
    if (atspi_initialized) {
        atspi.exit();
        atspi_initialized = FALSE;
    }
}

static gboolean ensure_atspi(void)
{
    if (!load_atspi()) {
        scu_fail("missing_dependency",
            "Install the guest AT-SPI library: at-spi2-core (Fedora/Arch), "
            "libatspi2.0-0 (Ubuntu).");
        return FALSE;
    }
    if (atspi_initialized) {
        return TRUE;
    }
    char *path =
        g_strdup_printf("/run/spaces/desktop/%lu/a11y/bus", (unsigned long)getuid());
#ifndef SCU_ATSPI_ADDRESS
    struct stat st;
    if (scu_lstat(path, &st) || !S_ISSOCK(st.st_mode)) {
        g_free(path);
        scu_fail(
            "accessibility_unavailable", "The forwarded AT-SPI socket is unavailable.");
        return FALSE;
    }
    char *address = g_strconcat("unix:path=", path, NULL);
#else
    char *address = g_strdup(SCU_ATSPI_ADDRESS);
#endif
    g_free(path);
    g_setenv("AT_SPI_BUS_ADDRESS", address, TRUE);
    g_free(address);
    atspi.set_timeout(700, 1500);
    if (atspi.init() != 0) {
        scu_fail("accessibility_unavailable",
            "Could not initialize the forwarded host AT-SPI bus.");
        return FALSE;
    }
    atspi_initialized = TRUE;
    if (!windows) {
        windows = g_ptr_array_new_with_free_func(free_window);
    }
    if (!elements) {
        elements = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, free_element);
    }
    return TRUE;
}

/* Diagnostics do not open a portal or change host accessibility settings. A
 * socket path alone is not sufficient evidence that the registry is usable. */
static gboolean probe_accessibility(void)
{
    if (!inside_atspi_worker) {
        return scu_atspi("_probe", NULL) != NULL;
    }
    if (!ensure_atspi()) {
        return FALSE;
    }
    AtspiAccessible *desktop = atspi.get_desktop(0);
    GError *error = NULL;
    gint count =
        desktop != NULL ? atspi.accessible_get_child_count(desktop, &error) : -1;
    if (desktop != NULL) {
        g_object_unref(desktop);
    }
    if (error != NULL || count < 0) {
        scu_fail("accessibility_unavailable",
            error != NULL ? error->message
                          : "Host accessibility registry is unavailable.");
        g_clear_error(&error);
        scu_atspi_close();
        return FALSE;
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
    AtspiRect *r = atspi.component_get_extents(c, ATSPI_COORD_TYPE_SCREEN, NULL);
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

static JsonResult *list_windows(JsonValue *arguments)
{
    if (!discover_windows()) {
        return NULL;
    }
    JsonResult *result = result_object();
    JsonResult *window_list = result_array();
    for (guint index = 0; index < windows->len; index++) {
        Window *window = g_ptr_array_index(windows, index);
        if (json_member(arguments, "pid") != NULL &&
            json_number(arguments, "pid", 0) != window->pid) {
            continue;
        }
        JsonResult *item = result_object();
        char *title = atspi.accessible_get_name(window->accessible, NULL);
        result_integer(item, "pid", window->pid);
        result_integer(item, "window_id", window->id);
        result_string(item, "app_name", window->app);
        result_string(item, "title", title);
        result_member(item, "bounds", accessible_frame(window->accessible));
        result_boolean(item, "is_on_screen",
            accessible_has_state(window->accessible, ATSPI_STATE_SHOWING));
        result_boolean(item, "active",
            accessible_has_state(window->accessible, ATSPI_STATE_ACTIVE));
        result_member(item, "z_index", yyjson_mut_null(scu.doc));
        result_append(window_list, item);
        g_free(title);
    }
    result_member(result, "windows", window_list);
    result_boolean(result, "incomplete", TRUE);
    result_string(result, "coverage",
        "Only applications exposed through AT-SPI are included; "
        "compositor stacking order is unavailable.");
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
            "Window is absent or belongs to a different process. Call list_windows "
            "again.");
    }
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
    return scu_action("accessibility", "background", FALSE,
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
    return scu_action("accessibility", "background", confirmed,
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
    return scu_action("accessibility", "background", confirmed,
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
        return scu_fail("stale_element", "Element token is absent or stale. Read "
                                         "get_window_state and use its new token.");
    }

    JsonValue *target = json_member(arguments, "target");
    gboolean wrong_process = json_member(arguments, "pid") != NULL &&
                             json_number(arguments, "pid", -1) != element->pid;
    gboolean wrong_window = json_member(arguments, "window_id") != NULL &&
                            json_number(arguments, "window_id", -1) != element->window;
    gboolean wrong_target =
        target != NULL && (g_strcmp0(json_string(target, "kind"), "window") != 0 ||
                              json_number(target, "pid", -1) != element->pid ||
                              json_number(target, "window_id", -1) != element->window);
    if (wrong_process || wrong_window || wrong_target) {
        return scu_fail("target_mismatch",
            "The element token does not belong to the requested window.");
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

static JsonResult *scu_atspi(const char *tool, JsonValue *args)
{
    if (!inside_atspi_worker) {
        return atspi_worker_call(tool, args);
    }
    if (!strcmp(tool, "_probe")) {
        return probe_accessibility() ? result_object() : NULL;
    }
    if (!ensure_atspi()) {
        return NULL;
    }
    if (!strcmp(tool, "list_windows") || !strcmp(tool, "get_accessibility_tree")) {
        return list_windows(args);
    }
    if (!strcmp(tool, "get_window_state")) {
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
 * no additional authority and inherits no portal/input descriptors. Its private
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
        if (!scu_tick()) {
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

static int run_atspi_worker(guint base)
{
    inside_atspi_worker = TRUE;
    next_window = snapshot = base;
    signal(SIGPIPE, SIG_IGN);
    char *line = NULL;
    size_t capacity = 0;
    while (getline(&line, &capacity, stdin) > 0) {
        yyjson_doc *document = yyjson_read(line, strlen(line), 0);
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
    free(line);
    scu_atspi_close();
    return 0;
}

/* Tool descriptions, validation and ordered batching. */
/* Visual sessions: one portal grant supplies PipeWire streams and EIS input.
 * All callbacks run on the main thread; poll_visual_events never blocks. */
#define PORTAL_NAME "org.freedesktop.portal.Desktop"
#define PORTAL_PATH "/org/freedesktop/portal/desktop"
#define PORTAL_REMOTE "org.freedesktop.portal.RemoteDesktop"
#define PORTAL_CAST "org.freedesktop.portal.ScreenCast"
#define PORTAL_CLIPBOARD "org.freedesktop.portal.Clipboard"
#define MAX_DISPLAYS 8
#define MAX_FRAME_BYTES (256u * 1024u * 1024u)

typedef struct {
    __typeof__(&pw_init) init;
    __typeof__(&pw_main_loop_new) main_loop_new;
    __typeof__(&pw_main_loop_get_loop) main_loop_get_loop;
    __typeof__(&pw_main_loop_destroy) main_loop_destroy;
    __typeof__(&pw_context_new) context_new;
    __typeof__(&pw_context_destroy) context_destroy;
    __typeof__(&pw_context_connect_fd) context_connect_fd;
    __typeof__(&pw_core_disconnect) core_disconnect;
    __typeof__(&pw_stream_new) stream_new;
    __typeof__(&pw_stream_destroy) stream_destroy;
    __typeof__(&pw_stream_add_listener) stream_add_listener;
    __typeof__(&pw_stream_connect) stream_connect;
    __typeof__(&pw_stream_dequeue_buffer) stream_dequeue_buffer;
    __typeof__(&pw_stream_queue_buffer) stream_queue_buffer;
    __typeof__(&pw_properties_new) properties_new;
} PipeWireLibrary;

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
    /* Round trips appeared in 1.4. Older guests retain capture support. */
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
    __typeof__(&gdk_pixbuf_scale_simple) scale_simple;
    __typeof__(&gdk_pixbuf_save_to_buffer) save_to_buffer;
} PixbufLibrary;

static PipeWireLibrary pipewire;
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
    guint node_id;
    char *mapping_id;
    char *serial;
    char id[32];
    struct pw_stream *stream;
    struct spa_hook listener;
    struct spa_video_info_raw format;
    GBytes *pixels;
    guint width, height;
    gint logical_x, logical_y, logical_width, logical_height;
    gboolean has_geometry;
    guint capture_width, capture_height;
    guint64 revision, capture_revision;
    guint64 frame_sequence, capture_frame_sequence, observed_input_sequence;
    gint64 frame_received;
    char *capture_id;
    gboolean failed;
} Display;

typedef struct {
    GDBusConnection *bus;
    char *session;
    guint closed_subscription, clipboard_subscription;
    gboolean clipboard_enabled, broken, opening;
    char *clipboard_text;
    struct pw_main_loop *main_loop;
    struct pw_context *context;
    struct pw_core *core;
    struct ei *input;
    GPtrArray *devices;
    Display displays[MAX_DISPLAYS];
    guint display_count, emulation_sequence, selected_display;
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
    void *pw_library = scu_library("libpipewire-0.3.so.0");
    void *ei_library = scu_library("libei.so.1");
    void *xkb_library = scu_library("libxkbcommon.so.0");
    void *pixbuf_library = scu_library("libgdk_pixbuf-2.0.so.0");
    if (pw_library == NULL || ei_library == NULL || xkb_library == NULL ||
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
#define LOAD_PW(name) LOAD_SYMBOL(pw_library, pipewire, "pw_", name)
    LOAD_PW(init);
    LOAD_PW(main_loop_new);
    LOAD_PW(main_loop_get_loop);
    LOAD_PW(main_loop_destroy);
    LOAD_PW(context_new);
    LOAD_PW(context_destroy);
    LOAD_PW(context_connect_fd);
    LOAD_PW(core_disconnect);
    LOAD_PW(stream_new);
    LOAD_PW(stream_destroy);
    LOAD_PW(stream_add_listener);
    LOAD_PW(stream_connect);
    LOAD_PW(stream_dequeue_buffer);
    LOAD_PW(stream_queue_buffer);
    LOAD_PW(properties_new);
#undef LOAD_PW
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
    LOAD_PIXBUF(scale_simple);
    LOAD_PIXBUF(save_to_buffer);
#undef LOAD_PIXBUF
#undef LOAD_SYMBOL
    pipewire.init(NULL, NULL);
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
    if (visual.devices != NULL) {
        g_ptr_array_unref(visual.devices);
    }
    if (visual.input != NULL) {
        ei_api.unref(visual.input);
    }
    for (guint index = 0; index < visual.display_count; index++) {
        Display *display = &visual.displays[index];
        if (display->stream != NULL) {
            pipewire.stream_destroy(display->stream);
        }
        g_clear_pointer(&display->pixels, g_bytes_unref);
        g_free(display->mapping_id);
        g_free(display->serial);
        g_free(display->capture_id);
    }
    if (visual.core != NULL) {
        pipewire.core_disconnect(visual.core);
    }
    if (visual.context != NULL) {
        pipewire.context_destroy(visual.context);
    }
    if (visual.main_loop != NULL) {
        pipewire.main_loop_destroy(visual.main_loop);
    }
    if (visual.bus != NULL) {
        if (visual.closed_subscription != 0) {
            g_dbus_connection_signal_unsubscribe(
                visual.bus, visual.closed_subscription);
        }
        if (visual.clipboard_subscription != 0) {
            g_dbus_connection_signal_unsubscribe(
                visual.bus, visual.clipboard_subscription);
        }
        if (visual.session != NULL && !g_dbus_connection_is_closed(visual.bus)) {
            GVariant *reply = g_dbus_connection_call_sync(visual.bus, PORTAL_NAME,
                visual.session, "org.freedesktop.portal.Session", "Close", NULL, NULL,
                G_DBUS_CALL_FLAGS_NONE, 700, NULL, NULL);
            g_clear_pointer(&reply, g_variant_unref);
        }
        g_object_unref(visual.bus);
    }
    g_free(visual.session);
    g_free(visual.clipboard_text);
    memset(&visual, 0, sizeof(visual));
}

static const char *scu_visual_state(void)
{
    if (visual.broken) {
        return "disconnected";
    }
    return visual.session != NULL ? "active" : "inactive";
}

static void stream_format_changed(
    void *data, uint32_t id, const struct spa_pod *parameter)
{
    Display *display = data;
    if (id != SPA_PARAM_Format || parameter == NULL) {
        return;
    }
    struct spa_video_info_raw format = {0};
    if (spa_format_video_raw_parse(parameter, &format) < 0) {
        display->failed = TRUE;
        return;
    }
    if (format.size.width != display->width || format.size.height != display->height) {
        display->revision++;
        display->capture_revision = 0;
        g_clear_pointer(&display->pixels, g_bytes_unref);
    }
    display->format = format;
    display->width = format.size.width;
    display->height = format.size.height;
}

static void stream_state_changed(void *data, enum pw_stream_state old_state,
    enum pw_stream_state state, const char *error)
{
    (void)old_state;
    (void)error;
    Display *display = data;
    if (state == PW_STREAM_STATE_ERROR || state == PW_STREAM_STATE_UNCONNECTED) {
        display->failed = TRUE;
        visual.broken = TRUE;
    }
}

/* Copy mapped raw frames while the buffer is ours; never retain a PipeWire
 * buffer after queueing it back. PNG conversion happens only on observation. */
static void stream_process(void *data)
{
    Display *display = data;
    struct pw_buffer *buffer;
    while ((buffer = pipewire.stream_dequeue_buffer(display->stream)) != NULL) {
        struct spa_buffer *raw = buffer->buffer;
        guint width = display->width, height = display->height;
        if (width == 0 || height == 0 ||
            (uint64_t)width * height * 4 > MAX_FRAME_BYTES || raw->n_datas < 1 ||
            raw->datas[0].data == NULL || raw->datas[0].chunk == NULL) {
            pipewire.stream_queue_buffer(display->stream, buffer);
            continue;
        }
        struct spa_data *plane = &raw->datas[0];
        gint stride = plane->chunk->stride;
        guint offset = plane->chunk->offset;
        gboolean bgr = display->format.format == SPA_VIDEO_FORMAT_BGRx ||
                       display->format.format == SPA_VIDEO_FORMAT_BGRA;
        gboolean rgb = display->format.format == SPA_VIDEO_FORMAT_RGBx ||
                       display->format.format == SPA_VIDEO_FORMAT_RGBA;
        uint64_t needed =
            (uint64_t)(height - 1) * (stride > 0 ? stride : 0) + width * 4u;
        if ((!bgr && !rgb) || stride < (gint)(width * 4u) || offset > plane->maxsize ||
            needed > plane->maxsize - offset || needed > plane->chunk->size) {
            pipewire.stream_queue_buffer(display->stream, buffer);
            continue;
        }
        guchar *pixels = g_malloc((gsize)width * height * 3);
        const guchar *source = (const guchar *)plane->data + offset;
        for (guint y = 0; y < height; y++) {
            for (guint x = 0; x < width; x++) {
                const guchar *in = source + (gsize)y * stride + x * 4;
                guchar *out = pixels + ((gsize)y * width + x) * 3;
                out[0] = in[bgr ? 2 : 0];
                out[1] = in[1];
                out[2] = in[bgr ? 0 : 2];
            }
        }
        g_clear_pointer(&display->pixels, g_bytes_unref);
        display->pixels = g_bytes_new_take(pixels, (gsize)width * height * 3);
        display->frame_sequence++;
        display->frame_received = g_get_monotonic_time();
        pipewire.stream_queue_buffer(display->stream, buffer);
    }
}

static const struct pw_stream_events capture_events = {
    PW_VERSION_STREAM_EVENTS,
    .state_changed = stream_state_changed,
    .param_changed = stream_format_changed,
    .process = stream_process,
};

static gboolean connect_display(Display *display)
{
    guint8 storage[1024];
    struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(storage, sizeof(storage));
    const struct spa_pod *format = spa_pod_builder_add_object(&builder,
        SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat, SPA_FORMAT_mediaType,
        SPA_POD_Id(SPA_MEDIA_TYPE_video), SPA_FORMAT_mediaSubtype,
        SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw), SPA_FORMAT_VIDEO_format,
        SPA_POD_CHOICE_ENUM_Id(4, SPA_VIDEO_FORMAT_BGRx, SPA_VIDEO_FORMAT_RGBx,
            SPA_VIDEO_FORMAT_BGRA, SPA_VIDEO_FORMAT_RGBA));
    struct pw_properties *properties =
        display->serial != NULL
            ? pipewire.properties_new(PW_KEY_MEDIA_TYPE, "Video", PW_KEY_MEDIA_CATEGORY,
                  "Capture", PW_KEY_MEDIA_ROLE, "Screen", PW_KEY_TARGET_OBJECT,
                  display->serial, NULL)
            : pipewire.properties_new(PW_KEY_MEDIA_TYPE, "Video", PW_KEY_MEDIA_CATEGORY,
                  "Capture", PW_KEY_MEDIA_ROLE, "Screen", NULL);
    display->stream =
        pipewire.stream_new(visual.core, "Spaces computer use", properties);
    if (display->stream == NULL) {
        return FALSE;
    }
    pipewire.stream_add_listener(
        display->stream, &display->listener, &capture_events, display);
    return pipewire.stream_connect(display->stream, PW_DIRECTION_INPUT,
               display->serial != NULL ? PW_ID_ANY : display->node_id,
               PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS, &format,
               1) >= 0;
}

static void poll_visual_events(void)
{
    if (visual.main_loop != NULL) {
        struct pw_loop *loop = pipewire.main_loop_get_loop(visual.main_loop);
        pw_loop_enter(loop);
        pw_loop_iterate(loop, 0);
        pw_loop_leave(loop);
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
                    device->resumed = TRUE;
                    ei_api.device_start_emulating(
                        raw_device, ++visual.emulation_sequence);
                } else if (type == EI_EVENT_DEVICE_PAUSED ||
                           type == EI_EVENT_DEVICE_REMOVED) {
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

typedef struct {
    char *path;
    gboolean received;
    guint response;
    GVariant *results;
} PortalResponse;

static void portal_response(GDBusConnection *connection, const char *sender,
    const char *path, const char *interface, const char *signal, GVariant *parameters,
    gpointer data)
{
    (void)connection;
    (void)sender;
    (void)interface;
    (void)signal;
    PortalResponse *response = data;
    if (response->path != NULL && strcmp(path, response->path) == 0 &&
        !response->received) {
        g_variant_get(parameters, "(u@a{sv})", &response->response, &response->results);
        response->received = TRUE;
    }
}

static GVariant *portal_call(
    const char *interface, const char *method, GVariant *parameters, GError **error)
{
    return g_dbus_connection_call_sync(visual.bus, PORTAL_NAME, PORTAL_PATH, interface,
        method, parameters, NULL, G_DBUS_CALL_FLAGS_NONE, 5000, NULL, error);
}

/* Subscribe before calling: a portal may respond before its method reply.
 * The subscription's local connection reference survives cancellation teardown. */
static GVariant *portal_request(
    const char *interface, const char *method, GVariant *parameters, const char *token)
{
    GDBusConnection *connection = g_object_ref(visual.bus);
    const char *unique = g_dbus_connection_get_unique_name(connection);
    char *sender_path = g_strdup(unique + 1);
    for (char *cursor = sender_path; *cursor != '\0'; cursor++) {
        if (*cursor == '.') {
            *cursor = '_';
        }
    }
    PortalResponse response = {
        .path = g_strdup_printf(PORTAL_PATH "/request/%s/%s", sender_path, token)};
    g_free(sender_path);
    guint subscription = g_dbus_connection_signal_subscribe(connection, PORTAL_NAME,
        "org.freedesktop.portal.Request", "Response", NULL, NULL,
        G_DBUS_SIGNAL_FLAGS_NONE, portal_response, &response, NULL);
    GError *error = NULL;
    GVariant *reply = portal_call(interface, method, parameters, &error);
    if (reply != NULL) {
        const char *path;
        g_variant_get(reply, "(&o)", &path);
        if (strcmp(path, response.path) != 0) {
            g_free(response.path);
            response.path = g_strdup(path);
        }
        while (!response.received && scu_pause(5)) {
        }
        if (!response.received) {
            /* Complete Close before dropping the last connection reference.
             * A fire-and-forget call can disappear during cancellation cleanup. */
            GVariant *closed = g_dbus_connection_call_sync(connection, PORTAL_NAME,
                response.path, "org.freedesktop.portal.Request", "Close", NULL, NULL,
                G_DBUS_CALL_FLAGS_NONE, 700, NULL, NULL);
            g_clear_pointer(&closed, g_variant_unref);
        }
        g_variant_unref(reply);
    } else {
        scu_fail("portal_unavailable",
            error != NULL ? error->message : "Portal call failed.");
    }
    g_clear_error(&error);
    g_dbus_connection_signal_unsubscribe(connection, subscription);
    g_object_unref(connection);
    g_free(response.path);
    if (response.received && response.response != 0) {
        scu_fail(response.response == 1 ? "consent_cancelled" : "portal_failed",
            "Host desktop consent was cancelled or the portal request failed.");
        g_clear_pointer(&response.results, g_variant_unref);
    }
    return response.results;
}

static GVariantBuilder request_options(char **token)
{
    GVariantBuilder options;
    *token = g_strdup_printf("scu_%08x_%08x", g_random_int(), g_random_int());
    g_variant_builder_init(&options, G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add(
        &options, "{sv}", "handle_token", g_variant_new_string(*token));
    return options;
}

static int portal_fd(const char *interface, const char *method, GVariant *parameters)
{
    GUnixFDList *descriptors = NULL;
    GError *error = NULL;
    GVariant *reply = g_dbus_connection_call_with_unix_fd_list_sync(visual.bus,
        PORTAL_NAME, PORTAL_PATH, interface, method, parameters, G_VARIANT_TYPE("(h)"),
        G_DBUS_CALL_FLAGS_NONE, 5000, NULL, &descriptors, NULL, &error);
    int fd = -1;
    if (reply != NULL) {
        gint handle;
        g_variant_get(reply, "(h)", &handle);
        fd = g_unix_fd_list_get(descriptors, handle, &error);
        g_variant_unref(reply);
    }
    if (fd < 0) {
        scu_fail("portal_unavailable",
            error != NULL ? error->message : "Portal returned no usable descriptor.");
    }
    g_clear_error(&error);
    g_clear_object(&descriptors);
    return fd;
}

static char *restore_token_path(void)
{
    /* XDG_STATE_HOME predates GLib's accessor (2.72). Avoid requiring a newer
     * GLib just to start the injected helper on an older guest. */
    const char *configured = g_getenv("XDG_STATE_HOME");
    char *state_home =
        configured != NULL && g_path_is_absolute(configured)
            ? g_strdup(configured)
            : g_build_filename(g_get_home_dir(), ".local", "state", NULL);
    char *directory = g_build_filename(state_home, "spaces", "scu", NULL);
    g_free(state_home);
    if (g_mkdir_with_parents(directory, 0700) != 0) {
        g_free(directory);
        return NULL;
    }
    struct stat metadata;
    if (scu_lstat(directory, &metadata) != 0 || !S_ISDIR(metadata.st_mode) ||
        metadata.st_uid != getuid() || (metadata.st_mode & 0077) != 0) {
        g_free(directory);
        return NULL;
    }
    /* The guest machine ID scopes consent state even when users share their
     * home directory across Spaces. No Space name or manager lookup is needed.
     * Without a valid machine ID, leave restoration to a fresh host prompt. */
    char *machine_id = NULL;
    if (!g_file_get_contents("/etc/machine-id", &machine_id, NULL, NULL)) {
        g_free(directory);
        return NULL;
    }
    g_strstrip(machine_id);
    if (strlen(machine_id) != 32 || strspn(machine_id, "0123456789abcdef") != 32) {
        g_free(machine_id);
        g_free(directory);
        return NULL;
    }
    char *name = g_strdup_printf("%s-%lu.token", machine_id, (unsigned long)getuid());
    g_free(machine_id);
    char *path = g_build_filename(directory, name, NULL);
    g_free(directory);
    g_free(name);
    return path;
}

static char *read_restore_token(void)
{
    char *path = restore_token_path();
    if (path == NULL) {
        return NULL;
    }
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    g_free(path);
    if (fd < 0) {
        return NULL;
    }
    struct stat metadata;
    char buffer[4097];
    ssize_t length = -1;
    if (scu_fstat(fd, &metadata) == 0 && S_ISREG(metadata.st_mode) &&
        metadata.st_uid == getuid() && (metadata.st_mode & 0077) == 0 &&
        metadata.st_size > 0 && metadata.st_size <= 4096) {
        length = read(fd, buffer, 4096);
    }
    close(fd);
    if (length <= 0) {
        return NULL;
    }
    buffer[length] = '\0';
    return g_utf8_validate(buffer, length, NULL) ? g_strdup(buffer) : NULL;
}

static void save_restore_token(const char *token)
{
    char *path = restore_token_path();
    if (path == NULL) {
        return;
    }
    if (token == NULL || *token == '\0') {
        unlink(path); /* Tokens are consumed even when no replacement is granted. */
    } else {
        char *temporary = g_strconcat(path, ".XXXXXX", NULL);
        int fd = g_mkstemp_full(temporary, O_WRONLY | O_CLOEXEC, 0600);
        if (fd >= 0) {
            gsize length = strlen(token);
            ssize_t written = write(fd, token, length);
            if (written == (ssize_t)length && fsync(fd) == 0) {
                rename(temporary, path);
            }
            close(fd);
            unlink(temporary);
        }
        g_free(temporary);
    }
    g_free(path);
}

static void session_closed(GDBusConnection *connection, const char *sender,
    const char *path, const char *interface, const char *signal, GVariant *parameters,
    gpointer data)
{
    (void)connection;
    (void)sender;
    (void)path;
    (void)interface;
    (void)signal;
    (void)parameters;
    (void)data;
    visual.broken = TRUE;
}

/* Clipboard writes can exceed a pipe's capacity. Transfer on a worker using an
 * immutable copy so the main loop continues to serve cancellation and revocation. */
typedef struct {
    GDBusConnection *bus;
    char *session, *text;
    guint serial;
    int fd;
} ClipboardTransfer;

static gpointer write_clipboard(gpointer pointer)
{
    ClipboardTransfer *transfer = pointer;
    gsize offset = 0, length = strlen(transfer->text);
    gint64 deadline = g_get_monotonic_time() + 5 * G_USEC_PER_SEC;
    fcntl(transfer->fd, F_SETFL, O_NONBLOCK);
    while (offset < length && g_get_monotonic_time() < deadline &&
           !g_atomic_int_get(&scu.stopping)) {
        ssize_t written = write(transfer->fd, transfer->text + offset, length - offset);
        if (written > 0) {
            offset += written;
        } else if (errno == EAGAIN || errno == EINTR) {
            g_usleep(1000);
        } else {
            break;
        }
    }
    close(transfer->fd);
    g_dbus_connection_call(transfer->bus, PORTAL_NAME, PORTAL_PATH, PORTAL_CLIPBOARD,
        "SelectionWriteDone",
        g_variant_new("(oub)", transfer->session, transfer->serial, offset == length),
        NULL, G_DBUS_CALL_FLAGS_NONE, 2000, NULL, NULL, NULL);
    g_object_unref(transfer->bus);
    g_free(transfer->session);
    g_free(transfer->text);
    g_free(transfer);
    return NULL;
}

static void clipboard_requested(GDBusConnection *connection, const char *sender,
    const char *path, const char *interface, const char *signal, GVariant *parameters,
    gpointer data)
{
    (void)connection;
    (void)sender;
    (void)path;
    (void)interface;
    (void)signal;
    (void)data;
    const char *session, *mime;
    guint serial;
    g_variant_get(parameters, "(&o&su)", &session, &mime, &serial);
    if (g_strcmp0(session, visual.session) != 0 || visual.clipboard_text == NULL ||
        (!g_str_has_prefix(mime, "text/plain") && strcmp(mime, "UTF8_STRING") != 0)) {
        return;
    }
    GUnixFDList *descriptors = NULL;
    GError *error = NULL;
    GVariant *reply = g_dbus_connection_call_with_unix_fd_list_sync(connection,
        PORTAL_NAME, PORTAL_PATH, PORTAL_CLIPBOARD, "SelectionWrite",
        g_variant_new("(ou)", session, serial), G_VARIANT_TYPE("(h)"),
        G_DBUS_CALL_FLAGS_NONE, 2000, NULL, &descriptors, NULL, &error);
    int fd = -1;
    if (reply != NULL) {
        gint handle;
        g_variant_get(reply, "(h)", &handle);
        if (descriptors != NULL) {
            fd = g_unix_fd_list_get(descriptors, handle, &error);
        }
        g_variant_unref(reply);
    }
    g_clear_object(&descriptors);
    if (fd < 0) {
        g_printerr("scu: clipboard transfer unavailable: %s\n",
            error != NULL ? error->message : "missing descriptor");
        g_clear_error(&error);
        return;
    }
    ClipboardTransfer *transfer = g_new0(ClipboardTransfer, 1);
    transfer->bus = g_object_ref(visual.bus);
    transfer->session = g_strdup(session);
    transfer->text = g_strdup(visual.clipboard_text);
    transfer->serial = serial;
    transfer->fd = fd;
    g_thread_unref(g_thread_new("scu-clipboard", write_clipboard, transfer));
}

/* Spaces' portal relay is on the guest user bus. Resolve its existing runtime
 * socket directly: MCP launchers commonly omit or filter the graphical
 * environment. Never autolaunch another bus or inherit a host session address. */
static GDBusConnection *guest_session_bus(GError **error)
{
#ifndef SCU_SESSION_BUS_SOCKET
    char *path = g_strdup_printf("/run/user/%lu/bus", (unsigned long)getuid());
#else
    char *path = g_strdup(SCU_SESSION_BUS_SOCKET);
#endif
    char *directory = g_path_get_dirname(path);
    struct stat metadata;
    gboolean safe = scu_lstat(directory, &metadata) == 0 && S_ISDIR(metadata.st_mode) &&
                    metadata.st_uid == getuid() && (metadata.st_mode & 0077) == 0;
    if (!safe || scu_lstat(path, &metadata) != 0 || !S_ISSOCK(metadata.st_mode) ||
        metadata.st_uid != getuid()) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_NOT_CONNECTED,
            "The guest user bus is unavailable or unsafe at %s. Check the guest user "
            "manager and Spaces desktop integration.",
            path);
        g_free(directory);
        g_free(path);
        return NULL;
    }
    g_free(directory);
    char *escaped = g_dbus_address_escape_value(path);
    char *address = g_strconcat("unix:path=", escaped, NULL);
    g_free(escaped);
    g_free(path);
    GDBusConnection *connection = g_dbus_connection_new_for_address_sync(address,
        G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT |
            G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
        NULL, NULL, error);
    g_free(address);
    return connection;
}

static gboolean start_portal_session(void)
{
    GError *error = NULL;
    visual.bus = guest_session_bus(&error);
    if (visual.bus == NULL) {
        scu_fail("portal_unavailable",
            error != NULL ? error->message : "Guest session bus unavailable.");
        g_clear_error(&error);
        return FALSE;
    }
    char *token;
    GVariantBuilder options = request_options(&token);
    g_variant_builder_add(
        &options, "{sv}", "session_handle_token", g_variant_new_string(token));
    GVariant *results = portal_request(
        PORTAL_REMOTE, "CreateSession", g_variant_new("(a{sv})", &options), token);
    g_free(token);
    if (results == NULL) {
        return FALSE;
    }
    const char *session;
    if (!g_variant_lookup(results, "session_handle", "&s", &session) ||
        !g_variant_is_object_path(session)) {
        g_variant_unref(results);
        scu_fail("portal_failed", "Portal returned an invalid session handle.");
        return FALSE;
    }
    visual.session = g_strdup(session);
    g_variant_unref(results);
    visual.closed_subscription = g_dbus_connection_signal_subscribe(visual.bus,
        PORTAL_NAME, "org.freedesktop.portal.Session", "Closed", visual.session, NULL,
        G_DBUS_SIGNAL_FLAGS_NONE, session_closed, NULL, NULL);

    options = request_options(&token);
    g_variant_builder_add(&options, "{sv}", "types", g_variant_new_uint32(3));
    g_variant_builder_add(&options, "{sv}", "persist_mode", g_variant_new_uint32(2));
    char *restore = read_restore_token();
    if (restore != NULL) {
        g_variant_builder_add(
            &options, "{sv}", "restore_token", g_variant_new_string(restore));
        g_free(restore);
    }
    save_restore_token(NULL);
    results = portal_request(PORTAL_REMOTE, "SelectDevices",
        g_variant_new("(oa{sv})", visual.session, &options), token);
    g_free(token);
    if (results == NULL) {
        return FALSE;
    }
    g_variant_unref(results);

    options = request_options(&token);
    g_variant_builder_add(&options, "{sv}", "types", g_variant_new_uint32(1));
    g_variant_builder_add(&options, "{sv}", "multiple", g_variant_new_boolean(TRUE));
    g_variant_builder_add(&options, "{sv}", "cursor_mode", g_variant_new_uint32(1));
    results = portal_request(PORTAL_CAST, "SelectSources",
        g_variant_new("(oa{sv})", visual.session, &options), token);
    g_free(token);
    if (results == NULL) {
        return FALSE;
    }
    g_variant_unref(results);

    /* Clipboard is optional; an unsupported backend does not prevent capture. */
    GVariant *reply = portal_call(PORTAL_CLIPBOARD, "RequestClipboard",
        g_variant_new("(oa{sv})", visual.session, NULL), &error);
    if (reply != NULL) {
        g_variant_unref(reply);
    }
    g_clear_error(&error);
    options = request_options(&token);
    results = portal_request(PORTAL_REMOTE, "Start",
        g_variant_new("(osa{sv})", visual.session, "", &options), token);
    g_free(token);
    if (results == NULL) {
        return FALSE;
    }
    const char *new_restore;
    save_restore_token(g_variant_lookup(results, "restore_token", "&s", &new_restore)
                           ? new_restore
                           : NULL);
    g_variant_lookup(results, "clipboard_enabled", "b", &visual.clipboard_enabled);
    GVariant *streams =
        g_variant_lookup_value(results, "streams", G_VARIANT_TYPE("a(ua{sv})"));
    if (streams == NULL || g_variant_n_children(streams) == 0 ||
        g_variant_n_children(streams) > MAX_DISPLAYS) {
        g_clear_pointer(&streams, g_variant_unref);
        g_variant_unref(results);
        scu_fail("capture_unavailable",
            "Grant between one and eight monitors for computer use.");
        return FALSE;
    }
    visual.display_count = g_variant_n_children(streams);
    for (guint index = 0; index < visual.display_count; index++) {
        Display *display = &visual.displays[index];
        GVariant *properties;
        g_variant_get_child(
            streams, index, "(u@a{sv})", &display->node_id, &properties);
        display->has_geometry = g_variant_lookup(properties, "position", "(ii)",
                                    &display->logical_x, &display->logical_y) &&
                                g_variant_lookup(properties, "size", "(ii)",
                                    &display->logical_width, &display->logical_height);
        const char *mapping;
        if (g_variant_lookup(properties, "mapping_id", "&s", &mapping)) {
            display->mapping_id = g_strdup(mapping);
        }
        guint64 serial;
        if (g_variant_lookup(properties, "pipewire-serial", "t", &serial)) {
            display->serial = g_strdup_printf("%" G_GUINT64_FORMAT, serial);
        }
        g_snprintf(display->id, sizeof(display->id), "display-%u", index + 1);
        g_variant_unref(properties);
    }
    g_variant_unref(streams);
    g_variant_unref(results);
    visual.clipboard_subscription = g_dbus_connection_signal_subscribe(visual.bus,
        PORTAL_NAME, PORTAL_CLIPBOARD, "SelectionTransfer", NULL, NULL,
        G_DBUS_SIGNAL_FLAGS_NONE, clipboard_requested, NULL, NULL);
    return TRUE;
}

static gboolean ensure_visual_session(void)
{
    if (visual.broken) {
        scu_visual_close();
        scu_fail("visual_session_closed",
            "Desktop session disconnected. Inspect state before requesting a new "
            "session; no action was replayed.");
        return FALSE;
    }
    if (visual.session != NULL) {
        return TRUE;
    }
    if (!load_visual_libraries()) {
        scu_fail("missing_dependency",
            "Install guest PipeWire, libei, libxkbcommon and "
            "GdkPixbuf libraries. See scu guest requirements.");
        return FALSE;
    }
    visual.opening = TRUE;
    if (!start_portal_session()) {
        goto failed;
    }
    int fd = portal_fd(PORTAL_CAST, "OpenPipeWireRemote",
        g_variant_new("(oa{sv})", visual.session, NULL));
    if (fd < 0) {
        goto failed;
    }
    visual.main_loop = pipewire.main_loop_new(NULL);
    if (visual.main_loop == NULL) {
        close(fd);
        goto failed;
    }
    visual.context =
        pipewire.context_new(pipewire.main_loop_get_loop(visual.main_loop), NULL, 0);
    if (visual.context == NULL) {
        close(fd);
        goto failed;
    }
    visual.core = pipewire.context_connect_fd(visual.context, fd, NULL, 0);
    if (visual.core == NULL) {
        goto failed;
    }
    for (guint index = 0; index < visual.display_count; index++) {
        if (!connect_display(&visual.displays[index])) {
            goto failed;
        }
    }
    fd = portal_fd(
        PORTAL_REMOTE, "ConnectToEIS", g_variant_new("(oa{sv})", visual.session, NULL));
    if (fd < 0) {
        goto failed;
    }
    visual.input = ei_api.new_sender(NULL);
    if (visual.input == NULL) {
        close(fd);
        goto failed;
    }
    visual.devices = g_ptr_array_new_with_free_func(free_input_device);
    ei_api.configure_name(visual.input, "Spaces computer use");
    if (ei_api.setup_backend_fd(visual.input, fd) < 0) {
        goto failed;
    }
    visual.opening = FALSE;
    return TRUE;
failed:
    scu_visual_close();
    scu_fail("visual_session_failed",
        "Could not establish the portal capture/input session.");
    return FALSE;
}

static gboolean visual_step(void)
{
    if (!scu_tick()) {
        return FALSE;
    }
    if (visual.session == NULL || visual.broken ||
        (visual.bus != NULL && g_dbus_connection_is_closed(visual.bus))) {
        scu_fail("visual_session_closed",
            "Desktop session disconnected. No remaining action was sent.");
        return FALSE;
    }
    return TRUE;
}

static Display *selected_display(const char *id)
{
    if (id == NULL) {
        return &visual.displays[visual.selected_display];
    }
    if (strcmp(id, "primary") == 0) {
        visual.selected_display = 0;
        return &visual.displays[0];
    }
    for (guint index = 0; index < visual.display_count; index++) {
        if (strcmp(id, visual.displays[index].id) == 0) {
            visual.selected_display = index;
            return &visual.displays[index];
        }
    }
    scu_fail("unknown_display",
        "Select a granted display_id from get_desktop_state or get_screen_size.");
    return NULL;
}

static gboolean wait_for_frame(Display *display)
{
    gint64 deadline = g_get_monotonic_time() + 8 * G_USEC_PER_SEC;
    while (display->pixels == NULL && g_get_monotonic_time() < deadline) {
        if (!visual_step() || !scu_pause(5)) {
            return FALSE;
        }
        if (display->failed) {
            break;
        }
    }
    if (display->pixels == NULL || display->failed) {
        scu_fail("capture_unavailable",
            "No CPU-readable PipeWire frame arrived for the selected display.");
        return FALSE;
    }
    return TRUE;
}

/* A PipeWire buffer already in memory says nothing about an input just sent.
 * Synchronize delivery when libei supports it, consume queued frames, and give
 * the application a short render interval. This is observation, not an action
 * retry or an assertion that the application has finished its work. */
static const char *observation_frame(Display *display)
{
    if (!wait_for_frame(display)) {
        return NULL;
    }
    if (visual.input_sequence == display->observed_input_sequence) {
        return display->frame_sequence > display->capture_frame_sequence
                   ? "new" : "cached";
    }

    gint64 deadline = g_get_monotonic_time() + G_USEC_PER_SEC;
    gboolean synchronized = FALSE;
    struct ei_ping *ping = NULL;
    if (visual.input != NULL && ei_api.new_ping != NULL && ei_api.ping != NULL &&
        ei_api.ping_unref != NULL && ei_api.event_pong_get_ping != NULL) {
        ping = ei_api.new_ping(visual.input);
    }
    if (ping != NULL) {
        visual.pending_ping = ping;
        visual.ping_done = FALSE;
        ei_api.ping(ping);
        gint64 sync_deadline = g_get_monotonic_time() + 250000;
        while (!visual.ping_done && g_get_monotonic_time() < sync_deadline) {
            if (!visual_step() || !scu_pause(2)) {
                visual.pending_ping = NULL;
                ei_api.ping_unref(ping);
                return NULL;
            }
        }
        synchronized = visual.ping_done;
        visual.pending_ping = NULL;
        ei_api.ping_unref(ping);
    }
    if (!visual_step()) {
        return NULL;
    }
    guint64 baseline = display->frame_sequence;
    gint64 render_after = visual.last_input_time + 100000;
    while (g_get_monotonic_time() < deadline) {
        if (!visual_step()) {
            return NULL;
        }
        if (g_get_monotonic_time() >= render_after &&
            display->frame_sequence > baseline &&
            display->frame_received >= visual.last_input_time) {
            break;
        }
        if (!scu_pause(5)) {
            return NULL;
        }
    }
    if (display->pixels == NULL || display->failed) {
        scu_fail("capture_unavailable", "The display has no readable frame after input.");
        return NULL;
    }
    display->observed_input_sequence = visual.input_sequence;
    if (!synchronized) {
        return "unknown";
    }
    return display->frame_sequence > baseline ? "new" : "cached";
}

static JsonResult *display_metadata(Display *selected)
{
    JsonResult *result = result_object(), *displays = result_array();
    result_string(result, "platform", "linux");
    result_string(result, "display", selected->id);
    result_integer(result, "screen_width", selected->width);
    result_integer(result, "screen_height", selected->height);
    result_number(result, "scale_factor", 1.0);
    for (guint index = 0; index < visual.display_count; index++) {
        Display *display = &visual.displays[index];
        JsonResult *item = result_object();
        result_string(item, "display_id", display->id);
        result_integer(item, "width", display->width);
        result_integer(item, "height", display->height);
        result_boolean(item, "default", index == 0);
        if (display->has_geometry) {
            JsonResult *logical = result_object();
            result_integer(logical, "x", display->logical_x);
            result_integer(logical, "y", display->logical_y);
            result_integer(logical, "width", display->logical_width);
            result_integer(logical, "height", display->logical_height);
            result_member(item, "logical_bounds", logical);
        }
        result_append(displays, item);
    }
    result_member(result, "displays", displays);
    return result;
}

static JsonResult *capture_desktop(Display *display, JsonValue *arguments)
{
    const char *frame_status = observation_frame(display);
    if (frame_status == NULL) {
        return NULL;
    }
    GdkPixbuf *image = pixbuf.new_from_bytes(display->pixels, GDK_COLORSPACE_RGB, FALSE,
        8, display->width, display->height, display->width * 3);
    if (image == NULL) {
        return scu_fail("capture_failed", "Could not create an RGB image.");
    }
    guint width = display->width, height = display->height;
    guint maximum = (guint)json_number(arguments, "max_image_dimension", 0);
    if (maximum != 0 && MAX(width, height) > maximum) {
        double ratio = (double)maximum / MAX(width, height);
        width = MAX(1, (guint)(width * ratio));
        height = MAX(1, (guint)(height * ratio));
        GdkPixbuf *scaled =
            pixbuf.scale_simple(image, width, height, GDK_INTERP_BILINEAR);
        g_object_unref(image);
        image = scaled;
    }
    GError *error = NULL;
    char *png = NULL;
    gsize length = 0;
    gboolean saved = image != NULL &&
                     pixbuf.save_to_buffer(image, &png, &length, "png", &error, NULL);
    if (image != NULL) {
        g_object_unref(image);
    }
    if (!saved || length > 32u * 1024u * 1024u) {
        scu_fail("capture_failed",
            error != NULL
                ? error->message
                : "PNG exceeds the 32 MiB image limit; reduce max_image_dimension.");
        g_clear_error(&error);
        g_free(png);
        return NULL;
    }
    char *base64 = g_base64_encode((guchar *)png, length);
    g_free(png);
    JsonResult *block = result_object();
    result_string(block, "type", "image");
    result_string(block, "mimeType", "image/png");
    result_string(block, "data", base64);
    result_append(scu.images, block);
    g_free(base64);
    g_free(display->capture_id);
    display->capture_id = g_strdup_printf("c%" G_GUINT64_FORMAT, ++next_capture);
    display->capture_width = width;
    display->capture_height = height;
    display->capture_revision = display->revision;
    display->capture_frame_sequence = display->frame_sequence;
    JsonResult *result = display_metadata(display);
    result_string(result, "capture_id", display->capture_id);
    result_string(result, "screenshot_mime_type", "image/png");
    result_integer(result, "screenshot_width", width);
    result_integer(result, "screenshot_height", height);
    result_string(result, "frame_status", frame_status);
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

/* Pixel targets are display-local. Mapping IDs pair PipeWire images with EIS
 * regions without assuming that native pixels equal compositor logical units. */
static gboolean pointer_target(Display *display, JsonValue *arguments, double x,
    double y, struct ei_device **device_out, double *screen_x, double *screen_y)
{
    if (display->capture_id == NULL || display->capture_revision != display->revision ||
        (json_string(arguments, "capture_id") != NULL &&
            strcmp(json_string(arguments, "capture_id"), display->capture_id) != 0)) {
        scu_fail("stale_capture", "Take a new get_desktop_state screenshot for this "
                                  "display before sending pixel input.");
        return FALSE;
    }
    if (x < 0 || y < 0 || x >= display->capture_width || y >= display->capture_height) {
        scu_fail("invalid_coordinates",
            "Coordinates lie outside the selected display's current screenshot.");
        return FALSE;
    }
    struct ei_region *chosen = NULL;
    struct ei_device *chosen_device = NULL;
    guint matches = 0, total_regions = 0;
    struct ei_region *only_region = NULL;
    struct ei_device *only_device = NULL;
    if (wait_for_device(EI_DEVICE_CAP_POINTER_ABSOLUTE) == NULL) {
        return FALSE;
    }
    for (guint index = 0; index < visual.devices->len; index++) {
        InputDevice *device = g_ptr_array_index(visual.devices, index);
        if (!device->resumed || !ei_api.device_has_capability(
                                    device->device, EI_DEVICE_CAP_POINTER_ABSOLUTE)) {
            continue;
        }
        for (guint region_index = 0;; region_index++) {
            struct ei_region *region =
                ei_api.device_get_region(device->device, region_index);
            if (region == NULL) {
                break;
            }
            total_regions++;
            only_region = region;
            only_device = device->device;
            const char *mapping = ei_api.region_get_mapping_id(region);
            if (display->mapping_id != NULL && mapping != NULL &&
                strcmp(mapping, display->mapping_id) == 0) {
                chosen = region;
                chosen_device = device->device;
                matches++;
            }
        }
    }
    if (matches == 0 && display->mapping_id == NULL && visual.display_count == 1 &&
        total_regions == 1) {
        chosen = only_region;
        chosen_device = only_device;
        matches = 1;
    }
    if (matches != 1 || ei_api.region_get_width(chosen) == 0 ||
        ei_api.region_get_height(chosen) == 0) {
        scu_fail("ambiguous_display_mapping",
            "PipeWire and EIS regions cannot be paired "
            "uniquely. No pointer input was sent.");
        return FALSE;
    }
    *device_out = chosen_device;
    *screen_x = (int32_t)ei_api.region_get_x(chosen) +
                x * ei_api.region_get_width(chosen) / display->capture_width;
    *screen_y = (int32_t)ei_api.region_get_y(chosen) +
                y * ei_api.region_get_height(chosen) / display->capture_height;
    return TRUE;
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

static gboolean click_pointer(struct ei_device *pointer, guint button, guint count)
{
    InputDevice *device = wait_for_device(EI_DEVICE_CAP_BUTTON);
    if (device == NULL) {
        return FALSE;
    }
    (void)pointer;
    struct ei_device *raw = device->device;
    for (guint index = 0; index < count; index++) {
        if (!visual_step() || !device_resumed(raw)) {
            return FALSE;
        }
        ei_api.device_button_button(raw, button, true);
        input_frame(raw);
        visual.held_device = raw;
        visual.held_button = button;
        if (!scu_pause(20)) {
            release_input();
            return FALSE;
        }
        release_input();
        if (index + 1 < count && !scu_pause(60)) {
            return FALSE;
        }
    }
    return TRUE;
}

static JsonResult *pointer_action(
    const char *tool, Display *display, JsonValue *arguments)
{
    double x = json_number(arguments, strcmp(tool, "drag") == 0 ? "from_x" : "x", -1);
    double y = json_number(arguments, strcmp(tool, "drag") == 0 ? "from_y" : "y", -1);
    struct ei_device *pointer;
    double screen_x, screen_y;
    if (!pointer_target(display, arguments, x, y, &pointer, &screen_x, &screen_y)) {
        return NULL;
    }
    struct ei_device *end_pointer = NULL;
    double end_x = 0, end_y = 0;
    if (strcmp(tool, "drag") == 0 &&
        !pointer_target(display, arguments, json_number(arguments, "to_x", -1),
            json_number(arguments, "to_y", -1), &end_pointer, &end_x, &end_y)) {
        return NULL;
    }
    if (end_pointer != NULL && end_pointer != pointer) {
        return scu_fail(
            "unsupported_feature", "Cross-display dragging is unsupported.");
    }
    InputDevice *button = NULL, *scroll = NULL;
    if (strcmp(tool, "drag") == 0) {
        button = wait_for_device(EI_DEVICE_CAP_BUTTON);
        if (button == NULL) {
            return NULL;
        }
    }
    if (strcmp(tool, "scroll") == 0) {
        scroll = wait_for_device(EI_DEVICE_CAP_SCROLL);
        if (scroll == NULL) {
            return NULL;
        }
    }
    struct ei_device *button_device = button != NULL ? button->device : NULL;
    struct ei_device *scroll_device = scroll != NULL ? scroll->device : NULL;
    if (!device_resumed(pointer) ||
        (button_device != NULL && !device_resumed(button_device)) ||
        (scroll_device != NULL && !device_resumed(scroll_device))) {
        return NULL;
    }
    ei_api.device_pointer_motion_absolute(pointer, screen_x, screen_y);
    input_frame(pointer);
    if (strcmp(tool, "drag") == 0) {
        guint code = button_code(json_string(arguments, "button"));
        ei_api.device_button_button(button_device, code, true);
        input_frame(button_device);
        visual.held_device = button_device;
        visual.held_button = code;
        guint duration = (guint)json_number(arguments, "duration_ms", 500);
        guint steps = MAX(1, duration / 16);
        for (guint index = 1; index <= steps; index++) {
            if (!scu_pause(duration / steps) || !visual_step() ||
                !device_resumed(pointer) || !device_resumed(button_device)) {
                release_input();
                return NULL;
            }
            double fraction = (double)index / steps;
            ei_api.device_pointer_motion_absolute(pointer,
                screen_x + (end_x - screen_x) * fraction,
                screen_y + (end_y - screen_y) * fraction);
            input_frame(pointer);
        }
        release_input();
    } else if (strcmp(tool, "scroll") == 0) {
        const char *direction = json_string(arguments, "direction");
        gint amount = (gint)json_number(arguments, "amount", 1) * 120;
        gint dx = strcmp(direction, "left") == 0    ? -amount
                  : strcmp(direction, "right") == 0 ? amount
                                                    : 0;
        gint dy = strcmp(direction, "up") == 0     ? -amount
                  : strcmp(direction, "down") == 0 ? amount
                                                   : 0;
        ei_api.device_scroll_discrete(scroll_device, dx, dy);
        input_frame(scroll_device);
    } else if (strcmp(tool, "move_cursor") != 0) {
        guint count = strcmp(tool, "double_click") == 0
                          ? 2
                          : (guint)json_number(arguments, "count", 1);
        guint code = strcmp(tool, "right_click") == 0
                         ? BTN_RIGHT
                         : button_code(json_string(arguments, "button"));
        if (!click_pointer(pointer, code, count)) {
            return NULL;
        }
    }
    return scu_action("global_input", "foreground", FALSE,
        "Foreground pointer input submitted; inspect a fresh screenshot to verify its "
        "effect.");
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
    return scu_action("global_input", "foreground", FALSE,
        "Foreground key chord submitted; inspect the resulting state.");
}

static gboolean paste_text(struct ei_device *device, KeyboardMap *map, const char *text)
{
    if (!visual.clipboard_enabled) {
        scu_fail("text_unrepresentable", "Text is absent from the keyboard map and the "
                                         "host did not grant clipboard fallback.");
        return FALSE;
    }
    KeyStroke control, paste;
    if (!lookup_keysym(map, XKB_KEY_Control_L, &control) ||
        !lookup_keysym(map, XKB_KEY_v, &paste)) {
        scu_fail("unsupported_key",
            "Clipboard paste shortcut is unavailable in the keyboard map.");
        return FALSE;
    }
    g_free(visual.clipboard_text);
    visual.clipboard_text = g_strdup(text);
    const char *types[] = {
        "text/plain;charset=utf-8", "text/plain", "UTF8_STRING", NULL};
    GVariantBuilder options;
    g_variant_builder_init(&options, G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add(
        &options, "{sv}", "mime_types", g_variant_new_strv(types, -1));
    GError *error = NULL;
    GVariant *reply = portal_call(PORTAL_CLIPBOARD, "SetSelection",
        g_variant_new("(oa{sv})", visual.session, &options), &error);
    if (reply == NULL) {
        scu_fail("clipboard_unavailable",
            error != NULL ? error->message : "Host clipboard rejected the selection.");
        g_clear_error(&error);
        return FALSE;
    }
    g_variant_unref(reply);
    /* Ctrl+V addresses the regular clipboard. Shift+Insert can instead paste
     * PRIMARY on Linux, which this portal deliberately does not replace. */
    guint codes[] = {control.code, paste.code};
    if (!press_codes(device, codes, G_N_ELEMENTS(codes))) {
        return FALSE;
    }
    /* Serve the application's asynchronous SelectionTransfer before the next
     * ordered action changes its selection or focus. This is delivery time,
     * not proof that the application accepted the paste. */
    return scu_pause(100);
}

static JsonResult *type_foreground_text(JsonValue *arguments)
{
    const char *text = json_string(arguments, "text");
    if (*text == '\0') {
        return scu_action(
            "global_input", "foreground", FALSE, "Empty text: no input sent.");
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
        return scu_action("global_input", "foreground", FALSE,
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
        JsonResult *result = scu_action("global_input", "foreground", FALSE, NULL);
        result_string(result, "route", "clipboard_paste");
        result_string(result, "warning", "Clipboard text replaced.");
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
    return scu_action("global_input", "foreground", FALSE,
        "Text submitted using the compositor's keyboard map; verify the visible "
        "result.");
}

static JsonResult *scu_visual(const char *tool, JsonValue *arguments)
{
    const char *mode = json_string(arguments, "delivery_mode");
    if (mode != NULL && strcmp(mode, "background") == 0) {
        return scu_fail("background_unavailable",
            "Raw desktop input requires delivery_mode foreground. No foreground "
            "fallback was attempted.");
    }
    if (!ensure_visual_session() || !visual_step()) {
        return NULL;
    }
    const char *id = json_string(arguments, "display_id");
    if (json_member(arguments, "target") != NULL) {
        JsonValue *target = json_member(arguments, "target");
        if (g_strcmp0(json_string(target, "kind"), "desktop") != 0 ||
            json_string(target, "display_id") == NULL) {
            return scu_fail("invalid_arguments",
                "A desktop target requires kind desktop and a display_id.");
        }
        id = json_string(target, "display_id");
    }
    Display *display = selected_display(id);
    if (display == NULL) {
        return NULL;
    }
    /* A keyboard target selects the observation surface, never keyboard focus. */
    if (strcmp(tool, "press_key") == 0 || strcmp(tool, "hotkey") == 0) {
        return keyboard_action(tool, arguments);
    }
    if (strcmp(tool, "type_text") == 0) {
        return type_foreground_text(arguments);
    }
    if (strcmp(tool, "get_desktop_state") == 0) {
        return capture_desktop(display, arguments);
    }
    if (strcmp(tool, "get_screen_size") == 0) {
        if (!wait_for_frame(display)) {
            return NULL;
        }
        JsonResult *result = display_metadata(display);
        result_integer(result, "width", display->width);
        result_integer(result, "height", display->height);
        return result;
    }
    JsonResult *result = pointer_action(tool, display, arguments);
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
typedef enum {
    ARG_STRING,
    ARG_NUMBER,
    ARG_INTEGER,
    ARG_BOOLEAN,
    ARG_STRING_ARRAY,
    ARG_OBJECT,
    ARG_OBJECT_ARRAY,
    ARG_TARGET,
} ArgumentType;

typedef struct {
    const char *name;
    ArgumentType type;
    const char *const *choices;
    double minimum;
    double maximum; /* Zero means no bound in this argument definition. */
    const char *description;
} Argument;

typedef enum {
    ARGS_NONE = 0,
    ARGS_SESSION = 1 << 0,
    ARGS_ACTION = 1 << 1,
    ARGS_POINT = 1 << 2,
    ARGS_WINDOW = 1 << 3,
} ArgumentGroups;

typedef struct {
    const char *name;
    const char *description;
    ArgumentGroups groups;
    const Argument *arguments;
    const char *const *required;
    gboolean action;
} Tool;

static const char *const delivery_modes[] = {"foreground", "background", NULL};
static const char *const pointer_buttons[] = {"left", "right", "middle", NULL};
static const char *const tree_formats[] = {"markdown", "elements", "both", NULL};
static const char *const scroll_directions[] = {"up", "down", "left", "right", NULL};
static const char *const scroll_units[] = {"line", NULL};
static const char *const target_kinds[] = {"desktop", "window", NULL};

static const Argument session_arguments[] = {
    {.name = "session", .type = ARG_STRING},
    {NULL},
};
static const Argument action_arguments[] = {
    {.name = "pid", .type = ARG_INTEGER},
    {.name = "window_id", .type = ARG_INTEGER},
    {.name = "element_token", .type = ARG_STRING},
    {.name = "delivery_mode", .type = ARG_STRING, .choices = delivery_modes},
    {.name = "target", .type = ARG_TARGET},
    {NULL},
};
static const Argument point_arguments[] = {
    {.name = "x", .type = ARG_NUMBER},
    {.name = "y", .type = ARG_NUMBER},
    {.name = "capture_id", .type = ARG_STRING},
    {NULL},
};
static const Argument window_arguments[] = {
    {.name = "pid", .type = ARG_INTEGER},
    {.name = "window_id", .type = ARG_INTEGER},
    {.name = "max_elements", .type = ARG_INTEGER, .minimum = 1, .maximum = 2000},
    {.name = "max_depth", .type = ARG_INTEGER, .minimum = 1, .maximum = 32},
    {.name = "tree_format", .type = ARG_STRING, .choices = tree_formats},
    {.name = "include_screenshot", .type = ARG_BOOLEAN},
    {NULL},
};
static const Argument list_arguments[] = {
    {.name = "pid", .type = ARG_INTEGER},
    {NULL},
};
static const Argument capture_arguments[] = {
    {.name = "display_id", .type = ARG_STRING},
    {.name = "max_image_dimension",
        .type = ARG_INTEGER,
        .minimum = 0,
        .maximum = 16384},
    {NULL},
};
static const Argument display_arguments[] = {
    {.name = "display_id", .type = ARG_STRING},
    {NULL},
};
static const Argument click_arguments[] = {
    {.name = "button", .type = ARG_STRING, .choices = pointer_buttons},
    {.name = "count", .type = ARG_INTEGER, .minimum = 1, .maximum = 3},
    {NULL},
};
static const Argument drag_arguments[] = {
    {.name = "capture_id", .type = ARG_STRING},
    {.name = "from_x", .type = ARG_NUMBER},
    {.name = "from_y", .type = ARG_NUMBER},
    {.name = "to_x", .type = ARG_NUMBER},
    {.name = "to_y", .type = ARG_NUMBER},
    {.name = "duration_ms", .type = ARG_INTEGER, .minimum = 0, .maximum = 10000},
    {.name = "button", .type = ARG_STRING, .choices = pointer_buttons},
    {NULL},
};
static const Argument scroll_arguments[] = {
    {.name = "direction", .type = ARG_STRING, .choices = scroll_directions},
    {.name = "amount", .type = ARG_INTEGER, .minimum = 1, .maximum = 50},
    {.name = "by", .type = ARG_STRING, .choices = scroll_units},
    {NULL},
};
static const Argument text_arguments[] = {
    {.name = "text", .type = ARG_STRING},
    {NULL},
};
static const Argument value_arguments[] = {
    {.name = "value", .type = ARG_STRING},
    {NULL},
};
static const Argument key_arguments[] = {
    {.name = "key", .type = ARG_STRING},
    {.name = "modifiers", .type = ARG_STRING_ARRAY, .maximum = 4},
    {NULL},
};
static const Argument hotkey_arguments[] = {
    {.name = "keys", .type = ARG_STRING_ARRAY, .minimum = 2, .maximum = 5},
    {NULL},
};
static const Argument batch_arguments[] = {
    {.name = "steps", .type = ARG_OBJECT_ARRAY, .minimum = 1, .maximum = 64},
    {.name = "delay_ms", .type = ARG_INTEGER, .minimum = 0, .maximum = 2000},
    {.name = "observe", .type = ARG_OBJECT},
    {.name = "pid", .type = ARG_INTEGER},
    {.name = "window_id", .type = ARG_INTEGER},
    {.name = "delivery_mode", .type = ARG_STRING, .choices = delivery_modes},
    {.name = "target", .type = ARG_TARGET,
        .description = "Default desktop monitor for foreground steps; explicit step targets override it."},
    {.name = "capture_id", .type = ARG_STRING,
        .description = "Default pixel capture, inherited only on the same monitor."},
    {NULL},
};
static const Argument target_arguments[] = {
    {.name = "kind", .type = ARG_STRING, .choices = target_kinds},
    {.name = "display_id", .type = ARG_STRING},
    {.name = "pid", .type = ARG_INTEGER},
    {.name = "window_id", .type = ARG_INTEGER},
    {NULL},
};

static const char *const require_window[] = {"pid", "window_id", NULL};
static const char *const require_point[] = {"x", "y", NULL};
static const char *const require_drag[] = {"from_x", "from_y", "to_x", "to_y", NULL};
static const char *const require_scroll[] = {"x", "y", "direction", NULL};
static const char *const require_text[] = {"text", NULL};
static const char *const require_value[] = {"element_token", "value", NULL};
static const char *const require_key[] = {"key", NULL};
static const char *const require_keys[] = {"keys", NULL};
static const char *const require_steps[] = {"steps", NULL};

static const Tool tools[] = {
    {.name = "get_accessibility_tree",
        "Discover accessible host applications and windows; incomplete coverage is "
        "reported.",
        ARGS_SESSION},
    {.name = "list_windows",
        "List accessible host windows. Window IDs belong to this scu process.",
        ARGS_SESSION,
        list_arguments},
    {.name = "get_window_state",
        "Read a bounded window tree. include_screenshot defaults false; window "
        "screenshots are unsupported.",
        ARGS_SESSION | ARGS_WINDOW,
        .required = require_window},
    {.name = "get_desktop_state",
        "Observe the selected monitor as PNG; coordinates use this image. An explicit "
        "display_id selects it for later calls; primary means first granted monitor.",
        ARGS_SESSION,
        capture_arguments},
    {.name = "get_screen_size",
        "Get selected display dimensions and granted displays. May open initial host "
        "consent.",
        ARGS_SESSION,
        display_arguments},
    {.name = "click",
        "Invoke an accessible element or click desktop image coordinates with "
        "foreground delivery.",
        ARGS_SESSION | ARGS_ACTION | ARGS_POINT,
        click_arguments,
        .action = TRUE},
    {.name = "double_click",
        "Double click foreground desktop coordinates.",
        ARGS_SESSION | ARGS_ACTION | ARGS_POINT,
        .action = TRUE},
    {.name = "right_click",
        "Right click desktop coordinates or invoke an accessible context-menu action.",
        ARGS_SESSION | ARGS_ACTION | ARGS_POINT,
        .action = TRUE},
    {.name = "move_cursor",
        "Move the foreground pointer in desktop image coordinates.",
        ARGS_SESSION | ARGS_ACTION | ARGS_POINT,
        .required = require_point,
        .action = TRUE},
    {.name = "drag",
        "Drag within one display, using image coordinates and foreground delivery.",
        ARGS_SESSION | ARGS_ACTION,
        drag_arguments,
        require_drag,
        TRUE},
    {.name = "scroll",
        "Scroll at desktop coordinates. amount counts lines; page scrolling is "
        "unsupported.",
        ARGS_SESSION | ARGS_ACTION | ARGS_POINT,
        scroll_arguments,
        require_scroll,
        TRUE},
    {.name = "type_text",
        "Insert UTF-8 without submitting. Clipboard fallback replaces clipboard text.",
        ARGS_SESSION | ARGS_ACTION,
        text_arguments,
        require_text,
        TRUE},
    {.name = "set_value",
        "Replace an accessible text/numeric value and check it through independent "
        "readback.",
        ARGS_SESSION | ARGS_ACTION,
        value_arguments,
        require_value,
        TRUE},
    {.name = "press_key",
        "Press/release one key in the foreground application; modifiers are optional.",
        ARGS_SESSION | ARGS_ACTION,
        key_arguments,
        require_key,
        TRUE},
    {.name = "hotkey",
        "Press modifiers and one key in the foreground application.",
        ARGS_SESSION | ARGS_ACTION,
        hotkey_arguments,
        require_keys,
        TRUE},
    {.name = "run_steps",
        "Preferred for known sequences: validate 1–64 actions, execute in order, stop "
        "on failure. Foreground batches return one final selected-monitor image. "
        "observe:false skips it; a desktop or window-tree object replaces it. "
        "Inspect the image to verify effects; actions are never retried.",
        ARGS_SESSION,
        batch_arguments,
        require_steps,
        TRUE},
    {.name = "health_report",
        "Report accessibility forwarding and optional guest libraries without opening "
        "consent dialogs.",
        ARGS_NONE},
};

static const Tool *find_tool(const char *name)
{
    for (unsigned index = 0; index < G_N_ELEMENTS(tools); index++) {
        if (g_strcmp0(name, tools[index].name) == 0) {
            return &tools[index];
        }
    }
    return NULL;
}

static void add_argument_schemas(JsonResult *properties, const Argument *arguments)
{
    if (arguments == NULL) {
        return;
    }
    for (const Argument *argument = arguments; argument->name != NULL; argument++) {
        JsonResult *property = result_object();
        const char *type;
        switch (argument->type) {
        case ARG_STRING:
            type = "string";
            break;
        case ARG_NUMBER:
            type = "number";
            break;
        case ARG_INTEGER:
            type = "integer";
            break;
        case ARG_BOOLEAN:
            type = "boolean";
            break;
        case ARG_STRING_ARRAY:
        case ARG_OBJECT_ARRAY:
            type = "array";
            break;
        case ARG_OBJECT:
        case ARG_TARGET:
            type = "object";
            break;
        default:
            g_assert_not_reached();
        }
        result_string(property, "type", type);
        if (argument->description != NULL) {
            result_string(property, "description", argument->description);
        }
        if (argument->choices != NULL) {
            JsonResult *choices = result_array();
            for (const char *const *choice = argument->choices; *choice != NULL;
                choice++) {
                result_append(choices, yyjson_mut_strcpy(scu.doc, *choice));
            }
            result_member(property, "enum", choices);
        }
        if (argument->type == ARG_STRING_ARRAY || argument->type == ARG_OBJECT_ARRAY) {
            JsonResult *items = result_object();
            result_string(items, "type",
                argument->type == ARG_STRING_ARRAY ? "string" : "object");
            result_member(property, "items", items);
            if (argument->minimum != 0) {
                result_integer(property, "minItems", argument->minimum);
            }
            if (argument->maximum != 0) {
                result_integer(property, "maxItems", argument->maximum);
            }
        } else if (argument->maximum != 0) {
            result_number(property, "minimum", argument->minimum);
            result_number(property, "maximum", argument->maximum);
        }
        if (argument->type == ARG_TARGET) {
            JsonResult *target_properties = result_object();
            JsonResult *required = result_array();
            add_argument_schemas(target_properties, target_arguments);
            result_append(required, yyjson_mut_str(scu.doc, "kind"));
            result_member(property, "properties", target_properties);
            result_member(property, "required", required);
            result_boolean(property, "additionalProperties", FALSE);
        }
        result_member(properties, argument->name, property);
    }
}

static yyjson_doc *build_tool_schema(const Tool *tool)
{
    JsonResult *schema = result_object();
    JsonResult *properties = result_object();
    JsonResult *required = result_array();
    if (tool->groups & ARGS_SESSION) {
        add_argument_schemas(properties, session_arguments);
    }
    if (tool->groups & ARGS_ACTION) {
        add_argument_schemas(properties, action_arguments);
    }
    if (tool->groups & ARGS_POINT) {
        add_argument_schemas(properties, point_arguments);
    }
    if (tool->groups & ARGS_WINDOW) {
        add_argument_schemas(properties, window_arguments);
    }
    add_argument_schemas(properties, tool->arguments);
    if (strcmp(tool->name, "run_steps") == 0) {
        /* Advertise the actual nested contract, not an opaque array of objects.
         * Individual steps are validated again after defaults are merged. */
        JsonResult *alternatives = result_array();
        for (unsigned index = 0; index < G_N_ELEMENTS(tools); index++) {
            const Tool *action = &tools[index];
            if (!action->action || action == tool) {
                continue;
            }
            yyjson_doc *input = build_tool_schema(action);
            JsonResult *named = result_object(), *named_properties = result_object();
            JsonResult *name = result_object(), *names = result_array();
            result_string(named, "type", "object");
            result_string(name, "type", "string");
            result_append(names, yyjson_mut_strcpy(scu.doc, action->name));
            result_member(name, "enum", names);
            result_member(named_properties, "tool", name);
            result_member(named_properties, "args",
                yyjson_val_mut_copy(scu.doc, yyjson_doc_get_root(input)));
            result_member(named, "properties", named_properties);
            JsonResult *required_names = result_array();
            result_append(required_names, yyjson_mut_str(scu.doc, "tool"));
            result_append(required_names, yyjson_mut_str(scu.doc, "args"));
            result_member(named, "required", required_names);
            result_boolean(named, "additionalProperties", FALSE);
            result_append(alternatives, named);

            JsonResult *short_form = result_object(), *short_properties = result_object();
            result_string(short_form, "type", "object");
            result_member(short_properties, action->name,
                yyjson_val_mut_copy(scu.doc, yyjson_doc_get_root(input)));
            result_member(short_form, "properties", short_properties);
            JsonResult *short_required = result_array();
            result_append(short_required, yyjson_mut_strcpy(scu.doc, action->name));
            result_member(short_form, "required", short_required);
            result_boolean(short_form, "additionalProperties", FALSE);
            result_append(alternatives, short_form);
            yyjson_doc_free(input);
        }
        JsonResult *items = result_object();
        result_member(items, "anyOf", alternatives);
        result_member(yyjson_mut_obj_get(properties, "steps"), "items", items);

        JsonResult *observe = result_object(), *choices = result_array();
        JsonResult *off = result_object(), *off_values = result_array();
        result_string(off, "type", "boolean");
        result_append(off_values, yyjson_mut_false(scu.doc));
        result_member(off, "enum", off_values);
        result_append(choices, off);
        const char *observation_tools[] = {"get_desktop_state", "get_window_state"};
        const char *kinds[] = {"desktop", "window"};
        for (unsigned index = 0; index < 2; index++) {
            yyjson_doc *input = build_tool_schema(find_tool(observation_tools[index]));
            JsonResult *option = yyjson_val_mut_copy(scu.doc, yyjson_doc_get_root(input));
            JsonResult *kind = result_object(), *values = result_array();
            result_string(kind, "type", "string");
            result_append(values, yyjson_mut_str(scu.doc, kinds[index]));
            result_member(kind, "enum", values);
            result_member(yyjson_mut_obj_get(option, "properties"), "kind", kind);
            JsonResult *observe_required = result_array();
            if (index == 0) {
                result_append(observe_required, yyjson_mut_str(scu.doc, "kind"));
            }
            /* Window identifiers can come from the last targeted step. */
            result_member(option, "required", observe_required);
            result_append(choices, option);
            yyjson_doc_free(input);
        }
        result_member(observe, "anyOf", choices);
        result_string(observe, "description",
            "Omit for a final image after foreground input; false disables it. "
            "Use {kind:desktop,display_id,max_image_dimension} for an image, "
            "or {kind:window,pid,window_id,tree_format} for a tree (kind optional). "
            "An explicit object replaces automatic observation.");
        result_member(properties, "observe", observe);
    }
    if (tool->required != NULL) {
        for (const char *const *name = tool->required; *name != NULL; name++) {
            result_append(required, yyjson_mut_strcpy(scu.doc, *name));
        }
    }
    result_string(schema, "type", "object");
    result_member(schema, "properties", properties);
    result_member(schema, "required", required);
    result_boolean(schema, "additionalProperties", FALSE);

    /* Validation reads an immutable schema; all temporary results share the
     * current response arena and disappear together at the end of the call. */
    return yyjson_mut_val_imut_copy(schema, NULL);
}

static gboolean validate_schema(JsonValue *v, JsonValue *s)
{
    JsonValue *alternatives = json_member(s, "anyOf");
    if (alternatives != NULL) {
        size_t index, count;
        JsonValue *option;
        yyjson_arr_foreach(alternatives, index, count, option) {
            if (validate_schema(v, option)) {
                return TRUE;
            }
        }
        return FALSE;
    }
    const char *type = json_string(s, "type");
    gboolean ok = type && ((!strcmp(type, "object") && yyjson_is_obj(v)) ||
                              (!strcmp(type, "array") && yyjson_is_arr(v)) ||
                              (!strcmp(type, "string") && yyjson_is_str(v) &&
                                  strlen(yyjson_get_str(v)) == yyjson_get_len(v)) ||
                              (!strcmp(type, "integer") && yyjson_is_int(v)) ||
                              (!strcmp(type, "number") && yyjson_is_num(v)) ||
                              (!strcmp(type, "boolean") && yyjson_is_bool(v)));
    if (!ok) {
        return FALSE;
    }
    JsonValue *e = json_member(s, "enum");
    if (e) {
        ok = FALSE;
        size_t i, m;
        JsonValue *x;
        yyjson_arr_foreach(e, i, m, x)
        {
            if (yyjson_equals(v, x)) {
                ok = TRUE;
            }
        }
        if (!ok) {
            return FALSE;
        }
    }
    if (yyjson_is_num(v) && (yyjson_get_num(v) < json_number(s, "minimum", -1e100) ||
                                yyjson_get_num(v) > json_number(s, "maximum", 1e100))) {
        return FALSE;
    }
    if (yyjson_is_obj(v)) {
        size_t i, m;
        JsonValue *k, *x, *props = json_member(s, "properties"),
                          *required = json_member(s, "required");
        yyjson_arr_foreach(required, i, m, x)
        {
            if (!json_member(v, yyjson_get_str(x))) {
                return FALSE;
            }
        }
        yyjson_obj_foreach(v, i, m, k, x)
        {
            JsonValue *p = json_member(props, yyjson_get_str(k));
            if (!p && yyjson_is_false(json_member(s, "additionalProperties"))) {
                return FALSE;
            }
            if (p && !validate_schema(x, p)) {
                return FALSE;
            }
        }
    }
    if (yyjson_is_arr(v)) {
        size_t n = yyjson_arr_size(v), i, m;
        JsonValue *x;
        if (n < json_number(s, "minItems", 0) || n > json_number(s, "maxItems", 1e9)) {
            return FALSE;
        }
        yyjson_arr_foreach(v, i, m, x)
        {
            if (!validate_schema(x, json_member(s, "items"))) {
                return FALSE;
            }
        }
    }
    return TRUE;
}

static gboolean validate_arguments(const Tool *t, JsonValue *args)
{
    yyjson_doc *d = build_tool_schema(t);
    gboolean ok = d && validate_schema(args, yyjson_doc_get_root(d));
    yyjson_doc_free(d);
    if (!ok) {
        scu_fail("invalid_arguments",
            "Arguments do not match this scu tool's advertised supported schema.");
    }
    if (!ok) {
        return FALSE;
    }
    gboolean semantic = json_string(args, "element_token") != NULL;
    gboolean pointer =
        strcmp(t->name, "click") == 0 || strcmp(t->name, "double_click") == 0 ||
        strcmp(t->name, "right_click") == 0 || strcmp(t->name, "move_cursor") == 0 ||
        strcmp(t->name, "scroll") == 0;
    if (pointer && !semantic &&
        (json_member(args, "x") == NULL || json_member(args, "y") == NULL)) {
        scu_fail("invalid_arguments", "Pixel actions require both x and y, or a "
                                      "supported accessibility element_token.");
        return FALSE;
    }
    if (semantic &&
        (json_member(args, "x") != NULL || json_member(args, "y") != NULL)) {
        scu_fail("invalid_arguments",
            "Choose either an element token or pixel coordinates.");
        return FALSE;
    }
    if (strcmp(t->name, "get_window_state") == 0 &&
        json_boolean(args, "include_screenshot")) {
        scu_fail("unsupported_feature",
            "Window screenshots are unsupported; use get_desktop_state.");
        return FALSE;
    }
    if (!semantic && strcmp(t->name, "run_steps") != 0 && t->action &&
        (json_member(args, "pid") != NULL || json_member(args, "window_id") != NULL)) {
        scu_fail("unsupported_feature",
            "Window-targeted raw input is unsupported; use a token or desktop target.");
        return FALSE;
    }
    JsonValue *target = json_member(args, "target");
    if (target != NULL) {
        const char *kind = json_string(target, "kind");
        if ((strcmp(kind, "desktop") == 0 &&
                (json_string(target, "display_id") == NULL || semantic)) ||
            (strcmp(kind, "window") == 0 &&
                (!semantic || json_member(target, "pid") == NULL ||
                    json_member(target, "window_id") == NULL))) {
            scu_fail("invalid_arguments",
                "Use a desktop target with display_id, or a "
                "semantic window target with pid and window_id.");
            return FALSE;
        }
    }
    if (t->action && strcmp(t->name, "run_steps") != 0 && !semantic &&
        g_strcmp0(json_string(args, "delivery_mode"), "background") == 0) {
        scu_fail("background_unavailable", "Raw desktop input requires foreground delivery.");
        return FALSE;
    }
    return TRUE;
}

static JsonResult *execute_tool(const Tool *t, JsonValue *args);
static JsonResult *error_payload(void)
{
    JsonResult *r = result_object();
    result_string(r, "code", scu.code ? scu.code : "operation_failed");
    result_string(r, "hint", scu.hint ? scu.hint : "Inspect state before retrying.");
    return r;
}

/* Batch defaults must not retarget an element or reuse a capture on another
 * monitor. Each merged request still passes the ordinary tool validator. */
static yyjson_doc *merge_batch_defaults(
    JsonValue *args, JsonValue *batch, gboolean observation)
{
    yyjson_mut_doc *document = yyjson_mut_doc_new(NULL);
    JsonResult *merged = yyjson_val_mut_copy(document, args);
    yyjson_mut_doc_set_root(document, merged);
    gboolean semantic = json_string(args, "element_token") != NULL;
    const char *defaults[] = {"pid", "window_id", "session", "delivery_mode", NULL};
    for (unsigned index = 0; defaults[index]; index++) {
        const char *key = defaults[index];
        if ((!strcmp(key, "pid") || !strcmp(key, "window_id")) && !observation &&
            (json_member(args, "pid") || json_member(args, "window_id") ||
                json_member(args, "target"))) {
            continue;
        }
        if (observation && !strcmp(key, "delivery_mode")) {
            continue;
        }
        if (!json_member(args, key) && json_member(batch, key)) {
            yyjson_mut_obj_add(merged, yyjson_mut_strcpy(document, key),
                yyjson_val_mut_copy(document, json_member(batch, key)));
        }
    }
    if (!observation && !semantic) {
        JsonValue *own_target = json_member(args, "target");
        JsonValue *batch_target = json_member(batch, "target");
        if (!own_target && batch_target && !json_member(args, "pid") &&
            !json_member(args, "window_id")) {
            yyjson_mut_obj_add(merged, yyjson_mut_str(document, "target"),
                yyjson_val_mut_copy(document, batch_target));
        }
        gboolean same_display = !own_target || (batch_target &&
            g_strcmp0(json_string(own_target, "display_id"),
                json_string(batch_target, "display_id")) == 0);
        gboolean pixel = json_member(args, "x") || json_member(args, "from_x");
        if (pixel && same_display && !json_member(args, "capture_id") &&
            json_member(batch, "capture_id")) {
            yyjson_mut_obj_add(merged, yyjson_mut_str(document, "capture_id"),
                yyjson_val_mut_copy(document, json_member(batch, "capture_id")));
        }
    }
    yyjson_mut_obj_remove_str(merged, "kind"); /* Observation discriminator only. */
    yyjson_doc *result = yyjson_mut_doc_imut_copy(document, NULL);
    yyjson_mut_doc_free(document);
    return result;
}

static void clear_tool_error(void)
{
    g_clear_pointer(&scu.code, g_free);
    g_clear_pointer(&scu.hint, g_free);
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
    const char *fields[] = {"display", "capture_id", "screenshot_width",
        "screenshot_height", "frame_status", "displays", NULL};
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
static JsonResult *run_steps(JsonValue *args)
{
    yyjson_doc *steps[64] = {0};
    const Tool *actions[64] = {0};
    JsonValue *array = json_member(args, "steps");
    size_t count = yyjson_arr_size(array), index, length;
    JsonValue *step, *last_window = NULL;
    yyjson_doc *observation = NULL;
    const Tool *observer = NULL;
    JsonValue *observe = json_member(args, "observe");
    gboolean automatic = observe == NULL, has_foreground = FALSE;
    gboolean valid = FALSE;
    JsonResult *result = result_object(), *warnings = result_array();

    yyjson_arr_foreach(array, index, length, step) {
        const char *name = json_string(step, "tool");
        JsonValue *arguments = json_member(step, "args");
        if (name) {
            if (yyjson_obj_size(step) != 2) {
                goto invalid;
            }
        } else {
            if (yyjson_obj_size(step) != 1) {
                goto invalid;
            }
            yyjson_obj_iter iterator = yyjson_obj_iter_with(step);
            JsonValue *key = yyjson_obj_iter_next(&iterator);
            name = yyjson_get_str(key);
            arguments = yyjson_obj_iter_get_val(key);
        }
        const Tool *action = actions[index] = find_tool(name);
        if (!action || !action->action || !strcmp(name, "run_steps") ||
            !yyjson_is_obj(arguments)) {
            goto invalid;
        }
        steps[index] = merge_batch_defaults(arguments, args, FALSE);
        JsonValue *merged = yyjson_doc_get_root(steps[index]);
        if (!validate_arguments(action, merged)) {
            goto done;
        }
        has_foreground |= json_string(merged, "element_token") == NULL;
        if (json_member(merged, "pid") && json_member(merged, "window_id")) {
            last_window = merged;
        } else {
            JsonValue *target = json_member(merged, "target");
            if (g_strcmp0(json_string(target, "kind"), "window") == 0) {
                last_window = target;
            }
        }
    }
    if (automatic && has_foreground) {
        const char *defaults = "{\"max_image_dimension\":1600}";
        observation = yyjson_read(defaults, strlen(defaults), 0);
        observer = find_tool("get_desktop_state");
    } else if (yyjson_is_obj(observe)) {
        gboolean desktop = g_strcmp0(json_string(observe, "kind"), "desktop") == 0;
        if (desktop) {
            /* Desktop observations do not inherit semantic window identifiers. */
            yyjson_doc *empty = yyjson_read("{}", 2, 0);
            observation = merge_batch_defaults(observe, yyjson_doc_get_root(empty), TRUE);
            yyjson_doc_free(empty);
        } else {
            observation = merge_batch_defaults(observe, last_window ? last_window : args, TRUE);
        }
        observer = find_tool(desktop ? "get_desktop_state" : "get_window_state");
    }
    if (observer && !validate_arguments(observer, yyjson_doc_get_root(observation))) {
        goto done;
    }
    valid = TRUE;
    guint completed = 0, skipped = 0;
    gboolean failed = FALSE, foreground_attempted = FALSE;
    for (index = 0; index < count; index++) {
        JsonValue *merged = yyjson_doc_get_root(steps[index]);
        foreground_attempted |= json_string(merged, "element_token") == NULL;
        JsonResult *value = execute_tool(actions[index], merged);
        if (value == NULL) {
            release_input();
            result_member(result, "failed", batch_failure(index, actions[index]));
            skipped = count - index - 1;
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
        if (index + 1 < count && !scu_pause((unsigned)json_number(args, "delay_ms", 0))) {
            JsonResult *failure = batch_failure(index, actions[index]);
            result_string(failure, "phase", "delay");
            result_member(result, "failed", failure);
            skipped = count - completed;
            failed = TRUE;
            clear_tool_error();
            break;
        }
    }
    result_integer(result, "completed", completed);
    result_integer(result, "skipped", skipped);
    result_string(result, "effect", "unverifiable");
    if (yyjson_mut_arr_size(warnings) > 0) {
        result_member(result, "warnings", warnings);
    }
    if (observer && (!automatic || foreground_attempted) &&
        !g_atomic_int_get(&scu.cancelled) && !g_atomic_int_get(&scu.stopping)) {
        /* A broken visual session is not reconnected inside the interrupted
         * batch. Only a subsequent request may establish another grant. */
        JsonResult *value = NULL;
        if (visual.broken || (failed && foreground_attempted &&
                visual.session == NULL && !strcmp(observer->name, "get_desktop_state"))) {
            scu_fail("visual_session_closed", "Visual session disconnected during the batch.");
        } else {
            value = execute_tool(observer, yyjson_doc_get_root(observation));
        }
        if (value) {
            result_member(result, "observation",
                !strcmp(observer->name, "get_desktop_state")
                    ? compact_observation(value) : value);
        } else {
            result_member(result, "observation_error", error_payload());
            failed = TRUE;
            clear_tool_error();
        }
    }
    result_boolean(result, "ok", !failed);
    goto done;
invalid:
    scu_fail("invalid_arguments", "Each step must be {tool,args} or one supported action-name shorthand.");
done:
    for (index = 0; index < count; index++) {
        yyjson_doc_free(steps[index]);
    }
    yyjson_doc_free(observation);
    return valid ? result : NULL;
}

static JsonResult *execute_tool(const Tool *t, JsonValue *args)
{
    if (!strcmp(t->name, "health_report")) {
        return scu_health();
    }
    if (!scu_tick()) {
        return NULL;
    }
    if (!strcmp(t->name, "run_steps")) {
        return run_steps(args);
    }
    if (!strcmp(t->name, "get_window_state") &&
        json_boolean(args, "include_screenshot")) {
        return scu_fail("unsupported_feature",
            "Window screenshots are unsupported; use get_desktop_state for monitor "
            "pixels.");
    }
    if (!strncmp(t->name, "get_accessibility", 17) ||
        !strcmp(t->name, "list_windows") || !strcmp(t->name, "get_window_state") ||
        !strcmp(t->name, "set_value") || json_string(args, "element_token")) {
        return scu_atspi(t->name, args);
    }
    if (json_member(args, "pid") || json_member(args, "window_id") ||
        !g_strcmp0(json_string(json_member(args, "target"), "kind"), "window")) {
        return scu_fail("unsupported_feature",
            "Window-targeted raw input is unsupported. Use an accessibility token or "
            "explicit desktop target.");
    }
    return scu_visual(t->name, args);
}

static char *request_id_key(JsonValue *id)
{
    return id ? yyjson_val_write(id, 0, NULL) : NULL;
}

/* Only this thread reads stdin. It queues complete request documents and can
 * flag cancellation immediately while the main thread is inside a desktop call.
 * It never uses response JSON or accessibility objects owned by the main thread. */
static gpointer read_requests(gpointer unused)
{
    (void)unused;
    GString *line = g_string_new(NULL);
    char c;
    gboolean oversized = FALSE;
    while (read(STDIN_FILENO, &c, 1) == 1) {
        if (c == '\n') {
            yyjson_doc *d = oversized ? NULL : yyjson_read(line->str, line->len, 0);
            JsonValue *r = d ? yyjson_doc_get_root(d) : NULL;
            if (!g_strcmp0(json_string(r, "method"), "notifications/cancelled")) {
                char *id =
                    request_id_key(json_member(json_member(r, "params"), "requestId"));
                g_mutex_lock(&active_lock);
                if (id && g_hash_table_size(cancelled_ids) < 256) {
                    g_hash_table_add(cancelled_ids, g_strdup(id));
                }
                if (id && !g_strcmp0(id, active_id)) {
                    g_atomic_int_set(&scu.cancelled, 1);
                }
                g_mutex_unlock(&active_lock);
                free(id);
                yyjson_doc_free(d);
            } else {
                if (d) {
                    g_async_queue_push(requests, d);
                } else {
                    g_async_queue_push(requests, GINT_TO_POINTER(1));
                }
            }
            g_string_truncate(line, 0);
            oversized = FALSE;
        } else if (line->len < 1024 * 1024) {
            g_string_append_c(line, c);
        } else {
            oversized = TRUE;
        }
    }
    g_string_free(line, TRUE);
    g_atomic_int_set(&scu.stopping, 1);
    return NULL;
}

static void send_response(
    JsonValue *id, JsonResult *payload, int rpc_error, const char *message)
{
    JsonResult *reply = result_object();
    if (modern && payload != NULL && yyjson_mut_obj_get(payload, "_meta") == NULL) {
        JsonResult *metadata = result_object(), *info = result_object();
        result_string(info, "name", "scu");
        result_string(info, "version", "0.1.0");
        result_member(metadata, "io.modelcontextprotocol/serverInfo", info);
        result_member(payload, "_meta", metadata);
    }
    result_string(reply, "jsonrpc", "2.0");
    result_member(
        reply, "id", id ? yyjson_val_mut_copy(scu.doc, id) : yyjson_mut_null(scu.doc));
    if (rpc_error) {
        JsonResult *e = result_object();
        result_integer(e, "code", rpc_error);
        result_string(e, "message", message);
        if (rpc_error == -32022) {
            JsonResult *data = result_object(), *supported = result_array();
            result_append(supported, yyjson_mut_str(scu.doc, "2026-07-28"));
            result_append(supported, yyjson_mut_str(scu.doc, "2025-11-25"));
            result_append(supported, yyjson_mut_str(scu.doc, "2025-06-18"));
            result_member(data, "supported", supported);
            result_string(data, "requested", message);
            result_member(e, "data", data);
            result_string(e, "message", "Unsupported protocol version");
        }
        result_member(reply, "error", e);
    } else {
        result_member(reply, "result", payload);
    }
    yyjson_mut_doc_set_root(scu.doc, reply);
    char *line = yyjson_mut_write(scu.doc, 0, NULL);
    if (line) {
        puts(line);
        fflush(stdout);
        free(line);
    }
}

/* Protocol metadata contains no desktop authority. Initialization and listing
 * tools must work even when accessibility permission or guest libraries are absent. */
static void add_server_info(JsonResult *result)
{
    result_string(result, "instructions",
        "Read a tree or desktop image first. Use run_steps for known action sequences; "
        "foreground batches return one final image. Inspect it to verify effects; "
        "if loading continues, observe again without replaying actions. "
        "An explicit display selects the monitor for later operations, not keyboard focus. "
        "Accessibility permission is required. No browser, shell, JavaScript or window capture tools.");
    JsonResult *capabilities = result_object();
    JsonResult *info = result_object();
    result_member(capabilities, "tools", result_object());
    result_member(result, "capabilities", capabilities);
    result_string(info, "name", "scu");
    result_string(info, "version", "0.1.0");
    if (modern) {
        JsonResult *metadata = result_object();
        result_member(metadata, "io.modelcontextprotocol/serverInfo", info);
        result_member(result, "_meta", metadata);
    } else {
        result_member(result, "serverInfo", info);
    }
}

static void list_tools(JsonResult *result)
{
    JsonResult *tool_list = result_array();
    for (unsigned index = 0; index < G_N_ELEMENTS(tools); index++) {
        const Tool *tool = &tools[index];
        JsonResult *item = result_object();
        JsonResult *annotations = result_object();
        yyjson_doc *schema = build_tool_schema(tool);

        result_string(item, "name", tool->name);
        result_string(item, "description", tool->description);
        result_member(item, "inputSchema",
            yyjson_val_mut_copy(scu.doc, yyjson_doc_get_root(schema)));
        result_boolean(annotations, "readOnlyHint", !tool->action);
        result_boolean(annotations, "idempotentHint", FALSE);
        result_boolean(annotations, "openWorldHint", TRUE);
        result_member(item, "annotations", annotations);
        result_append(tool_list, item);
        yyjson_doc_free(schema);
    }
    result_member(result, "tools", tool_list);
}

static void add_tool_result(JsonResult *result, const Tool *tool, JsonResult *output)
{
    JsonResult *content = result_array();
    JsonResult *text = result_object();
    JsonResult *structured = output != NULL ? output : error_payload();
    char *body = yyjson_mut_val_write(structured, 0, NULL);

    result_string(text, "type", "text");
    result_string(text, "text", body);
    result_append(content, text);
    free(body);

    size_t index, count;
    JsonResult *image;
    yyjson_mut_arr_foreach(scu.images, index, count, image)
    {
        /* Mutable values carry their container links. Copy before putting an
         * image in another array so iteration does not alter its source list. */
        result_append(content, yyjson_mut_val_mut_copy(scu.doc, image));
    }
    result_member(result, "content", content);
    result_member(result, "structuredContent", structured);
    gboolean batch_failed = output != NULL && tool != NULL &&
                            strcmp(tool->name, "run_steps") == 0 &&
                            yyjson_mut_is_false(yyjson_mut_obj_get(output, "ok"));
    result_boolean(result, "isError", output == NULL || batch_failed);
}

static gboolean call_tool(JsonValue *id, JsonValue *parameters, JsonResult *result)
{
    const Tool *tool = find_tool(json_string(parameters, "name"));
    JsonValue *arguments = json_member(parameters, "arguments");
    yyjson_doc *empty = NULL;
    if (arguments == NULL) {
        empty = yyjson_read("{}", 2, 0);
        arguments = yyjson_doc_get_root(empty);
    }

    char *key = request_id_key(id);
    g_mutex_lock(&active_lock);
    active_id = g_strdup(key);
    g_atomic_int_set(&scu.cancelled, g_hash_table_contains(cancelled_ids, key));
    g_mutex_unlock(&active_lock);

    JsonResult *output = NULL;
    if (tool == NULL) {
        scu_fail("unknown_tool", "Tool is not implemented by scu.");
    } else if (validate_arguments(tool, arguments)) {
        output = execute_tool(tool, arguments);
    }
    add_tool_result(result, tool, output);
    yyjson_doc_free(empty);

    g_mutex_lock(&active_lock);
    g_clear_pointer(&active_id, g_free);
    g_hash_table_remove(cancelled_ids, key);
    g_mutex_unlock(&active_lock);
    free(key);

    if (g_atomic_int_get(&scu.cancelled)) {
        scu_invalidate();
        return FALSE; /* Cancelled requests must not emit a late response. */
    }
    return TRUE;
}

static void handle_request(yyjson_doc *document)
{
    JsonValue *request = document != NULL ? yyjson_doc_get_root(document) : NULL;
    JsonValue *id = json_member(request, "id");
    JsonValue *parameters = json_member(request, "params");
    const char *method = json_string(request, "method");

    /* Everything constructed for this response shares one allocation arena. */
    scu.doc = yyjson_mut_doc_new(NULL);
    scu.images = result_array();
    scu.deadline = g_get_monotonic_time() + 60 * G_USEC_PER_SEC;
    g_clear_pointer(&scu.code, g_free);
    g_clear_pointer(&scu.hint, g_free);

    if (!yyjson_is_obj(request) ||
        g_strcmp0(json_string(request, "jsonrpc"), "2.0") != 0 || method == NULL ||
        (id != NULL && !yyjson_is_str(id) && !yyjson_is_int(id))) {
        send_response(id, NULL, -32600, "Invalid JSON-RPC request");
        goto cleanup;
    }
    if (strcmp(method, "notifications/initialized") == 0) {
        initialized = TRUE;
        goto cleanup;
    }
    if (id == NULL) {
        goto cleanup; /* JSON-RPC notifications receive no response. */
    }

    const char *version = json_string(
        json_member(parameters, "_meta"), "io.modelcontextprotocol/protocolVersion");
    if (!initialized && version == NULL && strcmp(method, "initialize") != 0) {
        send_response(id, NULL, -32602,
            "Modern requests require protocol metadata on every request.");
        goto cleanup;
    }
    modern = version != NULL;
    if (version != NULL) {
        if (strcmp(version, "2026-07-28") != 0) {
            send_response(id, NULL, -32022, version);
            goto cleanup;
        }
        if (!yyjson_is_obj(json_member(json_member(parameters, "_meta"),
                "io.modelcontextprotocol/clientCapabilities"))) {
            send_response(id, NULL, -32602,
                "Modern requests require clientCapabilities metadata.");
            goto cleanup;
        }
    }

    JsonResult *result = result_object();
    if (modern) {
        result_string(result, "resultType", "complete");
    }
    if (strcmp(method, "initialize") == 0) {
        version = json_string(parameters, "protocolVersion");
        if (version == NULL || (strcmp(version, "2025-06-18") != 0 &&
                                   strcmp(version, "2025-11-25") != 0)) {
            send_response(id, NULL, -32602,
                "Supported legacy protocols: 2025-11-25, 2025-06-18; modern: "
                "2026-07-28");
            goto cleanup;
        }
        modern = FALSE;
        initialized = TRUE;
        result_string(result, "protocolVersion", version);
        add_server_info(result);
    } else if (strcmp(method, "server/discover") == 0 && modern) {
        JsonResult *versions = result_array();
        result_append(versions, yyjson_mut_str(scu.doc, "2026-07-28"));
        result_member(result, "supportedVersions", versions);
        add_server_info(result);
    } else if (!initialized && !modern) {
        send_response(id, NULL, -32000, "Initialize scu first");
        goto cleanup;
    } else if (strcmp(method, "tools/list") == 0) {
        list_tools(result);
    } else if (strcmp(method, "tools/call") == 0) {
        if (!call_tool(id, parameters, result)) {
            goto cleanup;
        }
    } else if (strcmp(method, "ping") != 0) {
        send_response(id, NULL, -32601, "Method not found");
        goto cleanup;
    }
    send_response(id, result, 0, NULL);

cleanup:
    yyjson_mut_doc_free(scu.doc);
    scu.doc = NULL;
    scu.images = NULL;
    scu.deadline = 0;
}

static void stop_signal(int sig)
{
    (void)sig;
    g_atomic_int_set(&scu.stopping, 1);
}

/* Keep enrollment discoverable without starting MCP or touching the desktop. */
static void print_help(FILE *stream)
{
    fputs("scu - Spaces computer use\n"
          "\n"
          "Usage: scu mcp       Serve MCP over stdin/stdout (no listening port).\n"
          "       scu --help    Show this help.\n"
          "\n"
          "Enroll with your agent from inside the Space, as the agent's user:\n"
          "  Codex:\n"
          "    codex mcp add scu -- scu mcp\n"
          "  Claude Code:\n"
          "    claude mcp add --transport stdio --scope user scu -- scu mcp\n"
          "  Other MCP clients: command \"scu\", arguments [\"mcp\"], transport stdio.\n"
          "\n"
          "Reload the agent's MCP connections after enrollment.\n"
          "Host permission: run `sudo spaces configure <space>` on the host and\n"
          "enable Accessibility and Agent computer use.\n",
        stream);
}

int main(int argc, char **argv)
{
    if (argc == 1 || (argc == 2 &&
                        (!strcmp(argv[1], "--help") || !strcmp(argv[1], "-h")))) {
        print_help(stdout);
        return 0;
    }
    if (argc == 3 && !strcmp(argv[1], "--atspi-worker")) {
        return run_atspi_worker((guint)g_ascii_strtoull(argv[2], NULL, 10));
    }
    if (argc != 2 || strcmp(argv[1], "mcp")) {
        print_help(stderr);
        return 2;
    }
    signal(SIGPIPE, SIG_IGN);
    signal(SIGINT, stop_signal);
    signal(SIGTERM, stop_signal);
    requests = g_async_queue_new();
    cancelled_ids = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    g_thread_new("scu-stdin", read_requests, NULL);
    while (!g_atomic_int_get(&scu.stopping)) {
        gpointer p = g_async_queue_timeout_pop(requests, 100000);
        if (p == GINT_TO_POINTER(1)) {
            scu.doc = yyjson_mut_doc_new(NULL);
            send_response(NULL, NULL, -32700, "Malformed or oversized JSON message");
            yyjson_mut_doc_free(scu.doc);
            scu.doc = NULL;
        } else if (p) {
            handle_request(p);
            yyjson_doc_free(p);
        } else if (scu.generation) {
            scu_gate();
            g_clear_pointer(&scu.code, g_free);
            g_clear_pointer(&scu.hint, g_free);
        }
        poll_visual_events();
        while (g_main_context_iteration(NULL, FALSE)) {
        }
    }
    scu_invalidate();
    return 0;
}
