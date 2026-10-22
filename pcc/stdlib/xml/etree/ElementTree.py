"""Bounded, dependency-free ElementTree surface for native build tools.

Owned syntax: UTF-8 XML declarations, elements, quoted attributes, comments,
CDATA, the five predefined entities and numeric character references.  DTDs,
external/general entities, namespace registration, XInclude, pull/streaming
parsers and arbitrary XPath fail closed.  The XPath subset covers direct child
lookups plus ``.//tag`` and ``.//tag[@attribute]``, which is the surface used by
Meson's JUnit and Qt resource handling.
"""
from __future__ import annotations

import builtins


class ParseError(SyntaxError):
    def __init__(self, message, position=None):
        super().__init__(message)
        self.code = 0
        self.position = (0, 0) if position is None else position


def _require_text(value, what):
    if not isinstance(value, str):
        raise TypeError(what + " must be str")
    return value


class Element:
    def __init__(self, tag, attrib=None, **extra):
        self.tag = _require_text(tag, "element tag")
        self.attrib = {}
        if attrib is not None:
            for key, value in attrib.items():
                self.attrib[_require_text(key, "attribute name")] = _require_text(
                    value, "attribute value"
                )
        for key, value in extra.items():
            self.attrib[_require_text(key, "attribute name")] = _require_text(
                value, "attribute value"
            )
        self.text = None
        self.tail = None
        self._children = []

    def __len__(self):
        return len(self._children)

    def __iter__(self):
        return iter(self._children)

    def __getitem__(self, index):
        return self._children[index]

    def __setitem__(self, index, element):
        if not iselement(element):
            raise TypeError("expected an Element")
        self._children[index] = element

    def __delitem__(self, index):
        del self._children[index]

    def append(self, element):
        if not iselement(element):
            raise TypeError("append() argument must be an Element")
        self._children.append(element)

    def extend(self, elements):
        for element in elements:
            self.append(element)

    def insert(self, index, element):
        if not iselement(element):
            raise TypeError("insert() argument must be an Element")
        self._children.insert(index, element)

    def remove(self, element):
        self._children.remove(element)

    def clear(self):
        self.attrib.clear()
        self._children.clear()
        self.text = None
        self.tail = None

    def get(self, key, default=None):
        return self.attrib.get(key, default)

    def set(self, key, value):
        self.attrib[_require_text(key, "attribute name")] = _require_text(
            value, "attribute value"
        )

    def keys(self):
        return list(self.attrib.keys())

    def items(self):
        return list(self.attrib.items())

    def iter(self, tag=None):
        if tag is None or tag == "*" or self.tag == tag:
            yield self
        for child in self._children:
            for descendant in child.iter(tag):
                yield descendant

    def itertext(self):
        if self.text is not None:
            yield self.text
        for child in self._children:
            for value in child.itertext():
                yield value
            if child.tail is not None:
                yield child.tail

    def findall(self, path, namespaces=None):
        if namespaces not in (None, {}):
            raise NotImplementedError("ElementTree namespace maps are not runtime-owned")
        return _findall(self, path)

    def find(self, path, namespaces=None):
        found = self.findall(path, namespaces)
        return None if len(found) == 0 else found[0]

    def findtext(self, path, default=None, namespaces=None):
        found = self.find(path, namespaces)
        if found is None:
            return default
        return "" if found.text is None else found.text


def iselement(element):
    return isinstance(element, Element)


def SubElement(parent, tag, attrib=None, **extra):
    if not iselement(parent):
        raise TypeError("SubElement() parent must be an Element")
    element = Element(tag, {} if attrib is None else attrib, **extra)
    parent.append(element)
    return element


