# automas-script-maafw-managed

AUTO-MAS script adapter for resource-only, versioned MaaFW projects. The
internal `MaaFWManaged` provider is conversion-only: it is hidden from normal
script creation and reuses the ordinary MaaFW Vue editor. Users keep one MaaFW
entry and opt into managed resources from that project's wizard or resource
manager.

`GET`/`POST /plugin/maafw-managed/capabilities` is script-independent and
reports the plugin API/distribution versions plus host-gated feature flags.
`POST /plugin/maafw-managed/convert` converts an existing ordinary `MaaFW`
record in place. It reads the authoritative `Info.Path`, imports that directory
into Project Store, and binds the stable `maafw-script:<scriptId>` reference.
The script UUID, every user UUID/order/config and user run history are retained;
the action never adds, copies or deletes a script record. Conversion imports
the immutable version with `activate=false`, so either a failed CAS or a
successful per-script binding leaves Project Store's global current untouched.
It also holds the ordinary MaaFW process-wide source-path reservation across
inspection, import, commit and compensation, and fails fast while that path is
running, preparing or updating.

The adapter resolves an immutable Project Store version, routes it to a shared
runtime selector, materializes a per-script writable checkout under the
configured RunRoot, persists the Store/runtime identities, and delegates
execution to the existing MaaFW runner. The immutable Store payload is never a
runtime working directory. Its declarative actions accept a local project
folder or ZIP for import/upgrade, or discover and download an installable
MirrorChyan/GitHub Release candidate through `maafw.project_update.v1`.
Remote archives are validated without overwriting the active project, then
passed to Project Store through the same immutable import and confirmation
workflow. Actions also cover capability inspection, version listing/switching,
project/runtime deletion, pinning and garbage collection.

The downloaded ZIP is request-scoped staging, not a durable project resource.
Once a download succeeds, Managed releases the exact content-addressed package
in a cancellation-shielded `finally` path even when Project Store import,
upgrade planning, or host config persistence fails. Apply, cancel and restart
recovery use only
the immutable Store version and never need the ZIP. `LastDownload` retains
source/version/size/SHA256 plus an explicit `retained` cleanup result, but no
local staging path; remote imports also never persist that transient path as
`Managed.SourceArchive`. A cleanup failure is warned and reported as
`retained=true` without changing a completed resource import into failure.
Cleanup telemetry is written back only after the resource and host config
transaction committed, so a failed import never publishes a successful
`LastDownload` record.
Locally selected ZIP paths keep their existing persistence behavior.

When `features.operationProgress=true`, every mutating manager action accepts a
fresh `progressId`. `POST /plugin/maafw-managed/progress` polls the bounded
in-memory state by the exact `scriptId` + `operationId` pair, while best-effort
`maafw.managed.progress` WebSocket events are published to both IDs. Remote
downloads forward the updater's real byte counts; Project Store and Runtime
Pool operations report only truthful transaction-stage boundaries. A terminal
`success` or `error` state is published once, after operation locks have exited.
Request cancellation is deferred until any worker-thread mutation and protected
inner operation (including commit/compensation) has really finished. Progress
then records that real result—so a mutation that completed remains `success`—
before `CancelledError` is re-raised to the caller.

`POST /plugin/maafw-managed/operations/active` is the authoritative same-script
operation lookup. It returns a server epoch and the currently registered
operation; all mutations and remote checks are excluded by `scriptId`, and
plugin shutdown drains registered work before releasing the active slot.
Browser session storage is only a compatibility hint for older hosts.

Managed 0.3.1 registers `maafw.managed.environment.v1` for host-side
preparation by `scriptId`. The service resolves the authoritative Store
version, reserves the writable checkout, and invokes
Runner's exact prewarm route without starting the project Agent, controller, or
game. Only after preparation succeeds does it commit a reversible Store/runtime
binding and persist the host script record. If that host write fails, it
restores the previous Project Store binding plus the exact Store/Runtime Pool
reference deltas before either transaction unlocks; incomplete compensation is
attached to the original failure and never reported ready. The later run
consumes the same complete selector, `poolId`, `runtimeId`,
and `runtime.python` constraint, so an existing matching CP312/CP313 runtime is
reused instead of rebuilt. Managed 0.3.1 requires Script MaaFW 0.1.11, Runner
0.4.0, Project Store 0.2.3 and Runtime Pool 0.2.0 or newer compatible releases.

