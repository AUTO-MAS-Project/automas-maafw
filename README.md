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

共享运行栈按“每个 Python ABI 解释器族 + 每个完整规范化依赖 selector 的隔离
venv”组织：Runtime Pool 0.2.0 可路由 CP312/CP313，多个 selector 共用 pool 内 uv
缓存与基础解释器，但不共用 `site-packages`。Project Store 0.2.3 把脱壳前的
`runtime.python` 约束留在不可变 manifest；Runner 0.4.0 校验并复用该可信绑定，
准备页与实际运行不会在宿主 CP312 下重新计算 CP313 runtime 身份。

M9A project pack 与 `automas-m9a` 聚合包位于独立的 `automas-m9a` 仓库。

宿主接入、插件矩阵、普通 MaaFW/Managed 边界和当前 UI 暂停策略见
[`docs/plugin-integration.md`](docs/plugin-integration.md)；完整服务方法与稳定 DTO
见 [`docs/service-contracts.md`](docs/service-contracts.md)。

## Release

`publish.yml` 每次只发布一个 distribution，不会自动处理包间顺序。当前版本应按
以下层级发布；同层可并行，下一层须等待依赖版本已经可从 PyPI 安装：

1. `automas-maafw-interface` 0.2.0、`automas-maafw-project-store` 0.2.3、
   `automas-maafw-runtime-pool` 0.2.0。
2. `automas-maafw-agent-env` 0.1.4、`automas-maafw-project-update` 0.2.3。
3. `automas-maafw-runner` 0.4.0。
4. `automas-script-maafw` 0.1.12。
5. `automas-script-maafw-managed` 0.3.2。
6. 在独立 M9A 仓库依次发布 `automas-script-maafw-pack-m9a` 0.1.5 与
   `automas-m9a` 0.1.5。

不要以相同版本重新发布已经存在的 controller 包。首次发布 Project Store、
Runtime Pool 和 Managed 前，必须创建 `pypi-project-store`、
`pypi-runtime-pool`、`pypi-script-maafw-managed` GitHub Environments，并在
PyPI 为相同包名配置与 repo、`publish.yml` 和 environment 精确匹配的 pending
trusted publisher。

普通 MaaFW 脚本 0.1.12 的配置导入/用户复制与 Managed 0.3.2 都要求目标
AUTO-MAS `dev_v2` 已提供
`Config.script_config_transaction()`、`Config.script_config_write_scope()` 和
ScriptConfigStore 的 `write_transaction()`。Managed 原地转换还要求宿主提供
`Config.get_plugin_script_type_conversion_snapshot()` 和
`Config.convert_plugin_script_type()`；宿主事务与原子换型改动合入前不得发布或
启用这两个版本的新事务能力。M9A 的最低依赖必须对齐
`automas-maafw-runner>=0.4.0`、`automas-script-maafw>=0.1.11`，不要因本轮功能
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
