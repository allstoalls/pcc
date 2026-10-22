"""Closed-world source/import dependency discovery for the Python frontend."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from pcc.frontends.python import pipeline_exports as _pipeline_exports
from pcc.frontends.python import pipeline_import_policy as _pipeline_import_policy
from pcc.frontends.python import pipeline_import_scan as _pipeline_import_scan
from pcc.frontends.python import pipeline_libpython as _pipeline_libpython
from pcc.frontends.python import pipeline_packages as _pipeline_packages
from pcc.frontends.python.pipeline_unsafe_imports import (
    source_has_only_consumed_unsafe_imports,
)
from pcc.driver import paths as _pipeline_paths
from pcc.frontends.python.pipeline_modes import PyPipelineError
from pcc.frontends.python.pipeline_profile import profile_begin as _profile_begin, profile_end as _profile_end

_module_name_from_src = _pipeline_paths.module_name_from_src
_module_root_from_src = _pipeline_paths.module_root_from_src
_package_parts_for_module = _pipeline_paths.package_parts_for_module
_join_dotted_parts = _pipeline_paths.join_dotted_parts
_resolve_module_src = _pipeline_paths.resolve_module_src

_package_site_roots = _pipeline_packages.package_site_roots
_resolve_module_src_for_import = _pipeline_packages.resolve_module_src_for_import
_package_site_package_root_for_src = _pipeline_packages.package_site_package_root_for_src
_package_site_package_root_for_module_name = _pipeline_packages.package_site_package_root_for_module_name
_package_root_no_libpython_diagnostic = _pipeline_packages.package_root_no_libpython_diagnostic
_resolve_pcc_native_extension_path = _pipeline_packages.resolve_pcc_native_extension_path

_source_module_scope_lines = _pipeline_import_scan._source_module_scope_lines
_iter_source_import_specs = _pipeline_import_scan._iter_source_import_specs
_iter_source_importlib_literal_specs = _pipeline_import_scan._iter_source_importlib_literal_specs
_iter_source_importlib_resource_literal_specs = _pipeline_import_scan._iter_source_importlib_resource_literal_specs
_iter_source_import_from_specs = _pipeline_import_scan._iter_source_import_from_specs
_without_attribute_error_handler_imports = _pipeline_import_scan._without_attribute_error_handler_imports
_source_import_discovery_text = _pipeline_import_scan._source_import_discovery_text
_without_type_checking_imports = _pipeline_import_scan._without_type_checking_imports
_without_inactive_runtime_imports = _pipeline_import_scan._without_inactive_runtime_imports

_COMPILE_TIME_ONLY_IMPORT_FROMS = _pipeline_import_policy.COMPILE_TIME_ONLY_IMPORT_FROMS
_COMPILE_TIME_ONLY_IMPORT_MODULES = _pipeline_import_policy.COMPILE_TIME_ONLY_IMPORT_MODULES
_TEST_FACADE_IMPORT_MODULES = _pipeline_import_policy.TEST_FACADE_IMPORT_MODULES
_NATIVE_BUILTIN_IMPORTS = _pipeline_import_policy.NATIVE_BUILTIN_IMPORTS
_NATIVE_BUILTIN_IMPORTS_WITH_COMPILED_PROVIDER = _pipeline_import_policy.NATIVE_BUILTIN_IMPORTS_WITH_COMPILED_PROVIDER
_REQUIRED_COMPILED_STDLIB_PROVIDERS = (
    _pipeline_import_policy.REQUIRED_COMPILED_STDLIB_PROVIDERS
)
_SCAFFOLD_IMPORT_MODULES = _pipeline_import_policy.SCAFFOLD_IMPORT_MODULES
_PCC_OWNED_COMPONENT_IMPORT_PREFIXES = (
    _pipeline_import_policy.PCC_OWNED_COMPONENT_IMPORT_PREFIXES
)

_closed_world_is_node = _pipeline_exports._closed_world_is_node
_py_ast_field_value = _pipeline_libpython.ast_field_value

(
    _PCC_DIR,
    _PIPELINE_DIR,
    _PY_RUNTIME_DIR,
    _PY_RUNTIME_DIR_CANDIDATES,
) = _pipeline_paths.resolve_runtime_paths(__file__)


def _validate_package_site_no_libpython_abi(
    src_paths: list[str],
    *,
    libpython_mode: str,
) -> None:
    """Reject installed packages that cannot satisfy a no-libpython compile.

    The package installer records pcc-native metadata, but older installs may
    have been produced before the CPython-extension-ABI gate existed. Re-scan
    installed package roots here so ``pcc --python-libpython=off`` fails at the
    package boundary instead of generating thousands of opaque ``py_cpy_*``
    fallback calls later in codegen.
    """
    if libpython_mode != "off":
        return
    roots: list[str] = []
    seen: set[str] = set()
    for src in src_paths:
        root = _package_site_package_root_for_src(src)
        if root is None or root in seen:
            pass
        else:
            seen.add(root)
            roots.append(root)
        try:
            with open(src, "r", encoding="utf-8") as f:
                source = f.read()
        except OSError:
            source = ""
        source = _without_inactive_runtime_imports(source)
        for import_name in _iter_source_import_specs(
            source,
            top_level_only=False,
        ):
            root = _package_site_package_root_for_module_name(import_name)
            if root is None or root in seen:
                continue
            seen.add(root)
            roots.append(root)
        for module_spec, imported_names in _iter_source_import_from_specs(
            source,
            top_level_only=False,
        ):
            root = _package_site_package_root_for_module_name(module_spec)
            if root is not None and root not in seen:
                seen.add(root)
                roots.append(root)
            if module_spec.startswith("."):
                continue
            for imported_name in imported_names:
                if not imported_name or imported_name == "*":
                    continue
                root = _package_site_package_root_for_module_name(
                    module_spec + "." + imported_name
                )
                if root is None or root in seen:
                    continue
                seen.add(root)
                roots.append(root)
    if not roots:
        return
    for root in roots:
        diagnostic = _package_root_no_libpython_diagnostic(root)
        if diagnostic is None:
            continue
        code, path = diagnostic
        raise PyPipelineError(
            code
            + ": installed package cannot be used by pcc-native no-libpython import: "
            + path
            + "; reinstall with --abi=pcc-native from source, or choose an explicit "
            + "--abi=libpython / --abi=cpython-compat mode"
        )


def _prepare_multi_source_compile_closure(
    src_paths: list[str],
    module_names: list[str],
    *,
    recursive_stdlib: bool,
    ir_scaffold_mode: str,
    profile: Optional[dict] = None,
) -> tuple[list[str], list[str]]:
    """Build the admitted multi-file closure in dependency-pass order.

    Compile-time scaffold modules must be removed before recursive stdlib
    discovery. Otherwise a rejected provider can leak its own host-only
    dependencies into the final no-libpython closure.
    """
    t = _profile_begin(profile)
    src_paths, module_names = _collect_multi_source_relative_closure(
        src_paths,
        module_names,
        recursive_stdlib=False,
    )
    _profile_end(profile, "collect_multi_source_relative_closure", t)

    t = _profile_begin(profile)
    src_paths, module_names = _filter_ir_scaffold_closure(
        src_paths,
        module_names,
        ir_scaffold_mode=ir_scaffold_mode,
    )
    _profile_end(profile, "filter_ir_scaffold_closure", t)

    seen = {
        mod_name: src_path for src_path, mod_name in zip(src_paths, module_names)
    }
    _expand_required_native_builtin_providers(src_paths, module_names, seen)

    if recursive_stdlib:
        t = _profile_begin(profile)
        _expand_recursive_stdlib(src_paths, module_names, seen)
        # Recursive providers can themselves name installed pcc-native object
        # ports; admit those only after the provider closure is final.
        _expand_native_extension_module_object_ports(
            src_paths,
            module_names,
            seen,
        )
        _profile_end(profile, "expand_recursive_stdlib", t)
    return src_paths, module_names


def _top_level_import_targets(
    root_dir: str,
    source: str,
    *,
    top_level_only: bool,
    consumed_unsafe_imports: bool = False,
) -> list[tuple[str, str]]:
    source = _without_inactive_runtime_imports(
        _without_attribute_error_handler_imports(source)
    )
    masked_source = _source_import_discovery_text(source)
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add_candidate(module_name: str) -> None:
        if not module_name or module_name.startswith(".") or module_name in seen:
            return
        src = _resolve_module_src_for_import(root_dir, module_name)
        if src is None:
            src = _locate_pcc_owned_component_source(module_name)
        if src is None:
            return
        seen.add(module_name)
        targets.append((src, module_name))

    for target_mod in _iter_source_import_specs(
        masked_source,
        top_level_only=top_level_only,
    ) + _iter_source_importlib_literal_specs(
        source,
        top_level_only=top_level_only,
    ) + _iter_source_importlib_resource_literal_specs(
        source,
        # Resource packages are data dependencies of compiled function bodies,
        # not imports that execute while the containing module initializes.
        # A canonical literal anchor is finite and resolves only to a real
        # local source package, so retain it even when optional lazy imports
        # stay masked by the ordinary module-scope policy.
        top_level_only=False,
    ):
        add_candidate(target_mod)

    for module_spec, imported_names in _iter_source_import_from_specs(
        masked_source,
        top_level_only=top_level_only,
    ):
        if module_spec.startswith("."):
            continue
        if (consumed_unsafe_imports and module_spec == "pcc.unsafe"
                and _resolves_owned_unsafe_provider(root_dir)):
            continue
        add_candidate(module_spec)
        # ``from pkg import submodule`` — also try each imported name as a
        # SUBMODULE of the package, so the submodule file is discovered and
        # compiled natively.  Without this, ``from p import sub`` fell back to
        # ``py_cpy_import`` (``p.sub`` was never added to the compile set, so
        # the from-import lowering's ``_native_import_from_submodule`` lookup
        # missed it), even though ``import p.sub`` and ``from p.sub import W``
        # already work.  ``add_candidate`` only adds names that resolve to a
        # real module file, so an imported name that is actually a
        # function/class/constant export of the package (not a submodule) is
        # correctly skipped.
        for imported_name in imported_names:
            if imported_name and imported_name != "*":
                add_candidate(module_spec + "." + imported_name)
    return targets


def _resolves_owned_unsafe_provider(root_dir: str) -> bool:
    """Preserve alternate providers after ordinary nearest-first resolution."""
    try:
        resolved = _resolve_module_src_for_import(root_dir, "pcc.unsafe")
        if resolved is None:
            return False
        owned = os.path.join(_PCC_DIR, "unsafe", "__init__.py")
        return os.path.realpath(resolved) == os.path.realpath(owned)
    except Exception:
        # An optional proof must not introduce a new compilation failure.
        return False


def _package_import_targets(
    src_path: str,
    mod_name: str,
    *,
    root_dir: Optional[str] = None,
    top_level_only: bool = False,
    include_relative: bool = True,
    include_same_package_absolute: bool = True,
    consumed_unsafe_imports: bool = False,
) -> list[tuple[str, str]]:
    """Return package-local import targets for ``src_path``.

    This includes:
    - relative imports such as ``from .foo import bar``
    - absolute same-package imports such as ``from pcc.driver.cli_core import cli_main``

    The second form is kept intentionally narrow: only imports whose module path
    starts with the current package root are considered, so stdlib / third-party
    absolute imports still stay out of the native source closure.
    """
    if "." not in mod_name and os.path.basename(src_path) != "__init__.py":
        return []
    if root_dir is None:
        root_dir = _module_root_from_src(src_path, mod_name)

    with open(src_path, "r", encoding="utf-8") as f:
        source = f.read()
    source = _without_attribute_error_handler_imports(source)
    source = _without_inactive_runtime_imports(source)

    current_pkg = _package_parts_for_module(src_path, mod_name)
    package_root = mod_name.split(".")[0]
    targets: list[tuple[str, str]] = []
    seen_targets: set[str] = set()
    import_specs = _iter_source_import_from_specs(
        _source_import_discovery_text(source),
        top_level_only=top_level_only,
    )
    for module_spec, imported_names in import_specs:
        if (consumed_unsafe_imports and module_spec == "pcc.unsafe"
                and _resolves_owned_unsafe_provider(root_dir)):
            continue
        candidate_mods: list[str] = []
        level = 0
        while level < len(module_spec) and module_spec[level] == ".":
            level += 1
        module_name = module_spec[level:]
        if level > 0 and include_relative:
            up = level - 1
            if up > len(current_pkg):
                continue
            base_pkg = current_pkg[: len(current_pkg) - up]
            if module_name:
                candidate_mods.append(
                    _join_dotted_parts(base_pkg + module_name.split("."))
                )
                for imported_name in imported_names:
                    candidate_mods.append(
                        _join_dotted_parts(
                            base_pkg + module_name.split(".") + imported_name.split(".")
                        )
                    )
            else:
                # ``from . import name`` may bind an attribute exported by the
                # package ``__init__.py`` rather than a sibling module.  Add
                # the package only when at least one imported name does not
                # resolve as a real module; otherwise preserve the bounded
                # sibling-only closure used by bootstrap.
                package_attribute_needed = not imported_names
                for imported_name in imported_names:
                    imported_mod = _join_dotted_parts(
                        base_pkg + imported_name.split(".")
                    )
                    candidate_mods.append(imported_mod)
                    if _resolve_module_src_for_import(root_dir, imported_mod) is None:
                        package_attribute_needed = True
                if package_attribute_needed:
                    candidate_mods.insert(0, _join_dotted_parts(base_pkg))
        elif module_name and include_same_package_absolute:
            mod_parts = module_name.split(".")
            if mod_parts and mod_parts[0] == package_root:
                candidate_mods.append(module_name)
                for imported_name in imported_names:
                    candidate_mods.append(
                        _join_dotted_parts(mod_parts + imported_name.split("."))
                    )
        for target_mod in candidate_mods:
            if not target_mod or target_mod in seen_targets:
                continue
            target_src = _resolve_module_src_for_import(root_dir, target_mod)
            if target_src is None:
                continue
            seen_targets.add(target_mod)
            targets.append((target_src, target_mod))
    return targets


def _collect_relative_module_closure(
    entry_src: str,
    *,
    include_same_package_absolute: bool = False,
    recurse_same_package_absolute: bool = False,
) -> tuple[list[str], list[str]]:
    """Return ``([src...], [module_name...])`` for a package entry plus
    recursively imported relative siblings and, optionally, a one-hop
    same-package absolute-import leaf set."""
    entry_src = str(os.path.abspath(entry_src))
    entry_mod = _module_name_from_src(entry_src)
    with open(entry_src, "r", encoding="utf-8") as f:
        entry_source = f.read()
    # Only a single entry with no other imports can make this local proof.
    # Do not apply it to discovered providers or multiple source roots: their
    # lazy runtime obligations may be outside the module-scope import scan.
    consumed_unsafe_imports = source_has_only_consumed_unsafe_imports(entry_source)
    if "." not in entry_mod:
        root_dir = str(os.path.dirname(entry_src))
        ordered_srcs = [entry_src]
        ordered_mods = [entry_mod]
        seen = {entry_mod: entry_src}
        queue = [(entry_src, entry_mod)]
        while queue:
            src_path, mod_name = queue.pop(0)
            with open(src_path, "r", encoding="utf-8") as f:
                source = f.read()
            # The ENTRY module's imports are scanned INCLUDING indented ones
            # (module-level ``try:``/``if:`` blocks and lazy function-level
            # imports), so e.g. ``try: from p import real`` / ``if cond: import
            # p.sub`` is discovered and compiled natively instead of tripping
            # the no-libpython gate ("imports still lower through CPython
            # fallback").  ``add_candidate`` only adds names that resolve to a
            # real module file under the source root, so a missing / optional
            # C-extension import inside a ``try`` is still left to
            # ``py_cpy_import`` (its runtime ImportError is caught as usual).
            # The entry retains its existing all-indentation scan for lazy
            # imports.  Discovered dependencies use the module-scope scanner:
            # it includes eager imports nested in try/if/else suites while
            # excluding function/class-body lazy imports that are outside the
            # initialization claim.  Resolution remains bounded to real files
            # under root_dir / PCC_PACKAGE_SITE.
            is_entry = mod_name == entry_mod
            for target_src, target_mod in _top_level_import_targets(
                root_dir,
                source,
                top_level_only=not is_entry,
                consumed_unsafe_imports=consumed_unsafe_imports and is_entry,
            ):
                if target_mod in seen:
                    continue
                target_src = str(os.path.abspath(target_src))
                seen[target_mod] = target_src
                ordered_srcs.append(target_src)
                ordered_mods.append(target_mod)
                queue.append((target_src, target_mod))
            local_root = _module_root_from_src(src_path, mod_name)
            for target_src, target_mod in _package_import_targets(
                src_path,
                mod_name,
                root_dir=local_root,
                top_level_only=not is_entry,
                include_relative=True,
                include_same_package_absolute=True,
                consumed_unsafe_imports=consumed_unsafe_imports and is_entry,
            ):
                if target_mod in seen:
                    continue
                target_src = str(os.path.abspath(target_src))
                seen[target_mod] = target_src
                ordered_srcs.append(target_src)
                ordered_mods.append(target_mod)
                queue.append((target_src, target_mod))
        return ordered_srcs, ordered_mods

    root_dir = _module_root_from_src(entry_src, entry_mod)
    ordered_srcs = [entry_src]
    ordered_mods = [entry_mod]
    seen = {entry_mod: entry_src}
    queue = [(entry_src, entry_mod)]

    if include_same_package_absolute:
        for target_src, target_mod in _package_import_targets(
            entry_src,
            entry_mod,
            root_dir=root_dir,
            include_relative=False,
            include_same_package_absolute=True,
            consumed_unsafe_imports=consumed_unsafe_imports,
        ):
            if target_mod in seen:
                continue
            target_src = str(os.path.abspath(target_src))
            seen[target_mod] = target_src
            ordered_srcs.append(target_src)
            ordered_mods.append(target_mod)
            if recurse_same_package_absolute:
                queue.append((target_src, target_mod))

    while queue:
        src_path, mod_name = queue.pop(0)
        local_root = _module_root_from_src(src_path, mod_name)
        targets = _package_import_targets(
            src_path,
            mod_name,
            root_dir=local_root,
            top_level_only=src_path != entry_src and "." not in mod_name,
            include_relative=True,
            include_same_package_absolute=False,
            consumed_unsafe_imports=consumed_unsafe_imports and src_path == entry_src,
        )
        if recurse_same_package_absolute:
            # Keep historical same-package lazy edges; absolute resolution uses
            # the entry's stable search root, independent of importer location.
            targets.extend(
                _package_import_targets(
                    src_path,
                    mod_name,
                    root_dir=root_dir,
                    top_level_only=src_path != entry_src and "." not in mod_name,
                    include_relative=False,
                    include_same_package_absolute=True,
                    consumed_unsafe_imports=(
                        consumed_unsafe_imports and src_path == entry_src
                    ),
                )
            )
            # Executable package entries need the same locally resolved eager
            # absolute-import edges as plain script entries. A dependency can
            # import a sibling package solely to retain its native ABI exports.
            with open(src_path, "r", encoding="utf-8") as stream:
                source = stream.read()
            targets.extend(
                _top_level_import_targets(
                    root_dir,
                    source,
                    top_level_only=src_path != entry_src,
                    consumed_unsafe_imports=(
                        consumed_unsafe_imports and src_path == entry_src
                    ),
                )
            )
        for target_src, target_mod in targets:
            if target_mod in seen:
                continue
            target_src = str(os.path.abspath(target_src))
            seen[target_mod] = target_src
            ordered_srcs.append(target_src)
            ordered_mods.append(target_mod)
            queue.append((target_src, target_mod))

    return ordered_srcs, ordered_mods


def _collect_multi_source_relative_closure(
    src_paths: list[str],
    module_names: list[str],
    *,
    recursive_stdlib: bool = False,
) -> tuple[list[str], list[str]]:
    """Return explicit sources plus their package-local source closure.

    Relative imports retain the historical recursive scan used by the
    bootstrap compiler closure, including top-level package initializers.
    Module-scope absolute imports follow ordinary local/project/package-site
    resolution, including other packages that provide native ABI exports.
    Absolute imports inside dependency functions stay lazy. Ambient stdlib and
    third-party lookup remains the separate provider-admission path.

    When ``recursive_stdlib=True`` (Issue 11.B.1), also pulls in any
    pure-Python stdlib module that's transitively imported by the seed
    set, allowing them to be compiled natively rather than routed
    through ``py_cpy_import``. Modules whose source can't be located,
    aren't ``.py`` files (C extensions / built-ins), or fail pcc's
    parser silently fall back to the dynamic path."""
    ordered_srcs: list[str] = []
    ordered_mods: list[str] = []
    for p in src_paths:
        ordered_srcs.append(str(os.path.abspath(p)))
    for m in module_names:
        ordered_mods.append(str(m))
    seen = {
        mod_name: src_path for src_path, mod_name in zip(ordered_srcs, ordered_mods)
    }
    queue = list(zip(ordered_srcs, ordered_mods))
    absolute_root = _module_root_from_src(queue[0][0], queue[0][1]) if queue else ""
    queue_i = 0
    while queue_i < len(queue):
        src_path, mod_name = queue[queue_i]
        queue_i += 1
        root_dir = _module_root_from_src(src_path, mod_name)
        with open(src_path, "r", encoding="utf-8") as stream:
            source = stream.read()
        # Multi-file admission also serves single-entry direct-indexed builds.
        # Re-prove the sole original root before rediscovering its imports;
        # multiple roots and discovered providers keep their ordinary edges.
        consumed_unsafe_imports = (
            len(src_paths) == 1
            and len(module_names) == 1
            and queue_i == 1
            and source_has_only_consumed_unsafe_imports(source)
        )
        targets = _top_level_import_targets(
            absolute_root,
            source,
            top_level_only=True,
            consumed_unsafe_imports=consumed_unsafe_imports,
        )
        targets.extend(
            _package_import_targets(
                src_path,
                mod_name,
                root_dir=root_dir,
                top_level_only="." not in mod_name,
                include_relative=True,
                include_same_package_absolute=False,
                consumed_unsafe_imports=consumed_unsafe_imports,
            )
        )
        targets.extend(
            _package_import_targets(
                src_path,
                mod_name,
                root_dir=absolute_root,
                top_level_only=True,
                include_relative=False,
                include_same_package_absolute=True,
                consumed_unsafe_imports=consumed_unsafe_imports,
            )
        )
        for target_src, target_mod in targets:
            if target_mod in seen:
                continue
            target_src = str(os.path.abspath(target_src))
            seen[target_mod] = target_src
            ordered_srcs.append(target_src)
            ordered_mods.append(target_mod)
            queue.append((target_src, target_mod))

    if recursive_stdlib:
        _expand_recursive_stdlib(ordered_srcs, ordered_mods, seen)

    _expand_native_extension_module_object_ports(
        ordered_srcs,
        ordered_mods,
        seen,
    )

    return ordered_srcs, ordered_mods


def _filter_ir_scaffold_closure(
    src_paths: list[str],
    module_names: list[str],
    *,
    ir_scaffold_mode: str,
) -> tuple[list[str], list[str]]:
    """Map ON-mode IR scaffold imports to their real link provider.

    ``from pcc.ir.compat import ir`` is a compile-time scaffold
    import in ON mode. The linked stage binary needs definitions from
    ``pcc.ir.ir`` for the emitted ``user_pcc_ir_ir_*``
    calls, but it does not need ``compat.py`` or the LLVM-C/JIT
    ``binding.py`` module. Keeping those in the source closure is what
    drags libpython back into the self backend path.
    """
    if ir_scaffold_mode != "on":
        return src_paths, module_names
    skip = {"pcc.ir.compat"}
    saw_compat = False
    need_ir_provider = False
    out_srcs: list[str] = []
    out_mods: list[str] = []
    seen: set[str] = set()
    root_dir = None

    limit = len(src_paths)
    if len(module_names) < limit:
        limit = len(module_names)
    i = 0
    while i < limit:
        src = src_paths[i]
        mod = module_names[i]
        if root_dir is None and (mod == "pcc" or mod.startswith("pcc.")):
            root_dir = _module_root_from_src(src, mod)
        if mod == "pcc.frontends.python.pipeline" or mod.startswith(
            "pcc.frontends.python.codegen"
        ):
            need_ir_provider = True
        if mod == "pcc.ir.compat":
            saw_compat = True
            need_ir_provider = True
            if "pcc.ir.ir" not in seen and root_dir is not None:
                ir_src = _resolve_module_src(root_dir, "pcc.ir.ir")
                if ir_src is not None:
                    out_srcs.append(str(os.path.abspath(ir_src)))
                    out_mods.append("pcc.ir.ir")
                    seen.add("pcc.ir.ir")
            i += 1
            continue
        if mod in skip:
            i += 1
            continue
        if mod in seen:
            i += 1
            continue
        out_srcs.append(src)
        out_mods.append(mod)
        seen.add(mod)
        i += 1
    if saw_compat or need_ir_provider:
        if "pcc.ir.ir" not in seen and root_dir is not None:
            ir_src = _resolve_module_src(root_dir, "pcc.ir.ir")
            if ir_src is not None:
                out_srcs.append(str(os.path.abspath(ir_src)))
                out_mods.append("pcc.ir.ir")
                seen.add("pcc.ir.ir")
    return out_srcs, out_mods


_HOST_FIND_SPEC_ORIGIN_CACHE: dict = {}
# Everything that can change what the probe interpreter resolves.
_HOST_FIND_SPEC_ENV_KEYS = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSAFEPATH",
    "PYTHONNOUSERSITE",
    "PYTHONUSERBASE",
)


def _host_find_spec_origin(mod_name: str) -> str:
    if sys.implementation.name == "pcc":
        # Native discovery uses owned providers and configured source roots.
        # An unavailable provider must not be discovered by another Python.
        return ""
    py_cmd = str(os.environ.get("PCC_HOST_PYTHON", "") or sys.executable).strip()
    # A Stage1 closure walk asked the same 32 names 2,125 times (`pcc` alone
    # 1,366 times), each a new interpreter: 49 s of serial coordinator time.
    key_parts = [py_cmd, os.getcwd()]
    for env_key in _HOST_FIND_SPEC_ENV_KEYS:
        key_parts.append(str(os.environ.get(env_key, "") or ""))
    key_parts.append(mod_name)
    cache_key = "\0".join(key_parts)
    cached = _HOST_FIND_SPEC_ORIGIN_CACHE.get(cache_key)
    if cached is not None:
        return cached
    probe = (
        "import importlib.util,sys\n"
        "try:\n"
        "    spec=importlib.util.find_spec(sys.argv[1])\n"
        "except ModuleNotFoundError:\n"
        "    spec=None\n"
        "origin='' if spec is None or spec.origin is None else spec.origin\n"
        "print(origin)"
    )
    try:
        out = subprocess.check_output([py_cmd, "-c", probe, mod_name], text=True)
    except Exception:
        # Not cached: a transient spawn failure is retried as before.
        return ""
    origin = out.strip()
    _HOST_FIND_SPEC_ORIGIN_CACHE[cache_key] = origin
    return origin


_HOST_STDLIB_ROOTS_CACHE: Optional[list[str]] = None
_HOST_SITE_ROOTS_CACHE: Optional[list[str]] = None


def _host_sysconfig_roots(keys: list[str]) -> list[str]:
    if sys.implementation.name == "pcc":
        return []
    py_cmd = str(os.environ.get("PCC_HOST_PYTHON", "") or sys.executable).strip()
    probe = (
        "import os,sys,sysconfig\n"
        "paths=sysconfig.get_paths()\n"
        "for key in sys.argv[1:]:\n"
        "    value=paths.get(key,'') or ''\n"
        "    if value:\n"
        "        print(os.path.realpath(os.path.abspath(value)))"
    )
    try:
        out = subprocess.check_output([py_cmd, "-c", probe] + list(keys), text=True)
    except Exception:
        return []
    roots: list[str] = []
    for raw in out.splitlines():
        value = str(raw or "").strip()
        if value:
            _append_unique_path(roots, value)
    return roots


def _host_stdlib_roots() -> list[str]:
    global _HOST_STDLIB_ROOTS_CACHE
    if _HOST_STDLIB_ROOTS_CACHE is None:
        _HOST_STDLIB_ROOTS_CACHE = _host_sysconfig_roots(["stdlib", "platstdlib"])
    return list(_HOST_STDLIB_ROOTS_CACHE)


def _host_site_roots() -> list[str]:
    global _HOST_SITE_ROOTS_CACHE
    if _HOST_SITE_ROOTS_CACHE is None:
        _HOST_SITE_ROOTS_CACHE = _host_sysconfig_roots(["purelib", "platlib"])
    return list(_HOST_SITE_ROOTS_CACHE)


def _append_unique_path(paths: list[str], path: Optional[str]) -> None:
    if path is None:
        return
    path = str(path or "").strip()
    if not path:
        return
    path = os.path.abspath(path)
    for existing in paths:
        if existing == path:
            return
    paths.append(path)


def _path_is_under(path: str, root: str) -> bool:
    if not path or not root:
        return False
    normalized = os.path.realpath(os.path.abspath(path))
    normalized_root = os.path.realpath(os.path.abspath(root))
    try:
        return os.path.commonpath([normalized_root, normalized]) == normalized_root
    except ValueError:
        return False


def _path_is_under_any(path: str, roots: list[str]) -> bool:
    for root in roots:
        if _path_is_under(path, root):
            return True
    return False


def _host_origin_is_stdlib_py(origin: str) -> bool:
    if origin == "" or origin == "built-in":
        return False
    if not origin.endswith(".py"):
        return False
    if _path_is_under_any(origin, _host_site_roots()):
        return False
    return _path_is_under_any(origin, _host_stdlib_roots())


def _append_pcc_package_dir_candidate(paths: list[str], path: Optional[str]) -> None:
    if path is None:
        return
    path = str(path or "").strip()
    if not path:
        return
    path = os.path.abspath(path)
    _append_unique_path(paths, path)
    _append_unique_path(paths, os.path.join(path, "pcc"))
    if os.path.basename(path) == "stdlib":
        _append_unique_path(paths, os.path.dirname(path))


def _append_pcc_package_dir_ancestors(paths: list[str], path: Optional[str]) -> None:
    if path is None:
        return
    path = str(path or "").strip()
    if not path:
        return
    cur = os.path.abspath(path)
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)
    while cur:
        _append_pcc_package_dir_candidate(paths, cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent


def _pcc_package_dir_has_native_stdlib(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "stdlib", "__init__.py"))


def _pcc_package_dir_candidates() -> list[str]:
    candidates: list[str] = []
    _append_pcc_package_dir_candidate(
        candidates,
        os.environ.get("PCC_PY_STDLIB_ROOT"),
    )
    _append_pcc_package_dir_candidate(
        candidates,
        os.environ.get("PCC_SOURCE_ROOT"),
    )
    _append_pcc_package_dir_candidate(
        candidates,
        os.environ.get("PCC_REPO_ROOT"),
    )
    _append_pcc_package_dir_candidate(candidates, _PCC_DIR)
    _append_pcc_package_dir_candidate(candidates, _PIPELINE_DIR)
    _append_pcc_package_dir_candidate(candidates, os.path.dirname(_PY_RUNTIME_DIR))
    try:
        if len(sys.argv) > 0:
            _append_pcc_package_dir_ancestors(candidates, sys.argv[0])
    except Exception:
        pass
    try:
        _append_pcc_package_dir_ancestors(candidates, sys.executable)
    except Exception:
        pass
    _append_pcc_package_dir_ancestors(candidates, os.getcwd())
    out: list[str] = []
    for candidate in candidates:
        if _pcc_package_dir_has_native_stdlib(candidate):
            _append_unique_path(out, candidate)
    return out


def _is_pcc_owned_component_module(mod_name: str) -> bool:
    for prefix in _PCC_OWNED_COMPONENT_IMPORT_PREFIXES:
        if mod_name == prefix or mod_name.startswith(prefix + "."):
            return True
    return False


def _locate_pcc_owned_component_source(mod_name: str) -> Optional[str]:
    """Resolve an allow-listed first-party component from the pcc tree.

    This is intentionally separate from stdlib discovery. The allow-list names
    runtime components shared with providers, such as the semantic target;
    arbitrary pcc compiler internals are never admitted through this route.
    """
    if not _is_pcc_owned_component_module(mod_name):
        return None
    relative_parts = mod_name.split(".")[1:]
    if not relative_parts:
        return None
    for pcc_package_dir in _pcc_package_dir_candidates():
        source = _resolve_module_src(pcc_package_dir, ".".join(relative_parts))
        if source is not None:
            return source
    return None


def _locate_native_stdlib_module_source(mod_name: str) -> Optional[str]:
    """Resolve only pcc-owned stdlib providers, without a host probe."""
    rel = mod_name.replace(".", os.sep)
    for pcc_package_dir in _pcc_package_dir_candidates():
        for dirname in ("stdlib",):
            root = os.path.join(pcc_package_dir, dirname)
            for pcc_port in (
                os.path.join(root, f"{rel}.py"),
                os.path.join(root, rel, "__init__.py"),
                # Legacy flat dotted filename form.
                os.path.join(root, f"{mod_name}.py"),
            ):
                if os.path.isfile(pcc_port):
                    return pcc_port
    return None


def _native_stdlib_parent_package_sources(mod_name: str) -> list[tuple[str, str]]:
    """Return pcc-owned ``__init__.py`` ancestors for a dotted provider.

    A leaf provider is not a substitute for its package object.  Admitting the
    real parents keeps initialization order and runtime module identity intact
    for generic package providers such as ``urllib.parse`` and
    ``importlib.resources``.
    """
    parts = mod_name.split(".")
    out: list[tuple[str, str]] = []
    index = 1
    while index < len(parts):
        parent_name = ".".join(parts[:index])
        parent_source = _locate_native_stdlib_module_source(parent_name)
        if (
            parent_source is not None
            and os.path.basename(parent_source) == "__init__.py"
        ):
            out.append((parent_name, parent_source))
        index += 1
    return out


def _locate_stdlib_module_source(mod_name: str) -> Optional[str]:
    """Resolve ``mod_name`` to a ``.py`` source path for the recursive
    stdlib walker (Issue 11.C.1).

    Search order:
    1. ``pcc/stdlib/<name>.py`` — first-class pcc-native stdlib
       ports used by normal CPython spellings such as ``import string``.
    2. ``pcc/ir/support/<name>.py`` — legacy port registry kept for older
       struct/_float_bits tests.
    3. CPython's stdlib via an external ``python3`` find-spec probe.

    Returns ``None`` for built-ins, C extensions, or modules that
    can't be located.
    """
    pcc_port = _locate_native_stdlib_module_source(mod_name)
    if pcc_port is not None:
        return pcc_port
    try:
        origin = _host_find_spec_origin(mod_name)
    except Exception:
        return None
    if not _host_origin_is_stdlib_py(origin):
        return None
    return origin


def _native_stdlib_root_for_path(path: str) -> Optional[str]:
    normalized = os.path.abspath(path)
    for pcc_package_dir in _pcc_package_dir_candidates():
        for dirname in ("stdlib",):
            root = os.path.abspath(os.path.join(pcc_package_dir, dirname))
            try:
                common = os.path.commonpath([root, normalized])
            except ValueError:
                continue
            if common == root:
                return root
    return None


def _pcc_log_channel_enabled(channel: str) -> bool:
    raw = str(os.environ.get("PCC_LOG", "") or "").strip()
    if not raw:
        return False
    normalized = raw.lower()
    if normalized in ("1", "true", "yes", "on", "all"):
        return True
    for part in normalized.replace(";", ",").split(","):
        item = part.strip()
        if item == channel or item == "all":
            return True
    return False


def _pcc_json_escape(value: object) -> str:
    text = str(value or "")
    out = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            out += "\\\\"
        elif ch == '"':
            out += '\\"'
        elif ch == "\n":
            out += "\\n"
        elif ch == "\r":
            out += "\\r"
        elif ch == "\t":
            out += "\\t"
        else:
            out += ch
        i += 1
    return out


def _pcc_import_log_line(
    *,
    module: str,
    classification: str,
    source: str,
    provider: Optional[str] = None,
) -> str:
    top = module.split(".")[0] if module else ""
    provider_text = "" if provider is None else provider
    native = classification in (
        "compile_time_only",
        "native_user_module",
        "builtin_native_dispatch",
        "native_stdlib",
    )
    return (
        "{"
        '"schema":"pcc.import_log.v1",'
        '"category":"import",'
        '"event":"route",'
        '"module":"' + _pcc_json_escape(module) + '",'
        '"top":"' + _pcc_json_escape(top) + '",'
        '"classification":"' + _pcc_json_escape(classification) + '",'
        '"native":' + ("true" if native else "false") + ","
        '"provider":"' + _pcc_json_escape(provider_text) + '",'
        '"source":"' + _pcc_json_escape(source) + '"'
        "}"
    )


def _pcc_emit_import_log(
    *,
    module: str,
    classification: str,
    source: str,
    provider: Optional[str] = None,
) -> None:
    if not _pcc_log_channel_enabled("import"):
        return
    line = _pcc_import_log_line(
        module=module,
        classification=classification,
        source=source,
        provider=provider,
    )
    target = str(os.environ.get("PCC_LOG_FILE", "") or "")
    if target and target != "-":
        with open(target, "a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")
        return
    sys.stderr.write(line + "\n")


def _record_import_classification(
    mod_name: str,
    classification: str,
    *,
    source: str = "classify",
    provider: Optional[str] = None,
) -> str:
    _pcc_emit_import_log(
        module=mod_name,
        classification=classification,
        source=source,
        provider=provider,
    )
    return classification


def _classify_python_import(
    mod_name: str,
    *,
    native_modules: Optional[set[str]] = None,
) -> str:
    """Classify an import without changing the public CPython spelling.

    Return values are deliberately stable strings so tests and bootstrap
    diagnostics can assert the routing boundary:

    - ``compile_time_only``
    - ``native_user_module``
    - ``builtin_native_dispatch``
    - ``native_stdlib``
    - ``cpython_fallback``
    """
    if not mod_name:
        return _record_import_classification(
            mod_name,
            "cpython_fallback",
            source="empty",
        )
    if mod_name in _TEST_FACADE_IMPORT_MODULES:
        return _record_import_classification(
            mod_name,
            "compile_time_only",
            source="test_facade",
        )
    top = mod_name.split(".")[0]
    if top in _COMPILE_TIME_ONLY_IMPORT_MODULES:
        return _record_import_classification(
            mod_name,
            "compile_time_only",
            source="compile_time_only",
        )
    if native_modules is not None and mod_name in native_modules:
        return _record_import_classification(
            mod_name,
            "native_user_module",
            source="native_modules",
        )
    if mod_name in _NATIVE_BUILTIN_IMPORTS or top in _NATIVE_BUILTIN_IMPORTS:
        return _record_import_classification(
            mod_name,
            "builtin_native_dispatch",
            source="builtin_native_dispatch",
        )
    located = _locate_stdlib_module_source(mod_name)
    if located is not None and _native_stdlib_root_for_path(located) is not None:
        return _record_import_classification(
            mod_name,
            "native_stdlib",
            source="native_stdlib",
            provider=located,
        )
    return _record_import_classification(
        mod_name,
        "cpython_fallback",
        source="missing_native_provider",
        provider=located,
    )


def _source_uses_native_stdlib(src_path: str) -> bool:
    # A package can call a small factory during module initialization whose
    # body imports a pcc-owned stdlib port (simplejson's OrderedDict chooser is
    # one real shape).  This is dependency discovery, so do not run the full
    # parser a second time for every module before the content cache can be
    # queried.  The bootstrap-safe import scanner preserves statement
    # boundaries while masking strings and comments.
    try:
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return False
    for mod_name in _source_absolute_imports_for_discovery(source):
        if _locate_native_stdlib_module_source(mod_name) is not None:
            return True
    return False


def _sources_use_native_stdlib(src_paths: list[str]) -> bool:
    for src_path in src_paths:
        if _source_uses_native_stdlib(src_path):
            return True
    return False




def _source_absolute_imports_for_discovery(
    source: str,
    *,
    include_function_bodies: bool = True,
) -> list[str]:
    """Return absolute import module names without constructing an AST.

    Class bodies remain part of module initialization.  Function bodies are
    included only for the narrower pcc-owned-provider decision.
    """
    source = _without_inactive_runtime_imports(source)
    masked_source = _source_import_discovery_text(source)
    if include_function_bodies:
        lines = [(line, True) for line in masked_source.splitlines()]
    else:
        lines = _source_module_scope_lines(
            masked_source,
            include_class_bodies=True,
        )
    scan_parts: list[str] = []
    for raw_line, active in lines:
        if active:
            # Both tokens delimit simple statements in valid Python source.
            # Splitting them also covers ``if cond: import mod`` without
            # admitting deferred ``def f(): import mod`` lines, which were
            # marked inactive above.
            scan_parts.append(raw_line.replace(":", "\n").replace(";", "\n"))
        scan_parts.append("\n")
    scan_source = "".join(scan_parts)
    out: list[str] = []
    seen: set[str] = set()
    for mod_name in _iter_source_import_specs(scan_source, top_level_only=False):
        if mod_name and not mod_name.startswith(".") and mod_name not in seen:
            seen.add(mod_name)
            out.append(mod_name)
    for mod_name, imported_names in _iter_source_import_from_specs(
        scan_source,
        top_level_only=False,
    ):
        compile_only = _COMPILE_TIME_ONLY_IMPORT_FROMS.get(mod_name)
        if compile_only is not None and imported_names:
            compile_only_only = True
            for imported_name in imported_names:
                if imported_name not in compile_only:
                    compile_only_only = False
                    break
            if compile_only_only and mod_name == "dataclasses" and "field" in imported_names:
                from pcc.frontends.python.py_lift import parse_and_lift
                from pcc.frontends.python.pipeline_import_policy import dataclasses_field_runtime_names
                try:
                    field_module = parse_and_lift(source, "<field-import-scan>", "<scan>")
                    if dataclasses_field_runtime_names(field_module):
                        compile_only_only = False
                except Exception:
                    # An uncertain runtime binding must keep its real owner.
                    compile_only_only = False
            if compile_only_only:
                continue
        if mod_name and not mod_name.startswith(".") and mod_name not in seen:
            seen.add(mod_name)
            out.append(mod_name)
        # ``from package import child`` can name a real child module rather
        # than an attribute exported by ``package``.  Record both finite
        # candidates here; the caller still admits the child only when the
        # stdlib/provider registry resolves an actual source file.  This keeps
        # ordinary exports such as ``from pathlib import Path`` harmless while
        # allowing explicit siblings such as ``unittest.mock`` into the
        # recursive no-libpython closure.
        if (
            mod_name
            and not mod_name.startswith(".")
            and mod_name not in _COMPILE_TIME_ONLY_IMPORT_MODULES
        ):
            for imported_name in imported_names:
                if not imported_name or imported_name == "*":
                    continue
                child_name = mod_name + "." + imported_name
                if child_name not in seen:
                    seen.add(child_name)
                    out.append(child_name)
    return out


def _source_pcc_native_extension_paths(src_path: str) -> list[str]:
    """Installed pcc-native extension artifacts named by one source file."""
    try:
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return []
    source = _without_inactive_runtime_imports(source)
    out: list[str] = []
    seen: set[str] = set()

    def add_module(module_name: str) -> None:
        path = _resolve_pcc_native_extension_path(module_name)
        if path is None or path in seen:
            return
        seen.add(path)
        out.append(path)

    for module_name in _iter_source_import_specs(
        source,
        top_level_only=False,
    ):
        add_module(module_name)
    for module_name, imported_names in _iter_source_import_from_specs(
        source,
        top_level_only=False,
    ):
        if module_name.startswith("."):
            continue
        add_module(module_name)
        for imported_name in imported_names:
            add_module(module_name + "." + imported_name)
    return out


def _source_imports_pcc_native_extension(src_path: str) -> bool:
    """Whether source names an installed pcc-native extension module."""
    return bool(_source_pcc_native_extension_paths(src_path))


def _is_ascii_module_candidate(text: str) -> bool:
    if not text or len(text) > 512:
        return False
    parts = text.split(".")
    for part in parts:
        if not part:
            return False
        first = part[0]
        if not (first == "_" or ("a" <= first <= "z") or ("A" <= first <= "Z")):
            return False
        for ch in part[1:]:
            if not (
                ch == "_"
                or ("a" <= ch <= "z")
                or ("A" <= ch <= "Z")
                or ("0" <= ch <= "9")
            ):
                return False
    return True


def _native_extension_literal_module_candidates(path: str) -> list[str]:
    """Extract bounded ASCII identifier tokens from a native artifact.

    The result is only a candidate set. The closure caller requires a real
    Python source provider under a configured package root before accepting a
    token, which filters C symbols, source filenames, diagnostics, and other
    binary strings without relying on an external ``strings`` process.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []

    out: list[str] = []
    seen: set[str] = set()
    token_chars: list[str] = []
    token_too_long = False
    i = 0
    while i <= len(data):
        byte = data[i] if i < len(data) else 0
        is_token_byte = (
            byte == 46
            or byte == 95
            or 48 <= byte <= 57
            or 65 <= byte <= 90
            or 97 <= byte <= 122
        )
        if is_token_byte:
            if len(token_chars) < 512:
                token_chars.append(chr(byte))
            else:
                token_too_long = True
            i += 1
            continue
        if token_chars and not token_too_long:
            candidate = "".join(token_chars)
            if candidate not in seen and _is_ascii_module_candidate(candidate):
                seen.add(candidate)
                out.append(candidate)
        token_chars = []
        token_too_long = False
        i += 1
    return out


