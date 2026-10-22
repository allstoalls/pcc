/* pcc-owned stub for musl's __uflow.
 *
 * The vendored scan helpers (shgetc/floatscan) reference __uflow, musl's
 * refill hook for a REAL stdio FILE. pcc vendors the scanners only for the
 * strto*-family string path, where sh_fromstring points the buffer at the
 * caller's string and no refill can happen. Vendoring musl's stdio would be
 * LIBC-P2-STDIO-SUBSET's job, so this stub fails closed instead: it reports
 * EOF, which is what the scanners treat as "no more input".
 */
#include "stdio_impl.h"

int __uflow(FILE *f) {
    f->flags |= F_EOF;
    return -1;
}
