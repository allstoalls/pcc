"""Native-compilable sequence and human-readable diff helpers.

This is the generic ``SequenceMatcher``/``Differ`` surface used by NumPy and
Meson's diagnostics.  It implements CPython's contiguous-match algorithm,
junk/popular element handling, opcodes, ratios, close matches, ``ndiff``, and
``restore`` without importing regex, heap, namedtuple, or HTML machinery.
"""

from __future__ import annotations


_DEFAULT_CHARACTER_JUNK = "__pcc_difflib_default_character_junk__"


def _calculate_ratio(matches: int, length: int) -> float:
    if length != 0:
        return 2.0 * matches / length
    return 1.0


class SequenceMatcher:
    def __init__(self, isjunk=None, a="", b="", autojunk: bool = True) -> None:
        self.isjunk = isjunk
        self.a = None
        self.b = None
        self.autojunk = autojunk
        self.matching_blocks = None
        self.opcodes = None
        self.fullbcount = None
        self.b2j = {}
        self.bjunk = {}
        self.bpopular = {}
        self.set_seqs(a, b)

    def set_seqs(self, a, b) -> None:
        self.set_seq1(a)
        self.set_seq2(b)

    def set_seq1(self, a) -> None:
        if a is self.a:
            return
        self.a = a
        self.matching_blocks = None
        self.opcodes = None

    def set_seq2(self, b) -> None:
        if b is self.b:
            return
        self.b = b
        self.matching_blocks = None
        self.opcodes = None
        self.fullbcount = None
        self._chain_b()

    def _chain_b(self) -> None:
        self.b2j = {}
        i = 0
        while i < len(self.b):
            element = self.b[i]
            indices = self.b2j.get(element)
            if indices is None:
                indices = []
                self.b2j[element] = indices
            indices.append(i)
            i += 1

        self.bjunk = {}
        if self.isjunk is not None:
            for element in list(self.b2j.keys()):
                if self.isjunk == _DEFAULT_CHARACTER_JUNK:
                    is_junk = element == " " or element == "\t"
                else:
                    is_junk = self.isjunk(element)
                if is_junk:
                    self.bjunk[element] = True
            for element in self.bjunk.keys():
                del self.b2j[element]

        self.bpopular = {}
        length = len(self.b)
        if self.autojunk and length >= 200:
            threshold = length // 100 + 1
            for element in list(self.b2j.keys()):
                if len(self.b2j[element]) > threshold:
                    self.bpopular[element] = True
            for element in self.bpopular.keys():
                del self.b2j[element]

    def find_longest_match(
        self,
        alo: int = 0,
        ahi=None,
        blo: int = 0,
        bhi=None,
    ):
        if ahi is None:
            ahi = len(self.a)
        if bhi is None:
            bhi = len(self.b)
        best_i = alo
        best_j = blo
        best_size = 0
        previous_lengths = {}

        i = alo
        while i < ahi:
            new_lengths = {}
            for j in self.b2j.get(self.a[i], []):
                if j < blo:
                    continue
                if j >= bhi:
                    break
                size = previous_lengths.get(j - 1, 0) + 1
                new_lengths[j] = size
                if size > best_size:
                    best_i = i - size + 1
                    best_j = j - size + 1
                    best_size = size
            previous_lengths = new_lengths
            i += 1

        while (
            best_i > alo
            and best_j > blo
            and self.b[best_j - 1] not in self.bjunk
            and self.a[best_i - 1] == self.b[best_j - 1]
        ):
            best_i -= 1
            best_j -= 1
            best_size += 1
        while (
            best_i + best_size < ahi
            and best_j + best_size < bhi
            and self.b[best_j + best_size] not in self.bjunk
            and self.a[best_i + best_size] == self.b[best_j + best_size]
        ):
            best_size += 1
        while (
            best_i > alo
            and best_j > blo
            and self.b[best_j - 1] in self.bjunk
            and self.a[best_i - 1] == self.b[best_j - 1]
        ):
            best_i -= 1
            best_j -= 1
            best_size += 1
        while (
            best_i + best_size < ahi
            and best_j + best_size < bhi
            and self.b[best_j + best_size] in self.bjunk
            and self.a[best_i + best_size] == self.b[best_j + best_size]
        ):
            best_size += 1
        return (best_i, best_j, best_size)

    def get_matching_blocks(self):
        if self.matching_blocks is not None:
            return self.matching_blocks
        length_a = len(self.a)
        length_b = len(self.b)
        queue = [(0, length_a, 0, length_b)]
        blocks = []
        while len(queue) > 0:
            alo, ahi, blo, bhi = queue.pop()
            i, j, size = self.find_longest_match(alo, ahi, blo, bhi)
            if size != 0:
                blocks.append((i, j, size))
                if alo < i and blo < j:
                    queue.append((alo, i, blo, j))
                if i + size < ahi and j + size < bhi:
                    queue.append((i + size, ahi, j + size, bhi))
        blocks.sort()

        previous_i = 0
        previous_j = 0
        previous_size = 0
        merged = []
        for i, j, size in blocks:
            if previous_i + previous_size == i and previous_j + previous_size == j:
                previous_size += size
            else:
                if previous_size != 0:
                    merged.append((previous_i, previous_j, previous_size))
                previous_i = i
                previous_j = j
                previous_size = size
        if previous_size != 0:
            merged.append((previous_i, previous_j, previous_size))
        merged.append((length_a, length_b, 0))
        self.matching_blocks = merged
        return self.matching_blocks

    def get_opcodes(self):
        if self.opcodes is not None:
            return self.opcodes
        answer = []
        i = 0
        j = 0
        for match_i, match_j, size in self.get_matching_blocks():
            tag = ""
            if i < match_i and j < match_j:
                tag = "replace"
            elif i < match_i:
                tag = "delete"
            elif j < match_j:
                tag = "insert"
            if tag != "":
                answer.append((tag, i, match_i, j, match_j))
            i = match_i + size
            j = match_j + size
            if size != 0:
                answer.append(("equal", match_i, i, match_j, j))
        self.opcodes = answer
        return answer

    def ratio(self) -> float:
        matches = 0
        for block in self.get_matching_blocks():
            matches += block[2]
        return _calculate_ratio(matches, len(self.a) + len(self.b))

    def quick_ratio(self) -> float:
        if self.fullbcount is None:
            self.fullbcount = {}
            for element in self.b:
                self.fullbcount[element] = self.fullbcount.get(element, 0) + 1
        available = {}
        matches = 0
        for element in self.a:
            if element in available:
                count = available[element]
            else:
                count = self.fullbcount.get(element, 0)
            available[element] = count - 1
            if count > 0:
                matches += 1
        return _calculate_ratio(matches, len(self.a) + len(self.b))

    def real_quick_ratio(self) -> float:
        return _calculate_ratio(
            min(len(self.a), len(self.b)), len(self.a) + len(self.b)
        )


