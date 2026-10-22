#include <string.h>

/* pcc local patch: prototype for the musl-internal helper. */
void *__memrchr(const void *, int, size_t);

char *strrchr(const char *s, int c)
{
	return __memrchr(s, c, strlen(s) + 1);
}
