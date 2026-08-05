# AUTO-MAS MaaFW 插件接入总览

> 适用仓库：`automas-maafw`（dev2 当前实现）
>
> 文档用途：给 AUTO-MAS 宿主、MaaFW project pack 和后续维护者说明包边界、服务入口和运行约束。
>
> 契约详情：[`service-contracts.md`](service-contracts.md)

## 1. 这组插件解决什么问题

这组包把 MaaFW 的 ProjectInterface 读取、控制器、Agent 环境、项目更新、运行时和
脚本适配拆成可独立加载的 AUTO-MAS 插件。每个 distribution 都通过
`auto_mas.plugins` entry point 注册，不应通过导入另一个插件的内部类来通信。

普通 MaaFW (`ScriptType=MaaFW`) 是用户可见、可直接运行外部项目目录的脚本入口；
用户配置、游戏/模拟器绑定和项目任务仍由宿主保存。`MaaFWManaged` 是
`automas-script-maafw-managed` 内部的资源型脚本类型，只用于在同一个 MaaFW 入口下
托管导入的不可变 Project Store 版本和共享 Runtime Pool 依赖。它不是第二个用户入口，
也不应直接暴露为普通脚本创建类型。

当前宿主分支暂时关闭脱壳/Managed 资源管理器的前端入口：普通 MaaFW 页面仍可用，
Managed 服务、类型和后端契约保留，待资源管理器重新设计完成后再恢复 UI。这一开关
不改变包的服务契约，也不表示 Managed 已从仓库移除。

## 2. Distribution 与服务矩阵

| distribution（版本） | entry point | 服务 | 责任 |
| --- | --- | --- | --- |
| `automas-maafw-interface` 0.2.0 | `automas_maafw_interface.plugin:Plugin` | `maafw.interface.v1` | 加载/校验 ProjectInterface，生成任务快照与执行 payload |
| `automas-maafw-agent-env` 0.1.4 | `automas_maafw_agent_env.plugin:Plugin` | `maafw.agent_env.v1` | 识别 Agent 类型，生成命令计划，准备项目 Python/隔离 venv |
| `automas-maafw-controller-adb` 0.1.1 | `automas_maafw_controller_adb.plugin:Plugin` | `maafw.controller.adb` | 注册 ADB controller provider |
| `automas-maafw-controller-win32` 0.1.2 | `automas_maafw_controller_win32.plugin:Plugin` | `maafw.controller.win32` | 注册 Win32 controller provider |
| `automas-maafw-project-store` 0.2.3 | `automas_maafw_project_store.plugin:Plugin` | `maafw.project_store.v1` | 本地目录/ZIP 导入、不可变版本、checkout、引用、盘点与安全 GC |
| `automas-maafw-project-update` 0.2.3 | `automas_maafw_project_update.plugin:Plugin` | `maafw.project_update.v1` | MirrorChyan/GitHub 版本发现、受限下载、校验和进度 |
| `automas-maafw-runtime-pool` 0.2.0 | `automas_maafw_runtime_pool.plugin:Plugin` | `maafw.runtime_pool.v1` | 按完整依赖 selector 与 Python ABI 隔离/复用运行环境 |
| `automas-maafw-runner` 0.4.0 | `automas_maafw_runner.plugin:Plugin` | `maafw.runner.v1` | 可信 runtime 路由、预热、worker 生命周期与结果模型 |
| `automas-script-maafw` 0.1.13 | `automas_script_maafw.plugin:Plugin` | `maafw.registry.v1`、`maafw.configuration_reuse.v1`、`maafw.api.v1` | 注册普通 MaaFW 适配器、配置导入/复制、项目更新/环境预热 transport、运行绑定 |
| `automas-script-maafw-managed` 0.3.2 | `automas_script_maafw_managed.plugin:Plugin` | `maafw.managed.environment.v1` | 内部 Managed 转换、资源升级/切换、运行绑定、进度与回收动作 |

M9A 的 project pack 和聚合包位于独立仓库；通用 MaaFW 包不得反向依赖 M9A、MaaEnd
或其他具体项目适配包。project pack 通过 `maafw.registry.v1` 注册自己的资源服务和
配置规划器。

## 3. 宿主加载与服务调用

宿主按服务名获取服务，服务未加载必须作为可诊断的能力缺失处理：

```python
service = PluginManager.service.get("maafw.interface.v1")
if service is None:
    raise RuntimeError("插件服务未加载: maafw.interface.v1")
interface = service.load(project_path)
```

跨插件边界只传 JSON 兼容 dict/list、字符串、数字、布尔值或明确稳定 DTO；不要把
另一个插件创建的 Pydantic 实例身份作为契约。服务方法的完整签名、输入字段、错误
和进度事件见 [`service-contracts.md`](service-contracts.md)。

最小普通运行链如下：

