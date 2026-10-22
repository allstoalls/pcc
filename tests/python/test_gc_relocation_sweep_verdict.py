"""Relocation copies must not inherit a finished-cycle sweep verdict.

GC-P1-BACKEND4-CONCURRENT-SURVIVOR-FINALIZED: a freshly relocated copy of a
reachable value carried PY_FLAG_GC_SWEEP_CANDIDATE inherited through the
header memcpy from stale source metadata, and the next explicit collect --
which consumes pending candidates WITHOUT re-marking
(pcc_gc_collect_tracing sweeps pending candidates verbatim) -- ran PASS-0
``__del__`` on the live copy. CONFIRMED capture:
docs/investigations/gc-backend4-concurrent-entry-loss.md.

The contract under test is narrow and deterministic, no concurrency needed:
the backend-4 colored evacuation header memcpy must clear the stale verdict
from BOTH the destination copy and the sweep-visible source shell, so a
pending-candidate sweep can never consume a verdict that predates the copy.
The probe pokes the verdict directly, exactly as a finished tracing cycle
would have left it, relocates, and then drives the same explicit-collect
boundary the mutator drain used in the capture.

Scope note: the backend-3 generational oldify copy received the identical
mask hardening (1024 added to its cleared-flag set in both mirrors), but
its focused repro is not expressible through refill-driven promotion --
refill runs minor collections that consume a poked verdict on the young
source before oldify can copy it, which is the runtime consuming a verdict
correctly, not the inheritance defect. The backend-3 hardening is exercised
by the five-backend production contract instead.

The forwarding source remains a physical shell until remap retirement. It
must never run an independent finalizer, either while the replacement is
rooted or when its last real owner is released. This also guards the logical
object against duplicate finalization through the old address.

Nonclaims: this probe does not exercise the concurrent-stepping window
itself (the overlap probes own that).
"""
from pathlib import Path
import hashlib
import json
import os

import pytest

from tests.owned_runtime_c_fixture import admitted_runtime, link_c_harness
from tests.python.process_timeout import run_process_group_timeout

