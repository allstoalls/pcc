#include "_fake_defines.h"
#include "_fake_typedefs.h"

/* Minimal addrinfo / sockaddr placeholders so pcc's C parser can lower
 * sources that traffic in name-resolution structs (e.g.
 * pcc/runtime/src/py_http.c). The real definitions live in the
 * platform headers consumed by the system compiler; pcc only needs the
 * shape — field ordering matches both glibc and Darwin's netdb.h. */
#ifndef __PCC_FAKE_LIBC_ADDRINFO_DEFINED
#define __PCC_FAKE_LIBC_ADDRINFO_DEFINED

#ifndef AI_PASSIVE
#define AI_PASSIVE 0x00000001
#endif

typedef unsigned int socklen_t;

struct sockaddr {
    unsigned short sa_family;
    char sa_data[14];
};

struct addrinfo {
    int ai_flags;
    int ai_family;
    int ai_socktype;
    int ai_protocol;
    socklen_t ai_addrlen;
    char *ai_canonname;
    struct sockaddr *ai_addr;
    struct addrinfo *ai_next;
};

int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **res);
void freeaddrinfo(struct addrinfo *res);

#endif
