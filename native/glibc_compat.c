/*
 * Modern glibc startup objects select the newest __libc_start_main symbol
 * even when an executable otherwise uses only the long-stable ABI. Pin that
 * one startup reference to the baseline exported by each supported target.
 */

#if defined(__x86_64__)
#define SPACES_GLIBC_BASELINE "GLIBC_2.2.5"
#elif defined(__aarch64__)
#define SPACES_GLIBC_BASELINE "GLIBC_2.17"
#else
#error Unsupported Spaces authentication architecture
#endif

typedef int (*main_function)(int, char **, char **);
typedef void (*init_function)(void);

extern int spaces_libc_start_main(
    main_function main,
    int argc,
    char **argv,
    init_function init,
    init_function fini,
    init_function rtld_fini,
    void *stack_end);

__asm__(
    ".symver spaces_libc_start_main,"
    "__libc_start_main@" SPACES_GLIBC_BASELINE);

int __wrap___libc_start_main(
    main_function main,
    int argc,
    char **argv,
    init_function init,
    init_function fini,
    init_function rtld_fini,
    void *stack_end)
{
    return spaces_libc_start_main(
        main,
        argc,
        argv,
        init,
        fini,
        rtld_fini,
        stack_end);
}
