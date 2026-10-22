"""Final-link ownership gates for the production pcc-Python runtime."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.tools.runtime_archive_provenance import (
    PRODUCTION_POLICY,
    verify_runtime_archive_manifest,
)
from tests.python.test_pcc_native_extension_loader import _compile_demo_extension


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPO_ROOT / "pcc" / "runtime"
DARWIN_BOUNDARY = REPO_ROOT / "tests" / "darwin_python_runtime_libsystem_boundary.json"


def _darwin_link_map_owners(text: str) -> dict[str, str]:
    owners: dict[str, str] = {}
    object_section = text.split("# Object files:\n", 1)[1].split("# Sections:\n", 1)[0]
    for line in object_section.splitlines():
        match = re.match(r"\[\s*(\d+)\]\s+(.+)$", line)
        if match:
            owners[match.group(1)] = match.group(2)
    return owners


def _darwin_link_map_symbols(text: str, owner_ids: set[str]) -> set[str]:
    symbols: set[str] = set()
    symbol_table = text.split("# Symbols:\n", 1)[1].split(
        "# Dead Stripped Symbols:\n", 1
    )[0]
    for line in symbol_table.splitlines():
        match = re.search(r"\[\s*(\d+)\]\s+(\S+)$", line)
        if match and match.group(1) in owner_ids:
            symbols.add(match.group(2))
    return symbols


def _classify_darwin_link_map_owners(
    text: str,
    runtime_archive: Path,
    *,
    system_library_roots: tuple[Path, ...] = (),
) -> tuple[set[str], set[str]]:
    owners = _darwin_link_map_owners(text)
    runtime_archive = runtime_archive.resolve()
    resolved_system_roots = tuple(root.resolve() for root in system_library_roots)
    expected_generated_objects = {
        "self_backend_native_0.o",
        "self_backend_native_1.o",
    }
    seen_generated_objects: set[str] = set()
    runtime_members: set[str] = set()
    system_owner_ids: set[str] = set()
    seen_linker_synthesized = False
    unclassified: list[str] = []
    for owner_id, owner_path in owners.items():
        if owner_path == "linker synthesized":
            seen_linker_synthesized = True
            continue
        archive_match = re.fullmatch(r"(.+[.]a)\(([^()]+)\)", owner_path)
        if archive_match:
            if Path(archive_match.group(1)).resolve() == runtime_archive:
                runtime_members.add(archive_match.group(2))
            else:
                unclassified.append(owner_path)
            continue
        if owner_path.endswith(".tbd"):
            resolved_owner = Path(owner_path).resolve()
            if any(
                resolved_owner == root or resolved_owner.is_relative_to(root)
                for root in resolved_system_roots
            ):
                system_owner_ids.add(owner_id)
            else:
                unclassified.append(owner_path)
            continue
        basename = Path(owner_path).name
        if basename in expected_generated_objects and owner_path.endswith(
            "/" + basename
        ):
            seen_generated_objects.add(basename)
            continue
        unclassified.append(owner_path)
    if unclassified:
        raise ValueError("unclassified link-map owners: " + repr(unclassified))
    if not seen_linker_synthesized:
        raise ValueError("link map lacks the linker-synthesized owner")
    if seen_generated_objects != expected_generated_objects:
        raise ValueError(
            "generated self-backend object inventory mismatch: "
            + repr(seen_generated_objects)
        )
    if not runtime_members:
        raise ValueError("link map contains no production runtime members")
    live_symbol_table = text.split("# Symbols:\n", 1)
    if len(live_symbol_table) == 2:
        live_symbol_table = live_symbol_table[1].split(
            "# Dead Stripped Symbols:\n", 1
        )[0]
        unknown_symbol_owner_ids = {
            match.group(1)
            for line in live_symbol_table.splitlines()
            if (match := re.search(r"\[\s*(\d+)\]\s+\S+$", line))
            and match.group(1) not in owners
        }
        if unknown_symbol_owner_ids:
            raise ValueError(
                "live symbols reference unknown link-map owners: "
                + repr(sorted(unknown_symbol_owner_ids))
            )
    return runtime_members, system_owner_ids


def test_darwin_symbol_parser_excludes_dead_stripped_symbols() -> None:
    text = (
        "# Symbols:\n"
        "0x1 0x1 [ 7] _live.got\n"
        "# Dead Stripped Symbols:\n"
        "0x2 0x1 [ 7] _dead.got\n"
    )
    assert _darwin_link_map_symbols(text, {"7"}) == {"_live.got"}


@pytest.mark.parametrize(
    "foreign_owner",
    [
        "/tmp/loose_py_runtime.o",
        "/different/libpy_runtime_pcc_py.a(member.o)",
    ],
)
def test_darwin_owner_classifier_rejects_unattributed_link_inputs(
    tmp_path: Path,
    foreign_owner: str,
) -> None:
    archive = tmp_path / "libpy_runtime_pcc_py.a"
    text = (
        "# Object files:\n"
        "[ 0] linker synthesized\n"
        "[ 1] /tmp/self_backend_native_0.o\n"
        "[ 2] /tmp/self_backend_native_1.o\n"
        f"[ 3] {archive}(member.o)\n"
        f"[ 4] {foreign_owner}\n"
        "# Sections:\n"
    )
    with pytest.raises(ValueError, match="unclassified link-map owners"):
        _classify_darwin_link_map_owners(text, archive)


def test_darwin_owner_classifier_rejects_foreign_tbd_and_unknown_symbol_owner(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "libpy_runtime_pcc_py.a"
    sdk_lib = tmp_path / "MacOSX.sdk" / "usr" / "lib"
    sdk_lib.mkdir(parents=True)
    foreign_tbd = tmp_path / "foreign" / "libsystem_kernel.tbd"
    foreign_tbd.parent.mkdir()
    foreign_tbd.write_text("---\n", encoding="utf-8")
    base = (
        "# Object files:\n"
        "[ 0] linker synthesized\n"
        "[ 1] /tmp/self_backend_native_0.o\n"
        "[ 2] /tmp/self_backend_native_1.o\n"
        f"[ 3] {archive}(member.o)\n"
    )
    foreign_text = base + f"[ 4] {foreign_tbd}\n# Sections:\n"
    with pytest.raises(ValueError, match="unclassified link-map owners"):
        _classify_darwin_link_map_owners(
            foreign_text,
            archive,
            system_library_roots=(sdk_lib,),
        )

    unknown_symbol_text = (
        base
        + "# Sections:\n"
        + "# Symbols:\n"
        + "0x1 0x1 [ 99] _unowned_symbol\n"
    )
    with pytest.raises(ValueError, match="unknown link-map owners"):
        _classify_darwin_link_map_owners(unknown_symbol_text, archive)


# Darwin link-map boundary
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "darwin"))
def test_darwin_python_runtime_final_link_has_only_documented_libsystem_boundary(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    site = _compile_demo_extension(tmp_path)
    source = tmp_path / "runtime_closure.py"
    source.write_text(
        "import demo\n"
        "import gc\n"
        "class Box:\n"
        "    value: int\n"
        "box = Box()\n"
        "box.value = 40\n"
        "values = [box, 2]\n"
        "payload = {'answer': demo.add(values[0].value, values[1])}\n"
        "print(payload['answer'])\n"
        "print(gc.collect() >= 0)\n",
        encoding="utf-8",
    )
    executable = tmp_path / "runtime_closure"
    link_map = tmp_path / "runtime_closure.map"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_RUNTIME_CC"] = "pcc"
    env["PCC_RUNTIME_ARCHIVE"] = str(pcc_runtime_archive)
    # This gate consumes the system linker's attributed map as its Darwin
    # ownership oracle.  The pcc-owned Mach-O linker deliberately rejects
    # arbitrary ``-Wl`` arguments, including ``-map``; select the cc link
    # boundary explicitly instead of depending on the host's default mode.
    env["PCC_SELF_LINK"] = "cc"
    compiled = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend=self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            "--link-arg=-Wl,-map," + str(link_map),
            str(source),
            "-o",
            str(executable),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    run = subprocess.run(
        [str(executable)],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines() == ["42", "True"]

    manifest = verify_runtime_archive_manifest(
        pcc_runtime_archive,
        # The fixture is an immutable content-addressed snapshot.  Verify the
        # archive against the sources copied into that same snapshot rather
        # than the concurrently mutable repository tree.
        runtime_root=pcc_runtime_archive.parent,
    )
    assert manifest["policy"] == PRODUCTION_POLICY
    manifested_members = {record["member"] for record in manifest["members"]}

    ownership = link_map.read_text(encoding="utf-8")
    owners = _darwin_link_map_owners(ownership)
    sdk_probe = subprocess.run(
        ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert sdk_probe.returncode == 0, sdk_probe.stderr
    sdk_usr_lib = Path(sdk_probe.stdout.strip()) / "usr" / "lib"
    runtime_members, system_owner_ids = _classify_darwin_link_map_owners(
        ownership,
        pcc_runtime_archive,
        system_library_roots=(sdk_usr_lib,),
    )
    assert runtime_members <= manifested_members
    system_owner_names = {Path(owners[owner]).name for owner in system_owner_ids}
    system_symbols_by_owner = {
        Path(owners[owner]).name: sorted(
            _darwin_link_map_symbols(ownership, {owner})
        )
        for owner in system_owner_ids
    }

    dependency_probe = subprocess.run(
        ["otool", "-L", str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert dependency_probe.returncode == 0, dependency_probe.stderr
    dependencies = [
        line.strip().split(" ", 1)[0]
        for line in dependency_probe.stdout.splitlines()[1:]
        if line.strip()
    ]

    expected = json.loads(DARWIN_BOUNDARY.read_text(encoding="utf-8"))
    assert expected["mode"] == "darwin-arm64-self-no-libpython-pcc-runtime"
    assert dependencies == expected["dylibs"]
    assert system_owner_names == set(expected["link_map_system_owners"])
    assert system_symbols_by_owner == expected["link_map_system_symbols_by_owner"]
