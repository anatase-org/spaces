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
    const char *user = NULL;
    if (pam_get_user(pamh, &user, NULL) != PAM_SUCCESS || user == NULL)
        return PAM_AUTHINFO_UNAVAIL;
    struct passwd *account = getpwnam(user);
    if (account == NULL) return PAM_USER_UNKNOWN;

    char request[64];
    int request_size = snprintf(
        request,
        sizeof(request),
        "{\"uid\":%lu}",
        (unsigned long)account->pw_uid
    );
    if (request_size < 0 || (size_t)request_size >= sizeof(request)) {
        return PAM_SYSTEM_ERR;
    }
    int fd = connect_broker();
    if (fd < 0) return PAM_AUTHINFO_UNAVAIL;
    int send_result = spaces_send_frame(
        fd,
        SPACES_AUTHENTICATE,
        request,
        (uint32_t)request_size
    );
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
    (void)pamh; (void)flags; (void)argc; (void)argv;
    return PAM_SUCCESS;
}

PAM_EXTERN int pam_sm_close_session(
    pam_handle_t *pamh,
    int flags,
    int argc,
    const char **argv
) {
    (void)pamh; (void)flags; (void)argc; (void)argv;
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
