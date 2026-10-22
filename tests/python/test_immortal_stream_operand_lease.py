"""A standard stream or the object class passed as a call operand under GC3/4.

The standard streams and the object class root are malloc'd immortals that
never enter the object graph. The foreign-address lease gave such a no-node
non-leaf owner a mode guard only on GC0-2, so on GC3/4 passing sys.stdout as
an argument raised "slot-call owned-result lease failed". pcc1 hit this in the
GC3 native canary compile at phase_timing.report(sys.stderr, ...).
"""

from __future__ import annotations

import os
import subprocess
import sys


_PROGRAM = '''
import sys


class Sink:
    def report(self, stream, complete):
        stream.write("report " + str(complete) + "\\n")
        return stream


def main() -> None:
    sink = Sink()
    out = sink.report(sys.stdout, True)
    print(out is sys.stdout)
    kind = object
    print(kind is object)
    sink.report(sys.stdout, False)


main()
'''


def test_immortal_stream_and_class_operands_lease_on_every_collector(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "stream_operand.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "stream_operand"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{gc}: {ran.stdout}"
