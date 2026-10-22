"""Directory intrinsic ABI selection, with no shell or host interpreter."""

from pcc.ir import ir


def emit(owner, intrinsic, expr):
    pointer = ir.IntType(8).as_pointer()
    i32 = ir.IntType(32)
    i64 = ir.IntType(64)
    if intrinsic == "directory_entry_type":
        return _entry_type(owner, expr)
    owner._unsafe_expect_arity(intrinsic, expr, 1)
    argument = owner._unsafe_ptr_arg(expr.args[0])
    result_type = pointer if intrinsic in ("directory_open", "directory_next") else i64
    platform = owner._target_sys_platform_text()
    if platform in ("linux", "win32"):
        prefix = "pcc_linux_" if platform == "linux" else "pcc_win_"
        function = owner._declare_external_function(prefix + intrinsic, result_type, [pointer])
        return owner.builder.call(function, [argument])
    if platform != "darwin":
        raise NotImplementedError("owned directory target: " + platform)
    if intrinsic == "directory_open":
        function = owner._declare_external_function("opendir", pointer, [pointer])
        return owner.builder.call(function, [argument])
    if intrinsic == "directory_close":
        function = owner._declare_external_function("closedir", i32, [pointer])
        return owner.builder.sext(owner.builder.call(function, [argument]), i64)
    error_function = owner._declare_external_function("__error", i32.as_pointer(), [])
    error_pointer = owner.builder.call(error_function, [])
    if intrinsic == "directory_error":
        return owner.builder.sub(ir.Constant(i64, 0), owner.builder.sext(owner.builder.load(error_pointer), i64))
    owner.builder.store(ir.Constant(i32, 0), error_pointer)
    function = owner._declare_external_function("readdir", pointer, [pointer])
    entry = owner.builder.call(function, [argument])
    empty = owner.builder.icmp_unsigned("==", entry, ir.Constant(pointer, None))
    name = owner.builder.gep(entry, [ir.Constant(i64, 21)])
    return owner.builder.select(empty, ir.Constant(pointer, None), name)


def _entry_type(owner, expr):
    owner._unsafe_expect_arity("directory_entry_type", expr, 2)
    stream = owner._unsafe_ptr_arg(expr.args[0])
    entry = owner._unsafe_ptr_arg(expr.args[1])
    platform = owner._target_sys_platform_text()
    i8, i32, i64 = ir.IntType(8), ir.IntType(32), ir.IntType(64)
    if platform in ("linux", "darwin"):
        # Linux getdents64 and Darwin dirent both place d_type immediately
        # before the name exposed by our directory_next intrinsic.
        kind = owner.builder.load(owner.builder.gep(entry, [ir.Constant(i64, -1)]))
        unknown = owner.builder.icmp_unsigned("==", kind, ir.Constant(i8, 0))
        directory = owner.builder.icmp_unsigned("==", kind, ir.Constant(i8, 4))
        symlink = owner.builder.icmp_unsigned("==", kind, ir.Constant(i8, 10))
        known = owner.builder.select(directory, ir.Constant(i64, 2),
                    owner.builder.select(symlink, ir.Constant(i64, 3), ir.Constant(i64, 1)))
        return owner.builder.select(unknown, ir.Constant(i64, 0), known)
    if platform == "win32":
        # The owned stream starts WIN32_FIND_DATAW at byte 32. Attribute
        # and reparse tag snapshots are retained until directory_next.
        attributes = owner.builder.load(owner.builder.bitcast(
            owner.builder.gep(stream, [ir.Constant(i64, 32)]), i32.as_pointer()))
        reparse_tag = owner.builder.load(owner.builder.bitcast(
            owner.builder.gep(stream, [ir.Constant(i64, 68)]), i32.as_pointer()))
        reparse = owner.builder.icmp_unsigned("!=", owner.builder.and_(attributes, ir.Constant(i32, 1024)), ir.Constant(i32, 0))
        symlink = owner.builder.and_(reparse,
            owner.builder.icmp_unsigned("==", reparse_tag, ir.Constant(i32, 0xA000000C)))
        directory = owner.builder.icmp_unsigned("!=", owner.builder.and_(attributes, ir.Constant(i32, 16)), ir.Constant(i32, 0))
        return owner.builder.select(symlink, ir.Constant(i64, 3),
                    owner.builder.select(directory, ir.Constant(i64, 2), ir.Constant(i64, 1)))
    raise NotImplementedError("owned directory entry target: " + platform)
