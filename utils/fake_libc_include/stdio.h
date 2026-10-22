#ifndef _FAKE_STDIO_H
#define _FAKE_STDIO_H

#include "_fake_defines.h"
#include "_fake_typedefs.h"

/* setvbuf modes (C11 7.21.1p3); the values match glibc, musl and Darwin. */
#define _IOFBF 0
#define _IOLBF 1
#define _IONBF 2

#ifdef __APPLE__
extern FILE *__stdinp;
extern FILE *__stdoutp;
extern FILE *__stderrp;
#define stdin __stdinp
#define stdout __stdoutp
#define stderr __stderrp
#else
extern FILE *stdin;
extern FILE *stdout;
extern FILE *stderr;
#endif

int getc_unlocked(FILE *stream);
void flockfile(FILE *stream);
void funlockfile(FILE *stream);
int printf(const char *restrict format, ...);
int fflush(FILE *stream);
int fileno(FILE *stream);
int fseeko(FILE *stream, off_t offset, int whence);
off_t ftello(FILE *stream);
FILE *popen(const char *command, const char *mode);
int pclose(FILE *stream);
FILE *open_memstream(char **bufp, size_t *sizep);

#endif
