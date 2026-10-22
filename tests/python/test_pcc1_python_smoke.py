"""Self-host smoke: invoke the compiled ``pcc1`` binary to compile and
then run small Python programs.

The other ``test_python_*_parity.py`` files import
``pcc.frontends.python.pipeline.compile_python`` and call it from host CPython.
That tests "CPython hosting pcc the library". This file tests the
stricter contract: **the bootstrapped ``pcc1`` binary** (a
no-libpython native executable) compiles ``.py`` programs into native
binaries, and those binaries produce the expected output.

Failure to run any of these means the pcc1 binary itself is broken or
its native subset doesn't cover the language feature exercised. That
is a regression different from anything the host-pcc parity tests
catch.

This file is skipped when no pcc1 binary is on disk; it never
triggers a heavy bootstrap rebuild.
"""

from __future__ import annotations

import os
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from pcc1_gate import find_current_pcc1

REPO = Path(__file__).absolute().parents[2]
_PCC1_CANDIDATES = [
    REPO / "build" / "bootstrap-pytest-self" / "pcc1",
    REPO / "build" / "bootstrap" / "pcc1",
    REPO / "build" / "bootstrap-self-claude" / "pcc1",
    REPO / "build" / "bootstrap-llvm-claude" / "pcc1",
    REPO / "build" / "bootstrap-strict-self" / "pcc1",
    REPO / "build" / "bootstrap-self-darwin_arm64" / "pcc1",
    REPO / "build" / "bootstrap-llvm-darwin_arm64" / "pcc1",
]


def _find_pcc1() -> Path | None:
    if os.environ.get("PCC_CURRENT_PCC1"):
        # The selection every other pcc1 consumer honors, equally strict: a
        # missing path fails the module instead of testing another binary.
        return find_current_pcc1(REPO)
    env_path = os.environ.get("PCC1_BINARY")
    if env_path:
        p = Path(env_path)
        if p.exists() and p.is_file():
            return p
    for p in _PCC1_CANDIDATES:
        if p.exists() and p.is_file():
            return p
    return None


def _pcc1_supports_pytest() -> bool:
    if PCC1 is None:
        return False
    result = subprocess.run(
        [str(PCC1), "--help"],
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    return result.returncode == 0 and "--pytest" in result.stdout


PCC1 = _find_pcc1()
pytestmark = pytest.mark.pcc_gate(probe="pcc1")


@pytest.fixture(scope="module", autouse=True)
def _smoke_pcc1_present():
    global PCC1
    if PCC1 is None:
        PCC1 = _find_pcc1()
    if PCC1 is None:
        pytest.fail(
            "no pcc1 binary found even after session auto-provisioning; "
            "run scripts/bootstrap.py --stage 1 and read its error output"
        )
    yield


@pytest.fixture(scope="module", autouse=True)
def _smoke_pcc_runtime(pcc_runtime_archive):
    """Build the pcc-Python runtime archive pcc1 links before the smoke tests.

    The build/check lives in the shared ``pcc_runtime_archive`` fixture
    (tests/python/conftest.py). Without it, a tree missing
    ``libpy_runtime_pcc_py.a`` makes every smoke test fail the final link with
    ``Undefined symbols: _py_list_append, ...`` — a build-environment artifact,
    not a pcc1 codegen regression.
    """
    previous = os.environ.get("PCC_RUNTIME_ARCHIVE")
    os.environ["PCC_RUNTIME_ARCHIVE"] = str(pcc_runtime_archive)
    try:
        yield pcc_runtime_archive
    finally:
        if previous is None:
            os.environ.pop("PCC_RUNTIME_ARCHIVE", None)
        else:
            os.environ["PCC_RUNTIME_ARCHIVE"] = previous


def _compile_and_run(
    tmp_path: Path,
    src_text: str,
    *,
    timeout: float = 30.0,
    libpython_mode: str = "off",
    compile_env: dict[str, str] | None = None,
) -> str:
    """Use the compiled pcc1 binary to compile ``src_text`` to a native
    executable, run it, and return its stdout.

    Asserts that the compile succeeded and that the run exited 0.
    """
    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(src_text).lstrip(), encoding="utf-8")

    compile_cmd = [
        str(PCC1),
        str(src),
        "-o",
        str(exe),
        f"--python-libpython={libpython_mode}",
        "--ir-scaffold=on",
    ]
    compile_proc = subprocess.run(
        compile_cmd,
        capture_output=True,
        text=True,
        timeout=120.0,
        env={**os.environ, **(compile_env or {})},
    )
    assert compile_proc.returncode == 0, (
        f"pcc1 compile failed (exit {compile_proc.returncode}):\n"
        f"cmd: {' '.join(compile_cmd)}\n"
        f"stdout:\n{compile_proc.stdout}\n"
        f"stderr:\n{compile_proc.stderr}"
    )
    assert exe.exists(), f"pcc1 produced no binary at {exe}"

    run_proc = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert run_proc.returncode == 0, (
        f"pcc1-built binary exited {run_proc.returncode}\n"
        f"stdout:\n{run_proc.stdout}\nstderr:\n{run_proc.stderr}"
    )
    return run_proc.stdout


