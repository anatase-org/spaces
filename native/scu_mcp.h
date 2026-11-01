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
#define MAX_CAPTURE_ID G_GUINT64_CONSTANT(9007199254740991)
#define MAX_IMAGE_DIMENSION 2048

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

static const char server_instructions[] =
    "Computer use MCP server. Control the host desktop: screenshots, mouse, "
    "keyboard, and accessibility. Use `status` to inspect the desktop, "
    "`vision` for pointer/keyboard actions and screenshots, and `accessibility` "
    "for element actions and window trees.";
static const char no_vision_instructions[] =
    "Computer use MCP server. Control the host desktop through accessibility. "
    "Start with status for canvas metadata and window IDs, "
    "then use accessibility for element actions and window trees. ",
    "This MCP has no vision support.";

static const char status_description[] =
    "Run first for health checks, canvas metadata, apps/windows and a full desktop screenshot. "
    "Window bounds, element frames and pixel actions share the full screenshot's coordinates. "
    "Screenshots unlock the host desktop when needed. ";
static const char no_vision_status_description[] =
    "Run first for health checks, canvas metadata, apps and windows. Initializes the desktop overview internally "
    "and may unlock the desktop. Returns no images.";

static const char vision_description[] =
    "Execute up to 64 JSON steps shaped {tool,args}, in order and stop "
    "on failure. Returns one final observation. "
    "Responses report status:ok/failed and the completed step count. "
    "Insert {tool:delay_ms,args:{ms:500}} to pause anywhere, the final "
    "observation waits for all steps including trailing delays (there are "
    "some default delays if not provided). "
    "Use observe:false to skip observation or observe:{kind:window,window_id} "
    "for a screenshot and an accessibility tree. "
    "Prefer the accessibility tree to locate controls when working with a "
    "single application, especially browsers. "
    "When the tree provides the controls you need, use "
    "accessibility with observations:[window_id] for subsequent trees "
    "to skip the screenshot. "
    "All pixel coordinates are global to the full overview, including after "
    "observe.zoom. "
    "Use zoom:[left,top,right,bottom] to read native detail on large desktops. "
    "Screenshots and foreground input unlock the host desktop when needed; "
    "tree-only observations leave it locked. ";
static const char observation_description[] =
    "Omit for a desktop image with no steps or after foreground input; false disables it. "
    "Use {kind:desktop,zoom:[left,top,right,bottom]} for an image or native-detail crop. "
    "Use {kind:window,window_id} for a compact accessibility tree and window screenshot. "
    "Add screenshot:false for only the tree. "
    "An explicit object replaces automatic observation. Zoom coordinates and all actions "
    "stay in the full overview's global pixel space; zoom never changes that frame.";
static const char accessibility_description[] =
    "Execute up to 64 JSON steps shaped {tool,args}, in order and stop "
    "on failure. Responses report status:ok/failed and the completed step count. "
    "Insert {tool:delay_ms,args:{ms:500}} to pause anywhere. "
    "Use observations:[window_id,...] for accessibility trees after all steps, "
    "including trailing delays. Omit observations or pass [] to skip tree reads. "
    "Returns observations without images. Element actions can target offscreen controls. "
    "Accessibility-only steps and tree-only observations leave the host desktop locked; "
    "keyboard editing unlocks it when needed. ";

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
    ARGS_ACTION = 1 << 0,
    ARGS_POINT = 1 << 1,
    ARGS_WINDOW = 1 << 2,
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
    {.name = "element_token", .type = ARG_STRING},
    {NULL},
};
static const Argument point_arguments[] = {
    {.name = "x", .type = ARG_NUMBER},
    {.name = "y", .type = ARG_NUMBER},
    {.name = "id", .type = ARG_INTEGER, .minimum = 1, .maximum = MAX_CAPTURE_ID},
    {NULL},
};
static const Argument window_arguments[] = {
    {.name = "window_id", .type = ARG_INTEGER},
    {.name = "screenshot", .type = ARG_BOOLEAN,
        .description = "Include a window screenshot (default true). False returns only the tree "
            "and requires a valid previous desktop overview."},
    {NULL},
};
static const Argument capture_arguments[] = {
    {.name = "zoom", .type = ARG_NUMBER_ARRAY, .minimum = 4, .maximum = 4,
        .description = "[left,top,right,bottom] in full-overview coordinates. Read native detail without changing the global coordinate frame or capture ID."},
    {NULL},
};
static const Argument click_arguments[] = {
    {.name = "button", .type = ARG_STRING, .choices = pointer_buttons},
    {.name = "count", .type = ARG_INTEGER, .minimum = 1, .maximum = 3},
    {NULL},
};
static const Argument drag_arguments[] = {
    {.name = "id", .type = ARG_INTEGER, .minimum = 1, .maximum = MAX_CAPTURE_ID},
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
    {.name = "observe", .type = ARG_OBJECT},
    {.name = "id", .type = ARG_INTEGER, .minimum = 1, .maximum = MAX_CAPTURE_ID,
        .description = "Optional full-overview capture used for pixel steps and zoom. Omission uses the latest overview."},
    {NULL},
};

