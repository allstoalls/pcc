"""Decode inputs and both dynamic outputs survive fallible owner cleanup."""
import ast
from pathlib import Path
import re

import pytest

from pcc.frontends.python.codegen import string_method_lowering
from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_buffer_factory_ownership import BufferModel
from tests.python.test_foreign_address_leases import _functions
from tests.python.test_lambda_adapter_scope_lifetime import check_frames
from tests.python.test_lambda_constructor_roots import emit


CASES = {
    'typed_default': ('data: bytes', 'data.decode()', False),
    'typed_ascii': ('data: bytes', 'data.decode("ascii")', False),
    'bytearray': ('data: bytearray', 'data.decode("utf-8", "ignore")', False),
    'dynamic': ('data', 'data.decode("ascii")', True),
    'encoding_factory': ('data', 'data.decode(make("utf-8"))', True),
    'errors_factory': ('data', 'data.decode(errors=make("ignore"))', True),
    'keyword_order': ('data', 'data.decode(errors=make("ignore"), encoding=make("utf-8"))', True),
    'receiver_factory': ('data', 'make(data).decode("ascii")', True),
    'late_argument_error': ('data', 'data.decode(make("ascii"), fail())', True),
    'strip_consumer': ('data: bytes', 'data.decode("ascii").strip()', False),
    'original_ar_shape': ('data: bytes', 'int(data[48:58].decode("ascii").strip())', False),
    'tuple_consumer': ('data', '(data.decode("ascii"),)', True),
    'default_consumer': ('data', 'data.decode()', True),
}


def physical_body(body):
    aliases=dict(re.findall(r'(%[-\w.]+) = bitcast ptr (%[-\w.]+) to ptr',body))
    def resolve(match):
        value=match.group(0);seen=set()
        while value in aliases:
            assert value not in seen
            seen.add(value);value=aliases[value]
        return value
    return re.sub(r'%[-\w.]+',resolve,body)


@pytest.mark.parametrize('case',CASES)
def test_bytes_decode_publishes_before_error_check_and_cleanup(case):
    parameter,expression,dynamic=CASES[case]
    tail='    return take(value='+expression+')\n'
    if case=='default_consumer':
        tail='    def kept(value='+expression+'):\n        return value\n    return kept\n'
    text=emit('def take(*, value, **extras):\n    return value\ndef make(value):\n    return value\ndef fail():\n    raise ValueError("later decoder argument")\ndef probe('+parameter+'):\n'+tail)
    body=next(body for body in re.findall(r'^define[^\n]*\{\n.*?^}',text,re.M|re.S) if '@user_lambda_owned_probe(' in body.splitlines()[0])
    body=physical_body(body)
    call=re.search(r'(%[-\w.]+) = call ptr[^\n]*@py_bytes_decode_with_encoding\([^\n]*\n([^\n]+)',body)
    assert call is not None
    publication=re.search(r'store ptr '+re.escape(call.group(1))+r', ptr (%[-\w.]+)',call.group(2))
    assert publication is not None
    output=publication.group(1)
    arguments=re.search(r'@py_bytes_decode_with_encoding\(ptr (%[-\w.]+), ptr (%[-\w.]+), ptr (%[-\w.]+)\)',call.group(0))
    assert arguments is not None
    for value in arguments.groups():
        loaded=re.search(re.escape(value)+r' = load ptr, ptr (%[-\w.]+)',body[:call.start()])
        assert loaded is not None
        slot=loaded.group(1)
        assert re.search(r'@pcc_gc_foreign_lease_acquire\(ptr '+re.escape(slot)+r'\)',body[:call.start()]) is not None
    assert 'dyn.decode.result' not in body
    assert re.search(r'@pcc_gc_store_root\(ptr %decode.receiver[^\n]*ptr null\)',body) is not None
    if dynamic:
        assert re.search(r'@py_obj_call_slots\(ptr [^,]+, ptr [^,]+, ptr [^,]+, ptr '+re.escape(output)+r'\)',body) is not None
        lookup=re.search(r'(%[-\w.]+) = call ptr[^\n]*@py_obj_getattr\([^\n]*\n([^\n]+)',body)
        assert lookup is not None and 'store ptr '+lookup.group(1) in lookup.group(2)


