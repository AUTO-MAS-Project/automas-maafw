# automas-script-maafw

MaaFW script adapter plugin for AUTO-MAS.

It registers `ScriptType=MaaFW` through the script adapter registry and stores
new MaaFW scripts in `PluginScriptConfig`. M9A is provided by the
`automas-script-maafw-pack-m9a` project pack, and that pack registers the
user-visible `ScriptType=M9A` entry while reusing this adapter's runtime hooks.

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
