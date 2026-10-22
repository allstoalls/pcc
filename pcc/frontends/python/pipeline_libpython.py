"""Libpython fallback analysis and synthesized-main lifecycle rewriting."""

from __future__ import annotations

import os
import shlex
import subprocess

from pcc.frontends.python import pipeline_import_policy as import_policy
from pcc.frontends.python.codegen.layer1_support import _dataclass_field_names
from pcc.frontends.python.pipeline_packages import resolve_pcc_native_extension_path
from pcc.driver.paths import join_dotted_parts


class LibpythonLinkConfigError(RuntimeError):
    """Host libpython link flags could not be resolved."""


def ast_field_names(obj):
    return tuple(_dataclass_field_names(obj))


def ast_field_value(obj, field_name, default=None):
    return getattr(obj, field_name, default)


def ast_name_used_at_runtime(stmts, ident: str) -> bool:
    from pcc.frontends.python.py_ast import Import, ImportFrom, Name, Type

    annotation_slots = {"annotation", "return_ty"}
    pending = [stmts]
    while pending:
        item = pending.pop()
        if item is None or isinstance(item, Type) or isinstance(item, (Import, ImportFrom)):
            continue
        if isinstance(item, tuple):
            for child in item:
                pending.append(child)
            continue
        if isinstance(item, Name):
            if ast_field_value(item, "ident", "") == ident:
                return True
            continue
        for slot in ast_field_names(item):
            if slot not in annotation_slots:
                pending.append(ast_field_value(item, slot, None))
    return False


def module_import_is_scaffold(
    module_name: str | None,
    *,
    ir_scaffold_mode: str,
    current_module: str,
) -> bool:
    if module_name == "pcc.ir.compat":
        return (
            ir_scaffold_mode == "on"
            or current_module == "pcc.frontends.python.codegen.runtime_abi"
        )
    return module_name in import_policy.SCAFFOLD_IMPORT_MODULES


def resolve_relative_import(module, level, cur_parts):
    if not level:
        return module or ""
    if level > len(cur_parts):
        return module or ""
    base = cur_parts[: len(cur_parts) - level]
    if module:
        return ".".join(base + [module])
    return ".".join(base)


def module_needs_libpython(
    ast_module,
    *,
    native_modules=None,
    ir_scaffold_mode: str = "off",
    strict_no_libpython: bool = False,
) -> bool:
    from pcc.frontends.python.py_ast import Import, ImportFrom

    native_set = set(native_modules or ())
    cur_mod = ast_field_value(ast_module, "name", "") or ""
    cur_parts = cur_mod.split(".") if cur_mod else []
    pending_stmts = [ast_field_value(ast_module, "body", ())]
    while pending_stmts:
        stmts = pending_stmts.pop()
        for stmt in stmts:
            if isinstance(stmt, ImportFrom):
                stmt_module = ast_field_value(stmt, "module", None)
                stmt_level = ast_field_value(stmt, "level", 0) or 0
                stmt_names = ast_field_value(stmt, "names", ())
                if module_import_is_scaffold(
                    stmt_module,
                    ir_scaffold_mode=ir_scaffold_mode,
                    current_module=cur_mod,
                ):
                    continue
                if stmt_module in import_policy.TEST_FACADE_IMPORT_MODULES:
                    continue
                if (
                    stmt_module is not None
                    and stmt_module.split(".")[0]
                    in import_policy.COMPILE_TIME_ONLY_IMPORT_MODULES
                ):
                    continue
                resolved = resolve_relative_import(stmt_module, stmt_level, cur_parts)
                compile_only = import_policy.COMPILE_TIME_ONLY_IMPORT_FROMS.get(
                    resolved
                )
                if compile_only is not None:
                    remaining = []
                    runtime_fields = ()
                    if resolved == "dataclasses":
                        runtime_fields = import_policy.dataclasses_field_runtime_names(ast_module)
                    for alias_name, as_name in stmt_names:
                        if alias_name not in compile_only:
                            remaining.append(alias_name)
                        elif alias_name == "field" and (as_name or alias_name) in runtime_fields:
                            remaining.append(alias_name)
                    if not remaining:
                        continue
                allowed = import_policy.NATIVE_IMPORT_FROMS.get(resolved)
                if allowed is not None:
                    all_allowed = True
                    for alias_name, _ in stmt_names:
                        if alias_name not in allowed:
                            all_allowed = False
                            break
                    if all_allowed:
                        continue
                if stmt_level and (stmt_module is None or stmt_module == ""):
                    if resolved:
                        all_modules = True
                        for alias_name, _ in stmt_names:
                            if join_dotted_parts([resolved, alias_name]) not in native_set:
                                all_modules = False
                                break
                        if all_modules:
                            continue
                if resolved in native_set:
                    continue
                if not strict_no_libpython:
                    return True
            if isinstance(stmt, Import):
                remaining = []
                for module_name, as_name in ast_field_value(stmt, "names", ()):
                    local_name = as_name or module_name.split(".")[0]
                    if (
                        module_name in import_policy.TEST_FACADE_IMPORT_MODULES
                        or module_name.split(".")[0]
                        in import_policy.COMPILE_TIME_ONLY_IMPORT_MODULES
                        or module_name in import_policy.NATIVE_BUILTIN_IMPORTS
                        or module_name in native_set
                        or resolve_pcc_native_extension_path(module_name) is not None
                    ):
                        continue
                    if (
                        module_name in import_policy.ANNOTATION_ONLY_IMPORT_MODULES
                        and not ast_name_used_at_runtime(
                            ast_field_value(ast_module, "body", ()),
                            local_name,
                        )
                    ):
                        continue
                    remaining.append(module_name)
                if remaining and not strict_no_libpython:
                    return True
            for slot in ("body", "else_body", "finally_body"):
                body = ast_field_value(stmt, slot, None)
                if body:
                    pending_stmts.append(body)
            handlers = ast_field_value(stmt, "handlers", None)
            if handlers:
                for handler in handlers:
                    pending_stmts.append(ast_field_value(handler, "body", ()))
    return False


