"""
matcher.py
==========
Multi-strategy name matching engine with SSML pronunciation support.

Strategies (in order):
  1. Exact match (case-insensitive, accent-normalised)
  2. Phonetic match using Soundex (jellyfish)
  3. Fuzzy match (rapidfuzz token_sort_ratio)

For short/ambiguous names, a confidence margin check ensures the best
match is meaningfully better than the second-best before accepting.

TTS pronunciation overrides are stored in extensionAttribute1 in Azure AD.
SSML output is XML-escaped to prevent injection from config values.

Fixes from code review:
  - Corrected phonetic algorithm label (uses Soundex, not Double Metaphone)
  - Added confidence margin check to reduce false positives on short names
  - Added XML escaping for all SSML dynamic values
  - Removed misleading "Double Metaphone" references
"""

import logging
import re
import unicodedata
import xml.sax.saxutils as saxutils
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from rapidfuzz import process, fuzz

if TYPE_CHECKING:
    from graph_client import StaffMember

logger = logging.getLogger(__name__)

try:
    import jellyfish
    PHONETIC_AVAILABLE = True
except ImportError:
    PHONETIC_AVAILABLE = False
    logger.warning(
        "jellyfish not installed — phonetic matching disabled. Add it to requirements.txt.")

# Minimum score margin by which best match must beat second-best.
# Prevents false positives where multiple names score similarly.
CONFIDENCE_MARGIN = 10


@dataclass
class MatchResult:
    staff: Optional["StaffMember"] = None
    score: float = 0.0
    strategy: str = "none"
    matched_on: str = ""

    @property
    def found(self) -> bool:
        return self.staff is not None


class NameMatcher:
    """
    Matches a spoken name against a list of StaffMember objects.

    Usage:
        matcher = NameMatcher(threshold=65)
        result  = matcher.match("Hanson", staff_list)
        if result.found:
            ssml = build_ssml_transfer_message(result.staff, voice_name)
    """

    def __init__(self, threshold: int = 65):
        self.threshold = threshold

    def match(self, spoken: str, staff: list) -> MatchResult:
        if not spoken or not staff:
            return MatchResult()

        spoken_clean = _normalise(spoken)
        logger.info(
            "Matching '%s' (normalised='%s') against %d staff members",
            spoken, spoken_clean, len(staff)
        )

        # Strategy 1: Exact
        result = self._exact(spoken_clean, staff)
        if result.found:
            logger.info(
                "Exact match: '%s' → '%s'",
                spoken,
                result.staff.display_name)
            return result

        # Strategy 2: Phonetic (Soundex)
        if PHONETIC_AVAILABLE:
            result = self._phonetic(spoken_clean, staff)
            if result.found:
                logger.info(
                    "Phonetic match: '%s' → '%s' (score=%.1f)",
                    spoken, result.staff.display_name, result.score
                )
                return result

        # Strategy 3: Fuzzy
        result = self._fuzzy(spoken_clean, staff)
        if result.found:
            logger.info(
                "Fuzzy match: '%s' → '%s' (score=%.1f)",
                spoken, result.staff.display_name, result.score
            )
            return result

        logger.info(
            "No match found for '%s' above threshold %d",
            spoken,
            self.threshold)
        return MatchResult()

    def _exact(self, spoken: str, staff: list) -> MatchResult:
        for member in staff:
            for token in member.searchable_tokens:
                if _normalise(token) == spoken:
                    return MatchResult(
                        staff=member,
                        score=100.0,
                        strategy="exact",
                        matched_on=token)
        return MatchResult()

    def _phonetic(self, spoken: str, staff: list) -> MatchResult:
        """
        Soundex matching — good for simple phonetic variants
        (e.g. "Smith" / "Smyth", "Johnston" / "Johnson").
        Note: uses jellyfish.soundex(), NOT Double Metaphone.
        """
        try:
            spoken_sdx = jellyfish.soundex(spoken)
        except Exception:
            return MatchResult()

        matches = []
        for member in staff:
            for token in member.searchable_tokens:
                t = _normalise(token)
                if not t:
                    continue
                try:
                    if jellyfish.soundex(t) == spoken_sdx:
                        matches.append((member, token, 78.0))
                except Exception:
                    continue

        if not matches:
            return MatchResult()

        # If multiple members share the same Soundex, apply confidence margin
        if len(set(m[0].aad_id for m in matches)) > 1:
            logger.info(
                "Phonetic ambiguity — multiple members match Soundex of '%s'",
                spoken)
            return MatchResult()

        best_member, best_token, score = matches[0]
        if score >= self.threshold:
            return MatchResult(
                staff=best_member,
                score=score,
                strategy="phonetic",
                matched_on=best_token)
        return MatchResult()

    def _fuzzy(self, spoken: str, staff: list) -> MatchResult:
        """
        rapidfuzz token_sort_ratio — handles word-order variation.
        Applies confidence margin: best score must exceed second-best
        by CONFIDENCE_MARGIN to avoid false positives on short names.
        """
        candidates: list = []
        for member in staff:
            for token in member.searchable_tokens:
                candidates.append((_normalise(token), member))

        if not candidates:
            return MatchResult()

        names = [c[0] for c in candidates]
        results = process.extract(
            spoken,
            names,
            scorer=fuzz.token_sort_ratio,
            limit=3,
        )

        if not results:
            return MatchResult()

        best_name, best_score, best_idx = results[0]

        if best_score < self.threshold:
            return MatchResult()

        # Confidence margin check — reject if second-best is too close
        if len(results) > 1:
            second_score = results[1][1]
            if (best_score - second_score) < CONFIDENCE_MARGIN:
                logger.info(
                    "Fuzzy ambiguity: best=%.1f second=%.1f margin=%.1f (threshold=%d) — rejecting",
                    best_score,
                    second_score,
                    best_score - second_score,
                    CONFIDENCE_MARGIN)
                return MatchResult()

        _, best_member = candidates[best_idx]
        return MatchResult(
            staff=best_member,
            score=float(best_score),
            strategy="fuzzy",
            matched_on=best_name,
        )


