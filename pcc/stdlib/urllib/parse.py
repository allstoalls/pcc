"""pcc.stdlib.urllib.parse — narrow native subset.

The public spelling remains ``import urllib.parse``. This module is the
native provider selected by pcc's resolver for the subset needed during
self-host bring-up.
"""
from __future__ import annotations


_HEX = "0123456789ABCDEF"

# Public scheme classifications used by urljoin, including caller extensions.
uses_relative = [
    "", "ftp", "http", "gopher", "nntp", "imap", "wais", "file", "https",
    "shttp", "mms", "prospero", "rtsp", "rtsps", "rtspu", "sftp", "svn",
    "svn+ssh", "ws", "wss",
]
uses_netloc = [
    "", "ftp", "http", "gopher", "nntp", "telnet", "imap", "wais", "file",
    "mms", "https", "shttp", "snews", "prospero", "rtsp", "rtsps", "rtspu",
    "rsync", "svn", "svn+ssh", "sftp", "nfs", "git", "git+ssh", "ws", "wss",
    "itms-services",
]

# Complete Unicode 17.0 NFKC preimage of /?#@: outside ASCII. URL authority
# validation only needs this predicate, not a partial normalization function.
# ASCII delimiters cannot compose away under NFC; concatenation/composition
# therefore cannot introduce a delimiter absent from each character's NFKC.
_URL_NFKC_DELIMITERS = (
    "\u2047\u2048\u2049\u2100\u2101\u2105\u2106\u2a74\ufe13\ufe16"
    "\ufe55\ufe56\ufe5f\ufe6b\uff03\uff0f\uff1a\uff1f\uff20"
)


def _urljoin_ipv4(address: str) -> bool:
    octets = address.split(".")
    if len(octets) != 4:
        return False
    for octet in octets:
        if not octet or len(octet) > 3 or (len(octet) > 1 and octet[0] == "0"):
            return False
        value = 0
        for ch in octet:
            if ch < "0" or ch > "9":
                return False
            value = value * 10 + ord(ch) - 48
        if value > 255:
            return False
    return True


def _urljoin_ipv6(address: str) -> bool:
    host, marker, scope = address.partition("%")
    if marker and (not scope or "%" in scope):
        return False
    compressed = "::" in host
    if compressed:
        left, right = host.split("::", 1)
        groups = left.split(":") if left else []
        if right:
            groups = groups + right.split(":")
    else:
        groups = host.split(":")
    count = len(groups)
    for index in range(len(groups)):
        group = groups[index]
        if "." in group and index == len(groups) - 1:
            if not host.endswith(group) or not _urljoin_ipv4(group):
                return False
            count += 1
        else:
            if not group or len(group) > 4:
                return False
            for ch in group:
                if ch not in "0123456789abcdefABCDEF":
                    return False
    if compressed:
        return count < 8
    return count == 8


def _urljoin_check_authority(authority: str) -> None:
    if ("[" in authority) != ("]" in authority):
        raise ValueError("Invalid IPv6 URL")
    if "[" in authority:
        host_port = authority.rpartition("@")[2]
        before, opening, tail = host_port.partition("[")
        if opening:
            host, _closing, port = tail.partition("]")
            if before or (port and not port.startswith(":")):
                raise ValueError("Invalid IPv6 URL")
        else:
            host = host_port.partition(":")[0]
        if host.startswith(("v", "V")):
            version, dot, address = host[1:].partition(".")
            valid = bool(version) and bool(dot) and bool(address)
            for ch in version:
                if ch not in "0123456789abcdefABCDEF":
                    valid = False
            if not valid:
                raise ValueError("IPvFuture address is invalid")
        elif not _urljoin_ipv6(host):
            if _urljoin_ipv4(host):
                raise ValueError("An IPv4 address cannot be in brackets")
            raise ValueError("'" + host + "' does not appear to be an IPv4 or IPv6 address")
    for ch in authority:
        if ch in _URL_NFKC_DELIMITERS:
            raise ValueError(
                "netloc '" + authority + "' contains invalid characters under NFKC normalization"
            )


