"""C3 constructor snapshots remain owned/healable through GC and cleanup."""
from __future__ import annotations
import ast
from pathlib import Path
import pytest
from pcc.runtime.py import py_abi_constants as abi
from test_exception_constructor_roots import _Memory, _Object, _Slots

PORT = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_class.py"


class _Payload(_Slots):
    def __init__(self, size, name="raw payload"):
        super().__init__(size)
        self.name, self.alive = name, True


class _C3Memory(_Memory):
    def __init__(self, force=False, fail=None, publish_move=False):
        self.raw, self.spans, self.handles = [], [], {}
        self.reads, self.force, self.fail_kind = 0, force, fail
        self.doc_writes = []
        self.publish_move=publish_move
        super().__init__("unused")
        self.maps.update({"pcc_class_construct_owned_frame_map":5,
                          "pcc_class_construct_borrowed_frame_map":-2})
        self.root = self.klass("object", [])
        self.builtin_classes = {"pcc_type_cls_object": self.root,
                                "py_object_root_cache": self.root}
        self.parents = []
        self.namespace.update({name:getattr(abi,name) for name in dir(abi) if name.isupper()})
        self.namespace.update({
            "malloc":self.malloc, "free":self.free, "memcpy":self.memcpy,
            "cstr":lambda value:value,
            "load_i64":lambda value,offset:self.read(value,offset) or 0,
            "load_i32":lambda value,offset:self.read(value,offset) or 0,
            "pcc_gc_note_relocation_read":self.resolve,
            "pcc_gc_scheduler_root_register_handle":self.register,
            "pcc_gc_scheduler_root_unregister_handle":self.unregister,
            "pcc_gc_backend4_zpage_register_owner_payload_span":lambda owner,buffer,size:self.spans.append((owner,buffer,size)),
            "py_tuple_new":self.tuple_new, "py_tuple_set_item":self.tuple_set,
            "py_tuple_len":lambda value:self.read(value,16),
            "py_err_occurred":lambda:0,
            "ptr_eq":lambda first,second:first is second,
            "_alloc_user_tag":lambda:1000, "_object_root":lambda:self.root,
            "atomic_rmw_i32":self.rmw,
            # Model managed-pointer provenance and canonical builtin cache
            # identity; the actual runtime helpers perform the MRO lookup.
            "pcc_gc_pointer_is_managed":lambda value:isinstance(value, _Object) and value.alive,
            "global_load_ptr":lambda name:self.builtin_classes.get(name, self.none),
            "py_class_write_namespace_slots":self.write_namespace,
        })
        parsed=ast.parse(PORT.read_text(),filename=str(PORT))
        functions=[node for node in parsed.body if isinstance(node,ast.FunctionDef)
                   and (node.name in {"py_class_new", "py_class_is_str_subclass", "py_class_is_tuple_subclass",
                                      "_ptr_is_class", "_ptr_can_have_header", "_class_is_structseq"}
                        or node.name.startswith("_class_construct_"))]
        functions[:0] = [node for node in parsed.body if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == "_STRUCTSEQ_CLASS_FLAG"
                                 for target in node.targets)]
        exec(compile(ast.Module(body=functions,type_ignores=[]),str(PORT),"exec"),self.namespace)
        dispatch = PORT.with_name("py_obj_ops_dispatch.py")
        parsed = ast.parse(dispatch.read_text(), filename=str(dispatch))
        functions = [node for node in parsed.body if isinstance(node, ast.FunctionDef)
                     and node.name == "py_builtin_type_class_tag"]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(dispatch), "exec"), self.namespace)

    def malloc(self,size):
        if self.fail_kind=="raw": return None
        value=_Payload(size)
        self.raw.append(value)
        return value
    def free(self,value):
        if value is None:return
        assert value.alive
        value.alive=False
    def memcpy(self,destination,source,size):
        # Raw byte copies (the class's own name) carry no managed references.
        assert destination.alive and len(destination.fields)*8>=size
        destination.name=source
        return destination
    def read(self,value,offset):
        if isinstance(value,_Payload):assert value.alive, "read from retired raw MRO payload"
        return super().read(value,offset)
    def write(self,value,offset,item):
        if isinstance(value,_Payload):assert value.alive
        super().write(value,offset,item)
    def klass(self,name,tail):
        value=self.make(abi.PY_TYPE_CLASS,1)
        value.name=name
        payload=self.malloc((1+len(tail))*8)
        payload.name=name+" MRO"
        payload.fields={i*8:item for i,item in enumerate([value]+tail)}
        value.fields.update({40:1+len(tail),48:payload})
        return value
    def tuple_new(self,count):
        if self.fail_kind=="tuple":return None
        value=self.make(abi.PY_TYPE_TUPLE,16384)
        value.fields={16:count,**{24+i*8:None for i in range(count)}}
        return value
    def tuple_set(self,value,index,item):
        self.incref(item)
        value.fields[24+index*8]=item
        if all(value.fields.get(24+i*8) is not None for i in range(value.fields[16])):
            value.flags &= ~16384
    def register(self,slot):
        handle=object()
        self.handles[handle]=slot
        return handle
    def unregister(self,handle):del self.handles[handle]
    def write_namespace(self, class_slot, name, value_slot, remove):
        # Model only the existing owning-slot writer boundary. The native
        # docstring regression checks the real namespace implementation.
        assert remove == 0 and name == "__doc__"
        for slot in (class_slot, value_slot):
            base, _ = self.pointer(slot)
            assert id(base) in self.frames
        target, offset = self.pointer(value_slot)
        assert self.read(target, offset) is self.none
        self.doc_writes.append(name)
        return -1 if self.fail_kind == "doc" else 0
    def frame_enter(self,frame_map,slots):
        assert frame_map in(5,-2)
        self.frames[id(slots)]=slots
    def rmw(self,operation,value,offset,operand,_order):
        assert value.alive and offset==12
        old=value.flags
        value.flags=value.flags & operand if operation=="and" else value.flags | operand
        return old
    def resolve(self,value):
        self.reads+=1
        if self.force and self.reads==3:
            old=self.parents[0]
            replacement=self.make(old.tag,old.flags)
            replacement.name=old.name
            replacement.fields=old.fields.copy()
            before=old.fields[48]
            after=self.malloc(len(before.fields)*8)
            after.fields={offset:replacement if item is old else item for offset,item in before.fields.items()}
            replacement.fields[48]=after
            for slots in self.frames.values():
                for offset,item in list(slots.fields.items()):
                    if item is old:slots.fields[offset]=replacement
            for slot in self.handles.values():
                target,offset=self.pointer(slot)
                if self.read(target,offset) is old:self.write(target,offset,replacement)
            for obj in self.objects:
                if obj.alive:obj.fields={offset:replacement if item is old else item for offset,item in obj.fields.items()}
            for owner,buffer,size in self.spans:
                if buffer.alive:
                    for offset in range(0,size,8):
                        if buffer.fields.get(offset) is old:buffer.fields[offset]=replacement
            old.alive=before.alive=False
            self.parents[0]=replacement
            self.moves+=1
            return replacement if value is old else value
        return value
    def decref(self,value):
        if isinstance(value,_Object) and value.alive and value.tag==abi.PY_TYPE_CLASS and not value.flags&1 and value.references==1:
            for offset in(32,48,64,80):
                raw=value.fields.get(offset)
                if isinstance(raw,_Payload) and raw.alive:self.free(raw)
        super().decref(value)
    def publish(self,value):
        super().publish(value)
        if not self.publish_move:return
        replacement=self.make(value.tag,value.flags)
        replacement.references,replacement.fields=value.references,value.fields.copy()
        for offset in(32,48):
            before=value.fields.get(offset)
            if isinstance(before,_Payload):
                after=self.malloc(len(before.fields)*8)
                after.fields={position:replacement if item is value else item for position,item in before.fields.items()}
                replacement.fields[offset]=after
                before.alive=False
        for slots in self.frames.values():
            for offset,item in list(slots.fields.items()):
                if item is value:slots.fields[offset]=replacement
        value.alive=False
        self.moves+=1
    def construct(self,parents):
        self.parents=parents
        source=self.malloc(len(parents)*8)
        source.fields={i*8:value for i,value in enumerate(parents)}
        result=self.namespace["py_class_new"]("child",source,len(parents),None,0)
        self.free(source)
        return result


