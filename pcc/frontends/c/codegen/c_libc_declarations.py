"""C frontend libc declaration registry and ABI type projection."""

from __future__ import annotations

from pcc.frontends.c.c_libc_registry import LibcSignature, iter_signatures
from pcc.ir.compat import ir_c as ir

from pcc.frontends.c.codegen.c_types import cstring, double_t as _double, float_t as _float, int8_t, int32_t, int64_t, void_t as _VOID, voidptr_t


# Libc function signature registry: name -> (return_type, [param_types], var_arg)
# Covers: stdio.h, stdlib.h, string.h, ctype.h, math.h, unistd.h, time.h
_FILE_ptr = voidptr_t  # FILE* modeled as opaque void*
_size_t = int64_t
_time_t = int64_t

_LEGACY_LIBC_FUNCTIONS = {
    # === stdio.h ===
    "sprintf": (int32_t, [cstring, cstring], True),
    "snprintf": (int32_t, [cstring, _size_t, cstring], True),
    "vprintf": (int32_t, [cstring, voidptr_t], False),
    "vfprintf": (int32_t, [_FILE_ptr, cstring, voidptr_t], False),
    "vsprintf": (int32_t, [cstring, cstring, voidptr_t], False),
    "vsnprintf": (int32_t, [cstring, _size_t, cstring, voidptr_t], False),
    "scanf": (int32_t, [cstring], True),
    "fscanf": (int32_t, [_FILE_ptr, cstring], True),
    "sscanf": (int32_t, [cstring, cstring], True),
    "fopen": (_FILE_ptr, [cstring, cstring], False),
    # POSIX memory stream: without this prototype the call was implicitly
    # declared int and the returned FILE* lost its upper 32 bits
    # (libpy_runtime_pcc.a str(container) crash, 2026-07-31).
    "open_memstream": (_FILE_ptr, [voidptr_t, voidptr_t], False),
    "fclose": (int32_t, [_FILE_ptr], False),
    "fread": (_size_t, [voidptr_t, _size_t, _size_t, _FILE_ptr], False),
    "fwrite": (_size_t, [voidptr_t, _size_t, _size_t, _FILE_ptr], False),
    "fseek": (int32_t, [_FILE_ptr, int64_t, int32_t], False),
    "ftell": (int64_t, [_FILE_ptr], False),
    "rewind": (_VOID, [_FILE_ptr], False),
    "feof": (int32_t, [_FILE_ptr], False),
    "ferror": (int32_t, [_FILE_ptr], False),
    "fflush": (int32_t, [_FILE_ptr], False),
    "fgets": (cstring, [cstring, int32_t, _FILE_ptr], False),
    "fputs": (int32_t, [cstring, _FILE_ptr], False),
    "fgetc": (int32_t, [_FILE_ptr], False),
    "fputc": (int32_t, [int32_t, _FILE_ptr], False),
    "getc": (int32_t, [_FILE_ptr], False),
    "getc_unlocked": (int32_t, [_FILE_ptr], False),
    "putc": (int32_t, [int32_t, _FILE_ptr], False),
    "getchar": (int32_t, [], False),
    "putchar": (int32_t, [int32_t], False),
    "ungetc": (int32_t, [int32_t, _FILE_ptr], False),
    "flockfile": (_VOID, [_FILE_ptr], False),
    "funlockfile": (_VOID, [_FILE_ptr], False),
    "puts": (int32_t, [cstring], False),
    "perror": (_VOID, [cstring], False),
    "remove": (int32_t, [cstring], False),
    "rename": (int32_t, [cstring, cstring], False),
    "fseeko": (int32_t, [_FILE_ptr, int64_t, int32_t], False),
    "ftello": (int64_t, [_FILE_ptr], False),
    # === stdlib.h ===
    "calloc": (voidptr_t, [_size_t, _size_t], False),
    "realloc": (voidptr_t, [voidptr_t, _size_t], False),
    "exit": (_VOID, [int32_t], False),
    "_Exit": (_VOID, [int32_t], False),
    "abort": (_VOID, [], False),
    "atexit": (int32_t, [voidptr_t], False),
    "abs": (int32_t, [int32_t], False),
    "labs": (int64_t, [int64_t], False),
    "llabs": (int64_t, [int64_t], False),
    "imaxabs": (int64_t, [int64_t], False),
    "atoi": (int32_t, [cstring], False),
    "atol": (int64_t, [cstring], False),
    "atof": (_double, [cstring], False),
    "strtol": (int64_t, [cstring, voidptr_t, int32_t], False),
    "strtoul": (int64_t, [cstring, voidptr_t, int32_t], False),
    "strtod": (_double, [cstring, voidptr_t], False),
    "strtof": (_double, [cstring, voidptr_t], False),
    "rand": (int32_t, [], False),
    "srand": (_VOID, [int32_t], False),
    "qsort": (_VOID, [voidptr_t, _size_t, _size_t, voidptr_t], False),
    "bsearch": (voidptr_t, [voidptr_t, voidptr_t, _size_t, _size_t, voidptr_t], False),
    "getenv": (cstring, [cstring], False),
    "getlogin": (cstring, [], False),
    "getpwent": (voidptr_t, [], False),
    "getpwnam": (voidptr_t, [cstring], False),
    "getpwuid": (voidptr_t, [int32_t], False),
    "setpwent": (_VOID, [], False),
    "endpwent": (_VOID, [], False),
    "setenv": (int32_t, [cstring, cstring, int32_t], False),
    "putenv": (int32_t, [cstring], False),
    "unsetenv": (int32_t, [cstring], False),
    "system": (int32_t, [cstring], False),
    # === string.h ===
    "strcmp": (int32_t, [cstring, cstring], False),
    "strncmp": (int32_t, [cstring, cstring, _size_t], False),
    "strcpy": (cstring, [cstring, cstring], False),
    "strncpy": (cstring, [cstring, cstring, _size_t], False),
    "strcat": (cstring, [cstring, cstring], False),
    "strncat": (cstring, [cstring, cstring, _size_t], False),
    "strlcpy": (_size_t, [cstring, cstring, _size_t], False),
    "strlcat": (_size_t, [cstring, cstring, _size_t], False),
    "strchr": (cstring, [cstring, int32_t], False),
    "strrchr": (cstring, [cstring, int32_t], False),
    "strstr": (cstring, [cstring, cstring], False),
    "strpbrk": (cstring, [cstring, cstring], False),
    "strspn": (_size_t, [cstring, cstring], False),
    "strcspn": (_size_t, [cstring, cstring], False),
    "strtok": (cstring, [cstring, cstring], False),
    "memmove": (voidptr_t, [voidptr_t, voidptr_t, _size_t], False),
    "memcmp": (int32_t, [voidptr_t, voidptr_t, _size_t], False),
    "memchr": (voidptr_t, [voidptr_t, int32_t, _size_t], False),
    "strerror": (cstring, [int32_t], False),
    # === ctype.h ===
    "isalpha": (int32_t, [int32_t], False),
    "isdigit": (int32_t, [int32_t], False),
    "isalnum": (int32_t, [int32_t], False),
    "isspace": (int32_t, [int32_t], False),
    "isupper": (int32_t, [int32_t], False),
    "islower": (int32_t, [int32_t], False),
    "isprint": (int32_t, [int32_t], False),
    "ispunct": (int32_t, [int32_t], False),
    "iscntrl": (int32_t, [int32_t], False),
    "isxdigit": (int32_t, [int32_t], False),
    "isgraph": (int32_t, [int32_t], False),
    "toupper": (int32_t, [int32_t], False),
    "tolower": (int32_t, [int32_t], False),
    # === wchar.h / wctype.h ===
    "mbtowc": (int32_t, [int32_t.as_pointer(), cstring, _size_t], False),
    "wctomb": (int32_t, [cstring, int32_t], False),
    "mbrlen": (_size_t, [cstring, _size_t, voidptr_t], False),
    "mbrtowc": (_size_t, [int32_t.as_pointer(), cstring, _size_t, voidptr_t], False),
    "mbsrtowcs": (_size_t, [int32_t.as_pointer(), voidptr_t, _size_t, voidptr_t], False),
    "mbstowcs": (_size_t, [int32_t.as_pointer(), cstring, _size_t], False),
    "wcrtomb": (_size_t, [cstring, int32_t, voidptr_t], False),
    "wcsrtombs": (_size_t, [cstring, voidptr_t, _size_t, voidptr_t], False),
    "wcstombs": (_size_t, [cstring, voidptr_t, _size_t], False),
    "wcwidth": (int32_t, [int32_t], False),
    "wcswidth": (int32_t, [voidptr_t, _size_t], False),
    "wmemchr": (voidptr_t, [voidptr_t, int32_t, _size_t], False),
    "wmemcpy": (voidptr_t, [voidptr_t, voidptr_t, _size_t], False),
    "wmemmove": (voidptr_t, [voidptr_t, voidptr_t, _size_t], False),
    "wmemcmp": (int32_t, [voidptr_t, voidptr_t, _size_t], False),
    "iswalnum": (int32_t, [int32_t], False),
    "iswalpha": (int32_t, [int32_t], False),
    "iswcntrl": (int32_t, [int32_t], False),
    "iswctype": (int32_t, [int32_t, int32_t], False),
    "iswgraph": (int32_t, [int32_t], False),
    "iswlower": (int32_t, [int32_t], False),
    "iswprint": (int32_t, [int32_t], False),
    "iswspace": (int32_t, [int32_t], False),
    "iswupper": (int32_t, [int32_t], False),
    "towlower": (int32_t, [int32_t], False),
    "towupper": (int32_t, [int32_t], False),
    "wctype": (int32_t, [cstring], False),
    # === math.h ===
    "sin": (_double, [_double], False),
    "cos": (_double, [_double], False),
    "tan": (_double, [_double], False),
    "asin": (_double, [_double], False),
    "acos": (_double, [_double], False),
    "atan": (_double, [_double], False),
    "atan2": (_double, [_double, _double], False),
    "sinh": (_double, [_double], False),
    "cosh": (_double, [_double], False),
    "tanh": (_double, [_double], False),
    "exp": (_double, [_double], False),
    "exp2": (_double, [_double], False),
    "log": (_double, [_double], False),
    "log2": (_double, [_double], False),
    "log10": (_double, [_double], False),
    "pow": (_double, [_double, _double], False),
    "sqrt": (_double, [_double], False),
    # fma and its __builtin_ spelling: musl's pow() uses __builtin_fma on
    # targets with a fused multiply-add, and without a prototype the call was
    # emitted as an undefined ___builtin_fma symbol that failed the link.
    # aarch64 always has FMA, so both spellings map to the libm entry point.
    "fma": (_double, [_double, _double, _double], False),
    "fmaf": (ir.FloatType(), [ir.FloatType(), ir.FloatType(), ir.FloatType()], False),
    "__builtin_fma": (_double, [_double, _double, _double], False),
    "__builtin_fmaf": (
        ir.FloatType(),
        [ir.FloatType(), ir.FloatType(), ir.FloatType()],
        False,
    ),
    "cbrt": (_double, [_double], False),
    "hypot": (_double, [_double, _double], False),
    "ceil": (_double, [_double], False),
    "floor": (_double, [_double], False),
    "round": (_double, [_double], False),
    "trunc": (_double, [_double], False),
    "fmod": (_double, [_double, _double], False),
    "fabsf": (_float, [_float], False),
    "fabs": (_double, [_double], False),
    "fabsl": (_double, [_double], False),
    "ldexp": (_double, [_double, int32_t], False),
    # === time.h ===
    "time": (_time_t, [voidptr_t], False),
    "clock": (int64_t, [], False),
    "difftime": (_double, [_time_t, _time_t], False),
    "gmtime_r": (voidptr_t, [voidptr_t, voidptr_t], False),
    "localtime_r": (voidptr_t, [voidptr_t, voidptr_t], False),
    "nanosleep": (int32_t, [voidptr_t, voidptr_t], False),
    # === unistd.h (POSIX) ===
    "sleep": (int32_t, [int32_t], False),
    "alarm": (int32_t, [int32_t], False),
    "usleep": (int32_t, [int32_t], False),
    "getuid": (int32_t, [], False),
    "geteuid": (int32_t, [], False),
    "access": (int32_t, [cstring, int32_t], False),
    "fcntl": (int32_t, [int32_t, int32_t], True),
    "fsync": (int32_t, [int32_t], False),
    "ftruncate": (int32_t, [int32_t, int64_t], False),
    "pread": (int64_t, [int32_t, voidptr_t, _size_t, int64_t], False),
    "pwrite": (int64_t, [int32_t, voidptr_t, _size_t, int64_t], False),
    "unlink": (int32_t, [cstring], False),
    "readlink": (int64_t, [cstring, cstring, _size_t], False),
    "getpid": (int32_t, [], False),
    "getppid": (int32_t, [], False),
    "sysconf": (int64_t, [int32_t], False),
    "isatty": (int32_t, [int32_t], False),
    "mkstemp": (int32_t, [cstring], False),
    "tcdrain": (int32_t, [int32_t], False),
    "tcflow": (int32_t, [int32_t, int32_t], False),
    "tcgetattr": (int32_t, [int32_t, voidptr_t], False),
    "tcsetattr": (int32_t, [int32_t, int32_t, voidptr_t], False),
    "select": (int32_t, [int32_t, voidptr_t, voidptr_t, voidptr_t, voidptr_t], False),
    "pselect": (int32_t, [int32_t, voidptr_t, voidptr_t, voidptr_t, voidptr_t, voidptr_t], False),
    # === setjmp.h ===
    "setjmp": (int32_t, [voidptr_t], False),
    "longjmp": (_VOID, [voidptr_t, int32_t], False),
    "_setjmp": (int32_t, [voidptr_t], False),
    "_longjmp": (_VOID, [voidptr_t, int32_t], False),
    "sigsetjmp": (int32_t, [voidptr_t, int32_t], False),
    "siglongjmp": (_VOID, [voidptr_t, int32_t], False),
    # === signal.h ===
    "signal": (voidptr_t, [int32_t, voidptr_t], False),
    "sigaction": (int32_t, [int32_t, voidptr_t, voidptr_t], False),
    "sigaddset": (int32_t, [voidptr_t, int32_t], False),
    "sigdelset": (int32_t, [voidptr_t, int32_t], False),
    "sigemptyset": (int32_t, [voidptr_t], False),
    "sigismember": (int32_t, [voidptr_t, int32_t], False),
    "sigprocmask": (int32_t, [int32_t, voidptr_t, voidptr_t], False),
    "sigsuspend": (int32_t, [voidptr_t], False),
    "kill": (int32_t, [int32_t, int32_t], False),
    "raise": (int32_t, [int32_t], False),
    # === errno ===
    # === locale.h ===
    "setlocale": (cstring, [int32_t, cstring], False),
    "localeconv": (voidptr_t, [], False),
    "nl_langinfo": (cstring, [int32_t], False),
    # === misc ===
    "tmpnam": (cstring, [cstring], False),
    "tmpfile": (voidptr_t, [], False),
    "stat": (int32_t, [cstring, voidptr_t], False),
    "fstat": (int32_t, [int32_t, voidptr_t], False),
    "lstat": (int32_t, [cstring, voidptr_t], False),
    "chmod": (int32_t, [cstring, int32_t], False),
    "fchmod": (int32_t, [int32_t, int32_t], False),
    "mkdir": (int32_t, [cstring, int32_t], False),
    "umask": (int32_t, [int32_t], False),
    "utime": (int32_t, [cstring, voidptr_t], False),
    "utimes": (int32_t, [cstring, voidptr_t], False),
    "futimes": (int32_t, [int32_t, voidptr_t], False),
    "gettimeofday": (int32_t, [voidptr_t, voidptr_t], False),
    "gmtime": (voidptr_t, [voidptr_t], False),
    "localtime": (voidptr_t, [voidptr_t], False),
    "mktime": (_time_t, [voidptr_t], False),
    "strftime": (_size_t, [cstring, _size_t, cstring, voidptr_t], False),
    "ctime": (cstring, [voidptr_t], False),
    "asctime": (cstring, [voidptr_t], False),
    "frexp": (_double, [_double, int32_t.as_pointer()], False),
    # GCC/Clang builtins (no-op stubs)
    "__builtin_va_start": (_VOID, [voidptr_t], False),
    "__builtin_va_end": (_VOID, [voidptr_t], False),
    "__builtin_va_copy": (_VOID, [voidptr_t, voidptr_t], False),
    "__builtin_alloca": (voidptr_t, [int64_t], False),
    "__builtin_expect": (int64_t, [int64_t, int64_t], False),
    "__builtin_assume": (_VOID, [int64_t], False),
    "__builtin_prefetch": (_VOID, [voidptr_t, int32_t, int32_t], False),
    "__builtin_unreachable": (_VOID, [], False),
    "__builtin_add_overflow": (int32_t, [int64_t, int64_t, voidptr_t], False),
    "__builtin_sub_overflow": (int32_t, [int64_t, int64_t, voidptr_t], False),
    "__builtin_mul_overflow": (int32_t, [int64_t, int64_t, voidptr_t], False),
    "__builtin_abs": (int32_t, [int32_t], False),
    "__builtin_labs": (int64_t, [int64_t], False),
    "__builtin_llabs": (int64_t, [int64_t], False),
    "__builtin_imaxabs": (int64_t, [int64_t], False),
    "__builtin_bswap16": (int32_t, [int32_t], False),
    "__builtin_bswap32": (int32_t, [int32_t], False),
    "__builtin_bswap64": (int64_t, [int64_t], False),
    "__builtin_clz": (int32_t, [int32_t], False),
    "__builtin_clzll": (int32_t, [int64_t], False),
    "__builtin_ctz": (int32_t, [int32_t], False),
    "__builtin_ctzll": (int32_t, [int64_t], False),
    "__builtin_rotateleft32": (int32_t, [int32_t, int32_t], False),
    "__builtin_rotateleft64": (int64_t, [int64_t, int64_t], False),
    "__builtin_rotateright32": (int32_t, [int32_t, int32_t], False),
    "__builtin_rotateright64": (int64_t, [int64_t, int64_t], False),
    "__sync_synchronize": (_VOID, [], False),
    "__sync_fetch_and_add": (int64_t, [voidptr_t, int64_t], False),
    "__sync_bool_compare_and_swap": (int32_t, [voidptr_t, int64_t, int64_t], False),
    "__atomic_load_n": (int64_t, [voidptr_t, int32_t], False),
    "__atomic_store_n": (_VOID, [voidptr_t, int64_t, int32_t], False),
    "__atomic_add_fetch": (int64_t, [voidptr_t, int64_t, int32_t], False),
    "__atomic_sub_fetch": (int64_t, [voidptr_t, int64_t, int32_t], False),
    "__atomic_or_fetch": (int64_t, [voidptr_t, int64_t, int32_t], False),
    "__atomic_and_fetch": (int64_t, [voidptr_t, int64_t, int32_t], False),
    "__atomic_xor_fetch": (int64_t, [voidptr_t, int64_t, int32_t], False),
    "__atomic_fetch_add": (int64_t, [voidptr_t, int64_t, int32_t], False),
    "__atomic_fetch_sub": (int64_t, [voidptr_t, int64_t, int32_t], False),
    "__atomic_fetch_or": (int64_t, [voidptr_t, int64_t, int32_t], False),
    "__atomic_fetch_and": (int64_t, [voidptr_t, int64_t, int32_t], False),
    "__atomic_fetch_xor": (int64_t, [voidptr_t, int64_t, int32_t], False),
    "__atomic_exchange_n": (int64_t, [voidptr_t, int64_t, int32_t], False),
    "__atomic_compare_exchange_n": (
        int32_t,
        [voidptr_t, voidptr_t, int64_t, int32_t, int32_t, int32_t],
        False,
    ),
    "__atomic_test_and_set": (int32_t, [voidptr_t, int32_t], False),
    "__atomic_clear": (_VOID, [voidptr_t, int32_t], False),
    "__atomic_thread_fence": (_VOID, [int32_t], False),
    "modf": (_double, [_double, ir.DoubleType().as_pointer()], False),
    "ldexp": (_double, [_double, int32_t], False),
    # scalbn/scalbln are ldexp's siblings. Without a prototype the call went
    # through the implicit-int path, so the caller read the result from an
    # integer register and bit-cast it to double: scalbn(1.0, 10) came back as
    # 4.94e-323 (the bits of the integer 10). Same class as the 2026-07-31
    # implicit-declaration truncation batch.
    "scalbn": (_double, [_double, int32_t], False),
    "scalbnf": (ir.FloatType(), [ir.FloatType(), int32_t], False),
    "scalbln": (_double, [_double, int64_t], False),
    "__builtin_va_arg": (voidptr_t, [voidptr_t, int64_t], False),
    "strcoll": (int32_t, [cstring, cstring], False),
    "clearerr": (_VOID, [voidptr_t], False),
    "fileno": (int32_t, [voidptr_t], False),
    "popen": (voidptr_t, [cstring, cstring], False),
    "pclose": (int32_t, [voidptr_t], False),
    "dlopen": (voidptr_t, [cstring, int32_t], False),
    "dlsym": (voidptr_t, [voidptr_t, cstring], False),
    "dlclose": (int32_t, [voidptr_t], False),
    "dlerror": (cstring, [], False),
    "setvbuf": (int32_t, [voidptr_t, cstring, int32_t, _size_t], False),
    "freopen": (voidptr_t, [cstring, cstring, voidptr_t], False),
    "getc": (int32_t, [voidptr_t], False),
    # POSIX/libc calls the py_runtime sources use that previously fell into
    # the implicit-int declaration path. Pointer- and 64-bit-returning ones
    # were being truncated to i32 (libpy_runtime_pcc.a crash class,
    # 2026-07-31): realpath/inet_ntop lost pointer upper halves, strtoll and
    # getline lost 64-bit widths, and copysign/rint returned garbage because
    # a double came back in w0 instead of d0.
    "realpath": (cstring, [cstring, cstring], False),
    "getline": (int64_t, [voidptr_t, voidptr_t, voidptr_t], False),
    "strtoll": (int64_t, [cstring, voidptr_t, int32_t], False),
    "copysign": (_double, [_double, _double], False),
    "rint": (_double, [_double], False),
    "inet_ntop": (cstring, [int32_t, voidptr_t, cstring, int32_t], False),
    "accept": (int32_t, [int32_t, voidptr_t, voidptr_t], False),
    "bind": (int32_t, [int32_t, voidptr_t, int32_t], False),
    "listen": (int32_t, [int32_t, int32_t], False),
    "getsockname": (int32_t, [int32_t, voidptr_t, voidptr_t], False),
    "getpeername": (int32_t, [int32_t, voidptr_t, voidptr_t], False),
    "setsockopt": (int32_t, [int32_t, int32_t, int32_t, voidptr_t, int32_t], False),
    "shutdown": (int32_t, [int32_t, int32_t], False),
    "ntohs": (int32_t, [int32_t], False),
    "arc4random_buf": (_VOID, [voidptr_t, _size_t], False),
}

