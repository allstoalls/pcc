#ifndef __PCC_STDINT_H
#define __PCC_STDINT_H

#include "_fake_defines.h"
#include "_fake_typedefs.h"

/* C99 7.18.2: limits have the types of the promoted integer typedefs. */
#define INT8_MAX __INT8_MAX__
#define INT8_MIN (-INT8_MAX - 1)
#define UINT8_MAX __UINT8_MAX__
#define INT16_MAX __INT16_MAX__
#define INT16_MIN (-INT16_MAX - 1)
#define UINT16_MAX __UINT16_MAX__
#define INT32_MAX __INT32_MAX__
#define INT32_MIN (-INT32_MAX - 1)
#define UINT32_MAX __UINT32_MAX__
#define INT64_MAX __INT64_MAX__
#define INT64_MIN (-INT64_MAX - 1)
#define UINT64_MAX __UINT64_MAX__

/* The bundled least/fast typedefs use the corresponding exact-width types. */
#define INT_LEAST8_MAX INT8_MAX
#define INT_LEAST8_MIN INT8_MIN
#define UINT_LEAST8_MAX UINT8_MAX
#define INT_LEAST16_MAX INT16_MAX
#define INT_LEAST16_MIN INT16_MIN
#define UINT_LEAST16_MAX UINT16_MAX
#define INT_LEAST32_MAX INT32_MAX
#define INT_LEAST32_MIN INT32_MIN
#define UINT_LEAST32_MAX UINT32_MAX
#define INT_LEAST64_MAX INT64_MAX
#define INT_LEAST64_MIN INT64_MIN
#define UINT_LEAST64_MAX UINT64_MAX
#define INT_FAST8_MAX INT8_MAX
#define INT_FAST8_MIN INT8_MIN
#define UINT_FAST8_MAX UINT8_MAX
#define INT_FAST16_MAX INT16_MAX
#define INT_FAST16_MIN INT16_MIN
#define UINT_FAST16_MAX UINT16_MAX
#define INT_FAST32_MAX INT32_MAX
#define INT_FAST32_MIN INT32_MIN
#define UINT_FAST32_MAX UINT32_MAX
#define INT_FAST64_MAX INT64_MAX
#define INT_FAST64_MIN INT64_MIN
#define UINT_FAST64_MAX UINT64_MAX

#define INTPTR_MAX __INTPTR_MAX__
#define INTPTR_MIN (-INTPTR_MAX - 1)
#define UINTPTR_MAX __UINTPTR_MAX__
#define INTMAX_MAX __INTMAX_MAX__
#define INTMAX_MIN (-INTMAX_MAX - 1)
#define UINTMAX_MAX __UINTMAX_MAX__

/* C99 7.18.3: limits of the other integer types supplied by these headers. */
#define PTRDIFF_MAX __PTRDIFF_MAX__
#define PTRDIFF_MIN (-PTRDIFF_MAX - 1)
#define SIZE_MAX __SIZE_MAX__
#define SIG_ATOMIC_MAX __SIG_ATOMIC_MAX__
#define SIG_ATOMIC_MIN (-SIG_ATOMIC_MAX - 1)
#define WCHAR_MAX __WCHAR_MAX__
#if __WCHAR_MAX__ == __UINT16_MAX__ || __WCHAR_MAX__ == __UINT32_MAX__
#define WCHAR_MIN 0
#else
#define WCHAR_MIN (-WCHAR_MAX - 1)
#endif
#define WINT_MAX __WINT_MAX__
#define WINT_MIN (-WINT_MAX - 1)

/* C99 7.18.4: constants match the promoted least-width/max-width types.
 * In particular, uint_least8_t and uint_least16_t promote to signed int. */
#define INT8_C(value) value
#define UINT8_C(value) value
#define INT16_C(value) value
#define UINT16_C(value) value
#define INT32_C(value) value
#define UINT32_C(value) value ## U
#if __SIZEOF_LONG__ == 8
#define INT64_C(value) value ## L
#define UINT64_C(value) value ## UL
#define INTMAX_C(value) value ## L
#define UINTMAX_C(value) value ## UL
#else
#define INT64_C(value) value ## LL
#define UINT64_C(value) value ## ULL
#define INTMAX_C(value) value ## LL
#define UINTMAX_C(value) value ## ULL
#endif

#endif
