"""Self-host-safe package manifest, wheel-tag, and capability contract."""

PACKAGE_MANIFEST_SCHEMA = "pcc.package-manifest.v1"
PACKAGE_MANIFEST_SCHEMA_VERSION = 1
PCC_NATIVE_PYTHON_TAG = "pcc3"
PCC_NATIVE_ABI_TAG = "pcc_native"
PCC_CAPI_HEADERS = (
    "Python.h",
    "structmember.h",
    "pymem.h",
    "frameobject.h",
    "pythread.h",
    "pyerrors.h",
    "abstract.h",
    "datetime.h",
)

CAMPAIGN_PROFILES = {
    "numpy-core-l6": {
        "root_parts": ("numpy", "_core", "tests"),
        "default_area": "core",
        "area": "numpy-core",
        "description": (
            "NumPy L6 useful core-test subset profile. It selects stable "
            "numpy/_core/tests files that map to L6.2-L6.6 feature domains; "
            "it does not mark those tests passing."
        ),
        "selection_rule": ("fixed NumPy L6 core-test filename profile under numpy/_core/tests"),
        "files": {
            "test_multiarray.py": ("L6.2", "shape-strides-dtype"),
            "test_numeric.py": ("L6.2", "shape-strides-dtype"),
            "test_shape_base.py": ("L6.2", "shape-strides-dtype"),
            "test_dtype.py": ("L6.2", "shape-strides-dtype"),
            "test_array_coercion.py": ("L6.3", "scalar-coercion"),
            "test_scalarmath.py": ("L6.3", "scalar-types"),
            "test_indexing.py": ("L6.4", "indexing-slicing-broadcast"),
            "test_stride_tricks.py": ("L6.4", "indexing-slicing-broadcast"),
            "test_umath.py": ("L6.5", "ufunc-add-sub-mul-div"),
            "test_ufunc.py": ("L6.5", "ufunc-add-sub-mul-div"),
            "test_arrayprint.py": ("L6.6", "array-repr-print"),
        },
    }
}


def campaign_profile(name: str):
    return CAMPAIGN_PROFILES.get(name)


def pcc_native_wheel_tag(platform_tag: str) -> str:
    return PCC_NATIVE_PYTHON_TAG + "-" + PCC_NATIVE_ABI_TAG + "-" + platform_tag


def pcc_native_extension_suffix(platform_tag: str) -> str:
    """Return the explicit pcc-native extension ABI suffix for a platform."""

    return "." + pcc_native_wheel_tag(platform_tag) + ".so"


def _basename(path: str) -> str:
    last = -1
    i = 0
    while i < len(path):
        if path[i] == "/" or path[i] == "\\":
            last = i
        i += 1
    return path[last + 1 :]


def wheel_tag_fields(path: str):
    """Return ``[name, python, abi, platform]`` without host-only packaging."""
    text = str(path or "")
    base = _basename(text)
    stem = base[: len(base) - 4] if base.lower().endswith(".whl") else base
    parts = stem.split("-")
    if len(parts) >= 5 and text.lower().endswith(".whl"):
        return [parts[0], parts[len(parts) - 3], parts[len(parts) - 2], parts[-1]]
    return [parts[0] if len(parts) > 0 else stem, "", "", ""]


def wheel_tags(path: str) -> dict:
    fields = wheel_tag_fields(path)
    return {
        "python_tag": fields[1] or None,
        "abi_tag": fields[2] or None,
        "platform_tag": fields[3] or None,
    }


def directory_project_name(directory: str) -> str:
    """``name`` of a ``<name>-<version>`` directory, as sdists unpack.

    Only a last part that starts with a digit is a version, so a hyphenated
    name without one (``my-package``) stays whole.
    """
    stem, sep, version = directory.rpartition("-")
    if sep and stem and version[:1].isdigit():
        return stem
    return directory


def distribution_filename_fields(path: str):
    """Return [name, version] for an artifact, preserving project-name dashes."""
    base = _basename(path.rstrip("/\\"))
    lower = base.lower()
    if lower.endswith(".whl"):
        parts = base[:-4].split("-")
        if len(parts) >= 5:
            return [parts[0], parts[1]]
        return [base, "0"]
    archive = False
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip"):
        if lower.endswith(suffix):
            base = base[: -len(suffix)]
            archive = True
            break
    if archive:
        parts = base.split("-")
        index = len(parts) - 1
        while index > 0:
            part = parts[index]
            if part and "0" <= part[0] <= "9":
                return ["-".join(parts[:index]), "-".join(parts[index:])]
            index -= 1
    return [base or "package", "0"]