def _urljoin_split(text: str, allow_fragments):
    # Preserve absent delimiters as None, including an explicit empty query,
    # fragment or authority. urlparse's older six-field subset cannot carry
    # that distinction. Only leading C0/space is stripped; trailing space stays.
    rest = text.lstrip(
        "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
        "\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f "
    ).replace("\t", "").replace("\r", "").replace("\n", "")
    fragments = bool(allow_fragments)
    scheme = None
    colon = rest.find(":")
    if colon > 0 and rest[0] in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
        valid = True
        for ch in rest[:colon]:
            if ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-":
                valid = False
        if valid:
            scheme = rest[:colon].lower()
            rest = rest[colon + 1:]
    authority = None
    if rest.startswith("//"):
        rest = rest[2:]
        end = _first_authority_sep(rest)
        if end < 0:
            authority = rest
            rest = ""
        else:
            authority = rest[:end]
            rest = rest[end:]
        # CPython checks bracket syntax before splitting query and fragment.
        _urljoin_check_authority(authority)
    fragment = None
    if fragments and "#" in rest:
        rest, fragment = rest.split("#", 1)
    query = None
    if "?" in rest:
        rest, query = rest.split("?", 1)
    return scheme, authority, rest, query, fragment


def _urljoin_unsplit(scheme, authority, path: str, query, fragment) -> str:
    if authority is not None:
        if path and not path.startswith("/"):
            path = "/" + path
        path = "//" + authority + path
    elif path.startswith("//"):
        path = "//" + path
    if scheme:
        path = scheme + ":" + path
    if query is not None:
        path = path + "?" + query
    if fragment is not None:
        path = path + "#" + fragment
    return path


def _urljoin_text(base: str, url: str, allow_fragments) -> str:
    base_scheme, base_authority, base_path, base_query, base_fragment = _urljoin_split(
        base, allow_fragments,
    )
    scheme, authority, path, query, fragment = _urljoin_split(url, allow_fragments)
    if scheme is None:
        scheme = base_scheme
    if scheme != base_scheme or (scheme and scheme not in uses_relative):
        return url
    if not scheme or scheme in uses_netloc:
        if authority:
            return _urljoin_unsplit(scheme, authority, path, query, fragment)
        authority = base_authority
    if not path:
        path = base_path
        if query is None:
            query = base_query
            if fragment is None:
                fragment = base_fragment
        return _urljoin_unsplit(scheme, authority, path, query, fragment)

    if path.startswith("/"):
        segments = path.split("/")
    else:
        prefix = base_path.split("/")
        if prefix[-1] != "":
            prefix.pop()
        combined = prefix + path.split("/")
        segments = []
        for index in range(len(combined)):
            if combined[index] or index == 0 or index == len(combined) - 1:
                segments.append(combined[index])
    resolved: list[str] = []
    for segment in segments:
        if segment == "..":
            if resolved:
                resolved.pop()
        elif segment != ".":
            resolved.append(segment)
    if segments[-1] == "." or segments[-1] == "..":
        resolved.append("")
    path = "/".join(resolved)
    if not path:
        path = "/"
    return _urljoin_unsplit(scheme, authority, path, query, fragment)


def urljoin(base, url, allow_fragments=True):
    """Resolve a URL reference, preserving Python's text/ASCII-bytes contract."""
    # These are identity-preserving public short circuits, even for objects
    # that would otherwise fail coercion. Do not eagerly decode or validate.
    if not base:
        return url
    if not url:
        return base
    text_input = None
    for argument in (base, url):
        if argument:
            if text_input is None:
                text_input = isinstance(argument, str)
            elif text_input != isinstance(argument, str):
                raise TypeError("Cannot mix str and non-str arguments")
    if text_input is None:
        for argument in (base, url):
            if argument is not None:
                text_input = isinstance(argument, str)
                break
    if text_input is not False:
        return _urljoin_text(base, url, allow_fragments)
    decoded_base = base.decode("ascii", "strict") if base else "" if base is not None else None
    decoded_url = url.decode("ascii", "strict") if url else "" if url is not None else None
    result = _urljoin_text(decoded_base, decoded_url, allow_fragments)
    return result.encode("ascii")


