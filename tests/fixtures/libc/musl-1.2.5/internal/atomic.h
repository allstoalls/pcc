/* pcc local replacement for musl's src/internal/atomic.h.
 *
 * musl's atomic.h is a large per-architecture header (its per-arch
 * atomic_arch.h closure) that pcc's vendored tree does not carry — the runtime uses the C
 * kernel's own atomics. The only piece the vendored math sources need is
 * a_clz_64 (fma's normalization step), so this provides exactly that, in the
 * portable form musl itself falls back to.
 */
#ifndef PCC_VENDOR_MUSL_ATOMIC_H
#define PCC_VENDOR_MUSL_ATOMIC_H

#include <stdint.h>

static inline int a_clz_64(uint64_t x) {
    /* Portable fallback (musl's own generic implementation shape): binary
     * search over the halves. x == 0 is undefined upstream too. */
    int r = 0;
    if (!(x >> 32)) { r += 32; x <<= 32; }
    if (!(x >> 48)) { r += 16; x <<= 16; }
    if (!(x >> 56)) { r += 8; x <<= 8; }
    if (!(x >> 60)) { r += 4; x <<= 4; }
    if (!(x >> 62)) { r += 2; x <<= 2; }
    if (!(x >> 63)) { r += 1; }
    return r;
}

static inline int a_clz_32(uint32_t x) {
    int r = 0;
    if (!(x >> 16)) { r += 16; x <<= 16; }
    if (!(x >> 24)) { r += 8; x <<= 8; }
    if (!(x >> 28)) { r += 4; x <<= 4; }
    if (!(x >> 30)) { r += 2; x <<= 2; }
    if (!(x >> 31)) { r += 1; }
    return r;
}

#endif
