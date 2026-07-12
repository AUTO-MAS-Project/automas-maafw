# AUTO-MAS MaaFW Plugins

AUTO-MAS 的通用 MaaFW 插件工作区。仓库统一维护共享运行栈，目录内各项目仍作为独立 Python distribution 构建和发布。

## Packages

- `automas-maafw-interface`
- `automas-maafw-agent-env`
- `automas-maafw-controller-adb`
- `automas-maafw-controller-win32`
- `automas-maafw-project-update`
- `automas-maafw-runner`
- `automas-script-maafw`

M9A project pack 与 `automas-m9a` 聚合包位于独立的 `automas-m9a` 仓库。

## Development

```powershell
uv sync --all-packages --group dev
uv run --all-packages python -m unittest discover -s tests
```

