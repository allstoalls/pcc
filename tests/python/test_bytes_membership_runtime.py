"""Generic bytes containment must not silently report every match absent."""

from pathlib import Path
import subprocess


def test_bytes_and_bytearray_membership(tmp_path: Path, pcc_runtime_archive, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    archive = pcc_runtime_archive
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))
    source = tmp_path / "membership.py"
    executable = tmp_path / "membership"
    source.write_text('''def check(container):
    print(b"200 OK" in container)
    print(72 in container)
    print(b"missing" not in container)
    print(b"" in container)
check(b"HTTP/1.1 200 OK")
check(bytearray(b"HTTP/1.1 200 OK"))
''', encoding="utf-8")
    compile_python(str(source), str(executable), backend="self",
                   ir_scaffold_mode="on", libpython_mode="off")
    ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.splitlines() == ["True"] * 8
