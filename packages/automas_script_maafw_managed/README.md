# automas-script-maafw-managed

Declarative AUTO-MAS script adapter for resource-only, versioned MaaFW
projects. It registers `MaaFWManaged` through the host's generic schema editor,
without a project-specific Vue page.

The adapter resolves an immutable Project Store version, routes it to a shared
runtime selector, persists the exact runtime binding, and delegates execution
to the existing MaaFW runner. Its declarative actions accept a local project
folder or ZIP for import/upgrade, or discover and download an installable
MirrorChyan/GitHub Release candidate through `maafw.project_update.v1`.
Remote archives are validated without overwriting the active project, then
passed to Project Store through the same immutable import and confirmation
workflow. Actions also cover capability inspection, version listing/switching,
project/runtime deletion, pinning and garbage collection.

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
the ScriptConfigStore `write_transaction()` API. Do not enable or publish
Managed 0.2.0 for a host before that host transaction change is merged.
