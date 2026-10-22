from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).absolute().parents[2]
DOCKER_HARNESS = REPO_ROOT / "scripts" / "run_self_backend_linux_x86_64_docker.py"
CTESTSUITE_HARNESS = (
    REPO_ROOT / "scripts" / "run_self_backend_linux_x86_64_c_testsuite.py"
)
X86_64_LINUX_TRIPLE = "x86_64-unknown-linux-gnu"
X86_64_LINUX_ALIAS_TRIPLE = "amd64-pc-linux-gnu"
X86_64_LINUX_BUCKET_SIZE = 128
DOCKER_PROBE_TIMEOUT_SECONDS = 5
DOCKER_HARNESS_TIMEOUT_SECONDS = 900
LINUX_FULL_RUNTIME_ARTIFACT_ROOT = (
    Path.home() / ".cache" / "pcc" / "test-artifacts" / "linux-full-production-runtime"
)

pytestmark = pytest.mark.integration


@lru_cache(maxsize=1)
def _docker_available() -> bool:
    docker = shutil.which("docker")
    if docker is None or not DOCKER_HARNESS.is_file():
        return False
    try:
        result = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            cwd=str(REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DOCKER_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _run_linux_x86_64_harness(
    shell_script: str,
    *,
    host_artifacts: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = None
    if host_artifacts is not None:
        host_artifacts.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PCC_SELF_BACKEND_HOST_ARTIFACTS"] = str(host_artifacts.resolve())
    return subprocess.run(
        [sys.executable, str(DOCKER_HARNESS), "bash", "-lc", shell_script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=DOCKER_HARNESS_TIMEOUT_SECONDS,
        env=env,
    )


def test_docker_availability_requires_reachable_daemon(monkeypatch):
    calls = []

    def unavailable_daemon(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 1)

    _docker_available.cache_clear()
    monkeypatch.setattr(shutil, "which", lambda _name: "/fake/docker")
    monkeypatch.setattr(subprocess, "run", unavailable_daemon)

    assert _docker_available() is False
    assert calls[0][0][0] == [
        "/fake/docker",
        "info",
        "--format",
        "{{.ServerVersion}}",
    ]
    assert calls[0][1]["timeout"] == DOCKER_PROBE_TIMEOUT_SECONDS
    _docker_available.cache_clear()


def test_linux_harness_receives_persistent_artifact_mount(monkeypatch, tmp_path):
    calls = []

    def record_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(subprocess, "run", record_run)
    artifact_root = tmp_path / "host-artifacts"

    result = _run_linux_x86_64_harness(
        "true",
        host_artifacts=artifact_root,
    )

    assert result.returncode == 0
    assert artifact_root.is_dir()
    assert calls[0][1]["env"]["PCC_SELF_BACKEND_HOST_ARTIFACTS"] == str(
        artifact_root.resolve()
    )


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_docker_llvm_smoke_can_build_and_run():
    result = _run_linux_x86_64_harness(rf"""
set -euo pipefail
cat >/tmp/self_backend_linux_smoke.c <<'EOF'
int main(void) {{ return 0; }}
EOF
env -u LC_ALL uv run pcc --target {X86_64_LINUX_TRIPLE} --emit-obj /tmp/self_backend_linux_smoke.o /tmp/self_backend_linux_smoke.c
cc -no-pie /tmp/self_backend_linux_smoke.o -o /tmp/self_backend_linux_smoke
/tmp/self_backend_linux_smoke
""")

    assert result.returncode == 0, (
        "linux x86_64 docker llvm smoke failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_docker_self_backend_smoke_can_build_and_run():
    result = _run_linux_x86_64_harness(rf"""
set -euo pipefail
cat >/tmp/self_backend_linux_self_smoke.c <<'EOF'
int main(void) {{ return 42; }}
EOF
env -u LC_ALL uv run pcc --backend=self --target {X86_64_LINUX_TRIPLE} --emit-obj /tmp/self_backend_linux_self_smoke.o /tmp/self_backend_linux_self_smoke.c
cc -no-pie /tmp/self_backend_linux_self_smoke.o -o /tmp/self_backend_linux_self_smoke
/tmp/self_backend_linux_self_smoke
""")

    assert result.returncode == 42, (
        "linux x86_64 docker self-backend smoke failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_pcc_owned_static_elf_runs_without_undefined_symbols():
    """Pcc writes ET_REL and links ET_EXEC; neither as nor ld participates."""
    result = _run_linux_x86_64_harness(r"""
set -euo pipefail
work="$(mktemp -d /tmp/pcc-owned-elf.XXXXXX)"
assembly="$work/exit42.s"
exe="$work/exit42"
cat >"$assembly" <<'ASMEOF'
.intel_syntax noprefix
.text
.p2align 4, 0x90
.globl _start
.type _start, @function
_start:
  call answer
  mov edi, eax
  mov eax, 60
  syscall
.size _start, .-_start
.type answer, @function
answer:
  mov eax, 42
  ret
.size answer, .-answer
.section .note.GNU-stack,"",@progbits
ASMEOF
env -u LC_ALL uv run python scripts/pcc_link_elf.py \
  --asm "$assembly" --out "$exe" --entry _start
readelf -hW "$exe" | grep -E 'Type:[[:space:]]+EXEC' >/dev/null
if readelf -lW "$exe" | grep -E 'INTERP|DYNAMIC' >/dev/null; then
  echo 'owned static ELF unexpectedly has an interpreter/dynamic segment' >&2
  exit 91
fi
if nm -u "$exe" 2>/dev/null | grep -q .; then
  echo 'owned static ELF retained undefined symbols' >&2
  exit 92
fi
set +e
"$exe"
status=$?
set -e
test "$status" -eq 42
printf 'PCC_OWNED_STATIC_ELF_OK=exit:%s,interp:0,needed:0,undefined:0\n' "$status"
""")

    assert result.returncode == 0, (
        "pcc-owned static ELF gate failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "PCC_OWNED_STATIC_ELF_OK=exit:42" in result.stdout


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_freestanding_python_start_is_static_zero_libc():
    """The complete production artifact is one object derived from the
    freestanding pcc-Python source through the self backend.  ``as`` only
    assembles generated output; no hand-written C/assembly startup or runtime
    object participates in the link."""
    result = _run_linux_x86_64_harness(rf"""
set -euo pipefail
src=pcc/runtime/py/freestanding_linux_start.py
ll=/tmp/pcc_zero_libc.ll
asm=/tmp/pcc_zero_libc.generated.s
obj=/tmp/pcc_zero_libc.from_python.o
exe=/tmp/pcc_zero_libc
map=/tmp/pcc_zero_libc.map

env -u LC_ALL uv run pcc \
  --backend=self \
  --target {X86_64_LINUX_TRIPLE} \
  --python-library \
  --python-libpython=off \
  --emit-llvm="$ll" \
  "$src"
grep -F 'target triple = "{X86_64_LINUX_TRIPLE}"' "$ll" >/dev/null

env -u LC_ALL uv run python - <<'PYEOF'
from pathlib import Path
from pcc.backend.self_backend_dispatch import emit_self_asm

ir_path = Path("/tmp/pcc_zero_libc.ll")
asm_path = Path("/tmp/pcc_zero_libc.generated.s")
asm_path.write_text(
    emit_self_asm(ir_path.read_text(encoding="utf-8"), "{X86_64_LINUX_TRIPLE}"),
    encoding="utf-8",
)
PYEOF
grep -F '  mov r11, rsp' "$asm" >/dev/null
grep -F '  syscall' "$asm" >/dev/null
as --64 "$asm" -o "$obj"
ld -static -nostdlib -e _start -Map="$map" "$obj" -o "$exe"

file_text="$(file "$exe")"
printf 'FILE=%s\n' "$file_text"
printf '%s\n' "$file_text" | grep -F 'statically linked' >/dev/null
if readelf -l "$exe" | grep -q 'INTERP'; then
  echo 'unexpected PT_INTERP' >&2
  exit 1
fi
if readelf -d "$exe" 2>&1 | grep -q '(NEEDED)'; then
  echo 'unexpected DT_NEEDED' >&2
  exit 1
fi
undefined="$(nm -u "$exe")"
test -z "$undefined"
printf 'UNDEFINED=0\n'
load_objects="$(awk '$1 == "LOAD" && $2 ~ /[.]o$/ {{ print $2 }}' "$map")"
test "$load_objects" = "$obj"
printf 'LOAD_OBJECTS=%s\n' "$load_objects"
nm "$exe" | grep -E '[[:space:]]T[[:space:]]_start$' >/dev/null
"$exe" tracer-argument
""")

    assert result.returncode == 0, (
        "linux x86_64 zero-libc pcc-Python tracer failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "ELF 64-bit LSB executable" in result.stdout
    assert "statically linked" in result.stdout
    assert "UNDEFINED=0" in result.stdout
    assert "LOAD_OBJECTS=/tmp/pcc_zero_libc.from_python.o" in result.stdout
    assert result.stdout.rstrip().endswith("pcc zero-libc ok")


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_full_production_python_runtime_is_static_zero_libc():
    """A real managed app closes over the current production archive.

    The archive is built inside the Linux container with an isolated object
    directory and output path.  This deliberately does not consume or update
    the checkout's shared Darwin runtime artifacts.  Both executable inputs
    are authored in pcc-Python and lowered through the self backend; GNU ``ld``
    receives only those inputs plus the verified production archive.
    """
    script = r"""
set -euo pipefail
runtime_root="$PWD/pcc/runtime"
test -n "${PCC_HOST_TEST_ARTIFACTS:-}"

python_bin="$(env -u LC_ALL uv run python -c 'import sys; print(sys.executable)')"
pcc_bin="${python_bin%/python}/pcc"
test -x "$pcc_bin"
runtime_key="$(
  PCC_ACCEPT_PCC_BIN="$pcc_bin" PCC_ACCEPT_TARGET="__PCC_TARGET__" \
  env -u LC_ALL "$python_bin" - <<'PYEOF'
import hashlib
import os
from pathlib import Path

from tests.runtime_build_cache import _pcc_runtime_source_key

repo_root = Path.cwd()
digest = hashlib.sha256()
digest.update(b"pcc.linux-full-production-runtime.v1\0")
digest.update(
    _pcc_runtime_source_key(Path(os.environ["PCC_ACCEPT_PCC_BIN"])).encode(
        "ascii"
    )
)
for value in (
    os.environ["PCC_ACCEPT_TARGET"],
    "PCC_PYTHON_IR_PASS_TRANSPORT=text",
    "PCC_WITH_THREADS=1",
):
    digest.update(b"\0")
    digest.update(value.encode("utf-8"))
for relative in (
    "pyproject.toml",
    "uv.lock",
    "docker/self-backend-linux-x86_64.Dockerfile",
):
    digest.update(b"\0")
    digest.update((repo_root / relative).read_bytes())
print(digest.hexdigest()[:24])
PYEOF
)"
work="$PCC_HOST_TEST_ARTIFACTS/$runtime_key"
mkdir -p "$work"
objdir="$work/build_py"
archive="$work/libpy_runtime_pcc_py.a"
manifest="$archive.provenance.json"
app_src="$work/pcc_linux_runtime_app.py"
start_src="$runtime_root/py/freestanding_c_linux_start.py"
app_ll="$work/app.ll"
start_ll="$work/start.ll"
app_asm="$work/app.self.s"
start_asm="$work/start.self.s"
app_obj="$work/app.self.o"
start_obj="$work/start.self.o"
exe="$work/pcc-linux-production-runtime"
link_map="$work/pcc-linux-production-runtime.map"
printf 'ARTIFACT_DIR=%s\n' "$work"
printf 'RUNTIME_CACHE_KEY=%s\n' "$runtime_key"

# Keep the production fast pass preset enabled, but use its llvmlite-backed
# text transport: the Linux acceptance image intentionally carries the target
# toolchain and llvmlite, not an unrelated host LLVM-C 20 shared library.
printf 'RUNTIME_IR_PASS_TRANSPORT=text\n'
verify_runtime_archive() {
  PCC_ACCEPT_ARCHIVE="$archive" \
  PCC_ACCEPT_RUNTIME_ROOT="$runtime_root" \
  PCC_ACCEPT_TARGET="__PCC_TARGET__" \
  env -u LC_ALL "$python_bin" - <<'PYEOF'
import os
from pathlib import Path

from pcc.tools.runtime_archive_provenance import (
    PRODUCTION_POLICY,
    verify_runtime_archive_manifest,
)

archive = Path(os.environ["PCC_ACCEPT_ARCHIVE"])
runtime_root = Path(os.environ["PCC_ACCEPT_RUNTIME_ROOT"])
target = os.environ["PCC_ACCEPT_TARGET"]
manifest = verify_runtime_archive_manifest(
    archive,
    runtime_root=runtime_root,
)
if manifest["policy"] != PRODUCTION_POLICY:
    raise SystemExit("production provenance policy was not enforced")
if manifest["target_triple"] != target:
    raise SystemExit(
        f"runtime target {manifest['target_triple']!r} != {target!r}"
    )
bad_members = [
    record["member"]
    for record in manifest["members"]
    if record["source_kind"] != "pcc-python"
    or record["producer_kind"] != "pcc-python-library-ir-to-obj"
    or record["object_emitter"] != "llvmlite-target-machine"
    or record["uses_host_cc"] is not False
    or not str(record["source"]).startswith("pcc/runtime/py/")
    or not str(record["source"]).endswith(".py")
]
if bad_members:
    raise SystemExit(f"non-production members: {bad_members}")
print(f"PROVENANCE_MEMBERS={manifest['member_count']}")
PYEOF
}

runtime_cache_state=miss
if test -e "$archive" || test -e "$manifest"; then
  runtime_cache_state=invalid
  if test -s "$archive" && test -s "$manifest" \
    && verify_runtime_archive >"$work/runtime-cache-verify.log" 2>&1; then
    runtime_cache_state=hit
  fi
fi
if test "$runtime_cache_state" != hit; then
  make_force=()
  if test "$runtime_cache_state" = invalid; then
    make_force=(-B)
  fi
  if ! env -u LC_ALL PCC_PYTHON_IR_PASS_TRANSPORT=text \
    make "${make_force[@]}" -j2 -C "$runtime_root" \
    OBJDIR_PY="$objdir" \
    LIB_PCC_PY="$archive" \
    PCC="$pcc_bin" \
    PYTHON="$python_bin" \
    PCC_REPO_ROOT="$PWD" \
    PCC_WITH_THREADS=1 \
    "$archive" >"$work/runtime-build.log" 2>&1; then
    tail -200 "$work/runtime-build.log" >&2
    exit 1
  fi
fi
test -s "$archive"
test -s "$manifest"
verify_runtime_archive
printf '%s\n' "$runtime_key" >"$work/.runtime-ready.tmp"
mv -f "$work/.runtime-ready.tmp" "$work/.runtime-ready"
printf 'RUNTIME_CACHE=%s\n' "$runtime_cache_state"

cat >"$app_src" <<'PYEOF'
import gc

from pcc.extern import c_abi_typed_export, c_int32, c_int64, c_ptr, extern, c_obj, c_rawptr
from pcc.unsafe import cstr, load_ptr, ptr_diff, stack_alloc, store_ptr, write


PyOS_strtol = extern("PyOS_strtol", (c_ptr, c_ptr, c_int32), c_int64)
PyOS_strtoul = extern("PyOS_strtoul", (c_ptr, c_ptr, c_int32), c_int64)


@c_abi_typed_export("main", "i32", ("i32", "ptr", "ptr"))
def main(argc: int, argv: c_ptr, envp: c_ptr) -> int:
    write(1, cstr("P0\n"), 3)
    values = [20, 22]
    write(1, cstr("P1\n"), 3)
    answer_by_name = {"answer": values[0] + values[1]}
    write(1, cstr("P2\n"), 3)
    reclaimed = gc.collect()
    if reclaimed < 0:
        return 71
    if answer_by_name["answer"] != 42:
        return 72
    write(1, cstr("P3\n"), 3)
    end_slot = stack_alloc(8)
    signed_text = cstr(" -0x2a!")
    store_ptr(end_slot, 0, signed_text)
    if PyOS_strtol(signed_text, end_slot, 0) != -42:
        return 73
    if ptr_diff(load_ptr(end_slot, 0), signed_text) != 6:
        return 74
    unsigned_text = cstr("18446744073709551615!")
    store_ptr(end_slot, 0, unsigned_text)
    if PyOS_strtoul(unsigned_text, end_slot, 10) != -1:
        return 75
    if ptr_diff(load_ptr(end_slot, 0), unsigned_text) != 20:
        return 76
    write(1, cstr("P4\n"), 3)
    print("PCC_LINUX_FULL_RUNTIME_OK")
    write(1, cstr("P5\n"), 3)
    return 0
PYEOF
printf 'PHASE_MAP=P0:entry,P1:list,P2:dict,P3:gc,P4:strtol,P5:print\n'

PCC_HOST_PYTHON="$python_bin" env -u LC_ALL "$pcc_bin" \
  --backend=self \
  --target __PCC_TARGET__ \
  --python-library \
  --python-libpython=off \
  --ir-scaffold=on \
  --emit-llvm="$app_ll" \
  "$app_src"
PCC_HOST_PYTHON="$python_bin" env -u LC_ALL "$pcc_bin" \
  --backend=self \
  --target __PCC_TARGET__ \
  --python-library \
  --python-libpython=off \
  --ir-scaffold=on \
  --emit-llvm="$start_ll" \
  "$start_src"

grep -F 'target triple = "__PCC_TARGET__"' "$app_ll" >/dev/null
grep -F 'target triple = "__PCC_TARGET__"' "$start_ll" >/dev/null
grep -E 'call .*@py_list_new[(]' "$app_ll" >/dev/null
grep -E 'call .*@py_dict_new[(]' "$app_ll" >/dev/null
grep -E 'call .*@pcc_gc_collect[(]' "$app_ll" >/dev/null
grep -E 'define (external )?i32 @main[(]i32 ' "$app_ll" >/dev/null
grep -E 'define (external )?void @_start[(]ptr ' "$start_ll" >/dev/null
grep -E 'call i32 \(i32, ptr, ptr\) @main[(]' "$start_ll" >/dev/null
grep -F 'syscall' "$start_ll" >/dev/null

PCC_ACCEPT_WORK="$work" PCC_ACCEPT_TARGET="__PCC_TARGET__" \
env -u LC_ALL "$python_bin" - <<'PYEOF'
import os
from pathlib import Path

from pcc.backend.self_backend_dispatch import emit_self_asm

work = Path(os.environ["PCC_ACCEPT_WORK"])
target = os.environ["PCC_ACCEPT_TARGET"]
for name in ("app", "start"):
    ir_text = (work / f"{name}.ll").read_text(encoding="utf-8")
    (work / f"{name}.self.s").write_text(
        emit_self_asm(ir_text, target),
        encoding="utf-8",
    )
PYEOF

as --64 "$app_asm" -o "$app_obj"
as --64 "$start_asm" -o "$start_obj"
ld -static -nostdlib -e _start -Map="$link_map" \
  "$start_obj" "$app_obj" "$archive" -o "$exe"

file_text="$(file "$exe")"
printf 'FILE=%s\n' "$file_text"
printf '%s\n' "$file_text" | grep -F 'statically linked' >/dev/null
if ! readelf -h "$exe" | grep -E 'Type:.*EXEC' >/dev/null; then
  echo 'initial-thread TLS setup requires a static ET_EXEC image' >&2
  exit 1
fi
if readelf -l "$exe" | grep -q 'INTERP'; then
  echo 'unexpected PT_INTERP' >&2
  exit 1
fi
if readelf -d "$exe" 2>&1 | grep -q '(NEEDED)'; then
  echo 'unexpected DT_NEEDED' >&2
  exit 1
fi
undefined="$(nm -u "$exe")"
test -z "$undefined"
printf 'UNDEFINED=0\n'

tls_line="$(readelf -W -l "$exe" | awk '$1 == "TLS" {print}')"
test -n "$tls_line"
PCC_ACCEPT_TLS_LINE="$tls_line" env -u LC_ALL "$python_bin" - <<'PYEOF'
import os

fields = os.environ["PCC_ACCEPT_TLS_LINE"].split()
if len(fields) != 8 or fields[0] != "TLS":
    raise SystemExit(f"unexpected PT_TLS row: {fields!r}")
file_size = int(fields[4], 16)
memory_size = int(fields[5], 16)
alignment = int(fields[7], 16)
if file_size < 0 or file_size > memory_size:
    raise SystemExit(
        "PT_TLS file template exceeds its memory image: "
        f"filesz={file_size} memsz={memory_size}"
    )
if alignment <= 0 or alignment > 8 or alignment & (alignment - 1):
    raise SystemExit(
        "PT_TLS alignment exceeds the initial-thread TCB contract: "
        f"align={alignment}"
    )
rounded_size = (memory_size + alignment - 1) & -alignment
if memory_size <= 0 or rounded_size > 4088:
    raise SystemExit(
        "PT_TLS does not fit the compiler-owned initial-thread reserve: "
        f"memsz={memory_size} rounded={rounded_size}"
    )
print(
    "STATIC_TLS=variant-ii-auxv-template,"
    f"filesz={file_size},memsz={memory_size},align={alignment},reserve=4088"
)
PYEOF
if nm "$exe" | grep -q '__tls_get_addr'; then
  echo 'unexpected dynamic TLS resolver in static local-exec closure' >&2
  exit 1
fi
if readelf -rW "$exe" | grep -q 'R_X86_64_'; then
  echo 'unexpected residual relocation in static local-exec closure' >&2
  exit 1
fi

PCC_ACCEPT_ARCHIVE="$archive" \
PCC_ACCEPT_RUNTIME_ROOT="$runtime_root" \
PCC_ACCEPT_MAP="$link_map" \
PCC_ACCEPT_APP_OBJECT="$app_obj" \
PCC_ACCEPT_START_OBJECT="$start_obj" \
env -u LC_ALL "$python_bin" - <<'PYEOF'
import os
from pathlib import Path
import re

from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest

archive = Path(os.environ["PCC_ACCEPT_ARCHIVE"]).resolve()
runtime_root = Path(os.environ["PCC_ACCEPT_RUNTIME_ROOT"])
link_map = Path(os.environ["PCC_ACCEPT_MAP"])
app_object = Path(os.environ["PCC_ACCEPT_APP_OBJECT"]).resolve()
start_object = Path(os.environ["PCC_ACCEPT_START_OBJECT"]).resolve()
manifest = verify_runtime_archive_manifest(archive, runtime_root=runtime_root)
records = {record["member"]: record for record in manifest["members"]}
map_text = link_map.read_text(encoding="utf-8")

archive_references = re.findall(
    r"([^\s()]+[.]a)\(([^\s()]+)\)",
    map_text,
)
if not archive_references:
    raise SystemExit("link map contains no production archive members")
foreign_archives = sorted(
    {
        archive_name
        for archive_name, _member in archive_references
        if Path(archive_name).resolve() != archive
    }
)
if foreign_archives:
    raise SystemExit(f"foreign archives entered link: {foreign_archives}")
linked_members = sorted({member for _archive, member in archive_references})
unknown_members = sorted(set(linked_members) - set(records))
if unknown_members:
    raise SystemExit(f"linked members lack verified provenance: {unknown_members}")
bad_linked_members = [
    member
    for member in linked_members
    if records[member]["source_kind"] != "pcc-python"
    or records[member]["uses_host_cc"] is not False
    or str(records[member]["source"]).endswith(".c")
    or member.startswith("vendor_")
]
if bad_linked_members:
    raise SystemExit(f"forbidden linked members: {bad_linked_members}")

loads = {
    Path(line.removeprefix("LOAD ").strip()).resolve()
    for line in map_text.splitlines()
    if line.startswith("LOAD ")
}
expected_loads = {start_object, app_object, archive}
if loads != expected_loads:
    raise SystemExit(
        f"unexpected linker inputs: expected={sorted(map(str, expected_loads))} "
        f"actual={sorted(map(str, loads))}"
    )
print(f"LINKED_PROVENANCE_MEMBERS={len(linked_members)}")
print("LINK_INPUTS=2_SELF_PYTHON_OBJECTS_PLUS_PRODUCTION_ARCHIVE")
PYEOF

# Exercise the non-zero TLS template independently from the production
# archive's current all-zero TLS inventory.  This keeps the startup contract
# honest if a future pcc-Python runtime member adds initialized TLS.
tls_probe_ll="$work/tdata-probe.ll"
tls_probe_obj="$work/tdata-probe.o"
tls_probe_exe="$work/tdata-probe"
tls_probe_map="$work/tdata-probe.map"
PCC_ACCEPT_WORK="$work" env -u LC_ALL "$python_bin" - <<'PYEOF'
import os
from pathlib import Path

work = Path(os.environ["PCC_ACCEPT_WORK"])
(work / "tdata-probe.ll").write_text(
    '''target triple = "x86_64-unknown-linux-gnu"
@pcc_tls_probe_init = thread_local global i32 37, align 4
@pcc_tls_probe_zero = thread_local global i32 0, align 4

define i32 @main(i32 %argc, ptr %argv, ptr %envp) {
entry:
  %init = load i32, ptr @pcc_tls_probe_init, align 4
  %zero = load i32, ptr @pcc_tls_probe_zero, align 4
  %init_ok = icmp eq i32 %init, 37
  %zero_ok = icmp eq i32 %zero, 0
  %zero_status = select i1 %zero_ok, i32 0, i32 98
  %status = select i1 %init_ok, i32 %zero_status, i32 97
  ret i32 %status
}
''',
    encoding="utf-8",
)
PYEOF
env -u LC_ALL "$python_bin" -m pcc.tools.ir_to_obj \
  --target __PCC_TARGET__ "$tls_probe_ll" "$tls_probe_obj"
readelf -rW "$tls_probe_obj" >"$work/tdata-probe.object.relocations.txt"
tls_probe_relocations="$(
  grep -c 'R_X86_64_GOTTPOFF' "$work/tdata-probe.object.relocations.txt"
)"
test "$tls_probe_relocations" -eq 2
ld -static -nostdlib -e _start -Map="$tls_probe_map" \
  "$start_obj" "$tls_probe_obj" "$archive" -o "$tls_probe_exe"
readelf -h "$tls_probe_exe" >"$work/tdata-probe.elf-header.txt"
readelf -W -l "$tls_probe_exe" >"$work/tdata-probe.program-headers.txt"
readelf -rW "$tls_probe_exe" >"$work/tdata-probe.relocations.txt"
nm -u "$tls_probe_exe" >"$work/tdata-probe.nm-u.txt"
if ! grep -E 'Type:.*EXEC' "$work/tdata-probe.elf-header.txt" >/dev/null; then
  echo 'initialized TLS probe is not ET_EXEC' >&2
  exit 1
fi
test ! -s "$work/tdata-probe.nm-u.txt"
if grep -q 'R_X86_64_' "$work/tdata-probe.relocations.txt"; then
  echo 'initialized TLS probe retained an unresolved relocation' >&2
  exit 1
fi
tls_probe_line="$(
  awk '$1 == "TLS" {print}' "$work/tdata-probe.program-headers.txt"
)"
PCC_ACCEPT_TLS_LINE="$tls_probe_line" env -u LC_ALL "$python_bin" - <<'PYEOF'
import os

fields = os.environ["PCC_ACCEPT_TLS_LINE"].split()
if len(fields) != 8 or fields[0] != "TLS":
    raise SystemExit(f"unexpected initialized PT_TLS row: {fields!r}")
file_size = int(fields[4], 16)
memory_size = int(fields[5], 16)
alignment = int(fields[7], 16)
if (file_size, memory_size, alignment) != (4, 8, 4):
    raise SystemExit(
        "initialized PT_TLS layout drifted: "
        f"filesz={file_size} memsz={memory_size} align={alignment}"
    )
PYEOF
set +e
"$tls_probe_exe" >"$work/tdata-probe.stdout" \
  2>"$work/tdata-probe.stderr"
tls_probe_status=$?
set -e
printf '%s\n' "$tls_probe_status" >"$work/tdata-probe.status"
test "$tls_probe_status" -eq 0
test ! -s "$work/tdata-probe.stdout"
test ! -s "$work/tdata-probe.stderr"
printf 'TDATA_TLS_TEMPLATE_OK=filesz4,memsz8,align4,gottpoff2\n'

# Lower the identical initialized/zero TLS IR through the self backend and
# link it into the same zero-libc _start/runtime closure.  The LLVM object
# above remains the relocation/layout oracle; this second final ELF proves
# that the self path consumes the compiler-owned initial-thread TCB contract.
tls_probe_self_asm="$work/tdata-probe.self.s"
tls_probe_self_obj="$work/tdata-probe.self.o"
tls_probe_self_exe="$work/tdata-probe.self"
tls_probe_self_map="$work/tdata-probe.self.map"
PCC_ACCEPT_TLS_IR="$tls_probe_ll" \
PCC_ACCEPT_TLS_ASM="$tls_probe_self_asm" \
PCC_ACCEPT_TARGET="__PCC_TARGET__" \
env -u LC_ALL "$python_bin" - <<'PYEOF'
import os
from pathlib import Path

from pcc.backend.self_backend_dispatch import emit_self_asm

ir_path = Path(os.environ["PCC_ACCEPT_TLS_IR"])
Path(os.environ["PCC_ACCEPT_TLS_ASM"]).write_text(
    emit_self_asm(
        ir_path.read_text(encoding="utf-8"),
        os.environ["PCC_ACCEPT_TARGET"],
    ),
    encoding="utf-8",
)
PYEOF
as --64 "$tls_probe_self_asm" -o "$tls_probe_self_obj"
readelf -rW "$tls_probe_self_obj" >"$work/tdata-probe.self.object.relocations.txt"
tls_probe_self_relocations="$(
  grep -c 'R_X86_64_GOTTPOFF' "$work/tdata-probe.self.object.relocations.txt"
)"
test "$tls_probe_self_relocations" -eq 2
if grep -E 'R_X86_64_PC32.*pcc_tls_probe_(init|zero)' \
  "$work/tdata-probe.self.object.relocations.txt" >/dev/null; then
  echo 'self zero-libc TLS probe used an ordinary-data relocation' >&2
  exit 1
fi
ld -static -nostdlib -e _start -Map="$tls_probe_self_map" \
  "$start_obj" "$tls_probe_self_obj" "$archive" -o "$tls_probe_self_exe"
test -z "$(nm -u "$tls_probe_self_exe")"
if readelf -rW "$tls_probe_self_exe" | grep -q 'R_X86_64_'; then
  echo 'self zero-libc TLS probe retained an unresolved relocation' >&2
  exit 1
fi
self_tls_probe_layout="$(
  readelf -W -l "$tls_probe_self_exe" | \
    awk '$1 == "TLS" {print $5, $6, $8}'
)"
llvm_tls_probe_layout="$(printf '%s\n' "$tls_probe_line" | awk '{print $5, $6, $8}')"
test -n "$self_tls_probe_layout"
test "$self_tls_probe_layout" = "$llvm_tls_probe_layout"
set +e
"$tls_probe_self_exe" >"$work/tdata-probe.self.stdout" \
  2>"$work/tdata-probe.self.stderr"
tls_probe_self_status=$?
set -e
test "$tls_probe_self_status" -eq 0
test ! -s "$work/tdata-probe.self.stdout"
test ! -s "$work/tdata-probe.self.stderr"
printf 'SELF_TDATA_TLS_TEMPLATE_OK=filesz4,memsz8,align4,gottpoff2\n'

readelf -a "$exe" >"$work/pcc-linux-production-runtime.readelf.txt"
nm -an "$exe" >"$work/pcc-linux-production-runtime.nm.txt"
objdump -dr "$exe" >"$work/pcc-linux-production-runtime.objdump.txt"

set +e
"$exe" >"$work/program.stdout" 2>"$work/program.stderr"
program_status=$?
set -e
printf '%s\n' "$program_status" >"$work/program.status"
printf 'PROGRAM_STATUS=%s\n' "$program_status"
sed 's/^/PROGRAM_STDOUT:/' "$work/program.stdout"
sed 's/^/PROGRAM_STDERR:/' "$work/program.stderr"
if test "$program_status" -ne 0; then
  diag_src="$work/list_call_diagnostic.py"
  diag_ll="$work/list-call-diagnostic.ll"
  diag_asm="$work/list-call-diagnostic.self.s"
  diag_obj="$work/list-call-diagnostic.self.o"
  diag_exe="$work/list-call-diagnostic"
  diag_map="$work/list-call-diagnostic.map"
  cat >"$diag_src" <<'PYEOF'
from pcc.extern import (
    c_abi_typed_export,
    c_int32,
    c_int64,
    c_ptr,
    c_void,
    extern,
    c_obj,
    c_rawptr,
)
from pcc.unsafe import (
    cstr,
    gc_backend_current,
    malloc,
    null,
    ptr_is_null,
    store_i64,
    store_ptr,
    write,
)


pcc_diagnostics_runtime_log_event_code = extern(
    "pcc_diagnostics_runtime_log_event_code",
    (c_int32, c_int32, c_int64, c_int64, c_ptr),
    c_void,
)
pcc_gc_alloc = extern(
    "pcc_gc_alloc",
    (c_int64, c_int32, c_int32),
    c_obj,
)
pcc_gc_backend4_zpage_register_owner_payload_span = extern(
    "pcc_gc_backend4_zpage_register_owner_payload_span",
    (c_ptr, c_ptr, c_int64),
    c_int64,
)
pcc_threads_enabled = extern("pcc_threads_enabled", (), c_int64)
pcc_current_native_thread_token = extern(
    "pcc_current_native_thread_token",
    (),
    c_rawptr,
)


@c_abi_typed_export("main", "i32", ("i32", "ptr", "ptr"))
def main(argc: int, argv: c_ptr, envp: c_ptr) -> int:
    write(1, cstr("L0\n"), 3)
    pcc_diagnostics_runtime_log_event_code(1, 1, 40, 5, null())
    write(1, cstr("L1\n"), 3)
    write(1, cstr("A0\n"), 3)
    obj = pcc_gc_alloc(40, 5, 0)
    write(1, cstr("A1\n"), 3)
    if ptr_is_null(obj):
        return 81
    write(1, cstr("S0\n"), 3)
    store_i64(obj, 16, 0)
    write(1, cstr("S1\n"), 3)
    store_ptr(obj, 32, null())
    write(1, cstr("S2\n"), 3)
    store_i64(obj, 24, 4)
    write(1, cstr("S3\n"), 3)
    write(1, cstr("M0\n"), 3)
    items = malloc(32)
    write(1, cstr("M1\n"), 3)
    if ptr_is_null(items):
        return 82
    store_ptr(obj, 32, items)
    write(1, cstr("S4\n"), 3)
    write(1, cstr("Z0\n"), 3)
    pcc_gc_backend4_zpage_register_owner_payload_span(obj, items, 32)
    write(1, cstr("Z1\n"), 3)
    write(1, cstr("E0\n"), 3)
    threads_enabled = pcc_threads_enabled()
    write(1, cstr("E1\n"), 3)
    if threads_enabled != 0:
        return 83
    write(1, cstr("G0\n"), 3)
    backend = gc_backend_current()
    write(1, cstr("G1\n"), 3)
    if backend < 0 or backend > 4:
        return 84
    write(1, cstr("N0\n"), 3)
    token = pcc_current_native_thread_token()
    write(1, cstr("N1\n"), 3)
    if ptr_is_null(token):
        return 85
    return 0
PYEOF
  printf 'LIST_DIAG_MAP=L0/L1:log,A0/A1:gc-alloc,S*:stores,M0/M1:malloc,Z0/Z1:payload-span,E0/E1:threads-enabled,G0/G1:gc-backend,N0/N1:native-thread-token\n'
  if ! PCC_HOST_PYTHON="$python_bin" env -u LC_ALL "$pcc_bin" \
    --backend=self \
    --target __PCC_TARGET__ \
    --python-library \
    --python-libpython=off \
    --ir-scaffold=on \
    --emit-llvm="$diag_ll" \
    "$diag_src" >"$work/list-call-diagnostic.compile.log" 2>&1; then
    tail -200 "$work/list-call-diagnostic.compile.log" >&2
    exit 1
  fi
  PCC_ACCEPT_DIAG_LL="$diag_ll" \
  PCC_ACCEPT_DIAG_ASM="$diag_asm" \
  PCC_ACCEPT_TARGET="__PCC_TARGET__" \
  env -u LC_ALL "$python_bin" - <<'PYEOF'
import os
from pathlib import Path

from pcc.backend.self_backend_dispatch import emit_self_asm

ir_path = Path(os.environ["PCC_ACCEPT_DIAG_LL"])
asm_path = Path(os.environ["PCC_ACCEPT_DIAG_ASM"])
asm_path.write_text(
    emit_self_asm(
        ir_path.read_text(encoding="utf-8"),
        os.environ["PCC_ACCEPT_TARGET"],
    ),
    encoding="utf-8",
)
PYEOF
  as --64 "$diag_asm" -o "$diag_obj"
  ld -static -nostdlib -e _start -Map="$diag_map" \
    "$start_obj" "$diag_obj" "$archive" -o "$diag_exe"
  test -z "$(nm -u "$diag_exe")"
  readelf -a "$diag_exe" >"$work/list-call-diagnostic.readelf.txt"
  nm -an "$diag_exe" >"$work/list-call-diagnostic.nm.txt"
  objdump -dr "$diag_exe" >"$work/list-call-diagnostic.objdump.txt"
  set +e
  "$diag_exe" >"$work/list-call-diagnostic.stdout" \
    2>"$work/list-call-diagnostic.stderr"
  diag_status=$?
  set -e
  printf '%s\n' "$diag_status" >"$work/list-call-diagnostic.status"
  printf 'LIST_DIAG_STATUS=%s\n' "$diag_status"
  sed 's/^/LIST_DIAG_STDOUT:/' "$work/list-call-diagnostic.stdout"
  sed 's/^/LIST_DIAG_STDERR:/' "$work/list-call-diagnostic.stderr"
  exit "$program_status"
fi
cat >"$work/program.expected" <<'EOF'
P0
P1
P2
P3
P4
PCC_LINUX_FULL_RUNTIME_OK
P5
EOF
if ! cmp -s "$work/program.expected" "$work/program.stdout"; then
  diff -u "$work/program.expected" "$work/program.stdout" >&2
  exit 1
fi
""".replace("__PCC_TARGET__", X86_64_LINUX_TRIPLE)

    result = _run_linux_x86_64_harness(
        script,
        host_artifacts=LINUX_FULL_RUNTIME_ARTIFACT_ROOT,
    )

    assert result.returncode == 0, (
        "Linux full production pcc-Python runtime closure failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "PROVENANCE_MEMBERS=" in result.stdout
    assert "RUNTIME_IR_PASS_TRANSPORT=text" in result.stdout
    assert "RUNTIME_CACHE=" in result.stdout
    assert (
        "PHASE_MAP=P0:entry,P1:list,P2:dict,P3:gc,P4:strtol,P5:print" in result.stdout
    )
    assert "ELF 64-bit LSB executable" in result.stdout
    assert "statically linked" in result.stdout
    assert "UNDEFINED=0" in result.stdout
    assert "STATIC_TLS=variant-ii-auxv-template" in result.stdout
    assert "TDATA_TLS_TEMPLATE_OK=filesz4,memsz8,align4,gottpoff2" in result.stdout
    assert (
        "SELF_TDATA_TLS_TEMPLATE_OK=filesz4,memsz8,align4,gottpoff2"
        in result.stdout
    )
    assert "LINKED_PROVENANCE_MEMBERS=" in result.stdout
    assert "LINK_INPUTS=2_SELF_PYTHON_OBJECTS_PLUS_PRODUCTION_ARCHIVE" in result.stdout
    assert "PROGRAM_STATUS=0" in result.stdout
    assert "PROGRAM_STDOUT:PCC_LINUX_FULL_RUNTIME_OK" in result.stdout
    assert "PROGRAM_STDOUT:P5" in result.stdout


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_c_frontend_freestanding_libc_is_static_and_python_owned():
    """The public C CLI uses the strict pcc-Python libc and startup objects.

    Public output mode retains the exact owned executable and map. Compiler
    tool wrappers reject external product delegation; file/readelf/nm remain
    separately invoked artifact-inspection oracles.
    """
    result = _run_linux_x86_64_harness(r"""
set -euo pipefail
wrapper="$(mktemp -d /tmp/pcc-c-libc-owned.XXXXXX)"
for tool in cc gcc clang as ld ar; do
  cat >"$wrapper/$tool" <<'EOF'
#!/bin/sh
printf 'unexpected external compiler invocation: %s\n' "$0" >&2
printf '%s\n' "$0" >>/tmp/pcc-c-libc-external-attempt
exit 97
EOF
  chmod +x "$wrapper/$tool"
done

cat >/tmp/pcc-c-libc-consumer.c <<'EOF'
typedef unsigned long size_t;
void *malloc(size_t);
void free(void *);
void *memcpy(void *, const void *, size_t);
size_t strlen(const char *);

int main(int argc, char **argv, char **envp) {
    if (argc != 2 || argv == (void *)0 || envp == (void *)0) return 30;
    if (strlen(argv[1]) != 6 || envp[0] == (void *)0) return 31;
    char *copy = (char *)malloc(8);
    if (copy == (void *)0) return 32;
    memcpy(copy, argv[1], 7);
    int ok = copy[0] == 'b' && copy[5] == 'e' && copy[6] == 0;
    free(copy);
    return ok ? 0 : 33;
}
EOF

PATH="$wrapper:$PATH" \
PCC_RUNTIME_BUILD=owned PCC_SELF_OBJ=pcc PCC_SELF_LINK=pcc \
PCC_IR_TO_OBJ_EMITTER=pcc PCC_PYTHON_LIBPYTHON=off \
PCC_PYTHON_IR_PASSES=off \
env -u LC_ALL uv run pcc \
  --backend=self \
  --verbose \
  --freestanding-libc \
  --link-arg=-Wl,-Map,/tmp/pcc-c-libc.map \
  -o /tmp/pcc-c-libc-final \
  /tmp/pcc-c-libc-consumer.c
PATH="$wrapper:$PATH" /tmp/pcc-c-libc-final bridge
test ! -e /tmp/pcc-c-libc-external-attempt
file /tmp/pcc-c-libc-final | grep -F 'statically linked' >/dev/null
if readelf -l /tmp/pcc-c-libc-final | grep -q 'INTERP'; then
  echo 'unexpected PT_INTERP' >&2
  exit 1
fi
if readelf -d /tmp/pcc-c-libc-final 2>&1 | grep -q '(NEEDED)'; then
  echo 'unexpected DT_NEEDED' >&2
  exit 1
fi
test -z "$(nm -u /tmp/pcc-c-libc-final)"
grep -F 'libpy_runtime_pcc_py.a(freestanding_allocator.o)' /tmp/pcc-c-libc.map >/dev/null
grep -F 'libpy_runtime_pcc_py.a(freestanding_mem_str.o)' /tmp/pcc-c-libc.map >/dev/null
grep -F 'libpy_runtime_pcc_py.a(freestanding_platform_env.o)' /tmp/pcc-c-libc.map >/dev/null
uv run python - <<'PYMAP'
import hashlib
from pathlib import Path
import re
from pcc.backend.elf_x86_64 import read_archive_payloads

ownership = Path("/tmp/pcc-c-libc.map").read_text()
assert ownership.startswith("# pcc owned ELF link map v1\n")
assert "# entry _start\n" in ownership
image = Path("/tmp/pcc-c-libc-final").read_bytes()
assert "# image sha256=" + hashlib.sha256(image).hexdigest() + "\n" in ownership
for member in ("freestanding_allocator.o", "freestanding_mem_str.o", "freestanding_platform_env.o"):
    selected = re.search(
        r"^(.+libpy_runtime_pcc_py\.a)\(" + re.escape(member)
        + r"\) archive_sha256=([0-9a-f]{64}) member_sha256=([0-9a-f]{64})$",
        ownership, re.MULTILINE,
    )
    assert selected is not None, member
    archive = Path(selected.group(1)).read_bytes()
    assert hashlib.sha256(archive).hexdigest() == selected.group(2)
    payload = dict(read_archive_payloads(archive))[member]
    assert hashlib.sha256(payload).hexdigest() == selected.group(3)
PYMAP
if grep -E 'vendor_|libc[.]a|/build_py/.*[.]c' /tmp/pcc-c-libc.map >/dev/null; then
  echo 'non-pcc-Python libc owner entered final link' >&2
  exit 1
fi
printf 'C_FREESTANDING_STATIC_OK\n'
""")

    assert result.returncode == 0, (
        "Linux C-frontend freestanding-libc gate failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "C_FREESTANDING_STATIC_OK" in result.stdout


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_freestanding_allocator_executes_raw_syscall_object():
    """Run the allocator's own closed-object and ABI harness on Linux x86_64.

    The inner focused tests prove that the self-backend object has no undefined
    symbols, contains raw ``syscall`` instructions instead of mmap/munmap libc
    calls, and then execute its malloc-family ABI on the target.
    """
    result = _run_linux_x86_64_harness("""
set -euo pipefail
env -u LC_ALL PCC_PYTHON_IR_PASSES=off uv run pytest -q -n0 \
  tests/python/test_freestanding_allocator.py::test_freestanding_allocator_self_backend_uses_same_c_abi \
  tests/python/test_freestanding_allocator.py::test_linux_allocator_closes_over_raw_syscalls
""")

    assert result.returncode == 0, (
        "linux x86_64 freestanding allocator gate failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "2 passed" in result.stdout


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_docker_self_backend_smoke_supports_amd64_alias_triple():
    result = _run_linux_x86_64_harness(rf"""
set -euo pipefail
cat >/tmp/self_backend_linux_unsupported_alias.c <<'EOF'
int main(void) {{ return 7; }}
EOF
env -u LC_ALL uv run pcc --backend=self --target {X86_64_LINUX_ALIAS_TRIPLE} --emit-obj /tmp/self_backend_linux_unsupported_alias.o /tmp/self_backend_linux_unsupported_alias.c
cc -no-pie /tmp/self_backend_linux_unsupported_alias.o -o /tmp/self_backend_linux_unsupported_alias
/tmp/self_backend_linux_unsupported_alias
""")

    assert result.returncode == 7, (
        "linux x86_64 docker self-backend amd64-alias smoke failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_docker_self_backend_direct_call_and_binop_smoke():
    result = _run_linux_x86_64_harness(rf"""
set -euo pipefail
cat >/tmp/self_backend_linux_direct_call.c <<'EOF'
int add(int a, int b) {{ return a + b; }}
int main(void) {{ return add(40, 2); }}
EOF
env -u LC_ALL uv run pcc --backend=self --target {X86_64_LINUX_TRIPLE} --emit-obj /tmp/self_backend_linux_direct_call.o /tmp/self_backend_linux_direct_call.c
cc -no-pie /tmp/self_backend_linux_direct_call.o -o /tmp/self_backend_linux_direct_call
/tmp/self_backend_linux_direct_call
""")

    assert result.returncode == 42, (
        "linux x86_64 docker self-backend direct-call/binop smoke failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_docker_scalar_fp_int_bitcast_matches_clang():
    result = _run_linux_x86_64_harness(rf"""
set -euo pipefail
cat >/tmp/scalar_fp_int_bitcast.ll <<'EOF'
target triple = "{X86_64_LINUX_TRIPLE}"

define i32 @main() {{
entry:
  %double_value = bitcast i64 4607182418800017408 to double
  %double_bits = bitcast double %double_value to i64
  %double_ok = icmp eq i64 %double_bits, 4607182418800017408
  %float_value = bitcast i32 1065353216 to float
  %float_bits = bitcast float %float_value to i32
  %float_ok = icmp eq i32 %float_bits, 1065353216
  %ok = and i1 %double_ok, %float_ok
  %failed = xor i1 %ok, true
  %code = zext i1 %failed to i32
  ret i32 %code
}}
EOF
env -u LC_ALL uv run python - <<'PYEOF'
from pathlib import Path
from pcc.backend.self_backend_dispatch import emit_self_asm

ir_path = Path("/tmp/scalar_fp_int_bitcast.ll")
Path("/tmp/scalar_fp_int_bitcast.s").write_text(
    emit_self_asm(ir_path.read_text(encoding="utf-8"), "{X86_64_LINUX_TRIPLE}"),
    encoding="utf-8",
)
PYEOF
cc -no-pie /tmp/scalar_fp_int_bitcast.ll -o /tmp/scalar_fp_int_bitcast_llvm
cc -no-pie /tmp/scalar_fp_int_bitcast.s -o /tmp/scalar_fp_int_bitcast_self
/tmp/scalar_fp_int_bitcast_llvm
/tmp/scalar_fp_int_bitcast_self
""")

    assert result.returncode == 0, (
        "linux x86_64 scalar fp/int bitcast differential failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_docker_syscall6_differential_clang_vs_self_backend():
    """pcc.unsafe.syscall6 (musl x86_64 ABI): one IR module through the
    clang oracle and through the self backend must both perform the real
    SYS_write and return the raw rax result (16 written bytes)."""
    result = _run_linux_x86_64_harness(rf"""
set -euo pipefail
env -u LC_ALL uv run python - <<'PYEOF'
from pcc.ir import ir
from pcc.backend.self_backend_dispatch import emit_self_asm

MSG = b"pcc syscall6 ok\n"
mod = ir.Module(name="syscall6_smoke")
mod.triple = "{X86_64_LINUX_TRIPLE}"
i8 = ir.IntType(8)
i32 = ir.IntType(32)
i64 = ir.IntType(64)
arr_ty = ir.ArrayType(i8, len(MSG))
gv = ir.GlobalVariable(mod, arr_ty, name="pcc_syscall6_msg")
gv.initializer = ir.Constant(arr_ty, bytearray(MSG))
gv.global_constant = True
fn = ir.Function(mod, ir.FunctionType(i32, []), name="main")
b = ir.IRBuilder(fn.append_basic_block("entry"))
ptr = b.ptrtoint(gv, i64, name="msgaddr")
zero = ir.Constant(i64, 0)
ret = b.syscall6(ir.Constant(i64, 1), ir.Constant(i64, 1), ptr,
                 ir.Constant(i64, len(MSG)), zero, zero, zero, name="written")
b.ret(b.trunc(ret, i32, name="code"))
ir_text = str(mod)
open("/tmp/syscall6.ll", "w").write(ir_text)
open("/tmp/syscall6.s", "w").write(emit_self_asm(ir_text))
PYEOF
cc -no-pie /tmp/syscall6.ll -o /tmp/syscall6_llvm
cc -no-pie /tmp/syscall6.s -o /tmp/syscall6_self
set +e
/tmp/syscall6_llvm; echo "llvm_exit=$?"
/tmp/syscall6_self; echo "self_exit=$?"
exit 0
""")

    assert result.returncode == 0, (
        "linux x86_64 docker syscall6 differential harness failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert result.stdout.count("pcc syscall6 ok") == 2, result.stdout
    assert "llvm_exit=16" in result.stdout, result.stdout
    assert "self_exit=16" in result.stdout, result.stdout


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_self_tls_final_elf_matches_llvm_across_pthreads():
    """The self object must carry ELF TLS, not ordinary process-global data.

    One IR module is emitted through LLVM and through the x86 self backend,
    linked into separate final executables with the same native-pthread
    harness, and compared for initialized/zero TLS templates and isolation.
    """
    result = _run_linux_x86_64_harness(r"""
set -euo pipefail
work="$(mktemp -d /tmp/pcc-self-tls.XXXXXX)"
ir="$work/tls.ll"
self_asm="$work/tls.self.s"
self_obj="$work/tls.self.o"
llvm_obj="$work/tls.llvm.o"
harness="$work/harness.c"
self_exe="$work/tls.self"
llvm_exe="$work/tls.llvm"

cat >"$ir" <<'EOF'
target triple = "x86_64-unknown-linux-gnu"
@pcc_tls_probe_init = thread_local global i32 37, align 4
@pcc_tls_probe_zero = thread_local(initialexec) global i32 0, align 4

define i32 @pcc_tls_probe_read_init() {
entry:
  %value = load i32, ptr @pcc_tls_probe_init, align 4
  ret i32 %value
}

define i32 @pcc_tls_probe_read_zero() {
entry:
  %value = load i32, ptr @pcc_tls_probe_zero, align 4
  ret i32 %value
}

define void @pcc_tls_probe_write_init(i32 %value) {
entry:
  store i32 %value, ptr @pcc_tls_probe_init, align 4
  ret void
}

define ptr @pcc_tls_probe_address() {
entry:
  ret ptr @pcc_tls_probe_init
}
EOF

PCC_TLS_IR="$ir" PCC_TLS_ASM="$self_asm" env -u LC_ALL uv run python - <<'PYEOF'
import os
from pathlib import Path

from pcc.backend.self_backend_dispatch import emit_self_asm

ir_path = Path(os.environ["PCC_TLS_IR"])
Path(os.environ["PCC_TLS_ASM"]).write_text(
    emit_self_asm(
        ir_path.read_text(encoding="utf-8"),
        "x86_64-unknown-linux-gnu",
    ),
    encoding="utf-8",
)
PYEOF
as --64 "$self_asm" -o "$self_obj"
env -u LC_ALL uv run python -m pcc.tools.ir_to_obj \
  --target x86_64-unknown-linux-gnu "$ir" "$llvm_obj"

readelf -SW "$self_obj" >"$work/self.sections"
grep -E '[.]tdata[[:space:]].*WAT' "$work/self.sections" >/dev/null
grep -E '[.]tbss[[:space:]].*WAT' "$work/self.sections" >/dev/null
readelf -sW "$self_obj" >"$work/self.symbols"
grep -E 'TLS[[:space:]]+GLOBAL.*pcc_tls_probe_init$' "$work/self.symbols" >/dev/null
grep -E 'TLS[[:space:]]+GLOBAL.*pcc_tls_probe_zero$' "$work/self.symbols" >/dev/null
readelf -rW "$self_obj" >"$work/self.relocations"
self_tls_relocs="$(grep -c 'R_X86_64_GOTTPOFF' "$work/self.relocations")"
test "$self_tls_relocs" -eq 4
if nm -u "$self_obj" | grep -q '__tls_get_addr'; then
  echo 'self initial-exec TLS unexpectedly requires a dynamic TLS resolver' >&2
  exit 1
fi
if grep -E 'R_X86_64_PC32.*pcc_tls_probe_(init|zero)' "$work/self.relocations" >/dev/null; then
  echo 'self TLS access was downgraded to a process-global PC-relative relocation' >&2
  exit 1
fi

cat >"$harness" <<'EOF'
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>

int pcc_tls_probe_read_init(void);
int pcc_tls_probe_read_zero(void);
void pcc_tls_probe_write_init(int value);
void *pcc_tls_probe_address(void);

typedef struct {
    int id;
    int before;
    int zero;
    int after;
    uintptr_t address;
} WorkerResult;

static pthread_barrier_t worker_barrier;

static void *worker(void *raw) {
    WorkerResult *result = (WorkerResult *)raw;
    result->before = pcc_tls_probe_read_init();
    result->zero = pcc_tls_probe_read_zero();
    result->address = (uintptr_t)pcc_tls_probe_address();
    pthread_barrier_wait(&worker_barrier);
    pcc_tls_probe_write_init(100 + result->id);
    result->after = pcc_tls_probe_read_init();
    return 0;
}

int main(void) {
    WorkerResult first = {.id = 1};
    WorkerResult second = {.id = 2};
    pthread_t first_thread;
    pthread_t second_thread;
    int main_before = pcc_tls_probe_read_init();
    int main_zero = pcc_tls_probe_read_zero();
    uintptr_t main_address = (uintptr_t)pcc_tls_probe_address();
    pcc_tls_probe_write_init(91);
    if (pthread_barrier_init(&worker_barrier, 0, 3) != 0) return 79;
    if (pthread_create(&first_thread, 0, worker, &first) != 0 ||
        pthread_create(&second_thread, 0, worker, &second) != 0) {
        return 80;
    }
    pthread_barrier_wait(&worker_barrier);
    if (pthread_join(first_thread, 0) != 0 ||
        pthread_join(second_thread, 0) != 0) {
        return 81;
    }
    pthread_barrier_destroy(&worker_barrier);
    int distinct = main_address != first.address &&
                   main_address != second.address &&
                   first.address != second.address;
    int main_after = pcc_tls_probe_read_init();
    printf("TLS_OK main=%d/%d/%d workers=%d/%d/%d,%d/%d/%d distinct=%d\n",
           main_before, main_zero, main_after,
           first.before, first.zero, first.after,
           second.before, second.zero, second.after, distinct);
    if (main_before != 37 || main_zero != 0 || main_after != 91) return 82;
    if (first.before != 37 || first.zero != 0 || first.after != 101) return 83;
    if (second.before != 37 || second.zero != 0 || second.after != 102) return 84;
    if (!distinct) return 85;
    return 0;
}
EOF

cc -no-pie -pthread "$harness" "$self_obj" -o "$self_exe"
cc -no-pie -pthread "$harness" "$llvm_obj" -o "$llvm_exe"
readelf -W -l "$self_exe" >"$work/self.program-headers"
readelf -W -l "$llvm_exe" >"$work/llvm.program-headers"
self_tls_layout="$(awk '$1 == "TLS" {print $5, $6, $8}' "$work/self.program-headers")"
llvm_tls_layout="$(awk '$1 == "TLS" {print $5, $6, $8}' "$work/llvm.program-headers")"
test -n "$self_tls_layout"
test "$self_tls_layout" = "$llvm_tls_layout"
if readelf -rW "$self_exe" | grep -q 'R_X86_64_GOTTPOFF'; then
  echo 'final self ELF retained an unresolved TLS access relocation' >&2
  exit 1
fi
"$self_exe" >"$work/self.stdout"
"$llvm_exe" >"$work/llvm.stdout"
cmp "$work/self.stdout" "$work/llvm.stdout"
grep -F 'TLS_OK main=37/0/91 workers=37/0/101,37/0/102 distinct=1' \
  "$work/self.stdout" >/dev/null
printf 'SELF_TLS_FINAL_ELF_OK=layout:%s,gottpoff:%s,pthreads:2\n' \
  "$self_tls_layout" "$self_tls_relocs"
""")

    assert result.returncode == 0, (
        "linux x86_64 self TLS final-ELF differential failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "SELF_TLS_FINAL_ELF_OK=" in result.stdout


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_docker_atomics_differential_clang_vs_self_backend():
    """i32/i64/i8 atomics through the x86-TSO self-backend lowering: one
    IR module (21 checked load/store/rmw/cas/byte-flag/fence steps, exit
    0 on full agreement with LLVM semantics, else the failing step
    number) built from the .ll by clang (oracle) and from the
    self-backend .s."""
    result = _run_linux_x86_64_harness(r"""
set -euo pipefail
env -u LC_ALL uv run python tests/python/x86_64_atomics_ir_gen.py --out-ll /tmp/at.ll --out-s /tmp/at.s
cc -no-pie /tmp/at.ll -o /tmp/at_llvm 2>/dev/null
cc -no-pie /tmp/at.s -o /tmp/at_self
set +e
/tmp/at_llvm; echo "llvm_exit=$?"
/tmp/at_self; echo "self_exit=$?"
exit 0
""")

    assert result.returncode == 0, (
        "linux x86_64 docker atomics differential harness failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "llvm_exit=0" in result.stdout, result.stdout
    assert "self_exit=0" in result.stdout, result.stdout


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_docker_llvm_c_testsuite_exact_match_bucket():
    assert CTESTSUITE_HARNESS.is_file(), f"missing harness: {CTESTSUITE_HARNESS}"

    result = _run_linux_x86_64_harness(
        f"env -u LC_ALL uv run python scripts/run_self_backend_linux_x86_64_c_testsuite.py --mode llvm-native-exact --bucket-size {X86_64_LINUX_BUCKET_SIZE} --timeout 20"
    )

    assert result.returncode == 0, (
        "linux x86_64 docker llvm c-testsuite bucket failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_docker_self_backend_c_testsuite_bucket_handles_partial_support_cleanly():
    assert CTESTSUITE_HARNESS.is_file(), f"missing harness: {CTESTSUITE_HARNESS}"

    result = _run_linux_x86_64_harness(
        f"env -u LC_ALL uv run python scripts/run_self_backend_linux_x86_64_c_testsuite.py --mode self-partial --bucket-size {X86_64_LINUX_BUCKET_SIZE} --timeout 20"
    )

    assert result.returncode == 0, (
        "linux x86_64 docker self-backend partial c-testsuite bucket failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


@pytest.mark.pcc_gate(
    unavailable=None if _docker_available() else "docker harness not available"
)
def test_linux_x86_64_docker_self_backend_strict_exact_bucket():
    assert CTESTSUITE_HARNESS.is_file(), f"missing harness: {CTESTSUITE_HARNESS}"

    result = _run_linux_x86_64_harness(
        "env -u LC_ALL uv run python scripts/run_self_backend_linux_x86_64_c_testsuite.py "
        "--mode self-strict-exact --bucket-size 32 --timeout 20"
    )

    assert result.returncode == 0, (
        "linux x86_64 docker self-backend strict-exact c-testsuite bucket failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
