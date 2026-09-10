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
#ifndef EDEADLK
#define EDEADLK 11
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
#define EAGAIN 35
#endif
#ifndef EWOULDBLOCK
#define EWOULDBLOCK EAGAIN
#endif
/* py_asyncio_io.c tests a non-blocking connect() against these two alongside
 * EWOULDBLOCK; without them the pcc-C runtime archive could not be built at
 * all. Darwin values, matching the EAGAIN=35 line above. */
#ifndef EINPROGRESS
#define EINPROGRESS 36
#endif
#ifndef EALREADY
#define EALREADY 37
#endif
#ifndef ENOTSUP
#define ENOTSUP 45
#endif
#ifndef ENAMETOOLONG
#define ENAMETOOLONG 63
#endif
#ifndef ETIMEDOUT
#define ETIMEDOUT 60
#endif
#ifndef ENOLCK
#define ENOLCK 77
#endif
#ifndef EAUTH
#define EAUTH 80
#endif
#ifndef EOVERFLOW
#define EOVERFLOW 84
#endif
#ifndef EOPNOTSUPP
#define EOPNOTSUPP 102
#endif
