# automas-maafw-runtime-pool

Shared, selector-addressed Python environments for MaaFW runner workers.

The plugin provides the JSON-friendly `maafw.runtime_pool.v1` service. Runtime
identity is a selector derived from the canonical requirement set and current
Python ABI/platform/architecture. Projects with the same selector reuse one
environment while incompatible MaaFW selectors remain isolated.

The selector is not a resolved dependency lock: version ranges remain ranges
in the identity. Production installation records `pip freeze --all` as
`resolvedRequirements` in the runtime manifest for audit. Recreating a deleted
runtime can resolve newer packages still allowed by the same selector.

Project agents and project resources are intentionally outside this pool.

The service exposes `list_runtimes`, `resolve_runtime`, `ensure_runtime`,
`touch`, `pin`, `delete`, and `collect_garbage`. Durable project ownership is
reconciled with `set_references(runtime_id, references)`; active runner
processes use expiring leases instead. GC is dry-run by default and refuses to
delete pinned, referenced, or leased runtimes.

Local paths, editable installs, requirement includes, and local archives are
rejected because they cannot safely form a cross-project identity. Callers can
inject an installer for offline mirrors and deterministic tests.
