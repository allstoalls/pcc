"""Optimize existing runtime IR in place without recompiling any source.

The owned pass dispatcher runs the same ``mem2reg,sroa`` route a self-backend
compile takes. Verification, object emission and archiving also use pcc.

Working from the recorded ``.ll`` rather than recompiling is what makes this a
single-variable experiment: the frontend, the sources and the input IR are
byte-identical before and after the selected pass pipeline.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pcc.backend.ar_writer import write_archive
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module
from pcc.frontends.python.compiled_owned_passes import run_owned_passes
from pcc.tools.ir_to_obj import _emit_object_with_triple
from pcc.tools.runtime_archive_provenance import (
    assemble_runtime_archive_manifest, verify_runtime_archive_manifest,
    write_pcc_python_receipt, _source_from_logical_path,
)
from run_pcc_compile_ab import _performance_lock


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--modules", default="py_obj,py_list,py_gen",
                        help="comma-separated archive module names, or 'all'")
    parser.add_argument("--optimizer", choices=("owned",), default="owned",
                        help="'owned' runs pcc's own mem2reg,sroa dispatcher")
    parser.add_argument("--passes", default="mem2reg,sroa",
                        help="owned pass list to run")
    args = parser.parse_args()
    source = args.runtime_dir.resolve()
    output = args.output_dir.resolve()
    if output.exists():
        parser.error("refusing to overwrite an existing experiment")
    owned_passes = [name.strip() for name in args.passes.split(",") if name.strip()]
    modules = [name.strip() for name in args.modules.split(",") if name.strip()]
    # ``all`` means every archive member, resolved from the manifest below --
    # never every object in build_py.  That directory retains objects the
    # archive no longer ships (16 pcc_gui_* members after the GUI moved to its
    # own repository), and selecting one of those fails with a bare KeyError
    # against the manifest instead of naming the real problem.
    select_all = args.modules.strip() == "all"
    if not select_all and not modules:
        parser.error("no modules selected")
    archive_name = "libpy_runtime_pcc_py.a"
    baseline = source / archive_name
    with _performance_lock():
        original = verify_runtime_archive_manifest(baseline, runtime_root=source)
        shutil.copytree(source, output)
        # Only the new experimental tree becomes writable.
        for path in output.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        output.chmod(0o755)
        shutil.copyfile(__file__, output / "optimizer-script.py")
        report_path = output / "optimizer-experiment.json"
        report = {"schema": "pcc.runtime-ir-optimizer-experiment.v1", "complete": False,
                  "started_utc": datetime.now(timezone.utc).isoformat(),
                  "script_sha256": digest(__file__), "execution_owner": "pcc",
                  "baseline_archive": str(baseline), "baseline_sha256": digest(baseline),
                  "selected_modules": modules,
                  "optimizer": args.optimizer, "owned_passes": owned_passes,
                  "claim": "IR optimizer diagnostic on unchanged pcc-Python runtime sources",
                  "modules": []}

        def persist():
            report_path.write_text(json.dumps(report, indent=2) + "\n")

        persist()
        records = {r["member"]: r for r in original["members"]}
        if select_all:
            modules = sorted(member[:-2] for member in records)
            report["selected_modules"] = modules
        missing = sorted(name for name in modules if name + ".o" not in records)
        if missing:
            raise RuntimeError(
                "not archive members: "
                + ",".join(missing)
                + " (the archive ships "
                + str(len(records))
                + " members)"
            )
        for name in modules:
            member = name + ".o"
            record = records[member]
            ir_path = output / "build_py" / (name + ".ll")
            object_path = output / "build_py" / member
            before = ir_path.read_text()
            input_hash = digest(ir_path)
            if input_hash != record["ir_sha256"]:
                raise RuntimeError(name + " IR does not match its original object receipt")
            shutil.copyfile(ir_path, ir_path.with_suffix(".input.ll"))
            verify_parsed_module(parse_self_backend_module(before))
            triple = record["target_triple"]
            optimized = run_owned_passes(before, owned_passes, True)
            verify_parsed_module(parse_self_backend_module(optimized))
            ir_path.write_text(optimized)
            object_bytes, emitted_triple, emitter = _emit_object_with_triple(optimized, target_triple=triple)
            object_path.write_bytes(object_bytes)
            write_pcc_python_receipt(object_path=object_path, ir_path=ir_path,
                source_path=_source_from_logical_path(record["source"], output), runtime_root=output,
                target_triple=emitted_triple, object_bytes=object_bytes, object_emitter=emitter)
            report["modules"].append({"module": name,
                "source_sha256": record["source_sha256"],
                "input_ir_sha256": input_hash, "optimized_ir_sha256": digest(ir_path),
                "input_object_sha256": record["object_sha256"],
                "optimized_object_sha256": digest(object_path),
                "input_ir_bytes": len(before.encode()), "optimized_ir_bytes": len(optimized.encode())})
            persist()
            print("Optimized " + name, flush=True)
        objects = [output / "build_py" / record["member"] for record in original["members"]]
        archive = output / archive_name
        archive.write_bytes(write_archive([(path.name, path.read_bytes()) for path in objects]))
        candidate = assemble_runtime_archive_manifest(archive, objects, runtime_root=output)
        verify_runtime_archive_manifest(archive, runtime_root=output)
        changed = [after["member"] for before, after in zip(original["members"], candidate["members"], strict=True)
                   if before["object_sha256"] != after["object_sha256"]]
        selected_members = {name + ".o" for name in modules}
        if not set(changed) <= selected_members:
            raise RuntimeError(
                "members changed that were not selected: "
                + repr(sorted(set(changed) - selected_members))
            )
        report["unchanged_members"] = sorted(selected_members - set(changed))
        if digest(baseline) != report["baseline_sha256"]:
            raise RuntimeError("baseline changed during experiment")
        report.update(complete=True, candidate_archive=str(archive),
                      candidate_sha256=digest(archive), changed_members=changed)
        persist()
        print(archive, flush=True)


if __name__ == "__main__":
    main()
