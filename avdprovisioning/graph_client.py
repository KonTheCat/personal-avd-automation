"""Shared Microsoft Graph HTTP/token plumbing, used by graph_subscriptions and
graph_notifications. Same `DefaultAzureCredential` pattern as the rest of the
package: resolves to your `az login` session locally, a managed identity once
this runs inside the Function App.
"""
from __future__ import annotations

import requests
from azure.identity import DefaultAzureCredential

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
REQUEST_TIMEOUT_SECONDS = 30


def _get_token() -> str:
    credential = DefaultAzureCredential()
    return credential.get_token(GRAPH_SCOPE).token


def headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }


def get(path: str, params: dict | None = None) -> requests.Response:
    return requests.get(f"{GRAPH_BASE_URL}{path}", headers=headers(), params=params, timeout=REQUEST_TIMEOUT_SECONDS)


def post(path: str, json_body: dict) -> requests.Response:
    return requests.post(f"{GRAPH_BASE_URL}{path}", headers=headers(), json=json_body, timeout=REQUEST_TIMEOUT_SECONDS)


def patch(path: str, json_body: dict) -> requests.Response:
    return requests.patch(f"{GRAPH_BASE_URL}{path}", headers=headers(), json=json_body, timeout=REQUEST_TIMEOUT_SECONDS)


def delete(path: str) -> requests.Response:
    return requests.delete(f"{GRAPH_BASE_URL}{path}", headers=headers(), timeout=REQUEST_TIMEOUT_SECONDS)
