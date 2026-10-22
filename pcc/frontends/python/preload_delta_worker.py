"""Per-root unique-class preload deltas, computed in worker processes.

The coordinator's ``build_unique_external_class_preload_index`` rebuilds the
unique-class preload once per sensitive root -- 258 rebuilds over the other
391 modules, ~22 s of serial native coordinator time.  Each rebuild depends
only on the exports, so contiguous slices of the roots run in parallel
workers; the coordinator assigns type ids in root order afterwards, which
keeps the index identical to the serial build.

Wire: the exports file is the full-graph native exports wire; the roots file
lists one root per line; the result is ``_native_export_to_wire`` of
``((root, drop_keys, ((key, descriptor), ...)), ...)``.
"""

import json
import os


WORKER_ARG = "--pcc-preload-delta-worker"


def run(exports_path: str, roots_path: str, out_path: str) -> int:
    from pcc.frontends.python.pipeline_exports import _native_export_to_wire, _read_native_exports_wire
    from pcc.frontends.python.type_infer import build_unique_external_class_preload, preload_global_by_key, preload_root_delta
    from pcc.frontends.python.worker_resource_plan import publish_worker_resource

    native_exports, _derived = _read_native_exports_wire(exports_path)
    publish_worker_resource("exports")
    with open(roots_path, "r", encoding="utf-8") as stream:
        roots = [line for line in stream.read().splitlines() if line]
    global_by_key = preload_global_by_key(
        build_unique_external_class_preload(native_exports)
    )
    publish_worker_resource("preload")
    rows = []
    for root in roots:
        if root not in native_exports:
            raise ValueError("preload delta root is not an exported module")
        drop_keys, set_rows = preload_root_delta(native_exports, root, global_by_key)
        rows.append((root, drop_keys, set_rows))
        publish_worker_resource("root:" + root)
    text = json.dumps(_native_export_to_wire(tuple(rows)))
    partial = out_path + ".partial"
    with open(partial, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(partial, out_path)
    publish_worker_resource("complete")
    return 0


def read_result(out_path: str):
    from pcc.frontends.python.pipeline_exports import _native_export_from_wire

    with open(out_path, "r", encoding="utf-8") as stream:
        rows = _native_export_from_wire(json.loads(stream.read()))
    deltas = {}
    for row in rows:
        root, drop_keys, set_rows = row
        deltas[root] = (tuple(drop_keys), tuple((key, descriptor) for key, descriptor in set_rows))
    return deltas
