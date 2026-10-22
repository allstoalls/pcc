#include <stdio.h>
#include <stdlib.h>
#include <errno.h>
int main(int argc, char **argv) {
    FILE *good = fopen(argv[1], "wb");
    FILE *bad = fopen("/dev/full", "wb");
    if (!good || !bad) return 10;
    if (fwrite("saved", 1, 5, good) != 5 || fwrite("x", 1, 1, bad) != 1) return 11;
    errno = 0;
    if (fflush(NULL) != -1 || errno != 28) return 12;
    if (!ferror(bad)) return 13;
    _Exit(0);
    return 14;
}
