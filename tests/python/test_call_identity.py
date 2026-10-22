"""Semantic identity checks independent of scaffold spellings and ABI tables."""

from dataclasses import replace

from pcc.frontends.python.codegen.call_identity import (
    BindingIdentities,
    ClassIdentity,
    MethodIdentity,
    ReceiverIdentity,
    SymbolIdentity,
    resolve_method_identity,
    resolve_codegen_method_identity,
)


def _class(module, name, method="add", *, bases=(), target=None):
    symbol = SymbolIdentity(module, name)
    definition = MethodIdentity(
        target or SymbolIdentity(module, name + "." + method),
        "user_" + module.replace(".", "_") + "_" + name + "_" + method,
    )
    return ClassIdentity(symbol, (symbol,) + bases, {method: definition})


def _receiver(*classes, nullable=False):
    return ReceiverIdentity(
        tuple(c.symbol for c in classes),
        complete=True,
        lookup_stable=True,
        may_be_none=nullable,
        evidence=("test:verified-construction-and-no-mutation",),
    )


def test_same_class_spelling_does_not_select_ir_provider():
    ir_builder = _class("pcc.ir.ir", "IRBuilder")
    user_builder = _class("application", "IRBuilder")
    classes = {c.symbol: c for c in (ir_builder, user_builder)}
    actual = resolve_method_identity(_receiver(user_builder), "add", classes)
    assert actual.status == "proven"
    assert actual.target == SymbolIdentity("application", "IRBuilder.add")
    assert actual.emitted_symbol == "user_application_IRBuilder_add"


def test_inherited_method_is_proven_only_when_every_runtime_class_agrees():
    base = _class("application", "Base")
    child_symbol = SymbolIdentity("application", "Child")
    child = ClassIdentity(child_symbol, (child_symbol, base.symbol), {})
    classes = {c.symbol: c for c in (base, child)}
    decision = resolve_method_identity(_receiver(base, child), "add", classes)
    assert decision.status == "proven"
    assert decision.target == SymbolIdentity("application", "Base.add")

    child_override = _class("application", "Child", bases=(base.symbol,))
    classes[child.symbol] = child_override
    decision = resolve_method_identity(_receiver(base, child), "add", classes)
    assert decision.status == "unknown"
    assert decision.reason == "runtime receivers resolve different methods"


def test_custom_lookup_field_shadow_and_descriptor_are_not_static_methods():
    base = _class("application", "Builder")
    for unsafe in (
        replace(base, intercepted_lookup=True),
        replace(base, instance_fields=frozenset({"add"})),
        replace(base, descriptors=frozenset({"add"})),
        replace(base, class_attributes=frozenset({"__getattribute__"})),
        replace(base, descriptors=frozenset({"__getattr__"})),
    ):
        decision = resolve_method_identity(
            _receiver(unsafe), "add", {unsafe.symbol: unsafe}
        )
        assert decision.status == "unknown"
        assert decision.target is None


def test_annotation_or_constructor_alone_does_not_prove_stable_lookup():
    cls = _class("application", "Builder")
    exact = _receiver(cls)
    for incomplete in (
        replace(exact, complete=False),
        replace(exact, lookup_stable=False),
        replace(exact, evidence=()),
        replace(exact, may_be_none=True),
    ):
        assert (
            resolve_method_identity(incomplete, "add", {cls.symbol: cls}).status
            == "unknown"
        )


def test_incomplete_mro_cannot_produce_a_direct_call():
    missing = SymbolIdentity("external", "UnknownBase")
    cls = _class("application", "Builder", bases=(missing,))
    result = resolve_method_identity(_receiver(cls), "add", {cls.symbol: cls})
    assert result.status == "unknown"
    assert "incomplete MRO" in result.reason


def test_subclass_non_method_binding_stops_inherited_method_lookup():
    base = _class("application", "Base")
    child_symbol = SymbolIdentity("application", "Child")
    child = ClassIdentity(
        child_symbol,
        (child_symbol, base.symbol),
        {},
        class_attributes=frozenset({"add"}),
    )
    decision = resolve_method_identity(
        _receiver(child), "add", {base.symbol: base, child.symbol: child}
    )
    assert decision.status == "unknown"
    assert decision.target is None


def test_builtin_object_lookup_is_not_a_custom_attribute_hook():
    object_symbol = SymbolIdentity("builtins", "object")
    builtin = ClassIdentity(
        object_symbol,
        (object_symbol,),
        {
            "__getattribute__": MethodIdentity(
                SymbolIdentity("builtins", "object.__getattribute__"),
                "builtin_getattribute",
            )
        },
    )
    child = _class("application", "Builder", bases=(object_symbol,))
    decision = resolve_method_identity(
        _receiver(child), "add", {builtin.symbol: builtin, child.symbol: child}
    )
    assert decision.status == "proven"


def test_local_ir_parameter_shadows_a_module_import_before_any_assignment():
    module = BindingIdentities()
    provider = SymbolIdentity("pcc.ir", "ir")
    module.bind_symbol("ir", provider)
    method = BindingIdentities(module)
    method.shadow("ir")
    assert method.symbol("ir") is None
    assert module.symbol("ir") == provider


