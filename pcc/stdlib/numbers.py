"""Native-compilable subset of the standard :mod:`numbers` module.

The hierarchy and ``register`` call shape are sufficient for packages that
publish their scalar types during module initialization. Virtual-subclass
checks require the runtime ABC registry and are intentionally not claimed by
this module alone.
"""

from __future__ import annotations


class Number:
    @classmethod
    def register(cls, subclass):
        return subclass


class Complex(Number):
    @classmethod
    def register(cls, subclass):
        return subclass


class Real(Complex):
    @classmethod
    def register(cls, subclass):
        return subclass


class Rational(Real):
    @classmethod
    def register(cls, subclass):
        return subclass


class Integral(Rational):
    @classmethod
    def register(cls, subclass):
        return subclass


__all__ = ["Number", "Complex", "Real", "Rational", "Integral"]