def _is_unreserved(ch: str) -> bool:
    # RFC 3986 unreserved set: ALPHA / DIGIT / "-" / "." / "_" / "~"
    if ch >= "a" and ch <= "z":
        return True
    if ch >= "A" and ch <= "Z":
        return True
    if ch >= "0" and ch <= "9":
        return True
    if ch == "-" or ch == "." or ch == "_" or ch == "~":
        return True
    return False


def quote(s: str, safe: str = "/", encoding=None, errors=None) -> str:
    if s == "":
        return s
    # Host ``urllib.request`` quotes redirect URLs with
    # ``encoding="iso-8859-1"``. pcc-Python strings are UTF-8 text; the
    # byte-level round trip only matters for non-ASCII payloads, and the
    # registered encoder set does not expose iso-8859-1 yet. Accept and
    # validate the spellings the closed world resolves, then keep the
    # UTF-8 byte semantics of the native subset.
    if encoding is None:
        encoding = "utf-8"
    if errors is None:
        errors = "strict"
    key = encoding.lower().replace("_", "-")
    if key not in ("utf-8", "utf8", "iso-8859-1", "latin-1", "latin1"):
        raise LookupError("unknown encoding: " + encoding)
    if errors not in ("strict", "replace", "ignore", "surrogateescape"):
        raise LookupError("unsupported pcc-native str encode errors mode")
    out = ""
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if _is_unreserved(ch):
            out = out + ch
        elif ch in safe:
            out = out + ch
        else:
            b = ord(ch)
            # The native str stores UTF-8; the per-code-point path below
            # matches the host behaviour for ASCII and keeps the existing
            # byte-for-byte escaping for multi-byte code points seen by
            # the self-host probes.
            out = out + "%" + _HEX[(b >> 4) & 0xF] + _HEX[b & 0xF]
        i = i + 1
    return out


def _hex_digit_val(ch: str) -> int:
    if ch >= "0" and ch <= "9":
        return ord(ch) - ord("0")
    if ch >= "A" and ch <= "F":
        return ord(ch) - ord("A") + 10
    if ch >= "a" and ch <= "f":
        return ord(ch) - ord("a") + 10
    return -1


def _decode_utf8_replace(data: bytes) -> str:
    """Decode UTF-8, replacing each ill-formed subsequence once."""
    out: list[str] = []
    i = 0
    while i < len(data):
        first = data[i]
        if first < 128:
            out.append(chr(first))
            i += 1
            continue
        size = 0
        value = 0
        low = 128
        high = 191
        if 194 <= first <= 223:
            size = 2
            value = first & 31
        elif 224 <= first <= 239:
            size = 3
            value = first & 15
            if first == 224:
                low = 160
            elif first == 237:
                high = 159
        elif 240 <= first <= 244:
            size = 4
            value = first & 7
            if first == 240:
                low = 144
            elif first == 244:
                high = 143
        if size == 0:
            out.append("\ufffd")
            i += 1
            continue
        end = i + 1
        while end < len(data) and end < i + size:
            byte = data[end]
            if byte < low or byte > high:
                break
            value = (value << 6) | (byte & 63)
            end += 1
            low = 128
            high = 191
        if end == i + size:
            out.append(chr(value))
        else:
            out.append("\ufffd")
        i = end
    return "".join(out)


def _decode_unquoted(data: bytes, encoding: str, errors: str) -> str:
    key = encoding.lower().replace("_", "-")
    # The native bytes decoder does not yet provide the replace handler.
    if (key == "utf-8" or key == "utf8") and errors == "replace":
        return _decode_utf8_replace(data)
    return data.decode(encoding, errors)


def unquote(s: str, encoding: str = "utf-8", errors: str = "replace") -> str:
    if "%" not in s:
        return s
    if encoding is None:
        encoding = "utf-8"
    if errors is None:
        errors = "replace"
    out: list[str] = []
    pending: list[int] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "%" and i + 2 < len(s):
            hi = _hex_digit_val(s[i + 1])
            lo = _hex_digit_val(s[i + 2])
            if hi >= 0 and lo >= 0:
                pending.append((hi << 4) | lo)
                i += 3
                continue
        if ord(ch) < 128:
            pending.append(ord(ch))
        else:
            if pending:
                out.append(_decode_unquoted(bytes(pending), encoding, errors))
                pending = []
            out.append(ch)
        i += 1
    if pending:
        out.append(_decode_unquoted(bytes(pending), encoding, errors))
    return "".join(out)


