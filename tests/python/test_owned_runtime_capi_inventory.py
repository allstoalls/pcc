"""Owned runtime inventory bytes are canonical on every host newline convention."""

import builtins
import hashlib
from pathlib import Path

import pytest

from pcc.frontends.python import owned_runtime_build as owned
from pcc.tools import runtime_archive_provenance as provenance


@pytest.mark.parametrize("host_newline", ["\r\n", "\n"])
@pytest.mark.parametrize("target,prefix", [
    ("x86_64-pc-windows-msvc", ""),
    ("x86_64-unknown-linux-gnu", ""),
    ("arm64-apple-darwin", "_"),
])
def test_owned_runtime_writes_canonical_capi_inventory(
    tmp_path, monkeypatch, host_newline, target, prefix,
):
    runtime = tmp_path / "runtime"
    sources = runtime / "py"
    sources.mkdir(parents=True)
    modules = {
        "first": ["PyZulu", "_PyInternal", "ordinary_helper"],
        "second": ["PyAlpha", "PyZulu"],
    }
    for name in modules:
        (sources / (name + ".py")).write_text("# test runtime source\n", encoding="ascii")
    monkeypatch.setattr(owned, "runtime_modules", lambda *_args: list(modules))
    monkeypatch.setattr(owned, "runtime_ir_passes", lambda *_args: "instsimplify")

    def compile_module(name, source, output, requested_target):
        assert requested_target == target
        ir = f'target triple = "{target}"\n' + "".join(
            f"define i32 @{symbol}() {{\n  ret i32 7\n}}\n"
            for symbol in modules[name]
        )
        Path(output).write_text(ir, encoding="ascii")

    # Parent-local mocks exercise the serial build, not worker processes.
    monkeypatch.setenv("PCC_RUNTIME_BUILD_JOBS", "1")
    monkeypatch.setattr(owned, "_compile_runtime_module", compile_module)

    def host_open(path, mode="r", *args, **kwargs):
        # Emulate Windows default text translation on any test host, while
        # retaining explicit newline choices and the real binary I/O path.
        if "b" not in mode and kwargs.get("newline") is None:
            kwargs["newline"] = host_newline
        return builtins.open(path, mode, *args, **kwargs)

    monkeypatch.setattr(owned, "open", host_open, raising=False)
    archive = tmp_path / "output" / "libpy_runtime_pcc_py.a"
    owned.build_runtime_archive(str(runtime), str(archive), target)

    symbols = sorted(prefix + name for name in ("PyAlpha", "PyZulu", "_PyInternal"))
    expected = ("\n".join(symbols) + "\n").encode("ascii")
    inventory = Path(str(archive) + ".capi_syms")
    assert inventory.read_bytes() == expected
    assert provenance._load_capi_inventory(inventory) == (expected, symbols)
    manifest = provenance.verify_runtime_archive_manifest(archive, runtime_root=runtime)
    assert manifest["capi_symbols"] == symbols
    assert manifest["capi_symbol_count"] == 3
    assert manifest["capi_inventory_sha256"] == hashlib.sha256(expected).hexdigest()
    assert manifest["member_count"] == 2
    assert manifest["target_triple"] == target
    assert not Path(str(archive) + ".tmp").exists()
    assert not Path(str(archive) + ".tmp.capi_syms").exists()
    assert not (runtime / ".pcc-runtime-build.lock").exists()


@pytest.mark.parametrize("content", [
    b"PyAlpha\r\n_PyInternal\r\n",
    b"PyAlpha\r_PyInternal\r",
    b"PyAlpha\n_PyInternal",
    b"PyAlpha\n\n_PyInternal\n",
    b"_PyInternal\nPyAlpha\n",
    b"PyAlpha\nPyAlpha\n",
    b"PyAlph\xff\n",
    b"ordinary_helper\n",
    b"",
])
def test_capi_inventory_parser_still_rejects_noncanonical_bytes(tmp_path, content):
    inventory = tmp_path / "invalid.capi_syms"
    inventory.write_bytes(content)
    with pytest.raises(provenance.ProvenanceError):
        provenance._load_capi_inventory(inventory)