@pytest.mark.parametrize('expression', ['data.decode()', 'data.decode(errors="ignore", encoding="utf-8")', 'make(data).decode(make("ascii"))', 'data.decode(make("ascii"), fail())'])
def test_dynamic_decode_both_branches_have_balanced_error_and_normal_frames(expression):
    text=emit('def make(value):\n    return value\ndef fail():\n    raise ValueError("option")\ndef probe():\n    return lambda data: '+expression+'\n')
    body=next(body for body in re.findall(r'^define[^\n]*\{\n.*?^}',text,re.M|re.S) if re.search(r'@user_lambda_owned__native_lambda_\d+\(',body.splitlines()[0]))
    check_frames(body)


class _DecoderModel(BufferModel):
    """Execute current decoder/codec bodies over the existing moving model.

    The raw scratch allocator follows py_substrate's malloc/free wrappers;
    managed ownership boundaries still relocate every unleased/unpinned object.
    No codec operation is replaced with Python's decoder: CPython is the oracle.
    """
    def __init__(self, kind='bytes', data=b'abc', *, relocate=True, failure=None):
        super().__init__(kind, relocate=relocate, failure=failure)
        self.frames[self.external] = 4
        self.memory[self.external + 16] = None
        self.memory[self.external + 24] = None
        base = self.source
        while base[0].tag == self.ns['PY_TYPE_MEMORYVIEW']:
            base = base[0].base
        base[0].data, base[0].length = bytearray(data), len(data)
        self.cleanup_error = False
        self.ns.update(
            PY_TYPE_STR=abi.PY_TYPE_STR,
            PYSTROBJECT_BYTE_LEN_OFFSET=abi.PYSTROBJECT_BYTE_LEN_OFFSET,
            PYSTROBJECT_DATA_OFFSET=abi.PYSTROBJECT_DATA_OFFSET,
            global_addr=lambda name: -3 if name == 'pcc_text_decode_borrowed_map'
                else -1 if 'borrowed' in name else self.ns['_BUFFER_COUNT'],
            py_str_new=self.str_new,
            py_unicode_decode_error_from_buffer=self.decode_error,
            py_unicode_encode_error=self.encode_error,
        )
        source = Path(string_method_lowering.__file__).parents[3] / 'runtime/py/py_obj_stubs.py'
        tree = ast.parse(source.read_text())
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id.startswith(('_BUFFER_', '_TEXT_SPAN'))):
                self.ns[node.targets[0].id] = ast.literal_eval(node.value)
        _functions(source, {
            '_text_utf8_expected', '_text_decode_step', '_text_decode_failure',
            '_text_decode_record', 'py_text_decode_buffer',
            'py_text_decode_buffer_spans', 'py_text_decode', '_bytes_decode_body',
            '_bytes_decode_guarded', 'py_bytes_decode_with_encoding',
            'py_bytes_decode', 'py_bytes_decode_utf8_ignore',
            'py_bytes_decode_utf8_surrogateescape',
        }, self.ns)
        _functions(source.with_name('py_str_accessors.py'), {
            '_encode_name_equal', '_encode_codec_equal', '_encode_codec_id',
            '_encode_error_id', '_encode_validate_name', '_encode_width',
            '_encode_invalid', '_encode_raise_unicode', '_utf8_ord_at_byte',
            'py_text_codec_id', 'py_text_error_id',
        }, self.ns)

    def load(self, pointer, offset):
        if isinstance(pointer, str):
            return ord(pointer[offset])
        if isinstance(pointer, tuple) and pointer[0].tag == abi.PY_TYPE_STR:
            self.check(pointer)
            absolute = offset + (pointer[2] if len(pointer) == 3 else 0)
            if absolute >= abi.PYSTROBJECT_DATA_OFFSET:
                return pointer[0].data[absolute - abi.PYSTROBJECT_DATA_OFFSET]
        return super().load(pointer, offset)

    def raw_free(self, pointer):
        # This is unmanaged free(), not a managed-owner cleanup callback.
        assert pointer in self.raw, 'scratch double free or unknown allocation'
        del self.raw[pointer]
        self.events.append(('free', pointer))

    def str_new(self, data, length):
        self.boundary()
        if self.failure == 'string-allocation':
            self.raise_owned(self.error(19, 'string allocation failed'))
            return None
        result = self.new('decoded', abi.PY_TYPE_STR, fresh=True)
        result[0].data = bytearray(self.load(data, index) for index in range(length))
        result[0].length = length
        return result

    def decode_error(self, data, length, encoding, start, end, reason):
        payload = bytes(self.load(data, index) for index in range(length))
        error = self.error('UnicodeDecodeError', reason)
        error[0].details = (encoding, payload, start, end, reason)
        self.raise_owned(error)

    def encode_error(self, value, encoding, start, end, reason):
        payload = self.content(value).decode('utf-8', 'surrogatepass')
        error = self.error('UnicodeEncodeError', reason)
        error[0].details = (encoding, payload, start, end, reason)
        self.raise_owned(error)

    def acquire(self, slot):
        if self.failure == 'decode-lease':
            self.boundary()
            value = self.load(slot, 0)
            if value is not None and value[0].name == 'decoded':
                return -1
        return super().acquire(slot)

    def drop(self, value):
        super().drop(value)
        if (self.cleanup_error and value is not None and value[0].refs == 0
                and value[0].name in ('result', 'decoded')):
            self.cleanup_error = False
            self.raise_owned(self.error(99, 'cleanup callback error'))

    def name(self, value):
        if value is None:
            return None
        result = self.new('name', abi.PY_TYPE_STR if isinstance(value, str) else 1)
        if isinstance(value, str):
            result[0].data = bytearray(value.encode('utf-8', 'surrogatepass'))
            result[0].length = len(result[0].data)
        return result

    def run_decode(self, encoding='utf-8', errors='strict', *, symbol='py_bytes_decode_with_encoding'):
        self.store(self.external, 16, self.name(encoding))
        self.store(self.external, 24, self.name(errors))
        arguments = [self.load(self.external, 0)]
        if symbol == 'py_bytes_decode_with_encoding':
            arguments.extend((self.load(self.external, 16), self.load(self.external, 24)))
        result = self.ns[symbol](*arguments)
        self.store(self.external, 8, result)
        self.boundary()
        assert not self.raw and not self.leases and not self.pins
        assert self.frames == {self.external: 4}
        result = self.load(self.external, 8)
        if result is not None:
            assert result[0].name == 'decoded' and result[0].refs == 1
        for offset in (0, 16, 24):
            value = self.load(self.external, offset)
            if value is not None:
                assert value[0].refs == 1, 'borrowed input owner changed'
        return result

    def close(self):
        for offset in (0, 8, 16, 24):
            self.clear_root(self.external + offset, None)
        self.clear_error()
        assert all(obj.refs == 0 for obj in self.objects), 'decoder owner leaked'


