"""pcc.stdlib.socket - constants and address conversion helpers."""
from __future__ import annotations

AF_UNSPEC = 0
AF_INET = 2
AF_INET6 = 30
SOCK_STREAM = 1
SOCK_DGRAM = 2
SOL_SOCKET = 0xffff
SOL_IP = 0
IPPROTO_TCP = 6
IPPROTO_UDP = 17
SCM_RIGHTS = 1


class error(OSError):
    pass


def _parse_ipv4(addr: str):
    parts = addr.split(".")
    if len(parts) != 4:
        raise error("illegal IP address string passed to inet_aton")
    out = []
    for part in parts:
        value = int(part)
        if value < 0 or value > 255:
            raise error("illegal IP address string passed to inet_aton")
        out.append(value)
    return out


def inet_aton(addr: str) -> bytes:
    return bytes(_parse_ipv4(addr))


def inet_ntoa(packed) -> str:
    if len(packed) != 4:
        raise error("packed IP wrong length for inet_ntoa")
    return str(packed[0]) + "." + str(packed[1]) + "." + str(packed[2]) + "." + str(packed[3])


def _hex_value(ch: str) -> int:
    if "0" <= ch <= "9":
        return ord(ch) - ord("0")
    if "a" <= ch <= "f":
        return ord(ch) - ord("a") + 10
    if "A" <= ch <= "F":
        return ord(ch) - ord("A") + 10
    raise error("illegal IP address string passed to inet_pton")


def _parse_hextet(text: str) -> int:
    if text == "" or len(text) > 4:
        raise error("illegal IP address string passed to inet_pton")
    value = 0
    for ch in text:
        value = (value << 4) | _hex_value(ch)
    return value


def _ipv6_groups(addr: str):
    if addr == "::":
        return [0, 0, 0, 0, 0, 0, 0, 0]
    if "::" in addr:
        pieces = addr.split("::")
        if len(pieces) != 2:
            raise error("illegal IP address string passed to inet_pton")
        left = [] if pieces[0] == "" else pieces[0].split(":")
        right = [] if pieces[1] == "" else pieces[1].split(":")
        missing = 8 - len(left) - len(right)
        if missing < 0:
            raise error("illegal IP address string passed to inet_pton")
        groups = []
        for part in left:
            groups.append(_parse_hextet(part))
        i = 0
        while i < missing:
            groups.append(0)
            i += 1
        for part in right:
            groups.append(_parse_hextet(part))
        return groups
    parts = addr.split(":")
    if len(parts) != 8:
        raise error("illegal IP address string passed to inet_pton")
    groups = []
    for part in parts:
        groups.append(_parse_hextet(part))
    return groups


def inet_pton(family, addr: str) -> bytes:
    if family == AF_INET:
        return inet_aton(addr)
    if family != AF_INET6:
        raise error("address family not supported")
    groups = _ipv6_groups(addr)
    out = []
    for group in groups:
        out.append((group >> 8) & 0xff)
        out.append(group & 0xff)
    return bytes(out)


def inet_ntop(family, packed) -> str:
    if family == AF_INET:
        return inet_ntoa(packed)
    if family != AF_INET6:
        raise error("address family not supported")
    if len(packed) != 16:
        raise error("packed IP wrong length for inet_ntop")
    groups = []
    i = 0
    while i < 16:
        value = (packed[i] << 8) | packed[i + 1]
        groups.append(format(value, "x"))
        i += 2
    return ":".join(groups)
