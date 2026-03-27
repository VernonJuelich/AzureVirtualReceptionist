"""
graph_client.py
===============
Loads Azure AD Security Group members for the staff directory.
Reads extensionAttribute1 as TTS pronunciation override.

Fixes from code review:
  - Added pagination for groups with >999 members
  - Added object type filtering (users only — groups can contain other types)
  - Stale cache fallback is preserved but "unavailable" is now distinguishable
  - Cache TTL: 5 minutes
"""

import logging
import time
from dataclasses import dataclass

from azure.identity import ClientSecretCredential
from msgraph import GraphServiceClient

logger = logging.getLogger(__name__)

_cache_members: list = []
_cache_timestamp: float = 0.0
_cache_available: bool = True   # False when last fetch failed
CACHE_TTL = 300  # 5 minutes


@dataclass
class StaffMember:
    aad_id: str
    display_name: str
    given_name: str = ""
    surname: str = ""
    pronunciation_override: str = ""  # from extensionAttribute1

    @property
    def tts_name(self) -> str:
        """Name spoken aloud — uses pronunciation override if set."""
        return self.pronunciation_override or self.display_name

    @property
    def searchable_tokens(self) -> list:
        """All name forms used for matching."""
        tokens = set()
        tokens.add(self.display_name)
        if self.given_name:
            tokens.add(self.given_name)
        if self.surname:
            tokens.add(self.surname)
        parts = self.display_name.split()
        if len(parts) >= 2:
            tokens.add(parts[0])    # first name only
            tokens.add(parts[-1])   # last name only
        return list(tokens)


class DirectoryUnavailableError(Exception):
    """Raised when the staff directory cannot be loaded and no cache exists."""
    pass


async def get_staff_members(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    group_id: str,
) -> list:
    """
    Returns list of StaffMember from the Azure AD Security Group.
    Raises DirectoryUnavailableError if fetch fails and no cache exists.

    extensionAttribute1 on each user = TTS pronunciation override.
    Set in Azure AD: Users > select user > Edit > extensionAttribute1
    Examples:
      Hanson   → "HAN-son"
      Nguyen   → "win"
      Siobhan  → "ʃɪˈvɔːn"   (IPA)
    """
    global _cache_members, _cache_timestamp, _cache_available

    now = time.time()
    if _cache_members and (now - _cache_timestamp) < CACHE_TTL:
        logger.info(
            "Returning cached staff directory (%d members)",
            len(_cache_members))
        return _cache_members

    logger.info(
        "Fetching group members from Graph API (group=%s)...",
        group_id)

    try:
        credential = ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
        graph = GraphServiceClient(credential)
        members = []
        page = await graph.groups.by_group_id(group_id).members.get(
            request_configuration={
                "query_parameters": {
                    "$select": "id,displayName,givenName,surname,onPremisesExtensionAttributes,@odata.type",
                    "$top": 999,
                }
            }
        )

        while page:
            if page.value:
                for obj in page.value:
                    # Filter: only include user objects (groups can contain
                    # devices, groups, etc.)
                    odata_type = getattr(obj, "odata_type", "") or ""
                    if odata_type and "#microsoft.graph.user" not in odata_type.lower():
                        logger.debug(
                            "Skipping non-user member (type=%s)", odata_type)
                        continue

                    aad_id = getattr(obj, "id", "") or ""
                    display_name = getattr(obj, "display_name", "") or ""

                    if not aad_id or not display_name:
                        logger.debug(
                            "Skipping member with missing id or displayName")
                        continue

                    pronunciation = ""
                    ext = getattr(
                        obj, "on_premises_extension_attributes", None)
                    if ext:
                        pronunciation = getattr(
                            ext, "extension_attribute1", "") or ""

                    members.append(StaffMember(
                        aad_id=aad_id,
                        display_name=display_name,
                        given_name=getattr(obj, "given_name", "") or "",
                        surname=getattr(obj, "surname", "") or "",
                        pronunciation_override=pronunciation.strip(),
                    ))

            # Handle pagination — Graph returns nextLink for large groups
            next_link = getattr(page, "odata_next_link", None)
            if next_link:
                logger.info("Graph paging — fetching next page...")
                page = await graph.groups.by_group_id(group_id).members.with_url(next_link).get()
            else:
                break

        logger.info(
            "Staff directory loaded: %d members (%d with pronunciation overrides)",
            len(members),
            sum(1 for m in members if m.pronunciation_override)
        )
        _cache_members = members
        _cache_timestamp = now
        _cache_available = True
        return members

    except Exception as exc:
        logger.error("Graph API error loading staff directory: %s", exc)
        _cache_available = False
        if _cache_members:
            logger.warning(
                "Graph fetch failed — returning stale cache (%d members)",
                len(_cache_members))
            return _cache_members
        raise DirectoryUnavailableError(
            f"Staff directory unavailable and no cache exists: {exc}"
        ) from exc
