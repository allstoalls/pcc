from pathlib import Path
import platform
import subprocess
import sys

from pcc.frontends.python import pipeline


def _compile_page_probe(tmp_path: Path) -> Path:
    source = tmp_path / "page_probe.py"
    llvm_ir = tmp_path / "page_probe.ll"
    source.write_text(
        "from pcc import i64\n"
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import load_i64, page_alloc, page_free, ptr_is_null, store_i64\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export(\"page_roundtrip\")\n"
        "def page_roundtrip(size: i64, value: i64) -> i64:\n"
        "    memory = page_alloc(size)\n"
        "    if ptr_is_null(memory):\n"
        "        return -1\n"
        "    store_i64(memory, size - 8, value)\n"
        "    observed: i64 = load_i64(memory, size - 8)\n"
        "    if page_free(memory, size) != 0:\n"
        "        return -2\n"
        "    return observed\n",
        encoding="utf-8",
    )
    pipeline.compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return llvm_ir


def test_freestanding_page_provider_roundtrips_without_malloc_family(tmp_path):
    llvm_ir = _compile_page_probe(tmp_path)
    obj = tmp_path / "page_probe.o"
    build = subprocess.run(
        ["clang", "-c", str(llvm_ir), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    if sys.platform == "darwin":
        assert set(undefined.stdout.split()) == {"_mmap", "_munmap"}
    else:
        assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
        assert undefined.stdout.strip() == ""
    assert "malloc" not in undefined.stdout
    assert "calloc" not in undefined.stdout
    assert "realloc" not in undefined.stdout
    assert "free" not in undefined.stdout.replace("munmap", "")

    harness = tmp_path / "page_harness.c"
    executable = tmp_path / "page_harness"
    harness.write_text(
        "long page_roundtrip(long size, long value);\n"
        "int main(void) {\n"
        "  return page_roundtrip(4096, 73) == 73 ? 0 : 1;\n"
        "}\n",
        encoding="utf-8",
    )
    link = subprocess.run(
        ["clang", str(harness), str(obj), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_self_backend_page_provider_roundtrips_through_platform_boundary(tmp_path):
    supported_darwin = sys.platform == "darwin" and platform.machine() == "arm64"
    supported_linux = sys.platform.startswith("linux") and platform.machine() == "x86_64"
    assert supported_darwin or supported_linux
    from pcc.backend.self_backend_dispatch import emit_self_asm

    llvm_ir = _compile_page_probe(tmp_path)
    asm = tmp_path / "page_probe.s"
    obj = tmp_path / "page_probe_self.o"
    asm.write_text(
        emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    build = subprocess.run(
        ["clang", "-c", str(asm), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    if supported_darwin:
        assert set(undefined.stdout.split()) == {"_mmap", "_munmap"}
    else:
        assert undefined.stdout.strip() == ""

    harness = tmp_path / "page_self_harness.c"
    executable = tmp_path / "page_self_harness"
    harness.write_text(
        "long page_roundtrip(long size, long value);\n"
        "int main(void) { return page_roundtrip(8192, 91) == 91 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    link = subprocess.run(
        ["clang", str(harness), str(obj), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_linux_x86_64_page_provider_lowers_to_raw_syscalls(tmp_path, monkeypatch):
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin,
        "_target_sys_platform_text",
        lambda self: "linux",
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin,
        "_target_machine_text",
        lambda self: "x86_64",
    )
    llvm_ir = _compile_page_probe(tmp_path)
    ir_text = llvm_ir.read_text(encoding="utf-8")
    assert "@mmap(" not in ir_text
    assert "@munmap(" not in ir_text
    assert ir_text.count("syscall") >= 2

    linux_ir = "\n".join(
        'target triple = "x86_64-unknown-linux-gnu"'
        if line.startswith("target triple = ")
        else line
        for line in ir_text.splitlines()
    )
    asm = emit_self_asm(linux_ir, "x86_64-unknown-linux-gnu")
    assert "syscall" in asm
    assert "call mmap" not in asm
    assert "call munmap" not in asm
