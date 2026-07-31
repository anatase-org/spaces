#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <glib/gstdio.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#define PORTAL_NAME "org.freedesktop.portal.Desktop"
#define PORTAL_PATH "/org/freedesktop/portal/desktop"
#define PORTAL_SECRET "org.freedesktop.portal.Secret"
#define PORTAL_REQUEST "org.freedesktop.portal.Request"
#define DBUS_NAME "org.freedesktop.DBus"
#define DBUS_PATH "/org/freedesktop/DBus"
#define SECRET_SIZE 64
#define KWALLET_KEY_SIZE 56
#define PORTAL_TIMEOUT_SECONDS 30
#define PROVIDER_TIMEOUT_SECONDS 15

extern char **environ;

#if defined(__x86_64__) || defined(__aarch64__)
typedef int (*main_function)(int, char **, char **);
extern int spaces_old_libc_start_main(
    main_function,
    int,
    char **,
    void (*)(void),
    void (*)(void),
    void (*)(void),
    void *
);
#if defined(__x86_64__)
__asm__(".symver spaces_old_libc_start_main,"
        "__libc_start_main@GLIBC_2.2.5");
#else
__asm__(".symver spaces_old_libc_start_main,"
        "__libc_start_main@GLIBC_2.17");
#endif
int __wrap___libc_start_main(
    main_function function,
    int argc,
    char **argv,
    void (*init)(void),
    void (*fini)(void),
    void (*rtld_fini)(void),
    void *stack_end
)
{
    return spaces_old_libc_start_main(
        function, argc, argv, init, fini, rtld_fini, stack_end
    );
}
#endif

typedef struct {
    gboolean received;
    guint response;
    char *token;
} PortalResponse;

static void secure_clear(void *data, gsize size)
{
    volatile unsigned char *cursor = data;

    while (size-- > 0)
        *cursor++ = 0;
}

static void truncate_secret_fd(int descriptor)
{
    int result;

    do {
        result = ftruncate(descriptor, 0);
    } while (result < 0 && errno == EINTR);
}

static gboolean write_all(int descriptor, const void *data, gsize size)
{
    const guint8 *cursor = data;

    while (size > 0) {
        ssize_t written = write(descriptor, cursor, size);

        if (written < 0 && errno == EINTR)
            continue;
        if (written <= 0)
            return FALSE;
        cursor += written;
        size -= (gsize)written;
    }
    return TRUE;
}

void spaces_prepare_headless_qt(void)
{
    /*
     * KWallet's storage daemon and KF6 compatibility proxy both construct a
     * QApplication even when started only to serve D-Bus. They must not try
     * to attach to the Space's forwarded display: a minimal guest may not
     * contain Qt's Wayland shell-integration plugins, and no interactive
     * prompt is valid for this automatically unlocked wallet.
     */
    (void)setenv("QT_QPA_PLATFORM", "offscreen", 1);
}

gboolean spaces_derive_kwallet_key(
    const guint8 portal_secret[SECRET_SIZE],
    guint8 output[KWALLET_KEY_SIZE]
)
{
    static const guint8 domain[] =
        "org.anatase.spaces.kwallet-unlock.v1";
    guint8 digest[SECRET_SIZE];
    gsize digest_size = sizeof(digest);
    GHmac *hmac;

    if (portal_secret == NULL || output == NULL)
        return FALSE;
    hmac = g_hmac_new(
        G_CHECKSUM_SHA512, portal_secret, SECRET_SIZE
    );
    if (hmac == NULL)
        return FALSE;
    g_hmac_update(hmac, domain, sizeof(domain));
    g_hmac_get_digest(hmac, digest, &digest_size);
    g_hmac_unref(hmac);
    if (digest_size != sizeof(digest)) {
        secure_clear(digest, sizeof(digest));
        return FALSE;
    }
    memcpy(output, digest, KWALLET_KEY_SIZE);
    secure_clear(digest, sizeof(digest));
    return TRUE;
}

static int create_memfd(void)
{
#ifdef SYS_memfd_create
    return (int)syscall(SYS_memfd_create, "spaces-wallet-secret", 1U);
#else
    errno = ENOSYS;
    return -1;
#endif
}

static char *token_path(void)
{
    const char *state = g_getenv("XDG_STATE_HOME");
    char *fallback = NULL;
    char *directory;
    char *path;

    if (state == NULL || *state == '\0') {
        const char *home = g_get_home_dir();

        if (home == NULL)
            return NULL;
        fallback = g_build_filename(home, ".local", "state", NULL);
        state = fallback;
    }
    directory = g_build_filename(state, "spaces", NULL);
    path = g_build_filename(directory, "secret-portal-token", NULL);
    g_free(directory);
    g_free(fallback);
    return path;
}

