"""Builds Azure SDK management clients using DefaultAzureCredential.

Locally this resolves to your `az login` session automatically; unchanged,
it resolves to a managed identity when this same code later runs inside an
Azure Function App — no branching needed between the two.
"""
from __future__ import annotations

from dataclasses import dataclass

from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.desktopvirtualization import DesktopVirtualizationMgmtClient
from azure.mgmt.network import NetworkManagementClient

from .config import Config


@dataclass(frozen=True)
class AzureClients:
    compute: ComputeManagementClient
    network: NetworkManagementClient
    avd: DesktopVirtualizationMgmtClient


def build_clients(config: Config) -> AzureClients:
    credential = DefaultAzureCredential()
    return AzureClients(
        compute=ComputeManagementClient(credential, config.subscription_id),
        network=NetworkManagementClient(credential, config.subscription_id),
        avd=DesktopVirtualizationMgmtClient(credential, config.subscription_id),
    )
