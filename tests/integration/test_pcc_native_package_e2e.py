"""Pinned pcc-native pure-Python package E2E gate (generic-mechanism proof).

Takes a second, unrelated, pure-Python distribution (`wheel`) all the way
through the acquire/build/run package model (docs/design/pcc-package-model.md):

    pcc-native install (local wheel, no index/network) -> site
    -> pcc1 --backend self --python-libpython=off  compiles  `import wheel`
    -> the produced binary runs, prints wheel.__version__, links no libpython.

This is the "prove it is generic, not a numpy special case" rung of
`PKG-P1-NATIVE-EXTENSION-LADDER`: it shares the exact
``tests.integration.pcc_native_e2e.run_package_e2e`` skeleton the numpy gate
reuses, with only the package/driver/expected-output changed and zero
package-name branching.

Env-gated like the numpy L4/L5 gates so it never runs in the normal suite: set
``PCC_RUN_PACKAGE_E2E_INTEGRATION=1``. pcc1 via ``PCC1_BINARY`` or
``build/bootstrap/pcc1``; skips with an explicit reason when a prerequisite is
missing rather than fabricating success.
"""
from __future__ import annotations

import os
import shutil
import tarfile
from pathlib import Path

import pytest

from tests.integration.pcc_native_e2e import (
    compile_run_assert_no_libpython,
    pcc1_binary,
    run_package_e2e,
)

pytestmark = [pytest.mark.integration, pytest.mark.pcc_gate(env="PCC_RUN_PACKAGE_E2E_INTEGRATION")]

REPO = Path(__file__).resolve().parents[2]
WHEEL_FIXTURE_DIR = REPO / "tests" / "fixtures" / "packages"
WHEEL_FIXTURE = WHEEL_FIXTURE_DIR / "wheel-0.45.1-py3-none-any.whl"

NUMPY_SITE = REPO / "build" / "head-truth" / "numpy-core" / "site"
NUMPY_ROOT = REPO / "projects" / "numpy-2.4.4"
NUMPY_MESON_BUILD = NUMPY_ROOT / "build" / "pcc-package" / "meson-build"
SIMPLEJSON_SOURCE = REPO / "build" / "m1-site" / "simplejson-4.1.1"


def _require_gate() -> Path:
    if os.environ.get("PCC_RUN_PACKAGE_E2E_INTEGRATION") != "1":
        pytest.fail(
            "set PCC_RUN_PACKAGE_E2E_INTEGRATION=1 to run the pcc-native package E2E gate"
        )
    pcc1 = pcc1_binary()
    if not pcc1.is_file():
        pytest.fail(
            f"self-host pcc1 binary required: {pcc1} (set PCC1_BINARY or build via scripts/bootstrap.py --stage 1)"
        )
    if not WHEEL_FIXTURE.is_file():
        pytest.fail(f"pure-Python wheel fixture required: {WHEEL_FIXTURE}")
    return pcc1


def test_pure_python_wheel_package_e2e_through_pcc1_no_libpython(tmp_path):
    pcc1 = _require_gate()
    run_package_e2e(
        pcc1,
        tmp_path,
        package="wheel",
        find_links=[WHEEL_FIXTURE_DIR],
        driver_src="import wheel\nprint(wheel.__version__)\n",
        expected_stdout="0.45.1",
    )


def test_numpy_package_e2e_reuses_same_skeleton_no_libpython(tmp_path):
    """缝合: the numpy README example through the SAME generic run layer.

    Same ``compile_run_assert_no_libpython`` call as the pure-Python wheel gate
    — only the site (prebuilt pcc-native numpy core, from
    ``scripts/numpy_head_gate.py run --skip-loader``), driver, and expected
    output differ. No numpy-specific branching in the skeleton.
    """
    pcc1 = _require_gate()
    if not (NUMPY_SITE / "numpy" / "_core").is_dir():
        pytest.fail(
            f"pcc-native NumPy core site required: {NUMPY_SITE} "
            "(run: uv run python scripts/numpy_head_gate.py run --skip-loader)"
        )
    compile_run_assert_no_libpython(
        pcc1,
        tmp_path,
        driver_src=(
            "import numpy as np\n"
            "print(np.__version__)\n"
            "a = np.array([1, 2, 3])\n"
            "print([int(x) for x in a + 1])\n"
        ),
        expected_stdout="2.4.4\n[2, 3, 4]",
        compile_site=os.pathsep.join(
            [str(NUMPY_SITE), str(NUMPY_MESON_BUILD), str(NUMPY_ROOT)]
        ),
        label="numpy",
    )


def test_simplejson_c_extension_builds_from_cold_source_through_generic_e2e(
    tmp_path,
):
    """Real setup.py C extension: cold source -> pcc1 install -> import/run.

    The reusable M1 source checkout is copied without any prior extension,
    package manifest, or build tree.  Therefore success must come from the
    compiled pcc1 installer's generic C-extension source builder, then flow
    through the same no-libpython run layer used by wheel and numpy.
    """
    pcc1 = _require_gate()
    if not (SIMPLEJSON_SOURCE / "simplejson" / "_speedups.c").is_file():
        pytest.fail(
            f"simplejson 4.1.1 source prerequisite required: {SIMPLEJSON_SOURCE}"
        )
    source = tmp_path / "simplejson-4.1.1-source"
    shutil.copytree(
        SIMPLEJSON_SOURCE,
        source,
        ignore=shutil.ignore_patterns(
            "*.so",
            "*.dylib",
            "*.pyd",
            "pcc-package.json",
            "build",
            "__pycache__",
            "*.egg-info",
        ),
    )
    sdist = tmp_path / "simplejson-4.1.1.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        archive.add(source, arcname="simplejson-4.1.1")
    run_package_e2e(
        pcc1,
        tmp_path,
        package=str(sdist),
        find_links=(),
        driver_src=(
            "import simplejson\n"
            "import simplejson.decoder as decoder\n"
            "import simplejson.encoder as encoder\n"
            "import simplejson.scanner as scanner\n"
            "native = (scanner.c_make_scanner is not None "
            "and scanner.make_scanner is scanner.c_make_scanner "
            "and decoder.c_scanstring is not None "
            "and encoder.c_make_encoder is not None)\n"
            "payload = {'items': [1, 'two', None], 'ok': True}\n"
            "encoded = simplejson.dumps(payload, separators=(',', ':'), sort_keys=True)\n"
            "print('native', native)\n"
            "print('encoded', encoded)\n"
            "print('roundtrip', simplejson.loads(encoded) == payload)\n"
        ),
        expected_stdout=(
            "native True\n"
            'encoded {"items":[1,"two",null],"ok":true}\n'
            "roundtrip True"
        ),
    )
