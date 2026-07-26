# automas-maafw-agent-env

MaaFW agent command planning and Python environment preparation service.

It provides `maafw.agent_env.v1`. The package does not import `maa`, does not
start agents, and can be installed without the MaaFW runner package.

When a bundled `python/python.exe` has been removed from a resource-only
project, the planner selects `isolated_venv` if `child_args` names an existing
Python entry point inside the project (including `agent/bootstrap.py`). Paths
that escape the project remain external and are never treated as managed
Python entries.
