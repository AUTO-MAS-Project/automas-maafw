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
