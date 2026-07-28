#define _GNU_SOURCE

#include "protocol.h"

#include <pwd.h>
#include <security/pam_appl.h>
#include <security/pam_modules.h>
#include <stdio.h>
#include <sys/un.h>

#define SPACES_SOCKET "/run/spaces-host/auth.sock"

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
    const char *token = getenv("SPACES_AUTH_SESSION");
    const char *user = NULL;
    const void *service_item = NULL;
    if (
        !safe_token(token) ||
        pam_get_user(pamh, &user, NULL) != PAM_SUCCESS || user == NULL ||
        pam_get_item(pamh, PAM_SERVICE, &service_item) != PAM_SUCCESS ||
        !safe_service((const char *)service_item)
    ) return PAM_AUTHINFO_UNAVAIL;
    struct passwd *account = getpwnam(user);
    if (account == NULL) return PAM_USER_UNKNOWN;

    char request[512];
    int request_size = snprintf(
        request,
        sizeof(request),
        "{\"token\":\"%s\",\"uid\":%lu,\"service\":\"%s\"}",
        token,
        (unsigned long)account->pw_uid,
        (const char *)service_item
    );
    if (request_size < 0 || (size_t)request_size >= sizeof(request)) {
        return PAM_SYSTEM_ERR;
    }
    int fd = connect_broker();
    if (fd < 0) return PAM_AUTHINFO_UNAVAIL;
    if (
        spaces_send_frame(
            fd,
            SPACES_AUTHENTICATE,
            request,
            (uint32_t)request_size
        ) < 0
    ) {
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
