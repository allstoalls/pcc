"""Compiler filesystem primitives exercised by an emitted native program."""

import pytest

from tests.integration.test_owned_platform_native import compile_run, native_compiler

pytestmark = pytest.mark.integration


def test_native_frontend_artifact_copy(native_compiler, tmp_path):
    source = tmp_path / "inputs"
    nested = source / "nested 中文"
    nested.mkdir(parents=True)
    payload = bytes(range(256)) * 4097
    (nested / "module.ast").write_bytes(payload)
    (source / "empty.ast").write_bytes(b"")
    destination = tmp_path / "persistent" / "ast"
    compile_run(native_compiler, tmp_path / "program", """
        import sys
        import os
        from pcc.frontends.python.pipeline_frontend_parallel import _copy_frontend_artifact_tree
        def run():
            _copy_frontend_artifact_tree(sys.argv[1], sys.argv[2])
            with open(os.path.join(sys.argv[2], "empty.ast"), "rb") as stream:
                assert stream.read() == b""
            print("copied")
        run()
    """, argv=[str(source), str(destination)], expected="copied\n")
    assert (destination / "nested 中文" / "module.ast").read_bytes() == payload
