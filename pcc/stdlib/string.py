"""pcc.stdlib.string — constants pcc's code actually uses."""
from __future__ import annotations

ascii_lowercase: str = "abcdefghijklmnopqrstuvwxyz"
ascii_uppercase: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ascii_letters: str = ascii_lowercase + ascii_uppercase
digits: str = "0123456789"
hexdigits: str = "0123456789abcdefABCDEF"
octdigits: str = "01234567"
punctuation: str = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
whitespace: str = " \t\n\r\x0b\x0c"
printable: str = digits + ascii_letters + punctuation + whitespace


def capwords(s, sep=None):
    return (sep or " ").join(word.capitalize() for word in s.split(sep))


class Template:
    delimiter = "$"

    def __init__(self, template: str) -> None:
        self.template = template

    def substitute(self, mapping=None, /, **kws):
        if mapping is None:
            mapping = {}
        data = dict(mapping)
        data.update(kws)
        return self._convert(data, safe=False)

    def safe_substitute(self, mapping=None, /, **kws):
        if mapping is None:
            mapping = {}
        data = dict(mapping)
        data.update(kws)
        return self._convert(data, safe=True)

    def _convert(self, mapping, safe):
        s = self.template
        out = []
        i = 0
        while i < len(s):
            if s[i] != self.delimiter:
                out.append(s[i])
                i += 1
                continue
            if i + 1 < len(s) and s[i + 1] == self.delimiter:
                out.append(self.delimiter)
                i += 2
                continue
            j = i + 1
            if j < len(s) and s[j] == "{":
                k = s.find("}", j)
                if k < 0:
                    if safe:
                        out.append(s[i])
                        i += 1
                        continue
                    raise KeyError("")
                name = s[j+1:k]
                i = k + 1
            else:
                while j < len(s) and (s[j].isalnum() or s[j] == "_"):
                    j += 1
                name = s[i+1:j]
                i = j
            if name in mapping:
                out.append(str(mapping[name]))
            elif safe:
                out.append(self.delimiter + name)
            else:
                raise KeyError(name)
        return "".join(out)
