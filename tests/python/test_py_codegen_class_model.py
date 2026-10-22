from types import SimpleNamespace

import pytest

from pcc.frontends.python.codegen.class_alias_lowering import ClassAliasLoweringMixin
from pcc.frontends.python.codegen.class_model_lowering import ClassModelLoweringMixin
from pcc.frontends.python.py_ast import ClassType, Name


class _ReceiverProbe(ClassModelLoweringMixin):
    def __init__(self):
        self.env = {}
        self.current_class = None
        self.registered_types = []

    def _ensure_class_type_registered(self, ty):
        self.registered_types.append((ty.module, ty.name))
        return ty.name


class _ClassLoweringProbe:
    def __init__(self):
        self.classes = {}
        self.declared = []

    def declare_extern_class(
        self,
        *,
        owning_module,
        class_name,
        field_names,
        methods,
        local_name,
        field_types=(),
    ):
        self.declared.append((owning_module, class_name, local_name))
        info = SimpleNamespace(
            name=local_name,
            owning_module=owning_module,
            field_names=list(field_names),
            field_types=tuple(field_types),
            bases_ast=(),
            methods={m["name"]: object() for m in methods},
            method_kinds={m["name"]: m.get("kind", "instance") for m in methods},
        )
        self.classes[local_name] = info
        return info


class _ClassExportProbe(ClassModelLoweringMixin):
    def __init__(self):
        self.class_lowering = _ClassLoweringProbe()
        self._class_type_export_cache = {}
        self._native_module_exports = {
            "pkg.base": {
                "Base": {
                    "kind": "class",
                    "owning_module": "pkg.base",
                    "class_name": "Base",
                    "field_names": (),
                    "methods": {},
                    "base_names": (),
                }
            },
            "pkg.child": {
                "Child": {
                    "kind": "class",
                    "owning_module": "pkg.child",
                    "class_name": "Child",
                    "field_names": ("value",),
                    "methods": (
                        {
                            "name": "build",
                            "kind": "static",
                            "param_types": (),
                            "return_ty": ("dyn",),
                        },
                    ),
                    "base_names": ("Base",),
                }
            },
        }


class _ClassHintProbe(_ClassExportProbe, ClassAliasLoweringMixin):
    def __init__(self):
        super().__init__()
        self.current_class = None
        self.env = {}
        self.env_class_hint = {}
        self._class_aliases = {}
        self.ast_module = SimpleNamespace(name="pcc.frontends.python.py_lift")


@pytest.mark.parametrize("name,fields", [
    ("_Call", ("func", "args", "line")),
    ("_ClassDef", ("name", "bases", "body", "decorators", "line")),
])
@pytest.mark.parametrize("owner", ["pcc.frontends.python.py_parse", "other.parser"])
def test_underscore_class_hint_keeps_its_declared_module(name, fields, owner):
    probe = _ClassHintProbe()
    probe.class_lowering.classes[name[1:]] = SimpleNamespace(
        name=name[1:], owning_module="pcc.frontends.python.py_ast",
    )
    probe._native_module_exports[owner] = {
        name: {"kind": "class", "owning_module": owner,
               "class_name": name, "field_names": fields,
               "methods": (), "base_names": ()},
    }
    expr = Name(span=None, ty=ClassType(name, owner, (), ()), ident="node")

    hint = probe._class_hint_for_expr(expr)

    assert hint == name
    info = probe.class_lowering.classes[hint]
    assert info.owning_module == owner
    assert tuple(info.field_names) == fields


def test_class_hint_keeps_a_real_bound_alias():
    probe = _ClassHintProbe()
    probe._class_aliases["AstAlias"] = "AstCall"
    probe.class_lowering.classes["AstCall"] = SimpleNamespace(
        name="AstCall", owning_module="pcc.frontends.python.py_ast",
    )
    expr = Name(span=None, ty=ClassType("AstAlias", "", (), ()), ident="node")

    assert probe._class_hint_for_expr(expr) == "AstCall"


def test_self_receiver_class_name_prefers_inferred_receiver_over_lexical_class():
    probe = _ReceiverProbe()
    probe.current_class = SimpleNamespace(name="ClassModelLoweringMixin")
    probe.env["self"] = (
        object(),
        object(),
        ClassType(
            name="L1CodeGen",
            module="pcc.frontends.python.codegen.layer1",
            fields=(),
            bases=(),
        ),
    )

    assert probe._self_receiver_class_name() == "L1CodeGen"
    assert probe.registered_types == [
        ("pcc.frontends.python.codegen.layer1", "L1CodeGen")
    ]


def test_self_receiver_class_name_uses_current_class_only_as_fallback():
    probe = _ReceiverProbe()
    probe.current_class = SimpleNamespace(name="ClassModelLoweringMixin")

    assert probe._self_receiver_class_name() == "ClassModelLoweringMixin"
    assert probe.registered_types == []


def test_ensure_class_type_registered_uses_cached_native_export_lookup():
    probe = _ClassExportProbe()

    assert (
        probe._ensure_class_type_registered(
            ClassType(
                name="Child",
                module="pkg.child",
                fields=(),
                bases=(),
            )
        )
        == "Child"
    )
    assert (
        probe._ensure_class_type_registered(
            ClassType(
                name="Child",
                module="pkg.child",
                fields=(),
                bases=(),
            )
        )
        == "Child"
    )

    assert probe.class_lowering.declared == [
        ("pkg.base", "Base", "Base"),
        ("pkg.child", "Child", "Child"),
    ]


def test_resolve_method_mro_caches_positive_string_key_results_only():
    probe = _ClassExportProbe()

    first = probe._resolve_method_mro("Child", "build")
    second = probe._resolve_method_mro("Child", "build")

    assert first is second
    assert getattr(probe, "_method_mro_cache") == {("Child", "build"): first}
    assert probe.class_lowering.declared == [("pkg.child", "Child", "Child")]


def test_resolve_method_mro_does_not_cache_misses():
    probe = _ClassExportProbe()

    assert probe._resolve_method_mro("Child", "missing") is None

    assert not getattr(probe, "_method_mro_cache", {})
