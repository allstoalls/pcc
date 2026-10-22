import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PORT_SOURCE = (
    ROOT / "pcc" / "runtime" / "py" / "freestanding_metal_runtime.py"
)
RUNTIME_HEADER = ROOT / "pcc" / "runtime" / "include" / "py_runtime.h"
RUNTIME_MAKEFILE = ROOT / "pcc" / "runtime" / "Makefile"
RUNTIME_INCLUDE = ROOT / "pcc" / "runtime" / "include"


_CC_REASON = None if shutil.which("cc") else "cc is required for the Metal runtime C FFI shim test"
_PLATFORM_REASON = (
    None
    if sys.platform in ("darwin", "linux")
    else "dlfcn-based Metal runtime C FFI shim is only tested on Darwin/Linux"
)
pytestmark = [
    pytest.mark.pcc_gate(unavailable=_CC_REASON),
    pytest.mark.pcc_gate(unavailable=_PLATFORM_REASON),
]


def _cc() -> str:
    cc = shutil.which("cc")
    if cc is None:
        pytest.fail("cc is required for the Metal runtime C FFI shim test")
    return cc


def _compile(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, text=True, capture_output=True, timeout=30)


def _fake_bridge_path(tmp_path: Path) -> tuple[Path, list[str], list[str]]:
    if sys.platform == "darwin":
        bridge = tmp_path / "fake_metal_bridge.dylib"
        bridge_cmd = [_cc(), "-dynamiclib", "-o", str(bridge)]
        driver_extra: list[str] = []
    elif sys.platform.startswith("linux"):
        bridge = tmp_path / "fake_metal_bridge.so"
        bridge_cmd = [_cc(), "-shared", "-fPIC", "-o", str(bridge)]
        driver_extra = ["-ldl"]
    else:
        pytest.fail("dlfcn-based Metal runtime C FFI shim is only tested on Darwin/Linux")
    return bridge, bridge_cmd, driver_extra


def _build_fake_bridge(tmp_path: Path) -> tuple[Path, list[str]]:
    bridge, bridge_cmd, driver_extra = _fake_bridge_path(tmp_path)
    bridge_source = tmp_path / "fake_metal_bridge.c"
    bridge_source.write_text(
        textwrap.dedent(
            r"""
            #include <stdbool.h>
            #include <stdint.h>
            #include <stdlib.h>
            #include <string.h>

            typedef struct PccFakeBuffer {
                uint64_t nbytes;
                uint8_t storage[128];
            } PccFakeBuffer;

            int64_t pcc_fake_source_bridge(
                const char *source,
                uint64_t len,
                void **buffers,
                void **scalars,
                void (*fence_complete)(void *),
                void *fence_context,
                bool wait_until_completed
            ) {
                uint32_t u32_value = 0;
                double f64_value = 0.0;
                bool bool_value = false;

                if (source == 0 || len == 0 || source[0] != 'k') return 10;
                if (buffers == 0 || buffers[0] == 0) return 11;
                if (scalars == 0 || scalars[0] == 0 || scalars[1] == 0 || scalars[2] == 0) return 12;
                if (fence_complete != 0 || fence_context != 0) return 13;
                if (!wait_until_completed) return 14;

                memcpy(&u32_value, scalars[0], sizeof(u32_value));
                memcpy(&f64_value, scalars[1], sizeof(f64_value));
                memcpy(&bool_value, scalars[2], sizeof(bool_value));
                if (u32_value != 7u) return 15;
                if (f64_value != 1.5) return 16;
                if (!bool_value) return 17;
                return 0;
            }

            int64_t pcc_fake_metallib_bridge(
                const char *metallib_path,
                void **buffers,
                void **scalars,
                void (*fence_complete)(void *),
                void *fence_context,
                bool wait_until_completed
            ) {
                uint32_t u32_value = 0;
                double f64_value = 0.0;
                bool bool_value = false;

                if (metallib_path == 0 || metallib_path[0] != '/') return 30;
                if (buffers == 0 || buffers[0] == 0) return 31;
                if (scalars == 0 || scalars[0] == 0 || scalars[1] == 0 || scalars[2] == 0) return 32;
                if (fence_complete != 0 || fence_context != 0) return 33;
                if (!wait_until_completed) return 34;

                memcpy(&u32_value, scalars[0], sizeof(u32_value));
                memcpy(&f64_value, scalars[1], sizeof(f64_value));
                memcpy(&bool_value, scalars[2], sizeof(bool_value));
                if (u32_value != 7u) return 35;
                if (f64_value != 1.5) return 36;
                if (!bool_value) return 37;
                return 0;
            }

            int64_t pcc_metal_buffer_runtime_create(uint64_t nbytes, void **out_buffer) {
                if (out_buffer == 0) return 20;
                if (nbytes == 0 || nbytes > 128) return 21;
                PccFakeBuffer *buffer = (PccFakeBuffer *)calloc(1, sizeof(PccFakeBuffer));
                if (buffer == 0) return 28;
                buffer->nbytes = 128;
                *out_buffer = buffer;
                return 0;
            }

            int64_t pcc_metal_buffer_runtime_length(void *buffer, uint64_t *out_nbytes) {
                if (buffer == 0 || out_nbytes == 0) return 22;
                PccFakeBuffer *fake = (PccFakeBuffer *)buffer;
                *out_nbytes = fake->nbytes;
                return 0;
            }

            int64_t pcc_metal_buffer_runtime_write(
                void *buffer, uint64_t offset, const void *src, uint64_t nbytes) {
                if (buffer == 0 || src == 0) return 23;
                PccFakeBuffer *fake = (PccFakeBuffer *)buffer;
                if (offset > fake->nbytes || nbytes > fake->nbytes - offset) return 24;
                memcpy(fake->storage + offset, src, (size_t)nbytes);
                return 0;
            }

            int64_t pcc_metal_buffer_runtime_read(
                void *buffer, uint64_t offset, void *dst, uint64_t nbytes) {
                if (buffer == 0 || dst == 0) return 25;
                PccFakeBuffer *fake = (PccFakeBuffer *)buffer;
                if (offset > fake->nbytes || nbytes > fake->nbytes - offset) return 26;
                memcpy(dst, fake->storage + offset, (size_t)nbytes);
                return 0;
            }

            int64_t pcc_metal_buffer_runtime_release(void *buffer) {
                if (buffer == 0) return 27;
                free(buffer);
                return 0;
            }
            """
        )
    )
    _compile([*bridge_cmd, str(bridge_source)])
    return bridge, driver_extra


