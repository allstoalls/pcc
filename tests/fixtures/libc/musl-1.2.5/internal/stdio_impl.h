/* pcc local replacement for musl's src/internal/stdio_impl.h.
 *
 * The upstream header pulls in musl's syscall layer, which pcc's vendored
 * tree deliberately does not carry (macOS keeps libSystem for syscalls).
 * The float/int scanners only need the FILE buffer fields and the shgetc
 * plumbing, so this declares exactly that subset. The struct layout is
 * private to the vendored scan translation units — nothing outside them
 * sees this FILE.
 */
#ifndef PCC_VENDOR_MUSL_STDIO_IMPL_H
#define PCC_VENDOR_MUSL_STDIO_IMPL_H

#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

#define UNGET 8

struct _pcc_musl_scan_file {
    unsigned flags;
    unsigned char *rpos, *rend;
    int (*close)(struct _pcc_musl_scan_file *);
    unsigned char *wend, *wpos;
    unsigned char *mustbezero_1;
    unsigned char *wbase;
    size_t (*read)(struct _pcc_musl_scan_file *, unsigned char *, size_t);
    size_t (*write)(struct _pcc_musl_scan_file *, const unsigned char *, size_t);
    off_t (*seek)(struct _pcc_musl_scan_file *, off_t, int);
    unsigned char *buf;
    size_t buf_size;
    struct _pcc_musl_scan_file *prev, *next;
    int fd;
    int pipe_pid;
    long lockcount;
    int mode;
    volatile int lock;
    int lbf;
    void *cookie;
    off_t off;
    char *getln_buf;
    void *mustbezero_2;
    unsigned char *shend;
    off_t shlim, shcnt;
    struct _pcc_musl_scan_file *prev_locked, *next_locked;
    struct __locale_struct *locale;
};

#define FILE struct _pcc_musl_scan_file
#define F_EOF 16
#define F_ERR 32

#endif
