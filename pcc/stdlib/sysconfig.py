"""Compiler-owned ``sysconfig`` metadata for native Python build tools.

CPython normally fills this module from the interpreter's generated Makefile
and ``pyconfig.h``.  A pcc-native executable has neither artifact and must not
spawn a host ``python3`` to borrow them.  This port therefore owns a compact,
target-oriented configuration map and the standard install-scheme expansion
API.  Target values can be supplied through generic ``PCC_*`` environment
variables; defaults describe the running pcc semantic target.

Requests for the physical CPython Makefile or ``pyconfig.h`` fail closed.
Returning a fabricated host file would make downstream build provenance
incorrect even if a configure probe happened to pass.
"""
from __future__ import annotations

import os
import platform as _platform
import sys

from pcc.driver.python_target import PYTHON_TARGET_VERSION_PARTS


_SCHEME_KEYS = (
    "stdlib",
    "platstdlib",
    "purelib",
    "platlib",
    "include",
    "scripts",
    "data",
)


_POSIX_PREFIX = {
    "stdlib": "{installed_base}/{platlibdir}/{implementation_lower}{py_version_short}{abi_thread}",
    "platstdlib": "{platbase}/{platlibdir}/{implementation_lower}{py_version_short}{abi_thread}",
    "purelib": "{base}/lib/{implementation_lower}{py_version_short}{abi_thread}/site-packages",
    "platlib": "{platbase}/{platlibdir}/{implementation_lower}{py_version_short}{abi_thread}/site-packages",
    "include": "{installed_base}/include/{implementation_lower}{py_version_short}{abiflags}",
    "platinclude": "{installed_platbase}/include/{implementation_lower}{py_version_short}{abiflags}",
    "scripts": "{base}/bin",
    "data": "{base}",
}


_NT = {
    "stdlib": "{installed_base}/Lib",
    "platstdlib": "{base}/Lib",
    "purelib": "{base}/Lib/site-packages",
    "platlib": "{base}/Lib/site-packages",
    "include": "{installed_base}/Include",
    "platinclude": "{installed_base}/Include",
    "scripts": "{base}/Scripts",
    "data": "{base}",
}


_INSTALL_SCHEMES = {
    # These templates are immutable implementation data.  Sharing the source
    # maps avoids relying on ``dict(mapping)`` during native module
    # initialization; callers always receive a fresh result below.
    "posix_prefix": _POSIX_PREFIX,
    "posix_venv": _POSIX_PREFIX,
    "venv": _POSIX_PREFIX,
    "posix_home": {
        "stdlib": "{installed_base}/lib/{implementation_lower}",
        "platstdlib": "{base}/lib/{implementation_lower}",
        "purelib": "{base}/lib/{implementation_lower}",
        "platlib": "{base}/lib/{implementation_lower}",
        "include": "{installed_base}/include/{implementation_lower}",
        "platinclude": "{installed_base}/include/{implementation_lower}",
        "scripts": "{base}/bin",
        "data": "{base}",
    },
    "nt": _NT,
    "nt_venv": _NT,
    "nt_user": {
        "stdlib": "{userbase}/{implementation}{py_version_nodot_plat}",
        "platstdlib": "{userbase}/{implementation}{py_version_nodot_plat}",
        "purelib": "{userbase}/{implementation}{py_version_nodot_plat}/site-packages",
        "platlib": "{userbase}/{implementation}{py_version_nodot_plat}/site-packages",
        "include": "{userbase}/{implementation}{py_version_nodot_plat}/Include",
        "scripts": "{userbase}/{implementation}{py_version_nodot_plat}/Scripts",
        "data": "{userbase}",
    },
    "posix_user": {
        "stdlib": "{userbase}/{platlibdir}/{implementation_lower}{py_version_short}{abi_thread}",
        "platstdlib": "{userbase}/{platlibdir}/{implementation_lower}{py_version_short}{abi_thread}",
        "purelib": "{userbase}/lib/{implementation_lower}{py_version_short}{abi_thread}/site-packages",
        "platlib": "{userbase}/lib/{implementation_lower}{py_version_short}{abi_thread}/site-packages",
        "include": "{userbase}/include/{implementation_lower}{py_version_short}{abi_thread}",
        "scripts": "{userbase}/bin",
        "data": "{userbase}",
    },
    "osx_framework_user": {
        "stdlib": "{userbase}/lib/{implementation_lower}",
        "platstdlib": "{userbase}/lib/{implementation_lower}",
        "purelib": "{userbase}/lib/{implementation_lower}/site-packages",
        "platlib": "{userbase}/lib/{implementation_lower}/site-packages",
        "include": "{userbase}/include/{implementation_lower}{py_version_short}",
        "scripts": "{userbase}/bin",
        "data": "{userbase}",
    },
}


