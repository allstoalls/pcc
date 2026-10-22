"""Clang linking and runtime C-API export/isolation policy."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from pcc.frontends.python.pipeline_modes import failed_process_detail
from pcc.driver.paths import join_strings


class NativeLinkError(RuntimeError):
    """The native link request failed its owned boundary."""


def native_extension_export_link_flags(
    needs_native_extension_exports: bool = False,
    *,
    platform: Optional[str] = None,
) -> list[str]:
    if not needs_native_extension_exports:
        return []
    selected = sys.platform if platform is None else str(platform)
    if selected == "darwin":
        return ["-Wl,-export_dynamic"]
    if selected.startswith("linux"):
        return ["-rdynamic"]
    return []


def capi_export_anchor_symbols(
    runtime_archive: str,
    *,
    archive_requires_provenance,
    archive_bundle_valid,
    host_python_command,
) -> list[str]:
    sidecar = runtime_archive + ".capi_syms"
    requires_verified_inventory = archive_requires_provenance(runtime_archive)
    if requires_verified_inventory and not archive_bundle_valid(runtime_archive):
        raise NativeLinkError(
            "pcc-Python runtime archive has an invalid C-API inventory bundle: "
            + sidecar
        )
    try:
        with open(sidecar, "r") as stream:
            inventory_text = stream.read()
        anchors: list[str] = []
        seen: set[str] = set()
        for line in inventory_text.splitlines():
            symbol = line.strip()
            if symbol and symbol not in seen:
                seen.add(symbol)
                anchors.append(symbol)
        if anchors:
            if requires_verified_inventory:
                canonical_inventory = join_strings(anchors, "\n") + "\n"
                if inventory_text != canonical_inventory:
                    raise NativeLinkError(
                        "pcc-Python runtime C-API inventory is not canonical: "
                        + sidecar
                    )
                if not archive_bundle_valid(runtime_archive):
                    raise NativeLinkError(
                        "pcc-Python runtime C-API inventory changed during link: "
                        + sidecar
                    )
            return anchors
    except NativeLinkError:
        raise
    except Exception as exc:
        if requires_verified_inventory:
            raise NativeLinkError(
                "cannot read verified pcc-Python runtime C-API inventory: "
                + sidecar
            ) from exc
    if requires_verified_inventory:
        raise NativeLinkError(
            "verified pcc-Python runtime C-API inventory is empty: " + sidecar
        )
    nm_code = (
        "import subprocess\n"
        "import sys\n"
        "try:\n"
        "    out = subprocess.check_output(['nm', '-g', sys.argv[1]], stderr=subprocess.STDOUT, text=True, timeout=120)\n"
        "except Exception:\n"
        "    raise SystemExit(1)\n"
        "sys.stdout.write(out)\n"
    )
    try:
        output = subprocess.check_output(
            [host_python_command(), "-c", nm_code, runtime_archive],
            text=True,
        )
    except Exception:
        return []
    anchors = []
    seen = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] == "U" or not parts[1].isupper():
            continue
        symbol = parts[2]
        bare = symbol[1:] if symbol.startswith("_") else symbol
        if not (bare.startswith("Py") or bare.startswith("_Py")):
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        anchors.append(symbol)
    return anchors


def libpython_capi_isolation_link_flags(
    runtime_archive: Optional[str],
    needs_libpython: bool,
    *,
    anchor_symbols,
    platform: Optional[str] = None,
) -> list[str]:
    if not needs_libpython or runtime_archive is None:
        return []
    selected = sys.platform if platform is None else str(platform)
    if selected == "darwin":
        symbols = anchor_symbols(runtime_archive)
        if not symbols:
            raise NativeLinkError(
                "libpython mode requires a non-empty runtime C-API symbol inventory"
            )
        return [f"-Wl,-unexported_symbol,{symbol}" for symbol in symbols]
    if selected.startswith("linux"):
        return ["-Wl,--exclude-libs," + os.path.basename(runtime_archive)]
    raise NativeLinkError(
        "libpython C-API symbol isolation is unsupported on " + selected
    )


def runtime_archive_link_args_for_native_extensions(
    runtime_archive: str,
    needs_native_extension_exports: bool = False,
    *,
    anchor_symbols,
    platform: Optional[str] = None,
) -> list[str]:
    if not needs_native_extension_exports:
        return [runtime_archive]
    anchors = anchor_symbols(runtime_archive)
    if not anchors:
        selected = sys.platform if platform is None else str(platform)
        if selected == "darwin":
            return ["-Wl,-u,_PyArg_ParseTuple", runtime_archive]
        if selected.startswith("linux"):
            return ["-Wl,-u,PyArg_ParseTuple", runtime_archive]
        return [runtime_archive]
    return [f"-Wl,-u,{symbol}" for symbol in anchors] + [runtime_archive]
