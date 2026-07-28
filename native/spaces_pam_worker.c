#define _GNU_SOURCE

#include "protocol.h"

#include <fcntl.h>
#include <security/pam_appl.h>
#include <security/pam_misc.h>
#include <security/pam_modules.h>
#include <signal.h>
#include <stdio.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>

struct worker_context {
    int fd;
};

static int worker_conversation(
    int count,
    const struct pam_message **messages,
    struct pam_response **responses,
    void *data
) {
    struct worker_context *context = data;
    if (count <= 0 || count > 32) return PAM_CONV_ERR;
    struct pam_response *values = calloc((size_t)count, sizeof(*values));
    if (values == NULL) return PAM_BUF_ERR;
    for (int index = 0; index < count; ++index) {
        size_t text_size = strlen(messages[index]->msg);
        if (text_size + sizeof(uint32_t) > SPACES_MAX_PAYLOAD) goto fail;
        unsigned char *payload = malloc(text_size + sizeof(uint32_t));
        if (payload == NULL) goto fail;
        uint32_t style = htonl((uint32_t)messages[index]->msg_style);
        memcpy(payload, &style, sizeof(style));
        memcpy(payload + sizeof(style), messages[index]->msg, text_size);
        int sent = spaces_send_frame(
            context->fd,
            SPACES_PAM_MESSAGE,
            payload,
            (uint32_t)(text_size + sizeof(style))
        );
        free(payload);
        if (sent < 0) goto fail;

        uint8_t type;
        unsigned char *response = NULL;
        uint32_t response_size = 0;
        if (
            spaces_recv_frame(
                context->fd,
                &type,
                &response,
                &response_size
            ) < 0 ||
            type != SPACES_PAM_RESPONSE
        ) {
            free(response);
            goto fail;
        }
        values[index].resp = (char *)response;
        values[index].resp_retcode = 0;
    }
    *responses = values;
    return PAM_SUCCESS;

fail:
    for (int index = 0; index < count; ++index) {
        if (values[index].resp != NULL) {
            spaces_erase(values[index].resp, strlen(values[index].resp));
            free(values[index].resp);
        }
    }
    free(values);
    return PAM_CONV_ERR;
}

static int harden(void) {
    struct rlimit zero = {0, 0};

    if (setrlimit(RLIMIT_CORE, &zero) != 0) return -1;
    if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0) return -1;
    umask(077);
    signal(SIGPIPE, SIG_IGN);
    if (clearenv() != 0) return -1;
    if (setenv("LANG", "C.UTF-8", 1) != 0) return -1;
    if (setenv("PATH", "/usr/bin", 1) != 0) return -1;
    return 0;
}

int main(int argc, char **argv) {
    int fd = -1;
    const char *user = NULL;
    for (int index = 1; index < argc; ++index) {
        if (strcmp(argv[index], "--fd") == 0 && index + 1 < argc) {
            fd = atoi(argv[++index]);
        } else if (
            strcmp(argv[index], "--user") == 0 && index + 1 < argc
        ) {
            user = argv[++index];
        } else {
            return 2;
        }
    }
    if (fd < 0 || user == NULL || *user == '\0') return 2;
    int descriptor_flags = fcntl(fd, F_GETFD);
    int status_flags = fcntl(fd, F_GETFL);
    if (
        descriptor_flags < 0 ||
        status_flags < 0 ||
        fcntl(fd, F_SETFD, descriptor_flags | FD_CLOEXEC) != 0 ||
        fcntl(fd, F_SETFL, status_flags & ~O_NONBLOCK) != 0
    ) return 1;
    if (harden() != 0) return 1;
    struct worker_context context = { .fd = fd };
    struct pam_conv conversation = {
        .conv = worker_conversation,
        .appdata_ptr = &context,
    };
    pam_handle_t *handle = NULL;
    int result = pam_start("spaces", user, &conversation, &handle);
    if (result == PAM_SUCCESS) {
        spaces_send_frame(fd, SPACES_READY, NULL, 0);
        result = pam_authenticate(handle, PAM_DISALLOW_NULL_AUTHTOK);
    }
    if (result == PAM_SUCCESS) {
        result = pam_acct_mgmt(handle, PAM_DISALLOW_NULL_AUTHTOK);
    }
    uint32_t network_result = htonl((uint32_t)result);
    spaces_send_frame(
        fd,
        SPACES_RESULT,
        &network_result,
        sizeof(network_result)
    );
    if (handle != NULL) pam_end(handle, result);
    close(fd);
    return result == PAM_SUCCESS ? 0 : 1;
}