def test_pcc1_mixed_oversized_asm_and_safe_pco_handoff(tmp_path: Path):
    package = tmp_path / "mixed_handoff"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "lib.py").write_text(
        "def add(left: int, right: int) -> int:\n"
        "    return left + right\n",
        encoding="utf-8",
    )
    (package / "__main__.py").write_text(
        "from mixed_handoff.lib import add\n"
        "print(add(20, 22))\n"
        + ("# oversized scheduling input\n" * 8000),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(tmp_path),
            "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
            "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
            "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
            "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES": "1",
            "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
            "PCC_DIRECT_INDEXED_NATIVE_OBJECT": "1",
            "PCC_PY_FRONTEND_JOBS": "auto",
            "PCC_SELF_BACKEND_JOBS": "2",
            "PCC_MACHO_LINK_JOBS": "2",
            "PCC_DISABLE_PY_RUN_CACHE": "1",
        }
    )
    profile_path = tmp_path / "mixed-profile.json"
    output_path = tmp_path / "mixed.out"
    plan_path = tmp_path / "mixed.out.pcc-link-plan"
    codegen_plan = tmp_path / "mixed.out.pcc-codegen-plan"
    environment["PCC_DEFER_SELF_LINK_PLAN"] = str(plan_path)
    environment["PCC_DEFER_FRONTEND_CODEGEN_PLAN"] = str(codegen_plan)
    environment["PCC_DEFER_FRONTEND_OUTPUT"] = str(output_path)
    deferred_helper = REPO / "scripts" / "run_pcc_deferred_link.py"

    result = subprocess.run(
        [
            sys.executable,
            str(deferred_helper),
            "--timeout",
            "120",
            "--codegen-plan",
            str(codegen_plan),
            str(plan_path),
            "--",
            str(PCC1),
            "--profile-json",
            str(profile_path),
            "--backend",
            "self",
            "--python-libpython",
            "off",
            "--ir-scaffold",
            "on",
            str(package / "__main__.py"),
            "-o",
            str(output_path),
        ],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120.0,
    )

    assert result.returncode == 0, result.stderr[-3000:]
    executed = subprocess.run(
        [str(output_path)],
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert executed.returncode == 0, executed.stderr
    assert executed.stdout.strip() == "42"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    counters = profile["counters"]
    assert counters["multi_frontend_codegen_deferred"] == 1
    assert counters["multi_frontend_codegen_oversized_assembly_handoff"] >= 1
    codegen_receipt = json.loads(
        Path(str(codegen_plan) + ".result.json").read_text(encoding="utf-8")
    )
    assert codegen_receipt["worker_count"] >= 2
    assert codegen_receipt["oversized_count"] >= 1
    internal_lines = Path(
        str(codegen_plan) + ".internal-inputs"
    ).read_text(encoding="utf-8").splitlines()[2:]
    assert any(line.startswith("ASM\t") for line in internal_lines)
    assert any(line.startswith("ASM\t") for line in internal_lines)
    assert any(line.startswith("PCO\t") for line in internal_lines)


def test_pcc1_help_lists_bootstrap_cli_options():
    result = subprocess.run(
        [str(PCC1), "--help"],
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert result.returncode == 0, result.stderr
    for option in (
        "--backend",
        "--python-libpython",
        "--python-library",
        "--ir-scaffold",
        "--emit-llvm",
        "--profile-json",
        "--pass",
        "--disable-pass",
        "--diagnostic-format",
        "--pytest",
    ):
        assert option in result.stdout
    # C/project inputs no longer delegate to a host pcc: pcc1 runs the full C
    # frontend in-process (the old "C/project inputs are delegated" wording).
    assert "C/project requests enter the full C frontend" in result.stdout


def test_pcc1_native_subprocess_preserves_child_returncode(tmp_path):
    current_pcc1 = find_current_pcc1(REPO)
    assert current_pcc1 is not None, "pcc1 provisioning produced no current binary"

    src = tmp_path / "exit7.py"
    src.write_text("import sys\nsys.exit(7)\n", encoding="utf-8")
    result = subprocess.run(
        [
            str(current_pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
        ],
        capture_output=True,
        text=True,
        timeout=180.0,
    )

    assert result.returncode == 7, (
        f"pcc1 returned {result.returncode}, expected child exit 7\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    diagnostics = result.stdout + result.stderr
    assert "AttributeError: returncode" not in diagnostics
    assert "Traceback (most recent call last)" not in diagnostics


def test_pcc1_compiles_time_monotonic_float_literal_mul(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        import time

        def main():
            print(int(time.monotonic() * 1000.0) >= 0)

        main()
        """,
    )
    assert out.strip() == "True"


def test_pcc1_hoists_value_nested_def_with_genexpr_body(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def use(option, fn):
            fn(option)


        class Item:
            def __init__(self, name):
                self.name = name


        class Failer:
            def __init__(self):
                self.protos = [Item("a"), Item("b")]
                self.bind = "x"
                self.sslclient = False

            def start(self):
                raise RuntimeError("boom")


        def main():
            def print_fn(option, bind=None):
                names = ",".join(i.name for i in option.protos)
                print("Serving on", (bind or option.bind), "by", names + ("(SSL)" if option.sslclient else ""))

            for option in [Failer()]:
                try:
                    use(option, print_fn)
                    option.start()
                except Exception as ex:
                    print_fn(option)
                    print("failed", ex)


        main()
        """,
        compile_env={"PCC_GC_BACKEND": "4"},
    )
    assert out.splitlines() == [
        "Serving on x by a,b",
        "Serving on x by a,b",
        "failed boom",
    ]


def test_pcc1_partial_legacy_starstar_kwargs_no_nameerror(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        import functools


        def target(**kwargs):
            return kwargs


        def main():
            left = {"a": 1}
            right = {"b": 2}
            functools.partial(target, **left, **right)
            print("ok")


        main()
        """,
        compile_env={"PCC_GC_BACKEND": "4"},
    )
    assert out.strip() == "ok"


def test_pcc1_partial_kwargs_calls_native_function(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        import functools


        def target(a, b, c=3, **kwargs):
            print(a, b, c, kwargs.get("x"))


        def main():
            handler = functools.partial(target, b=2, x=9)
            handler(1)


        main()
        """,
        compile_env={"PCC_GC_BACKEND": "4"},
    )
    assert out.strip() == "1 2 3 9"


def test_pcc1_partial_async_function_returns_coroutine(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        import asyncio
        import functools


        async def target(a, b=3, **kwargs):
            print(a, b, kwargs.get("x"))


        def main():
            handler = functools.partial(target, b=2, x=9)
            asyncio.run(handler(1))


        main()
        """,
        compile_env={"PCC_GC_BACKEND": "4"},
    )
    assert out.strip() == "1 2 9"


def test_pcc1_module_runner_compiles_package_without_host_python(tmp_path):
    pkg = tmp_path / "demo_mod"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__main__.py").write_text(
        textwrap.dedent("""
            import sys

            print(len(sys.argv))
            print(sys.argv[1])
            print(sys.argv[2])
            """).lstrip(),
        encoding="utf-8",
    )
    host_python = tmp_path / "host_python_probe.sh"
    host_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-m\" ]; then\n"
        "  echo host module shim invoked >&2\n"
        "  exit 91\n"
        "fi\n"
        "exec python3 \"$@\"\n",
        encoding="utf-8",
    )
    host_python.chmod(0o755)

    result = subprocess.run(
        [str(PCC1), "-m", "demo_mod", "left", "right"],
        cwd=REPO,
        env={
            **os.environ,
            "PCC_HOST_PYTHON": str(host_python),
            "PYTHONPATH": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=120.0,
    )

    assert result.returncode == 0, (
        f"pcc1 -m failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert result.stdout == "3\nleft\nright\n"


def test_pcc1_module_runner_nested_async_closure_formats_captured_strings(tmp_path):
    pkg = tmp_path / "demo_http"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "proto.py").write_text(
        textwrap.dedent("""
            import re
            import urllib.parse

            HTTP_LINE = re.compile("([^ ]+) +(.+?) +(HTTP/[^ ]+)$")

            class Writer:
                def __init__(self):
                    self.payload = b""

                def write(self, data):
                    self.payload = data


            class HTTP:
                async def http_accept(self, user, method, path, authority, ver, lines, host, pauth, reply, authtable, users, httpget=None, **kw):
                    url = urllib.parse.urlparse(path)
                    if method == "CONNECT":
                        return user, "example.com", 443, None
                    host_name, port = "example.com", 80
                    newpath = url._replace(netloc="", scheme="").geturl()

                    async def connected(writer):
                        writer.write(f"{method} {newpath} {ver}\\r\\n{lines}\\r\\n\\r\\n".encode())
                        return True

                    return user, host_name, port, connected

            async def run():
                lines = b"GET http://example.com/ HTTP/1.1\\r\\nHost: example.com\\r\\nProxy-Connection: Keep-Alive\\r\\n\\r\\n"
                headers = lines[:-4].decode().split("\\r\\n")
                method, path, ver = HTTP_LINE.match(headers.pop(0)).groups()
                lines = "\\r\\n".join(i for i in headers if not i.startswith("Proxy-"))
                headers = dict(i.split(": ", 1) for i in headers if ": " in i)
                proto = HTTP()
                user, host_name, port, connected = await proto.http_accept(
                    True, method, path, None, ver, lines, headers.get("Host", ""),
                    headers.get("Proxy-Authorization"), None, None, None,
                )
                writer = Writer()
                ok = await connected(writer)
                print(writer.payload)
                print(host_name, port)
                return ok
            """).lstrip(),
        encoding="utf-8",
    )
    (pkg / "__main__.py").write_text(
        textwrap.dedent("""
            import asyncio
            from .proto import run

            print(asyncio.run(run()))
            """).lstrip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(PCC1), "-m", "demo_http"],
        cwd=REPO,
        env={
            **os.environ,
            "PCC_GC_BACKEND": "4",
            "PYTHONPATH": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=120.0,
    )

    assert result.returncode == 0, (
        f"pcc1 -m failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert result.stdout.strip().splitlines() == [
        "b'GET / HTTP/1.1\\r\\nHost: example.com\\r\\n\\r\\n'",
        "example.com 80",
        "True",
    ]


def test_pcc1_urllib_parse_result_derived_authority_attrs(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        import urllib.parse

        def main() -> None:
            first = urllib.parse.urlparse("http://example.com/")
            second = urllib.parse.urlparse("http://user:pass@Example.COM:8080/path")
            third = urllib.parse.urlparse("s://:8081")
            print(first.username, first.password, first.hostname, first.port)
            print(second.username, second.password, second.hostname, second.port)
            print(third.username, third.password, third.hostname, third.port)

        if __name__ == "__main__":
            main()
        """,
        compile_env={"PCC_GC_BACKEND": "4"},
    )
    assert out.strip().splitlines() == [
        "None None example.com None",
        "user pass example.com 8080",
        "None None None 8081",
    ]


def test_pcc1_accepts_host_pass_cli_for_python_inputs(tmp_path):
    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent("""
            def main() -> None:
                print("pass cli")

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )

    compile_cmd = [
        str(PCC1),
        "--backend",
        "self",
        "--python-libpython=off",
        "--ir-scaffold=on",
        "--pass",
        "called-value-prop",
        "--disable-pass=called-value-prop",
        str(src),
        "-o",
        str(exe),
    ]
    compile_proc = subprocess.run(
        compile_cmd,
        capture_output=True,
        text=True,
        timeout=120.0,
    )
    assert compile_proc.returncode == 0, (
        f"pcc1 compile failed (exit {compile_proc.returncode}):\n"
        f"cmd: {' '.join(compile_cmd)}\n"
        f"stdout:\n{compile_proc.stdout}\n"
        f"stderr:\n{compile_proc.stderr}"
    )
    run_proc = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert run_proc.returncode == 0, run_proc.stderr
    assert run_proc.stdout == "pass cli\n"


def test_pcc1_python_library_emit_llvm_cli(tmp_path):
    src = tmp_path / "libmod.py"
    ll = tmp_path / "libmod.ll"
    src.write_text(
        textwrap.dedent("""
            def exported() -> int:
                return 7
            """).lstrip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(PCC1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            "--python-library",
            "--emit-llvm=" + str(ll),
            str(src),
        ],
        capture_output=True,
        text=True,
        timeout=120.0,
    )
    assert result.returncode == 0, result.stderr
    ir_text = ll.read_text(encoding="utf-8")
    assert "define i32 @main(" not in ir_text
    assert "@_pcc_py_module_top_libmod" in ir_text


def test_pcc1_delegates_c_input_to_host_pcc(tmp_path):
    src = tmp_path / "hello.c"
    ll = tmp_path / "hello.ll"
    src.write_text(
        "int main(void) { return PCC1_DELEGATED_VALUE == 7 ? 0 : 1; }\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(PCC1),
            "--cpp-arg=-DPCC1_DELEGATED_VALUE=7",
            "--emit-llvm=" + str(ll),
            str(src),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120.0,
    )
    assert (
        result.returncode == 0
    ), f"pcc1 C delegation failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert ll.exists()


def test_pcc1_delegates_c_directory_input_to_host_pcc(tmp_path):
    project = tmp_path / "c_project"
    project.mkdir()
    ll = tmp_path / "project.ll"
    (project / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")

    result = subprocess.run(
        [
            str(PCC1),
            "--no-cache",
            "--emit-llvm=" + str(ll),
            str(project),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120.0,
    )
    assert (
        result.returncode == 0
    ), f"pcc1 C project delegation failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert ll.exists()


def test_pcc1_emit_llvm_and_profile_json_cli(tmp_path):
    src = tmp_path / "prog.py"
    ll = tmp_path / "prog.ll"
    profile = tmp_path / "profile.json"
    src.write_text(
        textwrap.dedent("""
            def main() -> None:
                print("profile")

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(PCC1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            "--emit-llvm=" + str(ll),
            "--profile-json",
            str(profile),
            str(src),
        ],
        capture_output=True,
        text=True,
        timeout=120.0,
    )
    assert result.returncode == 0, result.stderr
    assert ll.exists()
    assert profile.exists()
    data = json.loads(profile.read_text(encoding="utf-8"))
    assert data["schema"] == "pcc.profile.v1"
    assert data["metadata"]["emit_llvm"] is True
    assert data["metadata"]["time_unit"] == "seconds"
    assert "phase_totals_s" in data
    assert data["total_ms"] > 0
    assert any(v > 0 for v in data["phase_totals_ms"].values())


def test_pcc1_diagnostic_format_json_on_compile_error(tmp_path):
    # Use a SYNTAX error (which pcc always rejects at the parser
    # stage) rather than the historical undefined-name probe
    # (``print(missing_name)``).  pcc since stopped statically
    # rejecting undefined references and now defers them to
    # runtime NameError — matching CPython's Python-semantic
    # behavior.  The JSON-diagnostic contract is still exercised
    # by any compile-time-rejected input; a malformed funcdef
    # gives a stable PCC-PY-COMPILE-001 + python-frontend phase.
    src = tmp_path / "bad.py"
    out = tmp_path / "bad.out"
    src.write_text(
        textwrap.dedent("""
            def main(:
                pass
            """).lstrip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(PCC1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            "--diagnostic-format=json",
            str(src),
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=120.0,
    )
    assert result.returncode != 0
    data = json.loads(result.stderr)
    assert data["schema"] == "pcc.diagnostics.v1"
    assert data["has_errors"] is True
    diag = data["diagnostics"][0]
    assert diag["code"] == "PCC-PY-COMPILE-001"
    assert diag["phase"] == "python-frontend"
    assert "ParseError" in diag["message"]
    assert not out.exists()


def test_pcc1_smoke_hello_arithmetic(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def main() -> None:
            print("hello from pcc1")
            x = 0
            i = 0
            while i < 5:
                x = x + i
                i = i + 1
            print(x)

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == ["hello from pcc1", "10"]


def test_pcc1_unsafe_i64_floor_division_uses_shared_low_and_guarded_paths(
    tmp_path,
):
    out = _compile_and_run(
        tmp_path,
        """
        def low_literal(x: int) -> int:
            return x // 3

        def guarded_variable(x: int, y: int) -> int:
            return x // y

        def main() -> None:
            print(low_literal(-10))
            print(low_literal(10))
            print(guarded_variable(-10, 3))
            print(guarded_variable(10, -3))

        if __name__ == "__main__":
            main()
        """,
        compile_env={"PCC_PYTHON_TYPED_INT_ABI": "unsafe-i64"},
    )
    assert out.splitlines() == ["-4", "3", "-4", "-4"]


def test_pcc1_accepts_bare_return_from_none_nested_function(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def outer() -> None:
            def visit(name: str) -> None:
                if name == "":
                    return
                print(name)

            visit("")
            visit("ok")

        if __name__ == "__main__":
            outer()
        """,
    )
    assert out.strip() == "ok"


def test_pcc1_formats_string_fields_inside_instance_method(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        class Namer:
            def __init__(self) -> None:
                self.index = 0

            def fresh(self, hint: str) -> str:
                self.index += 1
                return f"{hint}.{self.index}"

        def main() -> None:
            n = Namer()
            print(n.fresh("inst.ObservabilityOptions"))

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip() == "inst.ObservabilityOptions.1"


def test_pcc1_compiles_keyword_class_ctor_inside_try(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        class Options:
            def __init__(
                self,
                diagnostic_format: str = "text",
                profile_json=None,
                explain_fallback: bool = False,
                phase: str = "compile",
                entry: str = "pcc",
            ) -> None:
                self.diagnostic_format = diagnostic_format
                self.profile_json = profile_json
                self.explain_fallback = explain_fallback
                self.phase = phase
                self.entry = entry

        def main() -> None:
            try:
                options = Options(
                    diagnostic_format="json",
                    profile_json=None,
                    explain_fallback=False,
                    phase="python-frontend",
                    entry="cli_bootstrap",
                )
            except ValueError as exc:
                print(str(exc))
                return
            print(options.phase)
            print(options.entry)

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == ["python-frontend", "cli_bootstrap"]


def test_pcc1_pcc_python_runtime_keeps_none_ctor_arg_visible_to_generic_attr(
    tmp_path,
):
    out = _compile_and_run(
        tmp_path,
        """
        class Options:
            def __init__(self, profile_json=None, phase: str = "compile") -> None:
                self.profile_json = profile_json
                self.phase = phase

        def parse():
            path = "x"
            profile_json = None
            explain = False
            return ((path, profile_json, explain), 0, None)

        def check(options) -> None:
            if options.profile_json:
                print("profile")
            else:
                print("none")
            print(options.phase)

        def main() -> None:
            parsed, exit_code, error = parse()
            (path, profile_json, explain) = parsed
            profile_json = None if profile_json is None else (profile_json or "") + ""
            options = Options(profile_json=profile_json, phase="python-frontend")
            check(options)

        if __name__ == "__main__":
            main()
        """,
        compile_env={"PCC_RUNTIME_CC": "pcc", "PCC_RUNTIME_HIGH": "py"},
    )
    assert out.strip().splitlines() == ["none", "python-frontend"]


def test_pcc1_pcc_python_runtime_keeps_local_attr_constructor_args(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        class Token:
            def __init__(self, text: str, line: int) -> None:
                self.text = text
                self.line = line

        class Name:
            def __init__(self, ident: str, line: int) -> None:
                self.ident = ident
                self.line = line

        class Parser:
            def _peek(self) -> Token:
                return Token("int", 1)

            def make(self) -> Name:
                t = self._peek()
                return Name(ident=t.text, line=t.line)

        def main() -> None:
            n = Parser().make()
            print(n.ident)
            print(n.line)

        if __name__ == "__main__":
            main()
        """,
        compile_env={"PCC_RUNTIME_CC": "pcc", "PCC_RUNTIME_HIGH": "py"},
    )
    assert out.strip().splitlines() == ["int", "1"]


def test_pcc1_pcc_python_runtime_keeps_py_lex_token_constructor_args(tmp_path):
    ll_out = tmp_path / "py_lex.ll"
    compile_cmd = [
        str(PCC1),
        "--emit-llvm",
        "--python-libpython=off",
        "--ir-scaffold=on",
        str(REPO / "pcc" / "frontends" / "python" / "py_lex.py"),
        "-o",
        str(ll_out),
    ]
    compile_proc = subprocess.run(
        compile_cmd,
        capture_output=True,
        text=True,
        timeout=120.0,
        env={
            **os.environ,
            "PCC_RUNTIME_CC": "pcc",
            "PCC_RUNTIME_HIGH": "py",
        },
    )
    assert compile_proc.returncode == 0, (
        f"pcc1 py_lex compile failed:\nstdout:\n{compile_proc.stdout}\n"
        f"stderr:\n{compile_proc.stderr}"
    )
    ir_text = ll_out.read_text(encoding="utf-8")
    for line in ir_text.splitlines():
        if "Token___init__" in line:
            assert "ptr null" not in line
            assert "i64 null" not in line


def test_pcc1_generator_expression_fstring_join(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        class Arg:
            def __init__(self, type: str, text: str) -> None:
                self.type = type
                self.text = text

            def __str__(self) -> str:
                return self.text

        def main() -> None:
            args = [Arg("i64", "%a"), Arg("ptr", "%b")]
            print(", ".join(f"{a.type} {a}" for a in args))

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip() == "i64 %a, ptr %b"


def test_pcc1_getattr_default_present_and_missing(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        class Info:
            def __init__(self) -> None:
                self.init_fn = "ready"

        def main() -> None:
            info = Info()
            missing = getattr(info, "missing", None)
            if missing is None:
                print("missing")
            else:
                print("bad")
            print(getattr(info, "init_fn", None))

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == ["missing", "ready"]


def test_pcc1_accepts_optional_str_return_none(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        from typing import Optional

        def maybe(flag: bool) -> Optional[str]:
            if flag:
                return "value"
            return None

        def main() -> None:
            value = maybe(False)
            if value is None:
                print("none")
            else:
                print(value)

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip() == "none"


def test_pcc1_short_circuit_keeps_operand_values(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def classify(args, kwargs) -> None:
            if args or kwargs:
                print("nonempty")
            else:
                print("empty")

        def main() -> None:
            classify([], {})
            classify(["x"], {})
            classify([], {"k": "v"})
            print("" or "right")
            print("left" and "right")

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == [
        "empty",
        "nonempty",
        "nonempty",
        "right",
        "right",
    ]


def test_pcc1_string_concat_and_false_bool_ctor_arg(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        class Flag:
            def __init__(self, enabled: bool = False) -> None:
                self.enabled = enabled

        def main() -> None:
            disabled = Flag(False)
            enabled = Flag(True)
            print("flag:" + ("on" if enabled.enabled else "off"))
            print("flag:" + ("on" if disabled.enabled else "off"))

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == ["flag:on", "flag:off"]


def test_pcc1_calls_positional_constructor_before_method_use(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        class Parser:
            def __init__(self, src: str, filename: str = "<input>") -> None:
                self.src = src
                self.filename = filename
                self.pos = 0

            def peek(self, off: int = 0) -> int:
                return self.pos + off

        def parse(src: str, filename: str) -> int:
            parser = Parser(src, filename)
            return parser.peek(2)

        def main() -> None:
            print(parse("x", "y"))

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip() == "2"


def test_pcc1_can_launch_pytest_for_test_directory(tmp_path):
    if not _pcc1_supports_pytest():
        pytest.fail("pcc1 binary predates --pytest launcher support; auto-provisioning should have rebuilt it")

    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_sample.py").write_text(
        "from pcc.test_runner import fixture\n\n"
        "@fixture\n"
        "def value() -> int:\n"
        "    return 42\n\n"
        "def test_sample(value: int) -> None:\n"
        "    assert value == 42\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(PCC1), "--pytest", str(test_dir), "-q", "-n0"],
        capture_output=True,
        text=True,
        timeout=60.0,
    )
    assert (
        result.returncode == 0
    ), f"pcc1 --pytest failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "pcc1 pytest file(s) passed" in result.stdout


@pytest.mark.integration
def test_pcc1_pytest_marker_selection_matches_repo_gates(tmp_path):
    if not _pcc1_supports_pytest():
        pytest.fail("pcc1 binary predates --pytest launcher support; auto-provisioning should have rebuilt it")

    default_dir = tmp_path / "default_tests"
    default_dir.mkdir()
    (default_dir / "test_sample.py").write_text(
        "from pcc.test_runner import fixture\n"
        "import pytest\n\n"
        "@fixture\n"
        "def value() -> int:\n"
        "    return 5\n\n"
        "def test_unit(value: int) -> None:\n"
        "    assert value == 5\n\n"
        "@pytest.mark.integration\n"
        "def test_integration_is_excluded_by_default(value: int) -> None:\n"
        "    assert value == 99\n",
        encoding="utf-8",
    )

    integration_dir = tmp_path / "integration_tests"
    integration_dir.mkdir()
    (integration_dir / "test_sample.py").write_text(
        "from pcc.test_runner import fixture\n"
        "import pytest\n\n"
        "@fixture\n"
        "def value() -> int:\n"
        "    return 7\n\n"
        "def test_unit_is_excluded_by_integration_marker(value: int) -> None:\n"
        "    assert value == 99\n\n"
        "@pytest.mark.integration\n"
        "def test_integration(value: int) -> None:\n"
        "    assert value == 7\n",
        encoding="utf-8",
    )

    default_result = subprocess.run(
        [str(PCC1), "--pytest", str(default_dir), "-q", "-n0"],
        capture_output=True,
        text=True,
        timeout=90.0,
    )
    assert default_result.returncode == 0, (
        "default pcc1 pytest marker selection failed\n"
        f"stdout:\n{default_result.stdout}\nstderr:\n{default_result.stderr}"
    )

    integration_result = subprocess.run(
        [str(PCC1), "--pytest", "-m", "integration", str(integration_dir), "-q", "-n0"],
        capture_output=True,
        text=True,
        timeout=90.0,
    )
    assert integration_result.returncode == 0, (
        "integration pcc1 pytest marker selection failed\n"
        f"stdout:\n{integration_result.stdout}\nstderr:\n{integration_result.stderr}"
    )

    module_mark_dir = tmp_path / "module_mark_integration_tests"
    module_mark_dir.mkdir()
    (module_mark_dir / "test_sample.py").write_text(
        "from pcc.test_runner import fixture\n"
        "import pytest\n"
        "pytestmark = pytest.mark.integration\n\n"
        "@fixture\n"
        "def value() -> int:\n"
        "    return 11\n\n"
        "def test_module_marked_integration(value: int) -> None:\n"
        "    assert value == 11\n",
        encoding="utf-8",
    )
    module_mark_result = subprocess.run(
        [str(PCC1), "--pytest", "-m", "integration", str(module_mark_dir), "-q", "-n0"],
        capture_output=True,
        text=True,
        timeout=90.0,
    )
    assert module_mark_result.returncode == 0, (
        "module-level integration pcc1 pytest marker selection failed\n"
        f"stdout:\n{module_mark_result.stdout}\nstderr:\n{module_mark_result.stderr}"
    )


def test_pcc1_pytest_literal_skipif_matches_pytest_subset(tmp_path):
    if not _pcc1_supports_pytest():
        pytest.fail("pcc1 binary predates --pytest launcher support; auto-provisioning should have rebuilt it")

    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_sample.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.skipif(True, reason='skip')\n"
        "def test_skipped_failure() -> None:\n"
        "    assert 1 == 2\n\n"
        "@pytest.mark.skipif(False, reason='run')\n"
        "def test_kept() -> None:\n"
        "    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(PCC1), "--pytest", str(test_dir), "-q", "-n0"],
        capture_output=True,
        text=True,
        timeout=90.0,
    )
    assert result.returncode == 0, (
        "literal skipif pcc1 pytest subset failed\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_pcc1_compiles_and_runs_pytest_style_assertions(tmp_path):
    """pcc1 compiles a pytest-style ``.py`` (def test_*() + ``assert``)
    into a native binary; running the binary actually executes the
    asserts inside pcc-native code. Unlike the ``--pytest`` launcher
    test above which forks host CPython's pytest, this test exercises
    pcc1's own compile-and-run path on assertion logic.

    Failure modes this catches:
    - assert lowering broken
    - method calls on str/list inside test functions broken
    - exception path on assertion failure unreachable
    - test discovery loop (calling each ``test_*`` from main) regressed
    """
    src_text = """
        # pytest-style test file. No pytest framework needed: a tiny
        # main() runner calls each ``test_*`` and aborts on the first
        # AssertionError. This is what pytest reduces to once you
        # strip discovery + reporting.

        def test_int_arithmetic() -> None:
            assert 1 + 1 == 2
            assert 7 - 3 == 4
            assert 6 * 7 == 42
            assert 10 // 3 == 3
            assert 10 % 3 == 1

        def test_string_methods() -> None:
            assert "hello".upper() == "HELLO"
            assert "WORLD".lower() == "world"
            assert "  spaced  ".strip() == "spaced"
            assert "a,b,c".split(",") == ["a", "b", "c"]
            assert "-".join(["x", "y", "z"]) == "x-y-z"

        def test_list_and_dict() -> None:
            xs = [1, 2, 3]
            xs.append(4)
            assert len(xs) == 4
            assert xs[-1] == 4
            assert sum(xs) == 10

            d = {"a": 1, "b": 2}
            d["c"] = 3
            assert len(d) == 3
            assert d["b"] == 2

        def test_assertion_failure_actually_raises() -> None:
            # Confirm the assert path triggers AssertionError when the
            # condition is False. Without this, the ``all passed`` line
            # below would lie if asserts silently no-op'd.
            raised = False
            try:
                assert 1 == 2
            except AssertionError:
                raised = True
            assert raised

        def main() -> None:
            test_int_arithmetic()
            test_string_methods()
            test_list_and_dict()
            test_assertion_failure_actually_raises()
            print("all passed")

        if __name__ == "__main__":
            main()
    """
    out = _compile_and_run(tmp_path, src_text)
    assert out.strip() == "all passed", (
        f"pcc1-compiled pytest-style file produced unexpected output: " f"{out!r}"
    )


def test_pcc1_smoke_class_and_method(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        class P:
            def __init__(self, x: int, y: int) -> None:
                self.x = x
                self.y = y

            def magnitude_squared(self) -> int:
                return self.x * self.x + self.y * self.y

        def main() -> None:
            p = P(3, 4)
            print(p.magnitude_squared())

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip() == "25"


def test_pcc1_smoke_generator(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def fibs(n: int):
            a = 0
            b = 1
            i = 0
            while i < n:
                yield a
                t = a + b
                a = b
                b = t
                i = i + 1

        def main() -> None:
            for v in fibs(7):
                print(v)

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == ["0", "1", "1", "2", "3", "5", "8"]


def test_pcc1_smoke_exception_handling(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def main() -> None:
            try:
                raise ValueError("boom")
            except ValueError as e:
                print(str(e))
            print("after")

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == ["boom", "after"]


def test_pcc1_smoke_list_comprehension(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def main() -> None:
            sq = [x * x for x in range(5)]
            print(sq[0], sq[1], sq[2], sq[3], sq[4])

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip() == "0 1 4 9 16"


def test_pcc1_smoke_walrus_expression(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def main() -> None:
            values = [1, 2, 3]
            if (n := len(values)) > 2:
                print(n)
            print(n + 4)

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == ["3", "7"]


def test_pcc1_smoke_integer_bitwise_and_shifts(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def main() -> None:
            c2 = -61 & 255
            c3 = -87 & 255
            c4 = -16 & 255
            c5 = -97 & 255
            c6 = -104 & 255
            c7 = -128 & 255
            print((c2 & 224) == 192)
            print((c3 & 192) == 128)
            print(((c2 & 31) << 6) | (c3 & 63))
            print(
                ((c4 & 7) << 18)
                | ((c5 & 63) << 12)
                | ((c6 & 63) << 6)
                | (c7 & 63)
            )

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == ["True", "True", "233", "128512"]


def test_pcc1_bigint_mask_to_bytes_preserves_quad_value(tmp_path):
    """The compiler-owned assembler's scalar-data expression stays exact.

    Disable direct ``.pco`` publication only to isolate the expression from
    the assembler path that consumes it.  The ordinary function-bearing pcc1
    gate exercises direct publication end to end.
    """
    out = _compile_and_run(
        tmp_path,
        """
        def parse_int(token: str) -> int | None:
            text = token.strip()
            try:
                return int(text, 0)
            except ValueError:
                return None


        def opaque(value):
            return value


        def main() -> None:
            value = parse_int("2891786578161389964")
            width = opaque(8)
            if value is not None:
                encoded = (value & ((1 << (width * 8)) - 1)).to_bytes(
                    width, "little"
                )
                print(int.from_bytes(encoded, "little"))

        if __name__ == "__main__":
            main()
        """,
        compile_env={"PCC_DIRECT_INDEXED_NATIVE_OBJECT": "0"},
    )
    assert out.strip() == "2891786578161389964"


def test_pcc1_smoke_fstring_ascii_conversion(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def main() -> None:
            value = "\\u00e9"
            face = chr(0x1f600)
            print(f"{value!a}")
            print(f"{face!a}")

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == ["'\\xe9'", "'\\U0001f600'"]


def test_pcc1_smoke_async_await_asyncio_run(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        import asyncio

        async def slow(n: int) -> int:
            await asyncio.sleep(0)
            return n * 2

        async def main_async() -> None:
            a = await slow(5)
            b = await slow(7)
            print(a + b)

        def main() -> None:
            asyncio.run(main_async())

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip() == "24"


def test_pcc1_smoke_str_methods(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def main() -> None:
            s = "hello world"
            print(s.upper())
            print(s.split()[1])
            print(len(s))
            print("abc" * 3)

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == ["HELLO WORLD", "world", "11", "abcabcabc"]


def test_pcc1_smoke_print_many_dynamic_args_keep_slots(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def main() -> None:
            xs = [10, 20, 30]
            print(xs[0], xs[1], xs[2])

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip() == "10 20 30"


def test_pcc1_smoke_list_dict_set(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def main() -> None:
            xs = [1, 2, 3]
            xs.append(4)
            print(len(xs), xs[0], xs[3])

            d: dict = {"a": 1, "b": 2}
            d["c"] = 3
            print(d["b"], len(d))

            s: set = {1, 2, 3}
            s.add(4)
            s.add(2)
            print(len(s))

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == ["4 1 4", "2 3", "4"]


def test_pcc1_unannotated_set_binding_keeps_union_type(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        base = {"left"}
        combined = base | set(["right"])
        print(sorted(combined))
        """,
    )
    assert out.strip() == "['left', 'right']"


def test_pcc1_module_set_operators_survive_and_do_not_misfire(tmp_path):
    # Set bindings use the first-class SetType projection. A self-hosted pcc
    # used to encode them as DynType(name="set"), lose that discriminator,
    # and lower these as integer bitwise ops. All four set operators must
    # survive, and a same-shaped numeric expression must stay numeric.
    out = _compile_and_run(
        tmp_path,
        """
        a = {1, 2, 3}
        b = {2, 3, 4}
        print(sorted(a | b))
        print(sorted(a & b))
        print(sorted(a - b))
        print(sorted(a ^ b))
        n = 6
        print(n - 2)
        print(n | 1)
        """,
    )
    assert out.strip().splitlines() == [
        "[1, 2, 3, 4]",
        "[2, 3]",
        "[1]",
        "[1, 4]",
        "4",
        "7",
    ]


def test_pcc1_cross_module_method_reads_provider_data_constant(tmp_path):
    """Classify the module-data-constant self-host family with a real pcc1.

    The provider owns both the tuple and the method that reads it; the entry
    module imports the class through the compiled package path.  This keeps the
    constant as data (not an inlined literal or reconstructing helper).
    """
    provider = tmp_path / "constant_provider.py"
    provider.write_text(
        textwrap.dedent(
            """
            VALUES = ("left", "right")

            class ConstantReader:
                def contains(self, value: str) -> bool:
                    return value in VALUES
            """
        ),
        encoding="utf-8",
    )
    out = _compile_and_run(
        tmp_path,
        """
        from constant_provider import ConstantReader

        reader = ConstantReader()
        print(reader.contains("left"))
        print(reader.contains("missing"))
        """,
        compile_env={"PCC_PACKAGE_SITE": str(tmp_path)},
    )

    assert out.strip().splitlines() == ["True", "False"]


def test_pcc1_dynamic_list_pop_index_does_not_dispatch_as_dict(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def pop_first(obj):
            return obj.pop(0)

        def main() -> None:
            xs = ["a", "b"]
            print(pop_first(xs))
            print(xs)

        if __name__ == "__main__":
            main()
        """,
        compile_env={"PCC_GC_BACKEND": "4"},
    )
    assert out.strip().splitlines() == ["a", "['b']"]


def test_pcc1_if_return_then_tuple_unpack_keeps_codegen_function(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def choose(flag):
            if flag:
                return "early"
            left, right = "late", 7
            return left + ":" + str(right)

        def main() -> None:
            print(choose(False))
            print(choose(True))

        if __name__ == "__main__":
            main()
        """,
        compile_env={"PCC_GC_BACKEND": "4"},
    )
    assert out.strip().splitlines() == ["late:7", "early"]


def test_pcc1_class_attr_initializer_restores_codegen_env(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        class Config:
            port = 80

        def main() -> None:
            print(Config.port)

        if __name__ == "__main__":
            main()
        """,
        compile_env={"PCC_GC_BACKEND": "0"},
    )
    assert out.strip() == "80"


def test_pcc1_filter_lambda_method_next_default(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        class Route:
            def __init__(self):
                self.alive = True

            def match_rule(self, host, port):
                return host == "example.com" and port == 80

        def schedule(rserver, host_name, port):
            filter_cond = lambda o: o.alive and o.match_rule(host_name, port)
            return next(filter(filter_cond, rserver), None)

        def main() -> None:
            r = Route()
            chosen = schedule([r], "example.com", 80)
            print(chosen is r)

        if __name__ == "__main__":
            main()
        """,
        compile_env={"PCC_GC_BACKEND": "4"},
    )
    assert out.strip() == "True"


def test_pcc1_nested_async_closure_formats_captured_strings(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        import asyncio
        import re
        import urllib.parse

        class Writer:
            def __init__(self):
                self.payload = b""

            def write(self, data):
                self.payload = data


        class HTTP:
            async def http_accept(self, user, method, path, authority, ver, lines, host, pauth, reply, authtable, users, httpget=None, **kw):
                url = urllib.parse.urlparse(path)
                if method == "CONNECT":
                    return user, "example.com", 443, None
                host_name, port = "example.com", 80
                newpath = url._replace(netloc="", scheme="").geturl()

                async def connected(writer):
                    writer.write(f"{method} {newpath} {ver}\\r\\n{lines}\\r\\n\\r\\n".encode())
                    return True

                return user, host_name, port, connected

        async def outer():
            http_line = re.compile("([^ ]+) +(.+?) +(HTTP/[^ ]+)$")
            lines = b"GET http://example.com/ HTTP/1.1\\r\\nHost: example.com\\r\\nProxy-Connection: Keep-Alive\\r\\n\\r\\n"
            headers = lines[:-4].decode().split("\\r\\n")
            method, path, ver = http_line.match(headers.pop(0)).groups()
            lines = "\\r\\n".join(i for i in headers if not i.startswith("Proxy-"))
            headers = dict(i.split(": ", 1) for i in headers if ": " in i)
            proto = HTTP()
            user, host_name, port, connected = await proto.http_accept(
                True, method, path, None, ver, lines, headers.get("Host", ""),
                headers.get("Proxy-Authorization"), None, None, None,
            )
            writer = Writer()
            ok = await connected(writer)
            print(writer.payload)
            print(host_name, port)
            return ok

        def main() -> None:
            print(asyncio.run(outer()))

        if __name__ == "__main__":
            main()
        """,
        compile_env={"PCC_GC_BACKEND": "4"},
    )
    assert out.strip().splitlines() == [
        "b'GET / HTTP/1.1\\r\\nHost: example.com\\r\\n\\r\\n'",
        "example.com 80",
        "True",
    ]


def test_pcc1_nested_async_closure_forwarded_through_method_parameter_keeps_writer(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        import asyncio

        class Writer:
            def __init__(self):
                self.payload = b""

            def write(self, data):
                self.payload = data


        class HTTP:
            async def accept(self, writer):
                async def reply(code, message, body=None, wait=False):
                    print(type(writer).__name__)
                    writer.write(message)
                    return True

                return await self.http_accept(reply)

            async def http_accept(self, reply):
                return lambda writer: reply(200, b"HTTP/1.1 200 Connection established\\r\\n\\r\\n")


        async def outer():
            client = Writer()
            connected = await HTTP().accept(client)
            ok = await connected(None)
            print(client.payload)
            return ok

        def main() -> None:
            print(asyncio.run(outer()))

        if __name__ == "__main__":
            main()
        """,
        compile_env={"PCC_GC_BACKEND": "4"},
    )
    assert out.strip().splitlines() == [
        "Writer",
        "b'HTTP/1.1 200 Connection established\\r\\n\\r\\n'",
        "True",
    ]


def test_pcc1_smoke_starred_rhs_tuple_display_assignment(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        def make_pair():
            return (10, 20)

        def main() -> None:
            first, second, third = [], *make_pair()
            print(first)
            print(second)
            print(third)
            print((*[1, 2], 3))
            print([0, *[1, 2], 3])

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == [
        "[]",
        "10",
        "20",
        "(1, 2, 3)",
        "[0, 1, 2, 3]",
    ]


def test_pcc1_smoke_inheritance_super(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        class Animal:
            def __init__(self, name: str) -> None:
                self.name = name

            def greeting(self) -> str:
                return "I am " + self.name

        class Dog(Animal):
            def __init__(self, name: str, breed: str) -> None:
                super().__init__(name)
                self.breed = breed

            def greeting(self) -> str:
                return super().greeting() + ", a " + self.breed

        def main() -> None:
            d = Dog("Rex", "lab")
            print(d.greeting())

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip() == "I am Rex, a lab"


def test_pcc1_smoke_json_loads(tmp_path):
    out = _compile_and_run(
        tmp_path,
        """
        import json

        def main() -> None:
            d = json.loads("{\\\"a\\\": 1, \\\"b\\\": 2}")
            print(d["a"], d["b"])
            text = "line1" + chr(10) + "line2"
            slash = "a" + chr(92) + "b"
            quote = 'a"b'
            decoded = json.loads(json.dumps({
                "text": text,
                "slash": slash,
                "quote": quote,
            }))
            print(decoded["text"] == text)
            print(decoded["slash"] == slash)
            print(decoded["quote"] == quote)

        if __name__ == "__main__":
            main()
        """,
    )
    assert out.strip().splitlines() == ["1 2", "True", "True", "True"]


def test_pcc1_smoke_threading_basic_lock(tmp_path):
    """4 threads contended on one Lock; final counter must be 4000.
    This validates the runtime threading + lock mutual-exclusion path
    in a binary built by pcc1 itself.
    """
    out = _compile_and_run(
        tmp_path,
        """
        from threading import Lock, Thread

        counts = [0]
        lock = Lock()

        def worker() -> None:
            i = 0
            while i < 1000:
                lock.acquire()
                counts[0] = counts[0] + 1
                lock.release()
                i = i + 1

        def main() -> None:
            t0 = Thread(target=worker)
            t1 = Thread(target=worker)
            t2 = Thread(target=worker)
            t3 = Thread(target=worker)
            t0.start(); t1.start(); t2.start(); t3.start()
            t0.join(); t1.join(); t2.join(); t3.join()
            print(counts[0])

        if __name__ == "__main__":
            main()
        """,
    )
    # NOTE: the pcc1 binary on this machine was built before the
    # 2026-05-08 list-indexed-Lock fix; the contended counter still
    # serializes correctly in this case because we use a single
    # global ``lock`` (not list-indexed).
    assert out.strip() == "4000"