# Public lowered map. Declarative signatures are merged below only after a
# source guard proves they do not retain a shadow entry in this legacy table.
LIBC_FUNCTIONS = dict(_LEGACY_LIBC_FUNCTIONS)


def _libc_registry_ir_type(type_name: str):
    """Lower a declarative libc type into the c_codegen IR type universe.

    The declarative registry intentionally stores portable C spelling.  This
    adapter is the bridge that makes the registry affect the real C frontend
    today without waiting for the full C1 codegen split.
    """
    raw = (type_name or "").strip()
    clean = raw.replace("const ", "").replace("volatile ", "").strip()
    clean = clean.replace("struct FILE", "FILE")
    if clean == "...":
        return None
    pointer_depth = 0
    while clean.endswith("*"):
        pointer_depth += 1
        clean = clean[:-1].strip()
    base_map = {
        "void": _VOID,
        "char": int8_t,
        "signed char": int8_t,
        "unsigned char": int8_t,
        "int": int32_t,
        "signed int": int32_t,
        "unsigned int": int32_t,
        "long": int64_t,
        "long int": int64_t,
        "unsigned long": int64_t,
        "long long": int64_t,
        "unsigned long long": int64_t,
        "size_t": _size_t,
        "ssize_t": int64_t,
        "time_t": _time_t,
        "double": _double,
        "float": _float,
        "FILE": _FILE_ptr,
    }
    if pointer_depth == 0:
        return base_map.get(clean, voidptr_t)
    if clean == "char" and pointer_depth == 1:
        return cstring
    if clean == "FILE" and pointer_depth == 1:
        return _FILE_ptr
    ir_type = base_map.get(clean, int8_t)
    if clean == "void":
        ir_type = int8_t
    while pointer_depth > 0:
        ir_type = ir_type.as_pointer()
        pointer_depth -= 1
    return ir_type


