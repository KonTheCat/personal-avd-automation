"""Key Vault-backed cache for the AVD host pool's registration token.

Stores the token as a Key Vault secret using its native `expires_on` attribute
instead of embedding expiration in a JSON payload the way the old blob cache did.
Mutual exclusion around the check-cache/mint-if-needed decision still lives
entirely in state.py's registration_token_lock() (a blob lease) — Key Vault
secrets have no conditional-write primitive equivalent to blob's
overwrite=False/If-Match, so this module provides no locking of its own;
callers must still hold that lock.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

logger = logging.getLogger(__name__)

REGISTRATION_TOKEN_SECRET_NAME = "registration-token"

__all__ = ["RegistrationTokenRecord", "RegistrationTokenVault"]


@dataclass(frozen=True)
class RegistrationTokenRecord:
    token: str
    expiration_time: datetime


class RegistrationTokenVault:
    def __init__(self, vault_url: str):
        credential = DefaultAzureCredential()
        self._client = SecretClient(vault_url=vault_url, credential=credential)

    def get_registration_token(self) -> RegistrationTokenRecord | None:
        try:
            secret = self._client.get_secret(REGISTRATION_TOKEN_SECRET_NAME)
        except ResourceNotFoundError:
            return None
        except HttpResponseError as exc:
            # A secret whose expires_on has already passed 403s on GET ("not allowed
            # on an expired secret") rather than 404ing like one that was never
            # created — both mean "no usable cached token" to callers.
            if exc.status_code == 403:
                return None
            raise
        if secret.properties.expires_on is None:
            return None
        return RegistrationTokenRecord(token=secret.value, expiration_time=secret.properties.expires_on)

    def save_registration_token(self, token: str, expiration_time: datetime) -> None:
        self._client.set_secret(REGISTRATION_TOKEN_SECRET_NAME, token, expires_on=expiration_time)
