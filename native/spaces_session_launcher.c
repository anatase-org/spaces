#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <pwd.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef DBUS_UPDATE_ACTIVATION_ENVIRONMENT
#define DBUS_UPDATE_ACTIVATION_ENVIRONMENT \
    "/usr/bin/dbus-update-activation-environment"
#endif

#ifndef SYSTEMCTL
#define SYSTEMCTL "/usr/bin/systemctl"
#endif

#ifndef AGENT_STATE_ROOT
#define AGENT_STATE_ROOT "/run/user"
#endif

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
    main_function main_function_pointer,
    int argc,
    char **argv,
    void (*init)(void),
    void (*fini)(void),
    void (*rtld_fini)(void),
    void *stack_end
)
{
    return spaces_old_libc_start_main(
        main_function_pointer,
        argc,
        argv,
        init,
        fini,
        rtld_fini,
        stack_end
    );
}
#endif

static volatile sig_atomic_t forwarded_child = -1;
static volatile sig_atomic_t termination_requested = 0;

typedef struct {
    pid_t pid;
    unsigned long long start_time;
    int state_descriptor;
    bool held;
    bool starting;
} AgentReference;

typedef struct {
    pid_t pid;
    unsigned long long start_time;
    unsigned int references;
} AgentState;

static void forward_term(int signum)
{
    pid_t child = (pid_t)forwarded_child;

    if (child > 0) {
        (void)kill(child, signum);
    } else {
        termination_requested = signum;
    }
}

static int configure_supervisor_signals(void)
{
    struct sigaction action;
    int ignored[] = {SIGHUP, SIGINT, SIGQUIT};
    size_t index;

    memset(&action, 0, sizeof(action));
    action.sa_handler = forward_term;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGTERM, &action, NULL) < 0)
        return -1;

    action.sa_handler = SIG_IGN;
    for (index = 0; index < sizeof(ignored) / sizeof(ignored[0]); index++) {
        if (sigaction(ignored[index], &action, NULL) < 0)
            return -1;
    }
    return 0;
}

static int status_code(int status)
{
    if (WIFEXITED(status))
        return WEXITSTATUS(status);
    if (WIFSIGNALED(status))
        return 128 + WTERMSIG(status);
    return 1;
}

static void stop_agent(pid_t agent)
{
    int attempts;
    int status;

    if (agent <= 0)
        return;
    (void)kill(-agent, SIGTERM);
    for (attempts = 0; attempts < 20; attempts++) {
        pid_t result = waitpid(agent, &status, WNOHANG);

        if (result == agent || (result < 0 && errno == ECHILD))
            return;
        usleep(100000);
    }
    (void)kill(-agent, SIGKILL);
    while (waitpid(agent, &status, 0) < 0 && errno == EINTR)
        ;
}

static void terminate_agent(pid_t agent)
{
    int attempts;

    if (agent <= 0)
        return;
    (void)kill(-agent, SIGTERM);
    for (attempts = 0; attempts < 20; attempts++) {
        if (kill(-agent, 0) < 0 && errno == ESRCH)
            return;
        usleep(100000);
    }
    (void)kill(-agent, SIGKILL);
}

static bool parse_unsigned(
    const char *begin,
    const char *end,
    unsigned long long maximum,
    unsigned long long *result
)
{
    unsigned long long value = 0;
    const char *cursor;

    if (begin == end)
        return false;
    for (cursor = begin; cursor < end; cursor++) {
        unsigned int digit;

        if (*cursor < '0' || *cursor > '9')
            return false;
        digit = (unsigned int)(*cursor - '0');
        if (value > (maximum - digit) / 10)
            return false;
        value = value * 10 + digit;
    }
    *result = value;
    return true;
}

static bool process_start_time(
    pid_t process,
    unsigned long long *start_time
)
{
    char buffer[2048];
    char path[64];
    char *cursor;
    char *end;
    ssize_t size;
    int descriptor;
    int field;

    if (process <= 1)
        return false;
    if (snprintf(
            path, sizeof(path), "/proc/%ld/stat", (long)process
        ) >= (int)sizeof(path))
        return false;
    descriptor = open(path, O_RDONLY | O_CLOEXEC);
    if (descriptor < 0)
        return false;
    do {
        size = read(descriptor, buffer, sizeof(buffer) - 1);
    } while (size < 0 && errno == EINTR);
    close(descriptor);
    if (size <= 0)
        return false;
    buffer[size] = '\0';
    cursor = strrchr(buffer, ')');
    if (cursor == NULL)
        return false;
    cursor++;
    for (field = 3; field <= 22; field++) {
        while (*cursor == ' ')
            cursor++;
        if (*cursor == '\0')
            return false;
        end = cursor;
        while (*end != '\0' && *end != ' ')
            end++;
        if (field == 22) {
            unsigned long long value;

            if (!parse_unsigned(cursor, end, ULLONG_MAX, &value))
                return false;
            *start_time = value;
            return true;
        }
        cursor = end;
    }
    return false;
}

