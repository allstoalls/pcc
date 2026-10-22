"""Native str.encode binding and UTF-8/ASCII/Latin-1 error-handler semantics."""
from __future__ import annotations

import ast
import encodings.aliases
import os
from pathlib import Path
import re
import random
import subprocess
import sys

import pytest

from pcc.frontends.python.pipeline import compile_python
from pcc.runtime.py import py_abi_constants as abi
from tests.owned_ir_validation import verify_ir_text

ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_TREE = ast.parse((ROOT / "pcc/runtime/py/py_str_accessors.py").read_text())
_RUNTIME_TREE.body = [node for node in _RUNTIME_TREE.body if isinstance(node, ast.FunctionDef)]
_RUNTIME_FUNCTIONS = compile(_RUNTIME_TREE, "py_str_accessors.py", "exec")


class _Memory:
    def __init__(self, data=b"", offset=0, owner=None, tag=abi.PY_TYPE_STR):
        self.data = bytearray(data) if owner is None else owner.data
        self.offset = offset
        self.owner = self if owner is None else owner
        self.tag = tag
        self.live = True
        self.text = None


class _EncoderModel:
    def __init__(self):
        self.error = None
        self.allocations = []
        self.inputs = []
        namespace = dict(vars(abi))
        namespace.update({
            "c_abi_export": lambda _name: lambda fn: fn,
            "ptr_is_null": lambda p: int(p is None),
            "is_tagged_int": lambda p: isinstance(p, int),
            "ptr_add": lambda p, n: _Memory(offset=p.offset + n, owner=p.owner),
            "load_i8": self.load_byte,
            "load_i32": lambda p, _offset: p.tag,
            "load_i64": lambda p, _offset: len(p.data) - abi.PYSTROBJECT_DATA_OFFSET,
            "store_i8": lambda p, n, v: p.data.__setitem__(p.offset + n, v & 255),
            "cstr": lambda s: _Memory(s.encode() + b"\0"),
            "null": lambda: None,
            "malloc": self.allocate,
            "free": self.free,
            "py_bytes_new": self.bytes_new,
            "py_exc_new": lambda code, message: {2: ValueError, 3: TypeError,
                11: NotImplementedError, 13: LookupError, 15: OverflowError,
                19: MemoryError}[code](bytes(message.data).rstrip(b"\0").decode()),
            "py_raise_owned": self.raise_error,
            "py_unicode_encode_error": self.unicode_error,
        })
        exec(_RUNTIME_FUNCTIONS, namespace)
        self.namespace = namespace

    def load_byte(self, p, n):
        assert p.owner.live, "read after source moved or scratch was freed"
        return p.data[p.offset + n]

    def allocate(self, n):
        p = _Memory(bytes(n))
        self.allocations.append(p)
        return p

    def free(self, p):
        assert p.live
        p.live = False

    def bytes_new(self, data, n):
        assert data.live
        result = bytes(data.data[data.offset:data.offset + n])
        # Result allocation can relocate every input; no raw source pointer may
        # be used by the encoder after this point.
        for value in self.inputs:
            value.live = False
        return result

    def raise_error(self, error):
        self.error = error

    def unicode_error(self, obj, encoding, start, end, reason):
        self.error = UnicodeEncodeError(bytes(encoding.data).rstrip(b"\0").decode(),
            obj.text, start, end, bytes(reason.data).rstrip(b"\0").decode())

    def object(self, value):
        if isinstance(value, str):
            p = _Memory(bytes(abi.PYSTROBJECT_DATA_OFFSET) + value.encode("utf8", "surrogatepass"))
            p.text = value
            self.inputs.append(p)
            return p
        if isinstance(value, int):
            return value
        return _Memory(tag=abi.PY_TYPE_NONE)

    def encode(self, value, codec="utf8", errors="strict"):
        result = self.namespace["_str_encode_names_leased"](
            self.object(value), self.object(codec), self.object(errors))
        assert not any(p.live for p in self.allocations), "scratch allocation leaked"
        if self.error is not None:
            raise self.error
        return result


def _outcome(call):
    try:
        return ("value", call())
    except UnicodeEncodeError as error:
        return (type(error), error.args)
    except Exception as error:
        return (type(error),)


@pytest.mark.parametrize("codec", ("utf-8", "UTF8", "u8", "_utf  8_", "ascii", "us-ascii", "646", "latin1", "iso-8859-1", "cp819"))
@pytest.mark.parametrize("mode", ("strict", "ignore", "replace", "surrogateescape", "surrogatepass", "backslashreplace", "xmlcharrefreplace", "unknown", "IGNORE"))
def test_encoder_source_matches_cpython_bytes_and_error_ranges(codec, mode):
    for value in ("", "abc\0XYZ", "é日本🙂", "A\udc80\ud800\udcffZ", "Aé\udc80\ud800Z", "A\ud800\udc80Z", "\U0010ffff"):
        assert _outcome(lambda: _EncoderModel().encode(value, codec, mode)) == _outcome(lambda: value.encode(codec, mode)), (repr(value), codec, mode)


