"""Lower the runtime's named Linux operations to the selected kernel ABI.

Numbers at this private seam identify x86-64 operations, not the public raw
syscall intrinsic.  AArch64 uses asm-generic numbers and different argument
lists for the legacy path, poll, fork and dup2 operations.
"""

from pcc.ir import ir

_I64 = ir.IntType(64)
_DIRECT = {
    0: 63, 1: 64, 3: 57, 8: 62, 9: 222, 11: 215, 18: 68, 35: 101,
    39: 172, 41: 198, 42: 203, 43: 202, 44: 206, 45: 207,
    48: 210, 49: 200, 50: 201, 51: 204, 52: 205, 54: 208,
    55: 209, 59: 221, 61: 260, 62: 129, 63: 160, 72: 25, 73: 32,
    74: 82, 77: 46, 79: 17, 80: 49, 109: 154, 204: 123, 228: 113,
    231: 94, 233: 21, 257: 56, 258: 34, 263: 35, 290: 19, 291: 20,
    293: 59,
}


def emit_linux_operation(owner, nr, a1, a2, a3, a4, a5, a6, name=""):
    machine = owner._target_machine_text()
    args = [a1, a2, a3, a4, a5, a6]
    if machine == "x86_64":
        return owner.builder.syscall6(nr, a1, a2, a3, a4, a5, a6, name=name)
    if machine not in ("aarch64", "arm64"):
        raise NotImplementedError("Linux syscall target: " + machine)
    operation = int(nr.value)
    zero = ir.Constant(_I64, 0)
    cwd = ir.Constant(_I64, -100)
    if operation == 2:  # open -> openat
        number, args = 56, [cwd, a1, a2, a3, zero, zero]
    elif operation == 4:  # stat -> newfstatat
        number, args = 79, [cwd, a1, a2, zero, zero, zero]
    elif operation == 6:  # lstat -> newfstatat(AT_SYMLINK_NOFOLLOW)
        number, args = 79, [cwd, a1, a2, ir.Constant(_I64, 256), zero, zero]
    elif operation == 21:  # access -> faccessat
        number, args = 48, [cwd, a1, a2, zero, zero, zero]
    elif operation == 82:  # rename -> renameat
        number, args = 38, [cwd, a1, cwd, a2, zero, zero]
    elif operation == 83:  # mkdir -> mkdirat
        number, args = 34, [cwd, a1, a2, zero, zero, zero]
    elif operation == 89:  # readlink -> readlinkat
        number, args = 78, [cwd, a1, a2, a3, zero, zero]
    elif operation == 90:  # chmod -> fchmodat
        number, args = 53, [cwd, a1, a2, zero, zero, zero]
    elif operation == 57:  # fork -> clone(SIGCHLD, NULL, ...)
        number, args = 220, [ir.Constant(_I64, 17), zero, zero, zero, zero, zero]
    elif operation == 33:  # dup2: dup3 rejects oldfd == newfd
        same = owner.builder.icmp_signed("==", a1, a2)
        # F_GETFD validates the descriptor in the equal-fd branch.
        number_value = owner.builder.select(same, ir.Constant(_I64, 25), ir.Constant(_I64, 24))
        second = owner.builder.select(same, ir.Constant(_I64, 1), a2)
        raw = owner.builder.syscall6(number_value, a1, second, zero, zero, zero, zero,
                                    name=name)
        ok = owner.builder.icmp_signed(">=", raw, zero)
        equal_ok = owner.builder.and_(same, ok)
        return owner.builder.select(equal_ok, a2, raw)
    elif operation == 7:  # poll -> ppoll, milliseconds -> timespec
        slot = owner.builder.alloca(ir.ArrayType(_I64, 2))
        seconds = owner.builder.sdiv(a3, ir.Constant(_I64, 1000))
        nanos = owner.builder.mul(owner.builder.srem(a3, ir.Constant(_I64, 1000)),
                                  ir.Constant(_I64, 1000000))
        owner.builder.store(seconds, owner.builder.gep(slot, [zero, zero]))
        owner.builder.store(nanos, owner.builder.gep(slot, [zero, ir.Constant(_I64, 1)]))
        timeout = owner.builder.ptrtoint(slot, _I64)
        timeout = owner.builder.select(owner.builder.icmp_signed("<", a3, zero), zero, timeout)
        number, args = 73, [a1, a2, timeout, zero, ir.Constant(_I64, 8), zero]
    elif operation == 232:  # epoll_wait -> epoll_pwait
        number, args = 22, [a1, a2, a3, a4, zero, ir.Constant(_I64, 8)]
    else:
        if operation not in _DIRECT:
            raise NotImplementedError("unmapped AArch64 Linux operation: " + str(operation))
        number = _DIRECT[operation]
    return owner.builder.syscall6(ir.Constant(_I64, number), args[0], args[1], args[2], args[3], args[4], args[5], name=name)