def test_class_constructor_reloads_mro_after_payload_relocation():
    memory=_C3Memory(force=True)
    parent=memory.klass("parent",[memory.root])
    result=memory.construct([parent])
    assert result.alive and result.fields[48].fields[8] is memory.parents[0]
    assert memory.moves==1
    assert not memory.frames and not memory.handles and memory.pin_metric==0


def test_class_constructor_c3_diamond_order_and_identity():
    memory=_C3Memory()
    a=memory.klass("A",[memory.root])
    b=memory.klass("B",[a,memory.root])
    c=memory.klass("C",[a,memory.root])
    result=memory.construct([b,c])
    values=list(result.fields[48].fields.values())
    assert values==[result,b,c,a,memory.root]
    assert not memory.frames and not memory.handles and memory.pin_metric==0


def test_class_constructor_inconsistent_mro_releases_unpublished_owner():
    memory=_C3Memory()
    a=memory.klass("A",[memory.root])
    b=memory.klass("B",[memory.root])
    x=memory.klass("X",[a,b,memory.root])
    y=memory.klass("Y",[b,a,memory.root])
    existing={id(obj) for obj in memory.objects}
    assert memory.construct([x,y]) is None
    assert not memory.frames and not memory.handles and memory.pin_metric==0
    assert all(not obj.alive for obj in memory.objects if id(obj) not in existing)
    assert x.alive and y.alive and a.alive and b.alive


