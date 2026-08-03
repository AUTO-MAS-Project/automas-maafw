# automas-maafw-agent-env

MaaFW agent command planning and Python environment preparation service.

It provides `maafw.agent_env.v1`. The package does not import `maa`, does not
start agents, and can be installed without the MaaFW runner package.

When a bundled `python/python.exe` has been removed from a resource-only
project, the planner selects `isolated_venv` if `child_args` names an existing
Python entry point inside the project (including `agent/bootstrap.py`). Paths
that escape the project remain external and are never treated as managed
Python entries.

A project-owned Python runtime is release content: preparation only checks that
the interpreter starts and imports `maa.agent.agent_server.AgentServer`. It is
not required to contain pip or ensurepip and AUTO-MAS never mutates it. Only an
AUTO-MAS-owned `isolated_venv` is bootstrapped and dependency-managed. The
optional progress callback reports deterministic per-Agent completion.

Agent Env 0.1.4 also recognizes a Runner-provided `shared_runtime` plan. It
validates that the Runtime Pool interpreter exists without attempting to
bootstrap or mutate that shared environment. Compatibility shims are written
atomically and idempotently, so concurrent preparation cannot expose a partial
`sitecustomize.py`.
