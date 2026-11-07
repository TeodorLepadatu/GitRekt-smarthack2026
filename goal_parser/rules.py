"""Rule-based goal extraction.

Used when the LLM is unavailable, times out, or returns something the
validator rejects. It produces the same *raw* shape as the LLM, so both paths
go through the same validation in :mod:`goal_parser.schema`.

The text helpers here (:func:`times_in`, :func:`unit_amounts`) are also used to
check the LLM's output against what the user actually wrote.
"""
from __future__ import annotations

import re
from typing import Any

from .schema import OVERNIGHT_WINDOW, normalize_time

_NUM = r"(-?\d+(?:[.,]\d+)?)"
# A clock time. Not preceded or followed by more digits, and not followed by an
# energy unit, so "by 6.5 MWh", "by 2 MWh" or the "00" in "1500" is never a time.
_TIME = r"(?<![\d.,:])(\d{1,2}(?:[:.h]\d{2})?(?:\s*(?:am|pm))?h?|noon|midnight)(?![\d.,]|\s*[mk]wh)"
_ENERGY_RE = re.compile(rf"(?<![\w.,]){_NUM}\s*(mwh|kwh)\b")
_PRICE = (
    r"(?:€|eur(?:os?)?)?\s*" + _NUM +
    r"\s*(€|eur(?:os?)?\b|ct\b|cents?\b)?\s*(?:/|per|a)?\s*(mwh|kwh)?"
)

_MIN_SELL_RES = [
    re.compile(
        r"\b(?:never|don'?t|do not|not|no)\s+(?:sell|export|discharge)\w*\b.*?"
        r"\b(?:below|under|less than|cheaper than|lower than)\s+" + _PRICE
    ),
    re.compile(r"\bmin(?:imum)?\.?\s+(?:sell(?:ing)?|export|sale)\s+price\s+(?:of\s+|is\s+|=\s*|:\s*)?" + _PRICE),
    re.compile(
        r"\b(?:only\s+(?:sell|export)\w*|(?:sell|export)\w*\s+only)\b.*?"
        r"\b(?:above|over|for more than|at least)\s+" + _PRICE
    ),
]

_PROFIT_RE = re.compile(r"\b(profit|revenue|money|earn\w*|income)\b")
_SELF_RE = re.compile(
    r"\b(own (?:energy|power|production|electricity|solar|wind)|my (?:solar|wind|panels)"
    r"|self[- ]?(?:consum\w*|sufficien\w*|use)|avoid(?:ing)? (?:the )?grid"
    r"|independen\w* (?:of|from) the grid)\b"
)
_RESERVE_WORD_RE = re.compile(r"\b(reserve|backup|back-up|buffer|store|keep|save|hold|stored)\b")
_OVERNIGHT_RE = re.compile(r"\b(over\s?night|at night|through the night|nightly)\b")
_WINDOW_RE = re.compile(rf"\b(?:from|between)\s+{_TIME}\s*(?:to|until|till|and|-)\s*{_TIME}")
_BY_RE = re.compile(rf"\b(?:by|before|at)\s+{_TIME}")
_UNTIL_RE = re.compile(rf"\b(?:until|till|til|through)\s+{_TIME}")

# Words that carry no goal on their own ("Actually, ...", "Thanks!").
_FILLER_RE = re.compile(
    r"^(?:(?:actually|also|and|but|so|then|ok(?:ay)?|please|hi|hello|hey|thanks?(?: you)?|"
    r"i mean|instead|oh|well|yes|no)\b[\s,]*)+"
)

# Split on sentence ends, but never inside a decimal ("6.5 MWh") or a time ("18.30").
_SENTENCE_SPLIT_RE = re.compile(r"[\n;!?]+|\.(?!\d)")
# Split "A and B" only when B starts a new instruction, so
# "between 22:00 and 06:00" stays together.
_CLAUSE_SPLIT_RE = re.compile(
    r"\s*(?:,|\band\b|\bbut\b|\bthen\b|\balso\b)\s*(?=(?:also\s+)?(?:i\s+(?:also\s+)?want\s+(?:to\s+)?|please\s+)?"
    r"(?:store|keep|never|don'?t|do not|maximi[sz]e|use|reserve|have|hold|charge|only|sell|make|save)\b)"
)

_UNIT_AMOUNT_RES = [
    re.compile(rf"(?<![\w.,]){_NUM}\s*(?:mwh|kwh|€|eur\w*|ct\b|cents?\b)"),
    re.compile(rf"(?:€|\beur\w*)\s*{_NUM}"),
]


def normalize_text(text: str) -> str:
    """Lower-case and rewrite spelled-out units and times into the short forms
    the patterns expect. Only used for matching; ``source_text`` keeps the
    user's own words."""
    t = text.lower().replace("’", "'")
    t = re.sub(r"\bmega\s?watt[- ]?hours?\b", "mwh", t)
    t = re.sub(r"\bkilo\s?watt[- ]?hours?\b", "kwh", t)
    t = re.sub(r"\s*o'?clock\b", "", t)
    t = re.sub(r"\b12\s+at night\b", "midnight", t)
    t = re.sub(r"\b(\d{1,2}(?::\d{2})?)\s+(?:in the (?:evening|afternoon)|at night|p\.m\.)", r"\1 pm", t)
    t = re.sub(r"\b(\d{1,2}(?::\d{2})?)\s+(?:in the morning|a\.m\.)", r"\1 am", t)
    return t


