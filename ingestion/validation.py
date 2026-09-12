"""
Plausible-range checks for extracted stat values.

These are deliberately SOFT: they never raise (unlike db.repository.validate_stat_value,
which enforces hard, always-true constraints like "receptions can't be negative").
A plausibility flag lowers confidence and forces a field to the Review Queue —
it never blocks the observation from existing, because OCR/vision extraction
is exactly the situation where an unusual-but-real value (a huge game) and a
misread digit look identical without a human glancing at it.
"""
from typing import List, Optional

# Generous single-game upper bounds. Anything above these is not impossible
# (NFL records exist near some of these) but is unusual enough that a human
# should confirm it wasn't a misread digit before it becomes trusted data.
PLAUSIBLE_MAX = {
    "pass_yards": 550,
    "pass_tds": 7,
    "interceptions": 6,
    "rush_yards": 300,
    "rush_tds": 6,
    "receptions": 20,
    "receiving_yards": 300,
    "receiving_tds": 5,
    "fumbles_lost": 4,
    "pass_completions": 55,
    "pass_attempts": 70,
}


def plausibility_flags(stat_name: str, value: Optional[float], scope: str = "weekly") -> List[str]:
    """scope='season' (Phase 4C season-scope extension) deliberately skips this
    check entirely rather than reusing the single-game PLAUSIBLE_MAX table —
    a season total of 4200 pass yards is completely ordinary but vastly
    exceeds the single-game cap of 550, so applying the same thresholds would
    flag nearly every real season projection as implausible. A genuine
    season-scale plausibility table could be added later based on real
    historical season distributions; inventing one now without that basis
    would be exactly the kind of unfounded assumption this project avoids
    elsewhere, so for now scope='season' simply isn't range-checked here."""
    flags = []
    if value is None or scope != "weekly":
        return flags
    cap = PLAUSIBLE_MAX.get(stat_name)
    if cap is not None and value > cap:
        flags.append(f"{stat_name}={value} exceeds the plausible single-game range (>{cap}); verify against the source")
    return flags


def completions_attempts_flags(pass_completions: Optional[float], pass_attempts: Optional[float]) -> List[str]:
    """Cross-field consistency check (spec: completions <= attempts). This can only
    run when both values are known at once, so callers gather both fields for a
    player/week before invoking it — unlike plausibility_flags, which is single-field."""
    flags = []
    if pass_completions is not None and pass_attempts is not None and pass_completions > pass_attempts:
        flags.append(f"pass_completions={pass_completions} exceeds pass_attempts={pass_attempts}; verify against the source")
    return flags


def week_consistency_flags(asserted_week_number: Optional[int], extracted_week_hint: Optional[int]) -> List[str]:
    """Compares the week the user selected at upload time against whatever week
    number the extractor found printed on the screenshot itself."""
    flags = []
    if asserted_week_number is not None and extracted_week_hint is not None:
        if int(asserted_week_number) != int(extracted_week_hint):
            flags.append(
                f"selected week {asserted_week_number} does not match the week printed on the "
                f"screenshot ({extracted_week_hint}); verify before accepting"
            )
    return flags
