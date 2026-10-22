"""Native-compilable INI parsing for Python build tools.

This module implements the generic ``configparser`` surface exercised by
Meson machine files, wrap files, editorconfig files, and NumPy's legacy
``npy-pkg-config`` reader.  It deliberately keeps the public CPython spelling:
callers import ``configparser`` and the recursive stdlib resolver selects this
provider under native compilation.

The supported interpolation is classic ``%(name)s`` interpolation.  Extended
``${section:option}`` interpolation and custom converter registration remain
fail-closed rather than being accepted with subtly different behavior.
"""
from __future__ import annotations

import sys


DEFAULTSECT = "DEFAULT"
MAX_INTERPOLATION_DEPTH = 10
_UNSET = object()


class Error(Exception):
    def __init__(self, msg=""):
        self.message = msg
        super().__init__(msg)

    def __repr__(self):
        return self.message

    def __str__(self):
        return self.message


class NoSectionError(Error):
    def __init__(self, section):
        Error.__init__(self, "No section: " + repr(section))
        self.section = section
        self.args = (section,)


class DuplicateSectionError(Error):
    def __init__(self, section, source=None, lineno=None):
        if source is None:
            message = "Section " + repr(section) + " already exists"
        else:
            message = "While reading from " + repr(source)
            if lineno is not None:
                message += " [line " + ("%2d" % lineno) + "]"
            message += ": section " + repr(section) + " already exists"
        Error.__init__(self, message)
        self.section = section
        self.source = source
        self.lineno = lineno
        self.args = (section, source, lineno)


class DuplicateOptionError(Error):
    def __init__(self, section, option, source=None, lineno=None):
        if source is None:
            message = (
                "Option "
                + repr(option)
                + " in section "
                + repr(section)
                + " already exists"
            )
        else:
            message = "While reading from " + repr(source)
            if lineno is not None:
                message += " [line " + ("%2d" % lineno) + "]"
            message += (
                ": option "
                + repr(option)
                + " in section "
                + repr(section)
                + " already exists"
            )
        Error.__init__(self, message)
        self.section = section
        self.option = option
        self.source = source
        self.lineno = lineno
        self.args = (section, option, source, lineno)


class NoOptionError(Error):
    def __init__(self, option, section):
        Error.__init__(
            self,
            "No option " + repr(option) + " in section: " + repr(section),
        )
        self.option = option
        self.section = section
        self.args = (option, section)


class InterpolationError(Error):
    def __init__(self, option, section, msg):
        Error.__init__(self, msg)
        self.option = option
        self.section = section
        self.args = (option, section, msg)


class InterpolationMissingOptionError(InterpolationError):
    def __init__(self, option, section, rawval, reference):
        message = (
            "Bad value substitution: option "
            + repr(option)
            + " in section "
            + repr(section)
            + " contains an interpolation key "
            + repr(reference)
            + " which is not a valid option name. Raw value: "
            + repr(rawval)
        )
        InterpolationError.__init__(self, option, section, message)
        self.reference = reference
        self.args = (option, section, rawval, reference)


class InterpolationSyntaxError(InterpolationError):
    pass


class InterpolationDepthError(InterpolationError):
    def __init__(self, option, section, rawval):
        message = (
            "Recursion limit exceeded in value substitution: option "
            + repr(option)
            + " in section "
            + repr(section)
            + " contains an interpolation key which cannot be substituted in "
            + str(MAX_INTERPOLATION_DEPTH)
            + " steps. Raw value: "
            + repr(rawval)
        )
        InterpolationError.__init__(self, option, section, message)
        self.args = (option, section, rawval)


class ParsingError(Error):
    def __init__(self, source=None, filename=None):
        if filename is not None and source is not None:
            raise ValueError(
                "Cannot specify both `filename' and `source'. Use `source'."
            )
        if source is None:
            source = filename
        if source is None:
            raise ValueError("Required argument `source' not given.")
        Error.__init__(self, "Source contains parsing errors: " + repr(source))
        self.source = source
        self.errors = []
        self.args = (source,)

    @property
    def filename(self):
        return self.source

    @filename.setter
    def filename(self, value):
        self.source = value

    def append(self, lineno, line):
        self.errors.append((lineno, line))
        self.message += "\n\t[line " + ("%2d" % lineno) + "]: " + line


