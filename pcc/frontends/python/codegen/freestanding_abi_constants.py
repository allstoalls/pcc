"""Generated freestanding ABI constants. Do not edit by hand."""

ABI_CONSTANTS: dict[str, int] = {
    'object.bytearray.byte_len_offset': 16,
    'object.bytearray.data_offset': 24,
    'object.bytes.byte_len_offset': 16,
    'object.bytes.data_offset': 24,
    'object.class.attrs_offset': 104,
    'object.class.bases_offset': 32,
    'object.class.del_method_offset': 96,
    'object.class.field_names_offset': 80,
    'object.class.instance_size_offset': 88,
    'object.class.metaclass_offset': 112,
    'object.class.methods_offset': 64,
    'object.class.mro_offset': 48,
    'object.class.n_bases_offset': 24,
    'object.class.n_fields_offset': 72,
    'object.class.n_methods_offset': 56,
    'object.class.n_mro_offset': 40,
    'object.class.name_offset': 16,
    'object.class.size': 120,
    'object.class.type_tag_alloc_offset': 92,
    'object.class_method.func_offset': 8,
    'object.class_method.name_hash_offset': 16,
    'object.class_method.name_length_offset': 20,
    'object.class_method.name_offset': 0,
    'object.class_method.size': 24,
    'object.classmethod.func_offset': 16,
    'object.classmethod.size': 24,
    'object.complex.imag_offset': 24,
    'object.complex.real_offset': 16,
    'object.dict.capacity_offset': 24,
    'object.dict.entries_offset': 40,
    'object.dict.entries_used_offset': 48,
    'object.dict.indices_offset': 32,
    'object.dict.item_count_offset': 16,
    'object.dict.size': 56,
    'object.dict_entry.hash_offset': 0,
    'object.dict_entry.key_offset': 8,
    'object.dict_entry.size': 24,
    'object.dict_entry.value_offset': 16,
    'object.flag.gc_tracked': 2,
    'object.float.value_offset': 16,
    'object.header.flags_offset': 12,
    'object.header.refcount_offset': 0,
    'object.header.size': 16,
    'object.header.type_tag_offset': 8,
    'object.instance.cls_offset': 16,
    'object.instance.fields_offset': 24,
    'object.instance.size': 24,
    'object.int.digits_offset': 24,
    'object.int.ndigits_offset': 20,
    'object.int.sign_offset': 16,
    'object.list.capacity_offset': 24,
    'object.list.items_offset': 32,
    'object.list.length_offset': 16,
    'object.list.size': 40,
    'object.memoryview.base_offset': 16,
    'object.pointer.size': 8,
    'object.property.fdel_offset': 32,
    'object.property.fget_offset': 16,
    'object.property.fset_offset': 24,
    'object.property.size': 40,
    'object.staticmethod.func_offset': 16,
    'object.staticmethod.size': 24,
    'object.str.byte_len_offset': 16,
    'object.str.cp_len_offset': 24,
    'object.str.data_offset': 40,
    'object.str.hash_offset': 32,
    'object.str.size': 40,
    'object.tuple.items_offset': 24,
    'object.tuple.len_offset': 16,
    'object.tuple.length_offset': 16,
    'object.tuple.size': 24,
    'object.type.bool': 1,
    'object.type.bytearray': 18,
    'object.type.bytes': 17,
    'object.type.cext_tag_base': 65536,
    'object.type.class': 10,
    'object.type.classmethod': 102,
    'object.type.complex': 16,
    'object.type.continuation': 29,
    'object.type.coroutine': 20,
    'object.type.cpy_handle': 32,
    'object.type.dict': 6,
    'object.type.ellipsis': 33,
    'object.type.exc': 12,
    'object.type.file': 13,
    'object.type.float': 3,
    'object.type.func': 9,
    'object.type.gen': 15,
    'object.type.instance': 11,
    'object.type.int': 2,
    'object.type.iter': 14,
    'object.type.list': 5,
    'object.type.memoryview': 19,
    'object.type.none': 0,
    'object.type.notimplemented': 34,
    'object.type.property': 101,
    'object.type.set': 8,
    'object.type.staticmethod': 103,
    'object.type.str': 4,
    'object.type.task': 28,
    'object.type.thread': 27,
    'object.type.thread_condition': 25,
    'object.type.thread_event': 24,
    'object.type.thread_lock': 22,
    'object.type.thread_rlock': 23,
    'object.type.thread_semaphore': 26,
    'object.type.tuple': 7,
    'object.type.user': 100,
    'object.type.user_class_start': 104,
    'object.type.valuebox': 200,
    'object.type.virtual_thread': 30,
    'object.type.vthread_channel': 31,
    'object.type.weakref': 21,
    'object.virtual_thread.cancel_requested_offset': 128,
    'object.virtual_thread.channel_arm_a_offset': 152,
    'object.virtual_thread.channel_arm_b_offset': 160,
    'object.virtual_thread.channel_index_offset': 184,
    'object.virtual_thread.channel_owner_a_offset': 136,
    'object.virtual_thread.channel_owner_b_offset': 144,
    'object.virtual_thread.channel_status_offset': 176,
    'object.virtual_thread.channel_value_offset': 168,
    'object.virtual_thread.continuation_offset': 16,
    'object.virtual_thread.exception_offset': 72,
    'object.virtual_thread.identity_offset': 192,
    'object.virtual_thread.io_entry_offset': 64,
    'object.virtual_thread.join_entry_offset': 104,
    'object.virtual_thread.join_target_offset': 112,
    'object.virtual_thread.join_wait_tail_offset': 96,
    'object.virtual_thread.join_waiters_offset': 88,
    'object.virtual_thread.outcome_offset': 80,
    'object.virtual_thread.pinned_offset': 48,
    'object.virtual_thread.queued_offset': 40,
    'object.virtual_thread.result_offset': 24,
    'object.virtual_thread.size': 200,
    'object.virtual_thread.state_offset': 32,
    'object.virtual_thread.timer_entry_offset': 56,
    'object.virtual_thread.wait_kind_offset': 120,
    'object.vthread_channel.core.capacity_offset': 24,
    'object.vthread_channel.core.flags_offset': 120,
    'object.vthread_channel.core.head_offset': 40,
    'object.vthread_channel.core.items_offset': 128,
    'object.vthread_channel.core.kind_offset': 16,
    'object.vthread_channel.core.length_offset': 32,
    'object.vthread_channel.core.oneshot_offset': 72,
    'object.vthread_channel.core.oneshot_sent_offset': 80,
    'object.vthread_channel.core.receiver_closed_offset': 64,
    'object.vthread_channel.core.recv_head_offset': 104,
    'object.vthread_channel.core.recv_tail_offset': 112,
    'object.vthread_channel.core.send_head_offset': 88,
    'object.vthread_channel.core.send_tail_offset': 96,
    'object.vthread_channel.core.sender_count_offset': 56,
    'object.vthread_channel.core.size': 128,
    'object.vthread_channel.core.tail_offset': 48,
    'object.vthread_channel.endpoint.closed_offset': 32,
    'object.vthread_channel.endpoint.core_offset': 24,
    'object.vthread_channel.endpoint.kind_offset': 16,
    'object.vthread_channel.endpoint.size': 40,
    'stackmap.function_size': 32,
    'stackmap.header_size': 24,
    'stackmap.location.managed': 1,
    'stackmap.location.owned': 2,
    'stackmap.location.stack_indirect': 1,
    'stackmap.location_size': 16,
    'stackmap.magic_i64': 3553411906360525648,
    'stackmap.no_offset': 4294967295,
    'stackmap.record_size': 32,
    'stackmap.runtime.function_size': 32,
    'stackmap.runtime.header_size': 40,
    'stackmap.runtime.live_set_size': 8,
    'stackmap.runtime.location_size': 4,
    'stackmap.runtime.rare_size': 16,
    'stackmap.runtime.record_size': 8,
    'stdio.file.aux_offset': 24,
    'stdio.file.buffer_capacity_offset': 40,
    'stdio.file.buffer_length_offset': 48,
    'stdio.file.buffer_offset': 32,
    'stdio.file.buffer_position_offset': 56,
    'stdio.file.fd_offset': 8,
    'stdio.file.flags_offset': 16,
    'stdio.file.magic': 5783538579059651889,
    'stdio.file.magic_offset': 0,
    'stdio.file.size': 64,
    'stdio.flag.append': 32,
    'stdio.flag.borrowed_descriptor': 64,
    'stdio.flag.eof': 8,
    'stdio.flag.error': 4,
    'stdio.flag.input_standard': 17,
    'stdio.flag.output_standard': 18,
    'stdio.flag.readable': 1,
    'stdio.flag.standard': 16,
    'stdio.flag.writable': 2,
}

