"""Blob-backed state store: the subscription singleton and the hostmap idempotency guard.

One container, two kinds of blobs (Section 3 of the plan):
  - "subscription.json"              the current Graph subscription (id, expiration, clientState, notificationUrl)
  - "hostmap/<user-object-id>.json"  one blob per user (vmName, sessionHostResourceId, state, createdUtc)

Blob ETags give the same guarantees Table Storage rows would:
  - claim_hostmap_entry() uploads with overwrite=False, which 409s
    (ResourceExistsError) if the blob already exists — the atomic
    "claim this user before doing anything else" idempotency guard against
    duplicate/replayed notifications racing each other.
  - update_* functions accept an optional etag from a prior read and pass it
    back as an `If-Match` precondition, so a stale concurrent writer is
    rejected (ResourceModifiedError) instead of silently clobbering newer data.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)

SUBSCRIPTION_BLOB_NAME = "subscription.json"
HOSTMAP_BLOB_PREFIX = "hostmap/"

__all__ = [
    "BlobRecord",
    "StateStore",
    "AlreadyClaimedError",
    "ConcurrentUpdateError",
]


class AlreadyClaimedError(RuntimeError):
    """Raised by claim_hostmap_entry() when another run already claimed this user."""


class ConcurrentUpdateError(RuntimeError):
    """Raised when an update's etag precondition doesn't match the blob's current state."""


@dataclass(frozen=True)
class BlobRecord:
    data: dict
    etag: str


class StateStore:
    def __init__(self, account_url: str, container_name: str):
        credential = DefaultAzureCredential()
        service_client = BlobServiceClient(account_url=account_url, credential=credential)
        self._container = service_client.get_container_client(container_name)

    def get_subscription(self) -> BlobRecord | None:
        return self._get_json(SUBSCRIPTION_BLOB_NAME)

    def save_subscription(self, data: dict, etag: str | None = None) -> None:
        self._put_json(SUBSCRIPTION_BLOB_NAME, data, etag=etag)

    def get_hostmap_entry(self, user_key: str) -> BlobRecord | None:
        return self._get_json(_hostmap_blob_name(user_key))

    def claim_hostmap_entry(self, user_key: str, data: dict) -> None:
        """Atomically create the hostmap entry if it doesn't exist yet.

        Raises AlreadyClaimedError if another run already claimed this user —
        callers should treat that as "someone else is handling this, skip",
        not as a fatal error.
        """
        blob_client = self._container.get_blob_client(_hostmap_blob_name(user_key))
        try:
            blob_client.upload_blob(json.dumps(data, default=str).encode("utf-8"), overwrite=False)
        except ResourceExistsError as exc:
            raise AlreadyClaimedError(f"hostmap entry for {user_key} already claimed") from exc

    def update_hostmap_entry(self, user_key: str, data: dict, etag: str | None = None) -> None:
        self._put_json(_hostmap_blob_name(user_key), data, etag=etag)

    def _get_json(self, blob_name: str) -> BlobRecord | None:
        blob_client = self._container.get_blob_client(blob_name)
        try:
            downloaded = blob_client.download_blob()
        except ResourceNotFoundError:
            return None
        content = downloaded.readall()
        return BlobRecord(data=json.loads(content), etag=downloaded.properties.etag)

    def _put_json(self, blob_name: str, data: dict, etag: str | None) -> None:
        blob_client = self._container.get_blob_client(blob_name)
        payload = json.dumps(data, default=str).encode("utf-8")
        try:
            if etag is not None:
                blob_client.upload_blob(
                    payload, overwrite=True, etag=etag, match_condition=MatchConditions.IfNotModified
                )
            else:
                blob_client.upload_blob(payload, overwrite=True)
        except ResourceModifiedError as exc:
            raise ConcurrentUpdateError(f"{blob_name} was modified since it was last read") from exc


def _hostmap_blob_name(user_key: str) -> str:
    return f"{HOSTMAP_BLOB_PREFIX}{user_key}.json"