@pytest.mark.parametrize('kind', ('bytes', 'bytearray', 'memoryview', 'nested-view'))
@pytest.mark.parametrize('encoding,errors,data', (
    ('ascii', 'strict', b'abc'), ('ascii', 'ignore', b'A\xffB'),
    ('utf-8', 'strict', b'A\xc3\xa9\xf0\x9f\x99\x82'),
    ('utf-8', 'ignore', b'A\xffB'),
    ('utf-8', 'surrogateescape', b'A\x80\xffZ'),
    ('latin-1', 'strict', b'A\xffZ'), ('utf-8', 'replace', b'A\xffZ'),
    ('utf-8', 'surrogatepass', b'\xed\xa0\x80'),
    ('ascii', 'backslashreplace', b'A\xffZ'),
    ('UTF_8', 'strict', b'abc'), ('iso-8859-1', 'strict', b'\xe9'),
    ('utf-8', 'unknown', b'abc'),
))
def test_actual_decoder_body_new_return_and_existing_capability_dispatch(kind, encoding, errors, data):
    model = _DecoderModel(kind, data)
    result = model.run_decode(encoding, errors)
    assert model.pending is None and result is not None
    assert model.content(result).decode('utf-8', 'surrogatepass') == data.decode(encoding, errors)
    assert any(obj.generation for obj in model.objects), 'relocation control did not run'
    model.close()


