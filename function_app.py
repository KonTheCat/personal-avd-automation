"""Azure Functions triggers (Sections 4-6 of the plan). Thin adapters only —
all real logic lives in the plain `avdprovisioning` package (Section 9), the
same package `local_cli.py` calls directly for ad-hoc testing.
"""
from __future__ import annotations

import json
import logging

import azure.functions as func

from avdprovisioning.config import load_config
from avdprovisioning.graph_notifications import extract_member_changes, resolve_user_upn
from avdprovisioning.provisioning import provision_session_host
from avdprovisioning.state import StateStore
from avdprovisioning.subscriptions import renew_or_recreate_subscription

logger = logging.getLogger(__name__)

app = func.FunctionApp()

QUEUE_NAME = "group-membership-changes"
QUEUE_CONNECTION = "AzureWebJobsStorage"


def _state_store() -> StateStore:
    config = load_config()
    return StateStore(config.state_storage_account_url, config.state_container_name)


@app.function_name(name="notification_listener")
@app.route(route="notifications", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@app.queue_output(arg_name="queue_messages", queue_name=QUEUE_NAME, connection=QUEUE_CONNECTION)
def notification_listener(req: func.HttpRequest, queue_messages: func.Out[str]) -> func.HttpResponse:
    # Graph's subscription validation handshake: a POST with `validationToken`
    # on the query string that must be echoed back as text/plain, HTTP 200,
    # within 10 seconds. Only happens at subscription create/recreate time.
    validation_token = req.params.get("validationToken")
    if validation_token is not None:
        logger.info("Echoing Graph validation token")
        return func.HttpResponse(validation_token, status_code=200, mimetype="text/plain")

    try:
        body = req.get_json()
    except ValueError:
        logger.warning("Notification request had no parseable JSON body")
        return func.HttpResponse(status_code=202)

    subscription_record = _state_store().get_subscription()
    expected_client_state = subscription_record.data.get("clientState") if subscription_record else None

    changes = extract_member_changes(body, expected_client_state=expected_client_state)

    messages = []
    for change in changes:
        if change.removed:
            logger.info("Member %s removed from group %s (out of scope, skipping)", change.member_id, change.group_id)
            continue
        messages.append(json.dumps({"group_id": change.group_id, "member_id": change.member_id}))

    if messages:
        queue_messages.set(messages)
        logger.info("Queued %d member-added change(s)", len(messages))

    return func.HttpResponse(status_code=202)


@app.function_name(name="group_change_processor")
@app.queue_trigger(arg_name="msg", queue_name=QUEUE_NAME, connection=QUEUE_CONNECTION)
def group_change_processor(msg: func.QueueMessage) -> None:
    payload = json.loads(msg.get_body().decode("utf-8"))
    member_id = payload["member_id"]

    upn = resolve_user_upn(member_id)
    if upn is None:
        logger.info("Member %s is not a user object, skipping", member_id)
        return

    config = load_config()
    result = provision_session_host(upn=upn, config=config, user_key=member_id)
    logger.info("provision_session_host(%s) -> %s", upn, result)


@app.function_name(name="subscription_renewer")
@app.schedule(schedule="0 0 3 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def subscription_renewer(timer: func.TimerRequest) -> None:
    config = load_config()
    result = renew_or_recreate_subscription(config, _state_store())
    logger.info("subscription_renewer -> %s", result)
