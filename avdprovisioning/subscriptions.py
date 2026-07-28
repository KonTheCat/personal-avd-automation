"""Keep the Graph change-notification subscription alive (Section 6 of the plan).

Called on a daily timer by the `subscription_renewer` Function. No delta
cursor to worry about (Section 2 decision): if the subscription had to be
recreated, membership changes just start flowing again from whatever
happens after recreation — there's no baseline/replay step.
"""
from __future__ import annotations

import logging

from . import graph_subscriptions
from .config import Config
from .state import StateStore

logger = logging.getLogger(__name__)


def renew_or_recreate_subscription(config: Config, state: StateStore) -> dict:
    record = state.get_subscription()
    if record is None:
        raise RuntimeError(
            "No subscription state persisted yet — run `subscribe-group-changes` once before "
            "relying on the renewer."
        )

    subscription_id = record.data["subscriptionId"]
    try:
        new_expiration = graph_subscriptions.renew_subscription(subscription_id)
        logger.info("Renewed subscription %s, new expiration %s", subscription_id, new_expiration)
        data = dict(record.data)
        data["expirationDateTime"] = new_expiration
        state.save_subscription(data, etag=record.etag)
        return {"status": "renewed", "subscriptionId": subscription_id, "expirationDateTime": new_expiration}
    except RuntimeError:
        logger.warning("Renewal of subscription %s failed, recreating", subscription_id, exc_info=True)

    if not config.users_group_name:
        raise RuntimeError("AVD_USERS_GROUP is not set — cannot recreate a lost subscription")
    if not config.notification_listener_url:
        raise RuntimeError("AVD_NOTIFICATION_LISTENER_URL is not set — cannot recreate a lost subscription")

    group_id = graph_subscriptions.resolve_group_id(config.users_group_name)
    subscription = graph_subscriptions.create_group_change_subscription(
        group_id=group_id, notification_url=config.notification_listener_url
    )
    logger.info("Recreated subscription %s", subscription.subscription_id)
    state.save_subscription(
        {
            "subscriptionId": subscription.subscription_id,
            "resource": subscription.resource,
            "notificationUrl": subscription.notification_url,
            "expirationDateTime": subscription.expiration_datetime,
            "clientState": subscription.client_state,
        }
    )
    return {"status": "recreated", "subscriptionId": subscription.subscription_id}
