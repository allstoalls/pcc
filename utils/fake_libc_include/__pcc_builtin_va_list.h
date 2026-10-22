#ifndef __PCC_BUILTIN_VA_LIST_H
#define __PCC_BUILTIN_VA_LIST_H

#if defined(__linux__) && defined(__aarch64__)
typedef struct {
    void *__stack;
    void *__gr_top;
    void *__vr_top;
    int __gr_offs;
    int __vr_offs;
} __builtin_va_list;
#elif defined(__linux__) && defined(__x86_64__)
typedef struct {
    unsigned int gp_offset;
    unsigned int fp_offset;
    void *overflow_arg_area;
    void *reg_save_area;
} __builtin_va_list[1];
#else
typedef char *__builtin_va_list;
#endif

#endif