def _expand_native_extension_module_object_ports(
    ordered_srcs: list[str],
    ordered_mods: list[str],
    seen: dict[str, str],
) -> None:
    """Publish explicitly imported builtin ports beside native extensions.

    Compiler-recognized builtins normally lower directly and therefore do not
    need runtime module objects.  A pcc-native C extension can observe them via
    ``PyImport_ImportModule``, though.  When such an extension is in the source
    graph, add only builtin modules explicitly imported by that graph and only
    when a pcc-Python port exists.  They then use the ordinary compiled-sibling
    registry; no requesting-package dispatch or C semantic module is added.
    """
    extension_paths: list[str] = []
    seen_extension_paths: set[str] = set()
    for src in ordered_srcs:
        for path in _source_pcc_native_extension_paths(src):
            if path in seen_extension_paths:
                continue
            seen_extension_paths.add(path)
            extension_paths.append(path)
    if not extension_paths:
        return

    package_queue: list[tuple[str, str]] = []
    for extension_path in extension_paths:
        for module_name in _native_extension_literal_module_candidates(extension_path):
            # A dotted spelling plus a source provider under an explicit
            # package root is the minimum evidence that a binary token is a
            # module dependency rather than a C symbol or diagnostic word.
            if "." not in module_name or module_name in seen:
                continue
            provider = None
            for site_root in _package_site_roots():
                provider = _resolve_module_src(site_root, module_name)
                if provider is not None:
                    break
            if provider is None:
                continue
            provider = str(os.path.abspath(provider))
            seen[module_name] = provider
            ordered_srcs.append(provider)
            ordered_mods.append(module_name)
            package_queue.append((provider, module_name))

    queue_i = 0
    while queue_i < len(package_queue):
        src_path, mod_name = package_queue[queue_i]
        queue_i += 1
        root_dir = _module_root_from_src(src_path, mod_name)
        for target_src, target_mod in _package_import_targets(
            src_path,
            mod_name,
            root_dir=root_dir,
            top_level_only=True,
            include_relative=True,
            include_same_package_absolute=True,
        ):
            if target_mod in seen:
                continue
            target_src = str(os.path.abspath(target_src))
            seen[target_mod] = target_src
            ordered_srcs.append(target_src)
            ordered_mods.append(target_mod)
            package_queue.append((target_src, target_mod))

    seed_srcs = list(ordered_srcs)
    for src_path in seed_srcs:
        for module_name in _stdlib_absolute_imports_in(src_path):
            top = module_name.split(".", 1)[0]
            if module_name in seen or top not in _NATIVE_BUILTIN_IMPORTS:
                continue
            provider = _locate_stdlib_module_source(module_name)
            if provider is None or _native_stdlib_root_for_path(provider) is None:
                continue
            provider = str(os.path.abspath(provider))
            seen[module_name] = provider
            ordered_srcs.append(provider)
            ordered_mods.append(module_name)


