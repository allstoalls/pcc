from pathlib import Path
import os
import platform
import subprocess
import sys

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOCATOR_SOURCE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_allocator.py"
)
ALLOCATOR_SYMBOLS = (
    "malloc",
    "calloc",
    "realloc",
    "free",
    "aligned_alloc",
    "malloc_usable_size",
    "memalign",
    "posix_memalign",
    "pcc_allocator_mapped_bytes",
    "pcc_allocator_live_requested_bytes",
    "pcc_allocator_live_usable_bytes",
    "pcc_os_heap_in_use_bytes",
    "pcc_os_heap_capacity_bytes",
    "pcc_allocator_reclaimable_slab_bytes",
    "pcc_allocator_trim",
)


def _compile_allocator_ir(tmp_path: Path) -> Path:
    out = tmp_path / "freestanding_allocator.ll"
    pipeline.compile_python(
        str(ALLOCATOR_SOURCE),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return out


def _build_allocator_object(tmp_path: Path) -> Path:
    llvm_ir = _compile_allocator_ir(tmp_path)
    obj = tmp_path / "freestanding_allocator.o"
    build = subprocess.run(
        ["clang", "-c", str(llvm_ir), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


def _build_allocator_self_object(tmp_path: Path) -> Path:
    from pcc.backend.self_backend_dispatch import emit_self_asm

    llvm_ir = _compile_allocator_ir(tmp_path)
    asm = tmp_path / "freestanding_allocator.s"
    obj = tmp_path / "freestanding_allocator_self.o"
    if sys.platform == "darwin" and platform.machine() == "arm64":
        target_triple = "arm64-apple-darwin"
    elif sys.platform.startswith("linux") and platform.machine() == "x86_64":
        target_triple = "x86_64-unknown-linux-gnu"
    else:
        raise AssertionError("unsupported allocator self-backend host")
    ir_text = "\n".join(
        'target triple = "' + target_triple + '"'
        if line.startswith("target triple = ")
        else line
        for line in llvm_ir.read_text(encoding="utf-8").splitlines()
    )
    asm.write_text(
        emit_self_asm(ir_text, target_triple),
        encoding="utf-8",
    )
    build = subprocess.run(
        ["clang", "-c", str(asm), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


def _run_allocator_harness(tmp_path: Path, name: str, obj: Path) -> None:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(
        r"""
#include <stddef.h>
#include <stdint.h>

void *malloc(size_t size);
void *calloc(size_t count, size_t size);
void *realloc(void *ptr, size_t size);
void free(void *ptr);
void *aligned_alloc(size_t alignment, size_t size);
void *memalign(size_t alignment, size_t size);
int posix_memalign(void **out, size_t alignment, size_t size);
size_t malloc_usable_size(void *ptr);
long pcc_allocator_mapped_bytes(void);
long pcc_allocator_live_requested_bytes(void);
long pcc_allocator_live_usable_bytes(void);
long pcc_os_heap_in_use_bytes(void);
long pcc_os_heap_capacity_bytes(void);

static int check_bytes(const unsigned char *ptr, size_t size, unsigned char seed) {
    size_t i = 0;
    while (i < size) {
        if (ptr[i] != (unsigned char)(seed + i)) return 0;
        i++;
    }
    return 1;
}

int main(void) {
    size_t n = 0;
    free((void *)0);
    while (n <= 513) {
        unsigned char *ptr = (unsigned char *)malloc(n);
        size_t used = n == 0 ? 1 : n;
        size_t i = 0;
        if (!ptr || ((uintptr_t)ptr & 15) != 0) return 1;
        if (malloc_usable_size(ptr) < used) return 2;
        while (i < n) {
            ptr[i] = (unsigned char)(37 + i);
            i++;
        }
        ptr = (unsigned char *)realloc(ptr, n + 257);
        if (!ptr || !check_bytes(ptr, n, 37)) return 3;
        if (malloc_usable_size(ptr) < n + 257) return 4;
        ptr = (unsigned char *)realloc(ptr, n == 0 ? 1 : n);
        if (!ptr || !check_bytes(ptr, n, 37)) return 5;
        free(ptr);
        n = n == 0 ? 1 : n * 2 + 1;
    }

    {
        unsigned char *zero = (unsigned char *)calloc(257, 3);
        size_t i = 0;
        if (!zero) return 6;
        while (i < 771) {
            if (zero[i] != 0) return 7;
            i++;
        }
        free(zero);
    }

    {
        void *aligned = aligned_alloc(256, 512);
        void *legacy = memalign(128, 33);
        void *out = (void *)(uintptr_t)0x1234;
        if (!aligned || ((uintptr_t)aligned & 255) != 0) return 8;
        if (!legacy || ((uintptr_t)legacy & 127) != 0) return 9;
        if (aligned_alloc(64, 96) != 0 || memalign(12, 33) != 0) return 10;
        if (posix_memalign(&out, 12, 64) != 22) return 11;
        if (out != (void *)(uintptr_t)0x1234) return 12;
        if (posix_memalign(&out, 512, 65) != 0) return 13;
        if (!out || ((uintptr_t)out & 511) != 0) return 14;
        free(aligned);
        free(legacy);
        free(out);
    }

    if (calloc((size_t)-1, 2) != 0) return 15;
    if (malloc((size_t)-1) != 0) return 16;
    {
        void *ptr = malloc(32);
        if (!ptr || realloc(ptr, 0) != 0) return 17;
    }
    {
        long mapped_before = pcc_allocator_mapped_bytes();
        int round = 0;
        while (round < 4096) {
            void *ptr = malloc(31);
            if (!ptr) return 18;
            free(ptr);
            round++;
        }
        if (pcc_allocator_mapped_bytes() > mapped_before + 65536) return 19;
        if (pcc_allocator_live_requested_bytes() != 0) return 20;
        if (pcc_allocator_live_usable_bytes() != 0) return 21;
        if (pcc_os_heap_in_use_bytes() != 0) return 28;
        if (pcc_os_heap_capacity_bytes() != pcc_allocator_mapped_bytes()) return 29;
    }
    {
        void *batch[4096];
        size_t requested = 0;
        size_t usable = 0;
        size_t i = 0;
        long mapped_before = pcc_allocator_mapped_bytes();
        long live_requested_before = pcc_allocator_live_requested_bytes();
        long live_usable_before = pcc_allocator_live_usable_bytes();
        while (i < 4096) {
            size_t size = (i * 173) % 2048 + 1;
            batch[i] = malloc(size);
            if (!batch[i]) return 22;
            requested += size;
            usable += malloc_usable_size(batch[i]);
            i++;
        }
        if (pcc_allocator_live_requested_bytes() - live_requested_before
            != (long)requested) return 23;
        if (pcc_allocator_live_usable_bytes() - live_usable_before
            != (long)usable) return 24;
        if (pcc_os_heap_in_use_bytes() != pcc_allocator_live_requested_bytes()) return 30;
        if (pcc_os_heap_capacity_bytes() != pcc_allocator_mapped_bytes()) return 31;
        if (pcc_allocator_mapped_bytes() - mapped_before
            > (long)usable + 4 * 1024 * 1024) return 25;
        i = 0;
        while (i < 4096) {
            free(batch[i]);
            i++;
        }
        if (pcc_allocator_live_requested_bytes() != live_requested_before) return 26;
        if (pcc_allocator_live_usable_bytes() != live_usable_before) return 27;
    }
    return 0;
}
""",
        encoding="utf-8",
    )
    link = subprocess.run(
        ["clang", "-fno-builtin", str(harness), str(obj), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def _run_threaded_allocator_harness(tmp_path: Path, name: str, obj: Path) -> None:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(
        r"""
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>

void *malloc(size_t size);
void *realloc(void *ptr, size_t size);
void free(void *ptr);
long pcc_allocator_mapped_bytes(void);

static void *worker(void *raw) {
    uintptr_t seed = (uintptr_t)raw;
    size_t i = 0;
    while (i < 5000) {
        size_t size = ((i * 131) + seed * 17) % 2048 + 1;
        unsigned char marker = (unsigned char)(i + seed);
        unsigned char *ptr = (unsigned char *)malloc(size);
        if (!ptr) return (void *)(uintptr_t)1;
        ptr[0] = marker;
        if (size > 1) ptr[size - 1] = (unsigned char)(marker ^ 0x5a);
        if ((i & 3) == 0) {
            size_t grown = size + 777;
            ptr = (unsigned char *)realloc(ptr, grown);
            if (!ptr || ptr[0] != marker) return (void *)(uintptr_t)2;
            ptr[grown - 1] = (unsigned char)(marker ^ 0xa5);
        } else if (size > 1 && ptr[size - 1] != (unsigned char)(marker ^ 0x5a)) {
            return (void *)(uintptr_t)3;
        }
        free(ptr);
        i++;
    }
    return (void *)0;
}

int main(void) {
    pthread_t threads[4];
    size_t i = 0;
    while (i < 4) {
        if (pthread_create(&threads[i], 0, worker, (void *)(uintptr_t)(i + 1)) != 0)
            return 4;
        i++;
    }
    i = 0;
    while (i < 4) {
        void *result = 0;
        if (pthread_join(threads[i], &result) != 0 || result != 0) return 5;
        i++;
    }
    if (pcc_allocator_mapped_bytes() > 8 * 1024 * 1024) return 6;
    return 0;
}
""",
        encoding="utf-8",
    )
    link = subprocess.run(
        [
            "clang",
            "-fno-builtin",
            "-pthread",
            str(harness),
            str(obj),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_freestanding_allocator_compiles_to_closed_platform_abi_object(tmp_path):
    obj = _build_allocator_object(tmp_path)
    symbols = subprocess.run(
        ["nm", "-g", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    for symbol in ALLOCATOR_SYMBOLS:
        assert "_" + symbol in symbols.stdout

    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    assert set(undefined.stdout.split()) == {"_mmap", "_munmap"}
    for forbidden in ("malloc", "calloc", "realloc", "free"):
        assert forbidden not in undefined.stdout.replace("munmap", "")


def test_freestanding_allocator_c_abi_size_alignment_and_realloc_matrix(tmp_path):
    _run_allocator_harness(tmp_path, "allocator_llvm_harness", _build_allocator_object(tmp_path))


def test_freestanding_allocator_self_backend_uses_same_c_abi(tmp_path):
    supported_darwin = sys.platform == "darwin" and platform.machine() == "arm64"
    supported_linux = sys.platform.startswith("linux") and platform.machine() == "x86_64"
    assert supported_darwin or supported_linux
    obj = _build_allocator_self_object(tmp_path)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    if supported_darwin:
        assert set(undefined.stdout.split()) == {"_mmap", "_munmap"}
    else:
        assert undefined.stdout.strip() == ""
    _run_allocator_harness(tmp_path, "allocator_self_harness", obj)


def test_freestanding_allocator_threaded_churn_llvm_and_self(tmp_path):
    _run_threaded_allocator_harness(
        tmp_path,
        "allocator_threaded_llvm",
        _build_allocator_object(tmp_path),
    )
    _run_threaded_allocator_harness(
        tmp_path,
        "allocator_threaded_self",
        _build_allocator_self_object(tmp_path),
    )


def test_linux_allocator_closes_over_raw_syscalls(tmp_path, monkeypatch):
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin,
        "_target_sys_platform_text",
        lambda self: "linux",
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin,
        "_target_machine_text",
        lambda self: "x86_64",
    )
    llvm_ir = _compile_allocator_ir(tmp_path)
    ir_text = llvm_ir.read_text(encoding="utf-8")
    assert "@mmap(" not in ir_text
    assert "@munmap(" not in ir_text
    declarations = [line for line in ir_text.splitlines() if line.startswith("declare ")]
    assert all("@malloc(" not in line for line in declarations)
    assert all("@calloc(" not in line for line in declarations)
    assert all("@realloc(" not in line for line in declarations)
    assert ir_text.count("syscall") >= 2

    linux_ir = "\n".join(
        'target triple = "x86_64-unknown-linux-gnu"'
        if line.startswith("target triple = ")
        else line
        for line in ir_text.splitlines()
    )
    asm = emit_self_asm(linux_ir, "x86_64-unknown-linux-gnu")
    assert "syscall" in asm
    assert "call mmap" not in asm
    assert "call munmap" not in asm


def test_pcc_python_runtime_archive_plan_selects_python_allocator_object():
    runtime_dir = REPO_ROOT / "pcc" / "runtime"
    plan = subprocess.run(
        ["make", "-B", "-n", "libpy_runtime_pcc_py.a"],
        cwd=runtime_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert plan.returncode == 0, plan.stdout + plan.stderr
    archive_lines = [
        line
        for line in plan.stdout.splitlines()
        if "ar rcs libpy_runtime_pcc_py.a.tmp" in line
    ]
    assert len(archive_lines) == 1
    assert "build_py/freestanding_allocator.o" in archive_lines[0]


def test_default_pcc_python_runtime_uses_allocator_under_all_gc_backends(
    tmp_path,
    pcc_runtime_archive,
    python_program_compiler,
):
    source = tmp_path / "allocator_runtime_smoke.py"
    executable = tmp_path / "allocator_runtime_smoke"
    source.write_text(
        "def main() -> None:\n"
        "    values = []\n"
        "    i = 0\n"
        "    while i < 257:\n"
        "        values.append(str(i) + ':owned')\n"
        "        i = i + 1\n"
        "    print(len(values))\n"
        "    print(values[256])\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    python_program_compiler(
        str(source),
        str(executable),
        backend="self",
        libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )

    symbols = subprocess.run(
        ["nm", "-g", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    allocator_symbol = "_malloc" if sys.platform == "darwin" else "malloc"
    assert any(
        line.endswith(" T " + allocator_symbol) for line in symbols.stdout.splitlines()
    ), symbols.stdout

    for backend in range(5):
        env = {**os.environ, "PCC_GC_BACKEND": str(backend)}
        run = subprocess.run(
            [str(executable)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert run.returncode == 0, (
            f"PCC_GC_BACKEND={backend}\n" + run.stdout + run.stderr
        )
        assert run.stdout == "257\n256:owned\n"


def test_freestanding_allocator_tracks_reclaimable_empty_slabs(tmp_path):
    """Per-slab free counter, explicit trim, and auto-trim on refill.

    Phase 1: fill two 16-byte-class slabs, free every cell; the reclaimable
    gauge reports fully-free slabs; taking one cell back drops it and freeing
    it restores it; an explicit pcc_allocator_trim() munmaps the fully-free
    slabs (mapped bytes fall, gauge -> 0) and retires their granule keys, so a
    following malloc -- whose fresh slab may land on the just-unmapped address
    -- re-registers cleanly (tombstone reuse path).
    Phase 2: five fully-free 16-class slabs sit idle; a refill of a DIFFERENT
    class must return them before mmapping (the self-limiting high-water).
    """
    obj = _build_allocator_object(tmp_path)
    harness = tmp_path / "reclaim_harness.c"
    executable = tmp_path / "reclaim_harness"
    harness.write_text(
        r"""
#include <stddef.h>
void *malloc(size_t size);
void free(void *ptr);
long pcc_allocator_reclaimable_slab_bytes(void);
long pcc_allocator_mapped_bytes(void);
long pcc_allocator_trim(void);

int main(void) {
    static void *p[5000];
    /* Phase 1: one 16-byte class slab holds 1024 cells; 1500 spans two. */
    for (int i = 0; i < 1500; i++) {
        p[i] = malloc(16);
        if (!p[i]) return 1;
    }
    if (pcc_allocator_reclaimable_slab_bytes() != 0) return 2;
    for (int i = 0; i < 1500; i++) free(p[i]);
    long r = pcc_allocator_reclaimable_slab_bytes();
    if (r < 65536) return 3;
    void *q = malloc(16);
    if (!q) return 4;
    if (pcc_allocator_reclaimable_slab_bytes() >= r) return 5;
    free(q);
    if (pcc_allocator_reclaimable_slab_bytes() != r) return 6;
    long mapped_before = pcc_allocator_mapped_bytes();
    if (pcc_allocator_trim() < 1) return 7;
    if (pcc_allocator_mapped_bytes() > mapped_before - 65536) return 8;
    if (pcc_allocator_reclaimable_slab_bytes() != 0) return 9;
    /* Re-registration after munmap (address may be reused) must work. */
    void *s = malloc(16);
    if (!s) return 10;
    free(s);
    /* Phase 2: five fully-free 16-class slabs, then a refill of another
       class must trim them before it mmaps. */
    for (int i = 0; i < 5000; i++) {
        p[i] = malloc(16);
        if (!p[i]) return 11;
    }
    for (int i = 0; i < 5000; i++) free(p[i]);
    if (pcc_allocator_reclaimable_slab_bytes() < 4 * 65536) return 12;
    long mapped_pre = pcc_allocator_mapped_bytes();
    void *big = malloc(1000); /* 1024-class: empty list -> refill -> trim */
    if (!big) return 13;
    if (pcc_allocator_reclaimable_slab_bytes() != 0) return 14;
    if (pcc_allocator_mapped_bytes() >= mapped_pre) return 15;
    free(big);
    return 0;
}
""",
        encoding="utf-8",
    )
    link = subprocess.run(
        ["clang", "-fno-builtin", str(harness), str(obj), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, f"exit={run.returncode}\n{run.stdout}{run.stderr}"
