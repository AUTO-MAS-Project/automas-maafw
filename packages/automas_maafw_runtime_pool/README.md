# automas-maafw-runtime-pool

Shared, selector-addressed Python environments for MaaFW runner workers.

The plugin provides the JSON-friendly `maafw.runtime_pool.v1` service. Runtime
identity is a selector derived from the canonical requirement set and current
Python ABI/platform/architecture. Projects with the same selector reuse one
environment while incompatible MaaFW selectors remain isolated.

Each complete canonical requirement set still receives its own isolated venv.
When `uv` is available, Runtime Pool creates a pool-local `cache/uv` and uses
`uv pip install --link-mode hardlink` for every environment. Different runtime
selectors can therefore reuse downloaded wheels and unpacked package files
without sharing `site-packages` or composing `PYTHONPATH` across environments.
The runtime manifest exposes the selected installer, cache scope/path and link
mode under `installerMetadata`.

If `uv` is unavailable, a full Python distribution that provides both `venv`
and `ensurepip` retains the legacy stdlib-venv + pip path. Embeddable Python
without those modules requires `uv`; Runtime Pool does not create a mixed
`PYTHONPATH` environment as a fallback.

The selector is not a resolved dependency lock: version ranges remain ranges
in the identity. Production installation records `pip freeze --all` as
`resolvedRequirements` in the runtime manifest for audit. uv-backed installs
use `uv pip freeze`; the legacy pip path uses `pip freeze --all`. Recreating a
deleted runtime can resolve newer packages still allowed by the same selector.

Project agents and project resources are intentionally outside this pool.

The service exposes `list_runtimes`, `resolve_runtime`, `ensure_runtime`,
`touch`, `pin`, `delete`, and `collect_garbage`. Durable project ownership is
reconciled with `set_references(runtime_id, references)`; active runner
processes use expiring leases instead. GC is dry-run by default and refuses to
delete pinned, referenced, or leased runtimes.

Every GC response also contains `cachePrune`. A dry run records the pool-local
uv cache size, file count, uv version, and the exact `uv cache prune` command
without invoking it. A real collection first removes eligible runtime
environments and then delegates dangling-entry cleanup to uv itself. The
result records before/after statistics, reclaimed bytes/files, exit status,
and uv output. Runtime Pool never substitutes a recursive directory delete for
uv cache maintenance; a missing uv executable, a symlinked cache path, or a
failed command is returned as an explicit `unavailable`, `unsafe`, or `error`
status.

Local paths, editable installs, requirement includes, and local archives are
rejected because they cannot safely form a cross-project identity. Callers can
inject an installer for offline mirrors and deterministic tests.
