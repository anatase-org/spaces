#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pwd.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef DBUS_UPDATE_ACTIVATION_ENVIRONMENT
#define DBUS_UPDATE_ACTIVATION_ENVIRONMENT \
    "/usr/bin/dbus-update-activation-environment"
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

static void forward_term(int signum)
{
    pid_t child = (pid_t)forwarded_child;

    if (child > 0)
        (void)kill(child, signum);
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
    execl(agent, agent, (char *)NULL);
    child_errno = errno;
    report_status(error_descriptor, child_errno);
    _exit(127);
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
                    unsigned char ready = 1;

                    (void)write(ready_descriptor, &ready, sizeof(ready));
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
    pid_t agent,
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
        terminate_agent(agent);
        _exit(1);
    }
    application = fork();
    if (application < 0) {
        report_status(status_descriptor, 1 << 8);
        close(status_descriptor);
        terminate_agent(agent);
        _exit(1);
    }
    if (application == 0)
        execute_application(command);

    forwarded_child = application;
    if (configure_supervisor_signals() < 0) {
        (void)kill(application, SIGTERM);
        report_status(status_descriptor, 1 << 8);
        close(status_descriptor);
        terminate_agent(agent);
        _exit(1);
    }
    (void)signal(SIGPIPE, SIG_IGN);

    for (;;) {
        pid_t waited = waitpid(-1, &status, 0);

        if (waited < 0) {
            if (errno == EINTR)
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
    }
    if (!reported) {
        report_status(status_descriptor, 1 << 8);
        close(status_descriptor);
    }
    terminate_agent(agent);
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
    char **command;
    size_t dbus_environment_count = 0;
    int status_pipe[2];
    int status;
    int agent_ready_descriptor;
    pid_t agent_pid;
    pid_t monitor;
    int index = 1;

    dbus_environment = calloc((size_t)argc, sizeof(*dbus_environment));
    if (dbus_environment == NULL)
        return 1;
    while (index < argc && strcmp(argv[index], "--") != 0) {
        if (strcmp(argv[index], "--agent") == 0) {
            if (index + 1 >= argc) {
                fprintf(stderr, "spaces: --agent requires a path\n");
                free(dbus_environment);
                return 2;
            }
            agent = argv[index + 1];
            index += 2;
        } else if (strcmp(argv[index], "--dbus-env") == 0) {
            if (index + 1 >= argc) {
                fprintf(stderr,
                        "spaces: --dbus-env requires a variable name\n");
                free(dbus_environment);
                return 2;
            }
            dbus_environment[dbus_environment_count++] = argv[index + 1];
            index += 2;
        } else {
            fprintf(stderr, "spaces: unknown option: %s\n", argv[index]);
            free(dbus_environment);
            return 2;
        }
    }
    if (index >= argc || strcmp(argv[index], "--") != 0) {
        fprintf(stderr, "spaces: expected -- before the command\n");
        free(dbus_environment);
        return 2;
    }
    command = &argv[index + 1];

    agent_pid = start_agent(agent, &agent_ready_descriptor);
    (void)update_dbus_environment(
        dbus_environment, dbus_environment_count
    );
    free(dbus_environment);
    agent_pid = finish_agent_start(agent_pid, agent_ready_descriptor);
    if (pipe2(status_pipe, O_CLOEXEC) < 0) {
        stop_agent(agent_pid);
        return 1;
    }
    monitor = fork();
    if (monitor < 0) {
        close(status_pipe[0]);
        close(status_pipe[1]);
        stop_agent(agent_pid);
        return 1;
    }
    if (monitor == 0) {
        close(status_pipe[0]);
        monitor_application(command, agent_pid, status_pipe[1]);
    }

    close(status_pipe[1]);
    forwarded_child = monitor;
    if (configure_supervisor_signals() < 0) {
        (void)kill(monitor, SIGTERM);
        close(status_pipe[0]);
        stop_agent(agent_pid);
        return 1;
    }
    if (read_status(status_pipe[0], &status) < 0) {
        (void)kill(monitor, SIGTERM);
        close(status_pipe[0]);
        stop_agent(agent_pid);
        return 1;
    }
    close(status_pipe[0]);
    forwarded_child = -1;
    (void)waitpid(monitor, NULL, WNOHANG);
    (void)waitpid(agent_pid, NULL, WNOHANG);
    return status_code(status);
}
