"""Form label rules shared by every site adapter and by the generic mapper. Profession neutral by construction."""

from __future__ import annotations

import re

# "Years of experience" in any profession's wording: "years of professional experience", "years of clinical
# experience", "years of B2B sales experience", "years of full-time experience". Up to two qualifying words.
YEARS_RULE_TEXT = r"years? of (?:[\w-]+ ){0,2}?experience"
YEARS_RULE = re.compile(YEARS_RULE_TEXT, re.I)

# Reason recorded on a personal/demographic question the candidate has not answered in their own profile.
SENSITIVE_BLANK_REASON = "sensitive disclosure: left blank unless you supply a preference"
PERSONAL_BLANK_REASON = "personal question: left blank unless you supply a preference"
