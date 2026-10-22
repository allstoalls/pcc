"""pcc.stdlib.ssl - small SSL context surface."""

from __future__ import annotations

CERT_NONE = 0
CERT_OPTIONAL = 1
CERT_REQUIRED = 2
PROTOCOL_TLS = 2
PROTOCOL_TLS_CLIENT = 16
PROTOCOL_TLS_SERVER = 17


class Purpose:
    SERVER_AUTH = "SERVER_AUTH"
    CLIENT_AUTH = "CLIENT_AUTH"


class SSLContext:
    def __init__(self, protocol=PROTOCOL_TLS, purpose=None):
        self.protocol = protocol
        self.purpose = purpose
        self.check_hostname = purpose == Purpose.SERVER_AUTH
        self.verify_mode = CERT_REQUIRED if self.check_hostname else CERT_NONE
        self.certfile = None
        self.keyfile = None

    def load_cert_chain(self, certfile, keyfile=None, password=None):
        self.certfile = certfile
        self.keyfile = keyfile
        if password is not None:
            raise NotImplementedError("SSLContext password loading is not implemented")
        return None

    def wrap_socket(self, *args, **kwargs):
        raise NotImplementedError("ssl socket wrapping is not implemented")


def create_default_context(
    purpose=Purpose.SERVER_AUTH, cafile=None, capath=None, cadata=None
):
    if cafile is not None or capath is not None or cadata is not None:
        raise NotImplementedError("custom CA loading is not implemented")
    return SSLContext(PROTOCOL_TLS, purpose)
