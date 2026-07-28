#define _GNU_SOURCE

#include "protocol.h"

#include <fcntl.h>
#include <grp.h>
#include <limits.h>
#include <pwd.h>
#include <security/pam_appl.h>
#include <security/pam_modules.h>
#include <stdio.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>

#define SPACES_SOCKET "/run/spaces-host/auth.sock"
#define SPACES_POLKIT_AGENT "/run/spaces-host/bin/spaces-polkit-agent"
#define SPACES_POLKIT_SERVICE "polkit-1"
#define SPACES_AGENT_RESTART_SECONDS 5
#define SPACES_TOKEN_SIZE 64

static int connect_broker(void) {
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;
    struct sockaddr_un address = { .sun_family = AF_UNIX };
    if (strlen(SPACES_SOCKET) >= sizeof(address.sun_path)) {
        close(fd);
        return -1;
    }
    strcpy(address.sun_path, SPACES_SOCKET);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static int safe_service(const char *value) {
    if (value == NULL || *value == '\0' || strlen(value) > 128) return 0;
    for (const unsigned char *p = (const unsigned char *)value; *p; ++p) {
        if (
            !((*p >= 'a' && *p <= 'z') ||
              (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') ||
              *p == '-' || *p == '_' || *p == '.')
        ) return 0;
    }
    return 1;
}

static int safe_token(const char *value) {
    if (value == NULL || strlen(value) != 64) return 0;
    for (const unsigned char *p = (const unsigned char *)value; *p; ++p) {
        if (!((*p >= '0' && *p <= '9') || (*p >= 'a' && *p <= 'f')))
            return 0;
    }
    return 1;
}

static int safe_session(const char *value) {
    if (value == NULL || *value == '\0' || strlen(value) > 128) return 0;
    for (const unsigned char *p = (const unsigned char *)value; *p; ++p) {
        if (
            !((*p >= 'a' && *p <= 'z') ||
              (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') ||
              *p == '_' || *p == '-')
        ) return 0;
    }
    return 1;
}

static int polkit_agent_pidfd(void) {
#ifdef SO_PEERPIDFD
    int descriptor = -1;
    socklen_t size = sizeof(descriptor);
    if (
        getsockopt(
            STDIN_FILENO,
            SOL_SOCKET,
            SO_PEERPIDFD,
            &descriptor,
            &size
        ) == 0 &&
        descriptor >= 0
    )
        return descriptor;
#endif
    return -1;
}

static void notify_session(pam_handle_t *pamh, uint8_t message_type) {
    const char *token = pam_getenv(pamh, "SPACES_AUTH_SESSION");
    const char *session_id = pam_getenv(pamh, "XDG_SESSION_ID");
    const char *user = NULL;
    struct passwd *account;
    char request[512];
    int request_size;
    int fd;

    if (token == NULL) token = getenv("SPACES_AUTH_SESSION");
    if (
        !safe_token(token) ||
        !safe_session(session_id) ||
        pam_get_user(pamh, &user, NULL) != PAM_SUCCESS ||
        user == NULL
    ) return;
    account = getpwnam(user);
    if (account == NULL) return;
    request_size = snprintf(
        request,
        sizeof(request),
        "{\"token\":\"%s\",\"uid\":%lu,\"session_id\":\"%s\"}",
        token,
        (unsigned long)account->pw_uid,
        session_id
    );
    if (request_size < 0 || (size_t)request_size >= sizeof(request)) return;
    fd = connect_broker();
    if (fd < 0) return;
    (void)spaces_send_frame(
        fd,
        message_type,
        request,
        (uint32_t)request_size
    );
    close(fd);
}

static void close_unrelated_descriptors(void) {
#ifdef SYS_close_range
    if (syscall(SYS_close_range, 3U, ~0U, 0U) == 0)
        return;
    if (errno != ENOSYS && errno != EINVAL)
        return;
#endif
    struct rlimit limit;
    if (getrlimit(RLIMIT_NOFILE, &limit) != 0)
        return;
    rlim_t maximum = limit.rlim_cur;
    if (maximum == RLIM_INFINITY || maximum > (rlim_t)INT_MAX)
        maximum = (rlim_t)INT_MAX;
    for (int descriptor = 3; (rlim_t)descriptor < maximum; descriptor++)
        close(descriptor);
}

static void redirect_standard_streams(void) {
    int descriptor = open("/dev/null", O_RDWR | O_CLOEXEC);
    if (descriptor < 0)
        _exit(EXIT_FAILURE);
    for (int standard = STDIN_FILENO; standard <= STDERR_FILENO; standard++) {
        if (dup2(descriptor, standard) < 0)
            _exit(EXIT_FAILURE);
    }
    if (descriptor > STDERR_FILENO)
        close(descriptor);
}

static void wait_for_process(pid_t pid) {
    int status;
    while (waitpid(pid, &status, 0) < 0 && errno == EINTR)
        continue;
}

static void supervise_polkit_agent(
    const char *token,
    const char *session_id
) {
    char token_environment[
        sizeof("SPACES_AUTH_SESSION=") + SPACES_TOKEN_SIZE
    ];
    char *const environment[] = {
        token_environment,
        "LANG=C",
        "LC_ALL=C",
        "PATH=/usr/bin:/bin",
        NULL,
    };
    char *const arguments[] = {
        SPACES_POLKIT_AGENT,
        "--session",
        (char *)session_id,
        NULL,
    };
    struct timespec delay = {
        .tv_sec = SPACES_AGENT_RESTART_SECONDS,
        .tv_nsec = 0,
    };

    if (
        snprintf(
            token_environment,
            sizeof(token_environment),
            "SPACES_AUTH_SESSION=%s",
            token
        ) < 0
    )
        _exit(EXIT_FAILURE);

    for (;;) {
        pid_t child = fork();
        if (child == 0) {
            execve(SPACES_POLKIT_AGENT, arguments, environment);
            _exit(EXIT_FAILURE);
        }
        if (child < 0)
            _exit(EXIT_FAILURE);
        wait_for_process(child);

        struct timespec remaining = delay;
        while (
            nanosleep(&remaining, &remaining) < 0 &&
            errno == EINTR
        )
            continue;
    }
}

static void launch_polkit_agent(pam_handle_t *pamh) {
    const char *token = pam_getenv(pamh, "SPACES_AUTH_SESSION");
    const char *session_id = pam_getenv(pamh, "XDG_SESSION_ID");
    const char *user = NULL;
    struct passwd *account;
    pid_t child;

    if (token == NULL)
        token = getenv("SPACES_AUTH_SESSION");
    if (
        geteuid() != 0 ||
        !safe_token(token) ||
        !safe_session(session_id) ||
        pam_get_user(pamh, &user, NULL) != PAM_SUCCESS ||
        user == NULL
    )
        return;
    account = getpwnam(user);
    if (account == NULL)
        return;

    child = fork();
    if (child < 0)
        return;
    if (child == 0) {
        pid_t supervisor = fork();
        if (supervisor < 0)
            _exit(EXIT_FAILURE);
        if (supervisor > 0)
            _exit(EXIT_SUCCESS);

        redirect_standard_streams();
        close_unrelated_descriptors();
        umask(077);
        if (
            chdir("/") != 0 ||
            setgroups(0, NULL) != 0 ||
            setresgid(
                account->pw_gid,
                account->pw_gid,
                account->pw_gid
            ) != 0 ||
            setresuid(
                account->pw_uid,
                account->pw_uid,
                account->pw_uid
            ) != 0
        )
            _exit(EXIT_FAILURE);

        struct rlimit core_limit = {0, 0};
        if (
            setrlimit(RLIMIT_CORE, &core_limit) != 0 ||
            prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0
        )
            _exit(EXIT_FAILURE);
        supervise_polkit_agent(token, session_id);
    }
    wait_for_process(child);
}

static int relay_message(
    pam_handle_t *pamh,
    int fd,
    const unsigned char *payload,
    uint32_t size
) {
    if (size < sizeof(uint32_t)) return PAM_CONV_ERR;
    uint32_t network_style;
    memcpy(&network_style, payload, sizeof(network_style));
    int style = (int)ntohl(network_style);
    const char *text = (const char *)(payload + sizeof(network_style));
    size_t text_size = size - sizeof(network_style);
    char *message_text = calloc(1, text_size + 1U);
    if (message_text == NULL) return PAM_BUF_ERR;
    memcpy(message_text, text, text_size);

    const void *item = NULL;
    int result = pam_get_item(pamh, PAM_CONV, &item);
    if (result != PAM_SUCCESS || item == NULL) {
        free(message_text);
        return PAM_CONV_ERR;
    }
    const struct pam_conv *conversation = item;
    struct pam_message message = {
        .msg_style = style,
        .msg = message_text,
    };
    const struct pam_message *message_pointer = &message;
    struct pam_response *response = NULL;
    result = conversation->conv(
        1,
        &message_pointer,
        &response,
        conversation->appdata_ptr
    );
    free(message_text);
    if (result != PAM_SUCCESS) return result;
    const char *answer = "";
    if (response != NULL && response[0].resp != NULL) {
        answer = response[0].resp;
    }
    size_t answer_size = strlen(answer);
    if (answer_size > SPACES_MAX_PAYLOAD) result = PAM_MAXTRIES;
    else if (
        spaces_send_frame(
            fd,
            SPACES_PAM_RESPONSE,
            answer,
            (uint32_t)answer_size
        ) < 0
    ) result = PAM_SYSTEM_ERR;
    if (response != NULL) {
        if (response[0].resp != NULL) {
            spaces_erase(response[0].resp, strlen(response[0].resp));
            free(response[0].resp);
        }
        free(response);
    }
    return result;
}

PAM_EXTERN int pam_sm_authenticate(
    pam_handle_t *pamh,
    int flags,
    int argc,
    const char **argv
) {
    (void)flags;
    (void)argc;
    (void)argv;
    const char *token = pam_getenv(pamh, "SPACES_AUTH_SESSION");
    const char *user = NULL;
    const void *service_item = NULL;
    const char *service;
    int token_required;
    if (token == NULL)
        token = getenv("SPACES_AUTH_SESSION");
    if (
        pam_get_user(pamh, &user, NULL) != PAM_SUCCESS || user == NULL ||
        pam_get_item(pamh, PAM_SERVICE, &service_item) != PAM_SUCCESS ||
        !safe_service((const char *)service_item)
    ) return PAM_AUTHINFO_UNAVAIL;
    service = service_item;
    token_required = strcmp(service, SPACES_POLKIT_SERVICE) != 0;
    if (token_required && !safe_token(token))
        return PAM_AUTHINFO_UNAVAIL;
    struct passwd *account = getpwnam(user);
    if (account == NULL) return PAM_USER_UNKNOWN;

    char request[512];
    int request_size;
    if (safe_token(token)) {
        request_size = snprintf(
            request,
            sizeof(request),
            "{\"token\":\"%s\",\"uid\":%lu,\"service\":\"%s\"}",
            token,
            (unsigned long)account->pw_uid,
            service
        );
    } else {
        request_size = snprintf(
            request,
            sizeof(request),
            "{\"uid\":%lu,\"service\":\"%s\"}",
            (unsigned long)account->pw_uid,
            service
        );
    }
    if (request_size < 0 || (size_t)request_size >= sizeof(request)) {
        return PAM_SYSTEM_ERR;
    }
    int fd = connect_broker();
    if (fd < 0) return PAM_AUTHINFO_UNAVAIL;
    int agent_pidfd = -1;
    if (!safe_token(token) && !token_required)
        agent_pidfd = polkit_agent_pidfd();
    int send_result;
    if (agent_pidfd >= 0) {
        send_result = spaces_send_frame_with_descriptor(
            fd,
            SPACES_AUTHENTICATE,
            request,
            (uint32_t)request_size,
            agent_pidfd
        );
        close(agent_pidfd);
    } else {
        send_result = spaces_send_frame(
            fd,
            SPACES_AUTHENTICATE,
            request,
            (uint32_t)request_size
        );
    }
    if (send_result < 0) {
        close(fd);
        return PAM_AUTHINFO_UNAVAIL;
    }

    int result = PAM_SYSTEM_ERR;
    for (;;) {
        uint8_t type;
        unsigned char *payload = NULL;
        uint32_t size = 0;
        if (spaces_recv_frame(fd, &type, &payload, &size) < 0) {
            result = PAM_AUTHINFO_UNAVAIL;
            break;
        }
        if (type == SPACES_READY) {
            free(payload);
            continue;
        }
        if (type == SPACES_PAM_MESSAGE) {
            result = relay_message(pamh, fd, payload, size);
            free(payload);
            if (result != PAM_SUCCESS) {
                spaces_send_frame(fd, SPACES_CANCEL, NULL, 0);
                break;
            }
            continue;
        }
        if (type == SPACES_RESULT && size == sizeof(uint32_t)) {
            uint32_t value;
            memcpy(&value, payload, sizeof(value));
            result = (int)ntohl(value);
            free(payload);
            break;
        }
        free(payload);
        result = type == SPACES_ERROR ? PAM_AUTHINFO_UNAVAIL : PAM_SYSTEM_ERR;
        break;
    }
    close(fd);
    return result;
}

PAM_EXTERN int pam_sm_setcred(
    pam_handle_t *pamh,
    int flags,
    int argc,
    const char **argv
) {
    (void)pamh; (void)flags; (void)argc; (void)argv;
    return PAM_SUCCESS;
}

PAM_EXTERN int pam_sm_acct_mgmt(
    pam_handle_t *pamh,
    int flags,
    int argc,
    const char **argv
) {
    (void)pamh; (void)flags; (void)argc; (void)argv;
    return PAM_SUCCESS;
}

PAM_EXTERN int pam_sm_open_session(
    pam_handle_t *pamh,
    int flags,
    int argc,
    const char **argv
) {
    (void)flags; (void)argc; (void)argv;
    notify_session(pamh, SPACES_SESSION_OPEN);
    launch_polkit_agent(pamh);
    return PAM_SUCCESS;
}

PAM_EXTERN int pam_sm_close_session(
    pam_handle_t *pamh,
    int flags,
    int argc,
    const char **argv
) {
    (void)flags; (void)argc; (void)argv;
    notify_session(pamh, SPACES_SESSION_CLOSE);
    return PAM_SUCCESS;
}

PAM_EXTERN int pam_sm_chauthtok(
    pam_handle_t *pamh,
    int flags,
    int argc,
    const char **argv
) {
    (void)pamh; (void)flags; (void)argc; (void)argv;
    return PAM_PERM_DENIED;
}