def _stdlib_absolute_imports_in(
    src_path: str,
    *,
    include_function_bodies: bool = False,
) -> list[str]:
    """Return absolute imports reachable during module initialization.

    Function bodies are normally excluded because eagerly pulling every lazy
    import turns a small package into the transitive closure of optional
    helpers (for example pydoc -> http.server -> email).  Callers may request
    them for the narrower pcc-owned-provider scan; host stdlib expansion still
    uses the default module-initialization boundary.
    """
    from pcc.frontends.python.py_ast import ClassDef as _ClassDef
    from pcc.frontends.python.py_ast import ExceptHandler as _ExceptHandler
    from pcc.frontends.python.py_ast import For as _For
    from pcc.frontends.python.py_ast import FuncDef as _FuncDef
    from pcc.frontends.python.py_ast import If as _If
    from pcc.frontends.python.py_ast import Import as _Import
    from pcc.frontends.python.py_ast import ImportFrom as _ImportFrom
    from pcc.frontends.python.py_ast import Try as _Try
    from pcc.frontends.python.py_ast import While as _While
    from pcc.frontends.python.py_ast import With as _With
    from pcc.frontends.python.py_lift import parse_and_lift

    with open(src_path, "r", encoding="utf-8") as f:
        text = f.read()
    text = _without_inactive_runtime_imports(text)
    try:
        ast_mod = parse_and_lift(text, src_path, "<scan>")
    except Exception:
        return []
    out: list[str] = []

    pending = [_py_ast_field_value(ast_mod, "body", ())]
    while pending:
        stmts = pending.pop()
        for stmt in stmts:
            if _closed_world_is_node(stmt, _Import):
                for mod_name, _ in _py_ast_field_value(stmt, "names", ()):
                    out.append(mod_name)
            elif _closed_world_is_node(stmt, _ImportFrom):
                module = _py_ast_field_value(stmt, "module", None)
                level = _py_ast_field_value(stmt, "level", 0)
                names = _py_ast_field_value(stmt, "names", ())
                if module is not None and level == 0:
                    # `from X import a, b` where every name is in
                    # _COMPILE_TIME_ONLY_IMPORT_FROMS[X] is a compile-time
                    # macro / decorator import (e.g. `from dataclasses import
                    # dataclass, field`). Don't treat it as evidence that the
                    # source uses module X at runtime — otherwise the native
                    # stdlib closure pulls in pcc/stdlib/X.py and forces
                    # libpython through any py_cpy_* fallbacks in that port.
                    compile_only = _COMPILE_TIME_ONLY_IMPORT_FROMS.get(module)
                    if compile_only is not None:
                        compile_only_only = True
                        for alias_name, _ in names:
                            if alias_name not in compile_only:
                                compile_only_only = False
                                break
                        if module == "dataclasses":
                            from pcc.frontends.python.pipeline_import_policy import dataclasses_field_runtime_names
                            if dataclasses_field_runtime_names(ast_mod):
                                compile_only_only = False
                        if compile_only_only:
                            pass
                        else:
                            out.append(module)
                    else:
                        out.append(module)
            if _closed_world_is_node(stmt, (_ClassDef, _With, _ExceptHandler)) or (
                include_function_bodies and _closed_world_is_node(stmt, _FuncDef)
            ):
                pending.append(_py_ast_field_value(stmt, "body", ()))
            elif _closed_world_is_node(stmt, (_If, _While, _For)):
                pending.append(_py_ast_field_value(stmt, "else_body", ()))
                pending.append(_py_ast_field_value(stmt, "body", ()))
            elif _closed_world_is_node(stmt, _Try):
                pending.append(_py_ast_field_value(stmt, "finally_body", ()))
                pending.append(_py_ast_field_value(stmt, "else_body", ()))
                pending.append(_py_ast_field_value(stmt, "handlers", ()))
                pending.append(_py_ast_field_value(stmt, "body", ()))
    return out