static void terminate_adopted_groups(int signum)
{
    char buffer[4096];
    char path[96];
    char *cursor;
    ssize_t size;
    pid_t own_group = getpgrp();
    pid_t groups[32];
    size_t group_count = 0;
    int descriptor;

    if (snprintf(
            path,
            sizeof(path),
            "/proc/self/task/%ld/children",
            (long)getpid()
        ) >= (int)sizeof(path))
        return;
    descriptor = open(path, O_RDONLY | O_CLOEXEC);
    if (descriptor < 0)
        return;
    do {
        size = read(descriptor, buffer, sizeof(buffer) - 1);
    } while (size < 0 && errno == EINTR);
    close(descriptor);
    if (size <= 0)
        return;
    buffer[size] = '\0';
    cursor = buffer;
    while (*cursor != '\0' && group_count < 32) {
        unsigned long long process_value;
        char *end;
        pid_t group;
        size_t index;
        bool known = false;

        while (*cursor == ' ')
            cursor++;
        if (*cursor == '\0')
            break;
        end = cursor;
        while (*end != '\0' && *end != ' ')
            end++;
        if (!parse_unsigned(
                cursor, end, (unsigned long long)INT_MAX, &process_value
            ))
            break;
        group = getpgid((pid_t)process_value);
        if (group > 1 && group != own_group) {
            for (index = 0; index < group_count; index++) {
                if (groups[index] == group) {
                    known = true;
                    break;
                }
            }
            if (!known) {
                groups[group_count++] = group;
                (void)kill(-group, signum);
            }
        }
        cursor = end;
    }
}

static int lock_agent_state(void)
{
    char path[256];
    char fallback_session[32];
    const char *session = getenv("XDG_SESSION_ID");
    const char *cursor;
    size_t session_length;
    int descriptor;

    if (session == NULL)
        session = "";
    session_length = strlen(session);
    for (cursor = session; *cursor != '\0'; cursor++) {
        if (!((*cursor >= 'a' && *cursor <= 'z')
              || (*cursor >= 'A' && *cursor <= 'Z')
              || (*cursor >= '0' && *cursor <= '9')
              || *cursor == '_' || *cursor == '-')) {
            session_length = 0;
            break;
        }
    }
    if (session_length == 0 || session_length > 64) {
        if (snprintf(
                fallback_session,
                sizeof(fallback_session),
                "p%ld",
                (long)getsid(0)
            ) >= (int)sizeof(fallback_session))
            return -1;
        session = fallback_session;
    }
    if (snprintf(
            path,
            sizeof(path),
            "%s/%lu/spaces-polkit-agent.%s.state",
            AGENT_STATE_ROOT,
            (unsigned long)getuid(),
            session
        ) >= (int)sizeof(path))
        return -1;
    descriptor = open(
        path,
        O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW,
        0600
    );
    if (descriptor < 0)
        return -1;
    while (flock(descriptor, LOCK_EX) < 0) {
        if (errno != EINTR) {
            close(descriptor);
            return -1;
        }
    }
    return descriptor;
}

static AgentState read_agent_state(int descriptor)
{
    AgentState state = {.pid = -1, .start_time = 0, .references = 0};
    char buffer[128];
    char *cursor;
    char *end;
    unsigned long long process;
    unsigned long long references;
    ssize_t size;
    int field;

    do {
        size = pread(descriptor, buffer, sizeof(buffer) - 1, 0);
    } while (size < 0 && errno == EINTR);
    if (size <= 0)
        return state;
    buffer[size] = '\0';
    cursor = buffer;
    for (field = 0; field < 3; field++) {
        unsigned long long value;
        unsigned long long maximum = field == 0
            ? (unsigned long long)INT_MAX
            : (field == 1 ? ULLONG_MAX : (unsigned long long)UINT_MAX);

        while (*cursor == ' ')
            cursor++;
        end = cursor;
        while (*end != '\0' && *end != ' ' && *end != '\n')
            end++;
        if (!parse_unsigned(cursor, end, maximum, &value))
            return state;
        if (field == 0)
            process = value;
        else if (field == 1)
            state.start_time = value;
        else
            references = value;
        cursor = end;
    }
    state.references = (unsigned int)references;
    state.pid = (pid_t)process;
    return state;
}

