"""Shared, self-hostable pcc package-environment selection contract."""

import json
import os
import sys

from pcc.driver.python_target import PYTHON_TARGET_VERSION

PACKAGE_ENVIRONMENT_SCHEMA = "pcc.package-environment.v1"
PCC_NATIVE_ABI_VERSION = "pcc-native-v1"
DEFAULT_PYTHON_SEMANTIC_TARGET = PYTHON_TARGET_VERSION
DEFAULT_PACKAGE_ABI_MODE = "pcc-native"


def _path_list_sep(platform_name=None):
    name = sys.platform if platform_name is None else str(platform_name)
    return ";" if name.startswith("win") else ":"


def _absolute_path(value: str, home: str) -> str:
    text = str(value or "")
    if text == "~":
        text = home
    elif text.startswith("~/") or text.startswith("~\\"):
        text = os.path.join(home, text[2:])
    return os.path.abspath(text)


def _append_unique(values, value):
    text = str(value or "")
    if text and text not in values:
        values.append(text)


def _split_path_list(
    raw: str,
    home: str,
    platform_name: str | None = None,
) -> list[str]:
    values: list[str] = []
    for item in str(raw or "").split(_path_list_sep(platform_name)):
        item = item.strip()
        if item:
            _append_unique(values, _absolute_path(item, home))
    return values


def _default_target_triple(
    explicit: str,
    target_arch: str,
    platform_name: str | None = None,
    machine: str | None = None,
) -> str:
    explicit = str(explicit or "").strip()
    if explicit:
        return explicit
    platform_value = sys.platform if platform_name is None else str(platform_name)
    machine_value = "" if machine is None else str(machine)
    if not machine_value:
        machine_value = str(target_arch or "")
    if not machine_value:
        try:
            machine_value = str(os.uname().machine)
        except Exception:
            machine_value = "unknown"
    if platform_value.startswith("darwin"):
        return machine_value + "-apple-darwin"
    if platform_value.startswith("linux"):
        return machine_value + "-unknown-linux-gnu"
    if platform_value.startswith("win"):
        return machine_value + "-pc-windows-msvc"
    return machine_value + "-unknown-" + platform_value


def _compatibility_component(value):
    out = []
    for ch in str(value or ""):
        if ch.isalnum():
            out.append(ch.lower())
        else:
            if len(out) == 0 or out[-1] != "_":
                out.append("_")
    while len(out) > 0 and out[-1] == "_":
        out.pop()
    return "".join(out) or "unknown"


def _compatibility_tag(python_target, abi_version, target_triple, abi_mode):
    native_tag = "-".join(
        (
            _compatibility_component(abi_version),
            _compatibility_component(target_triple),
            _compatibility_component(abi_mode),
        )
    )
    if abi_mode == "pcc-native":
        return native_tag
    python_component = "py" + str(python_target or "").replace(".", "")
    return _compatibility_component(python_component) + "-" + native_tag


def _user_data_root(
    explicit: str,
    xdg_data_home: str,
    home: str,
) -> tuple[str, str]:
    explicit = str(explicit or "").strip()
    if explicit:
        return _absolute_path(explicit, home), "PCC_DATA_HOME"
    xdg = str(xdg_data_home or "").strip()
    if xdg:
        return os.path.join(_absolute_path(xdg, home), "pcc"), "XDG_DATA_HOME"
    home = str(home or "").strip()
    if not home:
        home = os.path.abspath(".")
    return os.path.join(_absolute_path(home, home), ".local", "share", "pcc"), "HOME"


def _default_package_cache_values(
    explicit: str,
    xdg_cache_home: str,
    home: str,
) -> str:
    explicit = str(explicit or "").strip()
    if explicit:
        return _absolute_path(explicit, home)
    xdg = str(xdg_cache_home or "").strip()
    if xdg:
        return os.path.join(_absolute_path(xdg, home), "pcc", "package-cache")
    home = str(home or "").strip()
    if not home:
        home = os.path.abspath(".")
    return os.path.join(_absolute_path(home, home), ".cache", "pcc", "package-cache")


def _default_package_cache_mapping(environ: dict[str, str]) -> str:
    return _default_package_cache_values(
        str(environ.get("PCC_PACKAGE_CACHE") or ""),
        str(environ.get("XDG_CACHE_HOME") or ""),
        str(environ.get("HOME") or ""),
    )