def _stdlib_module_compiles(src_path: str, mod_name: str) -> bool:
    """Fail-soft recursive-stdlib codegen probe."""
    if _native_stdlib_root_for_path(src_path) is not None:
        return True
    from pcc.frontends.python.type_infer import infer_module
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_lift import parse_and_lift

    try:
        with open(src_path, "r", encoding="utf-8") as f:
            text = f.read()
        ast_mod = parse_and_lift(text, src_path, mod_name)
        typed = infer_module(ast_mod)
        cg = L1CodeGen(typed)
        cg._module_source_path = os.path.abspath(src_path)
        cg.generate(typed)
        return True
    except Exception:
        return False


def _expand_recursive_stdlib(
    ordered_srcs: list[str],
    ordered_mods: list[str],
    seen: dict[str, str],
) -> None:
    """Issue 11.B.1: pull pure-Python stdlib modules transitively into
    the native compile set. In-place: appends to ordered_srcs /
    ordered_mods.

    Walks the seed sources, looks up each absolute non-relative,
    non-whitelisted import via ``importlib.util.find_spec``. A module
    is added to the closure only if pcc can BOTH parse AND codegen
    it (Issue 11.B.2 — fail-soft). Modules that parse but fail
    codegen are excluded; their importers fall back to
    ``py_cpy_import`` for them.
    """
    queue = []
    for mod_name in ordered_mods:
        queue.append(mod_name)
    failures: list[str] = []

    def admit_provider(module_name: str, source_path: str) -> None:
        if module_name in seen or module_name in failures:
            return
        if not _stdlib_module_compiles(source_path, module_name):
            failures.append(module_name)
            return
        source_path = str(os.path.abspath(source_path))
        seen[module_name] = source_path
        ordered_srcs.append(source_path)
        ordered_mods.append(module_name)
        queue.append(module_name)

    while queue:
        cur_mod = queue.pop(0)
        cur_src = seen.get(cur_mod)
        if cur_src is None:
            continue
        try:
            with open(cur_src, "r", encoding="utf-8") as f:
                source = f.read()
        except OSError:
            continue
        if _native_stdlib_root_for_path(cur_src) is not None:
            # Providers are ordinary packages too.  Pull their relative and
            # same-package imports after recursive stdlib admission; the first
            # relative-closure pass necessarily ran before these files were
            # known.  Keep the edge bounded to pcc-owned provider sources.
            root_dir = _module_root_from_src(cur_src, cur_mod)
            for relative_src, relative_mod in _package_import_targets(
                cur_src,
                cur_mod,
                root_dir=root_dir,
                include_relative=True,
                include_same_package_absolute=True,
            ):
                if _native_stdlib_root_for_path(relative_src) is None:
                    continue
                for parent_mod, parent_src in _native_stdlib_parent_package_sources(
                    relative_mod
                ):
                    admit_provider(parent_mod, parent_src)
                admit_provider(relative_mod, relative_src)
        import_names = _source_absolute_imports_for_discovery(
            source,
            include_function_bodies=False,
        )
        # Include lazy imports only for first-class pcc-owned stdlib providers.
        # This covers module-init factories without admitting arbitrary host
        # stdlib/optional dependency trees into the no-libpython closure.
        for lazy_name in _source_absolute_imports_for_discovery(
            source,
            include_function_bodies=True,
        ):
            lazy_provider = _locate_native_stdlib_module_source(lazy_name)
            if (
                lazy_provider is not None
                and _native_stdlib_root_for_path(lazy_provider) is not None
            ):
                import_names.append(lazy_name)
        for import_name in import_names:
            top = import_name.split(".")[0]
            if top == "pcc" and _is_pcc_owned_component_module(import_name):
                component_source = _locate_pcc_owned_component_source(import_name)
                if component_source is not None:
                    admit_provider(import_name, component_source)
                continue
            if (
                import_name in seen
                or import_name in failures
                or import_name in _TEST_FACADE_IMPORT_MODULES
                or top in _COMPILE_TIME_ONLY_IMPORT_MODULES
                or (
                    top in _NATIVE_BUILTIN_IMPORTS
                    and import_name
                    not in _NATIVE_BUILTIN_IMPORTS_WITH_COMPILED_PROVIDER
                )
                or import_name in _SCAFFOLD_IMPORT_MODULES
            ):
                continue
            # Skip pcc internals UNLESS we're pulling pcc.ir.support (the
            # port registry — Issue 11.C.1). Other pcc.* modules are
            # already handled by the relative-import walker for the
            # bootstrap closure.
            if top == "pcc" and not import_name.startswith("pcc.ir.support."):
                continue
            target_src = _locate_stdlib_module_source(import_name)
            if target_src is None:
                continue
            if _native_stdlib_root_for_path(target_src) is not None:
                for parent_mod, parent_src in _native_stdlib_parent_package_sources(
                    import_name
                ):
                    admit_provider(parent_mod, parent_src)
            admit_provider(import_name, target_src)


