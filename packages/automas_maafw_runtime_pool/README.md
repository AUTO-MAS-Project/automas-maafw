# automas-maafw-runtime-pool

Shared, selector-addressed Python environments for MaaFW runner workers.

The plugin instance exposes a `Root` setting. Leaving it empty preserves the
legacy `config/maafw_runtime_pool` location under the AUTO-MAS working
directory. The path is fixed for the service lifetime and configuration changes
take effect after restart. Every pool has a persistent UUID in
`.auto_mas_maafw_runtime_pool.json`; legacy version-1 markers are upgraded in
place without moving runtimes. Empty roots and the known markerless default
layout can be initialized, while unknown non-empty directories, invalid marker
kinds, symlinks and Windows reparse-point chains are rejected. `storage_info()`
returns the resolved root, `poolId`, default-root flag and JSON-friendly
`rootIdentity`.

The plugin provides the JSON-friendly `maafw.runtime_pool.v1` service. Runtime
Pool 0.2.0 accepts an optional `python` request with a CPython constraint and
supports the CP312 and CP313 minor families. It resolves a configured, host, or
pool-local uv-managed interpreter for that ABI; `resolve_runtime()` never
downloads one, while `ensure_runtime()` may prepare the missing pool-local
interpreter. One interpreter family can seed multiple environments, but it is
not itself a shared `site-packages` directory.

Runtime identity is derived from the complete canonical requirement set plus
the selected Python ABI, probed patch version, platform and architecture for
explicit multi-ABI requests. Projects with the same full identity reuse one
environment, while a different dependency selector or Python interpreter
identity receives a different venv. Exact constraints such as `==3.13.14` are
looked up and installed as that exact uv-managed patch. Host Python uses the
same full probed patch identity, so it can reuse an explicit route only when the
physical interpreter and complete requirement selector are actually identical.

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
