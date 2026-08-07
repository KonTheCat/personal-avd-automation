"""Blob-backed state store: the subscription singleton, the hostmap idempotency
guard, and the registration-token mutual-exclusion lock.

One container, a few kinds of blobs (Section 3 of the plan):
  - "subscription.json"              the current Graph subscription (id, expiration, clientState, notificationUrl)
  - "hostmap/<user-object-id>.json"  one blob per user (vmName, sessionHostResourceId, state, createdUtc)
  - "registration-token.lock"        empty blob used only as a lease target — see
                                      registration_token_lock() below. The registration
                                      token's actual value+expiration lives in Key Vault
                                      (avdprovisioning/token_vault.py), not here.

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
import time
from dataclasses import dataclass

from azure.core import MatchConditions
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)

SUBSCRIPTION_BLOB_NAME = "subscription.json"
HOSTMAP_BLOB_PREFIX = "hostmap/"
REGISTRATION_LOCK_BLOB_NAME = "registration-token.lock"

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

    def registration_token_lock(self, lease_seconds: int = 30, acquire_timeout_seconds: float = 30) -> "_BlobLock":
        """Mutual-exclusion lock guarding the AVD host pool's registration-token mint.

        The host pool has exactly one active registration token at a time.
        CONFIRMED LIVE (2026-07-29): two provisioning runs that each decided
        "no valid token, mint one" 0.55s apart silently invalidated each
        other's already-embedded token, leaving one VM's agent stuck
        registering with a token that had already been overwritten. Callers
        must hold this lock around the check-existing/mint-if-needed
        decision (not around the whole provisioning flow — reading an
        already-valid token is safe for any number of concurrent callers).
        """
        return _BlobLock(self._container, REGISTRATION_LOCK_BLOB_NAME, lease_seconds, acquire_timeout_seconds)

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


class _BlobLock:
    """Context manager wrapping an Azure Blob lease as a short-lived mutex."""

    def __init__(self, container, blob_name: str, lease_seconds: int, acquire_timeout_seconds: float):
        self._blob_client = container.get_blob_client(blob_name)
        self._lease_seconds = lease_seconds
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._lease = None

    def __enter__(self) -> "_BlobLock":
        try:
            self._blob_client.upload_blob(b"", overwrite=False)
        except ResourceExistsError:
            pass
        except HttpResponseError as exc:
            # If the blob already exists AND is currently leased by another
            # holder, Azure returns LeaseIdMissing here instead of the plain
            # "already exists" conflict (confirmed live) — that still means
            # the blob is present, which is all this step needs; the lease
            # acquisition below is what actually enforces mutual exclusion.
            if exc.error_code != "LeaseIdMissing":
                raise
        deadline = time.monotonic() + self._acquire_timeout_seconds
        while True:
            try:
                self._lease = self._blob_client.acquire_lease(lease_duration=self._lease_seconds)
                return self
            except HttpResponseError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

    def __exit__(self, *exc_info) -> None:
        self._lease.release()
