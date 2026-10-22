#include "_fake_defines.h"
#include "_fake_typedefs.h"

/* IEEE-754 limits. Values are the host SDK's (verified by a cc oracle in
 * tests/python/test_musl_string_differential.py's sibling float-limits check);
 * on darwin-arm64 long double is the same 64-bit double as double. */
#define FLT_RADIX 2

#define FLT_MANT_DIG 24
#define FLT_DIG 6
#define FLT_MIN_EXP (-125)
#define FLT_MAX_EXP 128
#define FLT_MIN 1.17549435082228750797e-38F
#define FLT_MAX 3.40282346638528859812e+38F
#define FLT_EPSILON 1.1920928955078125e-07F

#define DBL_MANT_DIG 53
#define DBL_DIG 15
#define DBL_MIN_EXP (-1021)
#define DBL_MAX_EXP 1024
#define DBL_MIN 2.22507385850720138309e-308
#define DBL_MAX 1.79769313486231570815e+308
#define DBL_EPSILON 2.22044604925031308085e-16

#define LDBL_MANT_DIG DBL_MANT_DIG
#define LDBL_DIG DBL_DIG
#define LDBL_MIN_EXP DBL_MIN_EXP
#define LDBL_MAX_EXP DBL_MAX_EXP
#define LDBL_MIN DBL_MIN
#define LDBL_MAX DBL_MAX
#define LDBL_EPSILON DBL_EPSILON

/* Remaining C11 5.2.4.2.2 characteristics, from the owned IEEE predefines. */
#ifndef FLT_EVAL_METHOD
#define FLT_EVAL_METHOD 0
#endif
#ifndef FLT_MIN_10_EXP
#define FLT_MIN_10_EXP __FLT_MIN_10_EXP__
#endif
#ifndef FLT_MAX_10_EXP
#define FLT_MAX_10_EXP __FLT_MAX_10_EXP__
#endif
#ifndef FLT_DECIMAL_DIG
#define FLT_DECIMAL_DIG __FLT_DECIMAL_DIG__
#endif
#ifndef FLT_TRUE_MIN
#define FLT_TRUE_MIN __FLT_DENORM_MIN__
#endif
#ifndef FLT_HAS_SUBNORM
#define FLT_HAS_SUBNORM 1
#endif
#ifndef DBL_MIN_10_EXP
#define DBL_MIN_10_EXP __DBL_MIN_10_EXP__
#endif
#ifndef DBL_MAX_10_EXP
#define DBL_MAX_10_EXP __DBL_MAX_10_EXP__
#endif
#ifndef DBL_DECIMAL_DIG
#define DBL_DECIMAL_DIG __DBL_DECIMAL_DIG__
#endif
#ifndef DBL_TRUE_MIN
#define DBL_TRUE_MIN __DBL_DENORM_MIN__
#endif
#ifndef DBL_HAS_SUBNORM
#define DBL_HAS_SUBNORM 1
#endif
#ifndef LDBL_MIN_10_EXP
#define LDBL_MIN_10_EXP DBL_MIN_10_EXP
#endif
#ifndef LDBL_MAX_10_EXP
#define LDBL_MAX_10_EXP DBL_MAX_10_EXP
#endif
#ifndef LDBL_DECIMAL_DIG
#define LDBL_DECIMAL_DIG DBL_DECIMAL_DIG
#endif
#ifndef LDBL_TRUE_MIN
#define LDBL_TRUE_MIN DBL_TRUE_MIN
#endif
#ifndef LDBL_HAS_SUBNORM
#define LDBL_HAS_SUBNORM 1
#endif
#ifndef DECIMAL_DIG
#define DECIMAL_DIG DBL_DECIMAL_DIG
#endif
