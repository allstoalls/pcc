#include "_fake_defines.h"
#include "_fake_typedefs.h"

char *getenv(const char *name);
char *mkdtemp(char *template);
int system(const char *command);

int atoi(const char *text);
void exit(int status);
void _Exit(int status);
int atexit(void (*callback)(void));
