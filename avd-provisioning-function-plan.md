# AVD Per-User Personal Desktop Provisioning — Function App Plan

## 0. Assumptions (locked in from discussion)

- **Identity/domain join:** Microsoft Entra ID join only. No on-prem AD, no hybrid join, no AD DS.
- **Image source:** Marketplace image (e.g. Windows 11 Enterprise multi-session), not a Compute Gallery golden image.
- **Assignment model:** Direct assignment — the Function explicitly sets `assignedUser` on the session host to the UPN of the group member who triggered provisioning. Not relying on Automatic/first-connect assignment.
- **Pre-existing resources** (not created by this Function): the Personal host pool, its Application Group, Workspace, virtual network/subnet, and (if used) scaling plan. This plan only covers **session host provisioning** — creating and assigning the per-user VM — driven by group membership additions.
- Trigger source is a Microsoft Entra ID security group whose membership represents "should have a personal AVD desktop."

## 1. Architecture summary

Three separate pieces of compute, all in **one Python Function App**, each doing one job:

| # | Function | Trigger | Job |
|---|----------|---------|-----|
| 1 | `notification_listener` | HTTP | Validate the Graph webhook handshake, verify `clientState`, extract the added/removed member id(s) directly from the notification payload, and hand off to a queue — fast ack only, no heavy logic. |
| 2 | `group_change_processor` | Storage Queue | Do the actual work: for an add, resolve the member's object id to a UPN and provision+assign a new VM; for a remove, deallocate the VM already on record for that object id. |
| 3 | `subscription_renewer` | Timer (daily) | Keep the Graph subscription alive; recreate it if it's gone. |

**Confirmed by live test (2026-07-27):** a real add and a real remove against the `AVD Personal Users` group were captured (`sample-added-user-event-request-body.txt`, `sample-removed-user-event-request-body.txt`). Both show `resourceData['members@delta']` containing the changed member's object id directly on the notification itself — an add has just `{"id": ...}`, a remove has `{"id": ..., "@removed": "deleted"}`. `clientState` and `subscriptionId` on the payload matched the subscription created via `local_cli.py subscribe-group-changes` exactly, confirming the authenticity check works as designed.

Why split the listener from the processor: Graph expects an HTTP ack within ~30 seconds of a notification, but provisioning a VM can take several minutes. The listener's only job is "is this legitimate, queue it, return 202." The queue-triggered function does the slow work on its own clock, with retries, independent of Graph's timeout.

```
Entra ID group membership change
        │
        ▼
Microsoft Graph change notification (POST)
   body: value[].resourceData['members@delta'] = [{id}, ...]  (add)
                                              or  [{id, "@removed": "deleted"}, ...]  (remove)
        │
        ▼
[HTTP] notification_listener  ──validate clientState──▶  Storage Queue
        │ (202 within seconds)     one message per changed member id,
        ▼                          tagged change_type: "added"/"removed"
   (Graph is done, moves on)

Storage Queue
        │
        ▼
[Queue] group_change_processor
   added:   1. Resolve member object id → UPN  (GET /users/{id})
            2. provision_session_host(upn)
   removed: 1. deallocate_session_host(member_id)  — looks up the VM
               directly off the hostmap, no Graph call needed

[Timer, daily] subscription_renewer
   1. Load subscription record from Blob state (subscription.json)
   2. PATCH expirationDateTime forward
   3. If PATCH fails (410/404) → recreate subscription, store new id
```

## 2. Decision: act directly on the notification payload, no delta query

**Superseded design note:** earlier drafts of this plan assumed a Graph group change notification only says *"this group changed"* and that the processor must call `GET /groups/{group-id}/members/delta` (with a stored `deltaLink`) to find out who was actually added or removed. Live testing on 2026-07-27 disproved that assumption for this resource type — see the confirmation note in Section 1. The notification payload already carries the changed member's object id, with an `@removed` marker distinguishing removals from adds.

**Decision (deliberate, discussed 2026-07-27): drop the delta query entirely.** The pipeline trusts `resourceData['members@delta']` on each notification as the sole source of truth for membership changes. This is simpler (no deltaLink state, no first-call-returns-full-baseline special case, no serialization requirement around a shared deltaLink row) but carries a known, accepted risk: **Microsoft Graph does not guarantee 100% notification delivery** (notifications can be dropped under service-side issues or throttling). If one is lost, the corresponding add/remove is silently missed with no reconciliation to catch it later. If this risk stops being acceptable, the mitigation is to reintroduce a low-frequency (e.g. daily) delta-query reconciliation pass — the group's full membership vs. `hostmap` state — without touching the hot path built here.