class MissingSectionHeaderError(ParsingError):
    def __init__(self, filename, lineno, line):
        Error.__init__(
            self,
            "File contains no section headers.\nfile: "
            + repr(filename)
            + ", line: "
            + str(lineno)
            + "\n"
            + repr(line),
        )
        self.source = filename
        self.lineno = lineno
        self.line = line
        self.args = (filename, lineno, line)


class Interpolation:
    def before_get(self, parser, section, option, value, defaults):
        return value

    def before_set(self, parser, section, option, value):
        return value

    def before_read(self, parser, section, option, value):
        return value

    def before_write(self, parser, section, option, value):
        return value


class BasicInterpolation(Interpolation):
    def before_get(self, parser, section, option, value, defaults):
        return parser._interpolate_value(section, option, value, defaults, 1)

    def before_set(self, parser, section, option, value):
        index = 0
        while index < len(value):
            percent = value.find("%", index)
            if percent < 0:
                break
            if percent + 1 < len(value) and value[percent + 1] == "%":
                index = percent + 2
                continue
            if percent + 1 < len(value) and value[percent + 1] == "(":
                close = value.find(")s", percent + 2)
                if close >= 0:
                    index = close + 2
                    continue
            raise ValueError(
                "invalid interpolation syntax in "
                + repr(value)
                + " at position "
                + str(percent)
            )
        return value


class ExtendedInterpolation(Interpolation):
    def before_get(self, parser, section, option, value, defaults):
        if "$" not in value:
            return value
        raise NotImplementedError(
            "ExtendedInterpolation awaits native cross-section interpolation"
        )

    def before_set(self, parser, section, option, value):
        if "$" in value:
            raise NotImplementedError(
                "ExtendedInterpolation awaits native cross-section interpolation"
            )
        return value

    def before_read(self, parser, section, option, value):
        if "$" in value:
            raise NotImplementedError(
                "ExtendedInterpolation awaits native cross-section interpolation"
            )
        return value


class LegacyInterpolation(BasicInterpolation):
    def before_get(self, parser, section, option, value, defaults):
        raise NotImplementedError(
            "LegacyInterpolation is not part of the native build-tool surface"
        )

    def before_set(self, parser, section, option, value):
        raise NotImplementedError(
            "LegacyInterpolation is not part of the native build-tool surface"
        )

    def before_read(self, parser, section, option, value):
        raise NotImplementedError(
            "LegacyInterpolation is not part of the native build-tool surface"
        )


def _indent_level(line):
    count = 0
    while count < len(line):
        character = line[count]
        if character != " " and character != "\t":
            break
        count += 1
    return count


def _first_delimiter(line, delimiters):
    best_index = -1
    best_delimiter = ""
    for delimiter in delimiters:
        position = line.find(delimiter)
        if position >= 0 and (best_index < 0 or position < best_index):
            best_index = position
            best_delimiter = delimiter
    return best_index, best_delimiter


def _option_split(text, delimiters):
    # CPython 3.15.0 matches option, \s*, delimiter, \s*, value on the stripped
    # line. The option stops before the first delimiter; the greedy \s* makes
    # the delimiter the last one starting in the whitespace after the option.
    first_index, first_delimiter = _first_delimiter(text, delimiters)
    if first_index < 0:
        return -1, -1
    option_end = first_index
    while option_end > 0 and text[option_end - 1].isspace():
        option_end -= 1
    position = option_end
    while position < len(text) and text[position].isspace():
        position += 1
    while position >= option_end:
        for delimiter in delimiters:
            if text[position : position + len(delimiter)] == delimiter:
                return option_end, position + len(delimiter)
        position -= 1
    return -1, -1


def _strip_inline_comment(value, prefixes):
    if prefixes is None:
        return value
    cut = -1
    for prefix in prefixes:
        start = 0
        while start < len(value):
            position = value.find(prefix, start)
            if position < 0:
                break
            if position == 0 or value[position - 1].isspace():
                if cut < 0 or position < cut:
                    cut = position
                break
            start = position + len(prefix)
    if cut >= 0:
        return value[:cut].rstrip()
    return value


