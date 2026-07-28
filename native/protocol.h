#ifndef SPACES_PROTOCOL_H
#define SPACES_PROTOCOL_H

#include <arpa/inet.h>
#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define SPACES_MAGIC 0x53504155U
#define SPACES_VERSION 2
#define SPACES_MAX_PAYLOAD (16U * 1024U)
#define SPACES_TOKEN_HEX_SIZE 64U

enum spaces_message_type {
    SPACES_REGISTER = 1,
    SPACES_AUTHENTICATE = 2,
    SPACES_TOKEN = 3,
    SPACES_READY = 4,
    SPACES_ERROR = 5,
    SPACES_RESULT = 6,
    SPACES_PAM_MESSAGE = 7,
    SPACES_PAM_RESPONSE = 8,
    SPACES_CANCEL = 9,
    SPACES_SESSION_OPEN = 10,
    SPACES_SESSION_CLOSE = 11,
};

struct spaces_header {
    uint32_t magic;
    uint8_t version;
    uint8_t type;
    uint32_t size;
} __attribute__((packed));

static int spaces_write_all(int fd, const void *buffer, size_t size) {
    const unsigned char *cursor = buffer;
    while (size > 0) {
        ssize_t written = write(fd, cursor, size);
        if (written < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        cursor += (size_t)written;
        size -= (size_t)written;
    }
    return 0;
}

static int spaces_read_all(int fd, void *buffer, size_t size) {
    unsigned char *cursor = buffer;
    while (size > 0) {
        ssize_t count = read(fd, cursor, size);
        if (count == 0) return -1;
        if (count < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        cursor += (size_t)count;
        size -= (size_t)count;
    }
    return 0;
}

static int spaces_send_frame(
    int fd,
    uint8_t type,
    const void *payload,
    uint32_t size
) {
    if (size > SPACES_MAX_PAYLOAD) return -1;
    struct spaces_header header = {
        .magic = htonl(SPACES_MAGIC),
        .version = SPACES_VERSION,
        .type = type,
        .size = htonl(size),
    };
    if (spaces_write_all(fd, &header, sizeof(header)) < 0) return -1;
    if (size > 0 && spaces_write_all(fd, payload, size) < 0) return -1;
    return 0;
}

static inline int spaces_send_frame_with_descriptor(
    int fd,
    uint8_t type,
    const void *payload,
    uint32_t size,
    int descriptor
) {
    if (size > SPACES_MAX_PAYLOAD || descriptor < 0) return -1;
    size_t frame_size = sizeof(struct spaces_header) + (size_t)size;
    unsigned char *frame = malloc(frame_size);
    if (frame == NULL) return -1;
    struct spaces_header header = {
        .magic = htonl(SPACES_MAGIC),
        .version = SPACES_VERSION,
        .type = type,
        .size = htonl(size),
    };
    memcpy(frame, &header, sizeof(header));
    if (size > 0) memcpy(frame + sizeof(header), payload, size);

    struct iovec vector = {
        .iov_base = frame,
        .iov_len = frame_size,
    };
    union {
        struct cmsghdr alignment;
        unsigned char bytes[CMSG_SPACE(sizeof(int))];
    } control;
    memset(&control, 0, sizeof(control));
    struct msghdr message = {
        .msg_iov = &vector,
        .msg_iovlen = 1,
        .msg_control = control.bytes,
        .msg_controllen = sizeof(control.bytes),
    };
    struct cmsghdr *control_message = CMSG_FIRSTHDR(&message);
    control_message->cmsg_level = SOL_SOCKET;
    control_message->cmsg_type = SCM_RIGHTS;
    control_message->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(control_message), &descriptor, sizeof(descriptor));

    ssize_t written;
    do {
        written = sendmsg(fd, &message, 0);
    } while (written < 0 && errno == EINTR);
    if (
        written < 0 ||
        (
            (size_t)written < frame_size &&
            spaces_write_all(
                fd,
                frame + (size_t)written,
                frame_size - (size_t)written
            ) < 0
        )
    ) {
        free(frame);
        return -1;
    }
    free(frame);
    return 0;
}

static int spaces_recv_frame(
    int fd,
    uint8_t *type,
    unsigned char **payload,
    uint32_t *size
) {
    struct spaces_header header;
    if (spaces_read_all(fd, &header, sizeof(header)) < 0) return -1;
    uint32_t decoded_size = ntohl(header.size);
    if (
        ntohl(header.magic) != SPACES_MAGIC ||
        header.version != SPACES_VERSION ||
        decoded_size > SPACES_MAX_PAYLOAD
    ) return -1;
    unsigned char *value = NULL;
    if (decoded_size > 0) {
        value = calloc(1, (size_t)decoded_size + 1U);
        if (value == NULL) return -1;
        if (spaces_read_all(fd, value, decoded_size) < 0) {
            free(value);
            return -1;
        }
    }
    *type = header.type;
    *payload = value;
    *size = decoded_size;
    return 0;
}

static void spaces_erase(void *value, size_t size) {
    volatile unsigned char *cursor = value;
    while (size-- > 0) *cursor++ = 0;
}

#endif
