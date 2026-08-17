"""Parses Microsoft Graph group change-notification payloads.

Per the plan's Section 2 decision: group-resource change notifications carry
the changed member's object id directly in `resourceData['members@delta']`
(confirmed against live add/remove test payloads), so this module reads that
straight off the notification — no `/groups/{id}/members/delta` call needed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from . import graph_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemberChange:
    group_id: str
    member_id: str
    removed: bool


def extract_member_changes(notification_body: dict, expected_client_state: str | None = None) -> list[MemberChange]:
    changes: list[MemberChange] = []
    for notification in notification_body.get("value", []):
        if expected_client_state is not None and notification.get("clientState") != expected_client_state:
            logger.warning(
                "Discarding notification with mismatched clientState (subscriptionId=%s)",
                notification.get("subscriptionId"),
            )
            continue

        resource_data = notification.get("resourceData", {})
        group_id = resource_data.get("id")
        for member in resource_data.get("members@delta", []):
            changes.append(
                MemberChange(
                    group_id=group_id,
                    member_id=member["id"],
                    removed="@removed" in member,
                )
            )
    return changes


@dataclass(frozen=True)
class UserIdentity:
    upn: str
    given_name: str | None
    surname: str | None


def resolve_user_identity(user_id_or_upn: str) -> UserIdentity | None:
    """Resolve a member object id (or a UPN directly — Graph's /users/{key}
    accepts either) to the fields provisioning needs: the UPN, plus
    givenName/surname used to build the Windows computer name (naming.py).
    Returns None if it isn't a user (e.g. a nested group or service principal
    was added to the group, or an ad-hoc UPN doesn't exist in the tenant)."""
    resp = graph_client.get(
        f"/users/{user_id_or_upn}", params={"$select": "userPrincipalName,givenName,surname"}
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    return UserIdentity(upn=data["userPrincipalName"], given_name=data.get("givenName"), surname=data.get("surname"))
