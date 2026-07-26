# automas-maafw-project-store

Versioned, resource-only MaaFW project storage for AUTO-MAS.

The plugin provides the JSON-friendly `maafw.project_store.v1` service. Each
import creates an immutable project version, keeps the ProjectInterface,
imported fragments, complete MaaFW resource paths and agent-side dependencies,
and deliberately omits known UI shells, embedded runtimes, caches and updater
payloads.

Consumers resolve a project through `resolve_project(project_id, version)`.
The returned `dataPath` always points at a directory containing
`interface.json` or `interface.jsonc`; its private
`.auto_mas_maafw_project.json` manifest exposes the runtime constraint without
requiring host-specific models.

The service also exposes `import_project`, `update_project`, `list_projects`,
`list_versions`, `switch_version`, `delete_version`, `collect_garbage`,
`bind_runtime`, `release_runtime`, `set_references`, `acquire_lease`, and
`release_lease`. Bindings are routing metadata; current pointers, pins,
reconciled references, and unexpired project leases are the deletion guards.
