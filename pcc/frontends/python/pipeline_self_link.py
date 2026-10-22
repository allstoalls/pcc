"""Pure contracts for the Python pipeline's pcc-owned self-link route.

This module deliberately does not import :mod:`pcc.backend`. It owns mode
selection, capability validation and deterministic command construction;
the execution layer dispatches to the registered owned platform linker.
"""

from __future__ import annotations

from typing import Optional


class SelfLinkContractError(ValueError):
    """The requested self-link mode is unsupported or internally inconsistent."""


def _join_arguments(arguments: tuple[str, ...]) -> str:
    text = ""
    index = 0
    while index < len(arguments):
        if index > 0:
            text += " "
        text += str(arguments[index])
        index += 1
    return text


def default_self_link_mode(platform: str, machine: Optional[str]) -> str:
    """Select the owned linker for each supported host target, else fail."""
    host = str(platform or "")
    normalized = str(machine or "").strip().lower()
    if host.startswith("linux") and normalized in ("x86_64", "amd64", "arm64", "aarch64"):
        return "pcc"
    if host == "win32" and normalized in ("x86_64", "amd64", "x64"):
        return "pcc"
    if host == "darwin" and normalized in ("arm64", "aarch64"):
        return "pcc"
    if not normalized and (host in ("darwin", "win32") or host.startswith("linux")):
        name = "Darwin" if host == "darwin" else "Windows" if host == "win32" else "Linux"
        raise SelfLinkContractError(
            "cannot identify the " + name + " host architecture for self-link "
            "selection; set PCC_SELF_LINK=cc or PCC_SELF_LINK=pcc explicitly"
        )
    raise SelfLinkContractError(
        "pcc self-link default does not support host target "
        + repr(host + "/" + normalized)
        + "; PCC_SELF_LINK=cc is an explicit CPython-host reference oracle"
    )


def normalize_self_link_mode(raw: Optional[str], *, default_mode: str,
                             implementation: str = "cpython") -> str:
    """Normalize an explicit selector without converting typos into fallback."""
    value = str(raw or "").strip().lower()
    if not value:
        if default_mode != "pcc":
            raise SelfLinkContractError(
                "invalid default self-link mode " + repr(default_mode)
                + "; the production default must be 'pcc'"
            )
        return default_mode
    if value not in ("cc", "pcc"):
        raise SelfLinkContractError(
            "invalid PCC_SELF_LINK value "
            + repr(value)
            + "; expected 'cc' or 'pcc'"
        )
    if value == "cc" and implementation != "cpython":
        raise SelfLinkContractError(
            "PCC_SELF_LINK=cc is a CPython-host reference oracle; "
            "native pcc1 requires the pcc-owned linker"
        )
    return value


def validate_pcc_self_link_surface(
    selected_mode: str,
    *,
    extra_link_args: tuple[str, ...] = (),
    needs_libpython: bool = False,
    needs_native_extension_exports: bool = False,
) -> None:
    """Reject unsupported pcc-link semantics before any object is emitted."""
    if selected_mode != "pcc":
        return
    if extra_link_args:
        raise SelfLinkContractError(
            "pcc self-link mode does not support link arguments: "
            + _join_arguments(extra_link_args)
        )
    if needs_libpython:
        raise SelfLinkContractError(
            "pcc self-link mode does not support the libpython link surface"
        )
    if needs_native_extension_exports:
        raise SelfLinkContractError(
            "pcc self-link mode does not support native-extension export anchors"
        )


def build_pcc_link_command(
    *,
    host_python: str,
    driver: str,
    output: str,
    asm_path: Optional[str],
    internal_asm_inputs: tuple[str, ...],
    runtime_archive: Optional[str],
    extra_link_inputs: tuple[str, ...],
    internal_input_flag: str = "--asm",
    internal_input_manifest: Optional[str] = None,
    semantic_layout_policy: Optional[str] = None,
    profile_json: Optional[str] = None,
) -> list[str]:
    """Build the complete owned-link subprocess command in stable input order."""
    if internal_input_flag not in ("--asm", "--native-object", "--object"):
        raise SelfLinkContractError(
            "invalid owned-link internal input flag "
            + repr(internal_input_flag)
        )
    if internal_input_manifest and (asm_path or internal_asm_inputs):
        raise SelfLinkContractError(
            "ordered internal-input manifest cannot be combined with direct "
            "internal inputs"
        )
    command = [str(host_python), str(driver), "--out", str(output)]
    if profile_json:
        command.extend(["--profile-json", str(profile_json)])
    temporary_suffix = ".tmp"
    output_text = str(output)
    if output_text.endswith(temporary_suffix) and len(output_text) > len(
        temporary_suffix
    ):
        command.extend([
            "--previous-output",
            output_text[:-len(temporary_suffix)],
        ])
    if asm_path:
        command.extend([internal_input_flag, str(asm_path)])
    for internal_asm in internal_asm_inputs:
        command.extend([internal_input_flag, str(internal_asm)])
    if internal_input_manifest:
        command.extend(
            ["--internal-input-manifest", str(internal_input_manifest)]
        )
    if runtime_archive is not None:
        command.extend(["--archive", str(runtime_archive)])
    for extra in extra_link_inputs:
        command.extend(["--object", str(extra)])
    if semantic_layout_policy:
        command.extend(
            ["--semantic-layout-policy", str(semantic_layout_policy)]
        )
    return command
