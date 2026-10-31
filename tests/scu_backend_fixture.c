/* Include the production translation unit to test backend invariants without
 * exporting a second API or adding test-only hooks to the injected executable. */
#define main scu_program_main
#include "../native/scu.c"
#undef main

#include <assert.h>
#include <libeis.h>
#include <sys/socket.h>
#include <sys/un.h>

typedef struct {
    const char *mapping;
    int x, y;
    unsigned width, height;
} TestRegion;

typedef struct {
    TestRegion regions[2];
    unsigned count;
} TestDevice;

static bool fake_capability(
    struct ei_device *device, enum ei_device_capability capability)
{
    (void)device;
    return capability == EI_DEVICE_CAP_POINTER_ABSOLUTE;
}

static struct ei_region *fake_region(struct ei_device *pointer, size_t index)
{
    TestDevice *device = (TestDevice *)pointer;
    return index < device->count ? (struct ei_region *)&device->regions[index] : NULL;
}
static const char *fake_mapping(struct ei_region *region)
{
    return ((TestRegion *)region)->mapping;
}
static uint32_t fake_x(struct ei_region *region)
{
    return ((TestRegion *)region)->x;
}
static uint32_t fake_y(struct ei_region *region)
{
    return ((TestRegion *)region)->y;
}
static uint32_t fake_width(struct ei_region *region)
{
    return ((TestRegion *)region)->width;
}
static uint32_t fake_height(struct ei_region *region)
{
    return ((TestRegion *)region)->height;
}

static void test_mapping(void)
{
    TestDevice raw = {
        .regions = {{"left", -1920, -200, 1920, 1080}, {"right", 0, 0, 2560, 1440}},
        .count = 2};
    InputDevice device = {.device = (struct ei_device *)&raw, .resumed = TRUE};
    visual.devices = g_ptr_array_new();
    g_ptr_array_add(visual.devices, &device);
    visual.display_count = 2;
    ei_api.device_has_capability = fake_capability;
    ei_api.device_get_region = fake_region;
    ei_api.region_get_mapping_id = fake_mapping;
    ei_api.region_get_x = fake_x;
    ei_api.region_get_y = fake_y;
    ei_api.region_get_width = fake_width;
    ei_api.region_get_height = fake_height;
    Display display = {.mapping_id = "left",
        .capture_id = "capture",
        .revision = 1,
        .capture_revision = 1,
        .capture_width = 960,
        .capture_height = 540};
    yyjson_doc *arguments = yyjson_read("{}", 2, 0);
    struct ei_device *mapped;
    double x, y;
    assert(pointer_target(
        &display, yyjson_doc_get_root(arguments), 480, 270, &mapped, &x, &y));
    assert(mapped == (struct ei_device *)&raw && x == -960 && y == 340);
    display.mapping_id = "right";
    assert(pointer_target(
        &display, yyjson_doc_get_root(arguments), 480, 270, &mapped, &x, &y));
    assert(x == 1280 && y == 720);
    raw.regions[1].mapping = "left";
    display.mapping_id = "left";
    assert(!pointer_target(
        &display, yyjson_doc_get_root(arguments), 10, 10, &mapped, &x, &y));
    assert(strcmp(scu.code, "ambiguous_display_mapping") == 0);
    g_clear_pointer(&scu.code, g_free);
    display.capture_revision = 0;
    assert(!pointer_target(
        &display, yyjson_doc_get_root(arguments), 0, 0, &mapped, &x, &y));
    assert(strcmp(scu.code, "stale_capture") == 0);
    yyjson_doc_free(arguments);
    g_ptr_array_unref(visual.devices);
}

static struct pw_buffer fake_buffer;
static gboolean queued;
static struct pw_buffer *fake_dequeue(struct pw_stream *stream)
{
    (void)stream;
    return queued ? NULL : (queued = TRUE, &fake_buffer);
}
static int fake_queue(struct pw_stream *stream, struct pw_buffer *buffer)
{
    (void)stream;
    (void)buffer;
    return 0;
}