class RawConfigParser:
    BOOLEAN_STATES = {
        "1": True,
        "yes": True,
        "true": True,
        "on": True,
        "0": False,
        "no": False,
        "false": False,
        "off": False,
    }

    def __init__(
        self,
        defaults=None,
        dict_type=None,
        allow_no_value=False,
        delimiters=("=", ":"),
        comment_prefixes=("#", ";"),
        inline_comment_prefixes=None,
        strict=True,
        empty_lines_in_values=True,
        default_section=DEFAULTSECT,
        interpolation=None,
        converters=None,
    ):
        if dict_type is not None and dict_type is not dict:
            raise NotImplementedError("custom configparser dict_type is unsupported")
        if converters:
            raise NotImplementedError(
                "custom configparser converters await native descriptor support"
            )
        self._defaults = {}
        self._sections = {}
        self._delimiters = tuple(delimiters)
        self._comment_prefixes = tuple(comment_prefixes)
        self._inline_comment_prefixes = (
            None
            if inline_comment_prefixes is None
            else tuple(inline_comment_prefixes)
        )
        self._strict = bool(strict)
        self._allow_no_value = bool(allow_no_value)
        self._empty_lines_in_values = bool(empty_lines_in_values)
        self.default_section = default_section
        self._interpolation = interpolation
        if defaults is not None:
            for key, value in defaults.items():
                option = self.optionxform(str(key))
                self._defaults[option] = str(value)

    def defaults(self):
        return self._defaults

    def sections(self):
        return list(self._sections.keys())

    def add_section(self, section):
        if section == self.default_section:
            raise ValueError("Invalid section name: " + repr(section))
        if section in self._sections:
            raise DuplicateSectionError(section)
        self._sections[section] = {}

    def has_section(self, section):
        return section in self._sections

    def options(self, section):
        if section == self.default_section:
            return list(self._defaults.keys())
        if section not in self._sections:
            raise NoSectionError(section)
        # CPython copies the concrete section first and then updates it with
        # defaults.  Existing section keys retain their source order while
        # only missing defaults are appended.
        result = list(self._sections[section].keys())
        for option in self._defaults.keys():
            if option not in result:
                result.append(option)
        return result

    def optionxform(self, optionstr):
        return optionstr.lower()

    def read(self, filenames, encoding=None):
        if isinstance(filenames, (str, bytes)):
            names = [filenames]
        else:
            try:
                names = list(filenames)
            except TypeError:
                names = [filenames]
        successful = []
        for filename in names:
            path = str(filename)
            try:
                with open(path, "r", encoding=encoding) as stream:
                    self.read_file(stream, source=path)
            except OSError:
                continue
            # CPython normalizes os.PathLike entries with os.fspath before
            # returning them.  This freestanding subset already opened the
            # normalized string, so return that same stable representation.
            successful.append(path)
        return successful

    def read_file(self, f, source=None):
        if source is None:
            source = getattr(f, "name", "<???>")
        self._read_lines(f, source)

    def readfp(self, fp, filename=None):
        self.read_file(fp, source=filename)

    def read_string(self, string, source="<string>"):
        # CPython reads io.StringIO(string), whose lines end only at "\n".
        text = str(string)
        lines = []
        start = 0
        end = text.find("\n")
        while end >= 0:
            lines.append(text[start : end + 1])
            start = end + 1
            end = text.find("\n", start)
        if start < len(text):
            lines.append(text[start:])
        self._read_lines(lines, source)

    def read_dict(self, dictionary, source="<dict>"):
        seen_sections = []
        for section, keys in dictionary.items():
            section = str(section)
            if self._strict and section in seen_sections:
                raise DuplicateSectionError(section, source)
            seen_sections.append(section)
            if section != self.default_section and section not in self._sections:
                self.add_section(section)
            seen_options = []
            for key, value in keys.items():
                option = self.optionxform(str(key))
                if self._strict and option in seen_options:
                    raise DuplicateOptionError(section, option, source)
                seen_options.append(option)
                self.set(section, option, None if value is None else str(value))

    def _read_lines(self, lines, source):
        current_section = None
        current_name = ""
        current_option = None
        current_indent = 0
        pending_empty_lines = 0
        seen_sections = []
        seen_options = []
        parsing_error = None
        line_number = 0

        for original_line in lines:
            line_number += 1
            line = original_line.rstrip("\r\n")
            stripped = line.strip()

            if stripped == "":
                if (
                    self._empty_lines_in_values
                    and current_section is not None
                    and current_option is not None
                ):
                    pending_empty_lines += 1
                else:
                    current_option = None
                    pending_empty_lines = 0
                continue

            is_comment = False
            for prefix in self._comment_prefixes:
                if stripped.startswith(prefix):
                    is_comment = True
                    break
            if is_comment:
                continue

            indent = _indent_level(line)
            if (
                current_section is not None
                and current_option is not None
                and indent > current_indent
            ):
                old_value = current_section.get(current_option)
                if old_value is None:
                    raise NotImplementedError(
                        "multiline continuation after a valueless option is unsupported"
                    )
                continuation = _strip_inline_comment(
                    stripped, self._inline_comment_prefixes
                )
                if continuation == "":
                    continue
                separators = pending_empty_lines + 1
                current_section[current_option] = (
                    old_value + ("\n" * separators) + continuation
                )
                pending_empty_lines = 0
                continue

            pending_empty_lines = 0

            if stripped.startswith("["):
                close = stripped.find("]")
                if close > 1:
                    remainder = stripped[close + 1 :].strip()
                    valid_remainder = remainder == ""
                    for prefix in self._comment_prefixes:
                        if remainder.startswith(prefix):
                            valid_remainder = True
                    if valid_remainder:
                        section = stripped[1:close]
                        if self._strict and section in seen_sections:
                            raise DuplicateSectionError(section, source, line_number)
                        seen_sections.append(section)
                        current_name = section
                        if section == self.default_section:
                            current_section = self._defaults
                        else:
                            if section not in self._sections:
                                self._sections[section] = {}
                            current_section = self._sections[section]
                        current_option = None
                        current_indent = indent
                        continue

            if current_section is None:
                raise MissingSectionHeaderError(source, line_number, original_line)

            option_end, value_start = _option_split(stripped, self._delimiters)
            if option_end < 0:
                if self._allow_no_value:
                    option_text = stripped
                    value = None
                else:
                    if parsing_error is None:
                        parsing_error = ParsingError(source)
                    parsing_error.append(line_number, original_line)
                    current_option = None
                    continue
            else:
                option_text = stripped[:option_end]
                value = stripped[value_start:].strip()
                value = _strip_inline_comment(value, self._inline_comment_prefixes)

            if option_text == "":
                if parsing_error is None:
                    parsing_error = ParsingError(source)
                parsing_error.append(line_number, original_line)
                current_option = None
                continue

            option = self.optionxform(option_text.rstrip())
            option_key = current_name + "\x00" + option
            if self._strict and option_key in seen_options:
                raise DuplicateOptionError(
                    current_name,
                    option,
                    source,
                    line_number,
                )
            seen_options.append(option_key)
            if value is not None and self._interpolation is not None:
                value = self._interpolation.before_read(
                    self, current_name, option, value
                )
            current_section[option] = value
            current_option = option
            current_indent = indent

        if parsing_error is not None:
            raise parsing_error

    def _unify_values(self, section, vars=None):
        if section == self.default_section:
            section_values = {}
        elif section in self._sections:
            section_values = self._sections[section]
        else:
            raise NoSectionError(section)
        values = {}
        for key, value in self._defaults.items():
            values[key] = value
        for key, value in section_values.items():
            values[key] = value
        if vars is not None:
            for key, value in vars.items():
                values[self.optionxform(str(key))] = None if value is None else str(value)
        return values

    def _interpolate_value(self, section, option, value, values, depth):
        if depth > MAX_INTERPOLATION_DEPTH:
            raise InterpolationDepthError(option, section, value)
        result = ""
        position = 0
        while position < len(value):
            percent = value.find("%", position)
            if percent < 0:
                result += value[position:]
                break
            result += value[position:percent]
            if percent + 1 >= len(value):
                raise InterpolationSyntaxError(
                    option,
                    section,
                    "'%' must be followed by '%' or '(', found: " + repr(value[percent:]),
                )
            marker = value[percent + 1]
            if marker == "%":
                result += "%"
                position = percent + 2
                continue
            if marker != "(":
                raise InterpolationSyntaxError(
                    option,
                    section,
                    "'%' must be followed by '%' or '(', found: " + repr(value[percent:]),
                )
            close = value.find(")s", percent + 2)
            if close < 0:
                raise InterpolationSyntaxError(
                    option,
                    section,
                    "bad interpolation variable reference " + repr(value[percent:]),
                )
            reference = self.optionxform(value[percent + 2 : close])
            if reference not in values:
                raise InterpolationMissingOptionError(
                    option, section, value, reference
                )
            replacement = values[reference]
            if replacement is None:
                replacement = ""
            if "%" in replacement:
                replacement = self._interpolate_value(
                    section,
                    option,
                    replacement,
                    values,
                    depth + 1,
                )
            result += replacement
            position = close + 2
        return result

    def get(self, section, option, raw=False, vars=None, fallback=_UNSET):
        option = self.optionxform(option)
        try:
            values = self._unify_values(section, vars)
        except NoSectionError:
            if fallback is _UNSET:
                raise
            return fallback
        if option not in values:
            if fallback is _UNSET:
                raise NoOptionError(option, section)
            return fallback
        value = values[option]
        if value is None or raw or self._interpolation is None:
            return value
        return self._interpolation.before_get(
            self, section, option, value, values
        )

    def getint(self, section, option, raw=False, vars=None, fallback=_UNSET):
        value = self.get(section, option, raw=raw, vars=vars, fallback=fallback)
        if value is fallback and fallback is not _UNSET:
            return fallback
        return int(value)

    def getfloat(self, section, option, raw=False, vars=None, fallback=_UNSET):
        value = self.get(section, option, raw=raw, vars=vars, fallback=fallback)
        if value is fallback and fallback is not _UNSET:
            return fallback
        return float(value)

    def getboolean(self, section, option, raw=False, vars=None, fallback=_UNSET):
        value = self.get(section, option, raw=raw, vars=vars, fallback=fallback)
        if value is fallback and fallback is not _UNSET:
            return fallback
        normalized = str(value).lower()
        if normalized not in self.BOOLEAN_STATES:
            raise ValueError("Not a boolean: " + str(value))
        return self.BOOLEAN_STATES[normalized]

    def items(self, section=_UNSET, raw=False, vars=None):
        if section is _UNSET:
            result = [(self.default_section, self[self.default_section])]
            for name in self.sections():
                result.append((name, self[name]))
            return result
        values = self._unify_values(section, vars)
        result = []
        for option in values.keys():
            if raw:
                value = values[option]
            else:
                value = self.get(section, option, raw=False, vars=vars)
            result.append((option, value))
        return result

    def has_option(self, section, option):
        option = self.optionxform(option)
        if section == self.default_section:
            return option in self._defaults
        if section not in self._sections:
            return False
        return option in self._sections[section] or option in self._defaults

    def set(self, section, option, value=None):
        if value is None and not self._allow_no_value:
            raise TypeError("option values must be strings")
        if value is not None and not isinstance(value, str):
            raise TypeError("option values must be strings")
        option = self.optionxform(option)
        if value is not None and self._interpolation is not None:
            value = self._interpolation.before_set(self, section, option, value)
        if section == self.default_section:
            mapping = self._defaults
        else:
            if section not in self._sections:
                raise NoSectionError(section)
            mapping = self._sections[section]
        mapping[option] = value

    def remove_option(self, section, option):
        option = self.optionxform(option)
        if section == self.default_section:
            mapping = self._defaults
        else:
            if section not in self._sections:
                raise NoSectionError(section)
            mapping = self._sections[section]
        if option not in mapping:
            return False
        del mapping[option]
        return True

    def remove_section(self, section):
        if section not in self._sections:
            return False
        del self._sections[section]
        return True

    def write(self, fp, space_around_delimiters=True):
        delimiter = self._delimiters[0]
        if space_around_delimiters:
            delimiter = " " + delimiter + " "
        if self._defaults:
            self._write_section(fp, self.default_section, self._defaults, delimiter)
        for section in self.sections():
            self._write_section(fp, section, self._sections[section], delimiter)

    def _write_section(self, fp, section, values, delimiter):
        fp.write("[" + section + "]\n")
        for option, value in values.items():
            if value is None:
                fp.write(option + "\n")
            else:
                rendered = str(value).replace("\n", "\n\t")
                fp.write(option + delimiter + rendered + "\n")
        fp.write("\n")

    def clear(self):
        self._defaults.clear()
        self._sections.clear()

    def __len__(self):
        return len(self._sections) + 1

    def __iter__(self):
        return iter([self.default_section] + self.sections())

    def __contains__(self, key):
        return key == self.default_section or key in self._sections

    def __getitem__(self, key):
        if key != self.default_section and key not in self._sections:
            raise KeyError(key)
        return SectionProxy(self, key)

    def __setitem__(self, key, value):
        if key == self.default_section:
            self._defaults.clear()
        else:
            if key in self._sections:
                self._sections[key].clear()
            else:
                self.add_section(key)
        for option, option_value in value.items():
            self.set(key, option, str(option_value))

    def __delitem__(self, key):
        if key == self.default_section:
            raise ValueError("Cannot remove the default section.")
        if not self.remove_section(key):
            raise KeyError(key)

    def keys(self):
        return [self.default_section] + self.sections()

    def values(self):
        return [self[key] for key in self.keys()]