def split_clauses(text: str) -> list[str]:
    clauses = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        for clause in _CLAUSE_SPLIT_RE.split(sentence):
            clause = clause.strip(" ,.")
            if clause:
                clauses.append(clause)
    return clauses


def times_in(text: str) -> set[str]:
    """Every clock time written in ``text``, as ``HH:MM`` (plus the overnight
    window if the user said "overnight")."""
    low = normalize_text(text)
    found = set()
    for m in re.finditer(_TIME, low):
        try:
            found.add(normalize_time(m.group(1)))
        except ValueError:
            pass
    if _OVERNIGHT_RE.search(low):
        found.update(OVERNIGHT_WINDOW)
    return found


def unit_amounts(text: str) -> set[float]:
    """Numbers written next to an energy or money unit: the amounts a goal must use."""
    low = normalize_text(text)
    return {
        float(m.group(1).replace(",", "."))
        for pattern in _UNIT_AMOUNT_RES
        for m in pattern.finditer(low)
    }


def parse_raw(text: str) -> dict[str, Any]:
    """Extract objective, raw goals and clarifications from goal text."""
    objective = None
    goals: list[dict[str, Any]] = []
    clarifications: list[dict[str, str]] = []

    for clause in split_clauses(text):
        low = _FILLER_RE.sub("", normalize_text(clause)).strip(" ,.")
        if not low:
            continue  # pure filler: "Thanks!", "Actually,"
        matched = False

        # A later objective replaces an earlier one ("..., actually use my own energy first").
        if _SELF_RE.search(low):
            objective, matched = "self_consumption", True
        elif _PROFIT_RE.search(low):
            objective, matched = "max_profit", True

        price_goal = _min_sell_price(low, clause)
        if price_goal is not None:
            goals.append(price_goal)
            continue

        energy = _ENERGY_RE.search(low)
        if energy:
            goals.append(_reserve(low, clause, energy))
            continue

        if _RESERVE_WORD_RE.search(low):
            clarifications.append({
                "source_text": clause,
                "question": f'How much energy (in MWh) should I keep for "{clause}", and by what time?',
            })
            continue

        if not matched:
            # Hand it to the validator so the question wording lives in one place.
            goals.append({"type": "unknown", "source_text": clause})

    return {"objective": objective, "goals": goals, "clarifications": clarifications}


def _min_sell_price(low: str, clause: str) -> dict[str, Any] | None:
    for pattern in _MIN_SELL_RES:
        m = pattern.search(low)
        if m:
            amount, currency, per = m.group(1), m.group(2), m.group(3)
            if currency and currency.startswith(("ct", "cent")):
                unit = f"ct/{per or 'kwh'}"
            else:
                unit = f"EUR/{per or 'mwh'}"
            return {"type": "min_sell_price", "amount": amount, "unit": unit, "source_text": clause}
    return None


def _reserve(low: str, clause: str, energy: re.Match) -> dict[str, Any]:
    by = until = None
    if _OVERNIGHT_RE.search(low):
        by, until = OVERNIGHT_WINDOW
    elif (w := _WINDOW_RE.search(low)):
        by, until = w.group(1), w.group(2)
    else:
        if (b := _BY_RE.search(low)):
            by = b.group(1)
        if (u := _UNTIL_RE.search(low)):
            until = u.group(1)
    return {
        "type": "reserve",
        "amount": energy.group(1),
        "unit": energy.group(2),
        "by": _disambiguate(by, low),
        "until": _disambiguate(until, low),
        "source_text": clause,
    }


_PAREN_RE = re.compile(r"\(([^)]*)\)")
_PM_WORD_RE = re.compile(r"\b(pm|evening|afternoon|night|tonight)\b")
_AM_WORD_RE = re.compile(r"\b(am|morning)\b")


def _disambiguate(t: str | None, low: str) -> str | None:
    """Resolve a bare hour like "6" with the user's answer in brackets, e.g.
    "by 6 (6 pm)", "by 6 (18:00)" or "by 6 (evening)"."""
    if t is None or not re.fullmatch(r"\d{1,2}", t.strip()) or not 1 <= int(t) <= 12:
        return t
    h = int(t)
    candidates = {f"{h % 12:02d}:00", f"{h % 12 + 12:02d}:00"}
    for reply in _PAREN_RE.findall(low):
        for m in re.finditer(_TIME, reply):
            try:
                hhmm = normalize_time(m.group(1))
            except ValueError:
                continue
            if hhmm in candidates and not re.fullmatch(r"\d{1,2}", m.group(1).strip()):
                return hhmm
        if _PM_WORD_RE.search(reply):
            return f"{h} pm"
        if _AM_WORD_RE.search(reply):
            return f"{h} am"
    return t

