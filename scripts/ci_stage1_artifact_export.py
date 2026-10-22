"""Export existing, admitted Linux x86-64 Stage1 bytes without building anything."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import stat
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
TARGET = "x86_64-unknown-linux-gnu"
CONFIG = {"threads": False, "refcount": "atomic"}
OUTPUT_DIRECTORY = "build/stage1-native-export"
ARCHIVE_NAME = "stage1-native.tar.gz"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _digest(value, length=64):
    return (
        isinstance(value, str) and len(value) == length
        and all(char in "0123456789abcdef" for char in value)
    )


class _HashingReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size):
        data = self.stream.read(size)
        self.digest.update(data)
        return data


def export_bundle(root, admitted_archive, admitted_source_sha256, commit):
    """Bind the existing admission outputs to exact producer bytes and receipts.

    The workflow's immediately preceding admission owns source/provenance
    validation. This exporter does not repeat it or provision any native tools.
    Hashes are checked while streaming the selected files into a temporary tar;
    a failed check never publishes an incomplete bundle.
    """
    root = Path(root).resolve(strict=True)
    destination = root / OUTPUT_DIRECTORY
    _require(not destination.exists(), "refuse an existing export directory")
    _require(_digest(commit, 40), "invalid admitted source commit")
    _require(_digest(admitted_source_sha256), "invalid admitted source digest")
    runtime_path = root / "build/worker-prior-gate/runtime/libpy_runtime_pcc_py.a"
    compiler_path = root / "build/platform-qualification/shared/pcc1"
    _require(Path(admitted_archive) == runtime_path, "runtime path differs from admission")
    stage_path = root / "build/worker-prior-gate/stage1.json"
    shared_path = root / "build/platform-qualification/shared/receipt.json"
    stage_bytes = stage_path.read_bytes()
    shared_bytes = shared_path.read_bytes()
    stage = json.loads(stage_bytes)
    shared = json.loads(shared_bytes)
    _require(stage.get("phase") == "stage1" and stage.get("status") == "PASS",
             "export requires a PASS Stage1 receipt")
    for receipt in (stage, shared):
        source = receipt["source"]
        _require(source.get("clean_commit") is True
                 and source.get("commit") == commit
                 and source.get("source_sha256") == admitted_source_sha256,
                 "source differs from the admitted clean checkout")
    runtime = stage["runtime"]
    _require(runtime["path"] == str(runtime_path)
             and runtime["target"] == TARGET and runtime["config"] == CONFIG,
             "runtime identity differs from the admitted Linux x86-64 configuration")
    compiler = stage["compiler"]
    _require(compiler["path"] == str(compiler_path)
             and shared["compiler_path"] == str(compiler_path)
             and shared["target"] == TARGET,
             "compiler path or target differs from the Stage1 producer")
    stages = shared["stages"]
    _require(len(stages) == 1 and stages[0]["stage"] == 1
             and stages[0]["sha256"] == compiler["sha256"],
             "compiler hash differs between the original Stage1 receipts")
    members = [(compiler_path, compiler["sha256"])]
    _require(shared["runtime"]["path"] == str(runtime_path),
             "shared receipt runtime path differs")
    for suffix, key in (("", "sha256"), (".provenance.json", "provenance_sha256"),
                        (".capi_syms", "capi_inventory_sha256")):
        expected = runtime["hashes"][suffix]
        _require(shared["runtime"][key] == expected, "runtime receipt hash differs: " + suffix)
        members.append((Path(str(runtime_path) + suffix), expected))
    members.extend((
        (stage_path, hashlib.sha256(stage_bytes).hexdigest()),
        (shared_path, hashlib.sha256(shared_bytes).hexdigest()),
    ))
    # The real platform consumer locates these beside the relocated receipt.
    # Preserve original commands/absolute paths inside that receipt unchanged.
    canary = stages[0]["canary"]
    for name, key in (("canary.py", "source_sha256"), ("canary1", "executable_sha256"),
                      ("canary1-run.log", "output_sha256")):
        members.append((shared_path.parent / name, canary[key]))
    # These logs/samples have no producer digest. Freeze their current bytes in
    # the export manifest, then recheck while packing. Empty logs are legitimate.
    for name in ("stage1.log", "stage1.resources.jsonl", "canary1-compile.log",
                 "canary1-compile.resources.jsonl", "canary1-run.resources.jsonl"):
        members.append((shared_path.parent / name, None))
    files = {}
    for path, expected in members:
        name = path.relative_to(root).as_posix()
        info = path.lstat()
        _require(stat.S_ISREG(info.st_mode) and path.resolve(strict=True) == path,
                 "export input must be a regular, non-symlink file: " + str(path))
        _require(info.st_size > 0 or expected is None, "empty producer-bound file: " + name)
        if expected is None:
            with path.open("rb") as stream:
                expected = hashlib.file_digest(stream, "sha256").hexdigest()
        _require(_digest(expected), "invalid export hash: " + name)
        if path == compiler_path:
            _require(info.st_mode & 0o111, "Stage1 compiler is not executable")
        files[name] = {"sha256": expected, "size": info.st_size,
                       "mode": stat.S_IMODE(info.st_mode), "source_path": str(path)}
    manifest = {
        "schema": "pcc.ci-stage1-bundle.v1",
        "claim": "Stage1 identity only; not five-GC, prior, wheel or fixed-point qualification",
        "source": {"commit": commit, "source_sha256": admitted_source_sha256, "clean_commit": True},
        "target": TARGET, "runtime_config": CONFIG, "files": files,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="stage1-export.", dir=root / "build") as temporary:
        work = Path(temporary)
        with tarfile.open(str(work / ARCHIVE_NAME), "w|gz", compresslevel=1) as archive:
            for path, _expected in members:
                name = path.relative_to(root).as_posix()
                with path.open("rb") as stream:
                    info = os.fstat(stream.fileno())
                    _require(info.st_size == files[name]["size"]
                             and stat.S_IMODE(info.st_mode) == files[name]["mode"],
                             "export input changed before reading: " + name)
                    header = tarfile.TarInfo(name)
                    header.size = info.st_size
                    header.mode = stat.S_IMODE(info.st_mode)
                    reader = _HashingReader(stream)
                    archive.addfile(header, reader)
                    _require(not stream.read(1) and reader.digest.hexdigest() == files[name]["sha256"],
                             "export input hash or size mismatch: " + name)
            header = tarfile.TarInfo("manifest.json")
            header.size = len(manifest_bytes)
            header.mode = 0o644
            archive.addfile(header, io.BytesIO(manifest_bytes))
        (work / "manifest.json").write_bytes(manifest_bytes)
        _require(not destination.exists(), "export directory appeared during packing")
        work.rename(destination)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-archive", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    args = parser.parse_args(argv)
    _require(sys.platform.startswith("linux") and platform.machine().lower() in ("x86_64", "amd64"),
             "this export is restricted to Linux x86-64")
    manifest = export_bundle(ROOT, args.runtime_archive, args.source_sha256, os.environ["GITHUB_SHA"])
    print(json.dumps({"directory": OUTPUT_DIRECTORY, "claim": manifest["claim"],
                      "input_bytes": sum(row["size"] for row in manifest["files"].values()),
                      "archive_bytes": (ROOT / OUTPUT_DIRECTORY / ARCHIVE_NAME).stat().st_size}))


if __name__ == "__main__":
    main()
