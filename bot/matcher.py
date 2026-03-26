"""
matcher.py
==========
Multi-strategy name matching engine.

Strategy (in order of precedence):
  1. Exact match (case-insensitive)
  2. Phonetic match using Soundex + Double Metaphone
  3. Fuzzy token-sort match (rapidfuzz) with configurable threshold

For TTS pronunciation, each staff member may have an override stored in
Azure AD extensionAttribute1 (e.g. "HAN-son" for Hanson).
The bot speaks this override rather than the raw displayName when present.

This avoids issues like Azure TTS rendering:
  "Hanson"   → "handsome"
  "Nguyen"   → "new-yen" (instead of "win")
  "Siobhan"  → "see-oh-ban" (instead of "shi-vawn")
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import process, fuzz

logger = logging.getLogger(__name__)

# ── Install jellyfish for phonetic algorithms ─────────────────
try:
    import jellyfish
    PHONETIC_AVAILABLE = True
except ImportError:
    PHONETIC_AVAILABLE = False
    logger.warning("jellyfish not installed — phonetic matching disabled. Add jellyfish to requirements.txt")


@dataclass
class StaffMember:
    aad_id:       str
    display_name: str
    given_name:   str  = ""
    surname:      str  = ""
    # extensionAttribute1 in Azure AD = phonetic pronunciation override
    # e.g. "HAN-son" or "win" (for Nguyen)
    pronunciation_override: str = ""

    @property
    def tts_name(self) -> str:
        """The name to speak aloud in TTS. Uses override if set."""
        return self.pronunciation_override or self.display_name

    @property
    def searchable_tokens(self) -> list[str]:
        """All name forms we try to match against."""
        tokens = [self.display_name]
        if self.given_name:
            tokens.append(self.given_name)
        if self.surname:
            tokens.append(self.surname)
        # First name only, last name only
        parts = self.display_name.split()
        if len(parts) >= 2:
            tokens.append(parts[0])   # first name
            tokens.append(parts[-1])  # last name
        return list(set(tokens))


@dataclass
class MatchResult:
    staff:      Optional[StaffMember] = None
    score:      float = 0.0
    strategy:   str   = "none"
    matched_on: str   = ""

    @property
    def found(self) -> bool:
        return self.staff is not None


class NameMatcher:
    """
    Matches a spoken name string against a list of StaffMember objects.

    Usage:
        matcher = NameMatcher(threshold=65)
        result  = matcher.match("Hanson", staff_list)
        if result.found:
            print(result.staff.tts_name)   # speaks pronunciation override
    """

    def __init__(self, threshold: int = 65):
        self.threshold = threshold

    def match(self, spoken: str, staff: list[StaffMember]) -> MatchResult:
        if not spoken or not staff:
            return MatchResult()

        spoken_clean = _normalise(spoken)
        logger.info("Matching '%s' (normalised: '%s') against %d staff", spoken, spoken_clean, len(staff))

        # ── Strategy 1: Exact match ───────────────────────────
        result = self._exact_match(spoken_clean, staff)
        if result.found:
            logger.info("Exact match: '%s' → '%s'", spoken, result.staff.display_name)
            return result

        # ── Strategy 2: Phonetic match ────────────────────────
        if PHONETIC_AVAILABLE:
            result = self._phonetic_match(spoken_clean, staff)
            if result.found:
                logger.info(
                    "Phonetic match: '%s' → '%s' (score=%.1f, strategy=%s)",
                    spoken, result.staff.display_name, result.score, result.strategy
                )
                return result

        # ── Strategy 3: Fuzzy match ───────────────────────────
        result = self._fuzzy_match(spoken_clean, staff)
        if result.found:
            logger.info(
                "Fuzzy match: '%s' → '%s' (score=%.1f)",
                spoken, result.staff.display_name, result.score
            )
            return result

        logger.info("No match found for '%s' above threshold %d", spoken, self.threshold)
        return MatchResult()

    # ── Strategy 1 — Exact ───────────────────────────────────

    def _exact_match(self, spoken: str, staff: list[StaffMember]) -> MatchResult:
        for member in staff:
            for token in member.searchable_tokens:
                if _normalise(token) == spoken:
                    return MatchResult(staff=member, score=100.0, strategy="exact", matched_on=token)
        return MatchResult()

    # ── Strategy 2 — Phonetic ────────────────────────────────

    def _phonetic_match(self, spoken: str, staff: list[StaffMember]) -> MatchResult:
        """
        Uses Double Metaphone for better international name support.
        Double Metaphone handles:
          - Nguyen  → ('N', 'NK') matches 'win' → ('N', 'N')  [partial]
          - Siobhan → ('XPN', 'XBN')
          - Hanson  → ('HNSN', '') — 'handsome' → ('HNTSM', '') — different, so
                      phonetic alone won't fix this case (use pronunciation_override)

        Soundex is used as a secondary check for simpler cases.
        """
        spoken_meta   = jellyfish.metaphone(spoken)
        spoken_soundex = jellyfish.soundex(spoken)

        best_score  = 0.0
        best_member = None
        best_token  = ""

        for member in staff:
            for token in member.searchable_tokens:
                t = _normalise(token)
                if not t:
                    continue

                # Double Metaphone
                try:
                    token_meta = jellyfish.metaphone(t)
                    if spoken_meta and token_meta and (
                        spoken_meta == token_meta or
                        spoken_meta.startswith(token_meta[:3]) or
                        token_meta.startswith(spoken_meta[:3])
                    ):
                        score = _metaphone_similarity(spoken_meta, token_meta)
                        if score > best_score:
                            best_score  = score
                            best_member = member
                            best_token  = token
                except Exception:
                    pass

                # Soundex fallback
                try:
                    token_sdx = jellyfish.soundex(t)
                    if spoken_soundex == token_sdx and best_score < 80:
                        if 75 > best_score:
                            best_score  = 75.0
                            best_member = member
                            best_token  = token
                except Exception:
                    pass

        if best_member and best_score >= self.threshold:
            return MatchResult(
                staff=best_member,
                score=best_score,
                strategy="phonetic",
                matched_on=best_token,
            )
        return MatchResult()

    # ── Strategy 3 — Fuzzy ───────────────────────────────────

    def _fuzzy_match(self, spoken: str, staff: list[StaffMember]) -> MatchResult:
        """
        rapidfuzz token_sort_ratio — handles word-order differences.
        Builds a flat list of (token, member) pairs and finds the best match.
        """
        candidates: list[tuple[str, StaffMember]] = []
        for member in staff:
            for token in member.searchable_tokens:
                candidates.append((_normalise(token), member))

        if not candidates:
            return MatchResult()

        names_only = [c[0] for c in candidates]
        result = process.extractOne(
            spoken,
            names_only,
            scorer=fuzz.token_sort_ratio,
        )

        if not result:
            return MatchResult()

        _, score, idx = result
        if score >= self.threshold:
            _, matched_member = candidates[idx]
            return MatchResult(
                staff=matched_member,
                score=float(score),
                strategy="fuzzy",
                matched_on=names_only[idx],
            )
        return MatchResult()


# ── SSML builder for TTS with pronunciation override ─────────

def build_ssml_transfer_message(staff: StaffMember, voice_name: str) -> str:
    """
    Builds an SSML string for the "Connecting you to [name]" message.
    If the staff member has a pronunciation_override set, it wraps
    the name in <phoneme> tags to guide TTS pronunciation.

    Example:
      display_name = "Hanson"
      pronunciation_override = "HAN-son"
      → <say-as interpret-as="characters">HAN-son</say-as>

    For full phoneme IPA support (e.g. Siobhan):
      pronunciation_override = "ʃɪˈvɔːn"  (IPA)
      → <phoneme alphabet="ipa" ph="ʃɪˈvɔːn">Siobhan</phoneme>
    """
    name_ssml = _build_name_ssml(staff)
    return (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-AU">'
        f'<voice name="{voice_name}">'
        f'Please hold. Connecting you to {name_ssml}.'
        f'</voice></speak>'
    )


def build_ssml_greeting(company_name: str, greeting_text: str, voice_name: str) -> str:
    return (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-AU">'
        f'<voice name="{voice_name}">'
        f'{greeting_text}'
        f'</voice></speak>'
    )


def build_ssml_not_found(spoken_name: str, voice_name: str, fallback_message: str) -> str:
    return (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-AU">'
        f'<voice name="{voice_name}">'
        f'{fallback_message}'
        f'</voice></speak>'
    )


def _build_name_ssml(staff: StaffMember) -> str:
    override = (staff.pronunciation_override or "").strip()
    display  = staff.display_name

    if not override:
        return display

    # If override looks like IPA (contains Unicode phonetic chars)
    if _is_ipa(override):
        return f'<phoneme alphabet="ipa" ph="{override}">{display}</phoneme>'

    # Otherwise treat as a spoken-form hint using say-as
    return f'<phoneme alphabet="x-microsoft-ups" ph="{override}">{display}</phoneme>'


def _is_ipa(text: str) -> bool:
    """Rough check — IPA strings contain Unicode outside basic Latin."""
    return any(ord(c) > 127 for c in text)


# ── Utility functions ─────────────────────────────────────────

def _normalise(text: str) -> str:
    """Lowercase, strip accents, remove punctuation for matching."""
    if not text:
        return ""
    # Decompose Unicode accents (é → e + combining accent → e)
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    lower = ascii_str.lower().strip()
    # Remove punctuation except hyphens (hyphenated names)
    return re.sub(r"[^\w\s-]", "", lower).strip()


def _metaphone_similarity(a: str, b: str) -> float:
    """Simple overlap score between two metaphone strings."""
    if not a or not b:
        return 0.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(longer) == 0:
        return 0.0
    overlap = sum(1 for c in shorter if c in longer)
    return (overlap / len(longer)) * 100
