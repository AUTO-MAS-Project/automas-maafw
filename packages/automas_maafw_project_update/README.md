# automas-maafw-project-update

Standalone MaaFW project update service and AUTO-MAS plugin.

It provides `maafw.project_update.v1` and supports `mirrorchyan` and
`github_release` providers without depending on MaaFW runner or agent runtime.

`download_package()` downloads an installable candidate into a caller-owned
managed directory without changing an existing project tree. It accepts only
HTTPS URLs, validates every redirect, enforces a streamed size limit, checks
ZIP/SHA256 integrity and publishes the complete archive atomically. Managed
downloads use unique temporary files and a content-addressed final name, so
concurrent scripts cannot delete each other's in-progress archive. Project
Store consumers can then import the returned local path.

`discover_update()` distinguishes a newer version from an installable package.
It returns a discovery with `candidate=None` when the provider supplies version
metadata without a download URL. The legacy `check_update()` method returns only
an actionable candidate and raises a diagnostic error for a non-installable
discovery, so callers cannot accidentally present it as ready to install.

Archive validation (ZIP inspection and SHA256), publication, extraction,
copy/rollback and cleanup run in worker threads. The async service therefore
keeps the AUTO-MAS event loop responsive while applying large project updates.
