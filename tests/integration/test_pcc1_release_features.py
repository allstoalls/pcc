"""Native pcc1 execution gates for the current release's frontend changes."""

from pathlib import Path
import os
import subprocess
import textwrap

import pytest

from tests.host_pcc_pcc1_parity import ParityContractError, verify_pcc1_receipt
from tests.python.python_target_canary import PYTHON_TARGET_SOURCE, PYTHON_TARGET_STDOUT
from tests.python.test_cross_module_int_abi_slots import _APP, _RAW_INT_MODULE
from tests.python.test_self_host_oracle_diff import (
    _selected_pcc1_receipt_path,
    pcc1_self_host_binary,
)
from tests.runtime_build_cache import (
    self_backend_object_cache_key,
    self_host_source_key,
)

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]


def verify_release_compiler(compiler: Path) -> None:
    # The shared fixture provisions a source-addressed compiler or validates
    # PCC1_BINARY/PCC1_RECEIPT. Recheck immediately before each feature compile
    # so replacing its binary after fixture setup cannot qualify another build.
    try:
        verify_pcc1_receipt(
            compiler,
            _selected_pcc1_receipt_path(compiler),
            source_key=self_host_source_key(),
            object_cache_identity=self_backend_object_cache_key(),
        )
    except ParityContractError as exc:
        pytest.fail(f"fresh release pcc1 is not receipt-verified: {exc}")


def compile_and_run(source: Path, compiler: Path) -> str:
    verify_release_compiler(compiler)
    output = source.with_suffix("")
    # No backend/scaffold/libpython flags: exercise the ordinary native defaults.
    env = os.environ.copy()
    for name in ("LC_ALL", "PCC_BACKEND", "PCC_PYTHON_LIBPYTHON", "PCC_IR_SCAFFOLD"):
        env.pop(name, None)
    built = subprocess.run(
        [str(compiler), str(source), "-o", str(output)],
        cwd=source.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    result = subprocess.run(
        [str(output)],
        cwd=source.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    linkage = subprocess.run(
        ["otool", "-L", str(output)], capture_output=True, text=True, timeout=15
    )
    assert linkage.returncode == 0, linkage.stderr
    assert "libpython" not in linkage.stdout.lower(), linkage.stdout
    assert "libllvm" not in linkage.stdout.lower(), linkage.stdout
    return result.stdout


def test_native_pcc1_cross_module_integer_abi(tmp_path, pcc1_self_host_binary):
    (tmp_path / "rawmod.py").write_text(textwrap.dedent(_RAW_INT_MODULE).lstrip())
    app = tmp_path / "app.py"
    app.write_text(textwrap.dedent(_APP).lstrip())
    assert compile_and_run(app, pcc1_self_host_binary) == "sock:-1\n-5 3\n-6 8\n64 -7\n"


def test_native_pcc1_return_survives_parking_finally(tmp_path, pcc1_self_host_binary):
    app = tmp_path / "parking.py"
    app.write_text("""import pcc.virtual_thread as vt
def cleanup():
    vt.yield_now()
def worker():
    try:
        return 42
    finally:
        cleanup()
thread = vt.spawn(worker)
vt.run(1, 64)
print(vt.result(thread))
""")
    assert compile_and_run(app, pcc1_self_host_binary) == "42\n"


def test_native_pcc1_runtime_semantic_target_agrees_with_version_guards(
    tmp_path, pcc1_self_host_binary
):
    app = tmp_path / "semantic_target.py"
    app.write_text(PYTHON_TARGET_SOURCE)
    assert compile_and_run(app, pcc1_self_host_binary) == PYTHON_TARGET_STDOUT


def test_native_pcc1_compiles_package_identity_helpers(tmp_path, pcc1_self_host_binary):
    # Internal pcc modules are not public application imports. Compile the
    # unchanged helper source as a local module to exercise its native body;
    # the rebuilt CLI has a separate package-install integration gate.
    (tmp_path / "candidate_schema.py").write_bytes(
        (REPO / "pcc/package/schema.py").read_bytes()
    )
    app = tmp_path / "identity.py"
    app.write_text(
        "from candidate_schema import distribution_filename_fields, literal_project_metadata_fields, declarative_python_source_build\n"
        'fields = distribution_filename_fields("example-tools-1.2.3.tar.gz")\n'
        "print(fields[0], fields[1])\n"
        'fields = literal_project_metadata_fields(\'[project]\\nname = "example-tools"\\nversion = "1.2.3"\\n\')\n'
        "print(fields[0], fields[1])\n"
        "print(declarative_python_source_build('[build-system]\\nbuild-backend = \"hatchling.build\"\\n'))\n"
        "print(declarative_python_source_build('[build-system]\\nbuild-backend = \"hatchling.build\"\\n[tool.hatch.build.hooks.custom]\\n'))\n"
    )
    assert (
        compile_and_run(app, pcc1_self_host_binary)
        == "example-tools 1.2.3\nexample-tools 1.2.3\nTrue\nFalse\n"
    )


def test_native_pcc1_reads_only_artifact_metadata(tmp_path, pcc1_self_host_binary):
    (tmp_path / "candidate_metadata_paths.py").write_bytes(
        (REPO / "pcc/package/metadata_paths.py").read_bytes()
    )
    package = tmp_path / "package"
    own = package / "example_tools.egg-info/PKG-INFO"
    own.parent.mkdir(parents=True)
    own.write_text("Requires-Dist: actual-dependency\n")
    (package / "pyproject.toml").write_text('[project]\nname="example-tools"\n')
    foreign = package / ".venv/lib/site-packages/foreign.dist-info/METADATA"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("Requires-Dist: unrelated-dependency\n")
    app = tmp_path / "metadata.py"
    app.write_text(
        "from candidate_metadata_paths import package_metadata_paths\n"
        + "paths = package_metadata_paths("
        + repr(str(package))
        + ")\n"
        + "print(len(paths))\n"
        + 'print(paths[0].endswith("example_tools.egg-info/PKG-INFO"))\n'
    )
    assert compile_and_run(app, pcc1_self_host_binary) == "1\nTrue\n"
