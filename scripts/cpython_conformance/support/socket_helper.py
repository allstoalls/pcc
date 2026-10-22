"""``test.support.socket_helper`` for the conformance harness: no sockets."""

import unittest

HOST = "localhost"
HOSTv4 = "127.0.0.1"
HOSTv6 = "::1"
IPV6_ENABLED = False


def _no_sockets(*args, **kwargs):
    raise unittest.SkipTest("sockets are not part of a conformance run")


find_unused_port = _no_sockets
bind_port = _no_sockets
bind_unix_socket = _no_sockets
get_socket_conn_refused_errs = _no_sockets
create_unix_domain_name = _no_sockets
transient_internet = _no_sockets


def skip_unless_bind_unix_socket(test):
    return unittest.skip("sockets are not part of a conformance run")(test)


def skip_if_tcp_blackhole(test):
    return unittest.skip("sockets are not part of a conformance run")(test)


def tcp_blackhole():
    return False

has_gethostname = True
