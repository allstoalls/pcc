/* pcc local shim for musl's src/internal/features.h.
 *
 * musl marks internal symbols with `hidden` (an ELF visibility attribute).
 * pcc's C frontend has no visibility attribute, and these vendored objects
 * are archived (not exported through a dylib), so the qualifier is a no-op.
 */
#ifndef PCC_VENDOR_MUSL_FEATURES_H
#define PCC_VENDOR_MUSL_FEATURES_H
int *__error(void);
#undef errno
/* darwin's errno is (*__error()); the fake libc's `extern int errno` would
 * add a fresh libc import, so use the platform idiom and reuse __error. */
#define errno (*__error())

#define hidden
#define weak
#define weak_alias(old, new) extern __typeof(old) new
#endif
