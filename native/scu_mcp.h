/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* MCP transport and catalog shared by the guest frontend and host workers.
 * This layer has no desktop dependencies or desktop authority. */
#include "vendor/yyjson.h"
#include <glib.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#define MAX_IMAGE_DIMENSION 2048
#define ELEMENT_CAPACITY 20000

/* Public catalog: usage, descriptions, argument variants, membership and observations. */
static const char usage[] =
    "scu - Spaces computer use\n"
    "\n"
    "Usage: scu mcp [--no-vision]  Serve MCP over stdin/stdout.\n"
    "       scu --help    Show this help.\n"
    "\n"
    "Enroll with your agent from inside the Space, as the agent's user:\n"
    "  Codex:\n"
    "    codex mcp add scu -- scu mcp\n"
    "  Claude Code:\n"
    "    claude mcp add --transport stdio --scope user scu -- scu mcp\n"
    "  Other MCP clients: command \"scu\", arguments [\"mcp\"], transport stdio.\n"
    "\n"
    "Use --no-vision to hide vision and omit images from status.\n"
    "Reload the agent's MCP connections after enrollment.\n"
    "\n"
    "Host permission: run `sudo spaces configure <space>` on the host and\n"
    "enable Accessibility and Agent computer use or use the Development preset.\n";

static const char desktop_locked_warning[] =
    "The screen is locked. Vision tools are unavailable. You need to use the accesibility tool or ask the user to unlock.";
static const char keyboard_locked_warning[] =
    "This control requires keyboard input, which is unavailable while the screen is locked.";

static const char server_instructions[] =
    "Computer use MCP server. Control the host desktop: screenshots, mouse, "
    "keyboard, and accessibility. Use `status` to inspect the desktop, "
    "`vision` for pointer/keyboard actions and screenshots, and `accessibility` "
    "for element actions and window trees.";
static const char no_vision_instructions[] =
    "Computer use MCP server. Control the host desktop through accessibility. "
    "Start with status for canvas metadata and window IDs, "
    "then use accessibility for element actions and window trees. "
    "This MCP has no vision support.";

static const char status_description[] =
    "Run first for health checks, canvas metadata, apps/windows and a full desktop screenshot. "
    "Window bounds, element frames and pixel actions share the full screenshot's coordinates.";
static const char no_vision_status_description[] =
    "Run first for health checks, canvas metadata, apps and windows.";

static const char vision_description[] =
    "Execute up to 64 JSON steps shaped {tool,args}, in order and stop "
    "on failure. Returns final observations. "
    "Responses report status:ok/failed and the completed step count. "
    "Insert {tool:delay_ms,args:{ms:500}} to pause anywhere, the final "
    "observations wait for all steps including trailing delays (there are "
    "some default delays if not provided). "
    "Use observations:false to skip observation or observations:{window:window_id,refresh:false} "
    "for screenshots and accessibility trees of a window. "
    "Omit observations for a desktop screenshot. "
    "When the tree provides the controls you need, prefer the accessibility tool. "
    "I.e., working with a single application without the need for vision, especially browsers. "
    "When possible, a zero-context unified diff against the last returned tree is provided. "
    "Set refresh:true to force a full tree. "
    "All pixel coordinates are global to the full overview. ";
static const char accessibility_description[] =
    "Execute up to 64 JSON steps shaped {tool,args}, in order and stop "
    "on failure. Responses report status:ok/failed and the completed step count. "
    "Insert {tool:delay_ms,args:{ms:500}} to pause anywhere. "
    "Use observations:{window:window_id,refresh:false} for accessibility trees that also include text content "
    "after all steps, including trailing delays. Omit observations to skip tree reads. "
    "When possible, a zero-context unified diff against the last returned tree is provided. "
    "Set refresh:true to force a full tree. "
    "A different window or large diff returns a full tree. Set refresh:true for a full tree. "
    "Element actions can target offscreen controls. ";

typedef enum {
    ARG_STRING,
    ARG_NUMBER,
    ARG_INTEGER,
    ARG_BOOLEAN,
    ARG_STRING_ARRAY,
    ARG_OBJECT,
    ARG_OBJECT_ARRAY,
    ARG_NUMBER_ARRAY,
    ARG_INTEGER_ARRAY,
    ARG_OBSERVATION,
} ArgumentType;