static void test_frame(void)
{
    unsigned char pixels[] = {0, 0, 255, 0, 0, 255, 0, 0};
    struct spa_chunk chunk = {.offset = 0, .size = sizeof(pixels), .stride = 8};
    struct spa_data plane = {
        .data = pixels, .maxsize = sizeof(pixels), .chunk = &chunk};
    struct spa_buffer raw = {.n_datas = 1, .datas = &plane};
    fake_buffer.buffer = &raw;
    pipewire.stream_dequeue_buffer = fake_dequeue;
    pipewire.stream_queue_buffer = fake_queue;
    Display display = {
        .width = 2, .height = 1, .format = {.format = SPA_VIDEO_FORMAT_BGRx}};
    stream_process(&display);
    assert(display.frame_sequence == 1 && display.frame_received > 0);
    assert(display.revision == 0); /* Frame receipt must not change geometry. */
    gsize size;
    const unsigned char *result = g_bytes_get_data(display.pixels, &size);
    const unsigned char expected[] = {255, 0, 0, 0, 255, 0};
    assert(size == sizeof(expected) && memcmp(result, expected, size) == 0);
    g_bytes_unref(display.pixels);
    display.pixels = NULL;
    chunk.size = 2;
    queued = FALSE;
    stream_process(&display);
    assert(display.pixels == NULL); /* Truncated buffers must never be read. */
}

static void test_png(void)
{
    assert(load_visual_libraries());
    static const unsigned char pixels[] = {255, 0, 0, 0, 255, 0};
    Display *display = &visual.displays[0];
    *display = (Display){.width = 2, .height = 1, .revision = 1};
    strcpy(display->id, "display-1");
    visual.display_count = 1;
    display->pixels = g_bytes_new_static(pixels, sizeof(pixels));
    const char *request = "{\"max_image_dimension\":1}";
    yyjson_doc *arguments = yyjson_read(request, strlen(request), 0);
    JsonResult *result = capture_desktop(display, yyjson_doc_get_root(arguments));
    assert(
        result != NULL && display->capture_width == 1 && display->capture_height == 1);
    JsonResult *image = yyjson_mut_arr_get(scu.images, 0);
    const char *base64 = yyjson_mut_get_str(yyjson_mut_obj_get(image, "data"));
    gsize size;
    guchar *png = g_base64_decode(base64, &size);
    assert(size > 24 && memcmp(png, "\x89PNG\r\n\x1a\n", 8) == 0);
    g_free(png);
    yyjson_doc_free(arguments);
}

static void test_tokens(void)
{
    save_restore_token("one-use-token");
    char *token = read_restore_token();
    assert(token != NULL && strcmp(token, "one-use-token") == 0);
    g_free(token);
    char *path = restore_token_path();
    struct stat metadata;
    assert(scu_lstat(path, &metadata) == 0 && (metadata.st_mode & 077) == 0);
    save_restore_token(NULL);
    assert(read_restore_token() == NULL);
    assert(symlink("/etc/passwd", path) == 0);
    assert(read_restore_token() == NULL);
    unlink(path);
    g_free(path);
}

/* A real EIS peer validates wire delivery and release events. It runs on its
 * own thread just like the compositor; production still uses only libei. */
typedef struct {
    struct eis *server;
    gint stop, keys_down, buttons_down, keys, clicks, scrolls;
    double x, y;
} InputOracle;

