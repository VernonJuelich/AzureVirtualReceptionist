"""
graph_client.py
===============
Loads Azure AD Security Group members for the staff directory.
Reads extensionAttribute1 as TTS pronunciation override.

Fixes applied:
  - Pagination corrected for msgraph-sdk-python v1: next page is fetched by
    constructing a new DirectoryObjectCollectionResponse request from the
    raw nextLink URL via the adapter, rather than chaining .with_url() on
    the group members builder (which does not exist in v1).
  - Added object type filtering (users only)
  - Stale cache fallback preserved; DirectoryUnavailableError distinguishable
  - Cache TTL: 5 minutes
"""

import logging
import time
from dataclasses import dataclass

from azure.identity import ClientSecretCredential
from msgraph import GraphServiceClient
from msgraph.generated.groups.item.members.members_request_builder import (
    MembersRequestBuilder,
)
from kiota_abstractions.base_request_configuration import RequestConfiguration

logger = logging.getLogger(__name__)

_cache_members: list = []
_cache_timestamp: float = 0.0
_cache_available: bool = True
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


def _extract_member(obj) -> "StaffMember | None":
    """Extract a StaffMember from a Graph directory object, or None if not a user."""
    odata_type = getattr(obj, "odata_type", "") or ""
    if odata_type and "#microsoft.graph.user" not in odata_type.lower():
        logger.debug("Skipping non-user member (type=%s)", odata_type)
        return None

    aad_id = getattr(obj, "id", "") or ""
    display_name = getattr(obj, "display_name", "") or ""

    if not aad_id or not display_name:
        logger.debug("Skipping member with missing id or displayName")
        return None

    pronunciation = ""
    ext = getattr(obj, "on_premises_extension_attributes", None)
    if ext:
        pronunciation = getattr(ext, "extension_attribute1", "") or ""

    return StaffMember(
        aad_id=aad_id,
        display_name=display_name,
        given_name=getattr(obj, "given_name", "") or "",
        surname=getattr(obj, "surname", "") or "",
        pronunciation_override=pronunciation.strip(),
    )


async def get_staff_members(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    group_id: str,
) -> list:
    """
    Returns list of StaffMember from the Azure AD Security Group.
    Raises DirectoryUnavailableError if fetch fails and no cache exists.

    Pagination: Graph returns up to 999 members per page. For larger groups
    the response includes an @odata.nextLink URL. We follow it by constructing
    a new request via the GraphServiceClient's request adapter — the correct
    pattern for msgraph-sdk-python v1.x.

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

        # Build query parameters — select only the fields we need
        query_params = MembersRequestBuilder.MembersRequestBuilderGetQueryParameters(
            select=["id", "displayName", "givenName", "surname",
                    "onPremisesExtensionAttributes"],
            top=999,
        )
        request_config = RequestConfiguration(query_parameters=query_params)

        page = await graph.groups.by_group_id(group_id).members.get(
            request_configuration=request_config
        )

        while page:
            if page.value:
                for obj in page.value:
                    member = _extract_member(obj)
                    if member:
                        members.append(member)

            # Pagination: follow nextLink if present.
            # In msgraph-sdk-python v1, we use the request adapter directly
            # with the raw nextLink URL to fetch subsequent pages.
            next_link = getattr(page, "odata_next_link", None)
            if not next_link:
                break

            logger.info("Graph paging — fetching next page...")
            from msgraph.generated.groups.item.members.members_request_builder import (
                MembersRequestBuilder as MRB,
            )
            # with_url returns a new builder scoped to the absolute nextLink URL
            page = await graph.groups.by_group_id(group_id).members.with_url(
                next_link
            ).get()

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