def default_package_cache(environ: dict[str, str] | None = None) -> str:
    if environ is not None:
        return _default_package_cache_mapping(environ)
    return _default_package_cache_values(
        str(os.environ.get("PCC_PACKAGE_CACHE") or ""),
        str(os.environ.get("XDG_CACHE_HOME") or ""),
        str(os.environ.get("HOME") or ""),
    )


def _resolve_package_environment_values(
    home: str,
    python_target: str,
    abi_version: str,
    abi_mode: str,
    explicit_target_triple: str,
    target_arch: str,
    explicit_environment: str,
    virtual_environment: str,
    pcc_data_home: str,
    xdg_data_home: str,
    package_site: str,
    package_cache: str,
    xdg_cache_home: str,
    target_triple: str | None,
    platform_name: str | None,
    machine: str | None,
) -> dict[str, object]:
    python_target = str(python_target or DEFAULT_PYTHON_SEMANTIC_TARGET)
    abi_version = str(abi_version or PCC_NATIVE_ABI_VERSION)
    abi_mode = str(abi_mode or DEFAULT_PACKAGE_ABI_MODE)
    triple = (
        _default_target_triple(
            explicit_target_triple,
            target_arch,
            platform_name,
            machine,
        )
        if target_triple is None
        else str(target_triple)
    )
    tag = _compatibility_tag(python_target, abi_version, triple, abi_mode)

    explicit_environment = str(explicit_environment or "").strip()
    virtual_environment = str(virtual_environment or "").strip()
    data_source = ""
    if explicit_environment:
        root = _absolute_path(explicit_environment, home)
        reason = "explicit-environment"
    elif virtual_environment:
        root = os.path.join(
            _absolute_path(virtual_environment, home),
            ".pcc",
            "environments",
            tag,
        )
        reason = "virtual-env"
    else:
        data_root, data_source = _user_data_root(
            pcc_data_home,
            xdg_data_home,
            home,
        )
        root = os.path.join(data_root, "environments", tag)

        reason = "user-data"

    selected_site = os.path.join(root, "site-packages")
    sites = _split_path_list(package_site, home, platform_name)
    _append_unique(sites, selected_site)
    return {
        "schema": PACKAGE_ENVIRONMENT_SCHEMA,
        "root": root,
        "selection_reason": reason,
        "compatibility_tag": tag,
        "selected_site_packages": selected_site,
        "package_sites": sites,
        "python_semantic_target": python_target,
        "pcc_native_abi": abi_version,
        "package_abi_mode": abi_mode,
        "target_triple": triple,
        "cache_root": _default_package_cache_values(
            package_cache,
            xdg_cache_home,
            home,
        ),
        "lock_provenance": None,
        "override_provenance": {
            "PCC_ENVIRONMENT": bool(explicit_environment),
            "PCC_PACKAGE_SITE": bool(str(package_site or "").strip()),
            "PCC_DATA_HOME": data_source == "PCC_DATA_HOME",
            "XDG_DATA_HOME": data_source == "XDG_DATA_HOME",
            "VIRTUAL_ENV": bool(virtual_environment),
        },
    }


def _resolve_package_environment_mapping(
    environ: dict[str, str],
    target_triple: str | None,
    platform_name: str | None,
    machine: str | None,
) -> dict[str, object]:
    return _resolve_package_environment_values(
        str(environ.get("HOME") or ""),
        str(environ.get("PCC_PACKAGE_TARGET_PYTHON") or ""),
        str(environ.get("PCC_NATIVE_ABI_VERSION") or ""),
        str(environ.get("PCC_PACKAGE_ABI_MODE") or ""),
        str(environ.get("PCC_TARGET_TRIPLE") or ""),
        str(environ.get("PCC_TARGET_ARCH") or ""),
        str(environ.get("PCC_ENVIRONMENT") or ""),
        str(environ.get("VIRTUAL_ENV") or ""),
        str(environ.get("PCC_DATA_HOME") or ""),
        str(environ.get("XDG_DATA_HOME") or ""),
        str(environ.get("PCC_PACKAGE_SITE") or ""),
        str(environ.get("PCC_PACKAGE_CACHE") or ""),
        str(environ.get("XDG_CACHE_HOME") or ""),
        target_triple,
        platform_name,
        machine,
    )