static gpointer serve_input(gpointer pointer)
{
    InputOracle *oracle = pointer;
    while (!g_atomic_int_get(&oracle->stop)) {
        eis_dispatch(oracle->server);
        struct eis_event *event;
        while ((event = eis_get_event(oracle->server)) != NULL) {
            enum eis_event_type type = eis_event_get_type(event);
            if (type == EIS_EVENT_CLIENT_CONNECT) {
                struct eis_client *client = eis_event_get_client(event);
                eis_client_connect(client);
                struct eis_seat *seat = eis_client_new_seat(client, "fixture");
                eis_seat_configure_capability(seat, EIS_DEVICE_CAP_POINTER_ABSOLUTE);
                eis_seat_configure_capability(seat, EIS_DEVICE_CAP_BUTTON);
                eis_seat_configure_capability(seat, EIS_DEVICE_CAP_SCROLL);
                eis_seat_configure_capability(seat, EIS_DEVICE_CAP_KEYBOARD);
                eis_seat_add(seat);
                eis_seat_unref(seat);
            } else if (type == EIS_EVENT_SEAT_BIND) {
                struct eis_seat *seat = eis_event_get_seat(event);
                struct eis_device *device = eis_seat_new_device(seat);
                eis_device_configure_name(device, "fixture input");
                eis_device_configure_capability(
                    device, EIS_DEVICE_CAP_POINTER_ABSOLUTE);
                eis_device_configure_capability(device, EIS_DEVICE_CAP_BUTTON);
                eis_device_configure_capability(device, EIS_DEVICE_CAP_SCROLL);
                eis_device_configure_capability(device, EIS_DEVICE_CAP_KEYBOARD);
                struct eis_region *region = eis_device_new_region(device);
                eis_region_set_size(region, 1920, 1080);
                eis_region_set_mapping_id(region, "fixture");
                eis_region_add(region);
                eis_region_unref(region);
                struct xkb_context *context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
                struct xkb_rule_names names = {.layout = "us"};
                struct xkb_keymap *keymap = xkb_keymap_new_from_names(
                    context, &names, XKB_KEYMAP_COMPILE_NO_FLAGS);
                char *contents =
                    xkb_keymap_get_as_string(keymap, XKB_KEYMAP_FORMAT_TEXT_V1);
                int keymap_fd = memfd_create("scu-test-keymap", MFD_CLOEXEC);
                size_t length = strlen(contents) + 1;
                assert(write(keymap_fd, contents, length) == (ssize_t)length);
                struct eis_keymap *wire = eis_device_new_keymap(
                    device, EIS_KEYMAP_TYPE_XKB, keymap_fd, length);
                eis_keymap_add(wire);
                eis_keymap_unref(wire);
                close(keymap_fd);
                free(contents);
                xkb_keymap_unref(keymap);
                xkb_context_unref(context);
                eis_device_add(device);
                eis_device_resume(device);
                eis_device_unref(device);
            } else if (type == EIS_EVENT_KEYBOARD_KEY) {
                gboolean down = eis_event_keyboard_get_key_is_press(event);
                g_atomic_int_add(&oracle->keys_down, down ? 1 : -1);
                if (down) {
                    g_atomic_int_inc(&oracle->keys);
                }
            } else if (type == EIS_EVENT_BUTTON_BUTTON) {
                gboolean down = eis_event_button_get_is_press(event);
                g_atomic_int_add(&oracle->buttons_down, down ? 1 : -1);
                if (down) {
                    g_atomic_int_inc(&oracle->clicks);
                }
            } else if (type == EIS_EVENT_POINTER_MOTION_ABSOLUTE) {
                oracle->x = eis_event_pointer_get_absolute_x(event);
                oracle->y = eis_event_pointer_get_absolute_y(event);
            } else if (type == EIS_EVENT_SCROLL_DISCRETE) {
                g_atomic_int_inc(&oracle->scrolls);
            }
            eis_event_unref(event);
        }
        g_usleep(1000);
    }
    return NULL;
}

static gpointer cancel_input(gpointer unused)
{
    (void)unused;
    g_usleep(70000);
    g_atomic_int_set(&scu.cancelled, 1);
    return NULL;
}

static JsonResult *batch(const char *json)
{
    yyjson_doc *request = yyjson_read(json, strlen(json), 0);
    JsonValue *args = yyjson_doc_get_root(request);
    const Tool *tool = find_tool("run_steps");
    assert(validate_arguments(tool, args));
    JsonResult *result = execute_tool(tool, args);
    assert(result != NULL);
    yyjson_doc_free(request);
    return result;
}

static gboolean receive_frame(gpointer pointer)
{
    Display *display = pointer;
    display->frame_sequence++;
    display->frame_received = g_get_monotonic_time();
    return G_SOURCE_REMOVE;
}

static gboolean resize_display(gpointer pointer)
{
    Display *display = pointer;
    /* Same geometry invalidation performed by stream_format_changed. */
    display->revision++;
    display->capture_revision = 0;
    receive_frame(pointer);
    return G_SOURCE_REMOVE;
}

