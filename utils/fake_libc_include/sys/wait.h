#ifndef _FAKE_SYS_WAIT_H
#define _FAKE_SYS_WAIT_H

#include "_fake_defines.h"
#include "_fake_typedefs.h"

pid_t waitpid(pid_t pid, int *status, int options);

#ifndef WNOHANG
#define WNOHANG 1
#endif
#ifndef WUNTRACED
#define WUNTRACED 2
#endif

#define WIFEXITED(status) (((status) & 0x7f) == 0)
#define WEXITSTATUS(status) (((status) >> 8) & 0xff)
#define WIFSIGNALED(status) (((status) & 0x7f) != 0 && ((status) & 0x7f) != 0x7f)
#define WTERMSIG(status) ((status) & 0x7f)

#endif