static bool write_agent_state(int descriptor, AgentState state)
{
    char buffer[128];
    int length;
    ssize_t size;

    length = snprintf(
        buffer,
        sizeof(buffer),
        "%ld %llu %u\n",
        (long)state.pid,
        state.start_time,
        state.references
    );
    if (length <= 0 || length >= (int)sizeof(buffer))
        return false;
    if (ftruncate(descriptor, 0) < 0)
        return false;
    do {
        size = pwrite(descriptor, buffer, (size_t)length, 0);
    } while (size < 0 && errno == EINTR);
    return size == length;
}

static bool agent_state_is_live(AgentState state)
{
    unsigned long long start_time;

    return state.pid > 1
        && state.references > 0
        && getpgid(state.pid) == state.pid
        && process_start_time(state.pid, &start_time)
        && start_time == state.start_time
        && (kill(-state.pid, 0) == 0 || errno == EPERM);
}

static char *read_manager_environment(void)
{
    enum {
        INITIAL_CAPACITY = 4096,
        MAXIMUM_CAPACITY = 1024 * 1024,
        POLL_ATTEMPTS = 100
    };
    char *output;
    size_t capacity = INITIAL_CAPACITY;
    size_t length = 0;
    int output_pipe[2];
    int flags;
    int status = 0;
    int attempt;
    bool child_done = false;
    bool child_status_valid = false;
    bool end_of_file = false;
    pid_t child;

    if (pipe2(output_pipe, O_CLOEXEC) < 0)
        return NULL;
    child = fork();
    if (child < 0) {
        close(output_pipe[0]);
        close(output_pipe[1]);
        return NULL;
    }
    if (child == 0) {
        int null_fd = open("/dev/null", O_RDWR | O_CLOEXEC);

        close(output_pipe[0]);
        if (null_fd < 0
            || dup2(null_fd, STDIN_FILENO) < 0
            || dup2(output_pipe[1], STDOUT_FILENO) < 0
            || dup2(null_fd, STDERR_FILENO) < 0)
            _exit(127);
        if (null_fd > STDERR_FILENO)
            close(null_fd);
        if (output_pipe[1] > STDERR_FILENO)
            close(output_pipe[1]);
        execl(
            SYSTEMCTL,
            SYSTEMCTL,
            "--user",
            "show-environment",
            (char *)NULL
        );
        _exit(127);
    }
    close(output_pipe[1]);
    flags = fcntl(output_pipe[0], F_GETFL);
    if (flags < 0
        || fcntl(output_pipe[0], F_SETFL, flags | O_NONBLOCK) < 0) {
        close(output_pipe[0]);
        (void)kill(child, SIGKILL);
        (void)waitpid(child, NULL, 0);
        return NULL;
    }
    output = malloc(capacity);
    if (output == NULL) {
        close(output_pipe[0]);
        (void)kill(child, SIGKILL);
        (void)waitpid(child, NULL, 0);
        return NULL;
    }

    for (attempt = 0;
         attempt < POLL_ATTEMPTS && (!child_done || !end_of_file);
         attempt++) {
        struct pollfd descriptor = {
            .fd = output_pipe[0],
            .events = POLLIN | POLLHUP
        };
        ssize_t size;
        pid_t waited;

        for (;;) {
            if (length + 1 == capacity) {
                char *larger;
                size_t new_capacity;

                if (capacity >= MAXIMUM_CAPACITY)
                    break;
                new_capacity = capacity * 2;
                if (new_capacity > MAXIMUM_CAPACITY)
                    new_capacity = MAXIMUM_CAPACITY;
                larger = realloc(output, new_capacity);
                if (larger == NULL)
                    break;
                output = larger;
                capacity = new_capacity;
            }
            size = read(
                output_pipe[0], output + length, capacity - length - 1
            );
            if (size > 0) {
                length += (size_t)size;
                continue;
            }
            if (size == 0)
                end_of_file = true;
            else if (errno != EAGAIN && errno != EWOULDBLOCK
                     && errno != EINTR)
                end_of_file = true;
            break;
        }
        if (!child_done) {
            waited = waitpid(child, &status, WNOHANG);
            if (waited == child) {
                child_done = true;
                child_status_valid = true;
            } else if (waited < 0 && errno != EINTR) {
                child_done = true;
            }
        }
        if (!child_done || !end_of_file)
            (void)poll(&descriptor, 1, 10);
    }
    close(output_pipe[0]);
    if (!child_done) {
        (void)kill(child, SIGKILL);
        do {
            child_done = waitpid(child, &status, 0) == child;
        } while (!child_done && errno == EINTR);
        child_status_valid = child_done;
    }
    if (!child_done
        || !child_status_valid
        || !end_of_file
        || !WIFEXITED(status)
        || WEXITSTATUS(status) != 0) {
        free(output);
        return NULL;
    }
    output[length] = '\0';
    return output;
}

