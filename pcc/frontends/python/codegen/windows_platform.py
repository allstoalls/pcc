"""Target-specific intrinsic routing to pcc's Windows platform runtime."""

from pcc.ir import ir

# p=raw pointer, i=explicit i64, d=double, v=void.
_SIGNATURES = {
    "page_alloc": ("p", "i"), "page_free": ("i", "pi"),
    "read": ("i", "ipi"), "write": ("i", "ipi"), "close": ("i", "i"),
    "seek_file": ("i", "iii"), "open_readonly": ("i", "p"),
    "open_file": ("i", "pii"), "sync_file": ("i", "i"),
    "getpid": ("i", ""), "getcwd": ("p", "pi"),
    "stat_kind": ("i", "p"), "stat_mtime": ("d", "p"),
    "lstat_kind": ("i", "p"),
    "stat_size": ("i", "p"), "is_symlink": ("i", "p"),
    "access": ("i", "pi"), "mkdir": ("i", "pi"),
    "unlinkat": ("i", "pi"), "rename_file": ("i", "pp"),
    "chmod_file": ("i", "pi"), "clock_gettime": ("i", "ip"),
    "utime_file": ("i", "pp"),
    "nanosleep": ("i", "pp"), "cpu_query": ("i", "pi"),
    "process_exit": ("v", "i"), "initial_environ": ("p", ""),
    "getenv": ("p", "p"), "setenv": ("i", "ppi"), "unsetenv": ("i", "p"),
    "dynamic_library_open": ("p", "p"), "dynamic_library_symbol": ("p", "pp"),
    "dynamic_library_close": ("i", "p"),
    "spawn_process": ("i", "pppi"), "spawn_process_pipe": ("i", "pppip"),
    "spawn_process_redirect": ("i", "ppppiii"),
    "waitpid": ("i", "ipi"), "kill": ("i", "ii"),
    "readlink": ("i", "ppi"), "uname": ("i", "p"), "uname_field": ("p", "pi"),
    "socket_open": ("i", "iii"), "socket_connect": ("i", "ipi"),
    "socket_bind": ("i", "ipi"), "socket_listen": ("i", "ii"),
    "socket_accept": ("i", "i"), "socket_shutdown": ("i", "ii"),
    "socket_send": ("i", "ipii"), "socket_recv": ("i", "ipii"),
    "socket_setsockopt": ("i", "iiipi"), "socket_getsockopt": ("i", "iiipi"),
    "socket_sockname": ("i", "ipi"), "socket_peername": ("i", "ipi"),
    "file_lock_region": ("i", "iiii"),
    "fd_control": ("i", "iii"), "poll_fd": ("i", "iii"),
    "poll_readable_pair": ("i", "iii"),
    "dynamic_library_open_global": ("p", "p"),
}


def handles(intrinsic: str) -> bool:
    return intrinsic in _SIGNATURES


def emit(owner, intrinsic, expr):
    result, arguments = _SIGNATURES[intrinsic]
    types = {"p": ir.IntType(8).as_pointer(), "i": ir.IntType(64),
             "d": ir.DoubleType(), "v": ir.VoidType()}
    if intrinsic.startswith("dynamic_library_"):
        if not owner._unsafe_dynamic_library_target_matches(intrinsic, expr, len(arguments)):
            return ir.Constant(types[result], None if result == "p" else 0)
    else:
        owner._unsafe_expect_arity(intrinsic, expr, len(arguments))
    if intrinsic == "dynamic_library_open_global":
        intrinsic = "dynamic_library_open"
    symbol = ("pcc_platform_" if intrinsic in ("getenv", "setenv", "unsetenv") else "pcc_win_") + intrinsic
    fn = owner._declare_external_function(symbol, types[result], [types[kind] for kind in arguments])
    values = []
    for arg, kind in zip(expr.args, arguments):
        values.append(owner._unsafe_ptr_arg(arg) if kind == "p" else owner._unsafe_i64_arg(arg))
    value = owner.builder.call(fn, values, name="" if result == "v" else owner._fresh("unsafe.win." + intrinsic))
    return owner._unsafe_void_result() if result == "v" else value
