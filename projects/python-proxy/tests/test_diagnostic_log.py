import pytest
from pproxy.diagnostic_log import LogPolicy


@pytest.mark.parametrize('event', ['accepted', 'client_protocol', 'route_selected', 'upstream_connected',
                                  'upstream_handshake_complete', 'first_data', 'active_connection',
                                  'channel_start', 'channel_end', 'writer_close'])
def test_essential_omits_normal_connection_events(event):
    assert LogPolicy().select(event, {}, {}) is None


@pytest.mark.parametrize('event', ['process_start', 'process_stop', 'shutdown_requested', 'upstream_status'])
def test_lifecycle_and_upstream_transitions_are_kept(event):
    assert LogPolicy().select(event, {'available': False}, {}) == (event, {'available': False})


def test_failure_keeps_context_but_logs_only_once_per_connection():
    state = {'client': ('127.0.0.1', 1234), 'target': ('example.test', 443), 'upstream': 'remote:8084'}
    policy = LogPolicy()
    event, data = policy.select('upstream_connect_error', {'error_type': 'TimeoutError'}, state)
    assert event == 'upstream_connect_error' and data['target'] == ('example.test', 443)
    assert data['upstream'] == 'remote:8084'
    assert policy.select('handler_error', {'error_type': 'TimeoutError'}, state) is None


@pytest.mark.parametrize('error', ['CancelledError', 'KeyboardInterrupt'])
def test_expected_cancellation_is_silent(error):
    assert LogPolicy().select('read_error', {'error_type': error}, {}) is None


def test_client_handshake_abandonment_is_silent_but_upstream_truncation_is_not():
    fields = {'error_type': 'IncompleteReadError', 'expected_disconnect': True}
    assert LogPolicy().select('client_handshake_error', fields, {}) is None
    assert LogPolicy().select('upstream_handshake_error', fields, {}) is not None


def test_zero_response_upstream_close_is_actionable():
    state = {'client_bytes': 1900, 'target': ('video.example', 443)}
    result = LogPolicy().select('eof', {'direction': 'upstream_to_client', 'read_bytes': 0}, state)
    assert result[0] == 'upstream_closed_without_response'


@pytest.mark.parametrize('received,sent', [(0, 0), (100, 1900)])
def test_unused_or_successful_upstream_close_is_silent(received, sent):
    assert LogPolicy().select('eof', {'direction': 'upstream_to_client', 'read_bytes': received}, {'client_bytes': sent}) is None


def test_client_close_does_not_turn_peer_cleanup_into_an_upstream_failure():
    policy, state = LogPolicy(), {'client_bytes': 1900}
    assert policy.select('eof', {'direction': 'client_to_upstream', 'read_bytes': 1900}, state) is None
    assert policy.select('eof', {'direction': 'upstream_to_client', 'read_bytes': 0}, state) is None


def test_diagnostic_mode_keeps_full_events():
    policy = LogPolicy('diagnostic')
    assert policy.select('active_connection', {'read_bytes': 12}, {}) == ('active_connection', {'read_bytes': 12})


def test_unexpected_direct_fallback_is_kept():
    assert LogPolicy().select('direct_fallback', {'selected': 'DIRECT'}, {}) is not None
