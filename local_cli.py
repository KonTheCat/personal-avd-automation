"""Ad-hoc CLI for exercising avdprovisioning directly, no Function App needed.

Usage:
    python local_cli.py provision --upn jane@contoso.com
    python local_cli.py provision --upn jane@contoso.com --vm-size Standard_D8s_v5
    python local_cli.py provision --upn jane@contoso.com --dry-run

    python local_cli.py subscribe-group-changes
    python local_cli.py list-subscriptions
    python local_cli.py unsubscribe --id <subscription-id>

    python local_cli.py handle-notification --file sample-added-user-event-request-body.txt
    python local_cli.py handle-notification --file sample-added-user-event-request-body.txt --execute

    python local_cli.py show-subscription-state
    python local_cli.py show-hostmap-state --user <object-id>
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from avdprovisioning.config import Config, load_config
from avdprovisioning.graph_notifications import extract_member_changes, resolve_user_identity
from avdprovisioning.graph_subscriptions import (
    create_group_change_subscription,
    delete_subscription,
    find_existing_subscription,
    list_subscriptions,
    resolve_group_id,
)
from avdprovisioning.provisioning import deallocate_session_host, provision_session_host
from avdprovisioning.state import StateStore


def _state_store(config: Config) -> StateStore:
    if not config.state_storage_account_url:
        print("AVD_STATE_STORAGE_ACCOUNT_URL is not set in .env", file=sys.stderr)
        raise SystemExit(1)
    return StateStore(config.state_storage_account_url, config.state_container_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    provision_parser = subparsers.add_parser("provision", help="Provision and assign one session host")
    provision_parser.add_argument("--upn", required=True, help="User principal name to provision a desktop for")
    provision_parser.add_argument("--vm-size", default=None, help="Override AVD_VM_SIZE_DEFAULT for this run")
    provision_parser.add_argument("--dry-run", action="store_true", help="Log intended actions without calling Azure")
    provision_parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    subscribe_parser = subparsers.add_parser(
        "subscribe-group-changes",
        help="Create a Graph change-notification subscription on AVD_USERS_GROUP, pointed at the notification URL",
    )
    subscribe_parser.add_argument(
        "--notification-url", default=None, help="Override SAMPLE_HTTP_REQUEST_TARGET_FOR_GROUP_CHANGES for this run"
    )
    subscribe_parser.add_argument(
        "--force", action="store_true", help="Create a new subscription even if one already matches"
    )
    subscribe_parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    list_sub_parser = subparsers.add_parser("list-subscriptions", help="List all active Graph subscriptions")
    list_sub_parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    unsubscribe_parser = subparsers.add_parser("unsubscribe", help="Delete a Graph subscription by id")
    unsubscribe_parser.add_argument("--id", required=True, dest="subscription_id", help="Subscription id to delete")
    unsubscribe_parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    notification_parser = subparsers.add_parser(
        "handle-notification",
        help="Run a captured Graph change-notification payload through the member-change pipeline",
    )
    notification_parser.add_argument("--file", required=True, help="Path to a JSON file with the notification body")
    notification_parser.add_argument(
        "--client-state", default=None, help="Expected clientState to validate against; skipped if omitted"
    )
    notification_parser.add_argument(
        "--execute", action="store_true", help="Actually provision added members (default is dry-run)"
    )
    notification_parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    show_sub_state_parser = subparsers.add_parser(
        "show-subscription-state", help="Show the persisted subscription record from blob state"
    )
    show_sub_state_parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    show_hostmap_state_parser = subparsers.add_parser(
        "show-hostmap-state", help="Show the persisted hostmap record for one user from blob state"
    )
    show_hostmap_state_parser.add_argument("--user", required=True, help="User object id (hostmap blob key)")
    show_hostmap_state_parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "provision":
        config = load_config()
        identity = resolve_user_identity(args.upn)
        if identity is None:
            print(f"Warning: {args.upn} not found in Graph — computer name will fall back to the UPN local part.", file=sys.stderr)
        result = provision_session_host(
            upn=args.upn,
            config=config,
            vm_size=args.vm_size,
            dry_run=args.dry_run,
            given_name=identity.given_name if identity else None,
            surname=identity.surname if identity else None,
        )
        print(result)
        return 0

    if args.command == "subscribe-group-changes":
        config = load_config()
        notification_url = (
            args.notification_url or config.notification_listener_url or config.group_change_notification_url
        )
        if not config.users_group_name:
            print("AVD_USERS_GROUP is not set in .env", file=sys.stderr)
            return 1
        if not notification_url:
            print(
                "No notification URL: set AVD_NOTIFICATION_LISTENER_URL (or the legacy "
                "SAMPLE_HTTP_REQUEST_TARGET_FOR_GROUP_CHANGES) in .env, or pass --notification-url",
                file=sys.stderr,
            )
            return 1

        group_id = resolve_group_id(config.users_group_name)
        resource = f"/groups/{group_id}"

        if not args.force:
            existing = find_existing_subscription(resource, notification_url)
            if existing is not None:
                print(f"A matching subscription already exists: {existing['id']} (expires {existing['expirationDateTime']})")
                print("Pass --force to create another one anyway.")
                return 0

        subscription = create_group_change_subscription(group_id=group_id, notification_url=notification_url)
        print(f"Subscription created: {subscription.subscription_id}")
        print(f"  resource:      {subscription.resource}")
        print(f"  notificationUrl: {subscription.notification_url}")
        print(f"  expires:       {subscription.expiration_datetime}")
        print(f"  clientState:   {subscription.client_state}")

        store = _state_store(config)
        store.save_subscription(
            {
                "subscriptionId": subscription.subscription_id,
                "resource": subscription.resource,
                "notificationUrl": subscription.notification_url,
                "expirationDateTime": subscription.expiration_datetime,
                "clientState": subscription.client_state,
            }
        )
        print("Persisted to blob state (subscription.json).")
        return 0

    if args.command == "list-subscriptions":
        subs = list_subscriptions()
        if not subs:
            print("No active Graph subscriptions.")
            return 0
        for sub in subs:
            print(f"{sub['id']}  resource={sub['resource']}  expires={sub['expirationDateTime']}")
            print(f"  notificationUrl: {sub['notificationUrl']}")
        return 0

    if args.command == "unsubscribe":
        delete_subscription(args.subscription_id)
        print(f"Deleted subscription {args.subscription_id}")
        return 0

    if args.command == "handle-notification":
        with open(args.file, encoding="utf-8") as f:
            body = json.load(f)

        expected_client_state = args.client_state
        if expected_client_state is None:
            config_for_state = load_config()
            if config_for_state.state_storage_account_url:
                record = _state_store(config_for_state).get_subscription()
                if record is not None:
                    expected_client_state = record.data.get("clientState")
                    print("Using clientState from persisted subscription state.")

        changes = extract_member_changes(body, expected_client_state=expected_client_state)
        if not changes:
            print("No member changes found in notification payload.")
            return 0

        config = load_config()
        for change in changes:
            if change.removed:
                print(f"[removed] {change.member_id} left group {change.group_id}")
                result = deallocate_session_host(user_key=change.member_id, config=config, dry_run=not args.execute)
                print(f"  deallocate_session_host result: {result}")
                continue

            identity = resolve_user_identity(change.member_id)
            if identity is None:
                print(f"[added] {change.member_id} in group {change.group_id} is not a user object, skipping")
                continue

            print(f"[added] {change.member_id} in group {change.group_id} -> {identity.upn}")
            result = provision_session_host(
                upn=identity.upn,
                config=config,
                dry_run=not args.execute,
                given_name=identity.given_name,
                surname=identity.surname,
            )
            print(f"  provision_session_host result: {result}")
        return 0

    if args.command == "show-subscription-state":
        config = load_config()
        record = _state_store(config).get_subscription()
        if record is None:
            print("No subscription state persisted yet.")
            return 0
        print(json.dumps(record.data, indent=2))
        print(f"etag: {record.etag}")
        return 0

    if args.command == "show-hostmap-state":
        config = load_config()
        record = _state_store(config).get_hostmap_entry(args.user)
        if record is None:
            print(f"No hostmap state persisted for {args.user}.")
            return 0
        print(json.dumps(record.data, indent=2))
        print(f"etag: {record.etag}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