def _with_published_environment_state(report: dict[str, object]) -> dict[str, object]:
    manifest_path = os.path.join(str(report["root"]), "environment.json")
    if not os.path.isfile(manifest_path):
        return report
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            manifest = json.loads(stream.read())
    except (OSError, ValueError):
        return report
    if not isinstance(manifest, dict):
        return report
    if manifest.get("compatibility_tag") != report.get("compatibility_tag"):
        return report
    provenance = manifest.get("lock_provenance")
    if isinstance(provenance, dict):
        report["lock_provenance"] = provenance
    return report


def resolve_package_environment(
    environ: dict[str, str] | None = None,
    target_triple: str | None = None,
    platform_name: str | None = None,
    machine: str | None = None,
) -> dict[str, object]:
    if environ is not None:
        return _with_published_environment_state(
            _resolve_package_environment_mapping(
                environ,
                target_triple,
                platform_name,
                machine,
            )
        )
    return _with_published_environment_state(
        _resolve_package_environment_values(
            str(os.environ.get("HOME") or ""),
            str(os.environ.get("PCC_PACKAGE_TARGET_PYTHON") or ""),
            str(os.environ.get("PCC_NATIVE_ABI_VERSION") or ""),
            str(os.environ.get("PCC_PACKAGE_ABI_MODE") or ""),
            str(os.environ.get("PCC_TARGET_TRIPLE") or ""),
            str(os.environ.get("PCC_TARGET_ARCH") or ""),
            str(os.environ.get("PCC_ENVIRONMENT") or ""),
            str(os.environ.get("VIRTUAL_ENV") or ""),
            str(os.environ.get("PCC_DATA_HOME") or ""),
            str(os.environ.get("XDG_DATA_HOME") or ""),
            str(os.environ.get("PCC_PACKAGE_SITE") or ""),
            str(os.environ.get("PCC_PACKAGE_CACHE") or ""),
            str(os.environ.get("XDG_CACHE_HOME") or ""),
            target_triple,
            platform_name,
            machine,
        )
    )


def apply_locked_environment_resource_defaults() -> list[str]:
    """Publish conservative compiler defaults for a uv-locked package graph.

    A compiled package graph can retain substantially more frontend and
    self-backend worker memory than the compiler bootstrap graph. Apply the
    locked-environment defaults before compilation starts so every nested
    worker observes the same resource budget. User-provided non-empty values
    remain authoritative.
    """
    report = resolve_package_environment()
    if not isinstance(report.get("lock_provenance"), dict):
        return []
    applied = []
    if not str(os.environ.get("PCC_PY_FRONTEND_JOBS") or "").strip():
        os.environ["PCC_PY_FRONTEND_JOBS"] = "1"
        applied.append("PCC_PY_FRONTEND_JOBS")
    if not str(os.environ.get("PCC_SELF_BACKEND_JOBS") or "").strip():
        os.environ["PCC_SELF_BACKEND_JOBS"] = "1"
        applied.append("PCC_SELF_BACKEND_JOBS")
    return applied


def package_environment_fingerprint() -> str:
    """Cheap identity of every input the ``environ=None`` resolution reads.

    Callers may cache resolution results keyed on this string: two calls with
    equal fingerprints resolve identical ``package_sites`` (the
    environment.json merge only adds ``lock_provenance``; it never alters the
    site list).  Keep this list in exact sync with the os.environ reads in
    ``resolve_package_environment`` below.
    """
    return "\x1f".join(
        (
            str(os.environ.get("HOME") or ""),
            str(os.environ.get("PCC_PACKAGE_TARGET_PYTHON") or ""),
            str(os.environ.get("PCC_NATIVE_ABI_VERSION") or ""),
            str(os.environ.get("PCC_PACKAGE_ABI_MODE") or ""),
            str(os.environ.get("PCC_TARGET_TRIPLE") or ""),
            str(os.environ.get("PCC_TARGET_ARCH") or ""),
            str(os.environ.get("PCC_ENVIRONMENT") or ""),
            str(os.environ.get("VIRTUAL_ENV") or ""),
            str(os.environ.get("PCC_DATA_HOME") or ""),
            str(os.environ.get("XDG_DATA_HOME") or ""),
            str(os.environ.get("PCC_PACKAGE_SITE") or ""),
            str(os.environ.get("PCC_PACKAGE_CACHE") or ""),
            str(os.environ.get("XDG_CACHE_HOME") or ""),
        )
    )


