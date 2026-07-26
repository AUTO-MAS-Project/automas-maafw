# automas-script-maafw-managed

Declarative AUTO-MAS script adapter for resource-only, versioned MaaFW
projects. It registers `MaaFWManaged` through the host's generic schema editor,
without a project-specific Vue page.

The adapter resolves an immutable Project Store version, routes it to a shared
runtime selector, persists the exact runtime binding, and delegates execution
to the existing MaaFW runner. Its declarative actions cover import, update,
version listing/switching, project/runtime deletion, pinning and garbage
collection.

Durable `maafw-script:*` and `maafw-project:*` references plus active leases
protect configured or running versions from deletion. Missing bound runtimes
are rebuilt from the recorded exact `maafwVersion`. Shared Python Agent routing
is opt-in and requires the Project Store manifest to prove a complete flat
dependency declaration.