class ParseResult:
    def __init__(
        self,
        scheme: str,
        netloc: str,
        path: str,
        params: str,
        query: str,
        fragment: str,
    ) -> None:
        self.scheme = scheme
        self.netloc = netloc
        self.path = path
        self.params = params
        self.query = query
        self.fragment = fragment

    def __len__(self) -> int:
        return 6

    def __getitem__(self, index: int) -> str:
        if index < 0:
            index = index + 6
        if index == 0:
            return self.scheme
        if index == 1:
            return self.netloc
        if index == 2:
            return self.path
        if index == 3:
            return self.params
        if index == 4:
            return self.query
        if index == 5:
            return self.fragment
        raise IndexError("ParseResult index out of range")

    def _replace(
        self,
        scheme=None,
        netloc=None,
        path=None,
        params=None,
        query=None,
        fragment=None,
    ):
        return ParseResult(
            self.scheme if scheme is None else scheme,
            self.netloc if netloc is None else netloc,
            self.path if path is None else path,
            self.params if params is None else params,
            self.query if query is None else query,
            self.fragment if fragment is None else fragment,
        )

    def _userinfo_hostport(self):
        userinfo = ""
        hostport = self.netloc
        if "@" in hostport:
            userinfo, hostport = hostport.rsplit("@", 1)
        return userinfo, hostport

    @property
    def username(self):
        userinfo, _hostport = self._userinfo_hostport()
        if not userinfo:
            return None
        if ":" in userinfo:
            return userinfo.split(":", 1)[0]
        return userinfo

    @property
    def password(self):
        userinfo, _hostport = self._userinfo_hostport()
        if not userinfo:
            return None
        if ":" not in userinfo:
            return None
        return userinfo.split(":", 1)[1]

    @property
    def hostname(self):
        _userinfo, hostport = self._userinfo_hostport()
        if not hostport:
            return None
        if hostport.startswith("["):
            end = hostport.find("]")
            if end >= 0:
                host = hostport[1:end]
                return host.lower() if host else None
        if ":" in hostport:
            host = hostport.rsplit(":", 1)[0]
        else:
            host = hostport
        return host.lower() if host else None

    @property
    def port(self):
        _userinfo, hostport = self._userinfo_hostport()
        if not hostport:
            return None
        port_text = ""
        if hostport.startswith("["):
            end = hostport.find("]")
            if end >= 0 and end + 1 < len(hostport) and hostport[end + 1] == ":":
                port_text = hostport[end + 2 :]
        elif ":" in hostport:
            port_text = hostport.rsplit(":", 1)[1]
        if not port_text:
            return None
        return int(port_text)

    def geturl(self) -> str:
        out = ""
        if self.scheme:
            out = out + self.scheme + ":"
        if self.netloc:
            out = out + "//" + self.netloc
        out = out + self.path
        if self.params:
            out = out + ";" + self.params
        if self.query:
            out = out + "?" + self.query
        if self.fragment:
            out = out + "#" + self.fragment
        return out


def _first_authority_sep(text: str) -> int:
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "/" or ch == "?" or ch == "#":
            return i
        i += 1
    return -1


def urlparse(url: str, scheme: str = "", allow_fragments: bool = True):
    rest = url
    i = 0
    while i < len(url):
        ch = url[i]
        if ch == ":":
            scheme = url[:i].lower()
            rest = url[i + 1 :]
            break
        if ch == "/" or ch == "?" or ch == "#":
            break
        i += 1

    netloc = ""
    if rest.startswith("//"):
        rest = rest[2:]
        end = _first_authority_sep(rest)
        if end < 0:
            netloc = rest
            rest = ""
        else:
            netloc = rest[:end]
            rest = rest[end:]

    fragment = ""
    if allow_fragments:
        rest, sep, frag = rest.partition("#")
        if sep:
            fragment = frag

    query = ""
    rest, sep, qry = rest.partition("?")
    if sep:
        query = qry

    path = rest
    params = ""
    return ParseResult(scheme, netloc, path, params, query, fragment)
