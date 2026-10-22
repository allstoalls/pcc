#include <string.h>

/* pcc local patch: musl declares this in its internal string.h;
 * without a prototype pcc lowers the call as implicit int and
 * truncates the returned pointer. */
char *__strchrnul(const char *, int);

char *strchr(const char *s, int c)
{
	char *r = __strchrnul(s, c);
	return *(unsigned char *)r == (unsigned char)c ? r : 0;
}