static char *manager_environment_value(
    const char *environment,
    const char *name
)
{
    const char *line = environment;
    size_t name_length = strlen(name);

    while (*line != '\0') {
        const char *end = strchr(line, '\n');
        size_t line_length = end == NULL
            ? strlen(line)
            : (size_t)(end - line);

        if (line_length > name_length
            && line[name_length] == '='
            && strncmp(line, name, name_length) == 0) {
            size_t value_length = line_length - name_length - 1;
            char *value = malloc(value_length + 1);

            if (value == NULL)
                return NULL;
            memcpy(value, line + name_length + 1, value_length);
            value[value_length] = '\0';
            return value;
        }
        if (end == NULL)
            break;
        line = end + 1;
    }
    return NULL;
}

static bool path_has_component(
    const char *path,
    const char *component,
    size_t component_length
)
{
    const char *cursor = path;

    while (*cursor != '\0') {
        const char *end = strchr(cursor, ':');
        size_t length = end == NULL
            ? strlen(cursor)
            : (size_t)(end - cursor);

        if (length == component_length
            && memcmp(cursor, component, length) == 0)
            return true;
        if (end == NULL)
            break;
        cursor = end + 1;
    }
    return false;
}

static char *merge_path_environment(
    const char *preferred,
    const char *preserved
)
{
    size_t preferred_length = strlen(preferred);
    size_t preserved_length = strlen(preserved);
    char *merged = malloc(preferred_length + preserved_length + 2);
    const char *cursor = preserved;
    size_t length = preferred_length;

    if (merged == NULL)
        return NULL;
    memcpy(merged, preferred, preferred_length);
    merged[length] = '\0';
    while (*cursor != '\0') {
        const char *end = strchr(cursor, ':');
        size_t component_length = end == NULL
            ? strlen(cursor)
            : (size_t)(end - cursor);

        if (component_length > 0
            && !path_has_component(merged, cursor, component_length)) {
            if (length > 0)
                merged[length++] = ':';
            memcpy(merged + length, cursor, component_length);
            length += component_length;
            merged[length] = '\0';
        }
        if (end == NULL)
            break;
        cursor = end + 1;
    }
    return merged;
}

static int merge_manager_path_environment(
    char *const *names,
    const bool *merge_paths,
    size_t count
)
{
    /*
     * machinectl supplies Spaces' environment to this process, while the
     * guest user manager still holds distribution-provided additions.  Merge
     * the latter after the Spaces paths before synchronizing both D-Bus and
     * systemd, preserving Spaces' portal/appearance precedence.
     */
    char *manager_environment;
    size_t index;
    bool needed = false;
    int result = 0;

    for (index = 0; index < count; index++)
        needed = needed || merge_paths[index];
    if (!needed)
        return 0;
    manager_environment = read_manager_environment();
    if (manager_environment == NULL) {
        fprintf(stderr,
                "spaces: warning: could not read guest service-manager "
                "environment\n");
        return -1;
    }
    for (index = 0; index < count; index++) {
        const char *preferred;
        char *preserved;
        char *merged;

        if (!merge_paths[index])
            continue;
        preferred = getenv(names[index]);
        preserved = manager_environment_value(
            manager_environment, names[index]
        );
        if (preferred == NULL || preserved == NULL) {
            free(preserved);
            continue;
        }
        merged = merge_path_environment(preferred, preserved);
        free(preserved);
        if (merged == NULL || setenv(names[index], merged, 1) < 0) {
            free(merged);
            result = -1;
            continue;
        }
        free(merged);
    }
    free(manager_environment);
    if (result < 0)
        fprintf(stderr,
                "spaces: warning: could not merge guest path environment\n");
    return result;
}

