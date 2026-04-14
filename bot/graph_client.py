"""
graph_client.py
===============
Loads Microsoft Entra group members for the staff directory.

This version avoids msgraph-sdk request-shape drift by using a simple HTTPS
client with an app-only token from ClientSecretCredential.
"""

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from azure.identity import ClientSecretCredential

logger = logging.getLogger(__name__)

_cache_members: list = []
_cache_timestamp: float = 0.0
CACHE_TTL = 300
GRAPH_SCOPE = "https://graph.microsoft.com/.default"


@dataclass
class StaffMember:
    aad_id: str
    display_name: str
    given_name: str = ""
    surname: str = ""
    pronunciation_override: str = ""

    @property
    def tts_name(self) -> str:
        return self.pronunciation_override or self.display_name

    @property
    def searchable_tokens(self) -> list:
        tokens = {self.display_name}
        if self.given_name:
            tokens.add(self.given_name)
        if self.surname:
            tokens.add(self.surname)
        parts = self.display_name.split()
        if len(parts) >= 2:
            tokens.add(parts[0])
            tokens.add(parts[-1])
        return list(tokens)


class DirectoryUnavailableError(Exception):
    pass


async def get_staff_members(tenant_id: str, client_id: str, client_secret: str, group_id: str) -> list:
    global _cache_members, _cache_timestamp

    now = time.time()
    if _cache_members and (now - _cache_timestamp) < CACHE_TTL:
        logger.info("Returning cached staff directory (%d members)", len(_cache_members))
        return _cache_members

    try:
        members = await asyncio.to_thread(
            _get_staff_members_sync,
            tenant_id,
            client_id,
            client_secret,
            group_id,
        )
        _cache_members = members
        _cache_timestamp = now
        return members
    except Exception as exc:
        logger.error("Graph API error loading staff directory: %s", exc)
        if _cache_members:
            logger.warning("Graph fetch failed — returning stale cache (%d members)", len(_cache_members))
            return _cache_members
        raise DirectoryUnavailableError(f"Staff directory unavailable and no cache exists: {exc}") from exc


def _get_staff_members_sync(tenant_id: str, client_id: str, client_secret: str, group_id: str) -> list:
    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )
    token = credential.get_token(GRAPH_SCOPE).token

    params = urllib.parse.urlencode(
        {
            "$select": "id,displayName,givenName,surname,onPremisesExtensionAttributes",
            "$top": "999",
        }
    )
    url = f"https://graph.microsoft.com/v1.0/groups/{group_id}/members/microsoft.graph.user?{params}"

    members = []
    while url:
        payload = _graph_get(url, token)
        for obj in payload.get("value", []):
            aad_id = obj.get("id", "")
            display_name = obj.get("displayName", "")
            if not aad_id or not display_name:
                continue

            ext = obj.get("onPremisesExtensionAttributes") or {}
            pronunciation = (ext.get("extensionAttribute1") or "").strip()

            members.append(
                StaffMember(
                    aad_id=aad_id,
                    display_name=display_name,
                    given_name=obj.get("givenName", "") or "",
                    surname=obj.get("surname", "") or "",
                    pronunciation_override=pronunciation,
                )
            )

        url = payload.get("@odata.nextLink")

    logger.info(
        "Staff directory loaded: %d members (%d with pronunciation overrides)",
        len(members),
        sum(1 for m in members if m.pronunciation_override),
    )
    return members


def _graph_get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))
