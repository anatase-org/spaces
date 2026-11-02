/* Include the production translation unit to test backend invariants without
 * exporting a second API or adding test-only hooks to the injected executable. */
#define main scu_program_main
#include "../native/scu_host.c"
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
    return capability == EI_DEVICE_CAP_POINTER_ABSOLUTE || capability == EI_DEVICE_CAP_BUTTON;
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

static void reset_error(void)
{
    g_clear_pointer(&scu.code, g_free);
    g_clear_pointer(&scu.hint, g_free);
}

static void establish_canvas(guint width, guint height)
{
    CanvasLayout layout;
    char *key = NULL;
    assert(canvas_layout(&layout, &key));
    g_free(visual.canvas.key);
    visual.canvas = (CanvasSnapshot){.key = key,
        .width = width, .height = height, .layout = layout};
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
    visual.displays[0] = (Display){.mapping_id = "left", .width = 3840, .height = 2160,
        .has_geometry = TRUE, .logical_x = -1920, .logical_y = -200,
        .logical_width = 1920, .logical_height = 1080};
    visual.displays[1] = (Display){.mapping_id = "right", .width = 2560, .height = 1440,
        .has_geometry = TRUE, .logical_width = 2560, .logical_height = 1440};
    ei_api.device_has_capability = fake_capability;
    ei_api.device_get_region = fake_region;
    ei_api.region_get_mapping_id = fake_mapping;
    ei_api.region_get_x = fake_x;
    ei_api.region_get_y = fake_y;
    ei_api.region_get_width = fake_width;
    ei_api.region_get_height = fake_height;
    establish_canvas(1120, 410);
    struct ei_device *mapped;
    double x, y;
    assert(pointer_target(240, 135, FALSE, &mapped, &x, &y));
    assert(mapped == (struct ei_device *)&raw && x == -960 && y == 340);
    assert(pointer_target(800, 230, FALSE, &mapped, &x, &y));
    assert(x == 1280 && y == 720);
    assert(!pointer_target(1000, 5, FALSE, &mapped, &x, &y));
    assert(!strcmp(scu.code, "invalid_coordinates"));
    reset_error();
    assert(pointer_target(1000, 5, TRUE, &mapped, &x, &y));
    assert(mapped == NULL); /* Intermediate drag samples may cross a gap. */
    assert(!pointer_target(1120, 0, FALSE, &mapped, &x, &y));
    reset_error();
    visual.displays[0].has_geometry = visual.displays[1].has_geometry = FALSE;
    CanvasLayout unavailable; char *unused = NULL;
    assert(!canvas_layout(&unavailable, &unused));
    assert(!strcmp(scu.code, "canvas_layout_unavailable"));
    reset_error();
    visual.displays[0].has_geometry = visual.displays[1].has_geometry = TRUE;
    establish_canvas(1120, 410);
    raw.regions[1].mapping = "left";
    assert(!validate_canvas());
    assert(!strcmp(scu.code, "stale_capture"));
    reset_error();
    establish_canvas(1120, 410);
    assert(!pointer_target(240, 135, FALSE, &mapped, &x, &y));
    assert(!strcmp(scu.code, "ambiguous_display_mapping"));
    reset_error();
    visual.displays[0].revision++;
    assert(!validate_canvas());
    assert(!strcmp(scu.code, "stale_capture"));
    reset_error();
    /* Prefer a matching absolute pointer over an earlier relative/button device. */
    TestDevice other = {0};
    InputDevice first = {.device = (struct ei_device *)&other, .resumed = TRUE};
    g_ptr_array_insert(visual.devices, 0, &first);
    assert(seat_device((struct ei_device *)&raw, EI_DEVICE_CAP_BUTTON) == &device);
    g_ptr_array_unref(visual.devices);
    visual.devices = NULL;
}