# CPython 3.15.0 lists the same sorted schemes on every platform with a user
# base; osx_framework_library exists only in Homebrew's patched sysconfig.
_SCHEME_NAMES = (
    "nt",
    "nt_user",
    "nt_venv",
    "osx_framework_user",
    "posix_home",
    "posix_prefix",
    "posix_user",
    "posix_venv",
    "venv",
)



_CONFIG_VARS = None


def _is_pcc_runtime():
    return sys.implementation.name == "pcc"


def _version_parts():
    if _is_pcc_runtime():
        return PYTHON_TARGET_VERSION_PARTS
    return (
        str(sys.version_info.major),
        str(sys.version_info.minor),
        str(sys.version_info.micro),
    )


def get_python_version():
    major, minor, _micro = _version_parts()
    return major + "." + minor


def _full_python_version():
    major, minor, micro = _version_parts()
    return major + "." + minor + "." + micro


def _runtime_prefix():
    if _is_pcc_runtime():
        return os.path.normpath(os.getenv("PCC_PREFIX", "/usr/local"))
    return os.path.normpath(sys.prefix)


def _runtime_base_prefix():
    if _is_pcc_runtime():
        return os.path.normpath(
            os.getenv("PCC_BASE_PREFIX", os.getenv("PCC_PREFIX", "/usr/local"))
        )
    return os.path.normpath(sys.base_prefix)


def _runtime_userbase():
    configured = os.getenv("PYTHONUSERBASE", "")
    if configured:
        return os.path.normpath(configured)
    home = os.getenv("HOME", "")
    if home:
        return os.path.normpath(home + "/.local")
    return "/usr/local"


def _target_triple():
    configured = os.getenv("PCC_TARGET_TRIPLE", "")
    if configured:
        return configured
    machine = _platform.machine() or "unknown"
    if sys.platform.startswith("darwin"):
        return machine + "-apple-darwin"
    if sys.platform.startswith("linux"):
        return machine + "-unknown-linux-gnu"
    return machine + "-unknown-" + sys.platform


def get_platform():
    explicit = os.getenv("_PYTHON_HOST_PLATFORM", "")
    if explicit:
        return explicit
    triple = _target_triple()
    machine = _platform.machine() or "unknown"
    if "darwin" in triple or sys.platform.startswith("darwin"):
        # pcc owns a target tuple, not CPython's deployment-target-specific
        # wheel tag.  Keep that distinction visible in the returned value.
        return "darwin-" + machine.replace(" ", "_").replace("/", "-")
    if "linux" in triple or sys.platform.startswith("linux"):
        return "linux-" + machine.replace(" ", "_").replace("/", "-")
    if sys.platform.startswith("win"):
        if machine.lower() in ("amd64", "x86_64"):
            return "win-amd64"
        if machine.lower() in ("arm64", "aarch64"):
            return "win-arm64"
        return "win32"
    return sys.platform


def _pcc_extension_suffix():
    target = get_platform().replace("-", "_").replace(".", "_")
    return ".pcc3-pcc_native-" + target + ".so"


def _host_extension_suffix():
    cache_tag = sys.implementation.cache_tag or "python"
    machine = _platform.machine().replace(" ", "_").replace("/", "-")
    if sys.platform.startswith("darwin"):
        return "." + cache_tag + "-darwin.so"
    if sys.platform.startswith("linux"):
        return "." + cache_tag + "-" + machine + "-linux-gnu.so"
    if sys.platform.startswith("win"):
        return ".pyd"
    return ".so"


