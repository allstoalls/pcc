#include "_fake_defines.h"
#include "_fake_typedefs.h"

/* C99 7.12: the evaluation types. FLT_EVAL_METHOD == 0 on arm64/x86_64
 * darwin, so they are plain float/double. */
typedef float float_t;
typedef double double_t;

/* C99 <math.h> floating-point constants and classification macros.
 * pcc's C frontend lowers the matching __builtin_* forms directly
 * (see pcc/frontends/c/codegen/c_codegen.py), so map the standard names onto them
 * instead of leaving them undeclared. */
#ifndef INFINITY
#define INFINITY (__builtin_inff())
#endif
#ifndef NAN
#define NAN (__builtin_nanf(""))
#endif
#ifndef HUGE_VAL
#define HUGE_VAL (__builtin_huge_val())
#endif
#ifndef HUGE_VALF
#define HUGE_VALF (__builtin_huge_valf())
#endif

#ifndef isnan
#define isnan(x) __builtin_isnan(x)
#endif
#ifndef isinf
#define isinf(x) __builtin_isinf(x)
#endif
#ifndef isfinite
#define isfinite(x) __builtin_isfinite(x)
#endif
#ifndef signbit
#define signbit(x) __builtin_signbit(x)
#endif

/* ldexp's siblings; a missing prototype sends the call through the
 * implicit-int path and the double result comes back bit-cast from an
 * integer register (see BUG-P1-CC-SCALBN-RETURNS-INT-PARAM). */
double scalbn(double x, int n);
float scalbnf(float x, int n);
double scalbln(double x, long n);
