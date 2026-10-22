"""Native ``os.uname()`` sequence and named-field lowering."""
from __future__ import annotations

import os
import subprocess
import textwrap


def test_native_os_uname_matches_host_and_supports_named_field(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            import os
            import sys

            def get_platform_and_machine():
                try:
                    system, _, _, _, machine = os.uname()
                except AttributeError:
                    system = sys.platform
                    machine = os.environ.get("PROCESSOR_ARCHITECTURE", "unknown")
                return system, machine

            def main() -> None:
                system, node, release, version, machine = os.uname()
                print(system)
                print(node)
                print(release)
                print(version)
                print(machine)
                print(os.uname().sysname)
                platform_system, platform_machine = get_platform_and_machine()
                print(platform_system)
                print(platform_machine)

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    expected = os.uname()
    assert run.stdout.splitlines() == [
        *expected,
        expected.sysname,
        expected.sysname,
        expected.machine,
    ]
