#include "_fake_defines.h"
#include "_fake_typedefs.h"

/* Minimal address-family / socket-type / function placeholders so pcc's
 * C parser can lower sources that use BSD socket APIs (e.g.
 * pcc/py_runtime/src/py_http.c). Values come from BSD/Darwin defaults
 * and are wide enough for both glibc and macOS at the codegen layer;
 * the actual numeric values come from the system compiler's real
 * headers at link time. */
#ifndef __PCC_FAKE_LIBC_SOCKET_DEFINED
#define __PCC_FAKE_LIBC_SOCKET_DEFINED

#define AF_UNSPEC 0
#define AF_INET 2
#define AF_INET6 30
#define SOCK_STREAM 1
#define SOCK_DGRAM 2

#define SOL_SOCKET 0xffff
#define SO_REUSEADDR 0x0004
#define SO_REUSEPORT 0x0200
#define SO_NOSIGPIPE 0x1022
/* py_asyncio_io.c reads it with getsockopt to complete a non-blocking
 * connect(); its absence blocked the pcc-C runtime archive build. */
#define SO_ERROR 0x1007

#define SHUT_RD 0
#define SHUT_WR 1
#define SHUT_RDWR 2

/* netdb.h includes sys/socket.h via the platform header chain; declare
 * ``struct sockaddr`` here as well in case sys/socket.h is included
 * without netdb.h. The matching ``addrinfo`` shape lives in netdb.h. */
#ifndef __PCC_FAKE_LIBC_SOCKADDR_DEFINED
#define __PCC_FAKE_LIBC_SOCKADDR_DEFINED
typedef unsigned int socklen_t;
struct sockaddr {
    unsigned short sa_family;
    char sa_data[14];
};
#endif

/* Darwin-layout sockaddr_storage: 128 bytes, 8-byte aligned, used as a
 * generic address buffer (e.g. py_asyncio_io.c accept/getpeername). */
#ifndef __PCC_FAKE_LIBC_SOCKADDR_STORAGE_DEFINED
#define __PCC_FAKE_LIBC_SOCKADDR_STORAGE_DEFINED
struct sockaddr_storage {
    __uint8_t ss_len;
    __uint8_t ss_family;
    char      __ss_pad1[6];
    long long __ss_align;
    char      __ss_pad2[112];
};
#endif

int socket(int domain, int type, int protocol);
int connect(int fd, const struct sockaddr *addr, socklen_t addrlen);
ssize_t send(int fd, const void *buf, size_t len, int flags);
ssize_t recv(int fd, void *buf, size_t len, int flags);

#endif