def package_site_roots(
    environ: dict[str, str] | None = None,
    target_triple: str | None = None,
) -> list[str]:
    report = resolve_package_environment(environ, target_triple=target_triple)
    return list(report["package_sites"])


def default_package_site(
    environ: dict[str, str] | None = None,
    target_triple: str | None = None,
) -> str:
    report = resolve_package_environment(environ, target_triple=target_triple)
    return str(report["selected_site_packages"])


def _json_escape(value):
    out = ['"']
    for ch in str(value):
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 32:
            out.append("\\u" + format(code, "04x"))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _json_bool(value):
    return "true" if value else "false"


def _json_string_list(values):
    parts = []
    for value in values:
        parts.append(_json_escape(str(value)))
    return "[" + ",".join(parts) + "]"


def _json_lock_provenance(value):
    if not isinstance(value, dict):
        return "null"
    fields = [
        '"adapter_schema":' + _json_escape(str(value.get("adapter_schema") or "")),
        '"extras":' + _json_string_list(value.get("extras") or []),
        '"groups":' + _json_string_list(value.get("groups") or []),
        '"lock_path":' + _json_escape(str(value.get("lock_path") or "")),
        '"lock_sha256":' + _json_escape(str(value.get("lock_sha256") or "")),
        '"target_python":' + _json_escape(str(value.get("target_python") or "")),
        '"uv_lock_revision":' + str(int(value.get("uv_lock_revision") or 0)),
        '"uv_lock_version":' + str(int(value.get("uv_lock_version") or 0)),
    ]
    return "{" + ",".join(fields) + "}"


def environment_info_json(environ=None):
    report = resolve_package_environment(environ)
    provenance = report["override_provenance"]
    fields = [
        '"cache_root":' + _json_escape(str(report["cache_root"])),
        '"compatibility_tag":' + _json_escape(str(report["compatibility_tag"])),
        '"lock_provenance":' + _json_lock_provenance(report["lock_provenance"]),
        '"override_provenance":{'
        + '"PCC_DATA_HOME":'
        + _json_bool(provenance["PCC_DATA_HOME"])
        + ',"PCC_ENVIRONMENT":'
        + _json_bool(provenance["PCC_ENVIRONMENT"])
        + ',"PCC_PACKAGE_SITE":'
        + _json_bool(provenance["PCC_PACKAGE_SITE"])
        + ',"VIRTUAL_ENV":'
        + _json_bool(provenance["VIRTUAL_ENV"])
        + ',"XDG_DATA_HOME":'
        + _json_bool(provenance["XDG_DATA_HOME"])
        + "}",
        '"package_abi_mode":' + _json_escape(str(report["package_abi_mode"])),
        '"package_sites":' + _json_string_list(report["package_sites"]),
        '"pcc_native_abi":' + _json_escape(str(report["pcc_native_abi"])),
        '"python_semantic_target":'
        + _json_escape(str(report["python_semantic_target"])),
        '"root":' + _json_escape(str(report["root"])),
        '"schema":' + _json_escape(str(report["schema"])),
        '"selected_site_packages":'
        + _json_escape(str(report["selected_site_packages"])),
        '"selection_reason":' + _json_escape(str(report["selection_reason"])),
        '"target_triple":' + _json_escape(str(report["target_triple"])),
    ]
    return "{" + ",".join(fields) + "}\n"


def environment_info_text(environ=None):
    report = resolve_package_environment(environ)
    lines = [
        "pcc package environment",
        "  root: " + str(report["root"]),
        "  selection: " + str(report["selection_reason"]),
        "  compatibility tag: " + str(report["compatibility_tag"]),
        "  Python semantic target: " + str(report["python_semantic_target"]),
        "  pcc-native ABI: " + str(report["pcc_native_abi"]),
        "  package ABI mode: " + str(report["package_abi_mode"]),
        "  target triple: " + str(report["target_triple"]),
        "  cache root: " + str(report["cache_root"]),
        "  package sites:",
    ]
    for site in report["package_sites"]:
        lines.append("    - " + str(site))
    lock_provenance = report["lock_provenance"]
    if isinstance(lock_provenance, dict):
        lines.append("  uv lock: " + str(lock_provenance.get("lock_path") or ""))
        lines.append(
            "  uv lock sha256: " + str(lock_provenance.get("lock_sha256") or "")
        )
    return "\n".join(lines) + "\n"
