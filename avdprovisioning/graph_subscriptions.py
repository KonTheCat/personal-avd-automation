"""Microsoft Graph change-notification subscriptions for the group-change trigger.

Concept-proving phase: the `notificationUrl` passed in here points at a Logic
App's HTTP-request trigger instead of the eventual `notification_listener`
Azure Function (Section 4 of the plan). Creating the subscription is itself
the first real test — Graph performs a synchronous validation handshake
against `notificationUrl` (a POST with a `validationToken` query param that
must be echoed back as `text/plain`, HTTP 200, within 10 seconds) before it
will return success, so a failed `create_subscription` call usually means
the receiving endpoint isn't echoing that token back correctly, not that
anything here is wrong.

No Table Storage yet (Section 3 of the plan isn't built) — `clientState` is
generated fresh per call and only ever printed, not persisted. That's fine
for manually proving out the pipeline; real deployments must persist it to
validate incoming notifications.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import graph_client

logger = logging.getLogger(__name__)

# Ceiling for `group` resource change notifications is 41,760 minutes (~29
# days). Default to 20 days, same margin used for the subscription_renewer
# in the plan, so a first manual test subscription behaves like production.
DEFAULT_SUBSCRIPTION_MINUTES = 20 * 24 * 60


def resolve_group_id(display_name: str) -> str:
    resp = graph_client.get(
        "/groups",
        params={"$filter": f"displayName eq '{display_name}'", "$select": "id,displayName"},
    )
    resp.raise_for_status()
    groups = resp.json().get("value", [])
    if not groups:
        raise RuntimeError(f"No Entra ID group found with displayName '{display_name}'")
    if len(groups) > 1:
        raise RuntimeError(
            f"Multiple groups found with displayName '{display_name}'; disambiguate by object id instead"
        )
    return groups[0]["id"]


def generate_client_state() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Subscription:
    subscription_id: str
    resource: str
    notification_url: str
    expiration_datetime: str
    client_state: str


def list_subscriptions() -> list[dict]:
    resp = graph_client.get("/subscriptions")
    resp.raise_for_status()
    return resp.json().get("value", [])


def find_existing_subscription(resource: str, notification_url: str) -> dict | None:
    for sub in list_subscriptions():
        if sub.get("resource") == resource and sub.get("notificationUrl") == notification_url:
            return sub
    return None


def create_group_change_subscription(
    group_id: str,
    notification_url: str,
    client_state: str | None = None,
    minutes_from_now: int = DEFAULT_SUBSCRIPTION_MINUTES,
) -> Subscription:
    client_state = client_state or generate_client_state()
    expiration = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    body = {
        "changeType": "updated",
        "resource": f"/groups/{group_id}",
        "notificationUrl": notification_url,
        "expirationDateTime": expiration.strftime("%Y-%m-%dT%H:%M:%S.0Z"),
        "clientState": client_state,
    }
    logger.info("Creating Graph subscription: resource=%s notificationUrl=%s", body["resource"], notification_url)
    resp = graph_client.post("/subscriptions", body)
    if not resp.ok:
        raise RuntimeError(
            f"Graph subscription creation failed ({resp.status_code}): {resp.text}\n"
            "If this says the validation request failed/timed out, the receiving "
            "endpoint (the Logic App) isn't echoing the `validationToken` query "
            "param back as `text/plain` with HTTP 200 within 10 seconds — fix that "
            "in the workflow before retrying."
        )
    data = resp.json()
    return Subscription(
        subscription_id=data["id"],
        resource=data["resource"],
        notification_url=data["notificationUrl"],
        expiration_datetime=data["expirationDateTime"],
        client_state=client_state,
    )


def renew_subscription(subscription_id: str, minutes_from_now: int = DEFAULT_SUBSCRIPTION_MINUTES) -> str:
    """PATCH a subscription's expirationDateTime forward. Returns the new expirationDateTime.

    Raises RuntimeError on failure (e.g. 404 if the subscription no longer
    exists) — callers should treat that as "recreate the subscription".
    """
    expiration = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    body = {"expirationDateTime": expiration.strftime("%Y-%m-%dT%H:%M:%S.0Z")}
    resp = graph_client.patch(f"/subscriptions/{subscription_id}", body)
    if not resp.ok:
        raise RuntimeError(f"Graph subscription renewal failed ({resp.status_code}): {resp.text}")
    return resp.json()["expirationDateTime"]


def delete_subscription(subscription_id: str) -> None:
    resp = graph_client.delete(f"/subscriptions/{subscription_id}")
    if resp.status_code not in (204, 404):
        raise RuntimeError(f"Graph subscription deletion failed ({resp.status_code}): {resp.text}")
