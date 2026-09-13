"""Method overrides in the complete export graph, without IR declarations."""
from __future__ import annotations


def build_export_method_overrides(native_exports):
    records = {}
    aliases = {}
    names = {}
    for module_name, exports in native_exports.items():
        module_aliases = {}
        for export_name, info in exports.items():
            if not isinstance(info, dict) or info.get("kind") != "class":
                continue
            owner = info.get("owning_module", module_name)
            class_name = info.get("class_name", export_name)
            key = owner + "." + class_name
            records[key] = info
            module_aliases[export_name] = key
            for name in (export_name, class_name):
                candidates = names.setdefault(name, set())
                candidates.add(key)
        aliases[module_name] = module_aliases

    parents = {}
    for key, info in records.items():
        owner = info.get("owning_module", "")
        local_aliases = aliases.get(owner, {})
        bases = set()
        for base_name in info.get("base_names", ()):
            resolved = local_aliases.get(base_name)
            if resolved is not None:
                bases.add(resolved)
            elif base_name in records:
                bases.add(base_name)
            else:
                # An export can be visible through a re-export before its
                # defining module is present. Keep every compatible alias;
                # ambiguity must not justify a direct base-method call.
                bases.update(names.get(base_name, ()))
        parents[key] = bases

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
    return overrides
