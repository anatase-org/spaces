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
    scu.deadline = g_get_monotonic_time() + 5 * G_USEC_PER_SEC;
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
