import os
import shutil
import subprocess
import sys

import pytest

from pcc.driver.cli_core import cli_main, parse_cli_args
from pcc.backend.gpu_dispatch import resolve_gpu_backend
from tests.cli_support import run_cli


def test_help_shows_jobs_default_8(capfd):
    assert cli_main(["--help"]) == 0
    assert "--jobs N" in capfd.readouterr().out
    parsed, status, error = parse_cli_args(["main.c"])
    assert status == 0, error
    assert parsed[6] == 8


def test_python_m_help_does_not_import_click():
    repo_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
    code = (
        "import builtins, runpy, sys\n"
        "orig_import = builtins.__import__\n"
        "def blocked(name, globals=None, locals=None, fromlist=(), level=0):\n"
        "    if name == 'click' or name.startswith('click.'):\n"
        "        raise ImportError('click blocked for __main__ path')\n"
        "    return orig_import(name, globals, locals, fromlist, level)\n"
        "builtins.__import__ = blocked\n"
        "sys.argv = ['pcc', '--help']\n"
        "runpy.run_module('pcc', run_name='__main__')\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "Usage: pcc [OPTIONS] PATH" in result.stdout
    assert "click blocked" not in result.stderr


def test_installed_cli_launcher_help_does_not_import_click():
    repo_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
    code = (
        "import builtins, importlib\n"
        "orig_import = builtins.__import__\n"
        "def blocked(name, globals=None, locals=None, fromlist=(), level=0):\n"
        "    if name == 'click' or name.startswith('click.'):\n"
        "        raise ImportError('click blocked for CLI launcher')\n"
        "    return orig_import(name, globals, locals, fromlist, level)\n"
        "builtins.__import__ = blocked\n"
        "mod = importlib.import_module('pcc.driver.cli_launcher')\n"
        "print(callable(mod.main))\n"
        "print(mod.main(['--help']))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("True\nUsage: pcc [OPTIONS] PATH")
    assert result.stdout.rstrip().endswith("0")
    assert "click blocked" not in result.stderr


def test_plain_cli_supports_python_path_before_output_flag(tmp_path):
    script_path = tmp_path / "main.py"
    exe_path = tmp_path / "main_bin"
    script_path.write_text(
        "def main() -> None:\n"
        "    print(123)\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    , encoding="utf-8")

    result = cli_main([str(script_path), "-o", str(exe_path)])

    assert result == 0
    assert exe_path.is_file()

    run = subprocess.run(
        [str(exe_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "123\n"


def test_plain_cli_python_libpython_off_supports_native_subset(tmp_path):
    script_path = tmp_path / "main.py"
    exe_path = tmp_path / "main_bin"
    script_path.write_text(
        "def main() -> None:\n"
        "    print(123)\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    , encoding="utf-8")

    result = cli_main(
        ["--python-libpython=off", str(script_path), "-o", str(exe_path)]
    )

    assert result == 0
    assert exe_path.is_file()

    run = subprocess.run(
        [str(exe_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "123\n"


def test_plain_cli_python_libpython_default_is_off(tmp_path):
    repo_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
    script_path = tmp_path / "needs_fallback.py"
    exe_path = tmp_path / "needs_fallback_bin"
    script_path.write_text(
        "import tempfile\n"
        "print(tempfile.gettempdir())\n"
    , encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcc",
            str(script_path),
            "-o",
            str(exe_path),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 1
    assert "requires libpython fallback" in result.stderr
    assert "--python-libpython=auto/on" in result.stderr
    assert not exe_path.exists()


def test_python_libpython_resolver_defaults_to_off():
    from pcc.frontends.python.pipeline import _resolve_libpython_mode

    saved = os.environ.get("PCC_PYTHON_LIBPYTHON")
    try:
        os.environ.pop("PCC_PYTHON_LIBPYTHON", None)
        assert _resolve_libpython_mode(None) == "off"
        assert _resolve_libpython_mode("") == "off"
        assert _resolve_libpython_mode("auto") == "auto"
        os.environ["PCC_PYTHON_LIBPYTHON"] = "auto"
        assert _resolve_libpython_mode(None) == "auto"
    finally:
        if saved is None:
            os.environ.pop("PCC_PYTHON_LIBPYTHON", None)
        else:
            os.environ["PCC_PYTHON_LIBPYTHON"] = saved


def test_python_libpython_off_reports_friendly_error_for_fallback_script(tmp_path):
    repo_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
    script_path = tmp_path / "needs_fallback.py"
    exe_path = tmp_path / "needs_fallback_bin"
    script_path.write_text(
        "import tempfile\n"
        "print(tempfile.gettempdir())\n"
    , encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcc",
            "--python-libpython=off",
            str(script_path),
            "-o",
            str(exe_path),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 1
    assert "requires libpython fallback" in result.stderr
    assert "--python-libpython=auto/on" in result.stderr
    assert not exe_path.exists()


def test_jobs_requires_separate_tus(tmp_path):
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")

    result = run_cli(["--jobs", "2", str(tmp_path)])

    assert result.returncode == 1
    assert (
        "Error: --jobs requires --separate-tus, --depends-on, or --system-link"
        in (result.stdout + result.stderr)
    )


def test_depends_on_supports_file_with_dependency_make_goal(tmp_path):
    dep_dir = tmp_path / "lib"
    dep_dir.mkdir()
    (dep_dir / "helper.c").write_text("int helper(void) { return 41; }\n", encoding="utf-8")
    (dep_dir / "ignored.c").write_text("int ignored(void) { return 99; }\n", encoding="utf-8")
    (dep_dir / "Makefile").write_text(
        "lib: helper.o\n"
        "\tcc -o lib helper.o\n\n"
        "helper.o: helper.c\n"
        "\tcc -c -o helper.o helper.c\n\n"
        "ignored.o: ignored.c\n"
        "\tcc -c -o ignored.o ignored.c\n"
    , encoding="utf-8")
    main_path = tmp_path / "main.c"
    main_path.write_text(
        "int helper(void);\n"
        "int main(void) { return helper() == 41 ? 0 : 1; }\n"
    , encoding="utf-8")

    result = run_cli(
        ["--depends-on", f"{dep_dir}=lib", str(main_path)],
    )

    assert result.returncode == 0


def test_jobs_allowed_with_depends_on(tmp_path):
    dep_dir = tmp_path / "lib"
    dep_dir.mkdir()
    (dep_dir / "helper.c").write_text("int helper(void) { return 41; }\n", encoding="utf-8")
    (dep_dir / "Makefile").write_text(
        "lib: helper.o\n"
        "\tcc -o lib helper.o\n\n"
        "helper.o: helper.c\n"
        "\tcc -c -o helper.o helper.c\n"
    , encoding="utf-8")
    main_path = tmp_path / "main.c"
    main_path.write_text(
        "int helper(void);\n"
        "int main(void) { return helper() == 41 ? 0 : 1; }\n"
    , encoding="utf-8")

    result = run_cli(
        ["--jobs", "2", "--depends-on", f"{dep_dir}=lib", str(main_path)],
    )

    assert result.returncode == 0


def test_system_link_supports_depends_on_multi_input(tmp_path):
    helper_path = tmp_path / "helper.c"
    helper_path.write_text("int helper(void) { return 41; }\n", encoding="utf-8")
    main_path = tmp_path / "main.c"
    main_path.write_text(
        "int helper(void);\n"
        "int main(void) { return helper() == 41 ? 0 : 1; }\n"
    , encoding="utf-8")

    result = run_cli(
        ["--system-link", "--depends-on", str(helper_path), str(main_path)],
    )

    assert result.returncode == 0


def test_system_link_supports_link_arg_archive(tmp_path):
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    ar = shutil.which("ar")
    assert cc is not None
    assert ar is not None

    helper_c = tmp_path / "helper.c"
    helper_o = tmp_path / "helper.o"
    helper_a = tmp_path / "libhelper.a"
    main_path = tmp_path / "main.c"

    helper_c.write_text("int helper(void) { return 41; }\n", encoding="utf-8")
    main_path.write_text(
        "int helper(void);\n"
        "int main(void) { return helper() == 41 ? 0 : 1; }\n"
    , encoding="utf-8")

    subprocess.run(
        [cc, "-c", "-o", str(helper_o), str(helper_c)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [ar, "rcs", str(helper_a), str(helper_o)],
        check=True,
        capture_output=True,
        text=True,
    )

    result = run_cli(
        ["--system-link", f"--link-arg={helper_a}", str(main_path)],
    )

    assert result.returncode == 0


def test_prepare_cmd_and_ensure_make_goal_support_fresh_dependency_project(tmp_path):
    dep_dir = tmp_path / "lib"
    dep_dir.mkdir()
    configure_sh = dep_dir / "configure.sh"
    helper_c = dep_dir / "helper.c"
    main_path = tmp_path / "main.c"

    configure_sh.write_text(
        "#!/bin/sh\n"
        "cat > config.h <<'EOF'\n"
        "#define VALUE 41\n"
        "EOF\n"
        "cat > Makefile <<'EOF'\n"
        "CPPFLAGS = -I.\n"
        "OBJS = helper.o\n"
        "libhelper.a: $(OBJS)\n"
        "\tar rcs libhelper.a $(OBJS)\n\n"
        "helper.o: helper.c config.h\n"
        "\tcc $(CPPFLAGS) -c -o helper.o helper.c\n"
        "EOF\n"
    , encoding="utf-8")
    configure_sh.chmod(0o755)
    helper_c.write_text(
        "#include \"config.h\"\n"
        "int helper(void) { return VALUE; }\n"
    , encoding="utf-8")
    main_path.write_text(
        "int helper(void);\n"
        "int main(void) { return helper() == 41 ? 0 : 1; }\n"
    , encoding="utf-8")

    result = run_cli(
        [
            "--prepare-cmd",
            f"cd {dep_dir} && ./configure.sh",
            "--ensure-make-goal",
            f"{dep_dir}=libhelper.a",
            "--system-link",
            "--depends-on",
            f"{dep_dir}=libhelper.a",
            str(main_path),
        ],
    )

    assert result.returncode == 0


def test_cpp_arg_supports_single_file_define(tmp_path):
    main_path = tmp_path / "main.c"
    main_path.write_text(
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int main(void) { return VALUE == 42 ? 0 : 1; }\n"
    , encoding="utf-8")

    result = run_cli(
        ["--cpp-arg=-DVALUE=42", str(main_path)],
    )

    assert result.returncode == 0


def test_cpp_arg_supports_depends_on_multi_input(tmp_path):
    helper_path = tmp_path / "helper.c"
    helper_path.write_text(
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int helper(void) { return VALUE; }\n"
    , encoding="utf-8")
    main_path = tmp_path / "main.c"
    main_path.write_text(
        "int helper(void);\n"
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int main(void) { return helper() == VALUE ? 0 : 1; }\n"
    , encoding="utf-8")

    result = run_cli(
        ["--cpp-arg=-DVALUE=41", "--depends-on", str(helper_path), str(main_path)],
    )

    assert result.returncode == 0


@pytest.mark.parametrize("backend", ["llvm", "llvm_capi", "llvmlite", "llvm-capi"])
def test_removed_backends_are_rejected_before_compilation(tmp_path, backend):
    main_path = tmp_path / "main.c"
    main_path.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    result = run_cli(
        ["--backend", backend, str(main_path)],
    )

    assert result.returncode == 2
    assert "invalid backend" in result.stderr
    assert "expected self" in result.stderr


def test_backend_self_can_run_simple_program(tmp_path):
    main_path = tmp_path / "main.c"
    main_path.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    result = run_cli(
        ["--backend", "self", str(main_path)],
    )

    assert result.returncode == 0


def test_backend_self_env_can_run_simple_program(tmp_path, monkeypatch):
    main_path = tmp_path / "main.c"
    main_path.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    monkeypatch.setenv("PCC_BACKEND", "self")

    result = cli_main([str(main_path)])

    assert result == 0


def test_cli_launcher_emits_and_runs_owned_c_executable(tmp_path):
    source = tmp_path / "main.c"
    executable = tmp_path / "program"
    source.write_text(
        "int answer(void) { return 42; } int main(void) { return answer(); }\n",
        encoding="utf-8",
    )
    result = run_cli(
        ["--backend", "self", "-O2", str(source), "-o", str(executable)],
        entry="pcc.driver.cli_launcher",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "clamped" not in result.stderr
    assert "LLVM vectorizer" not in result.stderr
    assert subprocess.run([str(executable)], timeout=10).returncode == 42


def test_gpu_backend_metal_flag_is_accepted_as_device_config(tmp_path):
    main_path = tmp_path / "main.c"
    main_path.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    result = run_cli(
        ["--gpu-backend", "metal", str(main_path)],
    )

    assert result.returncode == 0, (result.stdout + result.stderr)


def test_gpu_backend_metal_is_annotated_kernel_only():
    config = resolve_gpu_backend("metal")

    assert config.kind == "metal"
    assert "host-device-split" in config.capabilities
    assert "annotated-kernel-only" in config.capabilities


def test_backend_self_emit_asm_starts_aarch64_mvp(tmp_path):
    main_path = tmp_path / "main.c"
    asm_path = tmp_path / "main.s"
    main_path.write_text("int main(void) { return 7; }\n", encoding="utf-8")

    result = run_cli(
        ["--backend", "self", "--emit-asm", str(asm_path), str(main_path)],
    )

    assert result.returncode == 0, (result.stdout + result.stderr)
    assert asm_path.is_file()
    asm_text = asm_path.read_text(encoding="utf-8")
    assert "_main:" in asm_text
    assert "movz w0, #7" in asm_text


def test_backend_self_emit_asm_honors_x86_64_linux_target(tmp_path):
    main_path = tmp_path / "main.c"
    asm_path = tmp_path / "main.s"
    main_path.write_text("int main(void) { return 7; }\n", encoding="utf-8")

    result = run_cli(
        [
            "--backend",
            "self",
            "--target",
            "x86_64-unknown-linux-gnu",
            "--emit-asm",
            str(asm_path),
            str(main_path),
        ],
    )

    assert result.returncode == 0, (result.stdout + result.stderr)
    asm_text = asm_path.read_text(encoding="utf-8")
    assert ".intel_syntax noprefix" in asm_text
    assert "\nmain:\n" in asm_text
    assert "mov eax, 7" in asm_text


def test_pass_option_can_select_single_repo_pass_at_o0(tmp_path):
    main_path = tmp_path / "main.c"
    main_path.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    result = run_cli(
        ["-O0", "--pass", "canonicalize", str(main_path)],
    )

    assert result.returncode == 0


def test_pass_option_can_select_owned_function_attribute_alias_at_o0(tmp_path):
    main_path = tmp_path / "main.c"
    main_path.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    result = run_cli(
        ["-O0", "--pass", "function-attrs", str(main_path)],
    )

    assert result.returncode == 0


def test_disable_pass_rejects_unknown_name(tmp_path):
    main_path = tmp_path / "main.c"
    main_path.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    result = run_cli(
        ["--disable-pass", "definitely-not-a-pass", str(main_path)],
    )

    assert result.returncode == 1
    assert "unknown pass name(s): definitely-not-a-pass" in (result.stdout + result.stderr)


def test_pass_option_can_select_owned_instruction_combining_alias(tmp_path):
    main_path = tmp_path / "main.c"
    main_path.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    result = run_cli(
        ["--pass", "instcombine", str(main_path)],
    )

    assert result.returncode == 0


def test_cpp_arg_supports_sources_from_make_directory(tmp_path):
    (tmp_path / "helper.c").write_text(
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int helper(void) { return VALUE; }\n"
    , encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "int helper(void);\n"
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int main(void) { return helper() == VALUE ? 0 : 1; }\n"
    , encoding="utf-8")
    (tmp_path / "ignored.c").write_text("int ignored(void) { return 99; }\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text(
        "app: helper.o main.o\n"
        "\tcc -o app helper.o main.o\n\n"
        "helper.o: helper.c\n"
        "\tcc -c -o helper.o helper.c\n\n"
        "main.o: main.c\n"
        "\tcc -c -o main.o main.c\n\n"
        "ignored.o: ignored.c\n"
        "\tcc -c -o ignored.o ignored.c\n"
    , encoding="utf-8")

    result = run_cli(
        ["--cpp-arg=-DVALUE=41", "--sources-from-make", "app", str(tmp_path)],
    )

    assert result.returncode == 0


def test_cpp_arg_supports_sources_from_make_directory_with_separate_tus(tmp_path):
    (tmp_path / "helper.c").write_text(
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int helper(void) { return VALUE; }\n"
    , encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "int helper(void);\n"
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int main(void) { return helper() == VALUE ? 0 : 1; }\n"
    , encoding="utf-8")
    (tmp_path / "ignored.c").write_text("int ignored(void) { return 99; }\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text(
        "app: helper.o main.o\n"
        "\tcc -o app helper.o main.o\n\n"
        "helper.o: helper.c\n"
        "\tcc -c -o helper.o helper.c\n\n"
        "main.o: main.c\n"
        "\tcc -c -o main.o main.c\n\n"
        "ignored.o: ignored.c\n"
        "\tcc -c -o ignored.o ignored.c\n"
    , encoding="utf-8")

    result = run_cli(
        [
            "--cpp-arg=-DVALUE=41",
            "--separate-tus",
            "--jobs",
            "2",
            "--sources-from-make",
            "app",
            str(tmp_path),
        ],
    )

    assert result.returncode == 0


def test_sources_from_make_infers_cpp_args_from_compile_commands(tmp_path):
    (tmp_path / "helper.c").write_text(
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int helper(void) { return VALUE; }\n"
    , encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "int helper(void);\n"
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int main(void) { return helper() == VALUE ? 0 : 1; }\n"
    , encoding="utf-8")
    (tmp_path / "Makefile").write_text(
        "CPPFLAGS = -DVALUE=41\n"
        "app: helper.o main.o\n"
        "\tcc -o app helper.o main.o\n\n"
        "helper.o: helper.c\n"
        "\tcc $(CPPFLAGS) -c -o helper.o helper.c\n\n"
        "main.o: main.c\n"
        "\tcc $(CPPFLAGS) -c -o main.o main.c\n"
    , encoding="utf-8")

    result = run_cli(
        ["--sources-from-make", "app", str(tmp_path)],
    )

    assert result.returncode == 0


def test_depends_on_make_goal_infers_cpp_args_from_compile_commands(tmp_path):
    dep_dir = tmp_path / "lib"
    dep_dir.mkdir()
    (dep_dir / "helper.c").write_text(
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int helper(void) { return VALUE; }\n"
    , encoding="utf-8")
    (dep_dir / "Makefile").write_text(
        "CPPFLAGS = -DVALUE=41\n"
        "lib: helper.o\n"
        "\tcc -o lib helper.o\n\n"
        "helper.o: helper.c\n"
        "\tcc $(CPPFLAGS) -c -o helper.o helper.c\n"
    , encoding="utf-8")
    main_path = tmp_path / "main.c"
    main_path.write_text(
        "int helper(void);\n"
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int main(void) { return helper() == VALUE ? 0 : 1; }\n"
    , encoding="utf-8")

    result = run_cli(
        ["--depends-on", f"{dep_dir}=lib", str(main_path)],
    )

    assert result.returncode == 0


def test_explicit_cpp_arg_overrides_make_inferred_cpp_arg(tmp_path):
    (tmp_path / "helper.c").write_text(
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int helper(void) { return VALUE; }\n"
    , encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "int helper(void);\n"
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int main(void) { return helper() == 41 ? 0 : 1; }\n"
    , encoding="utf-8")
    (tmp_path / "Makefile").write_text(
        "CPPFLAGS = -DVALUE=40\n"
        "app: helper.o main.o\n"
        "\tcc -o app helper.o main.o\n\n"
        "helper.o: helper.c\n"
        "\tcc $(CPPFLAGS) -c -o helper.o helper.c\n\n"
        "main.o: main.c\n"
        "\tcc $(CPPFLAGS) -c -o main.o main.c\n"
    , encoding="utf-8")

    result = run_cli(
        ["--sources-from-make", "app", "--cpp-arg=-DVALUE=41", str(tmp_path)],
    )

    assert result.returncode == 0
