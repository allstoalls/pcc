"""Resume frozen frontend/link plans inside a native compiler process.

The frontend coordinator has exited before this entry starts. Frontend and
indexed emission workers also exit between phases; only packed objects reach
the owned linker. No host interpreter implements any of these phases.
"""

import os
import sys

from pcc.frontends.python.pipeline_frontend_workers import shell_quote_arg
from pcc.frontends.python.deferred_frontend_schedule import run_frontend_pco_commands
from pcc.backend.owned_link_driver import main as link_main


def _lines(path):
    with open(path, "r", encoding="utf-8") as stream:
        return stream.read().splitlines()


def _absolute(path, label):
    if not path or not os.path.isabs(path):
        raise ValueError(label + " must be an absolute path")
    return path


def _file(path, label):
    _absolute(path, label)
    if not os.path.isfile(path):
        raise ValueError(label + " is missing: " + path)
    return path


def _native_worker(path):
    _file(path, "native worker")
    with open(path, "rb") as stream:
        magic = stream.read(4)
    if magic not in (b"\xcf\xfa\xed\xfe", b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf") or not os.access(path, os.X_OK):
        raise ValueError("deferred worker must be a native executable")


def _command(worker, arguments):
    # Explicit empty values override inherited plans in each worker. The
    # shared process pool executes argv/env through the platform ABI.
    parts = [
        "PCC_DEFER_FRONTEND_CODEGEN_PLAN=",
        "PCC_DEFER_FRONTEND_OUTPUT=",
        "PCC_DEFER_SELF_LINK_PLAN=",
        "PCC_PY_FRONTEND_IN_PROCESS_CODEGEN=0",
        "PCC_PY_FRONTEND_JOBS=1",
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE=1",
        "PCC_DIRECT_INDEXED_KERNEL_EMIT=1",
        "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES=1",
        "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND=1",
        "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK=1",
        "PCC_DIRECT_INDEXED_NATIVE_OBJECT=1",
        "PCC_DIRECT_INDEXED_SIDECAR=1",
        shell_quote_arg(worker),
    ]
    for argument in arguments:
        parts.append(shell_quote_arg(argument))
    return " ".join(parts)


def _link(output, manifest, runtime, extras):
    arguments = ["pcc-owned-link", "--out", output,
                 "--internal-input-manifest", manifest]
    if runtime:
        arguments.extend(["--archive", runtime])
    for extra in extras:
        arguments.extend(["--object", extra])
    link_main(arguments)
    if not os.path.isfile(output) or not os.access(output, os.X_OK):
        raise ValueError("native deferred link produced no executable")


def _codegen(lines):
    if len(lines) < 12 or lines[11] != "pidx-pco-v1":
        raise ValueError("native deferred codegen requires a v2 indexed plan")
    worker = lines[1]
    _native_worker(worker)
    output = _absolute(lines[2], "output")
    runtime = _file(lines[3], "runtime archive")
    manifest = _absolute(lines[5], "internal input manifest")
    artifacts = os.path.realpath(_absolute(lines[6], "artifact root"))
    count, oversized, jobs, manifests_count = int(lines[7]), int(lines[8]), int(lines[9]), int(lines[10])
    if count < 1 or manifests_count != count or len(lines) != 12 + count or oversized < 0 or oversized > count or jobs < 1 or jobs > 2:
        raise ValueError("native deferred codegen plan count mismatch")
    if not os.path.isdir(artifacts):
        raise ValueError("native deferred artifact root is missing")
    manifests = lines[12:]
    results = []
    indices = []
    commands = []
    expected_sidecars = []
    pco_commands = []
    seen = set()
    for path in manifests:
        row = _lines(_file(path, "worker manifest"))
        if len(row) < 12 or row[0] != "pcc.frontends.python.codegen_worker.v4" or row[-2] != "1":
            raise ValueError("native deferred codegen requires singleton v4 manifests")
        index = int(row[-1])
        if index < 0 or index >= count or index in seen:
            raise ValueError("native deferred manifest ownership mismatch")
        seen.add(index)
        indices.append(index)
        result = _absolute(row[1], "worker result")
        # Never accept a stale result after a worker incorrectly returns zero.
        if os.path.isfile(result):
            os.unlink(result)
        results.append(result)
        commands.append(_command(worker, ["--pcc-python-multi-codegen-worker", path]))
        # The worker writes its sidecar at this fixed name, so the module's PCO
        # job is known now and can follow its frontend job in the same pool.
        sidecar = os.path.join(
            _absolute(row[2], "worker IR directory"),
            "module_" + str(index) + ".direct.pidx",
        )
        if not os.path.realpath(sidecar).startswith(artifacts + os.sep):
            raise ValueError("indexed sidecar is outside the artifact root")
        packed = sidecar[:-5] + ".pco"
        for stale in (sidecar, packed):
            if os.path.isfile(stale):
                os.unlink(stale)
        expected_sidecars.append(sidecar)
        pco_commands.append(_command(worker, ["--pcc-self-backend-indexed-emit-worker", sidecar, packed, "PCO"]))
    run_frontend_pco_commands(
        commands, manifests, pco_commands, expected_sidecars, oversized, jobs,
    )
    ordered = [""] * count
    for position in range(count):
        rows = _lines(_file(results[position], "worker result"))
        if len(rows) != 1:
            raise ValueError("native deferred worker must return one module")
        parts = rows[0].split("\t")
        if len(parts) < 9 or parts[0] != "OK" or parts[3] != "0" or parts[4] != "0":
            raise ValueError("native deferred worker failed: " + rows[0])
        if int(parts[1]) != indices[position]:
            raise ValueError("native deferred result ownership mismatch")
        sidecars = []
        marker = 7
        while marker + 1 < len(parts):
            if parts[marker] == "PIDX":
                sidecars.append(parts[marker + 1])
            marker += 1
        if len(sidecars) != 1:
            raise ValueError("native deferred result requires one indexed sidecar")
        sidecar = _file(sidecars[0], "indexed sidecar")
        if not sidecar.endswith(".pidx") or not os.path.realpath(sidecar).startswith(artifacts + os.sep):
            raise ValueError("indexed sidecar is outside the artifact root")
        if os.path.realpath(sidecar) != os.path.realpath(expected_sidecars[position]):
            raise ValueError("native deferred sidecar is not the scheduled PCO input")
        ordered[indices[position]] = sidecar[:-5] + ".pco"
    with open(manifest, "w", encoding="utf-8") as stream:
        stream.write("pcc.macho-internal-inputs.v1\n" + str(count) + "\n")
        for packed in ordered:
            _file(packed, "packed native object")
            stream.write("PCO\t" + packed + "\n")
    _link(output, manifest, runtime, [])


def run(plan_path):
    lines = _lines(plan_path)
    if not lines:
        raise ValueError("empty native deferred plan")
    if lines[0] == "pcc.frontend-codegen-plan.v2":
        _codegen(lines)
    elif lines[0] == "pcc.deferred-self-link.v1":
        if len(lines) < 7 or int(lines[6]) < 0 or len(lines) != 7 + int(lines[6]):
            raise ValueError("invalid native deferred link plan")
        output = _absolute(lines[1], "output")
        runtime = _file(lines[2], "runtime archive") if lines[2] else ""
        manifest = _file(lines[3], "internal input manifest")
        extras = [_file(path, "extra input") for path in lines[7:]]
        _link(output, manifest, runtime, extras)
    else:
        raise ValueError("unsupported native deferred plan schema: " + lines[0])


def main():
    if sys.implementation.name != "pcc":
        raise RuntimeError("native deferred execution requires pcc1; host Python is not supported")
    if len(sys.argv) != 2:
        raise ValueError("native deferred execution requires one plan path")
    if sys.argv[1] != "--check":
        run(sys.argv[1])


if __name__ == "__main__":
    main()
