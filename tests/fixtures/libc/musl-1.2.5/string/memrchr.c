#include <string.h>

void *__memrchr(const void *m, int c, size_t n)
{
	const unsigned char *s = m;
	c = (unsigned char)c;
	while (n--) if (s[n]==c) return (void *)(s+n);
	return 0;
}

/* pcc local patch: plain wrapper instead of weak_alias. */
void *memrchr(const void *m, int c, size_t n)
{
	return __memrchr(m, c, n);
}
