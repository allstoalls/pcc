"""Comprehension-local flag allocation must not depend on host hash order."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


SOURCE = '''def probe(blocks):
    index = 99
    block = 100
    result = [block for index, block in enumerate(blocks) if index >= 0]
    assert index == 99
    assert block == 100
    return result
'''

EMIT = '''
from pathlib import Path
import sys
from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file
from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
source = Path(sys.argv[1]).read_text()
module = type_infer.infer_module(parse_and_lift(source, "ordering.py", "ordering"))
codegen = L1CodeGen(module, ir_scaffold_mode="on")
codegen._strict_no_libpython = True
text = codegen.generate(module)
Path(sys.argv[2] + ".ll").write_text(text)
encode_indexed_module_file(sys.argv[2] + ".pidx", codegen._direct_indexed_module)
'''


@pytest.mark.parametrize("direct_only", (False, True))
def test_comprehension_bound_flags_are_hash_seed_independent(tmp_path, direct_only):
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "ordering.py"
    source.write_text(SOURCE)
    artifacts = []
    for seed in ("0", "1", "2", "42"):
        output = tmp_path / ("seed_" + seed)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("PCC_") and key != "LC_ALL"}
        env.update(PYTHONHASHSEED=seed, PYTHONPATH=str(root),
                   PCC_DIRECT_INDEXED_KERNEL_CAPTURE="1",
                   PCC_DIRECT_INDEXED_KERNEL_EMIT="1" if direct_only else "0")
        result = subprocess.run(
            [sys.executable, "-c", EMIT, str(source), str(output)],
            cwd=root, env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        ir = output.with_suffix(".ll").read_bytes()
        pidx = output.with_suffix(".pidx").read_bytes()
        assert pidx
        if direct_only:
            assert ir == b""
        else:
            assert b".bound.index.owned" in ir
            assert b".bound.block.owned" in ir
        artifacts.append((ir, pidx))
    assert all(artifact == artifacts[0] for artifact in artifacts[1:])