# Compiler-facing aliases for the generated public object-tag ABI.
# Literal assignments keep self-hosted module initialization static;
# both this table and these aliases are emitted from the same ABI spec.
PY_TYPE_BOOL = 1
PY_TYPE_BYTEARRAY = 18
PY_TYPE_BYTES = 17
PY_TYPE_CEXT_TAG_BASE = 65536
PY_TYPE_CLASS = 10
PY_TYPE_CLASSMETHOD = 102
PY_TYPE_COMPLEX = 16
PY_TYPE_CONTINUATION = 29
PY_TYPE_COROUTINE = 20
PY_TYPE_CPY_HANDLE = 32
PY_TYPE_DICT = 6
PY_TYPE_ELLIPSIS = 33
PY_TYPE_EXC = 12
PY_TYPE_FILE = 13
PY_TYPE_FLOAT = 3
PY_TYPE_FUNC = 9
PY_TYPE_GEN = 15
PY_TYPE_INSTANCE = 11
PY_TYPE_INT = 2
PY_TYPE_ITER = 14
PY_TYPE_LIST = 5
PY_TYPE_MEMORYVIEW = 19
PY_TYPE_NONE = 0
PY_TYPE_NOTIMPLEMENTED = 34
PY_TYPE_PROPERTY = 101
PY_TYPE_SET = 8
PY_TYPE_STATICMETHOD = 103
PY_TYPE_STR = 4
PY_TYPE_TASK = 28
PY_TYPE_THREAD = 27
PY_TYPE_THREAD_CONDITION = 25
PY_TYPE_THREAD_EVENT = 24
PY_TYPE_THREAD_LOCK = 22
PY_TYPE_THREAD_RLOCK = 23
PY_TYPE_THREAD_SEMAPHORE = 26
PY_TYPE_TUPLE = 7
PY_TYPE_USER = 100
PY_TYPE_USER_CLASS_START = 104
PY_TYPE_VALUEBOX = 200
PY_TYPE_VIRTUAL_THREAD = 30
PY_TYPE_VTHREAD_CHANNEL = 31
PY_TYPE_WEAKREF = 21