def _selector(segment):
    if segment == "" or segment == ".":
        return ("*", None, None)
    if "[" not in segment:
        return (segment, None, None)
    if not segment.endswith("]") or "[@" not in segment:
        raise NotImplementedError("ElementTree XPath predicate is not runtime-owned")
    tag, predicate = segment.split("[@", 1)
    predicate = predicate[:-1]
    if "=" not in predicate:
        return (tag or "*", predicate, None)
    name, value = predicate.split("=", 1)
    value = value.strip()
    if len(value) < 2 or value[0] not in ("'", '"') or value[-1] != value[0]:
        raise NotImplementedError("ElementTree XPath value is not a literal")
    return (tag or "*", name.strip(), value[1:-1])


def _matches(element, selector):
    tag, attribute, value = selector
    if tag != "*" and element.tag != tag:
        return False
    if attribute is None:
        return True
    if attribute not in element.attrib:
        return False
    return value is None or element.attrib[attribute] == value


def _findall(element, path):
    path = _require_text(path, "ElementTree path")
    if path == ".":
        return [element]
    if path == "":
        raise NotImplementedError("empty ElementTree paths are not runtime-owned")
    descendant = False
    if path.startswith(".//"):
        descendant = True
        path = path[3:]
    elif path.startswith("./"):
        path = path[2:]
    if "//" in path or path.startswith("/") or path.endswith("/"):
        raise NotImplementedError("ElementTree XPath shape is not runtime-owned")
    segments = path.split("/") if path else ["*"]
    current = []
    if descendant:
        first = _selector(segments[0])
        for candidate in element.iter():
            if candidate is not element and _matches(candidate, first):
                current.append(candidate)
        segments = segments[1:]
    else:
        current = list(element._children)
    for segment_index, segment in enumerate(segments):
        selector = _selector(segment)
        if not descendant and segment_index == 0:
            current = [candidate for candidate in current if _matches(candidate, selector)]
            continue
        next_values = []
        for candidate in current:
            for child in candidate._children:
                if _matches(child, selector):
                    next_values.append(child)
        current = next_values
    return current


def _decode_entities(value):
    out = ""
    index = 0
    while index < len(value):
        if value[index] != "&":
            out += value[index]
            index += 1
            continue
        end = value.find(";", index + 1)
        if end < 0:
            raise ParseError("unterminated XML entity")
        name = value[index + 1 : end]
        replacements = {
            "amp": "&",
            "lt": "<",
            "gt": ">",
            "quot": '"',
            "apos": "'",
        }
        if name in replacements:
            out += replacements[name]
        elif name.startswith("#x") or name.startswith("#X"):
            try:
                out += chr(int(name[2:], 16))
            except Exception:
                raise ParseError("invalid hexadecimal XML character reference")
        elif name.startswith("#"):
            try:
                out += chr(int(name[1:], 10))
            except Exception:
                raise ParseError("invalid decimal XML character reference")
        else:
            raise ParseError("undefined XML entity: &" + name + ";")
        index = end + 1
    return out


