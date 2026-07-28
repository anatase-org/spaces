#include <errno.h>
#include <grp.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define PKCHECK "/usr/bin/pkcheck"
#define ACTION "org.anatase.spaces.root"

static bool parse_id(const char *value, unsigned long *result)
{
    char *end = NULL;

    errno = 0;
    *result = strtoul(value, &end, 10);
    return errno == 0 && value[0] != '\0' && end != NULL && *end == '\0';
}

static bool safe_space_name(const char *name)
{
    size_t length = strlen(name);

    if (length == 0 || length > 128)
        return false;
    for (size_t index = 0; index < length; index++) {
        char character = name[index];
        if (!((character >= 'a' && character <= 'z') ||
              (character >= 'A' && character <= 'Z') ||
              (character >= '0' && character <= '9') ||
              character == '-' || character == '_'))
            return false;
    }
    return true;
}

static bool harden_and_drop(uid_t uid, gid_t gid)
{
    struct rlimit limit = {0, 0};

    umask(077);
    if (setrlimit(RLIMIT_CORE, &limit) != 0 ||
        prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0)
        return false;
    if (setgroups(0, NULL) != 0 || setgid(gid) != 0 || setuid(uid) != 0)
        return false;
    if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0)
        return false;
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0)
        return false;
    return true;
}

int main(int argc, char **argv)
{
    unsigned long uid_value;
    unsigned long gid_value;
    unsigned long pid_value;
    unsigned long start_value;
    char subject[96];
    char *environment[] = {
        "LANG=C.UTF-8",
        "PATH=/usr/bin",
        NULL,
    };

    if (argc != 11 ||
        strcmp(argv[1], "--uid") != 0 ||
        strcmp(argv[3], "--gid") != 0 ||
        strcmp(argv[5], "--pid") != 0 ||
        strcmp(argv[7], "--start") != 0 ||
        strcmp(argv[9], "--space") != 0 ||
        !parse_id(argv[2], &uid_value) ||
        !parse_id(argv[4], &gid_value) ||
        !parse_id(argv[6], &pid_value) ||
        !parse_id(argv[8], &start_value) ||
        uid_value > UINT_MAX || gid_value > UINT_MAX ||
        pid_value > INT_MAX || !safe_space_name(argv[10]))
        return EXIT_FAILURE;

    if (!harden_and_drop((uid_t)uid_value, (gid_t)gid_value))
        return EXIT_FAILURE;
    snprintf(
        subject,
        sizeof(subject),
        "%lu,%lu,%lu",
        pid_value,
        start_value,
        uid_value);
    char *arguments[] = {
        PKCHECK,
        "--action-id",
        ACTION,
        "--process",
        subject,
        "--allow-user-interaction",
        "--detail",
        "space",
        argv[10],
        NULL,
    };
    execve(PKCHECK, arguments, environment);
    return EXIT_FAILURE;
}