PROBE_SOURCE = r"""
#include "py_internal.h"
#include <stdint.h>
#include <stdio.h>

static int64_t finalized;
static PyObject *watched_copy;
static int64_t del_on_copy;
static int64_t del_other;

extern struct PyClassObject *py_class_new(
    const char *, struct PyClassObject **, int32_t,
    const char **, int32_t
);
extern void py_class_add_method(
    struct PyClassObject *, const char *, PyObject *
);
extern PyObject *py_instance_new(struct PyClassObject *);

static PyObject *probe_del(PyObject *self) {
    __atomic_add_fetch(&finalized, 1, __ATOMIC_ACQ_REL);
    if (watched_copy != 0 && self == watched_copy) del_on_copy++;
    else del_other++;
    return py_int_from_i64(0);
}

extern int64_t pcc_gc_object_is_known_no_lock(PyObject *);

static int64_t instance_payload_size(PyObject *inst) {
    /* Mirror of the relocation validator's own size computation
     * (pcc_gc_relocate_copy_payload_prepared_locked): header plus one
     * slot per field and the optional __dict__ slot. */
    PyInstanceObject *obj = (PyInstanceObject *)inst;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        inst, (PyObject **)&obj->cls
    );
    if (cls == NULL) return 0;
    int32_t n_fields = cls->n_fields;
    if (n_fields < 0) n_fields = 0;
    int64_t n_slots = (int64_t)n_fields;
    if ((py_header((PyObject *)cls)->flags & 2) == 0) n_slots++;
    return (int64_t)sizeof(PyInstanceObject)
        + n_slots * (int64_t)sizeof(PyObject *);
}

static int32_t flags_of(PyObject *o) {
    return py_header_flags_load(py_header(o));
}

static int64_t refcount_of(PyObject *obj) {
    return pcc_refcount_load(&((PyObjectHeader *)obj)->refcount);
}

static int fail_state(
    int code,
    const char *what,
    PyObject *source,
    PyObject *copy
) {
    fprintf(
        stderr,
        "code=%d what=%s src=%p src_flags=0x%x "
        "copy=%p copy_flags=0x%x copy_rc=%lld finalized=%lld "
        "del_on_copy=%lld del_other=%lld\n",
        code,
        what,
        (void *)source,
        source ? (unsigned)flags_of(source) : 0u,
        (void *)copy,
        copy ? (unsigned)flags_of(copy) : 0u,
        (long long)(copy ? refcount_of(copy) : -1),
        (long long)finalized,
        (long long)del_on_copy,
        (long long)del_other
    );
    return code;
}

int main(void) {
    const int32_t backend = @BACKEND@;
    if (pcc_threads_enabled() != @EXPECTED_THREADS@) return 6;
    if (pcc_refcount_strategy() != @EXPECTED_REFCOUNT_STRATEGY@) return 1;
    if (pcc_gc_set_backend(backend) != 0) return 2;

    struct PyClassObject *value_class =
        py_class_new("SweepVerdictValue", 0, 0, 0, 0);
    if (value_class == 0) return 3;
    pcc_gc_pin((PyObject *)value_class);
    py_class_add_method(
        value_class, "__del__", (PyObject *)(uintptr_t)probe_del
    );
    PyObject *source = py_instance_new(value_class);
    if (source == 0) return 4;

    static PyObject *root_slot;
    pcc_gc_store_root(&root_slot, source);
    /* Transfer the constructor reference into the owning root slot. */
    py_decref(source);
    void *root_handle = pcc_gc_scheduler_root_register_handle(&root_slot);
    if (root_handle == 0) return 5;

    /* Poke the verdict exactly as a finished tracing cycle leaves it on
     * an object it judged unreachable (white->candidate cut). */
    py_header_flags_or(py_header(source), PY_FLAG_GC_SWEEP_CANDIDATE);

    pcc_gc_reset_relocation_set();
    if (pcc_gc_select_relocation_set(1) != 1)
        return fail_state(13, "select", source, 0);
    PyObject *copy = pcc_gc_relocate_copy(
        source, instance_payload_size(source)
    );
    if (copy != 0) {
        /* relocate_copy hands back a caller-owned transfer ref. */
        py_decref(copy);
    }
    if (copy == 0 || copy == source)
        return fail_state(14, "relocate", source, copy);
    watched_copy = copy;

    /* THE CONTRACT: no published copy carries a finished-cycle verdict. */
    if ((flags_of(copy) & PY_FLAG_GC_SWEEP_CANDIDATE) != 0)
        return fail_state(20, "destination-inherits-verdict", source, copy);
    if (
        backend == PCC_GC_KIND_COLORED_RELOCATING
        && (flags_of(source) & PY_FLAG_GC_SWEEP_CANDIDATE) != 0
    ) {
        /* The backend-4 shell stays sweep-visible until remap retirement,
         * so its own stale verdict must die with the old identity. */
        return fail_state(21, "shell-keeps-verdict", source, copy);
    }

    /* The capture's consumer: an explicit collect consumes pending
     * candidates verbatim. The live copy must survive untouched. */
    (void)pcc_gc_collect(0);
    if (del_other != 0)
        return fail_state(27, "finalizer-ran-on-forwarding-shell", 0, 0);
    if ((flags_of(copy) & PY_FLAG_GC_SWEEP_CANDIDATE) != 0)
        return fail_state(22, "collect-recut-live-copy", source, copy);
    if (del_on_copy != 0)
        return fail_state(23, "finalizer-ran-on-live-copy", source, copy);
    if (pcc_gc_object_is_known_no_lock(copy) != 1)
        return fail_state(24, "live-copy-unknown-after-collect", source, copy);

    /* Normal reclamation stays exactly-once: release the one owning root and
     * drive collections to the final dispatch.  Object-index membership is
     * not ownership and must never be used as a license to decref again. */
    pcc_gc_scheduler_root_unregister_handle(root_handle);
    pcc_gc_store_root(&root_slot, 0);
    root_slot = 0;
    for (int i = 0; i < 16 && del_on_copy == 0; i++) {
        (void)pcc_gc_collect(0);
    }
    if (backend == PCC_GC_KIND_COLORED_RELOCATING) {
        if (del_on_copy != 1)
            return fail_state(25, "exactly-once-violated", source, copy);
    } else if (pcc_gc_object_is_known_no_lock(copy) != 0) {
        return fail_state(26, "dead-copy-still-known", source, copy);
    }

    if (del_other != 0)
        return fail_state(27, "finalizer-ran-on-forwarding-shell", 0, 0);

    printf(
        "ok backend=%d del_other=%ld\n",
        backend,
        (long)del_other
    );
    return 0;
}
"""


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _compile_probe(
    tmp_path: Path, kind: str, backend: int, *, expected_threads: bool = True,
) -> Path:
    assert type(expected_threads) is bool
    archive = admitted_runtime()
    provenance = Path(str(archive) + ".provenance.json")
    manifest = json.loads(provenance.read_text(encoding="utf-8"))
    assert manifest["members"]
    assert all(member["runtime_build_config"]["threads"] is expected_threads
               and member["runtime_build_config"]["refcount"] == "atomic"
               for member in manifest["members"])
    runtime_hashes = {
        suffix: _digest(Path(str(archive) + suffix))
        for suffix in ("", ".provenance.json", ".capi_syms")
    }
    runtime_dir = Path(__file__).resolve().parents[2] / "pcc/runtime"
    stem = f"sweep_verdict_gc{backend}_{kind}"
    source = tmp_path / f"{stem}.c"
    executable = tmp_path / stem
    # Admission records the requested refcount setting; the single-thread
    # kernel deliberately implements the non-atomic runtime strategy.
    expected_strategy = (
        "PCC_REFCOUNT_STRATEGY_ATOMIC"
        if expected_threads else "PCC_REFCOUNT_STRATEGY_NONATOMIC"
    )
    source.write_text(
        PROBE_SOURCE.replace("@BACKEND@", str(backend))
        .replace("@EXPECTED_THREADS@", str(int(expected_threads)))
        .replace("@EXPECTED_REFCOUNT_STRATEGY@", expected_strategy),
        encoding="utf-8",
    )
    build = link_c_harness(
        source, executable, extra_include_dirs=(str(runtime_dir / "src"),),
    )
    assert build.returncode == 0, build.stdout + build.stderr
    assert all(_digest(Path(str(archive) + suffix)) == digest
               for suffix, digest in runtime_hashes.items())
    identity = {
        "schema": "pcc.gc4-relocation-sweep-verdict.v1",
        "compiler": "host-pcc0", "backend": "self", "gc_backend": backend,
        "runtime_provisioning": False, "runtime_archive": str(archive),
        "runtime_hashes": runtime_hashes,
        "runtime_config": {"threads": expected_threads, "refcount": "atomic"},
        "target_triple": manifest["target_triple"],
        "source_sha256": _digest(source), "binary_sha256": _digest(executable),
    }
    executable.with_suffix(".identity.json").write_text(
        json.dumps(identity, indent=2) + "\n", encoding="utf-8",
    )
    return executable


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_relocation_copy_clears_stale_sweep_verdict_backend4(
    tmp_path: Path, kind: str
) -> None:
    """Backend 4 colored evacuation must publish a verdict-free copy."""
    _run_probe(tmp_path, kind, expected_threads=True)