typedef struct {
    const char *name;
    ArgumentType type;
    const char *const *choices;
    double minimum;
    double maximum; /* Zero means no bound in this argument definition. */
    const char *description;
    gboolean allow_false;
} Argument;

typedef enum {
    ARGS_NONE = 0,
    ARGS_ACTION = 1 << 0,
    ARGS_POINT = 1 << 1,
} ArgumentGroups;

typedef enum { BATCH_NONE = 0, BATCH_VISION = 1, BATCH_ACCESSIBILITY = 2 } BatchKind;

typedef struct {
    const char *name;
    const char *description;
    ArgumentGroups groups;
    const Argument *arguments;
    const char *const *required;
    gboolean action;
    BatchKind batch, modes;
    const char *const *targeted_required;
} Tool;

static const char *const pointer_buttons[] = {"left", "right", "middle", NULL};
static const char *const scroll_directions[] = {"up", "down", "left", "right", NULL};

static const Argument action_arguments[] = {
    {.name = "element_id", .type = ARG_INTEGER, .minimum = 0, .maximum = ELEMENT_CAPACITY - 1,
        .description = "Element ID from any window tree. Rolls over after 20k elements. Remains valid while the element exists."},
    {NULL},
};
static const Argument point_arguments[] = {
    {.name = "x", .type = ARG_NUMBER},
    {.name = "y", .type = ARG_NUMBER},
    {NULL},
};
static const Argument click_arguments[] = {
    {.name = "button", .type = ARG_STRING, .choices = pointer_buttons},
    {.name = "count", .type = ARG_INTEGER, .minimum = 1, .maximum = 3},
    {NULL},
};
static const Argument drag_arguments[] = {
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
    {.name = "amount", .type = ARG_INTEGER, .minimum = 1, .maximum = 700},
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
    {.name = "keys", .type = ARG_STRING_ARRAY, .minimum = 1, .maximum = 5},
    {NULL},
};
static const Argument delay_arguments[] = {
    {.name = "ms", .type = ARG_INTEGER, .minimum = 0, .maximum = 30000,
        .description = "Milliseconds to wait."},
    {NULL},
};
static const Argument vision_arguments[] = {
    {.name = "steps", .type = ARG_OBJECT_ARRAY, .maximum = 64,
        .description = "Optional ordered {tool,args} actions. Omit or pass [] to observe without acting."},
    {.name = "observations", .type = ARG_OBSERVATION, .allow_false = TRUE,
        .description = "Window for a screenshot and tree after acting. Optional. Set to false to skip observation or do not provide for a desktop screenshot."},
    {NULL},
};

static const Argument accessibility_arguments[] = {
    {.name = "steps", .type = ARG_OBJECT_ARRAY, .maximum = 64,
        .description = "Optional ordered {tool,args} actions."},
    {.name = "observations", .type = ARG_OBSERVATION,
        .description = "Window for a tree after acting. Omit to skip observation."},
    {NULL},
};
static const char *const require_point[] = {"x", "y", NULL};
static const char *const require_element[] = {"element_id", NULL};
static const char *const require_drag[] = {"from_x", "from_y", "to_x", "to_y", NULL};
static const char *const require_scroll[] = {"x", "y", "direction", NULL};
static const char *const require_text[] = {"text", NULL};
static const char *const require_targeted_text[] = {"element_id", "text", NULL};
static const char *const require_value[] = {"element_id", "value", NULL};
static const char *const require_keys[] = {"keys", NULL};
static const char *const require_delay[] = {"ms", NULL};

static const Tool tools[] = {
    {.name = "status",
        status_description,
        ARGS_NONE},
    {.name = "vision",
        vision_description,
        .arguments = vision_arguments, .action = TRUE, .batch = BATCH_VISION},
    {.name = "accessibility",
        accessibility_description,
        .arguments = accessibility_arguments, .action = TRUE, .batch = BATCH_ACCESSIBILITY},
};

