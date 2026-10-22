"""Host-only failure receipts; all compilation and launch calls are mocked."""

import hashlib
import json
from pathlib import Path
import struct

import pytest

from scripts import bootstrap_platform as platform
from tests.python.test_platform_bootstrap_shared import _fake_platform


def _pe_image(magic=0x20B, *, section_count=2, directory_count=16):
    data = bytearray(8192)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    data[128:132] = b"PE\0\0"
    optional_size = 240 if magic == 0x20B else 224
    struct.pack_into("<HH", data, 132, 0x8664 if magic == 0x20B else 0x14C, section_count)
    struct.pack_into("<HH", data, 148, optional_size, 0x22)
    optional = 152
    struct.pack_into("<H", data, optional, magic)
    struct.pack_into("<I", data, optional + 16, 0x1000)
    struct.pack_into("<Q" if magic == 0x20B else "<I", data,
                     optional + (24 if magic == 0x20B else 28),
                     0x140000000 if magic == 0x20B else 0x400000)
    struct.pack_into("<II", data, optional + 32, 4096, 512)
    struct.pack_into("<II", data, optional + 56, 0x80000000, 1024)
    struct.pack_into("<HH", data, optional + 68, 3, 0x160)
    directories = optional + (112 if magic == 0x20B else 96)
    struct.pack_into("<I", data, directories - 4, directory_count)
    struct.pack_into("<II", data, directories + 8, 0x2000, 40)
    sections = optional + optional_size
    for index in range(min(section_count, 40)):
        section = sections + index * 40
        data[section:section + 8] = b".text\0\0\0"
        struct.pack_into("<IIII", data, section + 8, 0x1234, 0x1000 + index * 4096, 512, 1024)
        struct.pack_into("<I", data, section + 36, 0x60000020)
    return bytes(data)


@pytest.mark.parametrize("magic", [0x10B, 0x20B], ids=["pe32", "pe32-plus"])
def test_image_receipt_retains_bounded_pe_fields_without_a_loader_verdict(tmp_path, magic):
    path = tmp_path / "compiler.exe"
    data = _pe_image(magic)
    path.write_bytes(data)
    receipt = platform._image_header_receipt(path)

    assert receipt["file_size"] == len(data)
    assert receipt["captured_bytes"] == receipt["capture_limit_bytes"] == 4096
    assert receipt["captured_prefix_sha256"] == hashlib.sha256(data[:4096]).hexdigest()
    assert receipt["format"] == "PE"
    assert receipt["pe_offset"] == 128
    assert receipt["machine"] == (0x8664 if magic == 0x20B else 0x14C)
    assert receipt["optional_magic"] == magic
    assert receipt["image_base"] == (0x140000000 if magic == 0x20B else 0x400000)
    assert receipt["entry_point_rva"] == 4096
    assert receipt["size_of_image"] == 0x80000000
    assert receipt["size_of_headers"] == 1024
    assert receipt["section_alignment"] == 4096
    assert receipt["file_alignment"] == 512
    assert receipt["subsystem"] == 3
    assert receipt["dll_characteristics"] == 0x160
    assert receipt["directories"][1] == {"index": 1, "rva": 0x2000, "size": 40}
    assert receipt["sections"][0] == {
        "index": 0, "name": ".text", "virtual_size": 0x1234, "rva": 4096,
        "raw_size": 512, "raw_offset": 1024, "characteristics": 0x60000020,
    }
    assert not receipt["directories_truncated"] and not receipt["sections_truncated"]
    assert "inspection_error" not in receipt
    assert not ({"valid", "qualified", "loader_accepted", "data"} & receipt.keys())


def test_image_receipt_never_reads_beyond_its_capture_budget(tmp_path, monkeypatch):
    path = tmp_path / "compiler.exe"
    path.write_bytes(_pe_image(section_count=65535, directory_count=0xFFFFFFFF))
    original_open = Path.open
    reads = []

    class BoundedReader:
        def __enter__(self):
            self.stream = original_open(path, "rb")
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            reads.append(size)
            assert 0 <= size <= 4096
            return self.stream.read(size)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", lambda *args, **kwargs: BoundedReader())
        receipt = platform._image_header_receipt(path)
    assert reads == [4096]
    assert receipt["section_count"] == 65535
    assert len(receipt["sections"]) == 32 and receipt["sections_truncated"]
    assert receipt["directory_count"] == 0xFFFFFFFF
    assert len(receipt["directories"]) == 16 and receipt["directories_truncated"]


