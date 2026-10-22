"""Native durable-file primitives required by persistent PCC applications."""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path


def _compile_to_ir(tmp_path: Path, source: str, name: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    input_path = tmp_path / (name + ".py")
    output_path = tmp_path / (name + ".ll")
    input_path.write_text(source, encoding="utf-8")
    compile_python(
        str(input_path),
        str(output_path),
        emit_llvm_only=True,
        ir_scaffold_mode="on",
    )
    return output_path.read_text(encoding="utf-8")


def _function_body(ir_text: str, suffix: str) -> str:
    match = re.search(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        ir_text,
        re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def test_durable_file_surface_lowers_without_cpython(tmp_path: Path) -> None:
    source = textwrap.dedent(
        """
        import os

        def persist(source: str, destination: str) -> None:
            with open(source, "x", encoding="utf-8") as stream:
                stream.write("durable")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(source, 0o600)
            os.replace(source, destination)
            os.unlink(destination)
        """
    )
    body = _function_body(
        _compile_to_ir(tmp_path, source, "durable_file_lowering"), "persist"
    )
    for symbol in (
        "py_file_open",
        "py_file_fileno",
        "py_os_fsync",
        "py_os_chmod",
        "py_os_replace",
        "py_os_unlink",
    ):
        assert "@" + symbol in body, body
    assert "cpy." not in body, body


def test_durable_file_surface_round_trip(tmp_path: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    source_path = tmp_path / "durable_source.txt"
    destination_path = tmp_path / "durable_destination.txt"
    program = tmp_path / "durable_file_round_trip.py"
    executable = tmp_path / "durable_file_round_trip"
    program.write_text(
        textwrap.dedent(
            f"""
            import os

            SOURCE = {str(source_path)!r}
            DESTINATION = {str(destination_path)!r}

            def main() -> None:
                with open(SOURCE, "x", encoding="utf-8") as stream:
                    stream.write("durable")
                    stream.flush()
                    os.fsync(stream.fileno())
                duplicate_failed = False
                try:
                    with open(SOURCE, "x", encoding="utf-8") as duplicate:
                        duplicate.write("wrong")
                except OSError:
                    duplicate_failed = True
                os.chmod(SOURCE, 0o600)
                os.replace(SOURCE, DESTINATION)
                with open(DESTINATION, "r", encoding="utf-8") as stream:
                    print(stream.read())
                print(duplicate_failed)
                os.unlink(DESTINATION)
                print(os.path.exists(DESTINATION))

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(str(program), str(executable), ir_scaffold_mode="on")
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "durable\nTrue\nFalse\n"
    assert not source_path.exists()
    assert not destination_path.exists()


def test_linux_durable_file_intrinsics_use_raw_syscalls(
    tmp_path: Path, monkeypatch
) -> None:
    from pcc.frontends.python import pipeline
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    source = tmp_path / "durable_file_intrinsics.py"
    llvm_ir = tmp_path / "durable_file_intrinsics.ll"
    source.write_text(
        "from pcc import i64\n"
        "from pcc.extern import c_abi_export, c_ptr\n"
        "from pcc.unsafe import chmod_file, rename_file, sync_file\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('mutate')\n"
        "def mutate(source: c_ptr, destination: c_ptr, fd: i64) -> i64:\n"
        "    return rename_file(source, destination) + chmod_file(destination, 384) + sync_file(fd)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_sys_platform_text", lambda self: "linux"
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_machine_text", lambda self: "x86_64"
    )
    pipeline.compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
        target_triple="x86_64-unknown-linux-gnu",
    )
    text = llvm_ir.read_text(encoding="utf-8")
    assert "unsafe.rename_file.syscall" in text
    assert "unsafe.chmod_file.syscall" in text
    assert "unsafe.sync_file.syscall" in text
    for symbol in ("rename", "chmod", "fsync"):
        assert "declare i32 @" + symbol + "(" not in text


def test_host_exclusive_open_preserves_first_writer(tmp_path: Path) -> None:
    path = tmp_path / "exclusive.txt"
    with open(path, "x", encoding="utf-8") as stream:
        stream.write("first")
    try:
        with open(path, "x", encoding="utf-8") as stream:
            stream.write("second")
    except OSError:
        pass
    assert path.read_text(encoding="utf-8") == "first"
    assert os.stat(path).st_size == 5