def get_close_matches(word, possibilities, n: int = 3, cutoff: float = 0.6):
    if n <= 0:
        raise ValueError("n must be > 0: " + repr(n))
    if cutoff < 0.0 or cutoff > 1.0:
        raise ValueError("cutoff must be in [0.0, 1.0]: " + repr(cutoff))
    scored = []
    matcher = SequenceMatcher()
    matcher.set_seq2(word)
    for possibility in possibilities:
        matcher.set_seq1(possibility)
        if (
            matcher.real_quick_ratio() >= cutoff
            and matcher.quick_ratio() >= cutoff
            and matcher.ratio() >= cutoff
        ):
            scored.append((matcher.ratio(), possibility))
    scored.sort(reverse=True)
    result = []
    i = 0
    while i < len(scored) and i < n:
        result.append(scored[i][1])
        i += 1
    return result


def _keep_original_ws(text: str, tags: str) -> str:
    result = ""
    i = 0
    while i < len(text) and i < len(tags):
        if tags[i] == " " and text[i].isspace():
            result = result + text[i]
        else:
            result = result + tags[i]
        i += 1
    return result


class Differ:
    def __init__(self, linejunk=None, charjunk=None) -> None:
        self.linejunk = linejunk
        self.charjunk = charjunk

    def _dump(self, tag: str, values, low: int, high: int):
        result = []
        i = low
        while i < high:
            result.append(tag + " " + values[i])
            i += 1
        return result

    def _plain_replace(self, a, alo: int, ahi: int, b, blo: int, bhi: int):
        result = []
        if bhi - blo < ahi - alo:
            result.extend(self._dump("+", b, blo, bhi))
            result.extend(self._dump("-", a, alo, ahi))
        else:
            result.extend(self._dump("-", a, alo, ahi))
            result.extend(self._dump("+", b, blo, bhi))
        return result

    def _qformat(self, a_line: str, b_line: str, a_tags: str, b_tags: str):
        a_tags = _keep_original_ws(a_line, a_tags).rstrip()
        b_tags = _keep_original_ws(b_line, b_tags).rstrip()
        result = ["- " + a_line]
        if a_tags != "":
            result.append("? " + a_tags + "\n")
        result.append("+ " + b_line)
        if b_tags != "":
            result.append("? " + b_tags + "\n")
        return result

    def _fancy_helper(self, a, alo: int, ahi: int, b, blo: int, bhi: int):
        if alo < ahi:
            if blo < bhi:
                return self._fancy_replace(a, alo, ahi, b, blo, bhi)
            return self._dump("-", a, alo, ahi)
        if blo < bhi:
            return self._dump("+", b, blo, bhi)
        return []

    def _fancy_replace(self, a, alo: int, ahi: int, b, blo: int, bhi: int):
        best_ratio = 0.74
        cutoff = 0.75
        matcher = SequenceMatcher(self.charjunk)
        equal_i = None
        equal_j = None
        best_i = alo
        best_j = blo

        j = blo
        while j < bhi:
            b_line = b[j]
            matcher.set_seq2(b_line)
            i = alo
            while i < ahi:
                a_line = a[i]
                if a_line == b_line:
                    if equal_i is None:
                        equal_i = i
                        equal_j = j
                else:
                    matcher.set_seq1(a_line)
                    if (
                        matcher.real_quick_ratio() > best_ratio
                        and matcher.quick_ratio() > best_ratio
                        and matcher.ratio() > best_ratio
                    ):
                        best_ratio = matcher.ratio()
                        best_i = i
                        best_j = j
                i += 1
            j += 1

        if best_ratio < cutoff:
            if equal_i is None:
                return self._plain_replace(a, alo, ahi, b, blo, bhi)
            best_i = equal_i
            best_j = equal_j
        else:
            equal_i = None

        result = self._fancy_helper(a, alo, best_i, b, blo, best_j)
        a_line = a[best_i]
        b_line = b[best_j]
        if equal_i is None:
            a_tags = ""
            b_tags = ""
            matcher.set_seqs(a_line, b_line)
            for tag, ai1, ai2, bj1, bj2 in matcher.get_opcodes():
                a_length = ai2 - ai1
                b_length = bj2 - bj1
                if tag == "replace":
                    a_tags = a_tags + "^" * a_length
                    b_tags = b_tags + "^" * b_length
                elif tag == "delete":
                    a_tags = a_tags + "-" * a_length
                elif tag == "insert":
                    b_tags = b_tags + "+" * b_length
                elif tag == "equal":
                    a_tags = a_tags + " " * a_length
                    b_tags = b_tags + " " * b_length
            result.extend(self._qformat(a_line, b_line, a_tags, b_tags))
        else:
            result.append("  " + a_line)
        result.extend(
            self._fancy_helper(a, best_i + 1, ahi, b, best_j + 1, bhi)
        )
        return result

    def compare(self, a, b):
        result = []
        matcher = SequenceMatcher(self.linejunk, a, b)
        for tag, alo, ahi, blo, bhi in matcher.get_opcodes():
            if tag == "replace":
                result.extend(self._fancy_replace(a, alo, ahi, b, blo, bhi))
            elif tag == "delete":
                result.extend(self._dump("-", a, alo, ahi))
            elif tag == "insert":
                result.extend(self._dump("+", b, blo, bhi))
            elif tag == "equal":
                result.extend(self._dump(" ", a, alo, ahi))
        return result


def IS_LINE_JUNK(line: str) -> bool:
    stripped = line.strip()
    return stripped == "" or stripped == "#"


def IS_CHARACTER_JUNK(ch: str) -> bool:
    return ch == " " or ch == "\t"


def ndiff(
    a,
    b,
    linejunk=None,
    charjunk="__pcc_difflib_default_character_junk__",
):
    return Differ(linejunk, charjunk).compare(a, b)


def restore(delta, which):
    choice = int(which)
    if choice == 1:
        selected = "- "
    elif choice == 2:
        selected = "+ "
    else:
        raise ValueError(
            "unknown delta choice (must be 1 or 2): " + repr(which)
        )
    result = []
    for line in delta:
        prefix = line[:2]
        if prefix == "  " or prefix == selected:
            result.append(line[2:])
    return result
