"""A real strict-native acceptance input for the owned time producer."""
import gc
import time


class Payload:
    pass


def consume(*, seconds, zone, **extras):
    gc.collect()
    return seconds, zone


def replacement_year(value):
    return 1776


def replacement_length(value):
    return 37


def replacement_item(value, index):
    return 'changed', index


def main():
    values = (2024, 2, 29, 12, 34, 56, 3, 60, 0)
    record = time.struct_time(sequence=values)
    assert isinstance(record, tuple)
    assert len(record) == 9
    assert tuple(record) == values
    assert record[:] == values
    assert hash(record) == hash(values)
    assert record.tm_zone is None and record.tm_gmtoff is None
    assert record.n_fields == 11 and record.n_sequence_fields == 9
    assert not hasattr(record, '__dict__')
    for name in ('tm_year', 'tm_zone', 'tm_gmtoff', 'new_field'):
        try:
            setattr(record, name, 0)
        except AttributeError:
            pass
        else:
            raise AssertionError('mutable struct sequence')
    try:
        vars(record)
    except TypeError:
        pass
    else:
        raise AssertionError('visible hidden owner')
    # CPython 3.15 keeps the type namespace mutable even though instances
    # are readonly and the tuple layout cannot be subclassed.
    original_count = time.struct_time.n_fields
    time.struct_time.n_fields = 99
    assert time.struct_time.n_fields == 99 and record.n_fields == 99
    time.struct_time.n_fields = original_count
    original_year = time.struct_time.tm_year
    time.struct_time.tm_year = property(replacement_year)
    assert record.tm_year == 1776
    assert tuple(record) == values
    time.struct_time.tm_year = original_year
    original_length = time.struct_time.__len__
    time.struct_time.__len__ = replacement_length
    assert len(record) == 37
    assert tuple(record) == values
    time.struct_time.__len__ = original_length
    original_item = time.struct_time.__getitem__
    time.struct_time.__getitem__ = replacement_item
    assert record[2] == ('changed', 2)
    assert tuple(record) == values
    time.struct_time.__getitem__ = original_item
    try:
        type('Child', (time.struct_time,), {})
    except TypeError:
        pass
    else:
        raise AssertionError('subclassable struct-sequence type')
    try:
        tuple.__new__(time.struct_time, values)
    except TypeError:
        pass
    else:
        raise AssertionError('uninitialized struct-sequence bypass')
    extra = Payload()
    record = time.struct_time(values, {'tm_zone': extra, 'tm_gmtoff': [19800]})
    gc.collect()
    assert record.tm_zone is extra
    assert record.tm_gmtoff == [19800]
    assert tuple(record) == values
    assert record.__reduce__()[1][1]['tm_zone'] is extra
    assert tuple(time.struct_time(values + ('X', 3600))) == values
    for bad in (values[:8], values + (1,2,3)):
        try:
            time.struct_time(bad)
        except TypeError:
            pass
        else:
            raise AssertionError('bad field count')
    assert tuple(time.gmtime(-0.25)) == (1969,12,31,23,59,59,2,365,0)
    assert tuple(time.gmtime(-1.25)) == (1969,12,31,23,59,58,2,365,0)
    epoch = time.gmtime(0)
    observed = (tuple(epoch), epoch.tm_zone, epoch.tm_gmtoff)
    assert tuple(epoch) == (1970, 1, 1, 0, 0, 0, 3, 1, 0), observed
    # CPython preserves libc's UTC abbreviation; Darwin uses UTC.
    assert type(epoch.tm_zone) is str and epoch.tm_zone in ('GMT', 'UTC'), observed
    assert type(epoch.tm_gmtoff) is int and epoch.tm_gmtoff == 0, observed
    for stamp in (-2208988800, -1, 0, 951782400, 1710053999, 1710054000,
                  1730613599, 1730613600, 4102444800):
        localtm = time.localtime(stamp)
        gmtoff = localtm.tm_gmtoff
        zone = localtm.tm_zone
        assert consume(seconds=gmtoff, zone=zone) == (gmtoff, zone)
        print(tuple(localtm), zone, gmtoff)
    for value, kind in ((float('nan'),ValueError), (float('inf'),OverflowError),
                        (10**100,OverflowError), ('1',TypeError)):
        try:
            time.localtime(value)
        except kind:
            pass
        else:
            raise AssertionError('timestamp error lost')
    print('OWNED_TIME_STRUCTSEQ_OK')
    gc.collect()


main()