def module_imports_native_extension(
    ast_module,
    *,
    native_modules=None,
    ir_scaffold_mode: str = "off",
) -> bool:
    from pcc.frontends.python.py_ast import Import, ImportFrom

    native_set = set(native_modules or ())
    cur_mod = ast_field_value(ast_module, "name", "") or ""
    cur_parts = cur_mod.split(".") if cur_mod else []
    pending_stmts = [ast_field_value(ast_module, "body", ())]
    while pending_stmts:
        stmts = pending_stmts.pop()
        for stmt in stmts:
            if isinstance(stmt, ImportFrom):
                stmt_module = ast_field_value(stmt, "module", None)
                stmt_level = ast_field_value(stmt, "level", 0) or 0
                if module_import_is_scaffold(
                    stmt_module,
                    ir_scaffold_mode=ir_scaffold_mode,
                    current_module=cur_mod,
                ):
                    continue
                resolved = resolve_relative_import(stmt_module, stmt_level, cur_parts)
                if (
                    resolved not in native_set
                    and resolve_pcc_native_extension_path(resolved) is not None
                ):
                    return True
                for alias_name, _ in ast_field_value(stmt, "names", ()):
                    candidate = join_dotted_parts([resolved, alias_name])
                    if (
                        candidate not in native_set
                        and resolve_pcc_native_extension_path(candidate) is not None
                    ):
                        return True
            elif isinstance(stmt, Import):
                for module_name, _ in ast_field_value(stmt, "names", ()):
                    if (
                        module_name not in native_set
                        and resolve_pcc_native_extension_path(module_name) is not None
                    ):
                        return True
            for slot in ("body", "else_body", "finally_body"):
                body = ast_field_value(stmt, slot, None)
                if body:
                    pending_stmts.append(body)
            handlers = ast_field_value(stmt, "handlers", None)
            if handlers:
                for handler in handlers:
                    pending_stmts.append(ast_field_value(handler, "body", ()))
    return False


def resolve_python_config_command() -> str:
    """Resolve the host ``python-config`` command used for libpython flags."""
    config_env = str(os.environ.get("PCC_PYTHON_CONFIG", "")).strip()
    if config_env:
        return config_env
    try:
        import sysconfig as _sysconfig

        bindir = str(_sysconfig.get_config_var("BINDIR") or "").strip()
        ldversion = str(
            _sysconfig.get_config_var("LDVERSION")
            or _sysconfig.get_config_var("VERSION")
            or ""
        ).strip()
        candidates = []
        if bindir:
            if ldversion:
                candidates.append(
                    str(os.path.join(bindir, f"python{ldversion}-config"))
                )
            candidates.extend(
                [
                    str(os.path.join(bindir, "python3-config")),
                    str(os.path.join(bindir, "python-config")),
                ]
            )
        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    except Exception:
        pass
    return "python3-config"


