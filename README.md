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

`automas-script-maafw-managed` 在内部保留不可直接创建的 `MaaFWManaged`
脚本类型，并复用普通 MaaFW 的 Vue 编辑器。用户只从一个 MaaFW 入口创建项目，
再按需把现有普通项目原地转换为托管项目。项目发行包会被投影为版本化的脚本资源 Bundle，
不保留第三方 UI、内置 MaaFW runtime、嵌入式 Python 和可重建缓存；执行时再按
项目依赖清单路由到共享的 MaaFW runtime 环境。原有 `MaaFW` 脚本类型仍可用于
直接运行用户自行维护的完整项目目录。

M9A project pack 与 `automas-m9a` 聚合包位于独立的 `automas-m9a` 仓库。

## Release

`publish.yml` 每次只发布一个 distribution，不会自动处理包间顺序。当前版本应按
以下层级发布；同层可并行，下一层须等待依赖版本已经可从 PyPI 安装：

1. `automas-maafw-interface` 0.2.0、`automas-maafw-project-store` 0.2.1、
   `automas-maafw-runtime-pool` 0.1.5。
2. `automas-maafw-agent-env` 0.1.3、`automas-maafw-project-update` 0.2.2。
3. `automas-maafw-runner` 0.3.4。
4. `automas-script-maafw` 0.1.10。
5. `automas-script-maafw-managed` 0.2.1。

不要以相同版本重新发布已经存在的 controller 包。首次发布 Project Store、
Runtime Pool 和 Managed 前，必须创建 `pypi-project-store`、
`pypi-runtime-pool`、`pypi-script-maafw-managed` GitHub Environments，并在
PyPI 为相同包名配置与 repo、`publish.yml` 和 environment 精确匹配的 pending
trusted publisher。

普通 MaaFW 脚本 0.1.10 的配置导入/用户复制与 Managed 0.2.1 都要求目标
AUTO-MAS `dev_v2` 已提供
`Config.script_config_transaction()`、`Config.script_config_write_scope()` 和
ScriptConfigStore 的 `write_transaction()`。Managed 原地转换还要求宿主提供
`Config.get_plugin_script_type_conversion_snapshot()` 和
`Config.convert_plugin_script_type()`；宿主事务与原子换型改动合入前不得发布或
启用这两个版本的新事务能力。M9A 的最低依赖必须对齐
`automas-maafw-runner>=0.3.4`、`automas-script-maafw>=0.1.10`，不要因本轮功能
把普通脚本包错误提升到 0.2.0。

兼容性审计见
[`docs/pi-2.8.1-maafw-5.12-compatibility.md`](docs/pi-2.8.1-maafw-5.12-compatibility.md)，
其中区分了已实现的 PI v2.8.1 能力、MaaFW 5.12.2 宿主升级门禁和仅作为
黑盒样本的 `reference` 项目。

## Development

```powershell
uv sync --all-packages --group dev
uv run --all-packages python -m unittest discover -s tests
```
