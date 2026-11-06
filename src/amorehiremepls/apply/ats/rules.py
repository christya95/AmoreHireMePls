"""Form label rules shared by every site adapter and by the generic mapper. Profession neutral by construction."""

from __future__ import annotations

import re

# "Years of experience" in any profession's wording: "years of professional experience", "years of clinical
# experience", "years of B2B sales experience", "years of full-time experience". Up to two qualifying words.
YEARS_RULE_TEXT = r"years? of (?:[\w-]+ ){0,2}?experience"
YEARS_RULE = re.compile(YEARS_RULE_TEXT, re.I)

# Range options a years question may offer: "2-4 years", "Less than 2 years", "4+ years", "more than 4".
_RANGE = re.compile(r"(\d+)\s*(?:-|–|to)\s*(\d+)")
_LESS = re.compile(r"(?:less than|under|fewer than|<)\s*(\d+)")
_MORE = re.compile(r"(\d+)\s*\+|(?:more than|over|at least|>)\s*(\d+)")


def pick_years_bucket(options: list[str], years: int) -> str | None:
    """The option whose range holds `years`: a closed range first, then an open one ("less than", "4+")."""
    for o in options:
        m = _RANGE.search(o.lower())
        if m and int(m.group(1)) <= years <= int(m.group(2)):
            return o
    for o in options:
        low = o.lower()
        m = _LESS.search(low)
        if m and years < int(m.group(1)):
            return o
        m = _MORE.search(low)
        if m and years >= int(m.group(1) or m.group(2)):
            return o
    return None


# Reason recorded on a personal/demographic question the candidate has not answered in their own profile.
SENSITIVE_BLANK_REASON = "sensitive disclosure: left blank unless you supply a preference"
PERSONAL_BLANK_REASON = "personal question: left blank unless you supply a preference"
