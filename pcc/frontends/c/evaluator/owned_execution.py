"""Execute owned C objects and transport scalar results without exit-code loss."""

from __future__ import annotations

import math
import os
import re
import struct
import subprocess
import sys
import tempfile

from pcc.backend import BackendUnavailable
from pcc.backend.self_backend_parse import decode_global_name, parse_self_backend_module
from pcc.driver.project import TranslationUnit
from pcc.frontends.python.pipeline_ir_text import rename_llvm_global_refs


def _c_byte_string(value):
    data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return '"' + "".join("\\%03o" % byte for byte in data) + '"'


def _scalar_type(ty, *, unsigned=False):
    if ty.kind == "void":
        return "void", None
    if ty.kind == "int":
        types = {1: ("unsigned char", "B"), 8: ("signed char", "b"),
                 16: ("short", "h"), 32: ("int", "i"), 64: ("long long", "q")}
        if ty.width in types:
            c_type, format_code = types[ty.width]
            if unsigned and ty.width != 1:
                c_type = "unsigned " + ("char" if ty.width == 8 else c_type)
                format_code = format_code.upper()
            return c_type, format_code
    if ty.kind == "fp":
        return ("float", "f") if ty.width == 32 else ("double", "d")
    if ty.kind == "ptr":
        return "void *", "Q"
    raise BackendUnavailable("owned C evaluation cannot transport result type " + repr(ty))