@pytest.mark.parametrize("codec,mode", (("bogus", "strict"), ("u-t-f-8", "strict"), (None, "strict"), (7, "strict"), ("utf8", None), ("utf8", 7), ("utf\0-8", "strict"), ("utf8", "strict\0"), ("\udc80", "strict"), ("utf8", "\ud800")))
def test_encoder_source_validates_argument_types_and_names(codec, mode):
    for value in ("abc", "é", "\udc80"):
        assert _outcome(lambda: _EncoderModel().encode(value, codec, mode)) == _outcome(lambda: value.encode(codec, mode))


@pytest.mark.parametrize("codec", [name for name, owner in encodings.aliases.aliases.items()
                                   if owner in ("utf_8", "ascii", "latin_1")])
def test_all_builtin_aliases_of_owned_codecs(codec):
    for value in ("abc", "é日本🙂", "A\udc80\ud800Z"):
        assert _outcome(lambda: _EncoderModel().encode(value, codec, "replace")) == _outcome(lambda: value.encode(codec, "replace"))


def test_encoder_randomized_unicode_runs():
    rng = random.Random(431)
    alphabet = [0, 1, 65, 127, 128, 255, 256, 2047, 2048, 55295, 55296,
                56319, 56320, 56447, 56448, 56575, 56576, 57343, 57344,
                65535, 65536, 1114111]
    for _case in range(80):
        value = "".join(chr(rng.choice(alphabet)) for _index in range(rng.randrange(12)))
        for codec in ("utf8", "ascii", "latin1"):
            for mode in ("strict", "ignore", "replace", "surrogateescape",
                         "surrogatepass", "backslashreplace", "xmlcharrefreplace"):
                assert _outcome(lambda: _EncoderModel().encode(value, codec, mode)) == _outcome(lambda: value.encode(codec, mode)), (repr(value), codec, mode)


def test_encode_binder_keeps_native_bodies(tmp_path):
    path = tmp_path / "encode_binding.py"
    path.write_text('''
def archive(name: str):
    return name.encode("utf-8", "surrogateescape")
def dynamic(value, codec, errors):
    return value.encode(errors=errors, encoding=codec)
def default(value: str):
    return value.encode()
def errors_only(value: str):
    return value.encode(errors="surrogateescape")
def duplicate(value: str):
    return value.encode("utf8", encoding="ascii")
def unknown(value: str):
    return value.encode(unknown="ascii")
def excess(value: str):
    return value.encode("utf8", "strict", "extra")
''')
    output = tmp_path / "encode_binding.ll"
    compile_python(str(path), str(output), backend="self", libpython_mode="off",
                   emit_llvm_only=True, python_library=True)
    text = output.read_text()
    verify_ir_text(text)
    for name in ("archive", "dynamic", "default", "errors_only", "duplicate", "unknown", "excess"):
        body = re.search(r"^define[^\n]*@user_encode_binding_" + name + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
        assert body, name
        assert "strict.nolib.stub" not in body[1], name
        assert "@py_cpy_" not in body[1], name
        assert ("@py_raise(" if name in ("duplicate", "unknown", "excess") else "@py_str_encode_with_encoding(") in body[1], name


def test_encoded_constructors_root_and_check_errors(tmp_path):
    path = tmp_path / "encode_constructors.py"
    path.write_text('''
def immutable(value: str):
    return bytes(value, "utf-8")
def mutable(value: str):
    return bytearray(value, "latin-1")
''')
    output = tmp_path / "encode_constructors.ll"
    compile_python(str(path), str(output), backend="self", libpython_mode="off",
                   emit_llvm_only=True, python_library=True)
    text = output.read_text()
    verify_ir_text(text)
    for name, helper in (("immutable", "py_str_utf8_encode"), ("mutable", "py_str_latin1_encode")):
        body = re.search(r"^define[^\n]*@user_encode_constructors_" + name + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
        assert body, name
        assert "strict.nolib.stub" not in body[1], name
        assert "@" + helper + "(" in body[1], name
        assert "@py_err_occurred(" in body[1], name
        assert "@pcc_gc_take_pinned_slot(" in body[1], name
        assert "@pcc_gc_store_root(" in body[1], name


def test_encoder_scoped_runtime_ir(tmp_path):
    tree = ast.parse((ROOT / "pcc/runtime/py/py_str_accessors.py").read_text())
    tree.body = [node for node in tree.body if not isinstance(node, ast.FunctionDef)
                 or node.name.startswith("_encode_") or node.name in (
                     "_utf8_ord_at_byte", "_str_encode_codec", "_str_encode_names_leased",
                     "_str_encode_guarded", "py_str_encode_with_encoding",
                     "py_str_utf8_encode", "py_str_ascii_encode", "py_str_latin1_encode")]
    source = tmp_path / "py_runtime_encoding" / "py" / "encoding_runtime.py"
    source.parent.mkdir(parents=True)
    source.write_text(ast.unparse(tree))
    output = tmp_path / "encoding_runtime.ll"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   emit_llvm_only=True, python_library=True)
    text = output.read_text()
    verify_ir_text(text)
    assert "strict.nolib.stub" not in text
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)
    assert "@py_unicode_encode_error(" in text
    assert re.search(r"^define[^\n]*@py_str_encode_with_encoding\(ptr[^,]*, ptr[^,]*, ptr[^)]*\)", text, re.M)


