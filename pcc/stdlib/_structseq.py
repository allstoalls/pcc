"""Generic owned struct-sequence construction and hidden-field access.

Instances keep a true tuple payload containing only the sequence fields.
Non-sequence fields live in a separately traced, one-shot native owner, never
in an instance dictionary or extra visible tuple elements. Concrete providers
supply their field names and read-only properties. The literal class marker
seals a slots-only tuple subtype after its namespace has been initialized.
"""
from pcc.extern import c_obj, extern

_new = extern("py_structseq_new", (c_obj, c_obj, c_obj), c_obj)
_hidden = extern("py_structseq_hidden", (c_obj,), c_obj)
_storage = extern("py_structseq_dict_storage", (c_obj,), c_obj)
_dict_keys = extern("py_dict_keys", (c_obj,), c_obj)
_dict_get_default = extern("py_dict_get_default", (c_obj, c_obj, c_obj), c_obj)


def hidden_fields(value):
    result = _hidden(value)
    if isinstance(result, BaseException):
        raise result
    return result


def new(cls, sequence, mapping, names, visible, qualified_name):
    try:
        iterator = iter(sequence)
    except TypeError:
        raise TypeError("constructor requires a sequence") from None
    values = tuple(iterator)
    if not isinstance(mapping, dict):
        raise TypeError(qualified_name + "() takes a dict as second arg, if any")
    storage = _storage(mapping)
    if isinstance(storage, BaseException):
        raise storage
    length = len(values)
    if length < visible:
        raise TypeError(qualified_name + "() takes an at least " + str(visible)
                        + "-sequence (" + str(length) + "-sequence given)")
    if length > len(names):
        raise TypeError(qualified_name + "() takes an at most " + str(len(names))
                        + "-sequence (" + str(length) + "-sequence given)")
    # The constructor reads dict storage, including for dict subclasses; it
    # does not invoke overridden get()/keys()/__iter__ methods.
    for name in _dict_keys(storage):
        if name not in names[length:]:
            raise TypeError(qualified_name + "() got duplicate or unexpected field name(s)")
    extras = list(values[visible:])
    for name in names[length:]:
        extras.append(_dict_get_default(storage, name, None))
    initialized = _new(cls, values[:visible], tuple(extras))
    if isinstance(initialized, BaseException):
        raise initialized
    return initialized