def _expand_required_native_builtin_providers(
    ordered_srcs: list[str],
    ordered_mods: list[str],
    seen: dict[str, str],
) -> None:
    """Add finite semantic providers required by a shallow multi build.

    ``recursive_stdlib=False`` keeps optional stdlib discovery shallow, but it
    cannot omit a directly imported pcc-owned module required at runtime, nor
    one that defines symbols emitted directly by native lowering.  Keep these
    mandatory roots separate from the opt-in recursive stdlib closure; this
    pass admits only the named provider, not its transitive stdlib graph.
    """
    queue = list(zip(ordered_srcs, ordered_mods))
    queue_i = 0
    while queue_i < len(queue):
        src_path, _mod_name = queue[queue_i]
        queue_i += 1
        try:
            with open(src_path, "r", encoding="utf-8") as f:
                source = f.read()
        except OSError:
            continue
        for import_name in _source_absolute_imports_for_discovery(
            source,
            include_function_bodies=True,
        ):
            provider_name = import_name.split(".", 1)[0]
            component = (
                _locate_pcc_owned_component_source(import_name)
                if provider_name == "pcc"
                else None
            )
            if component is not None:
                provider_name = import_name
            if provider_name in seen:
                continue
            provider = component
            if provider is None:
                provider = _locate_stdlib_module_source(provider_name)
                if (
                    provider is None
                    or _native_stdlib_root_for_path(provider) is None
                ):
                    continue
                if (
                    provider_name in _NATIVE_BUILTIN_IMPORTS
                    and provider_name not in _REQUIRED_COMPILED_STDLIB_PROVIDERS
                ):
                    continue
            provider = str(os.path.abspath(provider))
            seen[provider_name] = provider
            ordered_srcs.append(provider)
            ordered_mods.append(provider_name)
            queue.append((provider, provider_name))