def validate_project_name(name: str) -> None:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if not name or name[0] in ".-_" or name[-1] in ".-_":
        raise ValueError("PCC-PKG-PROJECT-NAME-INVALID")
    for char in name:
        if char not in allowed:
            raise ValueError("PCC-PKG-PROJECT-NAME-INVALID")


def _package_toml_unsupported() -> None:
    raise ValueError("PCC-PKG-PROJECT-METADATA-UNSUPPORTED: configuration outside literal subset")


def _package_toml_statements(text: str) -> list[str]:
    """Bound statement boundaries before examining any configuration keys.

    This is an explicit literal subset, not a TOML validator. Composite and
    string values are opaque: their text can never introduce a table or
    assignment. Relevant consumers separately reject nonliteral values rather
    than approximating escape or multiline-string semantics.
    """
    statements: list[str] = []
    current: list[str] = []
    nesting = ""
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if (ord(char) < 32 and char not in "\t\r\n") or ord(char) == 127:
            _package_toml_unsupported()
        if quote:
            if quote[0] == '"' and char == "\\":
                if index + 1 == len(text):
                    _package_toml_unsupported()
                current.append(text[index : index + 2])
                index += 2
                continue
            if len(quote) == 1:
                if char in "\r\n":
                    _package_toml_unsupported()
                if char == quote:
                    quote = ""
            elif char == quote[0]:
                end = index
                while end < len(text) and text[end] == char:
                    end += 1
                if end - index >= 3:
                    if end - index > 5:
                        _package_toml_unsupported()
                    current.append(text[index:end])
                    index = end
                    quote = ""
                    continue
            current.append(char)
        elif char in "\"'":
            if text[index : index + 3] == char * 3:
                quote = char * 3
                current.append(quote)
                index += 3
                continue
            quote = char
            current.append(char)
        elif char == "#":
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        elif char in "[{":
            nesting += char
            current.append(char)
        elif char in "]}":
            expected = "[" if char == "]" else "{"
            if not nesting or nesting[-1] != expected:
                _package_toml_unsupported()
            nesting = nesting[:-1]
            current.append(char)
        elif char in "\r\n" and not nesting:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        index += 1
    if quote or nesting:
        _package_toml_unsupported()
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def _package_toml_key(text: str) -> str:
    """Encode key components without confusing quoted dots with separators."""
    bare = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    parts: list[str] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \t":
            index += 1
        if index == len(text):
            _package_toml_unsupported()
        char = text[index]
        start = index
        if char in "\"'":
            index += 1
            start = index
            while index < len(text) and text[index] != char:
                if text[index] == "\\" or ord(text[index]) < 32:
                    _package_toml_unsupported()
                index += 1
            if index == len(text):
                _package_toml_unsupported()
            part = text[start:index]
            index += 1
        else:
            while index < len(text) and text[index] in bare:
                index += 1
            part = text[start:index]
        if not part:
            _package_toml_unsupported()
        parts.append(part)
        while index < len(text) and text[index] in " \t":
            index += 1
        if index < len(text):
            if text[index] != ".":
                _package_toml_unsupported()
            index += 1
            if index == len(text):
                _package_toml_unsupported()
    if not parts:
        _package_toml_unsupported()
    return "\x1f".join(parts)


def _package_toml_entries(text: str) -> list[list[str]]:
    entries: list[list[str]] = []
    section = ""
    tables: list[str] = []
    assignments: list[str] = []
    opaque_arrays: list[str] = []
    opaque_section = False
    for statement in _package_toml_statements(text):
        if statement.startswith("["):
            if not statement.endswith("]") or "\n" in statement or "\r" in statement:
                _package_toml_unsupported()
            if statement.startswith("[["):
                if not statement.endswith("]]"):
                    _package_toml_unsupported()
                section = _package_toml_key(statement[2:-2])
                parts = section.split("\x1f")
                if parts[0] in ("project", "build-system"):
                    _package_toml_unsupported()
                if parts[0] == "tool" and (len(parts) == 1 or parts[1] == "hatch"):
                    _package_toml_unsupported()
                # Repeated tool configuration records cannot define a root
                # identity or Hatch/build-system ownership. Keep their child
                # tables opaque as well, including repeated child-table names.
                opaque_arrays.append(section)
                opaque_section = True
                continue
            section = _package_toml_key(statement[1:-1])
            opaque_section = False
            for array in opaque_arrays:
                if section == array or section.startswith(array + "\x1f"):
                    opaque_section = True
            if opaque_section:
                continue
            if section in tables:
                _package_toml_unsupported()
            for assigned in assignments:
                if (
                    section == assigned
                    or section.startswith(assigned + "\x1f")
                    or assigned.startswith(section + "\x1f")
                ):
                    _package_toml_unsupported()
            tables.append(section)
            entries.append([section, ""])
            continue
        if opaque_section:
            continue
        # An equals sign inside a quoted key is not the assignment boundary.
        quote = ""
        boundary = -1
        for index in range(len(statement)):
            char = statement[index]
            if quote:
                if char == quote:
                    quote = ""
            elif char in "\"'":
                quote = char
            elif char == "=":
                boundary = index
                break
        if boundary < 0:
            _package_toml_unsupported()
        key = _package_toml_key(statement[:boundary])
        path = section + "\x1f" + key if section else key
        value = statement[boundary + 1 :].strip()
        if not value or path in assignments or path in tables:
            _package_toml_unsupported()
        for assigned in assignments:
            if path.startswith(assigned + "\x1f") or assigned.startswith(path + "\x1f"):
                _package_toml_unsupported()
        assignments.append(path)
        entries.append([path, value])
    return entries