static int update_dbus_environment(char *const *names, size_t count)
{
    char **arguments;
    int attempts;
    int null_fd;
    int status;
    pid_t child;
    pid_t waited;
    size_t index;

    if (count == 0)
        return 0;
    child = fork();
    if (child < 0) {
        fprintf(stderr,
                "spaces: warning: could not update guest D-Bus "
                "activation environment: %s\n",
                strerror(errno));
        return -1;
    }
    if (child == 0) {
        null_fd = open("/dev/null", O_RDWR | O_CLOEXEC);
        if (null_fd < 0
            || dup2(null_fd, STDIN_FILENO) < 0
            || dup2(null_fd, STDOUT_FILENO) < 0
            || dup2(null_fd, STDERR_FILENO) < 0)
            _exit(127);
        if (null_fd > STDERR_FILENO)
            close(null_fd);

        arguments = calloc(count + 3, sizeof(*arguments));
        if (arguments == NULL)
            _exit(127);
        arguments[0] = (char *)DBUS_UPDATE_ACTIVATION_ENVIRONMENT;
        arguments[1] = (char *)"--systemd";
        for (index = 0; index < count; index++)
            arguments[index + 2] = names[index];
        execv(DBUS_UPDATE_ACTIVATION_ENVIRONMENT, arguments);
        _exit(127);
    }
    waited = 0;
    for (attempts = 0; attempts < 100 && waited == 0; attempts++) {
        waited = waitpid(child, &status, WNOHANG);
        if (waited < 0 && errno == EINTR) {
            waited = 0;
            continue;
        }
        if (waited == 0)
            usleep(10000);
    }
    if (waited == 0) {
        (void)kill(child, SIGKILL);
        do {
            waited = waitpid(child, &status, 0);
        } while (waited < 0 && errno == EINTR);
    }
    if (waited != child
        || !WIFEXITED(status)
        || WEXITSTATUS(status) != 0) {
        fprintf(stderr,
                "spaces: warning: could not update guest D-Bus "
                "activation environment\n");
        return -1;
    }
    return 0;
}

static void report_status(int descriptor, int status);
static void detach_terminal(void);

static void execute_agent(
    const char *agent,
    int error_descriptor,
    int output_descriptor
)
{
    int child_errno;
    int null_fd = open("/dev/null", O_RDWR | O_CLOEXEC);

    if (null_fd < 0
        || dup2(null_fd, STDIN_FILENO) < 0
        || dup2(null_fd, STDOUT_FILENO) < 0
        || dup2(output_descriptor, STDERR_FILENO) < 0) {
        child_errno = errno;
        report_status(error_descriptor, child_errno);
        _exit(127);
    }
    if (null_fd > STDERR_FILENO)
        close(null_fd);
    if (output_descriptor > STDERR_FILENO)
        close(output_descriptor);
    /*
     * polkit-kde reports successful agent registration with qDebug(). Fedora
     * disables the default Qt debug category, so enable it only in the agent
     * child. The supervisor consumes the output and uses the registration
     * message as its readiness signal.
     */
    if (setenv("QT_LOGGING_RULES", "default.debug=true", 1) < 0) {
        child_errno = errno;
        report_status(error_descriptor, child_errno);
        _exit(127);
    }
    execl(agent, agent, (char *)NULL);
    child_errno = errno;
    report_status(error_descriptor, child_errno);
    _exit(127);
}

static void write_ready_notification(int descriptor)
{
    unsigned char ready = 1;
    ssize_t written;

    do {
        written = write(descriptor, &ready, sizeof(ready));
    } while (written < 0 && errno == EINTR);
    if (written != sizeof(ready))
        fprintf(stderr,
                "spaces: warning: could not report polkit agent readiness\n");
}

static void relay_agent_output(int descriptor, int ready_descriptor)
{
    static const char ready_message[] =
        "Authentication agent result: true";
    char buffer[4096];
    size_t matched = 0;
    bool reported = false;

    for (;;) {
        ssize_t size = read(descriptor, buffer, sizeof(buffer));
        ssize_t index;

        if (size == 0)
            break;
        if (size < 0) {
            if (errno == EINTR)
                continue;
            break;
        }
        for (index = 0; index < size && !reported; index++) {
            if (buffer[index] == ready_message[matched]) {
                matched++;
                if (ready_message[matched] == '\0') {
                    write_ready_notification(ready_descriptor);
                    close(ready_descriptor);
                    ready_descriptor = -1;
                    reported = true;
                }
            } else {
                matched = buffer[index] == ready_message[0] ? 1 : 0;
            }
        }
    }
    close(descriptor);
    if (ready_descriptor >= 0)
        close(ready_descriptor);
}

static void supervise_agent(
    const char *agent,
    int error_descriptor,
    int ready_descriptor
)
{
    int output_pipe[2];
    int status;
    pid_t child;
    pid_t waited;

    (void)signal(SIGPIPE, SIG_IGN);
    if (pipe2(output_pipe, O_CLOEXEC) < 0) {
        status = errno;
        report_status(error_descriptor, status);
        _exit(127);
    }
    child = fork();
    if (child < 0) {
        status = errno;
        close(output_pipe[0]);
        close(output_pipe[1]);
        report_status(error_descriptor, status);
        _exit(127);
    }
    if (child == 0) {
        close(output_pipe[0]);
        execute_agent(agent, error_descriptor, output_pipe[1]);
    }
    close(error_descriptor);
    close(output_pipe[1]);
    detach_terminal();
    relay_agent_output(output_pipe[0], ready_descriptor);
    do {
        waited = waitpid(child, &status, 0);
    } while (waited < 0 && errno == EINTR);
    _exit(waited == child ? status_code(status) : 1);
}