static const Argument accessibility_arguments[] = {
    {.name = "steps", .type = ARG_OBJECT_ARRAY, .maximum = 64,
        .description = "Optional ordered {tool,args} actions."},
    {.name = "observations", .type = ARG_INTEGER_ARRAY, .maximum = 64,
        .description = "Window IDs to read after acting, once per distinct ID in input order. Omit or pass [] to skip tree reads."},
    {NULL},
};
static const char *const require_window[] = {"window_id", NULL};
static const char *const require_point[] = {"x", "y", NULL};
static const char *const require_element[] = {"element_token", NULL};
static const char *const require_drag[] = {"from_x", "from_y", "to_x", "to_y", NULL};
static const char *const require_scroll[] = {"x", "y", "direction", NULL};
static const char *const require_text[] = {"text", NULL};
static const char *const require_targeted_text[] = {"element_token", "text", NULL};
static const char *const require_value[] = {"element_token", "value", NULL};
static const char *const require_keys[] = {"keys", NULL};
static const char *const require_delay[] = {"ms", NULL};

/* Observation schemas are shared by validation and tools/list. */
static const Tool desktop_schema = {
    .name = "desktop",
    .groups = ARGS_NONE,
    .arguments = capture_arguments,
};

static const Tool window_schema = {
    .name = "window",
    .groups = ARGS_WINDOW,
    .required = require_window,
};

static const Tool *const observation_schemas[] = {&desktop_schema, &window_schema};

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
    if (tool->groups & ARGS_WINDOW) {
        add_argument_schemas(properties, window_arguments);
    }
    if (tool->batch != BATCH_NONE) {
        /* Advertise the actual nested contract, not an opaque array of objects.
         * Individual steps are validated again after defaults are merged. */
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

        if (tool->batch == BATCH_VISION) {
            JsonResult *observe = result_object(), *choices = result_array();
            JsonResult *off = result_object(), *off_values = result_array();
            result_string(off, "type", "boolean");
            result_append(off_values, yyjson_mut_false(scu.doc));
            result_member(off, "enum", off_values);
            result_append(choices, off);
            for (unsigned index = 0; index < G_N_ELEMENTS(observation_schemas); index++) {
                const Tool *observer = observation_schemas[index];
                yyjson_doc *input = build_tool_schema(observer);
                JsonResult *option = yyjson_val_mut_copy(scu.doc, yyjson_doc_get_root(input));
                JsonResult *kind = result_object(), *values = result_array();
                result_string(kind, "type", "string");
                result_append(values, yyjson_mut_str(scu.doc, observer->name));
                result_member(kind, "enum", values);
                result_member(yyjson_mut_obj_get(option, "properties"), "kind", kind);
                JsonResult *observe_required = result_array();
                result_append(observe_required, yyjson_mut_str(scu.doc, "kind"));
                if (observer->required) {
                    for (const char *const *field = observer->required; *field; field++)
                        result_append(observe_required, yyjson_mut_str(scu.doc, *field));
                }
                result_member(option, "required", observe_required);
                result_append(choices, option);
                yyjson_doc_free(input);
            }
            result_member(observe, "anyOf", choices);
            result_string(observe, "description", observation_description);
            result_member(properties, "observe", observe);
        }
    }
    if (tool->required != NULL) {
        JsonResult *ordered = result_object();
        JsonResult *capture = yyjson_mut_obj_get(properties, "id");
        if (capture) result_member(ordered, "id", yyjson_mut_val_mut_copy(scu.doc, capture));
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
    if (output != NULL) {
        yyjson_mut_arr_foreach(scu.images, index, count, image)
        {
            /* Copy container links. Hard errors return no partial images. */
            result_append(content, yyjson_mut_val_mut_copy(scu.doc, image));
        }
    }
    result_member(result, "content", content);
    result_member(result, "structuredContent", structured);
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
