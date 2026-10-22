/* pcc local patch: upstream includes musl's libm.h for the
 * shared float helpers; this body is pure bit manipulation, so
 * the two standard headers suffice and musl's endian.h/fp_arch.h
 * closure is not needed. */
#include <math.h>
#include <stdint.h>

double copysign(double x, double y) {
	union {double f; uint64_t i;} ux={x}, uy={y};
	ux.i &= -1ULL/2;
	ux.i |= uy.i & 1ULL<<63;
	return ux.f;
}
