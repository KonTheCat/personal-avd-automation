"""Loads and validates configuration from environment variables.

Uses the same variable names whether they come from a local `.env` file (CLI)
or app settings (a future Function App) — no branching needed between the two.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

_REQUIRED = [
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_RESOURCE_GROUP",
    "AZURE_LOCATION",
    "AVD_HOST_POOL_NAME",
    "AVD_VNET_NAME",
    "AVD_SUBNET_NAME",
    "AVD_IMAGE_PUBLISHER",
    "AVD_IMAGE_OFFER",
    "AVD_IMAGE_SKU",
    "AVD_IMAGE_VERSION",
    "AVD_VM_SIZE_DEFAULT",
    "AVD_OS_DISK_TYPE",
    "AVD_LOCAL_ADMIN_USERNAME",
    "AVD_LOCAL_ADMIN_PASSWORD",
    "AVD_AGENT_INSTALLER_URL",
    "AVD_BOOTLOADER_INSTALLER_URL",
]


@dataclass(frozen=True)
class Config:
    subscription_id: str
    resource_group: str
    location: str
    host_pool_name: str
    vnet_resource_group: str
    vnet_name: str
    subnet_name: str
    image_publisher: str
    image_offer: str
    image_sku: str
    image_version: str
    vm_size_default: str
    os_disk_type: str
    local_admin_username: str
    local_admin_password: str
    agent_installer_url: str
    bootloader_installer_url: str
    vm_name_prefix: str
    registration_timeout_seconds: int
    registration_poll_interval_seconds: int
    users_group_name: str | None
    group_change_notification_url: str | None
    notification_listener_url: str | None
    state_storage_account_url: str | None
    state_container_name: str


def load_config() -> Config:
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

    return Config(
        subscription_id=os.environ["AZURE_SUBSCRIPTION_ID"],
        resource_group=os.environ["AZURE_RESOURCE_GROUP"],
        location=os.environ["AZURE_LOCATION"],
        host_pool_name=os.environ["AVD_HOST_POOL_NAME"],
        vnet_resource_group=os.environ.get("AVD_VNET_RESOURCE_GROUP") or os.environ["AZURE_RESOURCE_GROUP"],
        vnet_name=os.environ["AVD_VNET_NAME"],
        subnet_name=os.environ["AVD_SUBNET_NAME"],
        image_publisher=os.environ["AVD_IMAGE_PUBLISHER"],
        image_offer=os.environ["AVD_IMAGE_OFFER"],
        image_sku=os.environ["AVD_IMAGE_SKU"],
        image_version=os.environ["AVD_IMAGE_VERSION"],
        vm_size_default=os.environ["AVD_VM_SIZE_DEFAULT"],
        os_disk_type=os.environ["AVD_OS_DISK_TYPE"],
        local_admin_username=os.environ["AVD_LOCAL_ADMIN_USERNAME"],
        local_admin_password=os.environ["AVD_LOCAL_ADMIN_PASSWORD"],
        agent_installer_url=os.environ["AVD_AGENT_INSTALLER_URL"],
        bootloader_installer_url=os.environ["AVD_BOOTLOADER_INSTALLER_URL"],
        vm_name_prefix=os.environ.get("AVD_VM_NAME_PREFIX", "avd-"),
        registration_timeout_seconds=int(
            os.environ.get("AVD_REGISTRATION_TIMEOUT_SECONDS", "900")
        ),
        registration_poll_interval_seconds=int(
            os.environ.get("AVD_REGISTRATION_POLL_INTERVAL_SECONDS", "20")
        ),
        users_group_name=os.environ.get("AVD_USERS_GROUP") or None,
        group_change_notification_url=os.environ.get("SAMPLE_HTTP_REQUEST_TARGET_FOR_GROUP_CHANGES") or None,
        notification_listener_url=os.environ.get("AVD_NOTIFICATION_LISTENER_URL") or None,
        state_storage_account_url=os.environ.get("AVD_STATE_STORAGE_ACCOUNT_URL") or None,
        state_container_name=os.environ.get("AVD_STATE_CONTAINER_NAME", "state"),
    )
