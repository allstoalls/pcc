"""pcc.stdlib.argparse - small native command-line parser subset."""

from __future__ import annotations

import sys


class ArgumentTypeError(Exception):
    pass


class Namespace:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self):
        return "Namespace()"


class _Action:
    def __init__(
        self,
        option_strings,
        dest,
        default=None,
        action=None,
        type=None,
        choices=None,
        help=None,
        version=None,
    ):
        self.option_strings = option_strings
        self.dest = dest
        self.default = default
        self.action = action or "store"
        self.type = type
        self.choices = choices
        self.help = help
        self.version = version


def _copy_default(value):
    if isinstance(value, list):
        out = []
        for item in value:
            out.append(item)
        return out
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out[key] = item
        return out
    return value


def _strip_option_prefix(text: str) -> str:
    i = 0
    while i < len(text) and text[i] == "-":
        i += 1
    return text[i:].replace("-", "_")


def _looks_like_option(text: str) -> bool:
    return len(text) > 1 and text[0] == "-"


class ArgumentParser:
    def __init__(
        self,
        prog=None,
        usage=None,
        description=None,
        epilog=None,
        **kwargs,
    ):
        self.prog = prog or (sys.argv[0] if sys.argv else "")
        self.usage = usage
        self.description = description
        self.epilog = epilog
        self._actions = []
        self._option_map = {}
        self._positionals = []

    def add_argument(
        self,
        *option_strings,
        dest=None,
        default=None,
        action=None,
        type=None,
        choices=None,
        help=None,
        version=None,
        **kwargs,
    ):
        opts = []
        for opt in option_strings:
            opts.append(opt)
        if dest is None:
            if opts:
                primary = opts[0]
                for opt in opts:
                    if opt.startswith("--"):
                        primary = opt
                        break
                dest = _strip_option_prefix(primary)
            else:
                raise ValueError("dest is required for positional arguments")
        spec = _Action(opts, dest, default, action, type, choices, help, version)
        self._actions.append(spec)
        if opts:
            for opt in opts:
                self._option_map[opt] = spec
        else:
            self._positionals.append(spec)
        return spec

    def _seed_defaults(self, namespace):
        for spec in self._actions:
            if spec.action == "store_true":
                value = False if spec.default is None else spec.default
            elif spec.action == "count":
                value = spec.default
            elif spec.default is not None:
                value = _copy_default(spec.default)
            else:
                value = None
            setattr(namespace, spec.dest, value)

    def _convert(self, spec, text):
        value = text
        if spec.type is not None:
            try:
                value = spec.type(text)
            except ArgumentTypeError:
                raise
            except Exception as exc:
                raise ArgumentTypeError(str(exc))
        if spec.choices is not None and value not in spec.choices:
            raise ArgumentTypeError("invalid choice: " + str(value))
        return value

    def _store_value(self, namespace, spec, value):
        if spec.action == "append":
            current = getattr(namespace, spec.dest, None)
            if current is None:
                current = []
                setattr(namespace, spec.dest, current)
            current.append(value)
        elif spec.action == "count":
            current = getattr(namespace, spec.dest, None)
            if current is None:
                current = 0
            setattr(namespace, spec.dest, current + 1)
        elif spec.action == "store_true":
            setattr(namespace, spec.dest, True)
        else:
            setattr(namespace, spec.dest, value)

    def _try_expand_cluster(self, token):
        # Expand a stacked single-dash short-option cluster like "-vv" into
        # ["-v", "-v"], matching CPython argparse. Returns the expanded token
        # list, or None when the cluster contains an unknown short option (in
        # which case the caller leaves the original token for normal handling).
        chars = token[1:]
        out = []
        k = 0
        n = len(chars)
        while k < n:
            opt = "-" + chars[k]
            if opt not in self._option_map:
                return None
            spec = self._option_map[opt]
            if (
                spec.action == "store_true"
                or spec.action == "count"
                or spec.action == "version"
            ):
                out.append(opt)
                k += 1
            else:
                # value-taking short option: the rest of the cluster is its
                # value (e.g. "-pNUM"); if nothing trails, the next argv token
                # is the value, handled by the normal parse loop.
                out.append(opt)
                rest = chars[k + 1 :]
                if len(rest) > 0:
                    out.append(rest)
                return out
        return out

    def _expand_short_clusters(self, tokens):
        out = []
        for token in tokens:
            if (
                len(token) >= 3
                and token[0] == "-"
                and token[1] != "-"
                and token not in self._option_map
            ):
                expanded = self._try_expand_cluster(token)
                if expanded is None:
                    out.append(token)
                else:
                    for t in expanded:
                        out.append(t)
            else:
                out.append(token)
        return out

    def parse_args(self, args=None, namespace=None):
        tokens = sys.argv[1:] if args is None else args
        tokens = self._expand_short_clusters(tokens)
        ns = namespace or Namespace()
        self._seed_defaults(ns)
        positional_index = 0
        i = 0
        while i < len(tokens):
            token = tokens[i]
            value_text = None
            opt_token = token
            if token.startswith("--") and "=" in token:
                opt_token, _, value_text = token.partition("=")
            if opt_token in self._option_map:
                spec = self._option_map[opt_token]
                if spec.action == "version":
                    text = spec.version or ""
                    print(text.replace("%(prog)s", self.prog))
                    sys.exit(0)
                if spec.action == "store_true" or spec.action == "count":
                    self._store_value(ns, spec, None)
                else:
                    if value_text is None:
                        i += 1
                        if i >= len(tokens):
                            raise SystemExit(2)
                        value_text = tokens[i]
                    self._store_value(ns, spec, self._convert(spec, value_text))
            else:
                if _looks_like_option(token):
                    raise SystemExit(2)
                if positional_index >= len(self._positionals):
                    raise SystemExit(2)
                spec = self._positionals[positional_index]
                positional_index += 1
                self._store_value(ns, spec, self._convert(spec, token))
            i += 1
        return ns

    def parse_known_args(self, args=None, namespace=None):
        return self.parse_args(args, namespace), []

    def error(self, message):
        raise SystemExit(2)

    def print_help(self, file=None):
        if self.description:
            if file is None:
                print(self.description)
            else:
                file.write(str(self.description) + "\n")