def _find_tag_end(source, start):
    quote = ""
    index = start
    while index < len(source):
        char = source[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
        elif char == ">":
            return index
        index += 1
    raise ParseError("unclosed XML tag")


def _start_tag(value):
    value = value.strip()
    if value == "":
        raise ParseError("empty XML tag")
    index = 0
    while index < len(value) and not value[index].isspace():
        index += 1
    tag = value[:index]
    attributes = {}
    while index < len(value):
        while index < len(value) and value[index].isspace():
            index += 1
        if index >= len(value):
            break
        name_start = index
        while index < len(value) and not value[index].isspace() and value[index] != "=":
            index += 1
        name = value[name_start:index]
        while index < len(value) and value[index].isspace():
            index += 1
        if index >= len(value) or value[index] != "=":
            raise ParseError("XML attribute is missing '='")
        index += 1
        while index < len(value) and value[index].isspace():
            index += 1
        if index >= len(value) or value[index] not in ("'", '"'):
            raise ParseError("XML attribute value must be quoted")
        quote = value[index]
        index += 1
        value_start = index
        while index < len(value) and value[index] != quote:
            index += 1
        if index >= len(value):
            raise ParseError("unterminated XML attribute")
        if name in attributes:
            raise ParseError("duplicate XML attribute: " + name)
        attributes[name] = _decode_entities(value[value_start:index])
        index += 1
    return (tag, attributes)


def _append_text(stack, text):
    if text == "":
        return
    decoded = _decode_entities(text)
    if len(stack) == 0:
        if decoded.strip() != "":
            raise ParseError("text outside the document element")
        return
    parent = stack[-1]
    if len(parent._children) == 0:
        parent.text = (parent.text or "") + decoded
    else:
        child = parent._children[-1]
        child.tail = (child.tail or "") + decoded


def fromstring(text, parser=None):
    if parser is not None:
        raise NotImplementedError("custom ElementTree parsers are not runtime-owned")
    if isinstance(text, bytes):
        raw = bytes(text)
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        text = raw.decode("utf-8")
    text = _require_text(text, "XML source")
    index = 0
    stack = []
    root = None
    while index < len(text):
        marker = text.find("<", index)
        if marker < 0:
            _append_text(stack, text[index:])
            index = len(text)
            break
        _append_text(stack, text[index:marker])
        if text.startswith("<!--", marker):
            end = text.find("-->", marker + 4)
            if end < 0:
                raise ParseError("unclosed XML comment")
            index = end + 3
            continue
        if text.startswith("<![CDATA[", marker):
            end = text.find("]]>", marker + 9)
            if end < 0:
                raise ParseError("unclosed XML CDATA section")
            if len(stack) == 0:
                raise ParseError("CDATA outside the document element")
            _append_text(stack, text[marker + 9 : end].replace("&", "&amp;"))
            index = end + 3
            continue
        if text.startswith("<?", marker):
            end = text.find("?>", marker + 2)
            if end < 0:
                raise ParseError("unclosed XML processing instruction")
            index = end + 2
            continue
        if text.startswith("<!", marker):
            raise ParseError("DTD and declaration markup are not runtime-owned")
        end = _find_tag_end(text, marker + 1)
        body = text[marker + 1 : end]
        if body.startswith("/"):
            tag = body[1:].strip()
            if len(stack) == 0 or stack[-1].tag != tag:
                raise ParseError("mismatched XML end tag: " + tag)
            stack.pop()
            index = end + 1
            continue
        self_closing = body.rstrip().endswith("/")
        if self_closing:
            body = body.rstrip()[:-1]
        tag, attributes = _start_tag(body)
        element = Element(tag, attributes)
        if len(stack) > 0:
            stack[-1].append(element)
        elif root is None:
            root = element
        else:
            raise ParseError("multiple XML document elements")
        if not self_closing:
            stack.append(element)
        index = end + 1
    if len(stack) != 0:
        raise ParseError("unclosed XML element: " + stack[-1].tag)
    if root is None:
        raise ParseError("no XML document element")
    return root


XML = fromstring


def parse(source, parser=None):
    if hasattr(source, "read"):
        data = source.read()
    else:
        with builtins.open(str(source), "rb") as stream:
            data = stream.read()
    return ElementTree(fromstring(data, parser=parser))


def _escape_text(value):
    value = _require_text(value, "element text")
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attribute(value):
    return _escape_text(value).replace('"', "&quot;").replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;")


def _serialize(element, short_empty_elements):
    attributes = ""
    for key, value in element.attrib.items():
        attributes += " " + key + '=\"' + _escape_attribute(value) + '\"'
    if len(element._children) == 0 and element.text is None and short_empty_elements:
        result = "<" + element.tag + attributes + " />"
    else:
        result = "<" + element.tag + attributes + ">"
        if element.text is not None:
            result += _escape_text(element.text)
        for child in element._children:
            result += _serialize(child, short_empty_elements)
            if child.tail is not None:
                result += _escape_text(child.tail)
        result += "</" + element.tag + ">"
    return result


def _ascii_xml(value):
    out = ""
    for char in value:
        codepoint = ord(char)
        out += char if codepoint < 128 else "&#" + str(codepoint) + ";"
    return out


def tostring(
    element,
    encoding="us-ascii",
    method="xml",
    *,
    xml_declaration=None,
    default_namespace=None,
    short_empty_elements=True,
):
    if not iselement(element):
        raise TypeError("tostring() expects an Element")
    if method != "xml":
        raise NotImplementedError("ElementTree non-XML serialization is not runtime-owned")
    if default_namespace is not None:
        raise NotImplementedError("ElementTree default namespaces are not runtime-owned")
    encoding = str(encoding)
    body = _serialize(element, bool(short_empty_elements))
    declaration = ""
    if xml_declaration is True:
        label = "utf-8" if encoding == "unicode" else encoding
        declaration = "<?xml version='1.0' encoding='" + label + "'?>\n"
    body = declaration + body
    if encoding == "unicode":
        return body
    normalized = encoding.lower().replace("_", "-")
    if normalized in ("utf-8", "utf8"):
        return body.encode("utf-8")
    if normalized in ("us-ascii", "ascii"):
        return _ascii_xml(body).encode("ascii")
    raise NotImplementedError("ElementTree encoding is not runtime-owned: " + encoding)


def tostringlist(element, encoding="us-ascii", method="xml", *, xml_declaration=None, default_namespace=None, short_empty_elements=True):
    return [
        tostring(
            element,
            encoding,
            method,
            xml_declaration=xml_declaration,
            default_namespace=default_namespace,
            short_empty_elements=short_empty_elements,
        )
    ]


class ElementTree:
    def __init__(self, element=None, file=None):
        if file is not None:
            if element is not None:
                raise TypeError("ElementTree accepts either element or file")
            parsed = parse(file)
            self._root = parsed.getroot()
        else:
            self._root = element

    def getroot(self):
        return self._root

    def _setroot(self, element):
        if not iselement(element):
            raise TypeError("_setroot() expects an Element")
        self._root = element

    def findall(self, path, namespaces=None):
        return [] if self._root is None else self._root.findall(path, namespaces)

    def find(self, path, namespaces=None):
        return None if self._root is None else self._root.find(path, namespaces)

    def iter(self, tag=None):
        return iter(()) if self._root is None else self._root.iter(tag)

    def write(
        self,
        file_or_filename,
        encoding="us-ascii",
        xml_declaration=None,
        default_namespace=None,
        method="xml",
        *,
        short_empty_elements=True,
    ):
        if self._root is None:
            raise ValueError("ElementTree has no root")
        data = tostring(
            self._root,
            encoding,
            method,
            xml_declaration=xml_declaration,
            default_namespace=default_namespace,
            short_empty_elements=short_empty_elements,
        )
        if hasattr(file_or_filename, "write"):
            file_or_filename.write(data)
            return None
        if isinstance(data, str):
            with builtins.open(
                str(file_or_filename), "w", encoding="utf-8"
            ) as stream:
                stream.write(data)
        else:
            with builtins.open(str(file_or_filename), "wb") as stream:
                stream.write(data)
        return None


def indent(tree, space="  ", level=0):
    root = tree.getroot() if isinstance(tree, ElementTree) else tree
    if not iselement(root):
        raise TypeError("indent() expects an Element or ElementTree")

    def visit(element, depth):
        if len(element._children) == 0:
            return
        prefix = "\n" + space * (depth + 1)
        if element.text is None or element.text.strip() == "":
            element.text = prefix
        for index, child in enumerate(element._children):
            visit(child, depth + 1)
            if child.tail is None or child.tail.strip() == "":
                child.tail = "\n" + space * depth if index == len(element._children) - 1 else prefix

    visit(root, int(level))


__all__ = [
    "ParseError",
    "Element",
    "SubElement",
    "ElementTree",
    "iselement",
    "parse",
    "fromstring",
    "XML",
    "tostring",
    "tostringlist",
    "indent",
]
