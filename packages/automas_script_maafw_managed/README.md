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
runtime selector, persists the exact runtime binding, and delegates execution
to the existing MaaFW runner. Its declarative actions accept a local project
folder or ZIP for import/upgrade, or discover and download an installable
MirrorChyan/GitHub Release candidate through `maafw.project_update.v1`.
Remote archives are validated without overwriting the active project, then
passed to Project Store through the same immutable import and confirmation
workflow. Actions also cover capability inspection, version listing/switching,
project/runtime deletion, pinning and garbage collection.

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

`ImportProjectId` is a first-import input only and is cleared after a successful
bind. The displayed `ProjectId` and `Version` are read-only. Once bound,
execution, upgrade validation, runtime installation and reference
reconciliation use the immutable Project Store manifest identity rather than
editable form data.

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
Managed 0.2.0 requires this Project Store 0.2.0 capability and fails closed
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
closed. Do not enable or publish Managed 0.2.0 before these host transaction and
conversion changes are merged.
