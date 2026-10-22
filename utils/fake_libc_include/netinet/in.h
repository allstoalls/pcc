#include "_fake_defines.h"
#include "_fake_typedefs.h"

#ifndef INET_ADDRSTRLEN
#define INET_ADDRSTRLEN 16
#endif
#ifndef INET6_ADDRSTRLEN
#define INET6_ADDRSTRLEN 46
#endif

/* Darwin-layout IPv4/IPv6 address structs so pcc can lower sources that
 * traffic in socket addresses (e.g. pcc/runtime/src/py_asyncio_io.c).
 * Field order/size match macOS so the bytes handed to the real libc at
 * link time are interpreted correctly. */
#ifndef __PCC_FAKE_LIBC_IN_ADDR_DEFINED
#define __PCC_FAKE_LIBC_IN_ADDR_DEFINED

typedef __uint32_t in_addr_t;
typedef __uint16_t in_port_t;
typedef __uint8_t  sa_family_t;

struct in_addr {
    in_addr_t s_addr;
};

struct sockaddr_in {
    __uint8_t      sin_len;
    sa_family_t    sin_family;
    in_port_t      sin_port;
    struct in_addr sin_addr;
    char           sin_zero[8];
};

/* ponytail: flat 16-byte array matches Darwin in6_addr size/bytes and gives
 * s6_addr directly; the real header's __u6_addr union is unused by runtime. */
struct in6_addr {
    __uint8_t s6_addr[16];
};

struct sockaddr_in6 {
    __uint8_t       sin6_len;
    sa_family_t     sin6_family;
    in_port_t       sin6_port;
    __uint32_t      sin6_flowinfo;
    struct in6_addr sin6_addr;
    __uint32_t      sin6_scope_id;
};

#endif