/* Actions are batch steps, never standalone MCP tools. */
static const Tool action_tools[] = {
    {.name = "click",
        "Click desktop image coordinates.",
        ARGS_POINT,
        click_arguments,
        require_point,
        .action = TRUE, .modes = BATCH_VISION},
    {.name = "move_cursor",
        "Move the foreground pointer in desktop image coordinates.",
        ARGS_POINT,
        .required = require_point,
        .action = TRUE, .modes = BATCH_VISION},
    {.name = "drag",
        "Drag across the combined canvas using full-overview coordinates.",
        ARGS_NONE,
        drag_arguments,
        require_drag,
        TRUE, .modes = BATCH_VISION},
    {.name = "scroll",
        "Scroll at desktop coordinates for a number of lines.",
        ARGS_POINT,
        scroll_arguments,
        require_scroll,
        TRUE, .modes = BATCH_VISION},
    {.name = "press_keys",
        "Press keys in listed order and release in reverse; put modifiers first. Use keys:[meta] for a single key or keys:[ctrl,a] for a chord.",
        ARGS_NONE,
        key_arguments,
        require_keys,
        TRUE, .modes = BATCH_VISION},
    {.name = "activate",
        "Perform an element's default action, such as following a link or pressing a button.",
        ARGS_ACTION,
        .required = require_element,
        .action = TRUE, .modes = BATCH_ACCESSIBILITY},
    {.name = "open_context_menu",
        "Open an element's context menu.",
        ARGS_ACTION,
        .required = require_element,
        .action = TRUE, .modes = BATCH_ACCESSIBILITY},
    {.name = "type_text",
        "Insert text at the caret without pressing Enter.",
        ARGS_NONE,
        text_arguments,
        require_text,
        TRUE, .targeted_required = require_targeted_text, .modes = BATCH_VISION | BATCH_ACCESSIBILITY},
    {.name = "set_value",
        "Replace a text field's contents or set a numeric control's value.",
        ARGS_ACTION,
        value_arguments,
        require_value,
        TRUE, .modes = BATCH_ACCESSIBILITY},
    {.name = "delay_ms",
        "Wait 0-30000 milliseconds before continuing. Use a trailing delay to wait before the final observation.",
        ARGS_NONE,
        delay_arguments,
        require_delay,
        TRUE, .modes = BATCH_VISION | BATCH_ACCESSIBILITY},
};