static char *load_token(void)
{
    char *path = token_path();
    char *token = NULL;
    gsize size = 0;

    if (path != NULL
        && g_file_get_contents(path, &token, &size, NULL)
        && (size == 0 || size > 4096)) {
        g_clear_pointer(&token, g_free);
    }
    g_free(path);
    return token;
}

static void save_token(const char *token)
{
    char *path;
    char *directory;
    char *separator;
    mode_t previous;

    if (token == NULL || *token == '\0')
        return;
    path = token_path();
    if (path == NULL)
        return;
    directory = g_strdup(path);
    separator = strrchr(directory, G_DIR_SEPARATOR);
    if (separator == NULL) {
        g_free(directory);
        g_free(path);
        return;
    }
    *separator = '\0';
    previous = umask(0077);
    if (g_mkdir_with_parents(directory, 0700) == 0
        && g_file_set_contents(path, token, -1, NULL))
        (void)g_chmod(path, 0600);
    umask(previous);
    g_free(directory);
    g_free(path);
}

static char *request_path(
    GDBusConnection *connection,
    const char *token
)
{
    const char *unique = g_dbus_connection_get_unique_name(connection);
    char *sender;
    char *cursor;
    char *path;

    if (unique == NULL)
        return NULL;
    sender = g_strdup(unique + (unique[0] == ':' ? 1 : 0));
    for (cursor = sender; *cursor != '\0'; cursor++) {
        if (*cursor == '.')
            *cursor = '_';
    }
    path = g_strdup_printf(
        PORTAL_PATH "/request/%s/%s", sender, token
    );
    g_free(sender);
    return path;
}

static void portal_response(
    GDBusConnection *connection,
    const char *sender,
    const char *object_path,
    const char *interface_name,
    const char *signal_name,
    GVariant *parameters,
    gpointer user_data
)
{
    PortalResponse *response = user_data;
    GVariant *results;
    const char *token = NULL;

    (void)connection;
    (void)sender;
    (void)object_path;
    (void)interface_name;
    (void)signal_name;
    g_variant_get(parameters, "(u@a{sv})", &response->response, &results);
    if (g_variant_lookup(results, "token", "&s", &token))
        response->token = g_strdup(token);
    g_variant_unref(results);
    response->received = TRUE;
}