@pytest.mark.parametrize('encoding,errors,data', (
    ('ascii', 'strict', b'A\xffZ'), ('utf-8', 'strict', b'A\xffZ'),
    ('utf-8', 'strict', b'A\xe2\x82'), ('utf-8', 'strict', b'A\xe2XZ'),
    ('utf-8', 'surrogatepass', b'A\xffZ'),
))
def test_decoder_unicode_errors_preserve_exact_ranges_and_free_scratch(encoding, errors, data):
    model = _DecoderModel('bytearray', data)
    assert model.run_decode(encoding, errors) is None
    with pytest.raises(UnicodeDecodeError) as caught:
        data.decode(encoding, errors)
    assert model.pending[0].kind == 'UnicodeDecodeError'
    assert model.pending[0].details == caught.value.args
    model.close()


@pytest.mark.parametrize('kind,encoding,errors,data,code', (
    ('count', 'ascii', 'strict', b'abc', 3),
    ('bytes', 'unknown', 'strict', b'abc', 13),
    ('bytes', 7, 'strict', b'abc', 3),
    ('bytes', 'utf-8', 7, b'abc', 3),
    ('bytes', 'utf\0-8', 'strict', b'abc', 2),
    ('bytes', 'utf-8', 'strict\0', b'abc', 2),
    ('bytes', 'utf-8', 'unknown', b'\xff', 13),
    # Encode-only handlers fail a decode with CPython's TypeError.
    ('bytes', 'utf-8', 'xmlcharrefreplace', b'\xff', 3),
    ('bytes', 'utf-8', 'namereplace', b'\xff', 3),
))
def test_decoder_validation_and_explicit_unsupported_handlers(kind, encoding, errors, data, code):
    model = _DecoderModel(kind, data)
    assert model.run_decode(encoding, errors) is None
    assert model.pending[0].kind == code
    model.close()


@pytest.mark.parametrize('failure', ('copy', 'allocation', 'raw-allocation', 'string-allocation', 'decode-lease'))
def test_decoder_partial_failure_balances_every_owner_and_preserves_error(failure):
    model = _DecoderModel('bytearray', b'abc', failure=failure)
    model.cleanup_error = True
    assert model.run_decode() is None
    assert model.pending[0].kind == 19
    assert model.pending[0].message != 'cleanup callback error'
    if failure in ('raw-allocation', 'string-allocation', 'decode-lease'):
        assert not model.cleanup_error, 'cleanup callback control did not run'
    model.close()


@pytest.mark.parametrize('symbol,errors', (
    ('py_bytes_decode', 'strict'), ('py_bytes_decode_utf8_ignore', 'ignore'),
    ('py_bytes_decode_utf8_surrogateescape', 'surrogateescape'),
))
def test_decoder_legacy_exports_keep_the_same_owned_guard(symbol, errors):
    data = b'abc' if errors == 'strict' else b'A\xffZ'
    model = _DecoderModel('memoryview', data)
    result = model.run_decode(symbol=symbol)
    assert model.pending is None
    assert model.content(result).decode('utf-8', 'surrogatepass') == data.decode('utf-8', errors)
    model.close()


def test_decoder_model_rejects_parking_before_new_result_publication():
    model = _DecoderModel()
    decode = model.ns['py_text_decode']
    def late_publish(*args):
        result = decode(*args)
        model.boundary()
        return result
    model.ns['py_text_decode'] = late_publish
    with pytest.raises(AssertionError, match='parking call before NEW result publication'):
        model.run_decode()


def test_bytes_decode_reference_binding_and_error_behavior():
    events=[]
    class Decoder:
        @property
        def decode(self):
            events.append('lookup')
            def invoke(*args,**kwargs):
                events.append(('invoke',args,kwargs));return 'done'
            return invoke
    def make(label,value):events.append(label);return value
    assert make('receiver',Decoder()).decode(make('encoding','ascii'),errors=make('errors','strict'))=='done'
    assert events==['receiver','lookup','encoding','errors',('invoke',('ascii',),{'errors':'strict'})]
    assert bytearray(b' 42 ').decode('ascii').strip()=='42'
    assert b' 42 '.decode(errors='ignore',encoding='utf-8').strip()=='42'
    with pytest.raises(TypeError):b'abc'.decode(None)