/* Shared response state and JSON construction helpers. */
typedef yyjson_val JsonValue;
typedef yyjson_mut_val JsonResult;
#ifdef SCU_HOST
static void host_send_line(const char *line);
static char *host_request_key(JsonValue *id);
#endif
typedef struct {
    yyjson_mut_doc *doc;
    JsonResult *images;
    char *code, *hint;
    gboolean vision_request, lock_seen, lock_monitor;
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

static void scu_invalidate(void);
static void poll_backend(void);
static gboolean scu_tick(void);
static GAsyncQueue *requests;
static GMutex active_lock;
static char *active_id;
static gboolean modern, initialized, no_vision;
static GHashTable *cancelled_ids;
static JsonResult *scu_fail(const char *code, const char *hint)
{
    if (!scu.code) {
        scu.code = g_strdup(code);
        scu.hint = g_strdup(hint);
    }
    return NULL;
}

/* Schema construction and runtime validation below consume only this catalog. */
static const Tool *find_tool(const char *name)
{
    for (unsigned index = 0; index < G_N_ELEMENTS(tools); index++) {
        if (g_strcmp0(name, tools[index].name) == 0 &&
            !(no_vision && tools[index].batch == BATCH_VISION)) {
            return &tools[index];
        }
    }
    return NULL;
}

static gboolean find_action(BatchKind batch, const char *name, Tool *selected)
{
    for (unsigned index = 0; index < G_N_ELEMENTS(action_tools); index++) {
        const Tool *action = &action_tools[index];
        if ((action->modes & batch) && g_strcmp0(name, action->name) == 0) {
            *selected = *action;
            if (batch == BATCH_ACCESSIBILITY && action->targeted_required) {
                selected->groups |= ARGS_ACTION;
                selected->required = action->targeted_required;
            }
            return TRUE;
        }
    }
    return FALSE;
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
        case ARG_NUMBER_ARRAY:
        case ARG_INTEGER_ARRAY:
            type = "array";
            break;
        case ARG_OBSERVATION:
        case ARG_OBJECT:
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
        if (argument->type == ARG_STRING_ARRAY || argument->type == ARG_OBJECT_ARRAY ||
            argument->type == ARG_NUMBER_ARRAY || argument->type == ARG_INTEGER_ARRAY) {
            JsonResult *items = result_object();
            result_string(items, "type",
                argument->type == ARG_STRING_ARRAY ? "string" :
                argument->type == ARG_NUMBER_ARRAY ? "number" :
                argument->type == ARG_INTEGER_ARRAY ? "integer" : "object");
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
        if (argument->type == ARG_OBSERVATION) {
            JsonResult *fields = result_object(), *window = result_object(), *refresh = result_object();
            JsonResult *required = result_array();
            result_string(window, "type", "integer");
            result_string(window, "description", "Window ID from status.");
            result_string(refresh, "type", "boolean");
            result_boolean(refresh, "default", FALSE);
            result_member(fields, "window", window);
            result_member(fields, "refresh", refresh);
            result_append(required, yyjson_mut_str(scu.doc, "window"));
            result_member(property, "properties", fields);
            result_member(property, "required", required);
            result_boolean(property, "additionalProperties", FALSE);
        }
        if (argument->allow_false) {
            JsonResult *alternatives = result_array(), *off = result_object(), *values = result_array();
            result_string(off, "type", "boolean");
            result_append(values, yyjson_mut_false(scu.doc));
            result_member(off, "enum", values);
            result_append(alternatives, property);
            result_append(alternatives, off);
            property = result_object();
            result_member(property, "anyOf", alternatives);
        }
        result_member(properties, argument->name, property);
    }
}

static yyjson_doc *build_tool_schema(const Tool *tool)
{
    JsonResult *schema = result_object();
    JsonResult *properties = result_object();
    JsonResult *required = result_array();
    add_argument_schemas(properties, tool->arguments);
    if (tool->groups & ARGS_ACTION) {
        add_argument_schemas(properties, action_arguments);
    }
    if (tool->groups & ARGS_POINT) {
        add_argument_schemas(properties, point_arguments);
    }
    if (tool->batch != BATCH_NONE) {
        /* Advertise the actual nested contract, not an opaque array of objects.
         * Individual steps are validated again before desktop effects. */
        JsonResult *alternatives = result_array();
        for (unsigned index = 0; index < G_N_ELEMENTS(action_tools); index++) {
            Tool variant;
            if (!find_action(tool->batch, action_tools[index].name, &variant)) continue;
            const Tool *action = &variant;
            yyjson_doc *input = build_tool_schema(action);
            JsonResult *named = result_object(), *named_properties = result_object();
            JsonResult *name = result_object(), *names = result_array();
            result_string(named, "type", "object");
            result_string(named, "description", action->description);
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

            yyjson_doc_free(input);
        }
        JsonResult *items = result_object();
        result_member(items, "anyOf", alternatives);
        result_member(yyjson_mut_obj_get(properties, "steps"), "items", items);

    }
    if (tool->required != NULL) {
        JsonResult *ordered = result_object();
        for (const char *const *name = tool->required; *name != NULL; name++) {
            result_append(required, yyjson_mut_strcpy(scu.doc, *name));
            JsonResult *property = yyjson_mut_obj_get(properties, *name);
            if (property) result_member(ordered, *name, yyjson_mut_val_mut_copy(scu.doc, property));
        }
        size_t index, count;
        JsonResult *key, *property;
        yyjson_mut_obj_foreach(properties, index, count, key, property) {
            if (!yyjson_mut_obj_get(ordered, yyjson_mut_get_str(key)))
                result_member(ordered, yyjson_mut_get_str(key), yyjson_mut_val_mut_copy(scu.doc, property));
        }
        properties = ordered;
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
    return ok;
}

static JsonResult *error_payload(void)
{
    JsonResult *r = result_object();
    result_string(r, "code", scu.code ? scu.code : "operation_failed");
    result_string(r, "hint", scu.hint ? scu.hint : "Inspect state before retrying.");
    return r;
}

#ifdef SCU_HOST
static JsonResult *execute_tool(const Tool *tool, JsonValue *args);
#else
static JsonResult *forward_tool(JsonValue *id, JsonValue *parameters);
#endif
static char *request_id_key(JsonValue *id)
{
    return id ? yyjson_val_write(id, 0, NULL) : NULL;
}

/* Only this thread reads stdin. It queues complete request documents and can
 * flag cancellation immediately while the main thread is inside a desktop call.
 * It never uses response JSON or accessibility objects owned by the main thread. */
static G_GNUC_UNUSED gpointer read_requests(gpointer unused)
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
#ifdef SCU_HOST
        host_send_line(line);
#else
        puts(line);
        fflush(stdout);
#endif
        free(line);
    }
}

