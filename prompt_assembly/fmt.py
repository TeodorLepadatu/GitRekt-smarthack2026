"""Number formatting shared by every prompt section.

The same value always looks the same, in the solved examples and in the
current situation, so the small LLM can compare numbers as text.
"""
from __future__ import annotations

from typing import Any

from common import finite_float


class PromptAssemblyError(ValueError):
    """The inputs cannot be turned into a prompt (missing or non-numeric field)."""


def num(value: Any, name: str) -> float:
    out = finite_float(value)
    if out is None:
        raise PromptAssemblyError(f"{name} must be a finite number, got {value!r}")
    return out


def mw(x: float) -> str:
    """Power / energy: up to 2 decimals, trailing zeros kept to one (3.0, 0.4, 2.11)."""
    s = f"{x:.2f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def qty(x: float) -> str:
    """Amounts people typed (goal targets): 6 rather than 6.0, 1.5 stays 1.5."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def signed_mw(x: float) -> str:
    return ("+" if x > 0 else "") + mw(x)


def frac(x: float) -> str:
    """battery_fraction-like values: always 2 decimals."""
    return f"{x:.2f}"


def signed_frac(x: float) -> str:
    return ("+" if x > 0 else "") + frac(x)


def eur(x: float) -> str:
    """Prices: whole euros unless there is a real fractional part."""
    return f"{x:.0f}" if abs(x - round(x)) < 1e-9 else f"{x:.1f}"