def _libc_registry_signature_to_codegen(sig: LibcSignature):
    params = []
    var_arg = False
    for arg in sig.arg_types:
        if arg.strip() == "...":
            var_arg = True
            continue
        lowered = _libc_registry_ir_type(arg)
        if lowered is not None:
            params.append(lowered)
    return (_libc_registry_ir_type(sig.return_type), params, var_arg)


def refresh_libc_registry_from_declarative() -> int:
    """Merge pcc.frontends.c.c_libc_registry into the real codegen libc map.

    This function is intentionally callable by tests and future platform setup
    code.  It replaces duplicate built-in entries with the declarative source
    of truth and adds new signatures without touching call sites.
    """
    signatures = iter_signatures()
    overlaps = sorted(
        sig.name for sig in signatures if sig.name in _LEGACY_LIBC_FUNCTIONS
    )
    if overlaps:
        raise AssertionError(
            "declarative libc signatures still shadow legacy codegen entries: "
            + ", ".join(overlaps)
        )
    count = 0
    for sig in signatures:
        LIBC_FUNCTIONS[sig.name] = _libc_registry_signature_to_codegen(sig)
        count += 1
    return count


def libc_registry_shadow_names() -> tuple[str, ...]:
    """Return declarative names that still have a legacy codegen definition."""
    return tuple(
        sorted(
            sig.name
            for sig in iter_signatures()
            if sig.name in _LEGACY_LIBC_FUNCTIONS
        )
    )


refresh_libc_registry_from_declarative()