def _relative_import_targets(
    src_path: str,
    mod_name: str,
    *,
    root_dir: Optional[str] = None,
    top_level_only: bool = False,
) -> list[tuple[str, str]]:
    """Return package-local import targets for init ordering."""
    return _package_import_targets(
        src_path,
        mod_name,
        root_dir=root_dir,
        top_level_only=top_level_only,
    )


def _order_module_init_deps_for(
    mod_name: str,
    module_to_src,
    module_set,
    dep_cache,
) -> list[str]:
    cached = dep_cache.get(mod_name)
    if cached is not None:
        return cached
    src_path = module_to_src[mod_name]
    deps: list[str] = []
    try:
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError:
        source = ""

    def add_dep(dep_mod: str) -> None:
        if dep_mod in module_set and dep_mod != mod_name and dep_mod not in deps:
            deps.append(dep_mod)

    for dep_mod in _iter_source_import_specs(source, top_level_only=True):
        add_dep(dep_mod)
        top_mod = dep_mod.split(".", 1)[0]
        add_dep(top_mod)
    for module_spec, imported_names in _iter_source_import_from_specs(
        source,
        top_level_only=True,
    ):
        if module_spec.startswith("."):
            continue
        add_dep(module_spec)
        for imported_name in imported_names:
            if imported_name and imported_name != "*":
                add_dep(module_spec + "." + imported_name)

    for _dep_src, dep_mod in _relative_import_targets(
        src_path,
        mod_name,
        top_level_only=True,
    ):
        resolved_dep = dep_mod
        if (
            dep_mod == "pcc.ir.compat"
            and dep_mod not in module_set
            and "pcc.ir.ir" in module_set
        ):
            # ON-mode scaffold filtering replaces compat with the real
            # pcc.ir.ir provider. Preserve the top-init dependency
            # so IR type class objects exist before runtime_abi/layer1 use
            # VoidType(), IntType(), FunctionType(), etc.
            resolved_dep = "pcc.ir.ir"
        if resolved_dep in module_set and resolved_dep != mod_name:
            deps.append(resolved_dep)
    dep_cache[mod_name] = deps
    return deps