```text
ProjectInterface
  -> maafw.interface.v1
  -> maafw.registry.v1 / controller provider
  -> maafw.runtime_pool.v1（按完整 selector 解析 runtime）
  -> maafw.agent_env.v1（Agent 命令/环境）
  -> maafw.runner.v1（worker、controller、tasker）
```

准备页和实际运行必须调用 Runner 的同一套 selector/runtime 解析。准备成功后再次
运行应复用同一 runtime identity；不同 Python ABI、补丁版本或完整依赖 selector 不得
共用 `site-packages`，但可以共用 pool 内 uv cache/hardlink。

## 4. 普通 MaaFW 与 Managed 边界

### 普通 MaaFW

- `Info.Path` 指向用户维护的完整 MaaFW 项目目录。
- 目录内的 interface、resource、Agent/native plugin 和项目自带 Python 由普通适配器读取。
- 更新前、运行准备和 worker 对同一外部目录使用排他预留，避免更新与运行并发。
- 可通过 `maafw.configuration_reuse.v1` 预览外部 MFA/MFW/CFA/MXU 配置或复制已有用户；
  导入只读，不回写来源目录。

### Managed（内部类型）

- 转换只接受现有普通 MaaFW 脚本，保留 script/user UUID、顺序、配置和运行历史。
- Project Store 保存不可变 `projectId/version` 资源；活动运行使用隔离 checkout，
  不把 Store 数据目录直接当作可写运行目录。
- Runtime Pool 以 Store/manifest 的完整依赖和 `runtime.python` 为权威，Runner 复用
  与准备阶段相同的 `runtimeId/poolId`。
- 升级先导入 inactive 版本、生成 pack 计划和 CAS/journal，再由确认动作原子切换；
  失败应保留旧版本并回滚配置。
- 所有跨类型写操作（转换、删除、升级、绑定）必须由服务端按 `scriptId` 校验并
  获取排他操作槽；浏览器存储只能作为旧宿主兼容提示，不能作为安全边界。

## 5. 事务与宿主最低能力

发布或启用普通脚本 0.1.13 / Managed 0.3.2 前，宿主必须提供：

```text
Config.script_config_transaction()
Config.script_config_write_scope()
ScriptConfigStore.write_transaction()
```

原地转换还必须提供：

```text
Config.get_plugin_script_type_conversion_snapshot()
Config.convert_plugin_script_type()
```

新转换统一使用宿主通用 journal kind `plugin.script-type-conversion.v1`，目标快照
使用 `plugin.script-type-conversion.v1.target`。旧的
`maafw.managed-conversion[-target]` 仅用于恢复已经开始的本地事务，不再写入。

转换顺序是“短宿主快照事务 → Project Store 生命周期锁 → 脚本锁与 CAS 复核 →
导入资源 → 短宿主提交事务”。任何宿主写入失败都必须恢复 Project Store 引用和
运行时引用；无法确定提交状态时宁可保留引用等待恢复，不得报告为已完成。

## 6. 更新、依赖和发布顺序

Managed 的远程更新源由宿主全局配置统一决定，其请求字段仅为兼容保留，不能绕过
宿主的 MirrorChyan CDK/GitHub token。普通 MaaFW 仍使用脚本级更新配置，并按契约
继承宿主提供的全局凭据。首次远程导入后，来源身份写入不可变 manifest。无下载 URL
的版本发现不是可安装候选，下载有 300 秒墙钟上限并报告真实进度；失败不得把当前
版本写成目标版本。

发行顺序（同层可并行，下一层等待 PyPI 可安装）为：

1. Interface 0.2.0、Project Store 0.2.3、Runtime Pool 0.2.0。
2. Agent Env 0.1.4、Project Update 0.2.3。
3. Runner 0.4.0。
4. Script MaaFW 0.1.13。
5. Script MaaFW Managed 0.3.2。
6. 独立 M9A 仓库的 pack，再发布聚合包。

Project Store、Runtime Pool、Managed 的首次 PyPI 发布仍需要对应 GitHub Environment
和 pending trusted publisher；本地文档或构建不会代替这些外部配置。

## 7. 明确不承诺的能力

以下内容不要在宿主或 project pack 中假设已经完成：

- Managed 资源管理器的前端入口目前关闭，恢复 UI 前不能要求用户操作它。
- Project Store/Runtime Pool 的全局孤儿盘点与 GC 必须以完整引用、pin、lease 和活动
  运行状态为依据；信息不完整时应 fail-closed，不得递归删除目录。
- 远程更新只支持插件明确声明的 MirrorChyan/GitHub Release 资源，不提供任意 URL
  下载或第三方项目的自动脱壳迁移。
- 通用插件不为具体 M9A、MaaEnd 或其他项目猜测 task/option 语义；这些逻辑属于
  project pack。
