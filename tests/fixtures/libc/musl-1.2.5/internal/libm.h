/* pcc local replacement for musl's src/internal/libm.h.
 *
 * Upstream libm.h pulls endian.h, fp_arch.h and the long-double layout
 * machinery that pcc's vendored tree deliberately does not carry. The pow
 * closure (pow.c, exp_data.c, pow_data.c, __math_*.c) needs only the double
 * bit-cast helpers, the evaluation/barrier helpers, the prediction macros and
 * the error-path prototypes, so this declares exactly that subset. Values and
 * semantics follow upstream; only the include closure is reduced.
 */
#ifndef PCC_VENDOR_MUSL_LIBM_H
#define PCC_VENDOR_MUSL_LIBM_H

#include <stdint.h>
#include <math.h>
#include <float.h>
#include "pcc_musl_features.h"

#define WANT_ROUNDING 1
#define WANT_SNAN 0
#define issignalingf_inline(x) 0
#define issignaling_inline(x) 0
#define TOINT_INTRINSICS 0

#define predict_true(x) (x)
#define predict_false(x) (x)

static inline uint32_t asuint(float f) {
    union { float f; uint32_t i; } u = {f};
    return u.i;
}

static inline float asfloat(uint32_t i) {
    union { uint32_t i; float f; } u = {i};
    return u.f;
}

static inline uint64_t asuint64(double f) {
    union { double f; uint64_t i; } u = {f};
    return u.i;
}

static inline double asdouble(uint64_t i) {
    union { uint64_t i; double f; } u = {i};
    return u.f;
}

static inline float eval_as_float(float x) { return x; }
static inline double eval_as_double(double x) { return x; }
static inline float fp_barrierf(float x) { volatile float y = x; return y; }
static inline double fp_barrier(double x) { volatile double y = x; return y; }

static inline void fp_force_evalf(float x) { volatile float y; y = x; (void)y; }
static inline void fp_force_eval(double x) { volatile double y; y = x; (void)y; }

#define FORCE_EVAL(x) fp_force_eval(x)

hidden double __math_xflow(uint32_t, double);
hidden double __math_uflow(uint32_t);
hidden double __math_oflow(uint32_t);
hidden double __math_divzero(uint32_t);
hidden double __math_invalid(double);
hidden float __math_xflowf(uint32_t, float);
hidden float __math_uflowf(uint32_t);
hidden float __math_oflowf(uint32_t);
hidden float __math_divzerof(uint32_t);
hidden float __math_invalidf(float);

#endif