class ConfigParser(RawConfigParser):
    def __init__(
        self,
        defaults=None,
        dict_type=None,
        allow_no_value=False,
        delimiters=("=", ":"),
        comment_prefixes=("#", ";"),
        inline_comment_prefixes=None,
        strict=True,
        empty_lines_in_values=True,
        default_section=DEFAULTSECT,
        interpolation=_UNSET,
        converters=None,
    ):
        if interpolation is _UNSET:
            interpolation = BasicInterpolation()
        RawConfigParser.__init__(
            self,
            defaults=defaults,
            dict_type=dict_type,
            allow_no_value=allow_no_value,
            delimiters=delimiters,
            comment_prefixes=comment_prefixes,
            inline_comment_prefixes=inline_comment_prefixes,
            strict=strict,
            empty_lines_in_values=empty_lines_in_values,
            default_section=default_section,
            interpolation=interpolation,
            converters=converters,
        )


class SectionProxy:
    def __init__(self, parser, name):
        self._parser = parser
        self._name = name

    @property
    def parser(self):
        return self._parser

    @property
    def name(self):
        return self._name

    def get(self, option, fallback=None, raw=False, vars=None):
        return self._parser.get(
            self._name,
            option,
            raw=raw,
            vars=vars,
            fallback=fallback,
        )

    def getint(self, option, fallback=None, raw=False, vars=None):
        return self._parser.getint(
            self._name, option, raw=raw, vars=vars, fallback=fallback
        )

    def getfloat(self, option, fallback=None, raw=False, vars=None):
        return self._parser.getfloat(
            self._name, option, raw=raw, vars=vars, fallback=fallback
        )

    def getboolean(self, option, fallback=None, raw=False, vars=None):
        return self._parser.getboolean(
            self._name, option, raw=raw, vars=vars, fallback=fallback
        )

    def __getitem__(self, key):
        # pcc's current physical ``dict(iterable)`` projection consumes a
        # sequence of 2-tuples.  Keep that compiler-only projection out of the
        # semantic CPython surface: on CPython an integer follows ordinary
        # option transformation (and fails in the same way as ConfigParser).
        # Meson's
        # ``dict(parser[section])`` can therefore stay no-libpython without a
        # package-name branch, while direct execution of this port remains a
        # differential oracle for SectionProxy.
        if sys.implementation.name == "pcc" and isinstance(key, int):
            keys = self.keys()
            index = key
            if index < 0:
                index += len(keys)
            if index < 0 or index >= len(keys):
                raise IndexError("SectionProxy index out of range")
            option = keys[index]
            return (option, self._parser.get(self._name, option))
        if not self._parser.has_option(self._name, key):
            raise KeyError(key)
        return self._parser.get(self._name, key)

    def __setitem__(self, key, value):
        self._parser.set(self._name, key, value)

    def __delitem__(self, key):
        if not self._parser.remove_option(self._name, key):
            raise KeyError(key)

    def __contains__(self, key):
        return self._parser.has_option(self._name, key)

    def __len__(self):
        return len(self._parser.options(self._name))

    def __iter__(self):
        return iter(self._parser.options(self._name))

    def keys(self):
        return self._parser.options(self._name)

    def items(self):
        return self._parser.items(self._name)

    def values(self):
        return [self[key] for key in self.keys()]


SafeConfigParser = ConfigParser


__all__ = [
    "DEFAULTSECT",
    "MAX_INTERPOLATION_DEPTH",
    "Error",
    "NoSectionError",
    "DuplicateSectionError",
    "DuplicateOptionError",
    "NoOptionError",
    "InterpolationError",
    "InterpolationMissingOptionError",
    "InterpolationSyntaxError",
    "InterpolationDepthError",
    "ParsingError",
    "MissingSectionHeaderError",
    "Interpolation",
    "BasicInterpolation",
    "ExtendedInterpolation",
    "LegacyInterpolation",
    "RawConfigParser",
    "ConfigParser",
    "SafeConfigParser",
    "SectionProxy",
]