def test_metal_runtime_python_archive_route_is_declared() -> None:
    makefile = RUNTIME_MAKEFILE.read_text(encoding="utf-8")
    assert "freestanding_metal_runtime" in makefile
    assert "pcc_metal_runtime.o:" not in makefile
    header = RUNTIME_HEADER.read_text(encoding="utf-8")
    assert "pcc_metal_source_runtime_call_prebuilt" in header
    assert "pcc_metal_metallib_runtime_call_prebuilt" in header

    source = PORT_SOURCE.read_text(encoding="utf-8")
    assert "__pcc_freestanding__ = True" in source
    assert "Python.h" not in source
    assert "ctypes" not in source
    assert "py_cpy" not in source


def test_metal_runtime_python_port_emits_through_self_backend(tmp_path: Path) -> None:
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.frontends.python.pipeline import compile_python

    llvm_ir = tmp_path / "freestanding_metal_runtime.ll"
    assembly = tmp_path / "freestanding_metal_runtime.s"
    obj = tmp_path / "freestanding_metal_runtime.o"
    compile_python(
        str(PORT_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    assembly.write_text(
        emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    _compile([_cc(), "-c", str(assembly), "-o", str(obj)])
    symbols = subprocess.run(
        ["nm", "-g", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    decorated = "_" if sys.platform == "darwin" else ""
    for symbol in (
        "pcc_metal_source_runtime_call_prebuilt",
        "pcc_metal_metallib_runtime_call_prebuilt",
        "pcc_metal_buffer_runtime_create_prebuilt",
        "pcc_metal_buffer_runtime_release_prebuilt",
    ):
        assert any(
            line.endswith(" T " + decorated + symbol)
            for line in symbols.stdout.splitlines()
        )


def test_metal_runtime_calls_prebuilt_bridge_without_python(
    tmp_path: Path, pcc_runtime_archive: Path,
) -> None:
    bridge, driver_extra = _build_fake_bridge(tmp_path)

    driver_source = tmp_path / "driver.c"
    driver = tmp_path / "driver"
    driver_source.write_text(
        textwrap.dedent(
            r"""
            #include "py_runtime.h"

            #include <stdint.h>
            #include <stdio.h>
            #include <string.h>

            int main(int argc, char **argv) {
                if (argc != 2) return 20;
                const uint8_t source[] = "kernel void k(){}";
                uint64_t buffers[] = {0x1234000ULL};
                uint8_t scalar_payload[17] = {0};
                uint64_t scalar_offsets[] = {0, 8, 16};
                uint32_t u32_value = 7u;
                double f64_value = 1.5;

                memcpy(scalar_payload + 0, &u32_value, sizeof(u32_value));
                memcpy(scalar_payload + 8, &f64_value, sizeof(f64_value));
                scalar_payload[16] = 1;

                int64_t rc = pcc_metal_source_runtime_call_prebuilt(
                    argv[1],
                    "pcc_fake_source_bridge",
                    source,
                    sizeof(source) - 1,
                    buffers,
                    1,
                    scalar_payload,
                    scalar_offsets,
                    3,
                    1
                );
                printf("rc=%lld\n", (long long)rc);
                return rc == 0 ? 0 : 1;
            }
            """
        )
    )
    _compile(
        [
            _cc(),
            "-I",
            str(RUNTIME_INCLUDE),
            str(driver_source),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(driver),
            *driver_extra,
        ]
    )

    result = subprocess.run(
        [str(driver), str(bridge)],
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rc=0"


def test_metal_runtime_calls_prebuilt_metallib_bridge_without_python(
    tmp_path: Path, pcc_runtime_archive: Path,
) -> None:
    bridge, driver_extra = _build_fake_bridge(tmp_path)

    driver_source = tmp_path / "driver_metallib.c"
    driver = tmp_path / "driver_metallib"
    driver_source.write_text(
        textwrap.dedent(
            r"""
            #include "py_runtime.h"

            #include <stdint.h>
            #include <stdio.h>
            #include <string.h>

            int main(int argc, char **argv) {
                if (argc != 2) return 20;
                uint64_t buffers[] = {0x1234000ULL};
                uint8_t scalar_payload[17] = {0};
                uint64_t scalar_offsets[] = {0, 8, 16};
                uint32_t u32_value = 7u;
                double f64_value = 1.5;

                memcpy(scalar_payload + 0, &u32_value, sizeof(u32_value));
                memcpy(scalar_payload + 8, &f64_value, sizeof(f64_value));
                scalar_payload[16] = 1;

                int64_t rc = pcc_metal_metallib_runtime_call_prebuilt(
                    argv[1],
                    "pcc_fake_metallib_bridge",
                    "/tmp/fake.metallib",
                    buffers,
                    1,
                    scalar_payload,
                    scalar_offsets,
                    3,
                    1
                );
                printf("rc=%lld\n", (long long)rc);
                return rc == 0 ? 0 : 1;
            }
            """
        )
    )
    _compile(
        [
            _cc(),
            "-I",
            str(RUNTIME_INCLUDE),
            str(driver_source),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(driver),
            *driver_extra,
        ]
    )

    result = subprocess.run(
        [str(driver), str(bridge)],
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rc=0"


def test_metal_runtime_calls_prebuilt_buffer_runtime(
    tmp_path: Path, pcc_runtime_archive: Path,
) -> None:
    bridge, driver_extra = _build_fake_bridge(tmp_path)

    driver_source = tmp_path / "buffer_driver.c"
    driver = tmp_path / "buffer_driver"
    driver_source.write_text(
        textwrap.dedent(
            r"""
            #include "py_runtime.h"

            #include <stdint.h>
            #include <stdio.h>
            #include <string.h>

            int main(int argc, char **argv) {
                if (argc != 2) return 20;
                uint64_t buffer = 0;
                uint64_t nbytes = 0;
                uint8_t payload[] = "pcc-metal";
                uint8_t readback[9] = {0};

                int64_t rc = pcc_metal_buffer_runtime_create_prebuilt(
                    argv[1], 64, &buffer);
                if (rc != 0 || buffer == 0) return 21;
                rc = pcc_metal_buffer_runtime_length_prebuilt(
                    argv[1], buffer, &nbytes);
                if (rc != 0 || nbytes != 128) return 22;
                rc = pcc_metal_buffer_runtime_write_prebuilt(
                    argv[1], buffer, 4, payload, sizeof(payload) - 1);
                if (rc != 0) return 23;
                rc = pcc_metal_buffer_runtime_read_prebuilt(
                    argv[1], buffer, 4, readback, sizeof(readback));
                if (rc != 0) return 24;
                if (memcmp(payload, readback, sizeof(readback)) != 0) return 25;
                rc = pcc_metal_buffer_runtime_release_prebuilt(argv[1], buffer);
                if (rc != 0) return 26;
                printf("nbytes=%llu data=%.*s\n",
                       (unsigned long long)nbytes,
                       (int)sizeof(readback),
                       readback);
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    _compile(
        [
            _cc(),
            "-I",
            str(RUNTIME_INCLUDE),
            str(driver_source),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(driver),
            *driver_extra,
        ]
    )

    result = subprocess.run(
        [str(driver), str(bridge)],
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "nbytes=128 data=pcc-metal"


def test_no_libpython_pcc_program_calls_metal_runtime_c_shim(tmp_path: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    bridge, _driver_extra = _build_fake_bridge(tmp_path)
    source_literal = "kernel void k(){}"
    src = tmp_path / "pcc_metal_runtime_probe.py"
    exe = tmp_path / "pcc_metal_runtime_probe"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int32, c_int64, c_ptr, c_uint64
            from pcc.unsafe import (
                cstr,
                free,
                load_i8,
                load_i64,
                malloc,
                store_f64,
                store_i8,
                store_i32,
                store_i64,
            )

            pcc_metal_call = extern(
                "pcc_metal_source_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_metallib_call = extern(
                "pcc_metal_metallib_runtime_call_prebuilt",
                (c_ptr, c_ptr, c_ptr, c_ptr, c_uint64, c_ptr, c_ptr, c_uint64, c_int32),
                c_int64,
            )
            pcc_buffer_create = extern(
                "pcc_metal_buffer_runtime_create_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_length = extern(
                "pcc_metal_buffer_runtime_length_prebuilt",
                (c_ptr, c_uint64, c_ptr),
                c_int64,
            )
            pcc_buffer_write = extern(
                "pcc_metal_buffer_runtime_write_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_read = extern(
                "pcc_metal_buffer_runtime_read_prebuilt",
                (c_ptr, c_uint64, c_uint64, c_ptr, c_uint64),
                c_int64,
            )
            pcc_buffer_release = extern(
                "pcc_metal_buffer_runtime_release_prebuilt",
                (c_ptr, c_uint64),
                c_int64,
            )

            def main() -> None:
                runtime = cstr({json.dumps(str(bridge))})
                out_buffer = malloc(8)
                out_nbytes = malloc(8)
                buffers = malloc(8)
                payload = malloc(9)
                readback = malloc(9)
                scalar_payload = malloc(17)
                scalar_offsets = malloc(24)

                rc = pcc_buffer_create(runtime, 64, out_buffer)
                if rc != 0:
                    print(rc)
                    return
                buffer_ptr: int = load_i64(out_buffer, 0)
                store_i64(buffers, 0, buffer_ptr)
                rc = pcc_buffer_length(runtime, buffer_ptr, out_nbytes)
                if rc != 0:
                    print(rc)
                    return
                if load_i64(out_nbytes, 0) != 128:
                    print(900)
                    return

                store_i8(payload, 0, 112)
                store_i8(payload, 1, 99)
                store_i8(payload, 2, 99)
                store_i8(payload, 3, 45)
                store_i8(payload, 4, 109)
                store_i8(payload, 5, 101)
                store_i8(payload, 6, 116)
                store_i8(payload, 7, 97)
                store_i8(payload, 8, 108)
                rc = pcc_buffer_write(runtime, buffer_ptr, 4, payload, 9)
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_read(runtime, buffer_ptr, 4, readback, 9)
                if rc != 0:
                    print(rc)
                    return
                if load_i8(readback, 0) != 112 or load_i8(readback, 8) != 108:
                    print(901)
                    return

                store_i32(scalar_payload, 0, 7)
                store_f64(scalar_payload, 8, 1.5)
                store_i8(scalar_payload, 16, 1)
                store_i64(scalar_offsets, 0, 0)
                store_i64(scalar_offsets, 8, 8)
                store_i64(scalar_offsets, 16, 16)

                rc = pcc_metal_call(
                    runtime,
                    cstr("pcc_fake_source_bridge"),
                    cstr({json.dumps(source_literal)}),
                    {len(source_literal)},
                    buffers,
                    1,
                    scalar_payload,
                    scalar_offsets,
                    3,
                    1,
                )
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_metallib_call(
                    runtime,
                    cstr("pcc_fake_metallib_bridge"),
                    cstr("/tmp/fake.metallib"),
                    buffers,
                    1,
                    scalar_payload,
                    scalar_offsets,
                    3,
                    1,
                )
                if rc != 0:
                    print(rc)
                    return
                rc = pcc_buffer_release(runtime, buffer_ptr)
                print(rc)

                free(scalar_offsets)
                free(scalar_payload)
                free(readback)
                free(payload)
                free(buffers)
                free(out_nbytes)
                free(out_buffer)

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
    result = subprocess.run(
        [str(exe)],
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"