static gboolean revoke_permission(gpointer unused)
{
    (void)unused;
    unlink(SCU_ATSPI_SOCKET);
    return G_SOURCE_REMOVE;
}

static void test_batch_observation(InputOracle *oracle)
{
    static const unsigned char pixels[] = {255, 0, 0, 0, 255, 0};
    for (unsigned index = 0; index < 2; index++) {
        Display *display = &visual.displays[index];
        *display = (Display){.width = 2, .height = 1, .revision = 1, .frame_sequence = 1};
        g_snprintf(display->id, sizeof(display->id), "display-%u", index + 1);
        display->pixels = g_bytes_new_static(pixels, sizeof(pixels));
        display->mapping_id = g_strdup(index ? "other" : "fixture");
    }
    visual.displays[1].width = 2000;
    g_bytes_unref(visual.displays[1].pixels);
    visual.displays[1].pixels = g_bytes_new_take(g_malloc0(6000), 6000);
    visual.display_count = 2;
    assert(selected_display("display-2") == &visual.displays[1]);
    assert(selected_display(NULL) == &visual.displays[1]);
    assert(selected_display("primary") == &visual.displays[0]);
    assert(selected_display("missing") == NULL);
    clear_tool_error();
    assert(selected_display(NULL) == &visual.displays[0]);

    /* A frame queued before delivery is drained, not called a new observation. */
    Display *display = selected_display("display-2");
    guint64 before_images = yyjson_mut_arr_size(scu.images);
    guint64 before_frames = display->frame_sequence;
    receive_frame(display);
    g_timeout_add(150, receive_frame, display);
    JsonResult *result = batch("{\"steps\":[{\"press_key\":{\"key\":\"a\"}}]}");
    assert(yyjson_mut_is_true(yyjson_mut_obj_get(result, "ok")));
    assert(yyjson_mut_obj_get(result, "steps") == NULL);
    assert(yyjson_mut_get_uint(yyjson_mut_obj_get(result, "completed")) == 1);
    JsonResult *observation = yyjson_mut_obj_get(result, "observation");
    assert(strcmp(yyjson_mut_get_str(yyjson_mut_obj_get(observation, "display")), "display-2") == 0);
    assert(strcmp(yyjson_mut_get_str(yyjson_mut_obj_get(observation, "frame_status")), "new") == 0);
    assert(yyjson_mut_get_uint(yyjson_mut_obj_get(observation, "screenshot_width")) == 1600);
    assert(display->frame_sequence > before_frames);
    assert(yyjson_mut_arr_size(scu.images) == before_images + 1);
    JsonResult *envelope = result_object();
    add_tool_result(envelope, find_tool("run_steps"), result);
    assert(yyjson_mut_is_false(yyjson_mut_obj_get(envelope, "isError")));
    JsonResult *content = yyjson_mut_obj_get(envelope, "content");
    assert(yyjson_mut_arr_size(content) == 2);
    assert(strcmp(yyjson_mut_get_str(yyjson_mut_obj_get(
        yyjson_mut_arr_get(content, 1), "type")), "image") == 0);

    result = batch("{\"steps\":[{\"press_key\":{\"key\":\"a\"}}],\"observe\":false}");
    assert(yyjson_mut_obj_get(result, "observation") == NULL);
    assert(yyjson_mut_arr_size(scu.images) == before_images + 1);
    const char *status = observation_frame(display);
    assert(status && strcmp(status, "cached") == 0); /* Static desktop, bounded wait. */

    /* An older libei still captures; it cannot prove input synchronization. */
    __typeof__(ei_api.new_ping) saved_ping = ei_api.new_ping;
    ei_api.new_ping = NULL;
    visual.input_sequence++;
    visual.last_input_time = g_get_monotonic_time();
    g_timeout_add(120, receive_frame, display);
    status = observation_frame(display);
    assert(status && strcmp(status, "unknown") == 0);
    ei_api.new_ping = saved_ping;

    result = batch("{\"steps\":[{\"press_key\":{\"key\":\"a\"}}],"
        "\"observe\":{\"kind\":\"desktop\",\"display_id\":\"primary\",\"max_image_dimension\":1}}");
    observation = yyjson_mut_obj_get(result, "observation");
    assert(yyjson_mut_get_uint(yyjson_mut_obj_get(observation, "screenshot_width")) == 1);
    assert(visual.selected_display == 0);

    /* Pixel defaults are shared within a batch; no intermediate capture replaces
     * their token. Explicit targets on other displays do not inherit that token. */
    char *json = g_strdup_printf("{\"target\":{\"kind\":\"desktop\",\"display_id\":\"primary\"},"
        "\"capture_id\":\"%s\",\"steps\":[{\"click\":{\"x\":0,\"y\":0}},"
        "{\"click\":{\"x\":0,\"y\":0}}],\"observe\":false}", visual.displays[0].capture_id);
    gint clicks = g_atomic_int_get(&oracle->clicks);
    result = batch(json);
    assert(yyjson_mut_is_true(yyjson_mut_obj_get(result, "ok")));
    assert(scu_pause(20));
    assert(g_atomic_int_get(&oracle->clicks) == clicks + 2);
    yyjson_doc *defaults = yyjson_read(json, strlen(json), 0);
    const char *override = "{\"target\":{\"kind\":\"desktop\",\"display_id\":\"display-2\"},\"x\":0,\"y\":0}";
    yyjson_doc *step = yyjson_read(override, strlen(override), 0);
    yyjson_doc *merged = merge_batch_defaults(yyjson_doc_get_root(step), yyjson_doc_get_root(defaults), FALSE);
    assert(json_member(yyjson_doc_get_root(merged), "capture_id") == NULL);
    yyjson_doc_free(merged);
    yyjson_doc_free(step);
    yyjson_doc_free(defaults);
    g_free(json);

    result = batch("{\"steps\":[{\"press_key\":{\"key\":\"a\"}},"
        "{\"press_key\":{\"key\":\"not-a-real-keysym\"}},"
        "{\"press_key\":{\"key\":\"b\"}}],\"observe\":false}");
    assert(yyjson_mut_is_false(yyjson_mut_obj_get(result, "ok")));
    assert(yyjson_mut_get_uint(yyjson_mut_obj_get(result, "completed")) == 1);
    assert(yyjson_mut_get_uint(yyjson_mut_obj_get(result, "skipped")) == 1);
    assert(yyjson_mut_get_uint(yyjson_mut_obj_get(yyjson_mut_obj_get(result, "failed"), "index")) == 1);

    /* Observation failure never erases a successful action. */
    result = batch("{\"steps\":[{\"press_key\":{\"key\":\"a\"}}],"
        "\"observe\":{\"kind\":\"desktop\",\"display_id\":\"missing\"}}");
    assert(yyjson_mut_is_false(yyjson_mut_obj_get(result, "ok")));
    assert(yyjson_mut_get_uint(yyjson_mut_obj_get(result, "completed")) == 1);
    assert(yyjson_mut_obj_get(result, "observation_error") != NULL);
    assert(yyjson_mut_obj_get(result, "failed") == NULL);

    display = &visual.displays[0];
    visual.input_sequence++;
    visual.last_input_time = g_get_monotonic_time();
    g_timeout_add(150, resize_display, display);
    status = observation_frame(display);
    assert(status && strcmp(status, "new") == 0 && display->capture_revision == 0);

    visual.input_sequence++;
    GThread *canceller = g_thread_new("cancel-observation", cancel_input, NULL);
    assert(observation_frame(display) == NULL);
    g_thread_join(canceller);
    g_atomic_int_set(&scu.cancelled, 0);
    assert(strcmp(scu.code, "cancelled") == 0);
    clear_tool_error();

    visual.input_sequence++;
    g_timeout_add(30, revoke_permission, NULL);
    assert(observation_frame(display) == NULL);
    assert(strcmp(scu.code, "accessibility_permission_required") == 0);
    assert(visual.session == NULL); /* Revocation also disposes captures/input. */
    clear_tool_error();
}

