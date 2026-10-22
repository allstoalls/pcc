"""Method overrides in the complete export graph, without IR declarations."""
from __future__ import annotations


def _export_class_graph(native_exports):
    records = {}
    aliases = {}
    names = {}
    owners = {}
    for module_name, exports in native_exports.items():
        module_aliases = {}
        for export_name, info in exports.items():
            if not isinstance(info, dict) or info.get("kind") != "class":
                continue
            owner = info.get("owning_module", module_name) or module_name
            class_name = info.get("class_name", export_name)
            key = owner + "." + class_name
            records[key] = info
            owners[key] = owner
            module_aliases[export_name] = key
            for name in (export_name, class_name):
                candidates = names.setdefault(name, set())
                candidates.add(key)
        aliases[module_name] = module_aliases

    parents = {}
    for key, info in records.items():
        owner = owners[key]
        local_aliases = aliases.get(owner, {})
        bases = set()
        for base_name in info.get("base_names", ()):
            resolved = local_aliases.get(base_name)
            if resolved is not None:
                bases.add(resolved)
            elif base_name in records:
                bases.add(base_name)
            else:
                module, separator, export_name = base_name.rpartition(".")
                qualified = aliases.get(module, {}).get(export_name) if separator else None
                if qualified is not None:
                    bases.add(qualified)
                    continue
                # An export can be visible through a re-export before its
                # defining module is present. Keep every compatible alias;
                # ambiguity must not justify a direct base-method call.
                bases.update(names.get(base_name, ()))
        parents[key] = bases
    return records, parents


def build_export_attribute_interceptors(native_exports):
    """Class identities whose instances may inherit an attribute hook.

    First propagate hooks to their descendants, then to those descendants'
    possible static base types. Each pass visits a class/edge once; unrelated
    siblings of a hooked subclass remain eligible for declared result types.
    """
    records, parents = _export_class_graph(native_exports)
    return _attribute_interceptors_for_graph(records, parents)


def _attribute_interceptors_for_graph(records, parents):
    children = {}
    intercepted = set()
    pending = []
    for key, info in records.items():
        for base in parents[key]:
            children.setdefault(base, set()).add(key)
        for method in info.get("methods", ()):
            if method.get("name") == "__getattribute__":
                pending.append(key)
                break
    while pending:
        key = pending.pop()
        if key in intercepted:
            continue
        intercepted.add(key)
        pending.extend(children.get(key, ()))
    pending = list(intercepted)
    compatible = set()
    while pending:
        key = pending.pop()
        if key in compatible:
            continue
        compatible.add(key)
        pending.extend(parents.get(key, ()))
    return compatible


def build_export_subclassed_classes(native_exports):
    """Class identities in the export graph, and those that have a subclass.

    A class missing from the first set is unknown to the closed world, so
    nothing proves that it has no subclass.
    """
    records, parents = _export_class_graph(native_exports)
    subclassed = set()
    for key in records:
        subclassed.update(parents[key])
    return set(records), subclassed


def build_export_method_overrides(native_exports):
    records, parents = _export_class_graph(native_exports)

    overrides = {}
    for key, info in records.items():
        methods = set()
        for method in info.get("methods", ()):
            methods.add(method["name"])
        pending = list(parents[key])
        seen = {key}
        while pending:
            base = pending.pop()
            if base in seen:
                continue
            seen.add(base)
            overrides.setdefault(base, set()).update(methods)
            pending.extend(parents.get(base, ()))
    # A subclass may inherit its hook from a different branch of a multiple
    # inheritance graph, without declaring the hook in its own method table.
    for key in _attribute_interceptors_for_graph(records, parents):
        overrides.setdefault(key, set()).add("__getattribute__")
    return overrides
