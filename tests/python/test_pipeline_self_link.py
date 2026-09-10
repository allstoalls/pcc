"""Pure self-link contracts extracted from the Python pipeline facade."""

from __future__ import annotations

import pytest

from pcc.py_frontend import pipeline_self_link


def test_default_and_explicit_self_link_modes_are_fail_closed():
    assert pipeline_self_link.default_self_link_mode("darwin", "arm64") == "pcc"
    assert pipeline_self_link.default_self_link_mode("darwin", "aarch64") == "pcc"
    assert pipeline_self_link.default_self_link_mode("darwin", "x86_64") == "cc"
    assert pipeline_self_link.default_self_link_mode("linux", None) == "cc"
    with pytest.raises(
        pipeline_self_link.SelfLinkContractError,
        match="cannot identify",
    ):
        pipeline_self_link.default_self_link_mode("darwin", "")

    assert pipeline_self_link.normalize_self_link_mode(
        " PCC ", default_mode="cc"
    ) == "pcc"
    assert pipeline_self_link.normalize_self_link_mode(
        "", default_mode="pcc"
    ) == "pcc"
    with pytest.raises(
        pipeline_self_link.SelfLinkContractError,
        match="invalid PCC_SELF_LINK",
    ):
        pipeline_self_link.normalize_self_link_mode(
            "system", default_mode="cc"
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"extra_link_args": ("-Wl,-map,out.map",)}, "link arguments"),
        ({"needs_libpython": True}, "libpython link surface"),
        (
            {"needs_native_extension_exports": True},
            "native-extension export anchors",
        ),
    ],
)
def test_pcc_surface_rejects_each_unowned_semantic_before_link(kwargs, message):
    with pytest.raises(
        pipeline_self_link.SelfLinkContractError,
        match=message,
    ):
        pipeline_self_link.validate_pcc_self_link_surface("pcc", **kwargs)
    pipeline_self_link.validate_pcc_self_link_surface("cc", **kwargs)


def test_darwin_arm64_default_link_mode_cannot_serve_native_extension_exports():
    """Pin the owned-link capability gap the native-extension tests opt out of.

    On Darwin AArch64 the default mode is ``pcc``, and loading a CPython
    extension needs C-API export anchors in the executable. Until the owned
    Mach-O link synthesizes them, every native-extension test declares
    ``PCC_SELF_LINK=cc``. This pins the gap at the production default so a
    silent fallback to the system linker cannot quietly replace the owned
    path: the owned mode must refuse the export surface before any object is
    emitted.
    """
    default_mode = pipeline_self_link.default_self_link_mode("darwin", "arm64")

    assert default_mode == "pcc"
    with pytest.raises(
        pipeline_self_link.SelfLinkContractError,
        match="native-extension export anchors",
    ):
        pipeline_self_link.validate_pcc_self_link_surface(
            default_mode,
            needs_native_extension_exports=True,
        )


def test_owned_link_command_preserves_input_kinds_and_stable_patch_base():
    command = pipeline_self_link.build_pcc_link_command(
        host_python="/repo/.venv/bin/python3",
        driver="/repo/scripts/pcc_link_macho.py",
        output="/tmp/compiler.tmp",
        asm_path="/tmp/main.s",
        internal_asm_inputs=("/tmp/a.s", "/tmp/b.s"),
        runtime_archive="/tmp/runtime.a",
        extra_link_inputs=("/tmp/bridge.o", "/tmp/resource.o"),
    )

    assert command[:4] == [
        "/repo/.venv/bin/python3",
        "/repo/scripts/pcc_link_macho.py",
        "--out",
        "/tmp/compiler.tmp",
    ]
    assert command[command.index("--previous-output") + 1] == "/tmp/compiler"
    assert [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--asm"
    ] == ["/tmp/main.s", "/tmp/a.s", "/tmp/b.s"]
    assert command[command.index("--archive") + 1] == "/tmp/runtime.a"
    assert [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--object"
    ] == ["/tmp/bridge.o", "/tmp/resource.o"]


def test_non_temporary_output_does_not_invent_incremental_base():
    command = pipeline_self_link.build_pcc_link_command(
        host_python="python3",
        driver="driver.py",
        output="program",
        asm_path=None,
        internal_asm_inputs=("input.s",),
        runtime_archive=None,
        extra_link_inputs=(),
    )
    assert "--previous-output" not in command


def test_owned_elf_link_command_labels_internal_inputs_as_objects():
    command = pipeline_self_link.build_pcc_link_command(
        host_python="python3",
        driver="scripts/pcc_link_elf.py",
        output="program",
        asm_path=None,
        internal_asm_inputs=("first.o", "second.o"),
        runtime_archive="runtime.a",
        extra_link_inputs=("extra.o",),
        internal_input_flag="--object",
    )
    assert "--asm" not in command
    assert [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--object"
    ] == ["first.o", "second.o", "extra.o"]


def test_owned_macho_link_command_labels_packed_native_inputs_and_profile():
    command = pipeline_self_link.build_pcc_link_command(
        host_python="python3",
        driver="scripts/pcc_link_macho.py",
        output="program.tmp",
        asm_path=None,
        internal_asm_inputs=("first.pco", "second.pco"),
        runtime_archive="runtime.a",
        extra_link_inputs=(),
        internal_input_flag="--native-object",
        profile_json="link-profile.json",
    )
    assert "--asm" not in command
    assert [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--native-object"
    ] == ["first.pco", "second.pco"]
    assert command[command.index("--profile-json") + 1] == "link-profile.json"


def test_owned_macho_link_command_accepts_ordered_internal_manifest():
    command = pipeline_self_link.build_pcc_link_command(
        host_python="python3",
        driver="scripts/pcc_link_macho.py",
        output="program.tmp",
        asm_path=None,
        internal_asm_inputs=(),
        runtime_archive="runtime.a",
        extra_link_inputs=(),
        internal_input_manifest="ordered-inputs.txt",
    )

    assert command[command.index("--internal-input-manifest") + 1] == (
        "ordered-inputs.txt"
    )
    assert "--asm" not in command
    assert "--native-object" not in command
