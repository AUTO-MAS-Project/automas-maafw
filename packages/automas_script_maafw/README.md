# automas-script-maafw

MaaFW script adapter plugin for AUTO-MAS.

It registers `ScriptType=MaaFW` through the script adapter registry and stores
new MaaFW scripts in `PluginScriptConfig`. The independently released M9A
plugin owns its user-visible `ScriptType=M9A` entry and project pack while
reusing this adapter's shared editor and runtime hooks.

Ordinary external project directories use one process-wide reservation shared
by run-before-update and worker execution. A second script pointing at the same
directory fails fast before reading or mutating it, so an in-place project
upgrade cannot overlap another MaaFW run. Interface loading and archive
application are dispatched off the AUTO-MAS event loop.

The setup wizard and add-user flow can reuse an explicitly selected native
MaaFW configuration. The generic importer recognizes legacy MFAAvalonia
instance files, the `multi_config.json + configs/*.json` layout shared by
MFAAvalonia and MFW/CFA, plus MXU instance files. It maps controller/resource,
game and ADB hints, and converts task options against the active
ProjectInterface. It never modifies the source directory. Plans are previewed,
fingerprinted and CAS-checked before the host creates the first/new user;
internal user copies exclude runtime state and resource journals. Project packs
may override source discovery and plan generation through JSON-only methods.

Script MaaFW 0.1.13 exposes ordinary project update, environment preparation,
progress, and configuration-reuse transport through the generic plugin gateway.
It requires the active `maafw.runtime_pool.v1` service and
routes its resolved `Root`/`poolId` into both environment preparation and the
real worker run. Managed execution additionally consumes the prevalidated
Store/runtime route (complete selector, binding and Python constraint), so the
environment promised by the wizard is the environment later leased by the
script instead of a second host-default pool. Its release minimums are Agent
Env 0.1.4, Runner 0.4.0 and Runtime Pool 0.2.0.