def _initial_config_vars():
    prefix = _runtime_prefix()
    base_prefix = _runtime_base_prefix()
    version = get_python_version()
    full_version = _full_python_version()
    nodot = version.replace(".", "")
    triple = _target_triple()
    is_pcc = _is_pcc_runtime()
    extension_suffix = _pcc_extension_suffix() if is_pcc else _host_extension_suffix()
    include = base_prefix + "/include/python" + version
    libdest = prefix + "/lib/python" + version
    cc = os.getenv("CC", "cc")
    cxx = os.getenv("CXX", "c++")
    cflags = os.getenv("CFLAGS", "")
    ldflags = os.getenv("LDFLAGS", "")
    soabi = extension_suffix[1:-3] if extension_suffix.endswith(".so") else ""
    return {
        # CPython 3.15.0 (GH-126920) keeps ``prefix`` as the active
        # ``sys.prefix``; only the ``installed_*`` keys name the base install.
        "prefix": prefix,
        "exec_prefix": prefix,
        "base": prefix,
        "platbase": prefix,
        "installed_base": base_prefix,
        "installed_platbase": base_prefix,
        "projectbase": prefix + "/bin",
        "userbase": _runtime_userbase(),
        "platlibdir": "lib",
        "abiflags": "",
        "ABIFLAGS": "",
        "implementation": "Python",
        "implementation_lower": "python",
        "abi_thread": "",
        "py_version": full_version,
        "py_version_short": version,
        "py_version_nodot": nodot,
        "py_version_nodot_plat": nodot,
        "VERSION": version,
        "BINDIR": prefix + "/bin",
        "BINLIBDEST": libdest,
        "LIBDEST": libdest,
        "LIBDIR": prefix + "/lib",
        "LIBPL": prefix + "/lib/python" + version + "/config",
        "INCLUDEPY": include,
        "CONFINCLUDEPY": include,
        "EXT_SUFFIX": extension_suffix,
        "SO": extension_suffix,
        "SHLIB_SUFFIX": ".pyd" if sys.platform.startswith("win") else ".so",
        "SOABI": soabi,
        "MULTIARCH": triple,
        "HOST_GNU_TYPE": triple,
        "BUILD_GNU_TYPE": triple,
        "CC": cc,
        "CXX": cxx,
        "CFLAGS": cflags,
        "CPPFLAGS": os.getenv("CPPFLAGS", ""),
        "LDFLAGS": ldflags,
        "LDSHARED": cc + " -shared " + ldflags,
        "AR": os.getenv("AR", "ar"),
        "ARFLAGS": os.getenv("ARFLAGS", "rcs"),
        "Py_DEBUG": 0,
        "Py_GIL_DISABLED": 0,
        "Py_ENABLE_SHARED": 0,
        "WITH_THREAD": 1,
        "LIBPYTHON": "" if is_pcc else "yes",
        "LDLIBRARY": "" if is_pcc else "libpython" + version,
        "MACOSX_DEPLOYMENT_TARGET": os.getenv("MACOSX_DEPLOYMENT_TARGET", ""),
        "PCC_NATIVE_ABI_VERSION": os.getenv(
            "PCC_NATIVE_ABI_VERSION", "pcc-native-v1"
        ),
    }


def get_config_vars(*args):
    global _CONFIG_VARS
    if _CONFIG_VARS is None:
        _CONFIG_VARS = _initial_config_vars()
    if args:
        return [_CONFIG_VARS.get(name) for name in args]
    return _CONFIG_VARS


def get_config_var(name):
    return get_config_vars().get(name)


def get_scheme_names():
    return _SCHEME_NAMES


def get_path_names():
    return _SCHEME_KEYS


def get_preferred_scheme(key):
    if key == "prefix":
        if not _is_pcc_runtime() and sys.prefix != sys.base_prefix:
            return "venv"
        return "posix_prefix"
    if key == "home":
        return "posix_home"
    if key == "user":
        return "posix_user"
    raise KeyError(key)


def get_default_scheme():
    return get_preferred_scheme("prefix")


def _get_default_scheme():
    return get_default_scheme()


def _expand_template(template, values):
    result = template
    for key, value in values.items():
        result = result.replace("{" + str(key) + "}", str(value))
    if "{" in result or "}" in result:
        raise AttributeError("unresolved sysconfig install-scheme variable")
    return os.path.normpath(result)


def get_paths(scheme=None, vars=None, expand=True):
    if scheme is None:
        scheme = get_default_scheme()
    if scheme not in _INSTALL_SCHEMES:
        raise KeyError(scheme)
    templates = _INSTALL_SCHEMES[scheme]
    if not expand:
        raw_paths = {}
        for name, template in templates.items():
            raw_paths[name] = template
        return raw_paths
    values = {}
    for key, value in get_config_vars().items():
        values[key] = value
    if vars is not None:
        for key, value in vars.items():
            values[key] = value
    paths = {}
    for name, template in templates.items():
        paths[name] = _expand_template(template, values)
    return paths


def get_path(name, scheme=None, vars=None, expand=True):
    return get_paths(scheme=scheme, vars=vars, expand=expand)[name]


def get_makefile_filename():
    raise NotImplementedError(
        "CPython Makefile metadata is not part of the pcc-native target"
    )


def get_config_h_filename():
    raise NotImplementedError(
        "CPython pyconfig.h metadata is not part of the pcc-native target"
    )


def parse_config_h(fp, vars=None):
    raise NotImplementedError(
        "parse_config_h requires an explicitly owned CPython pyconfig.h input"
    )


def parse_makefile(filename, vars=None, keep_unresolved=True):
    raise NotImplementedError(
        "parse_makefile requires an explicitly owned CPython Makefile input"
    )


def expand_makefile_vars(value, vars):
    raise NotImplementedError(
        "Makefile variable expansion requires explicitly owned Makefile metadata"
    )


def is_python_build(check_home=None):
    if check_home is not None:
        raise NotImplementedError(
            "source-tree probing is not part of the pcc-native target"
        )
    return False


__all__ = [
    "get_config_h_filename",
    "get_config_var",
    "get_config_vars",
    "get_default_scheme",
    "get_makefile_filename",
    "get_path",
    "get_path_names",
    "get_paths",
    "get_platform",
    "get_preferred_scheme",
    "get_python_version",
    "get_scheme_names",
    "expand_makefile_vars",
    "is_python_build",
    "parse_config_h",
    "parse_makefile",
]
