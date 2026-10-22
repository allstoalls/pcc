"""Native ``dict.update`` must consume owned temporary source dictionaries."""

from __future__ import annotations

import os
import subprocess
import textwrap

import pytest


_PROGRAM = textwrap.dedent(
    """
    created = 0
    finalized = 0


    class Tracked:
        def __init__(self, value: int):
            global created
            created = created + 1
            self.value = value

        def __del__(self):
            global finalized
            finalized = finalized + 1


    def churn(n: int) -> int:
        i = 0
        while i < n:
            target = {}
            target.update({"value": Tracked(i)})
            i = i + 1
        return 0


    def main() -> int:
        churn(50)
        print(str(created) + "," + str(finalized))
        return 0


    main()
    """
)


@pytest.fixture(scope="module")
def dict_update_binary(tmp_path_factory):
    from pcc.frontends.python.pipeline import compile_python

    base = tmp_path_factory.mktemp("dict_update_release")
    source = base / "dict_update_release.py"
    output = base / "dict_update_release"
    source.write_text(_PROGRAM, encoding="utf-8")
    compile_python(
        str(source),
        str(output),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    return output


@pytest.mark.parametrize("backend", ["0", "1", "2", "3", "4"])
def test_dict_update_releases_owned_source(dict_update_binary, backend):
    env = os.environ.copy()
    env["PCC_GC_BACKEND"] = backend
    result = subprocess.run(
        [str(dict_update_binary)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "50,50"