static gboolean retrieve_portal_secret(
    GDBusConnection *bus,
    guint8 output[SECRET_SIZE]
)
{
    GVariantBuilder options;
    GUnixFDList *fd_list = NULL;
    GUnixFDList *reply_fds = NULL;
    GVariant *reply = NULL;
    GError *error = NULL;
    PortalResponse response = {0};
    char *handle_token = NULL;
    char *path = NULL;
    char *saved_token = NULL;
    guint subscription = 0;
    int descriptor = -1;
    gboolean success = FALSE;
    gint64 deadline;
    guint8 extra;

    descriptor = create_memfd();
    if (descriptor < 0)
        goto out;
    handle_token = g_uuid_string_random();
    for (char *cursor = handle_token; *cursor != '\0'; cursor++) {
        if (*cursor == '-')
            *cursor = '_';
    }
    path = request_path(bus, handle_token);
    if (path == NULL)
        goto out;
    subscription = g_dbus_connection_signal_subscribe(
        bus, PORTAL_NAME, PORTAL_REQUEST, "Response", path, NULL,
        G_DBUS_SIGNAL_FLAGS_NONE, portal_response, &response, NULL
    );
    saved_token = load_token();
    g_variant_builder_init(&options, G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add(
        &options, "{sv}", "handle_token",
        g_variant_new_string(handle_token)
    );
    if (saved_token != NULL)
        g_variant_builder_add(
            &options, "{sv}", "token",
            g_variant_new_string(saved_token)
        );
    fd_list = g_unix_fd_list_new();
    if (g_unix_fd_list_append(fd_list, descriptor, &error) < 0)
        goto out;
    reply = g_dbus_connection_call_with_unix_fd_list_sync(
        bus, PORTAL_NAME, PORTAL_PATH, PORTAL_SECRET, "RetrieveSecret",
        g_variant_new("(h@a{sv})", 0, g_variant_builder_end(&options)),
        G_VARIANT_TYPE("(o)"), G_DBUS_CALL_FLAGS_NONE,
        PORTAL_TIMEOUT_SECONDS * 1000, fd_list, &reply_fds, NULL, &error
    );
    if (reply == NULL)
        goto out;
    const char *returned_path;
    g_variant_get(reply, "(&o)", &returned_path);
    if (!g_str_equal(returned_path, path))
        goto out;

    deadline = g_get_monotonic_time()
        + PORTAL_TIMEOUT_SECONDS * G_TIME_SPAN_SECOND;
    while (!response.received && g_get_monotonic_time() < deadline) {
        while (g_main_context_iteration(NULL, FALSE))
            ;
        if (!response.received)
            g_usleep(10000);
    }
    if (!response.received) {
        g_dbus_connection_call(
            bus, PORTAL_NAME, path, PORTAL_REQUEST, "Close", NULL, NULL,
            G_DBUS_CALL_FLAGS_NONE, 3000, NULL, NULL, NULL
        );
        goto out;
    }
    if (response.response != 0)
        goto out;
    if (lseek(descriptor, 0, SEEK_SET) < 0)
        goto out;
    gsize size = 0;
    while (size < SECRET_SIZE) {
        ssize_t count = read(descriptor, output + size, SECRET_SIZE - size);

        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0)
            goto out;
        size += (gsize)count;
    }
    do {
        ssize_t count = read(descriptor, &extra, 1);

        if (count < 0 && errno == EINTR)
            continue;
        if (count != 0)
            goto out;
        break;
    } while (TRUE);
    save_token(response.token);
    success = TRUE;

out:
    if (!success)
        secure_clear(output, SECRET_SIZE);
    if (subscription != 0)
        g_dbus_connection_signal_unsubscribe(bus, subscription);
    if (descriptor >= 0) {
        truncate_secret_fd(descriptor);
        close(descriptor);
    }
    g_clear_error(&error);
    g_clear_pointer(&reply, g_variant_unref);
    g_clear_object(&reply_fds);
    g_clear_object(&fd_list);
    g_free(response.token);
    g_free(saved_token);
    g_free(path);
    g_free(handle_token);
    return success;
}

static gboolean name_has_owner(
    GDBusConnection *bus,
    const char *name
)
{
    GVariant *reply;
    gboolean owned = FALSE;

    reply = g_dbus_connection_call_sync(
        bus, DBUS_NAME, DBUS_PATH, DBUS_NAME, "NameHasOwner",
        g_variant_new("(s)", name), G_VARIANT_TYPE("(b)"),
        G_DBUS_CALL_FLAGS_NONE, 3000, NULL, NULL
    );
    if (reply != NULL) {
        g_variant_get(reply, "(b)", &owned);
        g_variant_unref(reply);
    }
    return owned;
}

static gboolean wait_for_owner(
    GDBusConnection *bus,
    const char *name,
    pid_t child
)
{
    gint64 deadline = g_get_monotonic_time()
        + PROVIDER_TIMEOUT_SECONDS * G_TIME_SPAN_SECOND;

    while (g_get_monotonic_time() < deadline) {
        int status;

        if (name_has_owner(bus, name))
            return TRUE;
        if (child > 0 && waitpid(child, &status, WNOHANG) == child)
            return FALSE;
        g_usleep(50000);
    }
    return FALSE;
}

static gboolean wallet_is_open(
    GDBusConnection *bus,
    gboolean kf6
)
{
    const char *name = kf6
        ? "org.kde.ksecretd" : "org.kde.kwalletd5";
    const char *path = kf6
        ? "/ksecretd" : "/modules/kwalletd5";
    GVariant *reply;
    gboolean open = FALSE;

    reply = g_dbus_connection_call_sync(
        bus, name, path, "org.kde.KWallet", "isOpen",
        g_variant_new("(s)", "spaces-managed-v1"),
        G_VARIANT_TYPE("(b)"), G_DBUS_CALL_FLAGS_NONE,
        3000, NULL, NULL
    );
    if (reply != NULL) {
        g_variant_get(reply, "(b)", &open);
        g_variant_unref(reply);
    }
    return open;
}