def test_class_constructor_tuple_allocation_failure_keeps_bases_and_retires_class():
    memory=_C3Memory()
    parent=memory.klass("parent",[memory.root])
    existing={id(obj) for obj in memory.objects}
    memory.fail_kind="tuple"
    assert memory.construct([parent]) is None
    assert parent.alive and parent.references==1
    assert not memory.frames and not memory.handles and memory.pin_metric==0
    assert all(not obj.alive for obj in memory.objects if id(obj) not in existing)


def test_class_constructor_field_table_allocation_failure_retires_class():
    memory = _C3Memory()
    existing = {id(obj) for obj in memory.objects}
    field_names = _Payload(abi.C_POINTER_SIZE)
    field_names.fields[0] = "value"
    allocate = memory.namespace["malloc"]
    memory.namespace["malloc"] = lambda size: None if size == abi.C_POINTER_SIZE else allocate(size)
    result = memory.namespace["py_class_new"]("has_field", None, 0, field_names, 1)
    assert result is None
    assert field_names.alive
    assert not memory.frames and not memory.handles and memory.pin_metric == 0
    assert all(not obj.alive for obj in memory.objects if id(obj) not in existing)


def test_class_constructor_movement_at_publish_keeps_self_mro_and_handoff_owner():
    memory=_C3Memory(publish_move=True)
    parent=memory.klass("parent",[memory.root])
    result=memory.construct([parent])
    assert result.alive and result.references==1
    assert result.fields[48].fields[0] is result
    assert result.fields[48].fields[8] is parent
    assert memory.moves==1 and not memory.frames and not memory.handles and memory.pin_metric==0


def test_class_constructor_zero_bases_appends_object_root():
    memory=_C3Memory()
    result=memory.construct([])
    assert list(result.fields[48].fields.values())==[result,memory.root]
    assert not memory.frames and not memory.handles and memory.pin_metric==0
    assert memory.doc_writes == ["__doc__"]


def test_class_constructor_doc_write_failure_retires_unpublished_class():
    memory = _C3Memory(fail="doc")
    existing = {id(obj) for obj in memory.objects}
    assert memory.construct([]) is None
    assert memory.doc_writes == ["__doc__"]
    assert not memory.frames and not memory.handles and memory.pin_metric == 0
    assert all(not obj.alive for obj in memory.objects if id(obj) not in existing)


def test_class_constructor_temporary_snapshots_balance_nonimmortal_base_references():
    memory=_C3Memory()
    parent=memory.klass("parent",[memory.root])
    parent.flags=0
    result=memory.construct([parent])
    assert result.alive and parent.alive and parent.references==1
    assert result.fields[48].fields[8] is parent
    assert not memory.frames and not memory.handles and memory.pin_metric==0