static bool wait_for_agent(int descriptor)
{
    struct pollfd poll_descriptor = {
        .fd = descriptor,
        .events = POLLIN,
    };
    unsigned char ready;
    int result;

    do {
        result = poll(&poll_descriptor, 1, 2000);
    } while (result < 0 && errno == EINTR);
    if (result <= 0 || !(poll_descriptor.revents & POLLIN))
        return false;
    return read(descriptor, &ready, sizeof(ready)) == sizeof(ready)
        && ready == 1;
}

static pid_t start_agent(const char *agent, int *ready_descriptor)
{
    int error_pipe[2];
    int ready_pipe[2];
    int child_errno = 0;
    ssize_t size;
    pid_t child;

    *ready_descriptor = -1;
    if (agent == NULL)
        return -1;
    if (pipe2(error_pipe, O_CLOEXEC) < 0)
        return -1;
    if (pipe2(ready_pipe, O_CLOEXEC) < 0) {
        close(error_pipe[0]);
        close(error_pipe[1]);
        return -1;
    }
    child = fork();
    if (child < 0) {
        close(error_pipe[0]);
        close(error_pipe[1]);
        close(ready_pipe[0]);
        close(ready_pipe[1]);
        return -1;
    }
    if (child == 0) {
        close(error_pipe[0]);
        close(ready_pipe[0]);
        (void)setpgid(0, 0);
        supervise_agent(agent, error_pipe[1], ready_pipe[1]);
    }
    (void)setpgid(child, child);
    close(error_pipe[1]);
    close(ready_pipe[1]);
    do {
        size = read(error_pipe[0], &child_errno, sizeof(child_errno));
    } while (size < 0 && errno == EINTR);
    close(error_pipe[0]);
    if (size > 0) {
        fprintf(stderr, "spaces: warning: could not start polkit agent: %s\n",
                strerror(child_errno));
        (void)waitpid(child, NULL, 0);
        close(ready_pipe[0]);
        return -1;
    }
    *ready_descriptor = ready_pipe[0];
    return child;
}

static pid_t finish_agent_start(pid_t child, int ready_descriptor)
{
    int status;

    if (child <= 0)
        return child;
    if (!wait_for_agent(ready_descriptor)) {
        fprintf(stderr,
                "spaces: warning: polkit agent did not become ready\n");
    }
    close(ready_descriptor);
    if (waitpid(child, &status, WNOHANG) == child) {
        fprintf(stderr,
                "spaces: warning: polkit agent exited during startup\n");
        return -1;
    }
    return child;
}

static AgentReference acquire_agent_reference(
    const char *agent,
    int *ready_descriptor
)
{
    AgentReference reference = {
        .pid = -1,
        .start_time = 0,
        .state_descriptor = -1,
        .held = false,
        .starting = false,
    };
    AgentState state;

    *ready_descriptor = -1;
    if (agent == NULL)
        return reference;
    reference.state_descriptor = lock_agent_state();
    if (reference.state_descriptor < 0) {
        fprintf(
            stderr,
            "spaces: warning: could not lock shared polkit agent state\n"
        );
        return reference;
    }
    state = read_agent_state(reference.state_descriptor);
    if (agent_state_is_live(state) && state.references < UINT_MAX) {
        state.references++;
        if (write_agent_state(reference.state_descriptor, state)) {
            reference.pid = state.pid;
            reference.start_time = state.start_time;
            reference.held = true;
        }
        (void)flock(reference.state_descriptor, LOCK_UN);
        close(reference.state_descriptor);
        reference.state_descriptor = -1;
        return reference;
    }

    state.pid = 0;
    state.start_time = 0;
    state.references = 0;
    (void)write_agent_state(reference.state_descriptor, state);
    reference.pid = start_agent(agent, ready_descriptor);
    if (reference.pid <= 0) {
        (void)flock(reference.state_descriptor, LOCK_UN);
        close(reference.state_descriptor);
        reference.state_descriptor = -1;
        return reference;
    }
    reference.held = true;
    reference.starting = true;
    return reference;
}