static gboolean send_environment(const char *path)
{
    int descriptor;
    struct sockaddr_un address;
    gboolean success = FALSE;

    descriptor = socket(AF_UNIX, SOCK_STREAM, 0);
    if (descriptor < 0)
        return FALSE;
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    if (strlen(path) >= sizeof(address.sun_path))
        goto out;
    strcpy(address.sun_path, path);
    if (connect(
            descriptor, (struct sockaddr *)&address, sizeof(address)
        ) < 0)
        goto out;
    for (char **entry = environ; *entry != NULL; entry++) {
        if (g_str_has_prefix(*entry, "QT_QPA_PLATFORM="))
            continue;
        if (strchr(*entry, '\n') != NULL
            || !write_all(descriptor, *entry, strlen(*entry))
            || !write_all(descriptor, "\n", 1))
            goto out;
    }
    if (!write_all(
            descriptor,
            "QT_QPA_PLATFORM=offscreen\n",
            strlen("QT_QPA_PLATFORM=offscreen\n")
        ))
        goto out;
    success = TRUE;

out:
    close(descriptor);
    return success;
}

static pid_t spawn_storage_provider(
    const char *program,
    const guint8 key[KWALLET_KEY_SIZE]
)
{
    const char *runtime = g_getenv("XDG_RUNTIME_DIR");
    int key_pipe[2] = {-1, -1};
    int listener = -1;
    struct sockaddr_un address;
    char *socket_path = NULL;
    pid_t child = -1;

    if (runtime == NULL || *runtime == '\0' || pipe(key_pipe) < 0)
        goto out;
    socket_path = g_build_filename(
        runtime, "spaces-kwallet-environment.socket", NULL
    );
    if (strlen(socket_path) >= sizeof(address.sun_path))
        goto out;
    listener = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listener < 0)
        goto out;
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    strcpy(address.sun_path, socket_path);
    (void)unlink(socket_path);
    if (bind(
            listener, (struct sockaddr *)&address, sizeof(address)
        ) < 0
        || listen(listener, 1) < 0)
        goto out;
    child = fork();
    if (child < 0)
        goto out;
    if (child == 0) {
        char key_fd[32];
        char environment_fd[32];
        char *arguments[5];

        close(key_pipe[1]);
        snprintf(key_fd, sizeof(key_fd), "%d", key_pipe[0]);
        snprintf(environment_fd, sizeof(environment_fd), "%d", listener);
        spaces_prepare_headless_qt();
        (void)setenv("PAM_KWALLET5_LOGIN", "1", 1);
        arguments[0] = (char *)program;
        arguments[1] = "--pam-login";
        arguments[2] = key_fd;
        arguments[3] = environment_fd;
        arguments[4] = NULL;
        execv(program, arguments);
        _exit(127);
    }
    close(key_pipe[0]);
    key_pipe[0] = -1;
    if (!write_all(key_pipe[1], key, KWALLET_KEY_SIZE)) {
        kill(child, SIGTERM);
        child = -1;
        goto out;
    }
    close(key_pipe[1]);
    key_pipe[1] = -1;
    if (!send_environment(socket_path)) {
        kill(child, SIGTERM);
        child = -1;
        goto out;
    }

out:
    if (key_pipe[0] >= 0)
        close(key_pipe[0]);
    if (key_pipe[1] >= 0)
        close(key_pipe[1]);
    if (listener >= 0)
        close(listener);
    if (socket_path != NULL)
        (void)unlink(socket_path);
    g_free(socket_path);
    return child;
}

static pid_t spawn_plain(const char *program)
{
    pid_t child = fork();

    if (child == 0) {
        spaces_prepare_headless_qt();
        execl(program, program, (char *)NULL);
        _exit(127);
    }
    return child;
}

static gboolean marker_exists(const char *path)
{
    struct stat metadata;
    int descriptor = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    gboolean matches;

    if (descriptor < 0)
        return FALSE;
    matches = syscall(SYS_fstat, descriptor, &metadata) == 0
        && S_ISREG(metadata.st_mode)
        && metadata.st_uid == getuid();
    close(descriptor);
    return matches;
}

static gboolean create_marker(const char *path)
{
    int descriptor = open(
        path, O_WRONLY | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600
    );

    if (descriptor < 0)
        return FALSE;
    close(descriptor);
    return TRUE;
}

