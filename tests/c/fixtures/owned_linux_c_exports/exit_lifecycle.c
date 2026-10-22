#include <stdio.h>
#include <stdlib.h>
static FILE *output;
static int calls;
static void first(void) {
    if (calls != 40) _Exit(90);
    fwrite("A", 1, 1, output);
    puts("first");
}
static void late(void) { fwrite("C", 1, 1, output); puts("late"); }
static void second(void) {
    fwrite("B", 1, 1, output);
    puts("last");
    if (atexit(late) != 0) _Exit(91);
}
static void many(void) { calls += 1; }
int main(int argc, char **argv) {
    int index;
    output = fopen(argv[1], "wb");
    if (!output || fwrite("M", 1, 1, output) != 1) return 92;
    if (atexit(first) != 0 || atexit(second) != 0) return 93;
    for (index = 0; index < 40; index += 1) if (atexit(many) != 0) return 94;
    if (argc > 2) exit(37);
    return 37;
}