static void finish_agent_reference(
    AgentReference *reference,
    int ready_descriptor
)
{
    AgentState state;
    pid_t started;

    if (!reference->starting)
        return;
    started = finish_agent_start(reference->pid, ready_descriptor);
    if (started > 0
        && process_start_time(started, &reference->start_time)) {
        state.pid = started;
        state.start_time = reference->start_time;
        state.references = 1;
        if (write_agent_state(reference->state_descriptor, state)) {
            reference->starting = false;
        } else {
            started = -1;
        }
    }
    if (started <= 0) {
        state.pid = 0;
        state.start_time = 0;
        state.references = 0;
        (void)write_agent_state(reference->state_descriptor, state);
        stop_agent(reference->pid);
        reference->pid = -1;
        reference->start_time = 0;
        reference->held = false;
        reference->starting = false;
    }
    (void)flock(reference->state_descriptor, LOCK_UN);
    close(reference->state_descriptor);
    reference->state_descriptor = -1;
}

static void release_agent_reference(AgentReference *reference)
{
    AgentState state;
    int descriptor;

    if (!reference->held || reference->pid <= 0)
        return;
    descriptor = lock_agent_state();
    if (descriptor >= 0) {
        state = read_agent_state(descriptor);
        if (state.pid == reference->pid
            && state.start_time == reference->start_time
            && state.references > 0) {
            state.references--;
            if (state.references == 0) {
                state.pid = 0;
                state.start_time = 0;
                (void)write_agent_state(descriptor, state);
                terminate_agent(reference->pid);
            } else {
                (void)write_agent_state(descriptor, state);
            }
        }
        (void)flock(descriptor, LOCK_UN);
        close(descriptor);
    }
    reference->pid = -1;
    reference->start_time = 0;
    reference->held = false;
}

static void execute_application(char **command)
{
    int saved_errno;

    if (command[0] != NULL) {
        execvp(command[0], command);
    } else {
        struct passwd *account = getpwuid(getuid());
        const char *shell = "/bin/sh";
        const char *name;

        if (account != NULL && account->pw_shell != NULL
            && account->pw_shell[0] == '/')
            shell = account->pw_shell;
        name = strrchr(shell, '/');
        name = name == NULL ? shell : name + 1;
        {
            size_t size = strlen(name) + 2;
            char *login_name = malloc(size);

            if (login_name == NULL)
                _exit(127);
            login_name[0] = '-';
            memcpy(login_name + 1, name, size - 1);
            execl(shell, login_name, (char *)NULL);
        }
    }
    saved_errno = errno;
    fprintf(stderr, "spaces: could not start application: %s\n",
            strerror(saved_errno));
    _exit(saved_errno == ENOENT ? 127 : 126);
}

static void report_status(int descriptor, int status)
{
    const unsigned char *data = (const unsigned char *)&status;
    size_t remaining = sizeof(status);

    while (remaining > 0) {
        ssize_t size = write(descriptor, data, remaining);

        if (size < 0) {
            if (errno == EINTR)
                continue;
            return;
        }
        data += size;
        remaining -= (size_t)size;
    }
}

static void detach_terminal(void)
{
    int descriptor;

    descriptor = open("/dev/null", O_RDWR | O_CLOEXEC);
    if (descriptor < 0)
        return;
    (void)dup2(descriptor, STDIN_FILENO);
    (void)dup2(descriptor, STDOUT_FILENO);
    (void)dup2(descriptor, STDERR_FILENO);
    if (descriptor > STDERR_FILENO)
        close(descriptor);
}

static void monitor_application(
    char **command,
    AgentReference agent,
    int status_descriptor
)
{
    /*
     * Stay in the machinectl-created PAM session: polkit scopes the agent to
     * that session.  The original launcher may return after a GUI daemonizes,
     * while this adopted monitor keeps only the application tree and agent
     * alive, then removes both without creating another logind session.
     */
    pid_t application;
    bool reported = false;
    int status;

    if (prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) < 0) {
        report_status(status_descriptor, 1 << 8);
        close(status_descriptor);
        release_agent_reference(&agent);
        _exit(1);
    }
    application = fork();
    if (application < 0) {
        report_status(status_descriptor, 1 << 8);
        close(status_descriptor);
        release_agent_reference(&agent);
        _exit(1);
    }
    if (application == 0)
        execute_application(command);

    forwarded_child = application;
    if (configure_supervisor_signals() < 0) {
        (void)kill(application, SIGTERM);
        report_status(status_descriptor, 1 << 8);
        close(status_descriptor);
        release_agent_reference(&agent);
        _exit(1);
    }
    (void)signal(SIGPIPE, SIG_IGN);

    for (;;) {
        pid_t waited = waitpid(-1, &status, 0);

        if (waited < 0) {
            if (errno == EINTR && !termination_requested)
                continue;
            break;
        }
        if (waited == application && !reported) {
            forwarded_child = -1;
            report_status(status_descriptor, status);
            close(status_descriptor);
            status_descriptor = -1;
            reported = true;
            detach_terminal();
        }
        if (termination_requested)
            break;
    }
    if (termination_requested)
        terminate_adopted_groups((int)termination_requested);
    if (!reported) {
        report_status(status_descriptor, 1 << 8);
        close(status_descriptor);
    }
    release_agent_reference(&agent);
    _exit(0);
}

