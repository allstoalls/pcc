"""Explicit owned IR optimization entrypoint for host tools.

Unsupported passes fail at selection; no external parser, optimizer or compiler
is loaded to fill a capability gap.
"""
from __future__ import annotations

import os
import sys
import json
import time

from pcc.frontends.python.compiled_owned_passes import OWNED_PASS_NAMES, owns_passes, run_owned_passes
from pcc.frontends.python.compiled_default_passes import _has_py_cpy_call


class PythonIRPassError(RuntimeError):
    pass


def resolve_python_ir_pass_transport(raw=None):
    value = os.environ.get("PCC_PYTHON_IR_PASS_TRANSPORT", "") if raw is None else raw
    value = str(value or "").strip().lower()
    if value in ("", "text"):
        return "text"
    raise PythonIRPassError("PCC_PYTHON_IR_PASS_TRANSPORT=" + repr(value) + " was removed; owned passes consume pcc IR directly")


def _expand_pass_names(pass_names, ir_size=0, *, transport="text"):
    names = []
    for raw in pass_names:
        name = str(raw).strip()
        if name in ("default", "fast"):
            selected = ("mem2reg", "sroa")
        elif name in ("all", "full"):
            selected = OWNED_PASS_NAMES
        else:
            selected = (name,) if name else ()
        for selected_name in selected:
            if selected_name not in names:
                names.append(selected_name)
    return tuple(names)


def run_python_ir_pass_pipeline(ir_text, *, pass_names, module_name):
    resolve_python_ir_pass_transport()
    names = list(_expand_pass_names(pass_names))
    if not names:
        return str(ir_text)
    if not owns_passes(names):
        missing = [name for name in names if name not in OWNED_PASS_NAMES]
        raise PythonIRPassError("unsupported owned IR pass(es): " + ", ".join(missing))
    strict = str(os.environ.get("PCC_PYTHON_IR_PASS_STRICT_NO_LIBPYTHON", "")).lower() in ("1", "true", "on", "yes")
    current = str(ir_text)
    telemetry = str(os.environ.get("PCC_PYTHON_IR_PASS_TELEMETRY_PATH", "") or "").strip()
    if not telemetry:
        return run_owned_passes(current, names, strict)
    started = time.perf_counter()
    records = [{"event": "start", "module": module_name, "passes": names,
                "owner": "pcc", "ir_bytes": len(current.encode("utf-8"))}]
    try:
        result = run_owned_passes(current, names, strict)
    except Exception as error:
        records.append({"event": "error", "module": module_name,
                        "owner": "pcc", "error": str(error)})
        with open(telemetry, "a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(record) + "\n" for record in records))
        raise
    record = {"event": "pass" if len(names) == 1 else "pipeline", "module": module_name,
              "passes": names, "owner": "pcc",
              "status": "skipped" if strict and _has_py_cpy_call(current) else "run",
              "changed": result != current, "seconds": time.perf_counter() - started}
    if len(names) == 1:
        record["pass"] = names[0]
    records.append(record)
    records.append({"event": "end", "module": module_name, "owner": "pcc",
                    "ir_bytes": len(result.encode("utf-8"))})
    with open(telemetry, "a", encoding="utf-8") as stream:
        stream.write("".join(json.dumps(record) + "\n" for record in records))
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        print("usage: <module-name> <pass-csv> <ir-path>", file=sys.stderr)
        return 2
    module_name, pass_csv, path = argv
    with open(path, encoding="utf-8") as stream:
        text = stream.read()
    sys.stdout.write(run_python_ir_pass_pipeline(text, pass_names=pass_csv.split(","), module_name=module_name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