/* QImage decoding is tested against independent literal pixels. */
static void test_images(void)
{
    scu.doc = yyjson_mut_doc_new(NULL);
    GError *error = NULL;
    const guchar rgb[] = {10,20,30, 40,50,60};
    GBytes *pixels = decode_kwin_pixels(rgb, sizeof(rgb), 2, 1, 6, 13, &error);
    assert(pixels && !error && g_bytes_equal(pixels, g_bytes_new_static(rgb, sizeof(rgb))));
    g_bytes_unref(pixels);
    assert(!decode_kwin_pixels(rgb, 5, 2, 1, 6, 13, &error));
    g_clear_error(&error);
    assert(!decode_kwin_pixels(rgb, 6, 2, 1, 6, 999, &error));
    g_clear_error(&error);
    const uint32_t argb[] = {0xff112233, 0x80402010};
    pixels = decode_kwin_pixels((const guchar *)argb, sizeof(argb), 2, 1, 8, 5, &error);
    const guchar expected[] = {0x11,0x22,0x33, 0x20,0x10,0x08};
    assert(pixels && !memcmp(g_bytes_get_data(pixels, NULL), expected, sizeof(expected)));
    g_bytes_unref(pixels);
    /* Native detail survives a tiny overview. Three independently patterned
     * streams occupy a mixed-scale layout with a black inter-output gap. */
    CanvasLayout layout = {.count=3};
    layout.tiles[0]=(CanvasRect){-4,-1,4,2};
    layout.tiles[1]=(CanvasRect){0,0,4,2};
    layout.tiles[2]=(CanvasRect){5,0,4,2};
    GBytes *frames[3];
    guchar original[8*4*3];
    for (guint i=0;i<sizeof(original);i++) original[i]=(i*37)%256;
    for (guint i=0;i<3;i++) {
        layout.native_widths[i]=8; layout.native_heights[i]=4;
        frames[i]=g_bytes_new_static(original,sizeof(original));
    }
    CanvasRect full={-4,-1,13,3}, crop={-4,-1,4,2};
    pixels=render_canvas(&layout,&full,13,3,frames);
    assert(pixels && g_bytes_get_size(pixels)==13*3*3);
    const guchar *data=g_bytes_get_data(pixels,NULL);
    assert(data[(1*13+8)*3]==0); /* gap at logical x=4 */
    g_bytes_unref(pixels);
    pixels=render_canvas(&layout,&crop,8,4,frames);
    assert(pixels && !memcmp(g_bytes_get_data(pixels,NULL),original,sizeof(original)));
    g_bytes_unref(pixels);
    for(guint i=0;i<3;i++)g_bytes_unref(frames[i]);
    yyjson_mut_doc_free(scu.doc);
}

typedef struct {
    struct eis *server;
    gint stop, keys_down, buttons_down, keys, clicks, scrolls, motions, dragged_motions;
    gint x, y, button;
    int control;
    GPtrArray *clients;
    GHashTable *bound_seats;
    struct eis_client *keys_client, *buttons_client;
    struct xkb_state *keyboard;
    GString *text, *action_times;
    gint numeric_value;
    gboolean selected;
} InputOracle;

static void add_pointer(struct eis_seat *seat, const char *mapping, gint x, gint y)
{
    struct eis_device *device = eis_seat_new_device(seat);
    eis_device_configure_name(device, mapping);
    eis_device_configure_capability(device, EIS_DEVICE_CAP_POINTER_ABSOLUTE);
    struct eis_region *region = eis_device_new_region(device);
    eis_region_set_offset(region, x, y);
    eis_region_set_size(region, 1920, 1080);
    eis_region_set_mapping_id(region, mapping);
    eis_region_add(region);
    eis_region_unref(region);
    eis_device_add(device);
    eis_device_unref(device);
}

