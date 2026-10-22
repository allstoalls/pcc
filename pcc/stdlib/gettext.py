"""Native build-tool subset of :mod:`gettext`.

The no-catalog path is fully deterministic and matches CPython:
``NullTranslations`` delegates through fallbacks, module-level singular and
plural helpers return source messages, and domains can be selected.  Binary
GNU ``.mo`` parsing and locale-directory discovery remain explicit unsupported
boundaries; a bound domain is never silently treated as untranslated.
"""

from __future__ import annotations


class NullTranslations:
    def __init__(self, fp=None) -> None:
        self._info = {}
        self._charset = None
        self._fallback = None
        if fp is not None:
            self._parse(fp)

    def _parse(self, fp) -> None:
        return None

    def add_fallback(self, fallback) -> None:
        if self._fallback is not None:
            self._fallback.add_fallback(fallback)
        else:
            self._fallback = fallback

    def gettext(self, message: str) -> str:
        if self._fallback is not None:
            return self._fallback.gettext(message)
        return message

    def ngettext(self, msgid1: str, msgid2: str, n: int) -> str:
        if self._fallback is not None:
            return self._fallback.ngettext(msgid1, msgid2, n)
        return msgid1 if n == 1 else msgid2

    def pgettext(self, context: str, message: str) -> str:
        if self._fallback is not None:
            return self._fallback.pgettext(context, message)
        return message

    def npgettext(
        self,
        context: str,
        msgid1: str,
        msgid2: str,
        n: int,
    ) -> str:
        if self._fallback is not None:
            return self._fallback.npgettext(context, msgid1, msgid2, n)
        return msgid1 if n == 1 else msgid2

    def info(self):
        return self._info

    def charset(self):
        return self._charset

    def install(self, names=None) -> None:
        raise NotImplementedError(
            "gettext.install awaits owned builtins namespace mutation"
        )


class GNUTranslations(NullTranslations):
    def _parse(self, fp) -> None:
        raise NotImplementedError("GNU gettext .mo parsing is not yet native")


_localedirs = {}
_current_domain = "messages"


def textdomain(domain=None) -> str:
    global _current_domain
    if domain is not None:
        _current_domain = domain
    return _current_domain


def bindtextdomain(domain: str, localedir=None) -> str:
    if localedir is not None:
        _localedirs[domain] = localedir
    if domain in _localedirs:
        return _localedirs[domain]
    raise NotImplementedError(
        "unbound gettext locale-directory discovery is not yet native"
    )


def translation(
    domain: str,
    localedir=None,
    languages=None,
    class_=None,
    fallback: bool = False,
):
    if localedir is not None or languages is not None or class_ is not None:
        raise NotImplementedError(
            "gettext catalogue discovery and .mo parsing are not yet native"
        )
    if fallback:
        return NullTranslations()
    raise FileNotFoundError("No translation file found for domain: " + domain)


def install(domain: str, localedir=None, *, names=None) -> None:
    raise NotImplementedError(
        "gettext.install awaits catalogue and builtins namespace ownership"
    )


def _bound_domain_guard(domain: str) -> None:
    if domain in _localedirs:
        raise NotImplementedError(
            "bound gettext domains require native .mo catalogue parsing"
        )


def dgettext(domain: str, message: str) -> str:
    _bound_domain_guard(domain)
    return message


def dngettext(domain: str, msgid1: str, msgid2: str, n: int) -> str:
    _bound_domain_guard(domain)
    return msgid1 if n == 1 else msgid2


def dpgettext(domain: str, context: str, message: str) -> str:
    _bound_domain_guard(domain)
    return message


def dnpgettext(
    domain: str,
    context: str,
    msgid1: str,
    msgid2: str,
    n: int,
) -> str:
    _bound_domain_guard(domain)
    return msgid1 if n == 1 else msgid2


def gettext(message: str) -> str:
    return dgettext(_current_domain, message)


def ngettext(msgid1: str, msgid2: str, n: int) -> str:
    return dngettext(_current_domain, msgid1, msgid2, n)


def pgettext(context: str, message: str) -> str:
    return dpgettext(_current_domain, context, message)


def npgettext(context: str, msgid1: str, msgid2: str, n: int) -> str:
    return dnpgettext(_current_domain, context, msgid1, msgid2, n)


Catalog = translation