`ImportProjectId` is an optional first-import alias and is cleared after a successful
bind. If omitted, Project Store resolves the identity from the ProjectInterface's
formal ID, `name`, or source directory name. The displayed `ProjectId` and `Version`
are read-only. Once bound,
execution, upgrade validation, runtime installation and reference
reconciliation use the immutable Project Store manifest identity rather than
editable form data.

`POST /plugin/maafw-managed/inventory` requires no script context and reports
the configured Store, RunRoot and Runtime Pool identities plus every project,
version, checkout, runtime, reference, pin and lease. Corrupt entries are
returned as explicit errors and make the snapshot incomplete. This endpoint is
read-only; project-version deletion still requires a selected Managed script and
a complete preflight inventory. Project Store `Root`/`RunRoot` and Runtime Pool `Root` are
configured on their plugin instances as absolute paths; changing them does not
migrate data, and a stored `StoreId` prevents accidental reuse of a same-name
project from another root.

`POST /plugin/maafw-managed/gc` accepts an optional `scriptId`. A non-empty value
keeps the selected Managed script context; a missing, empty, or whitespace value
selects global GC and does not require any surviving Managed script. `dryRun`
defaults to `true`; real collection requires `dryRun=false` and the exact
`confirmation: "DELETE UNUSED"`. Global and per-script GC are mutually exclusive
in both directions on the server. Both paths reconcile references under the shared
Project Store lifecycle transaction and host config write gate, while retaining
`refs`/pin/lease, complete-inventory, and fail-closed deletion guards.

Task, user and pack-owned configuration stays outside the immutable resource
tree. A local upgrade imports the candidate as an inactive version, then asks
the project pack to plan the script record and every user record. Managed
persists a durable `planId` journal: the script record contains a config-free
summary while each user record retains only its own full source and target
snapshot. Confirmation applies that exact stored plan after checking the
resource hashes, configuration hashes and user set. JSON object fields are
replaced atomically, the resource is activated only after every configuration
write succeeds, and interrupted transactions are restored on startup. Plan
errors, stale records or manual actions leave the old version active. Existing
installed versions use the same plan/apply path; there is no force-switch
bypass. Migration from a non-plugin installation remains an external-tool
concern.

Durable `maafw-script:*` and `maafw-project:*` references plus active leases
protect configured or running versions from deletion. Missing bound runtimes
are rebuilt from the recorded exact `maafwVersion`. Shared Python Agent routing
is opt-in and requires the Project Store manifest to prove a complete flat
dependency declaration. Project reference snapshots, reconciliation and GC run
inside the Project Store's shared lifecycle transaction, so a concurrent stage
or runtime bind cannot lose its new reference before the matching script config
is durable. Import, stage, apply, cancel, delete and runtime installation use
resource-lifecycle → per-script upgrade → host-config ordering; destructive GC
holds the host's global config write gate from snapshot through collection.
Managed 0.3.1 requires this Project Store 0.2.3 capability and fails closed
instead of falling back to an unlocked older service.

It also requires an AUTO-MAS host that provides
`Config.script_config_transaction()`, `Config.script_config_write_scope()` and
the ScriptConfigStore `write_transaction()` API. In-place conversion additionally
requires `Config.get_plugin_script_type_conversion_snapshot()` and
`Config.convert_plugin_script_type()`. Conversion first takes a short host
transaction to capture the source snapshot, releases the global config gate,
then enters Project Store lifecycle → per-script locking, rechecks that snapshot,
and imports. Only the final CAS commit takes a second short host transaction
while those locks are held. The host
performs one atomic replacement of the complete script record and protects an
exact target-storage recovery artifact, while a non-sensitive durable journal
records `project_imported` and `committed` states. The deterministic operation ID
includes the source snapshot plus target project/version/runtime identity, so an
exact retry reuses the artifact but a changed target cannot do so accidentally.
Failures known to be pre-commit release the project reference; uncertain commit
states retain the reference for idempotent recovery. Hosts without both
conversion methods report `inPlaceConversion=false` and the convert action fails
closed. Do not enable or publish Managed 0.3.1 before these host transaction and
conversion changes are merged.

Managed HTTP operations and automatic updates use the host's global update
source and MirrorChyan CDK as their only provider settings. Request and legacy
script fields are accepted for compatibility but cannot override them;
AutoSite/CNB must be changed to MirrorChyan or GitHub before a Managed remote
operation. Each project's stable/beta channel is written only through
`/maafw-managed/settings`. The first remote import records its non-sensitive
repository/RID/tag/asset identity in the immutable Project Store manifest so
later upgrades never fall back to request fields. Secret-bearing fields and
signed download URLs are redacted from HTTP responses, progress, logs and
persisted discovery/import payloads.