static gpointer serve_input(gpointer pointer)
{
    InputOracle *oracle = pointer;
    while (!g_atomic_int_get(&oracle->stop)) {
        char command;
        ssize_t received = recv(oracle->control, &command, 1, MSG_DONTWAIT);
        if (received == 0) {
            break;
        }
        if (received > 0) {
            if(command=='C') {
                int fd=eis_backend_fd_add_client(oracle->server);
                char data='F'; struct iovec iov={&data,1};
                char control[CMSG_SPACE(sizeof(int))]={0};
                struct msghdr message={.msg_iov=&iov,.msg_iovlen=1,.msg_control=control,.msg_controllen=sizeof(control)};
                struct cmsghdr *c=CMSG_FIRSTHDR(&message);
                c->cmsg_level=SOL_SOCKET;c->cmsg_type=SCM_RIGHTS;c->cmsg_len=CMSG_LEN(sizeof(int));
                memcpy(CMSG_DATA(c),&fd,sizeof(fd));
                assert(sendmsg(oracle->control,&message,0)==1);close(fd);
            } else if(command=='S') {
                yyjson_mut_doc *document=yyjson_mut_doc_new(NULL);
                yyjson_mut_val *encoded=yyjson_mut_str(document,oracle->text->str);
                char *text=yyjson_mut_val_write(encoded,0,NULL);
                char *json=g_strdup_printf("{\"keys_down\":%d,\"buttons_down\":%d,\"keys\":%d,\"clicks\":%d,\"scrolls\":%d,\"motions\":%d,\"dragged_motions\":%d,\"x\":%d,\"y\":%d,\"button\":%d,\"text\":%s,\"selected\":%s,\"numeric_value\":%d,\"action_times\":[%s]}",
                    oracle->keys_down,oracle->buttons_down,oracle->keys,oracle->clicks,oracle->scrolls,
                    oracle->motions,oracle->dragged_motions,oracle->x,oracle->y,oracle->button,text,oracle->selected?"true":"false",oracle->numeric_value,oracle->action_times->str);
                free(text);yyjson_mut_doc_free(document);
                assert(send(oracle->control,json,strlen(json),0)>0);g_free(json);
            } else if(command=='D') {
                for(guint i=0;i<oracle->clients->len;i++)eis_client_disconnect(g_ptr_array_index(oracle->clients,i));
                g_ptr_array_set_size(oracle->clients,0);
            } else if(command=='Q') {
                break;
            }
        }
        eis_dispatch(oracle->server);
        struct eis_event *event;
        while ((event = eis_get_event(oracle->server)) != NULL) {
            enum eis_event_type type = eis_event_get_type(event);
            if (type == EIS_EVENT_CLIENT_CONNECT) {
                struct eis_client *client = eis_event_get_client(event);
                eis_client_connect(client);
                g_ptr_array_add(oracle->clients,eis_client_ref(client));
                struct eis_seat *seat = eis_client_new_seat(client, "fixture");
                eis_seat_configure_capability(seat, EIS_DEVICE_CAP_POINTER_ABSOLUTE);
                eis_seat_configure_capability(seat, EIS_DEVICE_CAP_BUTTON);
                eis_seat_configure_capability(seat, EIS_DEVICE_CAP_SCROLL);
                eis_seat_configure_capability(seat, EIS_DEVICE_CAP_KEYBOARD);
                eis_seat_add(seat);
                eis_seat_unref(seat);
                seat = eis_client_new_seat(client, "isolated");
                eis_seat_configure_capability(seat, EIS_DEVICE_CAP_POINTER_ABSOLUTE);
                eis_seat_add(seat);
                eis_seat_unref(seat);
            } else if (type == EIS_EVENT_CLIENT_DISCONNECT) {
                struct eis_client *client = eis_event_get_client(event);
                /* The compositor neutralizes a disconnected context even if
                 * teardown overtakes its queued release events. Keep the
                 * oracle's logical state consistent with that lifecycle. */
                if (oracle->keys_client == client) {
                    oracle->keys_down = 0;
                    oracle->keys_client = NULL;
                }
                if (oracle->buttons_client == client) {
                    oracle->buttons_down = 0;
                    oracle->buttons_client = NULL;
                }
                g_hash_table_remove_all(oracle->bound_seats);
                g_ptr_array_remove_fast(oracle->clients, client);
            } else if (type == EIS_EVENT_SEAT_BIND) {
                struct eis_seat *seat = eis_event_get_seat(event);
                if(g_hash_table_contains(oracle->bound_seats,seat)) {
                    eis_event_unref(event);continue;
                }
                if(strcmp(eis_seat_get_name(seat),"isolated") &&
                    (!eis_event_seat_has_capability(event,EIS_DEVICE_CAP_KEYBOARD) ||
                     !eis_event_seat_has_capability(event,EIS_DEVICE_CAP_BUTTON) ||
                     !eis_event_seat_has_capability(event,EIS_DEVICE_CAP_SCROLL))) {
                    eis_event_unref(event);continue;
                }
                g_hash_table_add(oracle->bound_seats,seat);
                if (!strcmp(eis_seat_get_name(seat), "isolated")) {
                    add_pointer(seat, "isolated", 8000, 0);
                    eis_event_unref(event);
                    continue;
                }
                struct eis_device *device = eis_seat_new_device(seat);
                eis_device_configure_name(device, "fixture input");
                eis_device_configure_capability(
                    device, EIS_DEVICE_CAP_POINTER_ABSOLUTE);
                eis_device_configure_capability(device, EIS_DEVICE_CAP_BUTTON);
                eis_device_configure_capability(device, EIS_DEVICE_CAP_SCROLL);
                eis_device_configure_capability(device, EIS_DEVICE_CAP_KEYBOARD);
                struct eis_region *region = eis_device_new_region(device);
                eis_region_set_size(region, 1920, 1080);
                eis_region_set_mapping_id(region, "center");
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
                            eis_device_unref(device);
                add_pointer(seat, "left", -1920, -200);
                add_pointer(seat, "right", 2080, 0);
            } else if (type == EIS_EVENT_DEVICE_READY) {
                eis_device_resume(eis_event_get_device(event));
            } else if (type == EIS_EVENT_KEYBOARD_KEY) {
                gboolean down = eis_event_keyboard_get_key_is_press(event);
                xkb_keycode_t code=eis_event_keyboard_get_key(event)+8;
                xkb_keysym_t symbol=xkb_state_key_get_one_sym(oracle->keyboard,code);
                if (down && symbol == XKB_KEY_Left) oracle->numeric_value = MAX(0, oracle->numeric_value - 5);
                if (down && symbol == XKB_KEY_Right) oracle->numeric_value = MIN(100, oracle->numeric_value + 5);
                gboolean control=xkb_state_mod_name_is_active(oracle->keyboard,XKB_MOD_NAME_CTRL,XKB_STATE_MODS_EFFECTIVE)>0;
                if(down && control && symbol==XKB_KEY_a)oracle->selected=TRUE;
                else if(down && !control) {
                    char text[8]={0};
                    int length=xkb_state_key_get_utf8(oracle->keyboard,code,text,sizeof(text));
                    if(symbol==XKB_KEY_BackSpace || (length>0 && (guchar)text[0]>=32)) {
                        if(oracle->selected)g_string_truncate(oracle->text,0);
                        else if(symbol==XKB_KEY_BackSpace && oracle->text->len)
                            g_string_truncate(oracle->text,g_utf8_prev_char(oracle->text->str+oracle->text->len)-oracle->text->str);
                        if(symbol!=XKB_KEY_BackSpace)g_string_append(oracle->text,text);
                        oracle->selected=FALSE;
                    }
                }
                xkb_state_update_key(oracle->keyboard,code,down?XKB_KEY_DOWN:XKB_KEY_UP);
                g_atomic_int_add(&oracle->keys_down, down ? 1 : -1);
                if (down) {
                    oracle->keys_client = eis_device_get_client(eis_event_get_device(event));
                    g_atomic_int_inc(&oracle->keys);
                }
            } else if (type == EIS_EVENT_BUTTON_BUTTON) {
                gboolean down = eis_event_button_get_is_press(event);
                g_atomic_int_set(&oracle->button, eis_event_button_get_button(event));
                g_atomic_int_add(&oracle->buttons_down, down ? 1 : -1);
                if (down) {
                    oracle->buttons_client = eis_device_get_client(eis_event_get_device(event));
                    g_atomic_int_inc(&oracle->clicks);
                    if (oracle->action_times->len) g_string_append_c(oracle->action_times, ',');
                    g_string_append_printf(oracle->action_times, "%.6f", g_get_monotonic_time() / 1000000.0);
                }
            } else if (type == EIS_EVENT_POINTER_MOTION_ABSOLUTE) {
                g_atomic_int_set(&oracle->x, (gint)eis_event_pointer_get_absolute_x(event));
                g_atomic_int_set(&oracle->y, (gint)eis_event_pointer_get_absolute_y(event));
                g_atomic_int_inc(&oracle->motions);
                if (g_atomic_int_get(&oracle->buttons_down)) {
                    g_atomic_int_inc(&oracle->dragged_motions);
                }
            } else if (type == EIS_EVENT_SCROLL_DISCRETE) {
                g_atomic_int_inc(&oracle->scrolls);
            }
            eis_event_unref(event);
        }
        g_usleep(1000);
    }
    return NULL;
}