def _ensure_interpreter_libdir_flag(flags: list[str]) -> list[str]:
    """Repair python-config output that names libpython without its LIBDIR."""
    needs_search_path = False
    for flag in flags:
        if flag.startswith("-lpython"):
            needs_search_path = True
            break
    if not needs_search_path:
        return flags
    try:
        import sysconfig as _sysconfig

        libdir = str(_sysconfig.get_config_var("LIBDIR") or "").strip()
        ldlibrary = str(_sysconfig.get_config_var("LDLIBRARY") or "").strip()
    except Exception:
        return flags
    if not libdir or not ldlibrary:
        return flags
    if not os.path.isfile(os.path.join(libdir, ldlibrary)):
        return flags

    expected = os.path.realpath(libdir)
    index = 0
    while index < len(flags):
        flag = flags[index]
        candidate = ""
        if flag == "-L" and index + 1 < len(flags):
            candidate = flags[index + 1]
            index += 1
        elif flag.startswith("-L") and len(flag) > 2:
            candidate = flag[2:]
        if candidate and os.path.realpath(candidate) == expected:
            return flags
        index += 1
    return ["-L" + libdir, *flags]


def link_flags() -> list[str]:
    """Return explicit or interpreter-owned embed linker flags."""
    ldflags_env = str(os.environ.get("PCC_PYTHON_LDFLAGS", "")).strip()
    if ldflags_env:
        return list(shlex.split(ldflags_env))
    config_cmd = resolve_python_config_command()
    try:
        output = str(
            subprocess.check_output(
                [config_cmd, "--ldflags", "--embed"],
                text=True,
            ).strip()
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise LibpythonLinkConfigError(
            f"{config_cmd} required for import-using programs: {exc}"
        ) from exc
    return _ensure_interpreter_libdir_flag(output.split())


def ir_needs_libpython(ir_text: str) -> bool:
    if "@py_cpy_" not in ir_text:
        return False
    for line in ir_text.splitlines():
        if "@py_cpy_" not in line:
            continue
        stripped = line.lstrip()
        if (
            stripped.startswith("call ")
            or stripped.startswith("tail call ")
            or " = call " in line
            or " = tail call " in line
        ):
            return True
    return False


def ensure_main_thread_init(ir_text: str) -> str:
    text = str(ir_text)
    lines = text.splitlines(keepends=True)
    in_main = False
    main_start = -1
    main_end = -1
    program_args_line = -1
    init_call_lines = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not in_main:
            if not stripped.startswith("define "):
                continue
            if " @main(" not in stripped and ' @"main"(' not in stripped:
                continue
            in_main = True
            main_start = index
            continue
        if (
            "@py_cpy_ensure_init" in stripped
            or '@"py_cpy_ensure_init"' in stripped
        ) and (
            stripped.startswith("call ")
            or " = call " in stripped
            or stripped.startswith("tail call ")
            or " = tail call " in stripped
        ):
            init_call_lines.append(index)
        if (
            "@py_set_program_args" in stripped
            or '@"py_set_program_args"' in stripped
        ) and "call " in stripped:
            program_args_line = index
        if stripped == "}":
            main_end = index
            break
    if main_start < 0 or main_end < 0 or program_args_line < 0:
        return text
    removed_before_args = 0
    for init_call_index in init_call_lines:
        if init_call_index < program_args_line:
            removed_before_args += 1
    for init_call_index in reversed(init_call_lines):
        del lines[init_call_index]
    program_args_line -= removed_before_args
    anchor = lines[program_args_line]
    indent_len = len(anchor) - len(anchor.lstrip())
    line_ending = "\n"
    if anchor.endswith("\r\n"):
        line_ending = "\r\n"
    elif not anchor.endswith("\n"):
        line_ending = ""
    lines.insert(
        program_args_line + 1,
        anchor[:indent_len] + "call void @py_cpy_ensure_init()" + line_ending,
    )
    return "".join(lines)