def _order_module_inits(
    src_paths: list[str],
    module_names: list[str],
    entry_module: str,
) -> list[str]:
    """Return a dependency-first order for sibling module top-inits."""
    if len(module_names) <= 1:
        return []
    module_to_src = {
        mod_name: str(os.path.abspath(src_path))
        for src_path, mod_name in zip(src_paths, module_names)
    }
    module_set = set(module_names)
    dep_cache: dict[str, list[str]] = {}
    ordered: list[str] = []
    visiting: set[str] = set()
    emitted: set[str] = set()

    roots = [entry_module]
    for mod_name in module_names:
        if mod_name != entry_module:
            roots.append(mod_name)
    for root in roots:
        stack = [(root, False)]
        while stack:
            mod_name, expanded = stack.pop()
            if mod_name in emitted:
                continue
            if expanded:
                visiting.discard(mod_name)
                emitted.add(mod_name)
                if mod_name != entry_module:
                    ordered.append(mod_name)
                continue
            if mod_name in visiting:
                continue
            visiting.add(mod_name)
            stack.append((mod_name, True))
            deps = _order_module_init_deps_for(
                mod_name,
                module_to_src,
                module_set,
                dep_cache,
            )
            dep_i = len(deps) - 1
            while dep_i >= 0:
                dep_mod = deps[dep_i]
                if dep_mod != entry_module and dep_mod not in emitted:
                    stack.append((dep_mod, False))
                dep_i -= 1
    return ordered
