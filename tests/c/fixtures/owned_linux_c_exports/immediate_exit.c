#include <stdlib.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc == 2) _exit(-19);
    _Exit(275);
    return 99;
}
