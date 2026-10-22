"""Native-compilable parser for the build-tool ``.netrc`` surface.

Explicit files are parsed with CPython-compatible machine/default credential
and macro rules.  The implicit ``~/.netrc`` path is security-sensitive:
CPython verifies owner and mode before accepting non-anonymous credentials.
pcc does not yet expose the required ``fstat``/uid metadata, so an existing
implicit file fails closed instead of accepting credentials without that
check.  A missing implicit file still raises ``FileNotFoundError`` normally,
which is the common Meson path.
"""

from __future__ import annotations

import os


class NetrcParseError(Exception):
    def __init__(self, msg: str, filename=None, lineno=None) -> None:
        super().__init__(msg)
        self.filename = filename
        self.lineno = lineno
        self.msg = msg

    def __str__(self) -> str:
        return (
            self.msg
            + " ("
            + str(self.filename)
            + ", line "
            + str(self.lineno)
            + ")"
        )


class _NetrcLexer:
    def __init__(self, stream) -> None:
        self.lineno = 1
        self.stream = stream
        self.pushback = []

    def _read_char(self) -> str:
        ch = self.stream.read(1)
        if ch == "\n":
            self.lineno += 1
        return ch

    def get_token(self):
        # None is end of input; "" is an empty quoted token (CPython 3.15.0).
        if len(self.pushback) > 0:
            return self.pushback.pop(0)

        ch = self._read_char()
        while ch != "" and ch in "\n\t\r ":
            ch = self._read_char()
        if ch == "":
            return None

        token = ""
        if ch == '"':
            ch = self._read_char()
            while ch != "":
                if ch == '"':
                    return token
                if ch == "\\":
                    ch = self._read_char()
                    if ch == "":
                        return token
                token = token + ch
                ch = self._read_char()
            return token

        while ch != "":
            if ch in "\n\t\r ":
                return token
            if ch == "\\":
                ch = self._read_char()
                if ch == "":
                    return token
            token = token + ch
            ch = self._read_char()
        return token

    def push_token(self, token) -> None:
        self.pushback.append(token)


class netrc:
    def __init__(self, file=None) -> None:
        default_netrc = file is None
        if file is None:
            home = os.environ.get("HOME")
            if home is None or home == "":
                raise NotImplementedError(
                    "netrc home discovery awaits native passwd lookup"
                )
            file = home.rstrip("/") + "/.netrc"

        self.hosts = {}
        self.macros = {}
        with open(file, "r", encoding="utf-8") as stream:
            if default_netrc:
                raise NotImplementedError(
                    "implicit .netrc awaits native owner and mode checks"
                )
            self._parse(str(file), stream)

    def _parse(self, filename: str, stream) -> None:
        lexer = _NetrcLexer(stream)
        while True:
            saved_lineno = lexer.lineno
            token = lexer.get_token()
            if token is None:
                break
            if token.startswith("#"):
                if lexer.lineno == saved_lineno and len(token) == 1:
                    lexer.stream.readline()
                continue
            if token == "machine":
                entryname = lexer.get_token()
            elif token == "default":
                entryname = "default"
            elif token == "macdef":
                entryname = lexer.get_token()
                self.macros[entryname] = []
                while True:
                    line = lexer.stream.readline()
                    if line == "":
                        raise NetrcParseError(
                            "Macro definition missing null line terminator.",
                            filename,
                            lexer.lineno,
                        )
                    if line == "\n":
                        break
                    self.macros[entryname].append(line)
                continue
            else:
                raise NetrcParseError(
                    "bad toplevel token " + repr(token),
                    filename,
                    lexer.lineno,
                )

            if not entryname:
                raise NetrcParseError(
                    "missing " + repr(token) + " name",
                    filename,
                    lexer.lineno,
                )

            login = ""
            account = ""
            password = ""
            while True:
                previous_lineno = lexer.lineno
                follower = lexer.get_token()
                if follower is not None and follower.startswith("#"):
                    if lexer.lineno == previous_lineno:
                        lexer.stream.readline()
                    continue
                if follower is None or follower in ("machine", "default", "macdef"):
                    self.hosts[entryname] = (login, account, password)
                    lexer.push_token(follower)
                    break
                if follower == "login" or follower == "user":
                    login = lexer.get_token() or ""
                elif follower == "account":
                    account = lexer.get_token() or ""
                elif follower == "password":
                    password = lexer.get_token() or ""
                else:
                    raise NetrcParseError(
                        "bad follower token " + repr(follower),
                        filename,
                        lexer.lineno,
                    )

    def authenticators(self, host: str):
        if host in self.hosts:
            return self.hosts[host]
        if "default" in self.hosts:
            return self.hosts["default"]
        return None

    def __repr__(self) -> str:
        result = ""
        for host in self.hosts.keys():
            attrs = self.hosts[host]
            result = result + "machine " + host + "\n\tlogin " + attrs[0] + "\n"
            if attrs[1] != "":
                result = result + "\taccount " + attrs[1] + "\n"
            result = result + "\tpassword " + attrs[2] + "\n"
        for macro in self.macros.keys():
            result = result + "macdef " + macro + "\n"
            for line in self.macros[macro]:
                result = result + line
            result = result + "\n"
        return result
