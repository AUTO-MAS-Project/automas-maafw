# automas-maafw-project-store

Versioned, resource-only MaaFW project storage for AUTO-MAS.

The plugin provides the JSON-friendly `maafw.project_store.v1` service. It
accepts either an unpacked release directory or a ZIP release. Each import
creates an immutable project version, keeps the ProjectInterface,
imported fragments, complete MaaFW resource paths and agent-side dependencies,
and deliberately omits known UI shells, embedded runtimes, caches and updater
payloads.

ZIP files are extracted only into this store's private `.staging` directory.
Absolute and parent paths, case-colliding entries, links, devices and other
special files are rejected. Entry count, per-file size, total expanded size and
compression ratio are bounded, and actual bytes are checked while extracting.
Archives may contain the project at their root or inside one unambiguous direct
wrapper directory.

The `version` argument is optional when ProjectInterface declares `version`.
When both are present they must match; a single leading `v` is treated as
equivalent (for example, `1.2.3` and `v1.2.3`). Directory or archive names are
never used to infer a version.

Consumers resolve a project through `resolve_project(project_id, version)`.
The returned `dataPath` always points at a directory containing
`interface.json` or `interface.jsonc`; its private
`.auto_mas_maafw_project.json` manifest exposes the runtime constraint without
requiring host-specific models.

Resolved records and list operations expose a compact `summary` containing
ProjectInterface capabilities, agent routing, stripped shell families, source
and projected sizes, ABI requirements and warning counts. If a bundled Python
interpreter is stripped, the projected ProjectInterface routes that agent
through `python` and the manifest records the `managed-python` route instead of
leaving a broken path to the removed executable.

The service also exposes `import_project`, `update_project`, `list_projects`,
`list_versions`, `switch_version`, `delete_version`, `collect_garbage`,
`bind_runtime`, `release_runtime`, `set_references`, `acquire_lease`, and
`release_lease`. Its async `resource_lifecycle_transaction()` coordinates
multi-call reference reconciliation, binding changes and destructive GC across
HTTP actions and script hooks. Bindings are routing metadata; current pointers,
pins, reconciled references, and unexpired project leases are the deletion
guards.

`resource_lifecycle_transaction()` is an in-process Python coordination surface,
not a JSON-returning service action or a cross-process file lock. Calls using
the same service instance and `asyncio.Task` may re-enter it; independent tasks
wait, while a child task that inherited the owner's context is rejected instead
of deadlocking. The transaction does not replace reference, pin or lease guards,
and the store's internal `RLock` still protects only each synchronous method
call.