static void test_input(void)
{
    /* The gate checks this isolated endpoint; no host bus is contacted. */
    int socket_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    g_strlcpy(address.sun_path, SCU_ATSPI_SOCKET, sizeof(address.sun_path));
    assert(bind(socket_fd, (struct sockaddr *)&address, sizeof(address)) == 0);
    assert(load_visual_libraries());
    InputOracle oracle = {.server = eis_new(NULL)};
    assert(eis_setup_backend_fd(oracle.server) == 0);
    int fd = eis_backend_fd_add_client(oracle.server);
    assert(fd >= 0);
    GThread *thread = g_thread_new("eis-oracle", serve_input, &oracle);
    visual.session = g_strdup("/test/session");
    visual.devices = g_ptr_array_new_with_free_func(free_input_device);
    visual.input = ei_api.new_sender(NULL);
    assert(ei_api.setup_backend_fd(visual.input, fd) == 0);
    scu.deadline = g_get_monotonic_time() + 20 * G_USEC_PER_SEC;
    InputDevice *keyboard = wait_for_device(EI_DEVICE_CAP_KEYBOARD);
    assert(keyboard != NULL);
    guint codes[] = {KEY_LEFTCTRL, KEY_A};
    assert(press_codes(keyboard->device, codes, G_N_ELEMENTS(codes)));
    const char *chord =
        "{\"keys\":[\"exclam\",\"at\",\"numbersign\",\"dollar\",\"percent\"]}";
    yyjson_doc *chord_arguments = yyjson_read(chord, strlen(chord), 0);
    assert(keyboard_action("hotkey", yyjson_doc_get_root(chord_arguments)) != NULL);
    yyjson_doc_free(chord_arguments);
    Display display = {.mapping_id = "fixture",
        .capture_id = "one",
        .capture_width = 960,
        .capture_height = 540,
        .revision = 1,
        .capture_revision = 1};
    visual.display_count = 1;
    const char *request = "{\"x\":100,\"y\":200,\"direction\":\"down\",\"amount\":2}";
    yyjson_doc *arguments = yyjson_read(request, strlen(request), 0);
    assert(pointer_action("click", &display, yyjson_doc_get_root(arguments)) != NULL);
    assert(pointer_action("scroll", &display, yyjson_doc_get_root(arguments)) != NULL);
    yyjson_doc_free(arguments);
    request =
        "{\"from_x\":100,\"from_y\":200,\"to_x\":200,\"to_y\":250,\"duration_ms\":30}";
    arguments = yyjson_read(request, strlen(request), 0);
    assert(pointer_action("drag", &display, yyjson_doc_get_root(arguments)) != NULL);
    assert(scu_pause(40));
    assert(oracle.x == 400 && oracle.y == 500);
    yyjson_doc_free(arguments);
    request =
        "{\"from_x\":100,\"from_y\":200,\"to_x\":200,\"to_y\":250,\"duration_ms\":500}";
    arguments = yyjson_read(request, strlen(request), 0);
    GThread *canceller = g_thread_new("cancel-input", cancel_input, NULL);
    assert(pointer_action("drag", &display, yyjson_doc_get_root(arguments)) == NULL);
    g_thread_join(canceller);
    g_atomic_int_set(&scu.cancelled, 0);
    assert(scu_pause(40));
    assert(g_atomic_int_get(&oracle.keys_down) == 0);
    assert(g_atomic_int_get(&oracle.buttons_down) == 0);
    assert(g_atomic_int_get(&oracle.keys) == 8);
    assert(g_atomic_int_get(&oracle.clicks) == 3);
    assert(g_atomic_int_get(&oracle.scrolls) == 1);
    yyjson_doc_free(arguments);
    clear_tool_error();
    test_batch_observation(&oracle);
    scu_visual_close();
    g_atomic_int_set(&oracle.stop, 1);
    g_thread_join(thread);
    eis_unref(oracle.server);
    close(socket_fd);
    unlink(SCU_ATSPI_SOCKET);
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    scu.doc = yyjson_mut_doc_new(NULL);
    scu.images = result_array();
    if (strcmp(argv[1], "mapping") == 0) {
        test_mapping();
    } else if (strcmp(argv[1], "frame") == 0) {
        test_frame();
    } else if (strcmp(argv[1], "png") == 0) {
        test_png();
    } else if (strcmp(argv[1], "tokens") == 0) {
        test_tokens();
    } else if (strcmp(argv[1], "input") == 0) {
        test_input();
    } else {
        return 2;
    }
    return 0;
}
