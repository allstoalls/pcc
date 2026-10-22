#ifndef _FAKE_UNISTD_H
#define _FAKE_UNISTD_H

#include "_fake_defines.h"
#include "_fake_typedefs.h"

ssize_t read(int fd, void *buf, size_t count);
ssize_t write(int fd, const void *buf, size_t count);
int close(int fd);
int pipe(int pipefd[2]);
pid_t fork(void);
void _exit(int status);
char *getcwd(char *buf, size_t size);
off_t lseek(int fd, off_t offset, int whence);
int isatty(int fd);
int fsync(int fd);
int ftruncate(int fd, off_t length);
ssize_t pread(int fd, void *buf, size_t count, off_t offset);
ssize_t pwrite(int fd, const void *buf, size_t count, off_t offset);
int unlink(const char *path);
int rmdir(const char *path);
ssize_t readlink(const char *restrict path, char *restrict buf, size_t bufsize);
pid_t getpid(void);
uid_t geteuid(void);
int fchown(int fd, uid_t owner, gid_t group);
long sysconf(int name);
int getpagesize(void);
unsigned int sleep(unsigned int seconds);
unsigned int alarm(unsigned int seconds);
int usleep(useconds_t usec);
int mkstemp(char *template);
int access(const char *path, int amode);

#ifndef F_OK
#define F_OK 0
#endif
#ifndef X_OK
#define X_OK 1
#endif
#ifndef W_OK
#define W_OK 2
#endif
#ifndef R_OK
#define R_OK 4
#endif
#ifndef _SC_PAGESIZE
#define _SC_PAGESIZE 29
#endif
#ifndef _SC_NPROCESSORS_ONLN
#define _SC_NPROCESSORS_ONLN 58
#endif

#endif