def _package_toml_literal(value: str) -> str:
    if len(value) < 2 or value[0] not in "\"'" or value[-1] != value[0]:
        _package_toml_unsupported()
    literal = value[1:-1]
    if not literal or value[0] in literal or "\\" in literal:
        _package_toml_unsupported()
    return literal


def literal_project_metadata_fields(text: str):
    """Read a literal project identity or explicitly reject unsupported syntax."""
    fields = ["", ""]
    saw_project = False
    for entry in _package_toml_entries(text):
        path = entry[0].split("\x1f")
        value = entry[1]
        if path[0] != "project":
            continue
        saw_project = True
        if value.startswith("{") or (len(path) == 1 and value):
            _package_toml_unsupported()
        if len(path) == 2 and path[1] in ("name", "version"):
            literal = _package_toml_literal(value)
            if path[1] == "name":
                validate_project_name(literal)
                fields[0] = literal
            else:
                fields[1] = literal
    if saw_project and not fields[0]:
        _package_toml_unsupported()
    return fields


def source_build_policy(text: str) -> str:
    """Classify build ownership; unsupported structure never earns an overlay.

    Inline configuration under build-system or Hatch is deliberately excluded.
    Unrelated tool settings may be opaque composites because they cannot define
    keys outside their enclosing assignment. The callers must reject a required
    hook if they cannot execute it; a False shortcut alone is not that policy.
    """
    backend = ""
    requires_hook = False
    for entry in _package_toml_entries(text):
        path = entry[0].split("\x1f")
        value = entry[1]
        if path[0] == "build-system":
            if (len(path) == 1 and value) or value.startswith("{"):
                _package_toml_unsupported()
            if len(path) == 2 and path[1] == "backend-path":
                requires_hook = True
            if len(path) == 2 and path[1] == "build-backend":
                backend = _package_toml_literal(value)
        if path[0] == "tool":
            if len(path) == 1 and value:
                _package_toml_unsupported()
            if len(path) > 1 and path[1] == "hatch":
                if value.startswith("{") or (len(path) == 2 and value):
                    _package_toml_unsupported()
                if "hooks" in path[2:]:
                    requires_hook = True
    if requires_hook:
        return "requires_build_hook"
    if backend == "hatchling.build":
        return "declarative_python_source"
    if backend and backend not in (
        "setuptools.build_meta",
        "setuptools.build_meta:__legacy__",
        "mesonpy",
    ):
        # A declared PEP 517 backend owns build hooks even for Python-only
        # projects. Absence of C files cannot prove that its output exists.
        return "requires_build_hook"
    return "unrecognized"


def declarative_python_source_build(text: str) -> bool:
    """Compatibility predicate; installers consume source_build_policy instead."""
    try:
        return source_build_policy(text) == "declarative_python_source"
    except ValueError:
        return False


def execution_mode(abi_mode: str) -> str:
    if abi_mode == "libpython" or abi_mode == "cpython-compat":
        return "cpython-compat"
    return "pcc-native"


def capability_profile(
    abi_mode: str,
    has_artifact_scan: bool,
    links_libpython: bool,
    uses_cpython_extension_abi: bool,
) -> dict:
    mode = execution_mode(abi_mode)
    native_claim = (
        mode == "pcc-native"
        and has_artifact_scan
        and not links_libpython
        and not uses_cpython_extension_abi
    )
    return {
        "execution_mode": mode,
        "native_package_claim": native_claim,
        "no_libpython_runtime": (
            mode == "pcc-native" and not links_libpython and not uses_cpython_extension_abi
        ),
    }