# ── SSML builders with XML escaping ──────────────────────────

def _xml_escape(text: str) -> str:
    """Escape XML special characters to prevent SSML injection."""
    return saxutils.escape(str(text or ""))


def build_ssml_transfer_message(staff: "StaffMember", voice_name: str) -> str:
    """
    Builds SSML for "Connecting you to [name]".
    Uses pronunciation override (extensionAttribute1) if set.
    All dynamic values are XML-escaped.
    """
    voice_safe = _xml_escape(voice_name)
    name_ssml = _build_name_element(staff)
    return (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-AU">'
        f'<voice name="{voice_safe}">'
        f'Please hold. Connecting you to {name_ssml}.'
        f'</voice></speak>'
    )


def build_ssml_message(text: str, voice_name: str) -> str:
    """Builds SSML for any plain message. Escapes text content."""
    voice_safe = _xml_escape(voice_name)
    text_safe = _xml_escape(text)
    return (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-AU">'
        f'<voice name="{voice_safe}">{text_safe}</voice>'
        f'</speak>'
    )


def _build_name_element(staff: "StaffMember") -> str:
    """
    Returns the SSML fragment for speaking a name.
    If pronunciation_override is set:
      - IPA string  → <phoneme alphabet="ipa" ph="...">
      - Plain text  → <phoneme alphabet="x-microsoft-ups" ph="...">
    All values are XML-escaped.
    """
    display = _xml_escape(staff.display_name)
    override = (staff.pronunciation_override or "").strip()

    if not override:
        return display

    ph_safe = _xml_escape(override)

    if _is_ipa(override):
        return f'<phoneme alphabet="ipa" ph="{ph_safe}">{display}</phoneme>'
    return f'<phoneme alphabet="x-microsoft-ups" ph="{ph_safe}">{display}</phoneme>'


def _is_ipa(text: str) -> bool:
    """Rough check: IPA strings contain Unicode outside basic Latin."""
    return any(ord(c) > 127 for c in text)


# ── Utilities ─────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """Lowercase, strip combining accents, remove punctuation."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    lower = ascii_str.lower().strip()
    return re.sub(r"[^\w\s-]", "", lower).strip()