def evaluate_units(evaluator, units, *, entry="main", args=None, prog_args=None,
                   optimize=True, base_dir=None, link_args=None, dump_dir=None,
                   return_descriptor=None):
    """Compile a typed result writer alongside the program and execute it.

    stdout/stderr retain their ordinary meaning. The return value travels in a
    private binary file, so negative/wide integers, floats and void do not depend
    on the platform's eight-bit process status. All code is emitted and linked
    by pcc. Pointer arguments must refer to data owned by the child process.
    """
    if evaluator.is_cross:
        raise BackendUnavailable("cannot execute C for a foreign target")
    functions = []
    for unit in units:
        functions.extend(parse_self_backend_module(unit[1]).functions)
    matches = [function for function in functions if function.name == entry]
    if len(matches) != 1:
        raise ValueError("expected one definition of C entry " + repr(entry))
    function = matches[0]
    symbols = set()
    # Declarations and data symbols occupy the same linker namespace as
    # definitions. Include all references before choosing bridge symbols.
    for unit in units:
        for match in re.finditer(r'@("(?:\\.|[^"\\])*"|[-a-zA-Z$._0-9]+)', unit[1]):
            symbols.add(decode_global_name("@" + match.group(1)))
    reserved = "__pcc_evaluate_entry"
    while reserved in symbols:
        reserved += "_"
    unsigned_return = any(
        '"pcc-c-unsigned-return"' in line
        and re.match(r'^\s*define\b.*@(?:"' + re.escape(entry) + r'"|' + re.escape(entry) + r')\(', line)
        for unit in units for line in unit[1].splitlines()
    )
    result_type, result_format = _scalar_type(function.ret_type, unsigned=unsigned_return)
    parameter_types = [_scalar_type(arg.type)[0] for arg in function.args]
    supplied = list(args or ())
    host_pointer_args = any(ty == "void *" and value is not None
                            and not isinstance(value, (str, bytes))
                            and not (isinstance(value, int) and value == 0)
                            for value, ty in zip(supplied, parameter_types))
    load_in_process = function.ret_type.kind == "ptr" or host_pointer_args
    if load_in_process and (
        sys.implementation.name != "cpython"
        or not evaluator.target_triple.startswith(("arm64-apple-", "aarch64-apple-"))
    ):
        raise BackendUnavailable("owned host pointer execution is unavailable for this target or entrypoint")
    if prog_args and supplied:
        raise ValueError("program arguments cannot be combined with raw C arguments")
    if entry == "main" and not supplied and parameter_types == ["int", "void *"]:
        call_arguments = ["argc", "argv"]
    else:
        if prog_args and parameter_types:
            raise ValueError("program arguments require main(int, char **) without raw args")
        if len(supplied) != len(parameter_types):
            raise ValueError("C entry expects " + str(len(parameter_types)) + " arguments")
        call_arguments = []
        for value, ty in zip(supplied, parameter_types):
            if ty == "void *":
                if isinstance(value, (str, bytes)):
                    call_arguments.append(_c_byte_string(value))
                elif value is None or value == 0:
                    call_arguments.append("0")
                elif load_in_process:
                    call_arguments.append("0")
                else:
                    raise BackendUnavailable("owned C evaluation cannot pass a host pointer to a child")
            elif ty in ("float", "double"):
                number = float(value)
                if math.isnan(number):
                    call_arguments.append("(0.0 / 0.0)")
                elif math.isinf(number):
                    call_arguments.append("(-1.0 / 0.0)" if number < 0 else "(1.0 / 0.0)")
                else:
                    call_arguments.append(number.hex())
            else:
                number = int(value)
                call_arguments.append(str(number) + ("ULL" if number > 2 ** 63 - 1 else "LL"))
    renamed = []
    rename_map = {entry: reserved}
    if entry != "main" and "main" in symbols:
        original_main = reserved + "_original_main"
        while original_main in symbols:
            original_main += "_"
        rename_map["main"] = original_main
    for name, ir_text, return_type, definitions in units:
        renamed_text = rename_llvm_global_refs(ir_text, rename_map)
        if not function.is_global:
            # Only the chosen private entry crosses into the result bridge.
            # Other TU-private functions keep their ordinary linkage.
            lines = []
            for line in renamed_text.splitlines(keepends=True):
                if re.match(r'^\s*define\b.*@(?:"' + re.escape(reserved) + r'"|' + re.escape(reserved) + r')\(', line):
                    line = re.sub(r'^(\s*define\s+)(?:internal|private)\s+', r'\1', line)
                lines.append(line)
            renamed_text = "".join(lines)
        renamed.append((name, renamed_text,
                        return_type, [(kind, rename_map.get(symbol, symbol), display)
                                      for kind, symbol, display in definitions]))
    if load_in_process:
        # Keep the host ABI adapter behind the same implementation boundary
        # used by native lowering and dependency discovery. The local import
        # proves the guard's owner without depending on global alias analysis.
        import sys as execution_runtime
        if execution_runtime.implementation.name == "cpython":
            if link_args:
                raise BackendUnavailable("owned host pointer execution does not yet accept extra link arguments")
            from pcc.backend.host_owned_load import call_function

            prepared = evaluator._prepare_self_backend_units(renamed, optimize=optimize) if optimize else renamed
            if return_descriptor is None:
                return_descriptor = matches and next((unit[2] for unit in units if entry in [fn.name for fn in parse_self_backend_module(unit[1]).functions]), None)
            native_args = supplied
            if entry == "main" and not supplied and parameter_types == ["int", "void *"]:
                import ctypes
                arguments = [b"pcc"] + [str(arg).encode("utf-8") for arg in (prog_args or ())]
                argv = (ctypes.c_char_p * (len(arguments) + 1))(*arguments, None)
                native_args = [len(arguments), argv]
            return call_function(evaluator, prepared, evaluator.target_triple, reserved,
                                 function, native_args, return_descriptor, unsigned_return, base_dir=base_dir)
        raise BackendUnavailable("owned host pointer execution is unavailable for this target or entrypoint")
    with tempfile.TemporaryDirectory(prefix="pcc_c_evaluate_") as temporary:
        result_path = os.path.join(temporary, "result.bin")
        call = reserved + "(" + ", ".join(call_arguments) + ")"
        statement = call + ";" if result_type == "void" else result_type + " value = " + call + ";"
        writer = "" if result_type == "void" else "if (fwrite(&value, sizeof(value), 1, stream) != 1) return 121;"
        wrapper = (
            "extern " + result_type + " " + reserved + "(" + (", ".join(parameter_types) or "void") + ");\n"
            "int main(int argc, char **argv) {\n" + statement + "\n"
            "void *stream = fopen(" + _c_byte_string(os.fsencode(result_path)) + ", \"wb\");\n"
            "if (!stream) return 120;\n" + writer + "\n"
            "if (fclose(stream) != 0) return 122;\nreturn 0;\n}\n"
        )
        wrapped = evaluator.compile_translation_units(
            [TranslationUnit(name="__pcc_result.c", path="", source=wrapper)],
            use_system_cpp=False, use_compile_cache=False, frontend_opt_level=0,
        )
        prepared = evaluator._prepare_self_backend_units(renamed + wrapped, optimize=optimize) if optimize else renamed + wrapped
        if dump_dir:
            from pcc.frontends.c.evaluator.c_evaluator import _write_llvm_dump
            _write_llvm_dump(dump_dir, "temp.ooptimize.bcode", "\n".join(unit[1] for unit in prepared))
            _write_llvm_dump(dump_dir, "temp.bcode", evaluator._self_backend_asm_text(prepared))
        executable = os.path.join(temporary, "program.exe" if os.name == "nt" else "program")
        evaluator.emit_executable(prepared, executable, optimize=False, link_args=link_args)
        completed = subprocess.run([executable] + [str(arg) for arg in (prog_args or ())],
                                   cwd=base_dir or os.getcwd(), timeout=120)
        if completed.returncode:
            raise RuntimeError("owned C evaluation exited before returning a value: " + str(completed.returncode))
        with open(result_path, "rb") as stream:
            payload = stream.read()
        if result_format is None:
            if payload:
                raise RuntimeError("unexpected payload from void C entry")
            return None
        if len(payload) != struct.calcsize("<" + result_format):
            raise RuntimeError("incomplete owned C result payload")
        return struct.unpack("<" + result_format, payload)[0]