static int read_status(int descriptor, int *status)
{
    unsigned char *data = (unsigned char *)status;
    size_t remaining = sizeof(*status);

    while (remaining > 0) {
        ssize_t size = read(descriptor, data, remaining);

        if (size == 0)
            return -1;
        if (size < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        data += size;
        remaining -= (size_t)size;
    }
    return 0;
}

int main(int argc, char **argv)
{
    const char *agent = NULL;
    char **dbus_environment;
    bool *merge_dbus_paths;
    char **command;
    size_t dbus_environment_count = 0;
    int status_pipe[2];
    int status;
    int agent_ready_descriptor;
    AgentReference agent_reference;
    pid_t monitor;
    int index = 1;

    dbus_environment = calloc((size_t)argc, sizeof(*dbus_environment));
    merge_dbus_paths = calloc((size_t)argc, sizeof(*merge_dbus_paths));
    if (dbus_environment == NULL || merge_dbus_paths == NULL) {
        free(dbus_environment);
        free(merge_dbus_paths);
        return 1;
    }
    while (index < argc && strcmp(argv[index], "--") != 0) {
        if (strcmp(argv[index], "--agent") == 0) {
            if (index + 1 >= argc) {
                fprintf(stderr, "spaces: --agent requires a path\n");
                free(dbus_environment);
                free(merge_dbus_paths);
                return 2;
            }
            agent = argv[index + 1];
            index += 2;
        } else if (strcmp(argv[index], "--dbus-env") == 0
                   || strcmp(argv[index], "--dbus-env-path") == 0) {
            bool merge_path =
                strcmp(argv[index], "--dbus-env-path") == 0;

            if (index + 1 >= argc) {
                fprintf(stderr,
                        "spaces: %s requires a variable name\n",
                        argv[index]);
                free(dbus_environment);
                free(merge_dbus_paths);
                return 2;
            }
            dbus_environment[dbus_environment_count] = argv[index + 1];
            merge_dbus_paths[dbus_environment_count] = merge_path;
            dbus_environment_count++;
            index += 2;
        } else {
            fprintf(stderr, "spaces: unknown option: %s\n", argv[index]);
            free(dbus_environment);
            free(merge_dbus_paths);
            return 2;
        }
    }
    if (index >= argc || strcmp(argv[index], "--") != 0) {
        fprintf(stderr, "spaces: expected -- before the command\n");
        free(dbus_environment);
        free(merge_dbus_paths);
        return 2;
    }
    command = &argv[index + 1];

    (void)merge_manager_path_environment(
        dbus_environment, merge_dbus_paths, dbus_environment_count
    );
    agent_reference = acquire_agent_reference(
        agent, &agent_ready_descriptor
    );
    (void)update_dbus_environment(
        dbus_environment, dbus_environment_count
    );
    free(dbus_environment);
    free(merge_dbus_paths);
    finish_agent_reference(&agent_reference, agent_ready_descriptor);
    if (pipe2(status_pipe, O_CLOEXEC) < 0) {
        release_agent_reference(&agent_reference);
        return 1;
    }
    monitor = fork();
    if (monitor < 0) {
        close(status_pipe[0]);
        close(status_pipe[1]);
        release_agent_reference(&agent_reference);
        return 1;
    }
    if (monitor == 0) {
        close(status_pipe[0]);
        monitor_application(command, agent_reference, status_pipe[1]);
    }
    agent_reference.held = false;

    close(status_pipe[1]);
    forwarded_child = monitor;
    if (configure_supervisor_signals() < 0) {
        (void)kill(monitor, SIGTERM);
        close(status_pipe[0]);
        return 1;
    }
    if (read_status(status_pipe[0], &status) < 0) {
        (void)kill(monitor, SIGTERM);
        close(status_pipe[0]);
        return 1;
    }
    close(status_pipe[0]);
    forwarded_child = -1;
    (void)waitpid(monitor, NULL, WNOHANG);
    return status_code(status);
}