/* Protocol metadata contains no desktop authority. Initialization and listing
 * tools must work even when accessibility permission or guest libraries are absent. */
static void add_server_info(JsonResult *result)
{
    result_string(result, "instructions", no_vision ? no_vision_instructions : server_instructions);
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
        if (no_vision && tool->batch == BATCH_VISION) continue;
        JsonResult *item = result_object();
        JsonResult *annotations = result_object();
        yyjson_doc *schema = build_tool_schema(tool);

        result_string(item, "name", tool->name);
        result_string(item, "description", no_vision && !strcmp(tool->name, "status")
            ? no_vision_status_description : tool->description);
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

/* The readable content is the tool response seen by the model. Internal
 * observation records are never returned as a second representation. */
static void append_result_value(GString *text, JsonResult *value)
{
    char *encoded = yyjson_mut_val_write(value, 0, NULL);
    if (encoded) g_string_append(text, encoded);
    free(encoded);
}

static void append_result_frame(GString *text, JsonResult *frame)
{
    const char *fields[] = {"x", "y", "width", "height"};
    g_string_append_c(text, '[');
    for (guint i = 0; i < G_N_ELEMENTS(fields); i++) {
        if (i) g_string_append_c(text, ',');
        double value = yyjson_mut_get_num(yyjson_mut_obj_get(frame, fields[i]));
        gint64 rounded = (gint64)(value < 0 ? value - 0.5 : value + 0.5);
        g_string_append_printf(text, "%" G_GINT64_FORMAT, rounded);
    }
    g_string_append_c(text, ']');
}

#ifdef SCU_HOST
static void append_tree_row(GString *text, JsonResult *row, gboolean include_frame)
{
    const char *role = yyjson_mut_get_str(yyjson_mut_obj_get(row, "role"));
    g_string_append_printf(text, "%*s", (int)yyjson_mut_get_sint(yyjson_mut_obj_get(row, "depth")) * 2, "");
    JsonResult *element_id = yyjson_mut_obj_get(row, "element_id");
    if (element_id) g_string_append_printf(text, "[%" G_GINT64_FORMAT "] ", yyjson_mut_get_sint(element_id));
    g_string_append_printf(text, "%s ", role ? role : "unknown");
    append_result_value(text, yyjson_mut_obj_get(row, "label"));
    const char *values[] = {"value", "minimum", "maximum"};
    for (guint i = 0; i < G_N_ELEMENTS(values); i++) {
        JsonResult *value = yyjson_mut_obj_get(row, values[i]);
        if (value) {
            g_string_append_printf(text, " %s=", values[i]);
            append_result_value(text, value);
        }
    }
    JsonResult *actions = yyjson_mut_obj_get(row, "actions"), *action;
    size_t i, n;
    yyjson_mut_arr_foreach(actions, i, n, action) {
        const char *name = yyjson_mut_get_str(action);
        if (!strcmp(name, "type_text") || !strcmp(name, "set_value"))
            g_string_append_printf(text, " %s", name);
    }
    if (yyjson_mut_is_false(yyjson_mut_obj_get(row, "enabled"))) g_string_append(text, " disabled");
    const char *flags[] = {"focused", "selected", "checked", "protected", "text_truncated"};
    for (guint i = 0; i < G_N_ELEMENTS(flags); i++) {
        if (yyjson_mut_is_true(yyjson_mut_obj_get(row, flags[i]))) g_string_append_printf(text, " %s", flags[i]);
    }
    JsonResult *frame = yyjson_mut_obj_get(row, "frame");
    if (include_frame && yyjson_mut_is_obj(frame)) {
        g_string_append(text, " frame=");
        append_result_frame(text, frame);
    }
    g_string_append_c(text, '\n');
}
#endif

static void append_result_error(GString *text, const char *label, JsonResult *error)
{
    if (!error) return;
    const char *code = yyjson_mut_get_str(yyjson_mut_obj_get(error, "code"));
    const char *hint = yyjson_mut_get_str(yyjson_mut_obj_get(error, "hint"));
    g_string_append_printf(text, "%s: %s", label, code ? code : "error");
    JsonResult *index = yyjson_mut_obj_get(error, "index");
    const char *tool = yyjson_mut_get_str(yyjson_mut_obj_get(error, "tool"));
    if (index) g_string_append_printf(text, " (step %" G_GINT64_FORMAT ", %s)", yyjson_mut_get_sint(index), tool ? tool : "unknown");
    if (hint) g_string_append_printf(text, ": %s", hint);
    g_string_append_c(text, '\n');
}

static char *tool_result_text(const Tool *tool, JsonResult *output)
{
    GString *text = g_string_new(NULL);
    if (!output) {
        append_result_error(text, "Error", error_payload());
    } else if (tool && !strcmp(tool->name, "status")) {
        g_string_append_printf(text, "Screen: %s\n", yyjson_mut_is_true(yyjson_mut_obj_get(output, "locked")) ? "locked" : "unlocked");
        JsonResult *canvas = yyjson_mut_obj_get(output, "canvas");
        g_string_append_printf(text, "Canvas: %" G_GINT64_FORMAT " x %" G_GINT64_FORMAT "\n",
            yyjson_mut_get_sint(yyjson_mut_obj_get(canvas, "width")), yyjson_mut_get_sint(yyjson_mut_obj_get(canvas, "height")));
        g_string_append(text, "Windows:\n");
        JsonResult *app, *window;
        size_t i, n, j, m;
        yyjson_mut_arr_foreach(yyjson_mut_obj_get(output, "apps"), i, n, app) {
            g_string_append(text, "  ");
            append_result_value(text, yyjson_mut_obj_get(app, "app_name"));
            g_string_append(text, ":\n");
            yyjson_mut_arr_foreach(yyjson_mut_obj_get(app, "windows"), j, m, window) {
                g_string_append_printf(text, "    window_id=%" G_GINT64_FORMAT " ", yyjson_mut_get_sint(yyjson_mut_obj_get(window, "window_id")));
                append_result_value(text, yyjson_mut_obj_get(window, "title"));
                if (yyjson_mut_is_true(yyjson_mut_obj_get(window, "active"))) g_string_append(text, " active");
                if (yyjson_mut_is_false(yyjson_mut_obj_get(window, "shown"))) g_string_append(text, " hidden");
                g_string_append(text, " bounds=");
                append_result_frame(text, yyjson_mut_obj_get(window, "bounds"));
                g_string_append_c(text, '\n');
            }
        }
    } else {
        const char *status = yyjson_mut_get_str(yyjson_mut_obj_get(output, "status"));
        g_string_append_printf(text, "Status: %s; completed: %" G_GINT64_FORMAT "\n", status ? status : "ok",
            yyjson_mut_get_sint(yyjson_mut_obj_get(output, "completed")));
        append_result_error(text, "Failed", yyjson_mut_obj_get(output, "failed"));
        JsonResult *warnings = yyjson_mut_obj_get(output, "warnings");
        JsonResult *warning;
        size_t warning_index, warning_count;
        yyjson_mut_arr_foreach(warnings, warning_index, warning_count, warning) {
            g_string_append_printf(text, "Warning (step %" G_GINT64_FORMAT ", %s): %s\n",
                yyjson_mut_get_sint(yyjson_mut_obj_get(warning, "index")),
                yyjson_mut_get_str(yyjson_mut_obj_get(warning, "tool")),
                yyjson_mut_get_str(yyjson_mut_obj_get(warning, "warning")));
        }
        JsonResult *observation;
        size_t i, n;
        yyjson_mut_arr_foreach(yyjson_mut_obj_get(output, "observations"), i, n, observation) {
            JsonResult *window = yyjson_mut_obj_get(observation, "window_id");
            if (window) g_string_append_printf(text, "\nWindow %" G_GINT64_FORMAT, yyjson_mut_get_sint(window));
            else g_string_append(text, "\nDesktop");
            if (!window) g_string_append_printf(text, "; %" G_GINT64_FORMAT " x %" G_GINT64_FORMAT,
                yyjson_mut_get_sint(yyjson_mut_obj_get(observation, "width")), yyjson_mut_get_sint(yyjson_mut_obj_get(observation, "height")));
            g_string_append_c(text, '\n');
            JsonResult *screenshot = yyjson_mut_obj_get(observation, "screenshot");
            if (screenshot) {
                g_string_append_printf(text, "Screenshot: %" G_GINT64_FORMAT " x %" G_GINT64_FORMAT "; bounds=",
                    yyjson_mut_get_sint(yyjson_mut_obj_get(screenshot, "width")), yyjson_mut_get_sint(yyjson_mut_obj_get(screenshot, "height")));
                append_result_frame(text, yyjson_mut_obj_get(screenshot, "bounds"));
                g_string_append_c(text, '\n');
            }
            append_result_error(text, "Error", yyjson_mut_obj_get(observation, "error"));
            append_result_error(text, "Accessibility", yyjson_mut_obj_get(observation, "accessibility_error"));
            append_result_error(text, "Screenshot", yyjson_mut_obj_get(observation, "screenshot_error"));
            if (yyjson_mut_is_true(yyjson_mut_obj_get(observation, "truncated"))) g_string_append(text, "Tree truncated; listed element IDs remain usable.\n");
            const char *tree = yyjson_mut_get_str(yyjson_mut_obj_get(observation, "tree_markdown"));
            if (tree) {
                g_string_append(text, yyjson_mut_is_false(yyjson_mut_obj_get(observation, "tree_full"))
                    ? "Tree (diff):\n" : "Tree (full):\n");
                g_string_append(text, tree);
            }
        }
    }
    return g_string_free(text, FALSE);
}

static void add_tool_result(JsonResult *result, const Tool *tool, JsonResult *output)
{
    JsonResult *content = result_array();
    JsonResult *text = result_object();
    char *body = tool_result_text(tool, output);
    if (output && tool && !strcmp(tool->name, "status") &&
        yyjson_mut_is_true(yyjson_mut_obj_get(output, "locked"))) {
        JsonResult *warning = result_object();
        result_string(warning, "type", "text");
        result_string(warning, "text", desktop_locked_warning);
        result_append(content, warning);
    }

    result_string(text, "type", "text");
    result_string(text, "text", body);
    result_append(content, text);
    free(body);

    size_t index, count;
    JsonResult *image;
    if (output != NULL) {
        yyjson_mut_arr_foreach(scu.images, index, count, image)
        {
            /* Copy container links. Hard errors return no partial images. */
            result_append(content, yyjson_mut_val_mut_copy(scu.doc, image));
        }
    }
    result_member(result, "content", content);
    gboolean batch_failed = output != NULL && tool != NULL &&
                            tool->batch != BATCH_NONE &&
                            g_strcmp0(yyjson_mut_get_str(yyjson_mut_obj_get(output, "status")), "failed") == 0;
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

    char *key =
#ifdef SCU_HOST
        host_request_key(id);
#else
        request_id_key(id);
#endif
    g_mutex_lock(&active_lock);
    active_id = g_strdup(key);
    g_atomic_int_set(&scu.cancelled, g_hash_table_contains(cancelled_ids, key));
    g_mutex_unlock(&active_lock);

    JsonResult *output = NULL;
    if (tool == NULL) {
        scu_fail("unknown_tool", "Tool is not implemented by scu.");
    } else if (validate_arguments(tool, arguments)) {
#ifdef SCU_HOST
        output = execute_tool(tool, arguments);
#else
        output = forward_tool(id, parameters);
#endif
    }
#ifdef SCU_HOST
    add_tool_result(result, tool, output);
#else
    if (output) {
        if (no_vision && !strcmp(tool->name, "status")) {
            JsonResult *filtered = result_array(), *block;
            size_t i, n;
            yyjson_mut_arr_foreach(yyjson_mut_obj_get(output, "content"), i, n, block) {
                if (g_strcmp0(yyjson_mut_get_str(yyjson_mut_obj_get(block, "type")), "image"))
                    result_append(filtered, yyjson_mut_val_mut_copy(scu.doc, block));
            }
            yyjson_mut_obj_remove_key(output, "content");
            result_member(output, "content", filtered);
        }
        size_t index, count;
        JsonResult *key, *value;
        yyjson_mut_obj_foreach(output, index, count, key, value) {
            result_member(result, yyjson_mut_get_str(key),
                yyjson_mut_val_mut_copy(scu.doc, value));
        }
    } else {
        add_tool_result(result, tool, NULL);
    }
#endif
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
    scu.vision_request = scu.lock_seen = scu.lock_monitor = FALSE;
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
    scu.vision_request = scu.lock_seen = scu.lock_monitor = FALSE;
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
    fputs(usage, stream);
}

static G_GNUC_UNUSED int serve_mcp(void)
{
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
        }
        poll_backend();
        while (g_main_context_iteration(NULL, FALSE)) {
        }
    }
    scu_invalidate();
    return 0;
}
