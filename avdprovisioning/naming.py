"""Deterministic VM/NIC resource names and Windows computer_name from a UPN.

Two different constraints apply:
  - Azure resource names (VM, NIC): up to 64 chars, can be descriptive.
  - Windows `computer_name`: hard ARM limit of 15 chars, alphanumeric/hyphen
    only, can't be all-numeric, can't end in a hyphen.
"""
from __future__ import annotations

import re


def _local_part(upn: str) -> str:
    return upn.split("@", 1)[0]


def _sanitize(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]", "", value).lower()


def vm_name(upn: str, prefix: str) -> str:
    return f"{prefix}{_sanitize(_local_part(upn))}"[:64]


def nic_name(upn: str, prefix: str) -> str:
    return f"{vm_name(upn, prefix)}-nic"[:80]


def computer_name(upn: str) -> str:
    sanitized = _sanitize(_local_part(upn)).strip("-")
    truncated = sanitized[:15].rstrip("-")
    if not truncated or truncated.isdigit():
        raise ValueError(
            f"Could not derive a valid Windows computer name from UPN {upn!r} "
            f"(got {truncated!r} after sanitizing/truncating)"
        )
    return truncated
