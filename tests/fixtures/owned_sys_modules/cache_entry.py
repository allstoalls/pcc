import sys
import gc
import re
import cache_provider
import cache_cycle_owner
import cache_cycle_peer
from cache_state import events


def take(*, value, later=None, **extras):
    gc.collect()
    return value


def later():
    gc.collect()
    return None


def main():
    name = ''.join(['cache_', 'provider'])
    mapping = sys.modules
    assert mapping is sys.modules
    assert mapping[name] is cache_provider
    assert events == ['provider']
    assert re.compile('a', mapping[name]._lexreflags | re.VERBOSE).match('A') is not None
    assert take(value=mapping[name], later=later()) is cache_provider
    assert cache_cycle_owner.partial is cache_cycle_owner
    assert cache_cycle_peer.observed == 'partial'
    assert cache_cycle_peer.same
    try:
        mapping['cache_missing_unimported']
    except KeyError as error:
        assert error.args[0] == 'cache_missing_unimported'
    else:
        raise AssertionError('missing lookup did not raise')
    assert 'cache_missing_unimported' not in mapping
    replacement = {'replacement': True}
    mapping[name] = replacement
    assert mapping[name] is replacement
    assert __import__(name) is replacement
    mapping[name] = None
    assert mapping[name] is None
    try:
        __import__(name)
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError('None cache entry did not stop import')
    del mapping[name]
    assert name not in mapping
    try:
        mapping[name]
    except KeyError:
        pass
    else:
        raise AssertionError('deleted lookup did not raise')
    mapping[name] = cache_provider
    assert __import__(name) is cache_provider
    try:
        import cache_failure
    except ValueError as error:
        assert error.args[0] == 'cache failure'
    else:
        raise AssertionError('initializer failure was lost')
    assert 'cache_failure' not in mapping
    assert mapping['cache_failed_escape'].__name__ == 'cache_failure'
    del mapping['cache_failed_escape']
    try:
        import cache_replaced_failure
    except ValueError as error:
        assert error.args[0] == 'replaced failure'
    else:
        raise AssertionError('replacement initializer failure was lost')
    assert 'cache_replaced_failure' not in mapping
    gc.collect()
    assert events.count('replacement dropped') == 1
    print('LIVE_MODULE_CACHE_OK')


main()
