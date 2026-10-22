#ifndef _FAKE_TYPEDEFS_H
#define _FAKE_TYPEDEFS_H

typedef __SIZE_TYPE__ size_t;
#include "__pcc_builtin_va_list.h"
typedef __builtin_va_list __gnuc_va_list;
typedef __builtin_va_list va_list;
typedef __builtin_va_list __darwin_va_list;
typedef signed char __int8_t;
typedef unsigned char __uint8_t;
typedef short __int16_t;
typedef unsigned short __uint16_t;
typedef short __int_least16_t;
typedef unsigned short __uint_least16_t;
typedef int __int32_t;
typedef unsigned int __uint32_t;
typedef __INT64_TYPE__ __int64_t;
typedef __UINT64_TYPE__ __uint64_t;
typedef int __int_least32_t;
typedef unsigned int __uint_least32_t;
typedef int _LOCK_T;
typedef int _LOCK_RECURSIVE_T;
typedef int _off_t;
typedef int __dev_t;
typedef int __uid_t;
typedef int __gid_t;
typedef int _off64_t;
typedef int _fpos_t;
typedef __PTRDIFF_TYPE__ _ssize_t;
typedef int wint_t;
typedef int wctype_t;
typedef int _mbstate_t;
typedef int _flock_t;
typedef int _iconv_t;
typedef int __ULong;
typedef char __FILE;
typedef __PTRDIFF_TYPE__ ptrdiff_t;
typedef __WCHAR_TYPE__ wchar_t;
typedef int __off_t;
typedef int __pid_t;
typedef int __loff_t;
typedef unsigned char u_char;
typedef unsigned short u_short;
typedef unsigned int u_int;
typedef unsigned long u_long;
typedef unsigned short ushort;
typedef unsigned int uint;
#if defined(__LP64__) || defined(_LP64) || defined(__x86_64__) || defined(__aarch64__)
typedef long clock_t;
#if defined(_WIN64)
typedef long long time_t;
#else
typedef long time_t;
#endif
#else
typedef int clock_t;
typedef int time_t;
#endif
typedef int daddr_t;
typedef int caddr_t;
typedef unsigned long ino_t;
typedef long off_t;
typedef int dev_t;
typedef int uid_t;
typedef int gid_t;
typedef int pid_t;
typedef int key_t;
typedef __PTRDIFF_TYPE__ ssize_t;
typedef unsigned short mode_t;
typedef unsigned short nlink_t;
typedef long blkcnt_t;
typedef int blksize_t;
typedef int fd_mask;
typedef int _types_fd_set;
typedef int clockid_t;
typedef int timer_t;
typedef int useconds_t;
typedef int suseconds_t;
typedef char FILE;
typedef int fpos_t;
typedef int cookie_read_function_t;
typedef int cookie_write_function_t;
typedef int cookie_seek_function_t;
typedef int cookie_close_function_t;
typedef int cookie_io_functions_t;
typedef int div_t;
typedef int ldiv_t;
typedef int lldiv_t;
#if defined(__linux__) || defined(_WIN32)
/* Layout shared by the owned signal adapters; kernel conversion is internal. */
typedef struct { unsigned long long __pcc_signal_bits[16]; } sigset_t;
typedef sigset_t __sigset_t;
#else
typedef int sigset_t;
typedef int __sigset_t;
#endif
typedef int _sig_func_ptr;
typedef int sig_atomic_t;
typedef int __tzrule_type;
typedef int __tzinfo_type;
typedef int mbstate_t;
typedef int sem_t;
#if defined(__linux__) || defined(_WIN32)
/* pcc-owned zero-libc thread handles, not a host pthread library ABI. */
typedef void *pthread_t;
#else
typedef int pthread_t;
#endif
typedef int pthread_attr_t;
#if defined(_WIN32)
typedef struct { void *__pcc_lock; } pthread_mutex_t;
typedef struct { void *__pcc_condition; } pthread_cond_t;
#else
typedef int pthread_mutex_t;
typedef int pthread_cond_t;
#endif
typedef int pthread_mutexattr_t;
typedef int pthread_condattr_t;
typedef int pthread_key_t;
typedef int pthread_once_t;
typedef int pthread_rwlock_t;
typedef int pthread_rwlockattr_t;
typedef int pthread_spinlock_t;
typedef int pthread_barrier_t;
typedef int pthread_barrierattr_t;
typedef int rlim_t;
typedef int stack_t;
typedef int siginfo_t;
typedef int z_stream;

/* C99 exact-width integer types */
typedef signed char int8_t;
typedef unsigned char uint8_t;
typedef short int16_t;
typedef unsigned short uint16_t;
typedef int int32_t;
typedef unsigned int uint32_t;
typedef __INT64_TYPE__ int64_t;
typedef __UINT64_TYPE__ uint64_t;

/* C99 minimum-width integer types */
typedef signed char int_least8_t;
typedef unsigned char uint_least8_t;
typedef short int_least16_t;
typedef unsigned short uint_least16_t;
typedef int int_least32_t;
typedef unsigned int uint_least32_t;
typedef __INT_LEAST64_TYPE__ int_least64_t;
typedef __UINT_LEAST64_TYPE__ uint_least64_t;

/* C99 fastest minimum-width integer types */
typedef signed char int_fast8_t;
typedef unsigned char uint_fast8_t;
typedef short int_fast16_t;
typedef unsigned short uint_fast16_t;
typedef int int_fast32_t;
typedef unsigned int uint_fast32_t;
typedef __INT_FAST64_TYPE__ int_fast64_t;
typedef __UINT_FAST64_TYPE__ uint_fast64_t;

/* C99 integer types capable of holding object pointers */
typedef __INTPTR_TYPE__ intptr_t;
typedef __UINTPTR_TYPE__ uintptr_t;

/* C99 greatest-width integer types */
typedef __INTMAX_TYPE__ intmax_t;
typedef __UINTMAX_TYPE__ uintmax_t;

/* C99 stdbool.h bool type. _Bool is built-in in C99 */
typedef _Bool bool;

#endif
