from __future__ import annotations

import os
import subprocess
import textwrap

from pcc.frontends.python import parser, type_infer
from pcc.frontends.python.py_ast import Assign, BytesLit, BytesType, Name
from pcc.frontends.python.pipeline import compile_python


def test_parser_preserves_bytes_literal_as_byteslit():
    mod = parser.parse('x = b"abc\\xff"\n', "bytes_probe.py")
    stmt = mod.body[0]
    assert isinstance(stmt, Assign)
    assert isinstance(stmt.value, BytesLit)
    assert stmt.value.value == b"abc\xff"


def test_type_infer_preserves_bytes_type():
    mod = parser.parse('x = b"abc"\ny = x\n', "bytes_probe.py")
    inferred = type_infer.infer_module(mod)
    first = inferred.body[0]
    second = inferred.body[1]
    assert isinstance(first, Assign)
    assert isinstance(first.value, BytesLit)
    assert isinstance(first.value.ty, BytesType)
    assert isinstance(second, Assign)
    assert isinstance(second.value, Name)
    assert isinstance(second.value.ty, BytesType)


def test_runtime_bytes_len_getitem_and_slice_native(tmp_path, pcc_runtime_archive):
    src = tmp_path / "bytes_probe.c"
    exe = tmp_path / "bytes_probe"
    src.write_text(
        textwrap.dedent(
            r"""
            #include "py_runtime.h"
            #include <stdio.h>

            int main(void) {
                PyObject *b = py_bytes_new("A\xffZ", 3);
                PyObject *one = py_int_from_i64(1);
                PyObject *none = py_None;
                PyObject *item = py_bytes_getitem(b, one);
                PyObject *slice = py_bytes_slice(b, one, none, none);
                if (py_bytes_len(b) != 3) return 1;
                if (py_int_to_i64(item, 0) != 255) return 2;
                if (py_bytes_len(slice) != 2) return 3;
                PyObject *slice0 = py_bytes_getitem(slice, py_int_from_i64(0));
                PyObject *slice1 = py_bytes_getitem(slice, py_int_from_i64(1));
                if (py_int_to_i64(slice0, 0) != 255) return 4;
                if (py_int_to_i64(slice1, 0) != 90) return 5;
                PyObject *cat = py_bytes_concat(b, py_bytearray_from_obj(slice));
                if (py_bytes_len(cat) != 5) return 6;
                PyObject *cat4 = py_bytes_getitem(cat, py_int_from_i64(4));
                if (py_int_to_i64(cat4, 0) != 90) return 7;
                PyObject *rep = py_bytes_repeat(slice, 3);
                if (py_bytes_len(rep) != 6) return 8;
                PyObject *rep2 = py_bytes_getitem(rep, py_int_from_i64(2));
                if (py_int_to_i64(rep2, 0) != 255) return 9;
                PyObject *ba = py_bytearray_from_obj(b);
                PyObject *ba_rep = py_bytes_repeat(ba, 2);
                if (py_bytearray_setitem(ba_rep, py_int_from_i64(0), py_int_from_i64(65)) != 0) return 10;
                PyObject *upper = py_bytes_upper(py_bytes_new("a1z", 3));
                if (py_int_to_i64(py_bytes_getitem(upper, py_int_from_i64(0)), 0) != 65) return 11;
                if (py_int_to_i64(py_bytes_getitem(upper, py_int_from_i64(1)), 0) != 49) return 12;
                if (py_int_to_i64(py_bytes_getitem(upper, py_int_from_i64(2)), 0) != 90) return 13;
                PyObject *ba_upper = py_bytes_upper(py_bytearray_from_obj(py_bytes_new("az", 2)));
                if (py_bytearray_setitem(ba_upper, py_int_from_i64(0), py_int_from_i64(65)) != 0) return 14;
                printf("bytes-ok\n");
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-I",
            str(pcc_runtime_archive.parent / "include"),
            str(src),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(exe),
        ],
        check=True,
    )
    proc = subprocess.run([str(exe)], check=True, text=True, capture_output=True)
    assert proc.stdout.strip() == "bytes-ok"


def test_bytes_concat_and_repeat_self_backend(tmp_path):
    src = tmp_path / "bytes_concat_repeat.py"
    src.write_text(
        textwrap.dedent(
            """
            def make(major: int, minor: int, padlen: int):
                header = b"\\x93NUMPY" + bytes([major, minor])
                trailer = b" " * padlen + b"\\n"
                return header + trailer

            out = make(1, 0, 3)
            print(len(out))
            print(out[0])
            print(out[6])
            print(out[7])
            print(out[8])
            print(out[11])

            data = bytearray(b"a") + b"b"
            print(len(data))
            print(data[0])
            print(data[1])
            data[0] = 90
            print(data[0])
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "bytes_concat_repeat.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "12",
        "147",
        "1",
        "0",
        "32",
        "10",
        "2",
        "97",
        "98",
        "90",
    ]
