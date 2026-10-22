#ifndef _FAKE_FCNTL_H
#define _FAKE_FCNTL_H

#include "_fake_defines.h"
#include "_fake_typedefs.h"

#ifndef O_RDONLY
#define O_RDONLY 0
#endif

#ifndef O_WRONLY
#define O_WRONLY 1
#endif

#ifndef O_RDWR
#define O_RDWR 2
#endif

#ifndef O_APPEND
#define O_APPEND 8
#endif

#ifndef O_CREAT
#if defined(__APPLE__) || defined(__PCC_HOST_DARWIN__)
#define O_CREAT 512
#else
#define O_CREAT 64
#endif
#endif

#ifndef O_TRUNC
#if defined(__APPLE__) || defined(__PCC_HOST_DARWIN__)
#define O_TRUNC 1024
#else
#define O_TRUNC 512
#endif
#endif

#ifndef O_EXCL
#if defined(__APPLE__) || defined(__PCC_HOST_DARWIN__)
#define O_EXCL 2048
#else
#define O_EXCL 128
#endif
#endif

#ifndef O_CLOEXEC
#if defined(__APPLE__) || defined(__PCC_HOST_DARWIN__)
#define O_CLOEXEC 0x1000000
#else
#define O_CLOEXEC 0x80000
#endif
#endif

#ifndef O_BINARY
#define O_BINARY 0
#endif

#ifndef O_NONBLOCK
#if defined(__linux__)
#define O_NONBLOCK 2048
#else
#define O_NONBLOCK 4
#endif
#endif

#ifndef O_NDELAY
#define O_NDELAY O_NONBLOCK
#endif

#ifndef O_NOFOLLOW
#define O_NOFOLLOW 0
#endif

#ifndef O_LARGEFILE
#define O_LARGEFILE 0
#endif

#ifndef F_DUPFD
#define F_DUPFD 0
#endif

#ifndef F_GETFD
#define F_GETFD 1
#endif

#ifndef F_SETFD
#define F_SETFD 2
#endif

#ifndef F_GETFL
#define F_GETFL 3
#endif

#ifndef F_SETFL
#define F_SETFL 4
#endif

#ifndef F_GETLK
#define F_GETLK 7
#endif

#ifndef F_SETLK
#define F_SETLK 8
#endif

#ifndef F_SETLKW
#define F_SETLKW 9
#endif

#ifndef F_FULLFSYNC
#define F_FULLFSYNC 51
#endif

#ifndef FD_CLOEXEC
#define FD_CLOEXEC 1
#endif

#ifndef F_RDLCK
#define F_RDLCK 1
#endif

#ifndef F_UNLCK
#define F_UNLCK 2
#endif

#ifndef F_WRLCK
#define F_WRLCK 3
#endif

struct flock {
    off_t l_start;
    off_t l_len;
    pid_t l_pid;
    short l_type;
    short l_whence;
};

int open(const char *path, int oflag, ...);
int fcntl(int fd, int cmd, ...);

#endif