## 3. State storage (Azure Blob Storage — one small container)

**Built and live-tested (2026-07-27):** `avdprovisioning/state.py`, backed by a real storage account (`avdperuserstate` in the `avd-per-user-automation` RG, container `state`). Chosen over Table Storage: once the Section 2 decision dropped the delta query, the only access pattern left is single-key lookups (one subscription record, one record per user) — Blob Storage handles that with a simpler dependency (`azure-storage-blob` vs. `azure-data-tables`) and no loss of capability.

| Blob name | Contents |
|---|---|
| `subscription.json` | `subscriptionId`, `expirationDateTime`, `clientState` (secret), `notificationUrl` |
| `hostmap/<user-object-id>.json` | `vmName`, `sessionHostResourceId`, `state` (provisioning/assigned), `createdUtc` |

No `delta` blob/partition — per the Section 2 decision, membership changes are read directly off each notification, not reconstructed via a stored `deltaLink`.

The `hostmap` blobs double as your idempotency guard, using conditional headers instead of Table Storage's ETag-on-entity mechanics — same guarantee, different plumbing:
- **Claim** (`StateStore.claim_hostmap_entry`): uploads with `overwrite=False` (`If-None-Match: *`), which fails atomically (`AlreadyClaimedError`) if a row already exists for this user — so a duplicate/replayed notification backs off instead of creating a second VM.
- **Update** (`StateStore.update_hostmap_entry`): takes the `etag` from a prior read and passes it back as an `If-Match` precondition (`ConcurrentUpdateError` if it's stale) for the `provisioning` → `assigned` transition.

Both behaviors (claim-rejects-duplicate, update-rejects-stale-etag) were verified live against the real container before being written up here.

**Identity:** whoever calls this (your `az login` locally, the Function App's managed identity in production) needs the **Storage Blob Data Contributor** role on the storage account — this is a data-plane RBAC role, separate from and not implied by control-plane roles like Owner/Contributor on the account itself.

**Built and live-tested (2026-08-06):** the `registration-token.json` blob (the cached AVD host pool registration token — see Section 5 step 4) moved out of this container into Key Vault; `registration-token.lock` stays here — it's a lease-based mutex, unrelated to where the token *value* is stored. See Section 7.

## 4. Function 1 — `notification_listener` (HTTP trigger)

**Built and deployed (2026-07-27):** `function_app.py`, route `/api/notifications`, anonymous auth (Graph's webhook POST carries no auth header — `clientState` is the authenticity check, not a function key).

**Responsibilities:**
1. **Validation handshake:** if the request has a `validationToken` query parameter, echo it back as `text/plain` with HTTP 200 within 10 seconds. This only happens at subscription creation/recreation time. (Originally proved out against the Logic App stand-in for this function; the real Function now does the same check directly.)
2. **Real notifications:** parse the `changeNotificationCollection` body. For each notification:
   - Compare `clientState` against the secret in `subscription.json` (Blob state, loaded fresh each invocation). Discard/reject anything that doesn't match — this is your only proof the call came from your own subscription and not a forged request.
   - Read `resourceData['members@delta']` directly off the notification. For each entry, push a message (group id, member object id, `change_type`: `"added"` or `"removed"` depending on whether the entry carries an `@removed` marker) onto the `group-membership-changes` Storage Queue via the function's queue output binding. **Built and live-tested (2026-08-17):** removals used to be dropped here entirely; both directions now flow through the same queue (see Section 5).
3. Return HTTP 202 immediately after queueing — don't wait on any Graph/VM calls in this function.

**Open decision, still open:** a `lifecycleNotificationUrl` (for `reauthorizationRequired` events) on a separate route hasn't been added. Not required for v1 — the daily renewer (Section 6) covers the same need on a schedule.

## 5. Function 2 — `group_change_processor` (Storage Queue trigger)

**Built and deployed (2026-07-27):** `function_app.py`, triggered on the `group-membership-changes` queue (`AzureWebJobsStorage` connection). Poison-message handling is the platform default (5 dequeue attempts, then `group-membership-changes-poison`) — no custom retry logic.

**Responsibilities, per invocation (one queued member id), branching on `change_type`:**
- `"added"`:
  1. Resolve the member's object id to a UPN: `GET /users/{id}?$select=userPrincipalName`. A 404 means the id isn't a user (e.g. a nested group or service principal was added to the group) — log and skip.
  2. `provision_session_host(upn, config, user_key=member_id)`
- `"removed"`: `deallocate_session_host(member_id, config)` — see below.

**Built and live-tested (2026-08-17):** removals used to be filtered out in `notification_listener` (Section 4) and never reached this queue, silently discarding a real signal ("this person left the group"). They now flow through and trigger `deallocate_session_host`.

### `deallocate_session_host(user_key)` steps
1. Look up `StateStore.get_hostmap_entry(user_key)` directly — no Graph call needed, since the queued `member_id` is already the hostmap key.
2. If no entry, or the entry's `state` isn't `"assigned"` (e.g. already `"deallocated"` from a prior/duplicate removal, or still `"provisioning"`), skip — this is what makes the function idempotent against duplicate/replayed removal notifications.
3. `ComputeManagementClient.virtual_machines.begin_deallocate(resource_group, vm_name)` — stops the VM and releases compute billing; the VM/NIC/disk and the host pool's session-host registration are left in place (deallocate, not delete). The host pool's `assigned_user` on the session host is deliberately left untouched.
4. `StateStore.update_hostmap_entry(user_key, {...state: "deallocated", deallocatedUtc...}, etag=...)`.

**Known gap, deliberately out of scope:** once a hostmap entry is `"deallocated"`, `claim_hostmap_entry`'s `overwrite=False` guard only checks "does a blob exist," not its state — if this user is re-added to the group later, `provision_session_host` will hit `AlreadyClaimedError` and silently no-op (`"already_claimed"`) instead of reallocating the existing VM. Fixing that (teaching `provision_session_host` to recognize and reallocate a `"deallocated"` entry) is its own follow-up, not covered here.

### `provision_session_host(user)` steps — **now wired to `StateStore` (2026-07-27), closing the previously-flagged gap**
1. **Idempotency check:** `StateStore.get_hostmap_entry(user_key)` — if a row exists in `provisioning`/`assigned` state, skip. `user_key` is the Graph object id when called from the queue processor (falls back to the UPN when called ad hoc via `local_cli.py provision --upn`, which has no Graph object id).
2. `StateStore.claim_hostmap_entry(user_key, {...state: "provisioning"...})` — claims it before doing anything else, so a concurrent duplicate message backs off with `AlreadyClaimedError` instead of racing to create a second VM. If `AVD_STATE_STORAGE_ACCOUNT_URL` isn't configured, this falls back to the old live-session-host-query check (logged as a warning) rather than failing outright.
3. **Create the VM** (`azure-mgmt-compute` + `azure-mgmt-network`):
   - NIC in the pre-existing subnet.
   - VM from the marketplace image (pin publisher/offer/sku/version explicitly — don't float on `latest` for a golden-path production build).
   - Entra ID join via the AADLoginForWindows VM extension (this is the standard mechanism for Entra-ID-join-only AVD hosts — no domain credentials needed).
4. **Install the AVD agent + boot loader** via a Custom Script Extension / Run Command, using a registration token obtained from `HostPoolsOperations.list_registration_tokens` (generate fresh, short-lived, don't reuse a stale one sitting in config).
5. **Wait for the session host to register** — poll `SessionHostsOperations.list` on the host pool for a new host with a matching VM resource ID. Set a reasonable timeout (VM boot + extension run is typically several minutes) and back off politely rather than tight-polling.
6. **Assign the user directly:** `SessionHostsOperations.update(..., assigned_user=user.userPrincipalName)`.
7. `StateStore.update_hostmap_entry(user_key, {...state: "assigned"...})`.
8. On any failure at any step: the hostmap entry is deliberately left in `provisioning` state (no auto-rollback) — this is the signal Section 11's "stuck in provisioning" alert watches for. Whether to also tear down partially-created resources (orphaned NIC/VM) on failure is still an open call, not implemented.

## 6. Function 3 — `subscription_renewer` (Timer trigger, daily)

**Built and deployed (2026-07-27):** `function_app.py` + `avdprovisioning/subscriptions.py`, timer schedule `0 0 3 * * *` (daily at 03:00 UTC).

1. `StateStore.get_subscription()` — read the current subscription record from `subscription.json`.
2. `PATCH /subscriptions/{id}` with `expirationDateTime` pushed forward (comfortably inside the 41,760-minute/~29-day ceiling for `group` resources — always renews to "now + 20 days" so there's a wide safety margin even if a run is missed for a few days).
3. If the PATCH fails because the subscription no longer exists (404) or is otherwise dead: **recreate** the subscription (new `clientState`, generate a fresh secret) pointed at `AVD_NOTIFICATION_LISTENER_URL`. No deltaLink to reset (Section 2 decision) — a fresh subscription just means new notifications start flowing again; there's no baseline/replay step needed since membership changes are read straight off each notification, not reconstructed from a delta cursor.
4. `StateStore.save_subscription(...)` — store the refreshed subscription id/expiration/clientState, passing the prior `etag` for the optimistic-concurrency guard.

## 7. Identity & permissions this Function App needs

**Built and granted (2026-07-27).** Simpler than originally sketched: since app-only Graph access via managed identity works fine for the subscriptions/user-lookup calls this pipeline actually makes (no delta-query edge cases to worry about post-Section-2), there's a single identity for everything — no separate app registration, no client secret. **Key Vault added (2026-08-06):** `avdperuserkv` (same RG, RBAC-authorized, no purge protection) caches the AVD host pool registration token as a secret (`avdprovisioning/token_vault.py`) — see below.

**Azure RBAC**, system-assigned managed identity (principal id `REDACTED-PRINCIPAL-ID`) granted on:
- `avd` resource group: Virtual Machine Contributor, Network Contributor, Desktop Virtualization Host Pool Contributor
- `avdperuserstate` storage account: Storage Blob Data Contributor
- `avdperuserkv` Key Vault: Key Vault Secrets Officer (needs both read and write — the app mints and caches new tokens, not just reads them)

**Microsoft Graph application permissions**, granted directly to the same managed identity's service principal via `appRoleAssignedTo` (no separate admin-consent step needed — programmatic app role assignment takes effect immediately):
- `GroupMember.Read.All` — resolve the group by name, create/manage the change-notification subscription
- `User.Read.All` — resolve a member's object id (from the notification) to a UPN

**Auth pattern:** system-assigned managed identity for *everything* — ARM and Graph both. `DefaultAzureCredential` resolves to it automatically once deployed, same code path as local `az login`. `AVD_LOCAL_ADMIN_PASSWORD` is stored as a plain Function App setting (not Key Vault) — a deliberate simplicity tradeoff for a personal project; revisit if this ever needs tighter secret hygiene.

## 8. Python packages

```
azure-functions
azure-identity
azure-mgmt-compute
azure-mgmt-network
azure-mgmt-desktopvirtualization
azure-storage-blob     # state.py — subscription + hostmap records (Section 3)
azure-storage-queue
azure-keyvault-secrets  # token_vault.py — registration-token cache (Section 3/7)
requests                # graph_client.py — plain REST against Graph, no msgraph-sdk abstraction
```

## 9. Code structure — keep business logic invokable outside the triggers

The three Azure Functions (Section 4–6) should be **thin adapters only** — parsing the trigger payload, then calling into a plain importable package that has no dependency on `azure.functions` or any trigger context. That package is what a local CLI wrapper calls directly, so the exact same code path that runs in production can be exercised ad hoc from a terminal, without going through Graph, the queue, or a deployed Function App.

### Suggested layout

```
/avdprovisioning/            ← plain importable package, no trigger-framework imports
    config.py                ← loads settings (subscription id, RG, host pool id, image ref, etc.)
                                 from environment variables — same variable names whether
                                 sourced from local.settings.json (Functions host) or a
                                 local .env / exported shell vars (CLI)
    graph_client.py            shared Graph HTTP/token plumbing (DefaultAzureCredential-based)
    graph_subscriptions.py      resolve_group_id, create/list/delete change-notification subscriptions
    graph_notifications.py      extract_member_changes(notification_body), resolve_user_upn(member_id)
    provisioning.py            provision_session_host(user, config) -> result
    subscriptions.py            renew_or_recreate_subscription(config) -> result
    state.py                   StateStore — Blob-backed subscription.json + hostmap/*.json (Section 3)

/function_app.py              ← the actual Functions triggers; each one is a few lines:
                                 parse input → call into avdprovisioning → return/ack

/local_cli.py                 ← argparse/click entry point for ad-hoc use, see below
```

### `local_cli.py` — ad-hoc invocation without the trigger plumbing

A small CLI (`argparse` today, in `local_cli.py`) exposes each unit of work directly. Commands that actually exist, as of this writing:

```
python local_cli.py provision --upn jane@contoso.com
python local_cli.py provision --upn jane@contoso.com --dry-run   # log intended calls, make none

python local_cli.py subscribe-group-changes            # create the Graph subscription, persist it to Blob state
python local_cli.py list-subscriptions
python local_cli.py unsubscribe --id <subscription-id>

python local_cli.py handle-notification --file sample-added-user-event-request-body.txt
python local_cli.py handle-notification --file sample-added-user-event-request-body.txt --execute

python local_cli.py show-subscription-state
python local_cli.py show-hostmap-state --user <object-id>
```

`handle-notification` was standing in for the full `notification_listener` → queue → `group_change_processor` chain before those Functions existed; now that they're deployed (Sections 4-6), it's still useful for replaying a captured notification payload file locally without waiting on a real group membership change or queue delivery: it validates `clientState` (auto-loaded from Blob state if `--client-state` isn't passed), resolves each added member's UPN, and calls `provision_session_host` (dry-run by default; `--execute` to actually provision).

This lets you test, e.g., "does provisioning actually work for one user" without waiting for a real group membership change, a Graph notification, and queue delivery — you just run the CLI against your dev/test subscription directly.

**Auth for local runs:** use `DefaultAzureCredential` in `config.py` regardless of caller — in the deployed Function App it resolves to the managed identity; on your laptop it falls back to your own `az login` session (or a service principal via env vars), so no code branching is needed for "local vs deployed" auth. Blob state access needs the **Storage Blob Data Contributor** role on the storage account (Section 3) — an ARM control-plane role like Owner does not imply this data-plane role.

**State storage for local runs:** points at the real `avdperuserstate` storage account by default (`AVD_STATE_STORAGE_ACCOUNT_URL` in `.env`). For a fully offline alternative, run [Azurite](https://learn.microsoft.com/en-us/azure/storage/common/storage-use-azurite) locally and point `AVD_STATE_STORAGE_ACCOUNT_URL` at its blob endpoint instead.

**`--dry-run` on `provision`:** worth building in from day one — log every Azure/Graph call that *would* be made (VM name, image reference, extension commands, assignment call) without executing them. This is the fastest way to sanity-check the naming convention, image reference, and assignment logic (Section 12 open questions) before spending real compute cost or waiting on VM boot time.

## 10. Hosting plan consideration

The Function App only ever calls **management-plane APIs** (ARM, Graph) — it does not need to reach *into* the VMs' private network itself (that's what the Run Command/extension does, executed by the platform against the VM). So **VNet integration is not required** for this workload, and Consumption or Flex Consumption plan is viable. Consider Premium only if:
- You want pre-warmed instances to avoid cold-start risk on the validation handshake, or
- You end up wanting the Function to reach something private (e.g. an internal API) later.

## 11. Observability & error handling

- Application Insights on the Function App — trace each provisioning attempt end-to-end with a correlation id (e.g. the user's object id) so a single user's journey is traceable across all three functions.
- Alert on: queue message processing failures/dead-letter, subscription renewal failures, provisioning attempts stuck in `provisioning` state past a timeout.
- Queue-triggered function should use the built-in poison-message handling (default 5 dequeue attempts) rather than custom retry logic — let the platform do it.

## 13. Deployment (built 2026-07-27)

**Function App:** `ktk-avd-per-user-automation` in the `avd-per-user-automation` RG — Linux, **Flex Consumption** plan, Python 3.14 runtime. This is a newer plan type than classic Consumption; a few things about it that weren't obvious going in:
- **No Kudu/SCM basic-auth publishing.** `az functionapp deployment list-publishing-credentials` and the classic `az functionapp deployment source config-zip` path are not properly supported — a zip pushed that way "succeeds" (202) but trigger-sync silently fails and the host never starts ("Function host is not running"). `az functionapp deploy` (the newer unified command) also 415'd in this CLI version (2.74.0). **What actually works:** Azure Functions Core Tools — `func azure functionapp publish <app-name> --python` — which performs a proper remote (Oryx) build against the app's configured runtime version and syncs triggers correctly. This is also what GitHub Actions uses under the hood (`Azure/functions-action@v1`).
- **Python 3.14 + remote build prints a scary-looking warning** ("Remote build for Python 3.14 is not yet supported for Flex") but the build completed and installed `requirements.txt` correctly regardless — don't take that warning as a hard failure without checking `/admin/host/status` first.
- **Python v2 programming model queue output binding gotcha:** annotate the parameter as `func.Out[str]` (singular), not `func.Out[list[str]]` — the latter throws `FunctionLoadError: ... invalid non-type annotation` at host startup on the Python 3.14 worker. Sending multiple queue messages from one invocation is still done by calling `.set([...])` with a list at runtime; the annotation itself must stay singular.

**Identity/permissions:** see Section 7 — system-assigned managed identity, no separate app registration, Key Vault added 2026-08-06 for the registration-token cache.

**CI/CD:** `.github/workflows/deploy.yml` — triggers on push to `main` touching `avdprovisioning/**`, `function_app.py`, `host.json`, or `requirements.txt` (plus manual `workflow_dispatch`). Authenticates to Azure via **OIDC** (`azure/login@v2`, no stored client secret): a dedicated app registration (`github-deploy-ktk-avd-per-user-automation`, app id `REDACTED-CLIENT-ID`) has a federated credential trusting `repo:KonTheCat/personal-avd-automation:ref:refs/heads/main`, and its service principal holds **Website Contributor** scoped only to this one Function App resource (not the whole RG) — enough to deploy code, nothing else.

Two gotchas hit and fixed after the initial build:
- **Federated credential subject format:** if the GitHub org/repo has ever been renamed, GitHub's OIDC token embeds immutable numeric IDs in the `sub` claim (`repo:OWNER@ownerId/REPO@repoId:ref:...`) instead of the plain-name form — `az login` fails with `AADSTS700213` until the federated credential's `subject` is updated to match exactly what the token presents (check the error message; it states the subject GitHub actually sent).
- **Deploy step must use Azure Functions Core Tools, not `Azure/functions-action@v1`:** the latter's zip push skips the remote (Oryx) build on this app's Flex Consumption plan — the deploy reports success but `requirements.txt` never gets installed server-side, and the host crashes on first import with `ModuleNotFoundError`. The workflow installs `azure-functions-core-tools@4` via npm and runs `func azure functionapp publish <app> --python`, reusing the `az login` session `azure/login@v2` already established — this is the one path confirmed (via manual testing, Section 13 above) to trigger a real remote build.

**GitHub repo secrets required** (not yet set — `gh` CLI wasn't available in this environment to set them programmatically; add these in the repo's Settings → Secrets and variables → Actions):
- `AZURE_CLIENT_ID` = `REDACTED-CLIENT-ID`
- `AZURE_TENANT_ID` = `REDACTED-TENANT-ID`
- `AZURE_SUBSCRIPTION_ID` = `REDACTED-AZURE-SUBSCRIPTION-ID`

None of these are secret-*material* in the OIDC sense (no client secret exists at all) but they're conventionally stored as Actions secrets alongside `azure/login` examples.

**Storage Queue:** `group-membership-changes` in the `avdperuserautomatiob8aa` storage account (the Function App's own `AzureWebJobsStorage` account — no separate account, no extra RBAC needed since the connection string app setting already grants full access). Poison messages land in the platform-managed `group-membership-changes-poison` queue after 5 failed dequeue attempts.

**Logic App retirement:** the `avd-per-user-group-chage-event-target` Logic App (the concept-proving stand-in for `notification_listener`) is slated for deletion once the real pipeline is confirmed working end-to-end against a real group membership change.

## 12. Open questions before implementation starts

1. **VM sizing/SKU** — fixed per policy, or does the group carry metadata (extension attribute, separate group tiers) indicating size?
2. **Naming convention** for VMs/NICs — e.g. `avd-<sanitized-upn>` vs `avd-<object-id-prefix>`. Needs to be deterministic and collision-free.
3. **Disk type/size, region, resource group** for new session hosts — same RG as existing host pool resources, or a dedicated one for easier cost tracking/cleanup?
4. **Capacity/cost guardrail** — any cap on how many personal desktops this will auto-create, or is group membership the only gate? Worth a sanity check/alert if the group grows unexpectedly fast.
5. **lifecycleNotificationUrl** — include it now (Section 4) or skip for v1 and rely solely on the daily renewer?