static int activate_provider(
    GDBusConnection *bus,
    const char *activation_name,
    const char *marker
)
{
    gboolean kf6 = g_file_test(
        "/usr/bin/ksecretd", G_FILE_TEST_IS_EXECUTABLE
    );
    const char *storage_program = kf6
        ? "/usr/bin/ksecretd" : "/usr/bin/kwalletd5";
    const char *storage_name = "org.freedesktop.secrets";
    gboolean storage_owned = name_has_owner(bus, storage_name);
    guint8 portal_secret[SECRET_SIZE];
    guint8 key[KWALLET_KEY_SIZE];
    pid_t child = -1;
    int result = 1;

    memset(portal_secret, 0, sizeof(portal_secret));
    memset(key, 0, sizeof(key));
    if (!g_file_test(storage_program, G_FILE_TEST_IS_EXECUTABLE)) {
        g_printerr("spaces-secret-helper: no supported KWallet provider\n");
        goto out;
    }
    if (storage_owned && !marker_exists(marker)) {
        g_printerr(
            "spaces-secret-helper: refusing an unmanaged secret provider\n"
        );
        goto out;
    }
    if (storage_owned && !wallet_is_open(bus, kf6)) {
        g_printerr(
            "spaces-secret-helper: managed wallet is not open\n"
        );
        goto out;
    }
    if (!storage_owned) {
        if (!retrieve_portal_secret(bus, portal_secret)
            || !spaces_derive_kwallet_key(portal_secret, key)) {
            g_printerr(
                "spaces-secret-helper: portal secret is unavailable\n"
            );
            goto out;
        }
        child = spawn_storage_provider(storage_program, key);
        secure_clear(key, sizeof(key));
        secure_clear(portal_secret, sizeof(portal_secret));
        if (child <= 0
            || !wait_for_owner(bus, storage_name, child)
            || !wallet_is_open(bus, kf6)
            || !create_marker(marker)) {
            if (child > 0)
                kill(child, SIGTERM);
            g_printerr(
                "spaces-secret-helper: KWallet did not become ready\n"
            );
            goto out;
        }
    }
    if (g_str_equal(activation_name, "org.kde.kwalletd5")
        && kf6
        && !name_has_owner(bus, "org.kde.kwalletd5")) {
        pid_t proxy;

        if (!g_file_test(
                "/usr/bin/kwalletd6", G_FILE_TEST_IS_EXECUTABLE
            )) {
            g_printerr(
                "spaces-secret-helper: KWallet compatibility proxy is missing\n"
            );
            goto out;
        }
        proxy = spawn_plain("/usr/bin/kwalletd6");
        if (proxy <= 0
            || !wait_for_owner(bus, "org.kde.kwalletd5", proxy)) {
            if (proxy > 0)
                kill(proxy, SIGTERM);
            goto out;
        }
    }
    if (!name_has_owner(bus, activation_name)) {
        g_printerr(
            "spaces-secret-helper: provider did not claim %s\n",
            activation_name
        );
        goto out;
    }
    result = 0;

out:
    secure_clear(key, sizeof(key));
    secure_clear(portal_secret, sizeof(portal_secret));
    return result;
}

int main(int argc, char **argv)
{
    const char *runtime = g_getenv("XDG_RUNTIME_DIR");
    const char *activation_name = NULL;
    char *lock_path;
    char *marker_path;
    int lock_descriptor;
    GDBusConnection *bus;
    GError *error = NULL;
    int result;

    if (argc == 3 && g_str_equal(argv[1], "--activation-name"))
        activation_name = argv[2];
    if (activation_name == NULL
        || !(g_str_equal(activation_name, "org.freedesktop.secrets")
             || g_str_equal(
                 activation_name, "org.kde.secretservicecompat"
             )
             || g_str_equal(activation_name, "org.kde.kwalletd5"))) {
        g_printerr("spaces-secret-helper: invalid activation name\n");
        return 2;
    }
    if (runtime == NULL || *runtime == '\0') {
        g_printerr("spaces-secret-helper: XDG_RUNTIME_DIR is unavailable\n");
        return 1;
    }
    signal(SIGPIPE, SIG_IGN);
    lock_path = g_build_filename(
        runtime, "spaces-secret-helper.lock", NULL
    );
    marker_path = g_build_filename(
        runtime, "spaces-secret-helper.ready", NULL
    );
    lock_descriptor = open(
        lock_path, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600
    );
    g_free(lock_path);
    if (lock_descriptor < 0 || flock(lock_descriptor, LOCK_EX) < 0) {
        if (lock_descriptor >= 0)
            close(lock_descriptor);
        g_free(marker_path);
        return 1;
    }
    bus = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &error);
    if (bus == NULL) {
        g_printerr(
            "spaces-secret-helper: %s\n",
            error == NULL ? "session bus is unavailable" : error->message
        );
        g_clear_error(&error);
        close(lock_descriptor);
        g_free(marker_path);
        return 1;
    }
    result = activate_provider(bus, activation_name, marker_path);
    g_object_unref(bus);
    close(lock_descriptor);
    g_free(marker_path);
    return result;
}
