"""Compile and execute C regressions through the public owned PCC API."""

import builtins
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pcc
from pcc import build


def compile_and_run_owned_c(source: str, *, timeout: int = 30):
    """Keep semantic assertions independent of an external LLVM/cc toolchain."""
    with tempfile.TemporaryDirectory(prefix="pcc_owned_c_test_") as tmpdir:
        source_path = Path(tmpdir) / "program.c"
        source_path.write_text(source, encoding="utf-8")
        # Host PCC owns preprocessing, object emission and linking. Any child
        # process during build would bypass this regression's ownership proof.
        with patch(
            "subprocess.Popen",
            side_effect=AssertionError("owned C fixture invoked a build subprocess"),
        ):
            artifact = build(
                source_path,
                kind="exe",
                backend="self",
                optimize=0,
                out_dir=Path(tmpdir) / "output",
                use_compile_cache=False,
            )
        assert artifact.backend == "self"
        return subprocess.run(
            [artifact.output_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def compile_owned_c_with_runtime(
    source: Path, executable: Path, *, runtime_archive: Path, cpp_args=(),
):
    """Emit a C runtime control using an explicitly admitted current archive.

    Unlike the general C helper, this path never provisions a runtime. The
    caller executes the artifact separately so one build can exercise all GCs.
    """
    from pcc.driver.project import TranslationUnit
    from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
    from pcc.frontends.python.pipeline_targets import host_target_triple
    from tests.runtime_fixture_provenance import _verified_test_runtime_archive

    root = Path(pcc.__file__).resolve().parents[1]
    archive, _manifest = _verified_test_runtime_archive(
        runtime_archive, runtime_root=root / "pcc/runtime",
    )
    original_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        if name == "llvmlite" or name.startswith("llvmlite."):
            raise AssertionError("owned C runtime control imported " + name)
        return original_import(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("owned C runtime control attempted an external process or runtime build")

    with (
        patch.dict(os.environ, {
            "PCC_RUNTIME_ARCHIVE": str(archive),
            "PCC_TEST_NO_NATIVE_PROVISIONING": "1",
        }),
        patch("builtins.__import__", checked_import),
        patch("subprocess.Popen", forbidden),
        patch("pcc.frontends.python.owned_runtime_build.build_runtime_archive", forbidden),
    ):
        evaluator = CEvaluator(backend="self", target_triple=host_target_triple())
        units = evaluator.compile_translation_units(
            [TranslationUnit(str(source), str(source), source.read_text())],
            use_system_cpp=False, use_compile_cache=False,
            include_dirs=[str(root / "pcc/runtime/include"), str(root / "utils/fake_libc_include")],
            cpp_args=list(cpp_args),
        )
        # ELF/PE executables always link the target runtime; a Mach-O C
        # executable links only libSystem unless the archive is named.
        link_args = [] if any(
            name in evaluator.target_triple for name in ("linux", "windows")
        ) else [str(archive)]
        evaluator.emit_executable(units, str(executable), link_args=link_args)
    return executable
