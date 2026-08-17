"""Deterministic VM/NIC resource names and Windows computer_name from a user.

Two different constraints apply:
  - Azure resource names (VM, NIC): up to 64 chars, can be descriptive.
  - Windows `computer_name`: hard ARM limit of 15 chars, alphanumeric/hyphen
    only, can't be all-numeric, can't end in a hyphen.

vm_name/nic_name and computer_name are both built from `prefix + _core_name`
(surname then given name, so the part most likely to survive a 15-char
truncation is the part someone would actually search/sort by) so the two
always agree on everything up to whichever one runs out of characters first —
both start with the same prefix (e.g. "avd-", so it's obvious in the Entra
device list which boxes this system manages) and the Windows-level computer
name is always a literal prefix of the Azure resource name.
"""
from __future__ import annotations

import re


def _local_part(upn: str) -> str:
    return upn.split("@", 1)[0]


def _sanitize(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]", "", value).lower()


def _core_name(upn: str, given_name: str | None = None, surname: str | None = None) -> str:
    """Surname + given name when Graph gave us real name parts; falls back to
    the UPN local part when it didn't (e.g. a user with no givenName/surname
    set, or an ad-hoc CLI run that couldn't resolve the UPN against Graph)."""
    last = _sanitize(surname or "")
    first = _sanitize(given_name or "")
    core = f"{last}{first}" if (last or first) else _sanitize(_local_part(upn))
    return core.strip("-")


def _prefixed_core(prefix: str, upn: str, given_name: str | None, surname: str | None) -> str:
    return f"{_sanitize(prefix)}{_core_name(upn, given_name, surname)}".strip("-")


def vm_name(upn: str, prefix: str, given_name: str | None = None, surname: str | None = None) -> str:
    return _prefixed_core(prefix, upn, given_name, surname)[:64]


def nic_name(upn: str, prefix: str, given_name: str | None = None, surname: str | None = None) -> str:
    return f"{vm_name(upn, prefix, given_name, surname)}-nic"[:80]


def computer_name(upn: str, prefix: str, given_name: str | None = None, surname: str | None = None) -> str:
    # Slicing prefix+core to 15 chars naturally gives "prefix, then as much
    # surname as fits, then as much given name as fits in what's left" — no
    # separate budget-splitting logic needed.
    truncated = _prefixed_core(prefix, upn, given_name, surname)[:15].rstrip("-")
    if not truncated or truncated.isdigit():
        raise ValueError(
            f"Could not derive a valid Windows computer name from UPN {upn!r} "
            f"(got {truncated!r} after sanitizing/truncating)"
        )
    return truncated
