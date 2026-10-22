#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
static void callback(void) { puts("unexpected callback"); }
int main(int argc, char **argv) {
    FILE *output = fopen(argv[1], "wb");
    if (!output || fwrite("unflushed", 1, 9, output) != 9) return 10;
    if (atexit(callback) != 0) return 11;
    if (argc > 2) _exit(29);
    _Exit(29);
    return 12;
}
