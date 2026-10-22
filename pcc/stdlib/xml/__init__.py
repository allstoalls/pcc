"""Finite native XML package used by build-tool providers.

Only :mod:`xml.etree.ElementTree` is owned.  DOM, SAX, DTD processing and
external entities remain outside the native runtime claim.
"""

__all__ = ["etree"]
