"""Native target passes retain rewritten branch strings across later scans."""

import os
from pathlib import Path
import subprocess

from pcc.backend.self_backend_aarch64_darwin import (
    _thread_trampoline_branches,
    _fold_cond_branch_to_fallthrough,
    _drop_fallthrough_uncond_branches,
)


_SOURCE = '''  b L_test_component_whiledotbodydot2050
L_pcc_smap_3c1d32b46aca4704_15:
  b L_test_component_whiledotbodydot2057dotouterdotouter
L_test_component_whiledotbodydot2057dotouterdotouter:
  b L_test_component_whiledotbodydot2057dotouter
'''


def test_native_threading_preserves_branch_string_owners(tmp_path, pcc_diagnostic_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    selected = os.environ.get("PCC_TEST_TARGET_PASS_DRIVER")
    if selected:
        binary = Path(selected).resolve(strict=True)
    else:
        driver = Path(__file__).resolve().parents[2] / "pcc/backend/owned_target_pass_driver.py"
        binary = tmp_path / "target-pass"
        compile_python(str(driver), str(binary), backend="self", libpython_mode="off",
                       ir_scaffold_mode="on", runtime_archive=str(pcc_diagnostic_runtime_archive))
    source, edges = tmp_path / "input.s", tmp_path / "edges.txt"
    source.write_text(_SOURCE)
    edges.write_text("")
    expected = _thread_trampoline_branches(_SOURCE.splitlines())
    expected_fold = _fold_cond_branch_to_fallthrough(expected, [])
    expected_drop = _drop_fallthrough_uncond_branches(expected_fold)
    for backend in range(5):
        prefix = str(tmp_path / ("gc" + str(backend) + "-"))
        result = subprocess.run([str(binary), str(source), str(edges), prefix],
                                capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PATH="/nonexistent", PCC_HOST_PYTHON="/usr/bin/false", PCC_RUNTIME_CC="/usr/bin/false"))
        assert result.returncode == 0, result.stderr
        for suffix, lines in (("thread", expected), ("fold", expected_fold), ("drop", expected_drop),
                              ("thread-after-fold", expected), ("fold-after-drop", expected_fold)):
            assert Path(prefix + suffix + ".s").read_text() == "\n".join(lines) + "\n", (backend, suffix)
