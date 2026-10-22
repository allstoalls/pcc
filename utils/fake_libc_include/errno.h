#include "_fake_defines.h"
#include "_fake_typedefs.h"

#ifndef EPERM
#define EPERM 1
#endif
#ifndef ENOENT
#define ENOENT 2
#endif
#ifndef EINTR
#define EINTR 4
#endif
#ifndef EIO
#define EIO 5
#endif
#ifndef ENXIO
#define ENXIO 6
#endif
#ifndef EBADF
#define EBADF 9
#endif
#ifndef ECHILD
#define ECHILD 10
#endif
#ifndef ENOTSOCK
#if defined(__linux__)
#define ENOTSOCK 88
#else
#define ENOTSOCK 38
#endif
#endif
#ifndef EDEADLK
#if defined(__linux__)
#define EDEADLK 35
#else
#define EDEADLK 11
#endif
#endif
#ifndef ENOMEM
#define ENOMEM 12
#endif
#ifndef EACCES
#define EACCES 13
#endif
#ifndef EFAULT
#define EFAULT 14
#endif
#ifndef EBUSY
#define EBUSY 16
#endif
#ifndef EEXIST
#define EEXIST 17
#endif
#ifndef ENOTDIR
#define ENOTDIR 20
#endif
#ifndef EISDIR
#define EISDIR 21
#endif
#ifndef EINVAL
#define EINVAL 22
#endif
#ifndef ENFILE
#define ENFILE 23
#endif
#ifndef EMFILE
#define EMFILE 24
#endif
#ifndef ENOSPC
#define ENOSPC 28
#endif
#ifndef EROFS
#define EROFS 30
#endif
#ifndef EPIPE
#define EPIPE 32
#endif
#ifndef ERANGE
#define ERANGE 34
#endif
#ifndef EAGAIN
#if defined(__linux__)
#define EAGAIN 11
#else
#define EAGAIN 35
#endif
#endif
#ifndef EWOULDBLOCK
#define EWOULDBLOCK EAGAIN
#endif
/* Non-blocking connect uses the selected target's kernel error values. */
#ifndef EINPROGRESS
#if defined(__linux__)
#define EINPROGRESS 115
#else
#define EINPROGRESS 36
#endif
#endif
#ifndef EALREADY
#if defined(__linux__)
#define EALREADY 114
#else
#define EALREADY 37
#endif
#endif
#ifndef ENOTSUP
#if defined(__linux__)
#define ENOTSUP 95
#else
#define ENOTSUP 45
#endif
#endif
#ifndef ENAMETOOLONG
#if defined(__linux__)
#define ENAMETOOLONG 36
#else
#define ENAMETOOLONG 63
#endif
#endif
#ifndef ETIMEDOUT
#if defined(__linux__)
#define ETIMEDOUT 110
#else
#define ETIMEDOUT 60
#endif
#endif
#ifndef ENOLCK
#if defined(__linux__)
#define ENOLCK 37
#else
#define ENOLCK 77
#endif
#endif
#ifndef EAUTH
#define EAUTH 80
#endif
#ifndef EOVERFLOW
#if defined(__linux__)
#define EOVERFLOW 75
#else
#define EOVERFLOW 84
#endif
#endif
#ifndef EOPNOTSUPP
#if defined(__linux__)
#define EOPNOTSUPP 95
#else
#define EOPNOTSUPP 102
#endif
#endif

/* Socket completion reports the selected target's exact SO_ERROR value. */
#ifndef ECONNREFUSED
#if defined(__linux__)
#define ECONNREFUSED 111
#else
#define ECONNREFUSED 61
#endif
#endif

/* The owned Linux errno cell is native TLS, shared with the runtime wrappers.
 * Darwin keeps the system accessor. This does not define a Windows CRT ABI. */
#ifndef errno
#if defined(__linux__)
int *pcc_errno_location(void);
#define errno (*pcc_errno_location())
#elif defined(__APPLE__) || defined(__PCC_HOST_DARWIN__)
int *__error(void);
#define errno (*__error())
#endif
#endif
