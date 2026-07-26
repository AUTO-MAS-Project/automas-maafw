# AUTO-MAS MaaFW Plugins

AUTO-MAS 的通用 MaaFW 插件工作区。仓库统一维护共享运行栈，目录内各项目仍作为独立 Python distribution 构建和发布。

## Packages

- `automas-maafw-interface`
- `automas-maafw-agent-env`
- `automas-maafw-controller-adb`
- `automas-maafw-controller-win32`
- `automas-maafw-project-store`
- `automas-maafw-project-update`
- `automas-maafw-runtime-pool`
- `automas-maafw-runner`
- `automas-script-maafw`
- `automas-script-maafw-managed`

`automas-script-maafw-managed` 提供宿主无专用前端登记的
`MaaFWManaged` 脚本类型。项目发行包会被投影为版本化的脚本资源 Bundle，
不保留第三方 UI、内置 MaaFW runtime、嵌入式 Python 和可重建缓存；执行时再按
项目依赖清单路由到共享的 MaaFW runtime 环境。原有 `MaaFW` 脚本类型仍可用于
直接运行用户自行维护的完整项目目录。

M9A project pack 与 `automas-m9a` 聚合包位于独立的 `automas-m9a` 仓库。

兼容性审计见
[`docs/pi-2.8.1-maafw-5.12-compatibility.md`](docs/pi-2.8.1-maafw-5.12-compatibility.md)，
其中区分了已实现的 PI v2.8.1 能力、MaaFW 5.12.2 宿主升级门禁和仅作为
黑盒样本的 `reference` 项目。

## Development

```powershell
uv sync --all-packages --group dev
uv run --all-packages python -m unittest discover -s tests
```
