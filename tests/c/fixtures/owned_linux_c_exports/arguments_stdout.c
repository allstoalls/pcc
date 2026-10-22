#include <stdlib.h>
#include <stdio.h>
int helper(char **argv) {
    if (atoi(" \t\r\n\v\f-2147483648!") != (-2147483647 - 1)) return 91;
    if (atoi("+2147483647tail") != 2147483647) return 92;
    if (atoi("-") != 0 || atoi("word") != 0 || atoi("") != 0) return 93;
    if (atoi("0017") != 17 || atoi("  +42!") != 42) return 94;
    if (puts("visible output") < 0 || puts("") < 0) return 95;
    return atoi(argv[1]);
}
int main(int argc, char **argv) { return helper(argv) + argc; }