@pytest.mark.integration
def test_relocation_copy_clears_stale_sweep_verdict_backend4_single_thread(
    tmp_path: Path,
) -> None:
    """Run the same native contract against the admitted CI compiler runtime."""
    _run_probe(tmp_path, "pcc_python_single_thread", expected_threads=False)


def _run_probe(tmp_path: Path, kind: str, *, expected_threads: bool) -> None:
    executable = _compile_probe(tmp_path, kind, backend=4, expected_threads=expected_threads)
    identity = json.loads(executable.with_suffix(".identity.json").read_text(encoding="utf-8"))
    run_env = {**os.environ}
    run_env.pop("PCC_REFCOUNT_STRATEGY", None)
    run = run_process_group_timeout(
        [str(executable)], timeout=120, env=run_env,
    )
    executable.with_suffix(".run.json").write_text(
        json.dumps(dict(identity, command=[str(executable)], returncode=run.returncode,
                        stdout=run.stdout, stderr=run.stderr), indent=2) + "\n",
        encoding="utf-8",
    )
    assert all(_digest(Path(identity["runtime_archive"] + suffix)) == digest
               for suffix, digest in identity["runtime_hashes"].items())
    assert run.returncode == 0, (
        f"{kind} gc4 sweep-verdict relocation probe returned "
        f"{run.returncode}: " + run.stdout + run.stderr
    )
    assert run.stdout == "ok backend=4 del_other=0\n", run.stdout + run.stderr


def test_source_sweep_verdict_clears_only_after_forwarding_commit() -> None:
    """Rollback must retain the source's pre-existing sweep verdict."""
    repo = Path(__file__).absolute().parents[2]

    py_source = (
        repo / "pcc/runtime/py/freestanding_gc_relocation_copy.py"
    ).read_text(encoding="utf-8")
    py_body = py_source.split(
        "def pcc_gc_backend4_relocate_copy_preallocated_unlocked(", 1
    )[1].split("@c_abi_export", 1)[0]
    py_commit = py_body.index(
        "pcc_gc_install_forwarding_preallocated_unlocked("
    )
    py_clear = py_body.index(
        "store_i32(from_obj, 12, load_i32(from_obj, 12) & ~1024)"
    )
    assert py_commit < py_clear