@pytest.mark.parametrize("fault", ["short-dos", "outside-prefix", "signature", "optional-size", "optional-magic"])
def test_malformed_image_headers_become_evidence_not_new_failures(tmp_path, fault):
    data = bytearray(_pe_image())
    if fault == "short-dos":
        data = data[:32]
    elif fault == "outside-prefix":
        struct.pack_into("<I", data, 60, 5000)
    elif fault == "signature":
        data[128:132] = b"NOPE"
    elif fault == "optional-size":
        struct.pack_into("<H", data, 148, 16)
    else:
        struct.pack_into("<H", data, 152, 0xFFFF)
    path = tmp_path / "compiler.exe"
    path.write_bytes(data)
    receipt = platform._image_header_receipt(path)
    assert receipt["inspection_error"]
    assert receipt["file_size"] == len(data)
    assert receipt["captured_bytes"] <= 4096


@pytest.mark.parametrize("data, expected", [(b"\x7fELF headers", "ELF"), (b"", "unknown")])
def test_non_pe_image_receipts_keep_identity_without_parsing(tmp_path, data, expected):
    path = tmp_path / "compiler"
    path.write_bytes(data)
    receipt = platform._image_header_receipt(path)
    assert receipt["format"] == expected
    assert receipt["file_size"] == len(data)
    assert receipt["captured_prefix_sha256"] == hashlib.sha256(data).hexdigest()


def test_unreadable_image_is_reported_without_raising(tmp_path):
    receipt = platform._image_header_receipt(tmp_path / "missing.exe")
    assert receipt["inspection_error"]


def _launch_failure(tmp_path, monkeypatch, error, *, stage=1):
    _fake_platform(tmp_path, monkeypatch)
    original_run = platform.run
    image = _pe_image()

    def run(command, **options):
        log = Path(options["log_path"])
        if log.name == f"canary{stage}-compile.log":
            raise error
        result = original_run(command, **options)
        if "--ir-scaffold=on" in command:
            Path(command[command.index("-o") + 1]).write_bytes(image)
        return result

    monkeypatch.setattr(platform, "run", run)
    return image


@pytest.mark.parametrize(
    "error", [OSError(193, "not a valid Win32 application"), RuntimeError("launch failed"),
              KeyboardInterrupt(), SystemExit(143)],
    ids=["create-process", "ordinary-error", "keyboard-interrupt", "system-exit"],
)
def test_first_launch_failure_retains_compilation_but_never_qualifies(tmp_path, monkeypatch, error):
    image = _launch_failure(tmp_path, monkeypatch, error)
    out = tmp_path / "chain"
    with pytest.raises(type(error)) as raised:
        platform.run_chain("0", out, stage_limit=1, lock_held=True)
    assert raised.value is error
    failure = json.loads((out / "failure.json").read_text())
    assert failure["error"] == str(error)
    assert failure["stages"] == []
    pending = failure["incomplete_stage"]
    assert pending["stage"] == 1 and pending["canary_status"] == "pending"
    assert pending["sha256"] == hashlib.sha256(image).hexdigest()
    assert pending["seconds"] == 1 and pending["peak_tree_rss"] == 10
    assert pending["image"]["file_size"] == len(image)
    assert pending["image"]["size_of_image"] == 0x80000000
    assert "canary" not in pending
    assert not (out / "receipt.json").exists()
    assert not (out / "progress.json").exists()


def test_later_launch_failure_keeps_only_prior_validated_stages(tmp_path, monkeypatch):
    error = OSError(193, "not a valid Win32 application")
    _launch_failure(tmp_path, monkeypatch, error, stage=2)
    out = tmp_path / "chain"
    with pytest.raises(OSError) as raised:
        platform.run_chain("0", out, stage_limit=3, lock_held=True)
    assert raised.value is error
    failure = json.loads((out / "failure.json").read_text())
    assert [item["stage"] for item in failure["stages"]] == [1]
    assert failure["stages"] == json.loads((out / "progress.json").read_text())
    assert failure["incomplete_stage"]["stage"] == 2
    assert failure["incomplete_stage"]["canary_status"] == "pending"
    assert not (out / "receipt.json").exists()


