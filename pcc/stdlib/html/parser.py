"""A parser for HTML and XHTML."""

# This file is based on sgmllib.py, but the API is slightly different.

# XXX There should be a way to distinguish between PCDATA (parsed
# character data -- the normal case), RCDATA (replaceable character
# data -- only char and entity references and end tags are special)
# and CDATA (character data -- only end tags are special).


# Derived from CPython 3.15.0rc1, under the Python license in LICENSE.txt.
# Regex token recognition is replaced by owned character scans. The public
# streaming state machine and callback semantics follow the upstream parser.
from . import unescape, _charref_end
from .entities import html5 as html5_entities

__all__ = ['HTMLParser']
_SPACE = '\t\n\r\f '


def _is_letter(ch):
    return 'a' <= ch <= 'z' or 'A' <= ch <= 'Z'


def _is_alnum(ch):
    return _is_letter(ch) or '0' <= ch <= '9'


def _ascii_lower(text):
    result = ''
    for ch in text:
        if 'A' <= ch <= 'Z':
            result += chr(ord(ch) + 32)
        else:
            result += ch
    return result


def _unescape_attrvalue(s):
    pieces = []
    i = 0
    while i < len(s):
        amp = s.find('&', i)
        if amp < 0:
            pieces.append(s[i:])
            break
        pieces.append(s[i:amp])
        end = _charref_end(s, amp + 1, True)
        if end == amp + 1:
            pieces.append('&')
            i = amp + 1
            continue
        ref = s[amp:end]
        if ref.startswith('&#') or (not ref.endswith('=') and ref[1:] in html5_entities):
            pieces.append(unescape(ref))
        else:
            pieces.append(ref)
        i = end
    return ''.join(pieces)


def _tag_open(s, i):
    return i + 1 < len(s) and s[i] == '<' and _is_letter(s[i + 1])


def _endtag_open(s, i):
    return i + 2 < len(s) and s[i:i + 2] == '</' and _is_letter(s[i + 2])


def _skip_separators(s, i, keep_close):
    while i < len(s):
        if s[i] in _SPACE:
            i += 1
        elif s[i] == '/' and not (keep_close and s[i:i + 2] == '/>'):
            i += 1
        else:
            break
    return i


def _tag_name_end(s, i):
    while i < len(s) and s[i] not in _SPACE + '/>':
        i += 1
    return i


def _attribute(s, i):
    """Return (end, name, value), or (-1, '', None) for a partial token."""
    first = i
    i += 1
    while i < len(s) and s[i] not in _SPACE + '/=>':
        i += 1
    name = s[first:i].lower()
    value = None
    probe = i
    while probe < len(s) and s[probe] in _SPACE:
        probe += 1
    if probe < len(s) and s[probe] == '=':
        i = probe + 1
        while i < len(s) and s[i] in _SPACE:
            i += 1
        if i < len(s) and s[i] in '\'"':
            quote = s[i]
            first = i + 1
            i = s.find(quote, first)
            if i < 0:
                # CPython's tolerant grammar can leave whitespace after '='
                # for the separator and accept an empty bare value. This
                # matters when the quoted value is still split across feeds.
                if first - 1 > probe + 1:
                    return probe + 1, name, ''
                return probe, name, None
            value = s[first:i]
            i += 1
        else:
            first = i
            while i < len(s) and s[i] not in _SPACE + '>':
                i += 1
            value = s[first:i]
    return i, name, value


def _whole_tag_end(s, i):
    i = _tag_name_end(s, i)
    while True:
        i = _skip_separators(s, i, False)
        if i == len(s):
            return -1
        if s[i] == '>':
            return i + 1
        if s[i - 1] not in _SPACE + '/\'"':
            return -1
        i, _name, _value = _attribute(s, i)
        if i < 0:
            return -1


def _entityref_match(s, i):
    j = i + 1
    if j >= len(s) or not _is_letter(s[j]):
        return None
    j += 1
    while j < len(s) and (_is_alnum(s[j]) or s[j] in '-.'):
        j += 1
    # The upstream regex can backtrack to its last '-' or '.' delimiter.
    if j == len(s):
        j = max(s.rfind('-', i + 2), s.rfind('.', i + 2))
        if j < 0:
            return None
    return s[i + 1:j], j + 1


