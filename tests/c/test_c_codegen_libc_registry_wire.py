from __future__ import annotations


def test_declarative_libc_registry_feeds_real_c_codegen():
    from pcc.frontends.c.c_libc_registry import LibcSignature, register_signature
    from pcc.frontends.c.codegen import c_codegen

    register_signature(
        LibcSignature(
            "__pcc_registry_probe",
            "int",
            ("const char*", "size_t"),
            "probe.h",
            ("linux", "darwin"),
            "test",
        ),
        replace=True,
    )

    count = c_codegen.refresh_libc_registry_from_declarative()
    assert count > 0
    ret_ty, arg_tys, var_arg = c_codegen.LIBC_FUNCTIONS["__pcc_registry_probe"]
    assert ret_ty == c_codegen.int32_t
    assert arg_tys == [c_codegen.cstring, c_codegen.int64_t]
    assert var_arg is False


def test_declarative_open_signature_overrides_builtin_vararg_shape():
    from pcc.frontends.c.codegen import c_codegen

    ret_ty, arg_tys, var_arg = c_codegen.LIBC_FUNCTIONS["open"]
    assert ret_ty == c_codegen.int32_t
    assert arg_tys[:2] == [c_codegen.cstring, c_codegen.int32_t]
    assert var_arg is True


def test_errno_location_is_not_duplicated_in_declarative_registry():
    from pcc.frontends.c.c_libc_registry import iter_signatures

    linux = [s for s in iter_signatures("linux") if s.name == "__errno_location"]
    darwin = [s for s in iter_signatures("darwin") if s.name == "__errno_location"]
    assert len(linux) == 1
    assert darwin == []


def test_declarative_libc_names_have_no_legacy_codegen_shadow():
    from pcc.frontends.c.c_libc_registry import iter_signatures
    from pcc.frontends.c.codegen import c_codegen

    declarative_names = {sig.name for sig in iter_signatures()}
    assert c_codegen.libc_registry_shadow_names() == ()
    assert declarative_names.isdisjoint(c_codegen._LEGACY_LIBC_FUNCTIONS)
    assert declarative_names <= c_codegen.LIBC_FUNCTIONS.keys()
