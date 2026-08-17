# AVD Per-User Automation — Implementation Guide

This is the step-by-step setup runbook: what to provision, what identity to create, and exactly which permissions to grant it, in order. For *why* the system is built the way it is (architecture rationale, superseded designs, live-test history), see [avd-provisioning-function-plan.md](avd-provisioning-function-plan.md) — this guide is the reproducible checklist version of that document, with the RBAC question nailed down explicitly.

Current live deployment (for reference, values will differ in a new tenant/subscription): Function App `ktk-avd-per-user-automation`, resource group `avd-per-user-automation`, managed identity principal id `REDACTED-PRINCIPAL-ID`.

## 1. What this system does

A single Entra ID security group is the source of truth for "who should have a personal AVD desktop":

- **User added to the group** → a Function App provisions a dedicated VM, Entra-ID-joins it, installs the AVD agent, and assigns it to that user in an existing AVD host pool.
- **User removed from the group** → the Function App deallocates (stops, doesn't delete) their VM.

Detection is via a Microsoft Graph **change-notification subscription** on the group (a webhook), not polling or Event Grid. A daily timer keeps that subscription alive.

```
Entra group membership change
        │
        ▼
Graph change-notification webhook (HTTP POST)
        │
        ▼
notification_listener (HTTP)  →  Storage Queue  →  group_change_processor
                                                          │
                                              provisions or deallocates the VM
```

Everything the app does at runtime is either a **Microsoft Graph** call (read group/user data, manage the subscription) or an **Azure Resource Manager** call (create/deallocate VMs, manage AVD session hosts) — plus Blob Storage and Key Vault for its own state.

## 2. RBAC — the full picture, least privilege

Three distinct identities are involved. Don't conflate them — each gets a different, narrow grant.

| Identity | Used for | Held permanently? |
|---|---|---|
| Function App's **system-assigned managed identity** | All runtime Graph + ARM + Storage + Key Vault calls | Yes — this is the thing to keep minimal |
| A **human admin** (you) | One-time: consenting to the app's Graph permissions | No — needed for a few minutes during setup only |
| **GitHub Actions OIDC** app registration | Deploying code on push to `main` | Yes, but scoped to one resource |

### 2.1 The managed identity's Microsoft Graph permissions

Application (not delegated) permissions, granted as app roles on the managed identity's service principal:

| Permission | Why |
|---|---|
| `GroupMember.Read.All` | Resolve the group by display name; required to create/manage a change-notification subscription on a `group` resource |
| `User.Read.All` | Resolve a changed member's object id to a UPN (`GET /users/{id}`) |

That's the entire Graph surface this app touches. It cannot write group membership, reset passwords, read mail, or manage any directory object outside these two read scopes.

### 2.2 The managed identity's Azure RBAC (ARM) roles

All scoped to the specific resource group / resource — not the subscription:

| Role | Scope | Why |
|---|---|---|
| Virtual Machine Contributor | AVD resource group | Create NICs' attached VMs, run the agent-install command, deallocate VMs on removal |
| Network Contributor | AVD resource group (or just the VNet, if it lives elsewhere) | Create the NIC in the existing subnet |
| Desktop Virtualization Host Pool Contributor | AVD resource group | Mint registration tokens, list/update session hosts, assign the user |
| Storage Blob Data Contributor | State storage account only | Read/write `subscription.json` and `hostmap/*.json` — this is a **data-plane** role; Owner/Contributor on the account does not imply it |
| Key Vault Secrets Officer | The one Key Vault only | Cache the AVD registration token as a secret — needs write (mint/refresh), not just read, so Secrets User isn't enough |

None of this is subscription-wide, and none of it is a directory role.

### 2.3 What access *you* (the implementer) need to run this guide

This is separate from §2.1/§2.2 above — those are what the *app* ends up holding permanently. This is what the human doing the setup needs, temporarily, to get there. Two different kinds of access cover everything in §3:

**Azure RBAC** — for creating the resource group/storage/Key Vault/Function App, and for assigning the roles in §2.2 and §2.4 to the managed identity and CI/CD service principal (role assignment requires `Microsoft.Authorization/roleAssignments/write`, which plain Contributor does not include):
- **Owner**, or **Contributor + User Access Administrator**, scoped to the specific resource group(s) involved (the automation RG, and the AVD RG if it's separate). Subscription-wide Owner is not needed unless resources are scattered across RGs you don't already control.

**Two Entra ID directory roles**, for two genuinely different actions — don't collapse these into one:

- **Granting the two Graph app-role assignments** (`GroupMember.Read.All`, `User.Read.All`) to the managed identity's service principal — this is consent for *Microsoft Graph* application permissions specifically, and per Microsoft's own docs, Application Administrator and Cloud Application Administrator are **blanket-excluded from consenting to any Microsoft Graph application permission**, not just a curated high-risk subset ("This role also grants the ability to consent to delegated permissions, and application permissions **excluding Microsoft Graph**" — [Delegate application management administrator permissions](https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/delegate-app-roles)). That leaves exactly two roles that can do it: **Global Administrator** or **Privileged Role Administrator**. Use **Privileged Role Administrator** — it's the purpose-built, narrower of the two: no Conditional Access, no general user/device/mail administration, no licensing, nothing outside role- and permission-management. It's trusted for Graph consent for the same reason Global Admin is (both can already reach tenant-wide privilege via role assignment), but it doesn't carry Global Admin's much broader operational surface.
- **Creating the GitHub OIDC app registration + its federated credential** is a separate action that does *not* touch Graph permission consent — **Application Administrator** (or Cloud Application Administrator) is correct and sufficient for this one, and can also create the registration regardless of the tenant's "users can register applications" default.

**Not Global Administrator** for either. Privileged Role Administrator covers the one action that genuinely requires a top-tier role here, without the rest of what Global Admin carries (tenant-wide password reset including other admins', Conditional Access, every other app in the tenant, etc.) — none of which this setup touches. If your org supports PIM, activate Privileged Role Administrator and Application Administrator as time-boxed eligible assignments for the setup window, and Global Administrator never needs to enter the picture.

### 2.4 CI/CD identity (GitHub Actions OIDC)

Separate app registration, federated credential trusting `repo:<owner>/<repo>:ref:refs/heads/main` (no stored secret). Service principal role: **Website Contributor**, scoped to just the one Function App resource — enough to deploy code, nothing else. Not involved in any Graph or AVD permission question.

### 2.5 Summary — what to grant, to whom, once

```
Managed identity (system-assigned, on the Function App) — permanent
├── Graph app roles:      GroupMember.Read.All, User.Read.All
├── ARM (on AVD RG):      Virtual Machine Contributor
│                         Network Contributor
│                         Desktop Virtualization Host Pool Contributor
├── ARM (on storage acct): Storage Blob Data Contributor
└── ARM (on Key Vault):    Key Vault Secrets Officer

You, running the setup — temporary, activate/deactivate around the setup window
├── Azure RBAC:  Owner (or Contributor + User Access Administrator),
│                scoped to the automation/AVD resource group(s)
│                — creates resources, assigns the roles above
└── Entra ID:    Privileged Role Administrator
                 — the only way (short of Global Admin) to grant the two
                   Graph app roles above; Application/Cloud Application
                   Administrator are blanket-excluded from consenting to
                   ANY Microsoft Graph application permission
                 Application Administrator
                 — creates the GitHub OIDC app registration + federated
                   credential (a separate action, doesn't touch Graph consent)

GitHub Actions app registration — permanent
└── Website Contributor, scoped to the Function App resource only
```

Nothing in this system, at any point, needs Global Administrator.

## 3. Step-by-step setup

### 3.1 Prerequisites

- An existing AVD host pool (Personal, direct assignment), its application group, workspace, and the VNet/subnet the session hosts will join. This system does not create these.
- A marketplace VM image reference (publisher/offer/sku/**pinned version**, not `latest`).
- The Entra security group that will gate access (e.g. "AVD Personal Users").

### 3.2 Provision supporting infrastructure

Requires the Azure RBAC access described in §2.3 (Owner, or Contributor + User Access Administrator, on the target resource group(s)). No IaC currently — done via `az` CLI or portal:

1. Resource group for the automation (or reuse the AVD resource group).
2. Storage account for Function state (`AzureWebJobsStorage`) — created automatically with the Function App.
3. A **second** storage account (or container) for app state: `subscription.json`, `hostmap/*.json`.
4. Key Vault, RBAC-authorized (not access-policy mode), for the registration-token cache.
5. Function App: Linux, Flex Consumption, Python runtime, **system-assigned managed identity enabled**.

### 3.3 Grant the managed identity its Azure RBAC roles

```bash
principalId=$(az functionapp identity show -g <rg> -n <app-name> --query principalId -o tsv)

az role assignment create --assignee "$principalId" --role "Virtual Machine Contributor" \
  --scope /subscriptions/<sub>/resourceGroups/<avd-rg>
az role assignment create --assignee "$principalId" --role "Network Contributor" \
  --scope /subscriptions/<sub>/resourceGroups/<avd-rg>
az role assignment create --assignee "$principalId" --role "Desktop Virtualization Host Pool Contributor" \
  --scope /subscriptions/<sub>/resourceGroups/<avd-rg>
az role assignment create --assignee "$principalId" --role "Storage Blob Data Contributor" \
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<state-account>
az role assignment create --assignee "$principalId" --role "Key Vault Secrets Officer" \
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.KeyVault/vaults/<vault-name>
```

### 3.4 Grant the managed identity its Graph app roles (the consent step)

Run this as a user holding Application Administrator or Privileged Role Administrator (§2.3):

```bash
graphSpId=$(az ad sp list --filter "appId eq '00000003-0000-0000-c000-000000000000'" --query "[0].id" -o tsv)

for perm in GroupMember.Read.All User.Read.All; do
  roleId=$(az ad sp show --id "$graphSpId" --query "appRoles[?value=='$perm'].id" -o tsv)
  az rest --method POST \
    --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$principalId/appRoleAssignments" \
    --body "{\"principalId\":\"$principalId\",\"resourceId\":\"$graphSpId\",\"appRoleId\":\"$roleId\"}"
done
```

Verify: `az ad sp show --id $principalId` → check `appRoleAssignedTo`, or Entra portal → Enterprise Applications → the app → Permissions.

### 3.5 Configure app settings

Set every variable in `.env.example` as a Function App setting (see `avdprovisioning/config.py` for the full required/optional list) — subscription id, resource group, host pool name, VNet/subnet, image reference (pinned version), VM size, local admin username, agent/bootloader installer URLs, state storage account URL, Key Vault URL.

**`AVD_LOCAL_ADMIN_PASSWORD` — store as a Key Vault secret, reference it from the app setting:**

```bash
az keyvault secret set --vault-name <vault-name> --name avd-local-admin-password --value "<password>"

az functionapp config appsettings set -g <rg> -n <app-name> --settings \
  "AVD_LOCAL_ADMIN_PASSWORD=@Microsoft.KeyVault(SecretUri=https://<vault-name>.vault.azure.net/secrets/avd-local-admin-password/)"
```

This is a platform feature, not a code change: the Functions host resolves `@Microsoft.KeyVault(...)` app settings via the managed identity before the function process starts, so `config.py` still reads a plain string from the environment — it never touches Key Vault directly. No extra RBAC needed either: the managed identity already holds **Key Vault Secrets Officer** on this vault (§2.2) for the registration-token cache, which covers reading this secret too.

### 3.6 Deploy the code

```bash
func azure functionapp publish <app-name> --python
```

Not `Azure/functions-action@v1` and not `az functionapp deployment source config-zip` — both skip or mishandle the remote Oryx build on Flex Consumption (see plan §13). GitHub Actions does this same command via OIDC on push to `main`.

### 3.7 Bootstrap the Graph subscription

The subscription doesn't exist until created once:

```bash
python local_cli.py subscribe-group-changes
```

This resolves the group, creates the subscription pointed at the deployed `/api/notifications` URL, and persists it to Blob state. After this, the daily `subscription_renewer` timer keeps it alive indefinitely.

### 3.8 Validate end-to-end

1. Add a test user to the group → confirm a queue message appears → confirm a VM is created, joined, agent-registered, and assigned (check `hostmap/<object-id>.json` reaches `state: assigned`).
2. Remove the test user → confirm the VM deallocates (`state: deallocated`).
3. Replay a captured payload without waiting on real Graph delivery: `python local_cli.py handle-notification --file sample-added-user-event-request-body.txt --execute`.

## 4. Operational notes carried over from the design doc

Worth knowing before you call this done — none of these are blockers, but they're gaps to plan around:

- **No reconciliation for missed webhooks.** Graph doesn't guarantee delivery; a dropped notification silently drops an add/remove. Mitigation (daily delta-query reconciliation) is deferred, not built.
- **Re-adding a removed user doesn't reallocate.** A `deallocated` hostmap entry blocks re-provisioning (`AlreadyClaimedError`, silent no-op) until this is explicitly fixed.
- **No rollback on partial provisioning failure** — a failed step after the hostmap claim leaves the entry stuck in `provisioning` forever; this is meant to be alerted on, not auto-healed.
- **No cap on VM count** — group membership is the only gate on cost.

Full detail on each is in [avd-provisioning-function-plan.md](avd-provisioning-function-plan.md) §5, §12.