def test_importing_ir_does_not_grant_an_unknown_builder_an_identity():
    module = BindingIdentities()
    module.bind_symbol("ir", SymbolIdentity("pcc.ir", "ir"))
    function = BindingIdentities(module)
    function.shadow("builder")
    decision = resolve_method_identity(function.receiver("builder"), "add", {})
    assert decision.status == "unknown"
    assert decision.target is None


def test_ir_specific_method_spellings_still_resolve_user_methods():
    for name in ("as_pointer", "add_case", "add_incoming", "append_basic_block"):
        cls = _class("application", "Builder", method=name)
        decision = resolve_method_identity(_receiver(cls), name, {cls.symbol: cls})
        assert decision.target == SymbolIdentity("application", "Builder." + name)


def test_alias_save_restore_and_unknown_reassignment_preserve_provenance():
    cls = _class("pcc.ir.ir", "IRBuilder")
    scope = BindingIdentities()
    scope.bind_receiver("builder", _receiver(cls))
    scope.assign_alias("saved", "builder")
    scope.shadow("builder")
    assert not scope.receiver("builder").complete
    scope.assign_alias("builder", "saved")
    assert (
        resolve_method_identity(
            scope.receiver("builder"), "add", {cls.symbol: cls}
        ).status
        == "proven"
    )


def test_control_flow_join_keeps_none_and_unknown_paths():
    cls = _class("pcc.ir.ir", "IRBuilder")
    parent = BindingIdentities()
    parent.shadow("builder")
    left, right = BindingIdentities(parent), BindingIdentities(parent)
    left.bind_receiver("builder", _receiver(cls))
    joined = left.join(right)
    assert not joined.receiver("builder").complete
    right.bind_receiver("builder", _receiver(cls, nullable=True))
    joined = left.join(right)
    assert joined.receiver("builder").complete
    assert joined.receiver("builder").may_be_none


def _declared_host():
    from types import SimpleNamespace

    from pcc.ir import ir
    from pcc.frontends.python.codegen.class_gen import ClassInfo
    from pcc.frontends.python.py_ast import ClassDef, Module, SourceSpan

    module = ir.Module(name="application")
    pointer = ir.IntType(8).as_pointer()
    info = ClassInfo("Builder", ir.GlobalVariable(module, pointer, ".Builder"), ())
    info.expanded_cd = ClassDef(
        SourceSpan("application.py", 1, 0, 5, 0), "Builder", (), (), ()
    )
    # Deliberately use an actual non-derived symbol and a mixed machine ABI.
    # Guessing from 'Builder.add' or its Python annotations would be wrong.
    info.methods["add"] = ir.Function(
        module,
        ir.FunctionType(ir.IntType(64), [pointer, pointer, ir.IntType(64)]),
        "actual_declared_collision_suffix",
    )
    host = SimpleNamespace(
        ast_module=Module("application", (info.expanded_cd,)),
        class_lowering=SimpleNamespace(classes={"Builder": info}),
        _class_attr_runtime_state={},
    )
    return host, info


def test_codegen_adapter_uses_real_declaration_symbol_and_machine_slots():
    host, info = _declared_host()
    fact = ReceiverIdentity(
        (SymbolIdentity("application", "Builder"),),
        complete=True,
        lookup_stable=True,
        evidence=("verified-flow",),
    )
    decision = resolve_codegen_method_identity(host, fact, "add")
    assert decision.status == "proven"
    assert decision.emitted_symbol == info.methods["add"].name
    assert decision.abi.parameter_types == tuple(
        str(arg.type) for arg in info.methods["add"].args
    )
    assert decision.abi.return_type == "i64"
    assert decision.abi.parameter_types[-1] == "i64"
    assert tuple(host.class_lowering.classes) == ("Builder",)


def test_codegen_adapter_never_assumes_missing_external_metadata_is_complete():
    host, info = _declared_host()
    info.expanded_cd = None
    info.owning_module = "external"
    fact = ReceiverIdentity(
        (SymbolIdentity("external", "Builder"),),
        complete=True,
        lookup_stable=True,
        evidence=("verified-flow",),
    )
    assert resolve_codegen_method_identity(host, fact, "add").status == "unknown"


def test_codegen_adapter_cannot_hide_an_undeclared_lookup_hook():
    host, info = _declared_host()
    info.methods["__getattribute__"] = None
    fact = ReceiverIdentity(
        (SymbolIdentity("application", "Builder"),),
        complete=True,
        lookup_stable=True,
        evidence=("verified-flow",),
    )
    assert resolve_codegen_method_identity(host, fact, "add").status == "unknown"


def test_codegen_adapter_keeps_inheritance_and_mutation_as_proof_obligations():
    from pcc.frontends.python.py_ast import DynType, Name

    host, info = _declared_host()
    fact = ReceiverIdentity(
        (SymbolIdentity("application", "Builder"),),
        complete=True,
        lookup_stable=True,
        evidence=("verified-flow",),
    )
    host._class_attr_runtime_state[("Builder", "add")] = "deleted"
    assert (
        resolve_codegen_method_identity(host, fact, "add").reason
        == "class lookup has runtime mutations"
    )
    host._class_attr_runtime_state.clear()
    info.expanded_cd = replace(
        info.expanded_cd,
        bases=(Name(info.expanded_cd.span, DynType("dyn"), "object"),),
    )
    # An identifier spelled 'object' is not proof of the builtin binding.
    assert resolve_codegen_method_identity(host, fact, "add").status == "unknown"