def test_encoder_runtime_abi_has_no_stubs(tmp_path):
    output = tmp_path / "py_str_accessors.ll"
    compile_python(str(ROOT / "pcc/runtime/py/py_str_accessors.py"), str(output),
                   backend="self", libpython_mode="off", emit_llvm_only=True, python_library=True)
    text = output.read_text()
    verify_ir_text(text)
    assert re.search(r"^define[^\n]*@py_str_encode_with_encoding\(ptr[^,]*, ptr[^,]*, ptr[^)]*\)", text, re.M)
    assert "strict.nolib.stub" not in text
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)


PROGRAM = r'''
trace = []
def mark(name, value):
    trace.append(name)
    return value
def dynamic(value, codec, errors):
    return value.encode(encoding=codec, errors=errors)
def immutable(value: str):
    return bytes(value, "utf-8")
def mutable(value: str):
    return bytearray(value, "latin-1")
def main():
    print(immutable(mark("bytes source", "café")), mutable(mark("bytearray source", "café")))
    try:
        immutable(chr(0xd800))
    except UnicodeEncodeError as error:
        print("bytes error", error.encoding, error.start, error.end)
    try:
        mutable("日本")
    except UnicodeEncodeError as error:
        print("bytearray error", error.encoding, error.start, error.end)
    escaped = b"A\x80\xff\xc3\xa9Z".decode("utf8", "surrogateescape")
    print(escaped.encode("utf8", "surrogateescape"))
    print(mark("receiver", "café").encode(errors=mark("errors", "replace"), encoding=mark("encoding", "ascii")))
    print(trace)
    for value in ("", "ASCII\0", "café日本🙂", escaped, chr(0xd800) + chr(0xdc80)):
        for codec in ("utf8", "ASCII", "latin-1"):
            for errors in ("strict", "ignore", "replace", "surrogateescape", "surrogatepass", "backslashreplace", "xmlcharrefreplace", "bogus"):
                try:
                    print(dynamic(value, codec, errors))
                except UnicodeEncodeError as error:
                    print("unicode", error.encoding, error.start, error.end, error.reason)
                    print(error.object == value, error.args == (error.encoding, value, error.start, error.end, error.reason))
                    print(str(error))
                    print(repr(error))
                except LookupError:
                    print("lookup")
    caught = 0
    try:
        "abc".encode("utf8", encoding=mark("duplicate", "ascii"))
    except TypeError:
        caught += 1
    try:
        "abc".encode(unknown=mark("unknown", "ascii"))
    except TypeError:
        caught += 1
    try:
        "abc".encode("utf8", "strict", mark("extra", "extra"))
    except TypeError:
        caught += 1
    for codec in (7, None):
        try:
            "abc".encode(codec)
        except TypeError:
            caught += 1
    for errors in (7, None):
        try:
            "abc".encode(errors=errors)
        except TypeError:
            caught += 1
    print(caught, trace)
main()
'''


CLEANUP_PROGRAM = '''
import gc
trace = []
anchor = "café"
class Token:
    def __del__(self):
        trace.append("destroyed")
def fail():
    raise ValueError("argument")
def replace():
    global anchor
    anchor = "changed"
    gc.collect()
    return "ascii"
def main():
    caught = 0
    for index in range(12):
        try:
            "abc".encode(errors=Token(), encoding=fail())
        except ValueError:
            caught += 1
        try:
            "abc".encode(encoding=Token(), errors=fail())
        except ValueError:
            caught += 1
        try:
            "abc".encode(Token(), unknown=Token())
        except TypeError:
            caught += 1
        try:
            "abc".encode(errors=Token())
        except TypeError:
            caught += 1
    print(anchor.encode(errors="replace", encoding=replace()))
    gc.collect()
    print(caught, len(trace))
main()
'''


@pytest.mark.integration
@pytest.mark.parametrize("program", (PROGRAM, CLEANUP_PROGRAM), ids=("unicode_bytes_errors", "finalizers_and_rebinding"))
def test_encode_executes_all_collectors(tmp_path, pcc_diagnostic_runtime_archive, python_program_compiler, monkeypatch, program):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    path = tmp_path / "encode_execution.py"
    path.write_text(program)
    oracle = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=15)
    assert oracle.returncode == 0, oracle.stderr
    binary = tmp_path / "encode_execution"
    python_program_compiler(str(path), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_diagnostic_runtime_archive))
    for backend in range(5):
        actual = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                                capture_output=True, text=True, timeout=30)
        assert actual.returncode == 0, (backend, actual.stdout, actual.stderr)
        assert actual.stdout == oracle.stdout, (backend, actual.stdout, oracle.stdout)