def test_reused_compiler_does_not_borrow_another_gc_canary(tmp_path, monkeypatch):
    _fake_platform(tmp_path, monkeypatch)
    shared = platform.run_chain("0", tmp_path / "shared", stage_limit=1, lock_held=True)
    original_canary = shared["stages"][0]["canary"]
    error = OSError(193, "not a valid Win32 application")

    def fail_launch(*args, **kwargs):
        raise error

    monkeypatch.setattr(platform, "run", fail_launch)
    out = tmp_path / "gc1"
    with pytest.raises(OSError) as raised:
        platform.run_chain("1", out, stage_limit=1, shared=shared, lock_held=True)
    assert raised.value is error
    pending = json.loads((out / "failure.json").read_text())["incomplete_stage"]
    assert pending["reused"] and pending["canary_status"] == "pending"
    assert "canary" not in pending
    assert shared["stages"][0]["canary"] == original_canary
    assert pending["sha256"] == shared["stages"][0]["sha256"]


def test_later_compile_failure_has_no_stale_incomplete_stage(tmp_path, monkeypatch):
    _fake_platform(tmp_path, monkeypatch)
    original_run = platform.run
    error = RuntimeError("second compilation failed")

    def run(command, **options):
        if Path(options["log_path"]).name == "stage2.log":
            raise error
        return original_run(command, **options)

    monkeypatch.setattr(platform, "run", run)
    out = tmp_path / "chain"
    with pytest.raises(RuntimeError) as raised:
        platform.run_chain("0", out, lock_held=True)
    assert raised.value is error
    failure = json.loads((out / "failure.json").read_text())
    assert [item["stage"] for item in failure["stages"]] == [1]
    assert failure["incomplete_stage"] is None


def test_optional_header_read_failure_does_not_mask_launch_error(tmp_path, monkeypatch):
    error = OSError(193, "not a valid Win32 application")
    _launch_failure(tmp_path, monkeypatch, error)
    original_open = Path.open
    image_reads = 0

    def open_image(path, *args, **kwargs):
        nonlocal image_reads
        mode = args[0] if args else kwargs.get("mode", "r")
        if path.name in ("pcc1", "pcc1.exe") and mode == "rb":
            image_reads += 1
            if image_reads == 2:
                raise PermissionError("image metadata read denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_image)
    out = tmp_path / "chain"
    with pytest.raises(OSError) as raised:
        platform.run_chain("0", out, stage_limit=1, lock_held=True)
    assert raised.value is error
    pending = json.loads((out / "failure.json").read_text())["incomplete_stage"]
    assert pending["image"]["inspection_error"] == "image metadata read denied"


def test_early_failure_does_not_reuse_a_previous_incomplete_stage(tmp_path, monkeypatch):
    _fake_platform(tmp_path, monkeypatch)
    out = tmp_path / "chain"
    out.mkdir()
    (out / "failure.json").write_text(json.dumps({"incomplete_stage": {"stage": 99}}))
    error = RuntimeError("runtime preparation failed")

    def fail_runtime(*args, **kwargs):
        raise error

    monkeypatch.setattr(platform, "run", fail_runtime)
    with pytest.raises(RuntimeError) as raised:
        platform.run_chain("0", out, lock_held=True)
    assert raised.value is error
    failure = json.loads((out / "failure.json").read_text())
    assert failure["stages"] == [] and failure["incomplete_stage"] is None


def test_success_keeps_existing_stage_qualification_contract(tmp_path, monkeypatch):
    _fake_platform(tmp_path, monkeypatch)
    out = tmp_path / "chain"
    receipt = platform.run_chain("0", out, lock_held=True)
    assert receipt["qualified"] and receipt["fixed_point"]
    assert [item["stage"] for item in receipt["stages"]] == [1, 2, 3]
    assert all("canary" in item for item in receipt["stages"])
    assert json.loads((out / "progress.json").read_text()) == receipt["stages"]
    assert "incomplete_stage" not in receipt
    assert not (out / "failure.json").exists()
