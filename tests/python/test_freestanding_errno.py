from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

REPO_ROOT = Path(__file__).absolute().parents[2]
ERRNO_SOURCE = REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_errno.py"


def _emit_ir(tmp_path: Path) -> str:
    output = tmp_path / "freestanding_errno.ll"
    pipeline.compile_python(
        str(ERRNO_SOURCE),
        str(output),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return output.read_text(encoding="utf-8")


def test_linux_errno_ir_has_pcc_tls_and_no_libc_or_loader_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_sys_platform_text", lambda self: "linux"
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_machine_text", lambda self: "x86_64"
    )
    ir_text = _emit_ir(tmp_path)
    assert "thread_local global i32 0" in ir_text
    for forbidden in (
        "@__error",
        "@__errno_location",
        "@strerror",
        "@strerror_r",
        "@dlopen",
        "@dlsym",
        "@dlclose",
    ):
        assert forbidden not in ir_text


def _build_errno_object(tmp_path: Path, emitter: str) -> Path:
    ir_text = _emit_ir(tmp_path)
    source = tmp_path / "freestanding_errno.ll"
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_errno.s"
        source.write_text(emit_self_asm(ir_text), encoding="utf-8")
    output = tmp_path / f"freestanding_errno_{emitter}.o"
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(output)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return output


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_errno_message_and_native_tls_are_thread_isolated(
    tmp_path: Path, emitter: str
) -> None:
    supported = (sys.platform == "darwin" and platform.machine() == "arm64") or (
        sys.platform.startswith("linux") and platform.machine() == "x86_64"
    )
    assert supported

    obj = _build_errno_object(tmp_path, emitter)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    undefined_names = {
        line.split()[-1].lstrip("_")
        for line in undefined.stdout.splitlines()
        if line.strip()
    }
    if sys.platform == "darwin":
        expected = {"error", "dlclose", "dlopen", "dlsym"}
        if emitter == "llvm":
            # LLVM's native Darwin TLS model enters libSystem's TLV resolver;
            # the self backend emits its own direct TLS sequence.
            expected.add("tlv_bootstrap")
        assert undefined_names == expected
    else:
        assert undefined_names == set()

    harness = tmp_path / "errno_harness.c"
    executable = tmp_path / f"errno_harness_{emitter}"
    harness.write_text(
        "#include <errno.h>\n"
        "#include <pthread.h>\n"
        "#include <stdint.h>\n"
        "#include <stdio.h>\n"
        "#include <string.h>\n"
        "int *pcc_errno_location(void);\n"
        "int pcc_errno_get(void);\n"
        "void pcc_errno_set(int);\n"
        "int pcc_errno_message_into(int, char *, uint64_t);\n"
        "typedef struct { int code; int seen; char message[128]; } Task;\n"
        "static pthread_mutex_t gate_lock = PTHREAD_MUTEX_INITIALIZER;\n"
        "static pthread_cond_t gate_cond = PTHREAD_COND_INITIALIZER;\n"
        "static int gate_arrived = 0;\n"
        "static void gate_wait(void) {\n"
        "  pthread_mutex_lock(&gate_lock);\n"
        "  gate_arrived++;\n"
        "  if (gate_arrived == 2) pthread_cond_broadcast(&gate_cond);\n"
        "  while (gate_arrived < 2) pthread_cond_wait(&gate_cond, &gate_lock);\n"
        "  pthread_mutex_unlock(&gate_lock);\n"
        "}\n"
        "static void *worker(void *raw) {\n"
        "  Task *task = (Task *)raw;\n"
        "  pcc_errno_set(task->code);\n"
        "  gate_wait();\n"
        "  task->seen = pcc_errno_get();\n"
        "  pcc_errno_message_into(task->seen, task->message, sizeof(task->message));\n"
        "  return NULL;\n"
        "}\n"
        "int main(void) {\n"
        "  char message[128];\n"
        "  errno = ENOENT;\n"
        "  if (pcc_errno_get() != ENOENT) return 10;\n"
        "  if (pcc_errno_message_into(ENOENT, message, sizeof(message)) != 0) return 11;\n"
        "  if (strcmp(message, strerror(ENOENT)) != 0) return 12;\n"
        "  pcc_errno_set(EACCES);\n"
        "  if (pcc_errno_get() != EACCES) return 13;\n"
        "  if (pcc_errno_message_into(EACCES, message, sizeof(message)) != 0) return 14;\n"
        "  if (strcmp(message, strerror(EACCES)) != 0) return 15;\n"
        "  if (pcc_errno_message_into(9999, message, sizeof(message)) != 0) return 16;\n"
        '  if (strstr(message, "9999") == NULL) return 17;\n'
        "#if defined(__linux__) && defined(__GLIBC__)\n"
        "  for (int code = 0; code <= 133; code++) {\n"
        "    if (pcc_errno_message_into(code, message, sizeof(message)) != 0) return 24;\n"
        "    if (strcmp(message, strerror(code)) != 0) return 25;\n"
        "  }\n"
        "#endif\n"
        "  Task left = { ENOENT, 0, {0} };\n"
        "  Task right = { EACCES, 0, {0} };\n"
        "  pthread_t a, b;\n"
        "  if (pthread_create(&a, NULL, worker, &left) != 0) return 19;\n"
        "  if (pthread_create(&b, NULL, worker, &right) != 0) return 20;\n"
        "  pthread_join(a, NULL); pthread_join(b, NULL);\n"
        "  if (left.seen != left.code || right.seen != right.code) return 21;\n"
        "  if (strcmp(left.message, strerror(left.code)) != 0) return 22;\n"
        "  if (strcmp(right.message, strerror(right.code)) != 0) return 23;\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    build = subprocess.run(
        ["clang", str(harness), str(obj), "-pthread", "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert (
        run.returncode == 0
    ), f"exit={run.returncode} stdout={run.stdout!r} stderr={run.stderr!r}"
