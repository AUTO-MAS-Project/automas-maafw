# automas-maafw-runner

Isolated MaaFW runner service for AUTO-MAS.

It provides `maafw.runner.v1`. The service builds run plans in the host process
and runs MaaFW through a worker subprocess so importing the service does not load
`maa` into the AUTO-MAS main process.

The wheel includes the MaaFW runtime worker code. `maa` is imported only by the
worker subprocess entrypoint, not by `MaaFWRunnerService`.

Runner environments are now selected from `maafw.runtime_pool.v1` by a
canonical requirement selector rather than by project path. The returned
environment holds a lease for the worker lifetime; callers must invoke
`MaaFWRunnerService.release_environment()` in their worker cleanup path.
Runtime Pool keeps selector venvs isolated while uv reuses a pool-local package
cache with hardlinks. Runner 0.3.3 requires Runtime Pool 0.1.4+, which records
installer/cache/link metadata, never composes environments through
`PYTHONPATH`, and prunes the pool-local uv cache after collecting stale
runtimes. Successful reclaim statistics and explicit unavailable/unsafe/error
states are written to the runner log.

Managed projects may declare `runtime.constraint`, `runtime.binding`, and
`nativePluginPaths` in `.auto_mas_maafw_project.json`. An isolated Python Agent
reuses the worker interpreter only when the manifest explicitly sets
`runtime.sharedAgentDependenciesComplete` to `true`. Legacy, incomplete,
binary, and external Agents keep their previous routing behavior.

Runner 0.3.0 also implements ProjectInterface v2.8 hotkey substitution for
AUTO-MAS Direct:

- saved values stay readable (`E`, `Ctrl+A`, `Ctrl+Shift+A`);
- `{Name}` and `{Name}.primary` become one integer virtual key code;
- `{Name}.modifier1` and `{Name}.modifier2` follow the saved modifier order;
- Win32 uses Windows Virtual-Key codes and Adb uses Android `KeyEvent` codes;
- unsupported controller types, unknown keys and missing referenced modifiers
  fail the run-plan build with an actionable error.