def _charref_match(s, i):
    j = i + 2
    digits = '0123456789'
    if j < len(s) and s[j] in 'xX':
        j += 1
        digits = '0123456789abcdefABCDEF'
    first = j
    while j < len(s) and s[j] in digits:
        j += 1
    if j == first or j == len(s) or s[j] in '0123456789abcdefABCDEF':
        return None
    return s[i + 2:j], j + 1


def _incomplete_charref(s, i):
    j = i + 2
    if j < len(s) and s[j] in '0123456789':
        return True
    return j + 1 < len(s) and s[j] in 'xX' and s[j + 1] in '0123456789abcdefABCDEF'


class HTMLParser:
    """Find tags and other markup and call handler functions.

    Usage:
        p = HTMLParser()
        p.feed(data)
        ...
        p.close()

    Start tags are handled by calling self.handle_starttag() or
    self.handle_startendtag(); end tags by self.handle_endtag().  The
    data between tags is passed from the parser to the derived class
    by calling self.handle_data() with the data as argument (the data
    may be split up in arbitrary chunks).  If convert_charrefs is
    True the character references are converted automatically to the
    corresponding Unicode character (and self.handle_data() is no
    longer split in chunks), otherwise they are passed by calling
    self.handle_entityref() or self.handle_charref() with the string
    containing respectively the named or numeric reference as the
    argument.
    """

    # See the HTML5 specs section "13.4 Parsing HTML fragments".
    # https://html.spec.whatwg.org/multipage/parsing.html#parsing-html-fragments
    # CDATA_CONTENT_ELEMENTS are parsed in RAWTEXT mode
    CDATA_CONTENT_ELEMENTS = ("script", "style", "xmp", "iframe", "noembed", "noframes")
    RCDATA_CONTENT_ELEMENTS = ("textarea", "title")

    def __init__(self, *, convert_charrefs=True, scripting=False):
        """Initialize and reset this instance.

        If convert_charrefs is true (the default), all character references
        are automatically converted to the corresponding Unicode characters.

        If *scripting* is false (the default), the content of the
        ``noscript`` element is parsed normally; if it's true,
        it's returned as is without being parsed.
        """
        self.convert_charrefs = convert_charrefs
        self.scripting = scripting
        self.reset()

    def reset(self):
        """Reset this instance.  Loses all unprocessed data."""
        self.rawdata = ''
        self.lasttag = '???'
        self.cdata_elem = None
        self._support_cdata = True
        self._escapable = True
        self._pending = []
        self._pending_len = 0
        self._parse_threshold = 1
        self.lineno = 1
        self.offset = 0

    def feed(self, data):
        r"""Feed data to the parser.

        Call this as often as you want, with as little or as much text
        as you want (may include '\n').
        """
        # Accumulate new data in a list and only join and parse it once
        # enough has piled up.  Rescanning an unparsed buffer (e.g. an
        # unterminated tag) and concatenating onto it on every call would
        # both be quadratic in the input size.
        self._pending_len += len(data)
        if self._pending_len < self._parse_threshold:
            self._pending.append(data)
        else:
            if not self._pending:
                self.rawdata += data
            else:
                self._pending.append(data)
                self.rawdata += ''.join(self._pending)
                self._pending.clear()
            self._pending_len = 0
            n = len(self.rawdata)
            self.goahead(0)
            if len(self.rawdata) < n:
                # Some data was parsed; resume on the next call.
                self._parse_threshold = 1
            else:
                # Nothing was parsed; wait until the buffer doubles.
                self._parse_threshold = len(self.rawdata)

    def close(self):
        """Handle any buffered data."""
        if self._pending:
            self.rawdata += ''.join(self._pending)
            self._pending.clear()
            self._pending_len = 0
        self.goahead(1)

    __starttag_text = None

    def get_starttag_text(self):
        """Return full source of start tag: '<...>'."""
        return self.__starttag_text

    def set_cdata_mode(self, elem, *, escapable=False):
        self.cdata_elem = elem.lower()
        self._escapable = escapable

    def clear_cdata_mode(self):
        self.cdata_elem = None
        self._escapable = True

    def getpos(self):
        """Return the current line number and offset."""
        return self.lineno, self.offset

    def updatepos(self, i, j):
        if i >= j:
            return j
        data = self.rawdata[i:j]
        nlines = data.count('\n')
        if nlines:
            self.lineno += nlines
            self.offset = len(data) - data.rfind('\n') - 1
        else:
            self.offset += j - i
        return j

    def _find_interesting(self, rawdata, i):
        if not self.cdata_elem:
            while i < len(rawdata):
                if rawdata[i] in '&<':
                    return i
                i += 1
            return -1
        if self.cdata_elem == 'plaintext':
            return len(rawdata)
        marker = '</' + self.cdata_elem
        while i < len(rawdata):
            if self._escapable and not self.convert_charrefs and rawdata[i] == '&':
                return i
            end = i + len(marker)
            if (end < len(rawdata) and _ascii_lower(rawdata[i:end]) == marker
                    and rawdata[end] in _SPACE + '/>'):
                return i
            i += 1
        return -1

    def _set_support_cdata(self, flag=True):
        """Enable or disable support of the CDATA sections.
        If enabled, "<[CDATA[" starts a CDATA section which ends with "]]>".
        If disabled, "<[CDATA[" starts a bogus comments which ends with ">".

        This method is not called by default. Its purpose is to be called
        in custom handle_starttag() and handle_endtag() methods, with
        value that depends on the adjusted current node.
        See https://html.spec.whatwg.org/multipage/parsing.html#markup-declaration-open-state
        for details.
        """
        self._support_cdata = flag

    # Internal -- handle data as far as reasonable.  May leave state
    # and data to be processed by a subsequent call.  If 'end' is
    # true, force handling all data as if followed by EOF marker.
    def goahead(self, end):
        rawdata = self.rawdata
        i = 0
        n = len(rawdata)
        while i < n:
            if self.convert_charrefs and not self.cdata_elem:
                j = rawdata.find('<', i)
                if j < 0:
                    # if we can't find the next <, either we are at the end
                    # or there's more text incoming.  If the latter is True,
                    # we can't pass the text to handle_data in case we have
                    # a charref cut in half at end.  Try to determine if
                    # this is the case before proceeding by looking for an
                    # & near the end and see if it's followed by a space or ;.
                    amppos = rawdata.rfind('&', max(i, n-34))
                    if (amppos >= 0 and
                        not any(ch in _SPACE + ';' for ch in rawdata[amppos:])):
                        break  # wait till we get all the text
                    j = n
            else:
                match_pos = self._find_interesting(rawdata, i)
                if match_pos >= 0:
                    j = match_pos
                else:
                    if self.cdata_elem:
                        break
                    j = n
            if i < j:
                if self.convert_charrefs and self._escapable:
                    self.handle_data(unescape(rawdata[i:j]))
                else:
                    self.handle_data(rawdata[i:j])
            i = self.updatepos(i, j)
            if i == n: break
            startswith = rawdata.startswith
            if startswith('<', i):
                if _tag_open(rawdata, i): # < + letter
                    k = self.parse_starttag(i)
                elif startswith("</", i):
                    k = self.parse_endtag(i)
                elif startswith("<!--", i):
                    k = self.parse_comment(i)
                elif startswith("<?", i):
                    k = self.parse_pi(i)
                elif startswith("<!", i):
                    k = self.parse_html_declaration(i)
                elif (i + 1) < n or end:
                    self.handle_data("<")
                    k = i + 1
                else:
                    break
                if k < 0:
                    if not end:
                        break
                    if _tag_open(rawdata, i):  # < + letter
                        pass
                    elif startswith("</", i):
                        if i + 2 == n:
                            self.handle_data("</")
                        elif _endtag_open(rawdata, i):  # </ + letter
                            pass
                        else:
                            # bogus comment
                            self.handle_comment(rawdata[i+2:])
                    elif startswith("<!--", i):
                        j = n
                        for suffix in ("--!", "--", "-"):
                            if rawdata.endswith(suffix, i+4):
                                j -= len(suffix)
                                break
                        self.handle_comment(rawdata[i+4:j])
                    elif startswith("<![CDATA[", i) and self._support_cdata:
                        self.unknown_decl(rawdata[i+3:])
                    elif rawdata[i:i+9].lower() == '<!doctype':
                        self.handle_decl(rawdata[i+2:])
                    elif startswith("<!", i):
                        # bogus comment
                        self.handle_comment(rawdata[i+2:])
                    elif startswith("<?", i):
                        self.handle_pi(rawdata[i+2:])
                    else:
                        raise AssertionError("we should not get here!")
                    k = n
                i = self.updatepos(i, k)
            elif startswith("&#", i):
                match = _charref_match(rawdata, i)
                if match:
                    name = match[0]
                    self.handle_charref(name)
                    k = match[1]
                    if not startswith(';', k-1):
                        k = k - 1
                    i = self.updatepos(i, k)
                    continue
                match = _incomplete_charref(rawdata, i)
                if match:
                    if end:
                        self.handle_charref(rawdata[i+2:])
                        i = self.updatepos(i, n)
                        break
                    # incomplete
                    break
                elif i + 3 < n:  # larger than "&#x"
                    # not the end of the buffer, and can't be confused
                    # with some other construct
                    self.handle_data("&#")
                    i = self.updatepos(i, i + 2)
                else:
                    break
            elif startswith('&', i):
                match = _entityref_match(rawdata, i)
                if match:
                    name = match[0]
                    self.handle_entityref(name)
                    k = match[1]
                    if not startswith(';', k-1):
                        k = k - 1
                    i = self.updatepos(i, k)
                    continue
                match = (i + 1 < n and (_is_letter(rawdata[i + 1]) or rawdata[i + 1] == '#'))
                if match:
                    if end:
                        self.handle_entityref(rawdata[i+1:])
                        i = self.updatepos(i, n)
                        break
                    # incomplete
                    break
                elif i + 1 < n:
                    # not the end of the buffer, and can't be confused
                    # with some other construct
                    self.handle_data("&")
                    i = self.updatepos(i, i + 1)
                else:
                    break
            else:
                assert 0, "interesting.search() lied"
        # end while
        if end and i < n:
            if self.convert_charrefs and self._escapable:
                self.handle_data(unescape(rawdata[i:n]))
            else:
                self.handle_data(rawdata[i:n])
            i = self.updatepos(i, n)
        self.rawdata = rawdata[i:]

    # Internal -- parse html declarations, return length or -1 if not terminated
    # See w3.org/TR/html5/tokenization.html#markup-declaration-open-state
    # See also parse_declaration in _markupbase
    def parse_html_declaration(self, i):
        rawdata = self.rawdata
        assert rawdata[i:i+2] == '<!', ('unexpected call to '
                                        'parse_html_declaration()')
        if rawdata[i:i+4] == '<!--':
            # this case is actually already handled in goahead()
            return self.parse_comment(i)
        elif rawdata[i:i+9] == '<![CDATA[' and self._support_cdata:
            j = rawdata.find(']]>', i+9)
            if j < 0:
                return -1
            self.unknown_decl(rawdata[i+3: j])
            return j + 3
        elif rawdata[i:i+9].lower() == '<!doctype':
            # find the closing >
            gtpos = rawdata.find('>', i+9)
            if gtpos == -1:
                return -1
            self.handle_decl(rawdata[i+2:gtpos])
            return gtpos+1
        else:
            return self.parse_bogus_comment(i)

    # Internal -- parse comment, return length or -1 if not terminated
    # see https://html.spec.whatwg.org/multipage/parsing.html#comment-start-state
    def parse_comment(self, i, report=True):
        rawdata = self.rawdata
        assert rawdata.startswith('<!--', i), 'unexpected call to parse_comment()'
        # An empty comment is abruptly closed by the first ">" or "->",
        # taking priority over a later "-->" or "--!>" close.
        start = i + 4
        if rawdata.startswith('>', start):
            finish = start + 1
        elif rawdata.startswith('->', start):
            finish = start + 2
        else:
            first = rawdata.find('-->', start)
            second = rawdata.find('--!>', start)
            if first < 0 and second < 0:
                return -1
            if first >= 0 and (second < 0 or first < second):
                start = first
                finish = first + 3
            else:
                start = second
                finish = second + 4
        if report:
            self.handle_comment(rawdata[i + 4:start])
        return finish

    # Internal -- parse bogus comment, return length or -1 if not terminated
    # see https://html.spec.whatwg.org/multipage/parsing.html#bogus-comment-state
    def parse_bogus_comment(self, i, report=1):
        rawdata = self.rawdata
        assert rawdata[i:i+2] in ('<!', '</'), ('unexpected call to '
                                                'parse_bogus_comment()')
        pos = rawdata.find('>', i+2)
        if pos == -1:
            return -1
        if report:
            self.handle_comment(rawdata[i+2:pos])
        return pos + 1

    # Internal -- parse processing instr, return end or -1 if not terminated
    def parse_pi(self, i):
        rawdata = self.rawdata
        assert rawdata[i:i+2] == '<?', 'unexpected call to parse_pi()'
        j = rawdata.find('>', i + 2)
        if j < 0:
            return -1
        self.handle_pi(rawdata[i + 2:j])
        return j + 1

    # Internal -- handle starttag, return end or -1 if not terminated
    def parse_starttag(self, i):
        # See the HTML5 specs section "13.2.5.8 Tag name state"
        # https://html.spec.whatwg.org/multipage/parsing.html#tag-name-state
        self.__starttag_text = None
        endpos = self.check_for_whole_start_tag(i)
        if endpos < 0:
            return endpos
        rawdata = self.rawdata
        self.__starttag_text = rawdata[i:endpos]

        # Now parse the data between i+1 and j into a tag and attrs
        attrs = []
        tag_end = _tag_name_end(rawdata, i + 1)
        self.lasttag = tag = rawdata[i + 1:tag_end].lower()
        k = _skip_separators(rawdata, tag_end, True)
        while k < endpos and rawdata[k] not in _SPACE + '/>':
            if rawdata[k - 1] not in _SPACE + '/\'"':
                break
            k, attrname, attrvalue = _attribute(rawdata, k)
            if attrvalue:
                attrvalue = _unescape_attrvalue(attrvalue)
            attrs.append((attrname, attrvalue))
            k = _skip_separators(rawdata, k, True)

        end = rawdata[k:endpos].strip()
        if end not in (">", "/>"):
            self.handle_data(rawdata[i:endpos])
            return endpos
        if end.endswith('/>'):
            # XHTML-style empty tag: <span attr="value" />
            self.handle_startendtag(tag, attrs)
        else:
            self.handle_starttag(tag, attrs)
            if (tag in self.CDATA_CONTENT_ELEMENTS or
                (self.scripting and tag == "noscript") or
                tag == "plaintext"):
                self.set_cdata_mode(tag, escapable=False)
            elif tag in self.RCDATA_CONTENT_ELEMENTS:
                self.set_cdata_mode(tag, escapable=True)
        return endpos

    # Internal -- check to see if we have a complete starttag; return end
    # or -1 if incomplete.
    def check_for_whole_start_tag(self, i):
        rawdata = self.rawdata
        return _whole_tag_end(rawdata, i + 1)

    # Internal -- parse endtag, return end or -1 if incomplete
    def parse_endtag(self, i):
        # See the HTML5 specs section "13.2.5.7 End tag open state"
        # https://html.spec.whatwg.org/multipage/parsing.html#end-tag-open-state
        rawdata = self.rawdata
        assert rawdata[i:i+2] == "</", "unexpected call to parse_endtag"
        if rawdata.find('>', i+2) < 0:  # fast check
            return -1
        if not _endtag_open(rawdata, i):  # </ + letter
            if rawdata[i+2:i+3] == '>':  # </> is ignored
                # "missing-end-tag-name" parser error
                return i+3
            else:
                return self.parse_bogus_comment(i)

        j = _whole_tag_end(rawdata, i + 2)
        if j < 0:
            return -1
        tag_end = _tag_name_end(rawdata, i + 2)
        tag = rawdata[i + 2:tag_end].lower()
        self.handle_endtag(tag)
        self.clear_cdata_mode()
        return j

    # Overridable -- finish processing of start+end tag: <tag.../>
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    # Overridable -- handle start tag
    def handle_starttag(self, tag, attrs):
        pass

    # Overridable -- handle end tag
    def handle_endtag(self, tag):
        pass

    # Overridable -- handle character reference
    def handle_charref(self, name):
        pass

    # Overridable -- handle entity reference
    def handle_entityref(self, name):
        pass

    # Overridable -- handle data
    def handle_data(self, data):
        pass

    # Overridable -- handle comment
    def handle_comment(self, data):
        pass

    # Overridable -- handle declaration
    def handle_decl(self, decl):
        pass

    # Overridable -- handle processing instruction
    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass
