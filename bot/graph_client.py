"""
graph_client.py
===============
Microsoft Graph API client.
Loads Azure AD Security Group members for the staff directory.
Reads extensionAttribute1 as the TTS pronunciation override.
Caches results for 5 minutes.
"""

import logging
import time
from azure.identity import ClientSecretCredential
from msgraph import GraphServiceClient as MSGraphClient

from matcher import StaffMember

logger = logging.getLogger(__name__)

_cache_members:   list[StaffMember] = []
_cache_timestamp: float = 0.0
CACHE_TTL = 300  # 5 minutes


async def get_staff_members(
    tenant_id:     str,
    client_id:     str,
    client_secret: str,
    group_id:      str,
) -> list[StaffMember]:
    """
    Returns list of StaffMember objects from the Azure AD Security Group.
    Cached for CACHE_TTL seconds to reduce Graph API calls.

    extensionAttribute1 on each user = TTS pronunciation override.
    Set this in Azure AD: Users > select user > Edit > extensionAttribute1
    Examples:
      Hanson   → "HAN-son"
      Nguyen   → "win"
      Siobhan  → "ʃɪˈvɔːn"  (IPA)
      Zbigniew → "zbig-nyev"
    """
    global _cache_members, _cache_timestamp

    now = time.time()
    if _cache_members and (now - _cache_timestamp) < CACHE_TTL:
        logger.info("Returning cached staff list (%d members)", len(_cache_members))
        return _cache_members

    logger.info("Fetching group members from Graph API (group=%s)...", group_id)

    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )
    graph = MSGraphClient(credential)

    try:
        result = await graph.groups.by_group_id(group_id).members.get(
            request_configuration={
                "query_parameters": {
                    # extensionAttribute1 holds the pronunciation override
                    "$select": "id,displayName,givenName,surname,onPremisesExtensionAttributes",
                    "$top": 999,
                }
            }
        )

        members: list[StaffMember] = []
        if result and result.value:
            for user in result.value:
                # Pull pronunciation override from extensionAttribute1
                pronunciation = ""
                ext_attrs = getattr(user, "on_premises_extension_attributes", None)
                if ext_attrs:
                    pronunciation = getattr(ext_attrs, "extension_attribute1", "") or ""

                members.append(StaffMember(
                    aad_id=user.id or "",
                    display_name=user.display_name or "",
                    given_name=user.given_name or "",
                    surname=user.surname or "",
                    pronunciation_override=pronunciation.strip(),
                ))

        logger.info(
            "Graph returned %d staff members (%d with pronunciation overrides)",
            len(members),
            sum(1 for m in members if m.pronunciation_override)
        )

        _cache_members   = members
        _cache_timestamp = now
        return members

    except Exception as exc:
        logger.error("Graph API error fetching group members: %s", exc)
        # Return stale cache if available rather than failing the call
        if _cache_members:
            logger.warning("Returning stale cache due to Graph error")
            return _cache_members
        return []