@pytest.mark.parametrize("kind", ["builtin", "inherited", "name_only"])
@pytest.mark.parametrize("publish_move", [False, True])
def test_class_constructor_str_payload_uses_c3_builtin_identity(kind, publish_move):
    memory = _C3Memory(publish_move=publish_move)
    builtin = memory.klass("str", [memory.root])
    memory.builtin_classes["pcc_type_cls_str"] = builtin
    if kind == "builtin":
        parent = builtin
    elif kind == "inherited":
        parent = memory.klass("Text", [builtin, memory.root])
    else:
        parent = memory.klass("str", [memory.root])
    result = memory.construct([parent])
    is_str = kind != "name_only"
    assert memory.namespace["py_class_is_str_subclass"](result) == is_str
    base_size = abi.PYINSTANCEOBJECT_SIZE + abi.C_POINTER_SIZE
    assert result.fields[abi.PYCLASSOBJECT_INSTANCE_SIZE_OFFSET] == base_size + (abi.C_POINTER_SIZE if is_str else 0)
    assert result.fields[abi.PYCLASSOBJECT_MRO_OFFSET].fields[0] is result
    assert result.fields[abi.PYCLASSOBJECT_MRO_OFFSET].fields[abi.C_POINTER_SIZE] is parent
    assert result.alive and result.references == 1
    assert memory.moves == int(publish_move)
    assert not memory.frames and not memory.handles and memory.pin_metric == 0


def test_class_constructor_sensitive_c3_exception_shape_reaches_owned_emitter(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    from pcc.backend.owned_object_emit import emit_owned_object
    source=tmp_path/"c3_exception.py"
    source.write_text("""class A:
    pass
class B(A):
    pass
class C(A):
    pass
class D(B,C):
    pass
class E(Exception):
    pass
class F(E):
    pass
def main():
    assert isinstance(D(),A)
    try:
        raise F("payload")
    except E as error:
        assert isinstance(error,Exception)
main()
""")
    output=tmp_path/"c3_exception.ll"
    target_triple = "arm64-apple-darwin"
    compile_python(
        str(source),
        str(output),
        emit_llvm_only=True,
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        target_triple=target_triple,
    )
    object_bytes = emit_owned_object(output.read_text(), target_triple)
    assert len(object_bytes) > 64
    assert object_bytes[:4] == b"\xcf\xfa\xed\xfe"
    assert int.from_bytes(object_bytes[4:8], "little") == 0x0100000C


@pytest.mark.parametrize("triple", ["arm64-apple-darwin", "aarch64-unknown-linux-gnu",
                                    "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"])
def test_class_constructor_roots_reach_owned_emitter(tmp_path,monkeypatch,triple):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.owned_runtime_build import runtime_ir_passes
    from pcc.frontends.python.pipeline import compile_python
    source=tmp_path/"py_runtime_class"/"py"/"py_class.py"
    source.parent.mkdir(parents=True)
    source.write_text(PORT.read_text())
    output=tmp_path/"py_class.ll"
    monkeypatch.setenv("PCC_WITH_THREADS","1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES",runtime_ir_passes(str(PORT.parent.parent)))
    compile_python(str(source),str(output),emit_llvm_only=True,python_library=True,
                   backend="self",libpython_mode="off",ir_scaffold_mode="on",target_triple=triple)
    text=output.read_text()
    assert "@pcc_class_construct_owned_frame_map" in text
    assert "@pcc_class_construct_borrowed_frame_map" in text
    assert "@user_py_class__class_construct_snapshot(" in text
    payload=emit_owned_object(text,triple)
    assert len(payload)>64
    (tmp_path/"py_class.o").write_bytes(payload)


@pytest.mark.parametrize("kind", ["builtin", "inherited", "name_only"])
@pytest.mark.parametrize("publish_move", [False, True])
def test_class_constructor_tuple_payload_uses_c3_builtin_identity(kind, publish_move):
    memory = _C3Memory(publish_move=publish_move)
    builtin = memory.klass("tuple", [memory.root])
    memory.builtin_classes["pcc_type_cls_tuple"] = builtin
    if kind == "builtin":
        parent = builtin
    elif kind == "inherited":
        parent = memory.klass("TupleChild", [builtin, memory.root])
    else:
        parent = memory.klass("tuple", [memory.root])
    result = memory.construct([parent])
    is_tuple = kind != "name_only"
    assert memory.namespace["py_class_is_tuple_subclass"](result) == is_tuple
    base_size = abi.PYINSTANCEOBJECT_SIZE + abi.C_POINTER_SIZE
    assert result.fields[abi.PYCLASSOBJECT_INSTANCE_SIZE_OFFSET] == base_size + (abi.C_POINTER_SIZE if is_tuple else 0)
    assert result.alive and result.references == 1
    assert memory.moves == int(publish_move)
    assert not memory.frames and not memory.handles and memory.pin_metric == 0
