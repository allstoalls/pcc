"""Pure host contract tests; no compiler, native runtime, or PCC package import.

Load the standalone stdlib-only helper directly. Fake kernels deliberately
reject instruction projection, and the fake encoder emits only a synthetic
codec-sized payload. These tests establish handoff checks, not codec/emitter
correctness or any native/bootstrap/performance claim.
"""

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch


_SOURCE = (Path(__file__).resolve().parents[2]
           / "pcc/frontends/python/pipeline_indexed_handoff.py")
_SPEC = importlib.util.spec_from_file_location("indexed_handoff_host_test", _SOURCE)
handoff = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(handoff)
_FRONTEND_TOKEN = "a" * 64
_BACKEND_TOKEN = "b" * 64


class Kernel:
    def __init__(self, count):
        for position, field in enumerate(handoff._ARENA_FIELDS):
            setattr(self, field, range(count + position))

    @property
    def instructions(self):
        raise AssertionError("instruction projection must not be used")


class Function:
    def __init__(self, count):
        self.indexed_kernel = Kernel(count)

    @property
    def instructions(self):
        raise AssertionError("instruction projection must not be used")


def module(function_sizes=(1, 3), globals_count=2, target="x86_64-unknown-linux-gnu"):
    return types.SimpleNamespace(
        triple=target,
        functions=tuple(Function(size) for size in function_sizes),
        globals_=tuple(range(globals_count)),
    )


def encode(path, direct):
    header = json.dumps({"schema": handoff.CODEC_SCHEMA, "triple": direct.triple}).encode()
    count = sum(len(getattr(function.indexed_kernel, field))
                for function in direct.functions for field in handoff._ARENA_FIELDS)
    Path(path).write_bytes(handoff._MAGIC + str(len(header)).encode() + b"\n"
                           + header + b"\0" * (8 * count))


class HandoffContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ir = self.root / "ir"
        self.ir.mkdir()
        self.manifest = self.root / "worker_0.manifest"
        self.result = self.root / "worker_0.tsv"
        self.sidecar = self.ir / "module_0.direct.pidx"
        self.output = self.ir / "module_0.direct.pco"
        self.request = Path(str(self.sidecar) + ".request.json")
        self.seal = Path(str(self.sidecar) + ".seal.json")
        self.receipt = Path(str(self.output) + ".receipt.json")
        self.manifest.write_text("\n".join([
            "pcc.frontends.python.codegen_worker.v4", str(self.result), str(self.ir),
            str(self.root / "exports.json"), "codegen", str(self.root / "ast"),
            "sample", "off", "direct", "0", "0", "1",
            "0\tsample\t" + str(self.root / "sample.py"), "1", "0", "",
        ]))
        self.fields = {
            "schema": handoff.REQUEST_SCHEMA,
            "manifest_path": str(self.manifest),
            "manifest_sha256": hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
            "source_identity": "|".join(["c" * 64, "d" * 64]),
            "index": 0, "module": "sample", "target": "x86_64-unknown-linux-gnu",
            "passes": ["mem2reg"], "sidecar_path": str(self.sidecar),
            "output_path": str(self.output), "seal_path": str(self.seal),
            "receipt_path": str(self.receipt),
        }
        self.direct = module()
        self.environment = patch.dict(os.environ, {
            handoff.RESOURCE_TOKEN_ENV: _FRONTEND_TOKEN,
            handoff.ENV_REQUEST: str(self.request), handoff.ENV_SEAL: "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        handoff.write_request(str(self.request), self.fields)
        self.task = {
            "handoff_request": str(self.request),
            "handoff_source_identity": self.fields["source_identity"],
            "class": "private|host|collector0|indexed-backend", "depends_on": 0,
            "inputs": [], "estimate_bytes": 0, "source_identity": "",
        }

    def symlink(self, path, target, target_is_directory=False):
        try:
            path.symlink_to(target, target_is_directory=target_is_directory)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows account lacks symlink privilege")
            raise

    def publish(self, timing=False):
        result = handoff.publish_handoff(
            str(self.request), str(self.manifest), 0, "sample", self.direct,
            ["mem2reg"], False, True, encode,
        )
        self.assertEqual(result, str(self.sidecar))
        parts = ["OK", "0", "sample", "0", "1", "0", str(self.ir / "module_0.ll")]
        if timing:
            parts.extend(["1", "2", "3"])
        parts.extend(["PIDX", str(self.sidecar)])
        self.result.write_text("\t".join(parts) + "\n")
        return json.loads(self.seal.read_text())

    def prepare(self, timing=False):
        self.publish(timing)
        handoff.prepare_handoff_task(self.task, _FRONTEND_TOKEN)
        os.environ[handoff.ENV_SEAL] = self.task["handoff_seal_sha256"]
        os.environ[handoff.RESOURCE_TOKEN_ENV] = _BACKEND_TOKEN

    def publish_result(self):
        self.output.write_bytes(b"fake packed object")
        handoff.publish_handoff_result(str(self.request))

    def rewrite(self, path, changes):
        value = json.loads(path.read_text())
        value.update(changes)
        path.write_text(json.dumps(value))

    def test_actual_shape_fixed_22_arenas_without_projection(self):
        seal = self.publish()
        self.assertEqual(len(handoff._ARENA_FIELDS), 22)
        self.assertEqual(seal["shape"][1:3], [2, 2])
        self.assertEqual(len(seal["shape"]), 47)
        for index in range(22):
            self.assertEqual(seal["shape"][3 + 2 * index:5 + 2 * index],
                             [4 + 2 * index, 3 + index])
        self.assertEqual(seal["pidx_sha256"], hashlib.sha256(self.sidecar.read_bytes()).hexdigest())
        self.assertEqual(seal["pidx_size"], self.sidecar.stat().st_size)

    def test_timing_and_no_timing_result_rows_and_idempotent_prepare(self):
        for timing in (False, True):
            with self.subTest(timing=timing):
                self.publish(timing)
                handoff.prepare_handoff_task(self.task, _FRONTEND_TOKEN)
                before = dict(self.task)
                handoff.prepare_handoff_task(self.task, _FRONTEND_TOKEN)
                self.assertEqual(self.task, before)
                self.assertEqual(self.task["estimate_bytes"], 0)
                self.assertIn("indexed-handoff", self.task["class"])
                self.assertEqual(len(self.task["inputs"]), 47)

    def test_successful_retirement_preserves_request_receipt_and_output(self):
        self.prepare()
        request, seal = handoff.validate_handoff_input(
            str(self.request), str(self.sidecar), str(self.output), "PCO")
        self.assertEqual(request, self.fields)
        self.assertEqual(seal["frontend_token"], _FRONTEND_TOKEN)
        handoff.validate_handoff_module(str(self.request), self.direct)
        self.publish_result()
        handoff.retire_handoff_task(self.task, _BACKEND_TOKEN)
        self.assertFalse(self.sidecar.exists())
        self.assertFalse(self.seal.exists())
        self.assertTrue(self.request.exists())
        self.assertTrue(self.receipt.exists())
        self.assertTrue(self.output.exists())

    def test_missing_or_invalid_seal_rejected(self):
        with self.assertRaises((ValueError, OSError)):
            handoff.prepare_handoff_task(self.task, _FRONTEND_TOKEN)
        self.publish()
        self.seal.write_text("{}")
        with self.assertRaises(ValueError):
            handoff.prepare_handoff_task(self.task, _FRONTEND_TOKEN)
        self.assertTrue(self.sidecar.exists())
        self.assertNotIn("input_ready", self.task)

    def test_stale_frontend_token_rejected(self):
        self.publish()
        with self.assertRaisesRegex(ValueError, "stale frontend"):
            handoff.prepare_handoff_task(self.task, "e" * 64)
        self.assertNotIn("input_ready", self.task)

    def test_stale_backend_token_retains_inputs(self):
        self.prepare()
        self.publish_result()
        with self.assertRaisesRegex(ValueError, "receipt binding/token"):
            handoff.retire_handoff_task(self.task, "e" * 64)
        self.assertTrue(self.sidecar.exists())
        self.assertTrue(self.seal.exists())

    def test_missing_receipt_and_output_mismatch_retain_inputs(self):
        self.prepare()
        with self.assertRaises((ValueError, OSError)):
            handoff.retire_handoff_task(self.task, _BACKEND_TOKEN)
        self.publish_result()
        self.output.write_bytes(b"changed output")
        with self.assertRaisesRegex(ValueError, "output digest/size"):
            handoff.retire_handoff_task(self.task, _BACKEND_TOKEN)
        self.assertTrue(self.sidecar.exists())
        self.assertTrue(self.seal.exists())

    def test_cancelled_backend_reset_preserves_original_input(self):
        self.prepare()
        original = self.sidecar.read_bytes()
        seal = self.seal.read_bytes()
        self.publish_result()
        Path(str(self.output) + ".tmp").write_bytes(b"partial")
        handoff.reset_handoff_output(self.task)
        self.assertEqual(self.sidecar.read_bytes(), original)
        self.assertEqual(self.seal.read_bytes(), seal)
        self.assertFalse(self.output.exists())
        self.assertFalse(Path(str(self.output) + ".tmp").exists())
        self.assertFalse(self.receipt.exists())
        # A retry can consume the exact original PIDX and use a fresh token.
        os.environ[handoff.RESOURCE_TOKEN_ENV] = "f" * 64
        handoff.validate_handoff_input(str(self.request), str(self.sidecar), str(self.output), "PCO")
        self.publish_result()
        handoff.retire_handoff_task(self.task, "f" * 64)

    def test_reset_revalidates_inputs_before_removing_any_output(self):
        self.prepare()
        self.output.write_bytes(b"failure evidence")
        self.sidecar.write_bytes(b"changed input")
        with self.assertRaises(ValueError):
            handoff.reset_handoff_output(self.task)
        self.assertEqual(self.output.read_bytes(), b"failure evidence")
        self.assertTrue(self.seal.exists())

    def test_changed_manifest_rejected(self):
        self.publish()
        self.manifest.write_text(self.manifest.read_text() + "garbage\n")
        with self.assertRaisesRegex(ValueError, "manifest digest"):
            handoff.prepare_handoff_task(self.task, _FRONTEND_TOKEN)

    def test_producer_target_passes_flags_and_owner_rejected(self):
        arguments = [str(self.request), str(self.manifest), 0, "sample",
                     self.direct, ["mem2reg"], False, True, encode]
        for position, value in ((1, str(self.root / "other.manifest")),
                                (2, True), (3, "other"),
                                (4, module(target="aarch64-apple-darwin")),
                                (5, []), (6, True), (7, 1)):
            with self.subTest(position=position):
                bad = list(arguments)
                bad[position] = value
                with self.assertRaises(ValueError):
                    handoff.publish_handoff(*bad)
        self.assertFalse(self.sidecar.exists())

    def test_request_schema_unknown_fields_types_and_paths_rejected(self):
        changes = [
            {"schema": "future"}, {"extra": 1}, {"index": True}, {"passes": "mem2reg"},
            {"target": "unknown-unknown-unknown"}, {"manifest_sha256": "bad"},
            {"sidecar_path": str(self.root / "outside.pidx")},
            {"output_path": str(self.ir / "other.pco")},
            {"seal_path": str(self.ir / "other.seal")},
            {"receipt_path": str(self.ir / "other.receipt")},
        ]
        original = self.request.read_bytes()
        for change in changes:
            with self.subTest(change=change):
                value = dict(self.fields)
                value.update(change)
                self.request.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    handoff.publish_handoff(str(self.request), str(self.manifest),
                                            0, "sample", self.direct,
                                            ["mem2reg"], False, True, encode)
                self.request.write_bytes(original)

    def test_request_cannot_overwrite_existing_request(self):
        original = self.request.read_bytes()
        with self.assertRaises(FileExistsError):
            handoff.write_request(str(self.request), self.fields)
        self.assertEqual(self.request.read_bytes(), original)

    def test_exclusive_json_temporary_collision_fails_closed(self):
        destination = self.ir / "atomic-request.json"
        temporary = Path(str(destination) + ".tmp")
        temporary.write_bytes(b"another or interrupted publisher")
        with self.assertRaises(FileExistsError):
            handoff._atomic_json(str(destination), {"fixture": True}, exclusive=True)
        self.assertEqual(temporary.read_bytes(), b"another or interrupted publisher")
        self.assertFalse(destination.exists())

    def test_exclusive_json_rechecks_destination_before_replace(self):
        destination = self.ir / "atomic-request.json"
        temporary = Path(str(destination) + ".tmp")

        class WritingProxy:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def write(self, data):
                count = self.stream.write(data)
                destination.write_bytes(b"existing request must survive")
                return count

            def flush(self):
                self.stream.flush()

        def opening(path, mode):
            self.assertEqual(path, str(temporary))
            self.assertEqual(mode, "xb")
            return WritingProxy(open(path, mode))

        with patch.object(handoff, "open", side_effect=opening, create=True):
            with self.assertRaises(FileExistsError):
                handoff._atomic_json(str(destination), {"fixture": True}, exclusive=True)
        self.assertEqual(destination.read_bytes(), b"existing request must survive")
        self.assertFalse(temporary.exists())

    def test_failed_atomic_json_replace_leaves_no_partial_destination(self):
        destination = self.ir / "atomic-request.json"
        temporary = Path(str(destination) + ".tmp")
        with patch.object(handoff.os, "replace", side_effect=OSError("injected replace failure")):
            with self.assertRaises(OSError):
                handoff._atomic_json(str(destination), {"fixture": True}, exclusive=True)
        self.assertFalse(destination.exists())
        self.assertFalse(temporary.exists())

    def test_nonexclusive_receipt_retry_reuses_regular_temp_and_rejects_symlink(self):
        self.prepare()
        self.output.write_bytes(b"fake packed object")
        temporary = Path(str(self.receipt) + ".tmp")
        temporary.write_bytes(b"interrupted receipt from retired backend")
        handoff.publish_handoff_result(str(self.request))
        self.assertFalse(temporary.exists())
        original = self.receipt.read_bytes()
        receipt = json.loads(original)
        self.assertEqual(receipt["backend_token"], _BACKEND_TOKEN)
        self.assertEqual(receipt["output_sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        outside = self.root / "preserved-receipt-target.bin"
        outside.write_bytes(b"preserve outside receipt target")
        self.symlink(temporary, outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            handoff.publish_handoff_result(str(self.request))
        self.assertEqual(outside.read_bytes(), b"preserve outside receipt target")
        self.assertTrue(temporary.is_symlink())
        self.assertEqual(self.receipt.read_bytes(), original)
        self.assertTrue(self.sidecar.exists())
        self.assertTrue(self.seal.exists())

    def test_duplicate_and_oversized_json_rejected(self):
        self.request.write_text('{"schema":"x","schema":"y"}')
        with self.assertRaises(ValueError):
            handoff.prepare_handoff_task(self.task, _FRONTEND_TOKEN)
        self.request.write_bytes(b" " * (handoff._MAX_JSON_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "size limit"):
            handoff.prepare_handoff_task(self.task, _FRONTEND_TOKEN)

    def test_seal_target_pass_schema_shape_and_flags_mismatch(self):
        self.publish()
        original = self.seal.read_bytes()
        for change in ({"target": "other"}, {"passes": []}, {"schema": "other"},
                       {"codec_schema": "other"}, {"shape": [1]},
                       {"needs_libpython": True}, {"needs_native_exports": 1},
                       {"index": False}, {"source_identity": "different"}):
            with self.subTest(change=change):
                self.rewrite(self.seal, change)
                with self.assertRaises(ValueError):
                    handoff.prepare_handoff_task(self.task, _FRONTEND_TOKEN)
                self.seal.write_bytes(original)

    def test_result_module_index_pidX_path_flags_and_extra_rows_rejected(self):
        self.publish()
        original = self.result.read_text()
        parts = original.strip().split("\t")
        for position, value in ((0, "ERR"), (1, "1"), (2, "other"),
                                (3, "1"), (4, "0"), (5, "-1"),
                                (6, str(self.root / "outside.ll")),
                                (7, "PCO"), (8, str(self.output))):
            with self.subTest(position=position):
                changed = list(parts)
                changed[position] = value
                self.result.write_text("\t".join(changed) + "\n")
                with self.assertRaises(ValueError):
                    handoff.prepare_handoff_task(self.task, _FRONTEND_TOKEN)
        self.result.write_text(original + original)
        with self.assertRaises(ValueError):
            handoff.prepare_handoff_task(self.task, _FRONTEND_TOKEN)

    def test_backend_environment_and_artifact_binding(self):
        self.prepare()
        for sidecar, output, kind in ((str(self.sidecar), str(self.output), "ASM"),
                                     (str(self.output), str(self.output), "PCO"),
                                     (str(self.sidecar), str(self.sidecar), "PCO")):
            with self.subTest(kind=kind, sidecar=sidecar):
                with self.assertRaises(ValueError):
                    handoff.validate_handoff_input(str(self.request), sidecar, output, kind)
        os.environ[handoff.ENV_SEAL] = "e" * 64
        with self.assertRaisesRegex(ValueError, "seal digest"):
            handoff.validate_handoff_input(str(self.request), str(self.sidecar), str(self.output), "PCO")

    def test_decoded_target_and_shape_mismatch(self):
        self.prepare()
        for case, direct in (
            ("target", module(target="aarch64-apple-darwin")),
            ("arena-shape", module((1, 4))),
            ("function-count", module((1,))),
            ("global-count", module(globals_count=1)),
        ):
            # pytest-xdist serializes subTest parameters, not just their repr.
            with self.subTest(case=case):
                with self.assertRaisesRegex(ValueError, "target/shape"):
                    handoff.validate_handoff_module(str(self.request), direct)

    def test_same_totals_but_different_arena_maxima_rejected(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, "target/shape"):
            handoff.validate_handoff_module(str(self.request), module((2, 2)))

    def test_zero_function_shape(self):
        self.direct = module((), globals_count=0)
        seal = self.publish()
        self.assertEqual(seal["shape"][1:], [0] * 46)
        handoff.prepare_handoff_task(self.task, _FRONTEND_TOKEN)

    def test_symlink_output_cannot_be_reset_or_retired(self):
        self.prepare()
        outside = self.root / "keep.bin"
        outside.write_bytes(b"preserve")
        self.symlink(self.output, outside)
        with self.assertRaises(ValueError):
            handoff.reset_handoff_output(self.task)
        self.assertEqual(outside.read_bytes(), b"preserve")
        self.assertTrue(self.sidecar.exists())

    def test_stale_fixed_producer_temporary_is_reused_without_orphan(self):
        temporary = Path(str(self.sidecar) + ".tmp")
        temporary.write_bytes(b"partial payload from cancelled frontend")
        seen = []

        def checking_encode(path, direct):
            seen.append(path)
            self.assertEqual(path, str(temporary))
            self.assertFalse(temporary.exists())
            encode(path, direct)

        handoff.publish_handoff(str(self.request), str(self.manifest), 0, "sample",
                                self.direct, ["mem2reg"], False, True, checking_encode)
        self.assertEqual(seen, [str(temporary)])
        self.assertTrue(self.sidecar.exists())
        self.assertTrue(self.seal.exists())
        self.assertFalse(temporary.exists())
        original = self.sidecar.read_bytes()
        temporary.write_bytes(b"another cancelled partial payload")
        handoff.publish_handoff(str(self.request), str(self.manifest), 0, "sample",
                                self.direct, ["mem2reg"], False, True, checking_encode)
        self.assertEqual(self.sidecar.read_bytes(), original)
        self.assertEqual(seen, [str(temporary), str(temporary)])
        self.assertFalse(temporary.exists())

    def test_producer_temporary_symlink_refuses_without_touching_target(self):
        temporary = Path(str(self.sidecar) + ".tmp")
        outside = self.root / "preserved.bin"
        outside.write_bytes(b"preserve outside target")
        self.symlink(temporary, outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            handoff.publish_handoff(str(self.request), str(self.manifest), 0, "sample",
                                    self.direct, ["mem2reg"], False, True, encode)
        self.assertEqual(outside.read_bytes(), b"preserve outside target")
        self.assertTrue(temporary.is_symlink())
        self.assertFalse(self.sidecar.exists())
        self.assertFalse(self.seal.exists())

    def test_temporary_root_parent_symlink_is_supported(self):
        # Model /var -> /private/var without accepting artifact-file links.
        real = self.root / "real"
        real.mkdir()
        alias = self.root / "alias"
        self.symlink(alias, real, target_is_directory=True)
        old_root = str(self.root)
        new_root = str(alias)
        manifest_text = self.manifest.read_text().replace(old_root, new_root)
        for name in ("ir", "manifest", "result", "sidecar", "output", "request", "seal", "receipt"):
            setattr(self, name, Path(str(getattr(self, name)).replace(old_root, new_root)))
        self.ir.mkdir()
        self.manifest.write_text(manifest_text)
        for key in ("manifest_path", "sidecar_path", "output_path", "seal_path", "receipt_path"):
            self.fields[key] = self.fields[key].replace(old_root, new_root)
        self.fields["manifest_sha256"] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.task["handoff_request"] = str(self.request)
        os.environ[handoff.ENV_REQUEST] = str(self.request)
        handoff.write_request(str(self.request), self.fields)
        self.prepare()
        handoff.validate_handoff_input(str(self.request), str(self.sidecar), str(self.output), "PCO")
        self.publish_result()
        handoff.retire_handoff_task(self.task, _BACKEND_TOKEN)
        self.assertTrue(self.output.exists())
        self.assertTrue(self.receipt.exists())
        self.assertFalse(self.sidecar.exists())

    def test_codec_arena_order_matches_current_source_without_import(self):
        codec = _SOURCE.parents[2] / "backend/self_backend_indexed_codec.py"
        if not codec.is_file():
            self.skipTest("isolated packet has no codec; integration source check required")
        tree = ast.parse(codec.read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "_ARENA_FIELDS"
                    for target in node.targets):
                fields = ast.literal_eval(node.value)
                self.assertEqual(tuple(field[1] for field in fields), handoff._ARENA_FIELDS)
                break
        else:
            self.fail("codec arena contract was not found")


if __name__ == "__main__":
    unittest.main()
