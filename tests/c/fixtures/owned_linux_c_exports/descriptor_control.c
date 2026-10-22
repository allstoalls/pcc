#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
int main(void) {
    int flags, duplicate;
    errno = 123;
    flags = fcntl(1, F_GETFD);
    if (flags < 0 || errno != 123) return 10;
    duplicate = fcntl(1, F_DUPFD, 10);
    if (duplicate < 10 || errno != 123) return 11;
    if (fcntl(duplicate, F_SETFD, FD_CLOEXEC) != 0) return 12;
    if (fcntl(duplicate, F_GETFD) != FD_CLOEXEC) return 13;
    flags = fcntl(duplicate, F_GETFL);
    if (flags < 0) return 14;
    if (fcntl(duplicate, F_SETFL, flags | O_NONBLOCK) != 0) return 15;
    if ((fcntl(duplicate, F_GETFL) & O_NONBLOCK) == 0) return 16;
    if (fcntl(duplicate, F_SETFL, flags) != 0) return 17;
    errno = 123;
    if (close(duplicate) != 0 || errno != 123) return 18;
    if (close(duplicate) != -1 || errno != 9) return 19;
    errno = 123;
    if (fcntl(-1, F_GETFD) != -1 || errno != 9) return 20;
    errno = 123;
    if (fcntl(1, 2147483647) != -1 || errno != 22) return 21;
    return 0;
}