static void free_oracle_client(gpointer pointer)
{
    eis_client_unref(pointer);
}

static int run_eis_server(int fd)
{
    InputOracle oracle={.server=eis_new(NULL),.control=fd};
    struct xkb_context *context=xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    struct xkb_rule_names names={.layout="us"};
    struct xkb_keymap *map=xkb_keymap_new_from_names(context,&names,XKB_KEYMAP_COMPILE_NO_FLAGS);
    oracle.keyboard=xkb_state_new(map);
    oracle.text=g_string_new("");
    oracle.action_times=g_string_new("");
    oracle.numeric_value=80;
    oracle.clients=g_ptr_array_new_with_free_func(free_oracle_client);
    oracle.bound_seats=g_hash_table_new(g_direct_hash,g_direct_equal);
    eis_set_flag(oracle.server,EIS_FLAG_DEVICE_READY);
    assert(eis_setup_backend_fd(oracle.server)==0);
    serve_input(&oracle);
    for(guint i=0;i<oracle.clients->len;i++)eis_client_disconnect(g_ptr_array_index(oracle.clients,i));
    g_ptr_array_unref(oracle.clients);
    g_hash_table_unref(oracle.bound_seats);
    eis_unref(oracle.server);
    g_string_free(oracle.text,TRUE);
    g_string_free(oracle.action_times,TRUE);
    xkb_state_unref(oracle.keyboard);
    xkb_keymap_unref(map);
    xkb_context_unref(context);
    return 0;
}
int main(int argc,char **argv)
{
    if(argc==3&&!strcmp(argv[1],"eis-server"))return run_eis_server(atoi(argv[2]));
    assert(argc==2);
    if(!strcmp(argv[1],"mapping"))test_mapping();
    else if(!strcmp(argv[1],"images"))test_images();
    else return 2;
    return 0;
}
