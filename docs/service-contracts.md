# MaaFW 插件服务契约与调用示例

> 契约版本：v1  
> 对应 AUTO-MAS 合并：`AUTO-MAS-Project/AUTO-MAS#290`  
> 适用范围：MaaFW ProjectInterfaceV2 插件组及 M9A project pack

## 1. 目标与兼容原则

本文档描述 MaaFW 插件组向 AUTO-MAS 宿主及其他插件公开的服务名、方法、输入输出模型和调用方法。

兼容原则：

- 以服务注册表中的服务名作为跨插件边界，不依赖插件实例或内部模块。
- 带 `.v1` 后缀的服务名代表稳定契约版本；破坏性变更应注册新的 `.v2` 服务。
- 不以下划线开头的 service 方法属于公开调用面；`_` 开头的函数和 runner 内部类不保证兼容。
- 输入模型通常同时接受 Pydantic 模型和等价 `dict`，输出优先使用明确的 Pydantic 模型或 dataclass。
- 调用方必须处理服务未加载的情况，不能假定某个可选插件始终启用。
- 新增未知 ProjectInterface 字段时，已支持的 task、option 和 preset 应继续工作；未知字段只记录后台警告。

## 2. 通用服务获取方式

宿主或普通模块通过全局服务注册表获取服务：

```python
from typing import Any

from app.plugins.manager import PluginManager


def require_plugin_service(name: str) -> Any:
    service = PluginManager.service.get(name)
    if service is None:
        raise RuntimeError(f"插件服务未加载: {name}")
    return service
```

插件实例内部优先通过 `PluginContext` 获取：

```python
class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx

    async def on_start(self) -> None:
        interface_service = self.ctx.get("maafw.interface.v1")
        if interface_service is None:
            raise RuntimeError("缺少 maafw.interface.v1")
```

服务调用属于进程内 Python 契约，不是 HTTP API。业务异常默认向调用方抛出，由 API 或任务边界转换为用户可见结果。

## 3. 插件与服务总览

| PyPI 包 | 当前版本 | 服务名 | 主要职责 |
|---|---:|---|---|
| `automas-maafw-interface` | 0.2.0 | `maafw.interface.v1` | PI 加载、校验、预览、任务快照和 option 归一化 |
| `automas-maafw-project-update` | 0.1.3 | `maafw.project_update.v1` | MirrorChyan/GitHub Release 版本发现、可安装候选与更新 |
| `automas-maafw-agent-env` | 0.1.2 | `maafw.agent_env.v1` | agent 运行方式识别、命令规划和 Python 环境准备 |
| `automas-maafw-controller-adb` | 0.1.0 | `maafw.controller.adb` | ADB provider 与设备参数构建 |
| `automas-maafw-controller-win32` | 0.1.1 | `maafw.controller.win32` | Win32 provider、窗口扫描与设备参数构建 |
| `automas-maafw-project-store` | 0.2.0 | `maafw.project_store.v1` | 本地目录/ZIP 资源导入、不可变版本、能力摘要、引用和 GC |
| `automas-maafw-runtime-pool` | 0.1.4 | `maafw.runtime_pool.v1` | 按 requirement selector 隔离环境并通过 uv cache/hardlink 复用依赖 |
| `automas-maafw-runner` | 0.3.3 | `maafw.runner.v1` | 运行计划、worker job、runtime 路由和结果模型 |
| `automas-script-maafw` | 0.1.9 | `maafw.registry.v1` | MaaFW 脚本适配与 controller/project pack 注册表 |
| `automas-script-maafw-managed` | 0.2.0 | 无 | 声明式本地资源管理、运行绑定与 pack 升级计划 |
| `automas-script-maafw-pack-m9a` | 0.1.0 | `maafw.pack.m9a.v1` | M9A 默认约定、通知翻译和旧配置迁移草稿 |
| `automas-m9a` | 0.1.0 | 无 | 聚合安装上述 MaaFW/M9A 插件 |

## 4. `maafw.interface.v1`

### 4.1 公开方法

```python
load(path, *, force_reload=False) -> MaaFWInterface

preview(path, *, force_reload=False) -> MaaFWInterfacePreviewData

validate(interface) -> MaaFWInterfaceValidationReport

build_default_snapshot(
    interface,
    *,
    preset=None,
) -> MaaFWTaskPresetSnapshot

normalize_snapshot(
    interface,
    snapshot,
) -> MaaFWTaskPresetSnapshot

normalize_execution_payload(
    interface,
    tasks,
    options,
    *,
    controller=None,
    resource=None,
) -> tuple[list[str], dict[str, dict[str, Any]]]

rescan_option(path, option_name) -> list[dict[str, str]]
```

### 4.2 ProjectInterface 主字段

`MaaFWInterface` 支持以下顶层字段：

```text
interface_version
languages
name / label / title / icon
mirrorchyan_rid / mirrorchyan_multiplatform
github / version / contact / license
welcome / description
controller
resource
group
pretask
agent
task
option
global_option
import
preset
```

`pretask` 字段：

```text
name
label
description
icon
exec
args
option
resource
controller
```

`MaaFWTaskPresetSnapshot` 字段：

```text
taskOrder: list[str]
taskChecked: dict[str, bool]
taskOptions: dict[str, dict[str, Any]]
```

校验和预览输出：

```text
MaaFWInterfaceValidationReport:
  ok: bool
  message: str

MaaFWInterfacePreviewData:
  path: str
  project: dict[str, Any]
  globalOption: list[str]
  controllers: list[dict[str, Any]]
  resources: list[dict[str, Any]]
  groups: list[dict[str, Any]]
  tasks: list[dict[str, Any]]
  options: list[dict[str, Any]]
  presets: list[dict[str, Any]]
  importCount: int
  agentCount: int
  controlCapabilities: dict[str, Any]
```

当前由 AUTO-MAS 配置界面支持的 option 类型：

```text
select
checkbox
input
switch
scan_select
```

`hotkey`、`setting` 和未来未知类型不进入前端配置界面，只记录后台警告，不阻断已支持任务的加载、编辑和执行。`hotkey` 应由用户在原项目或原应用中配置。

### 4.3 pretask 语义

- pretask 在用户手动加入任务队列后才执行，不是项目加载时自动执行。
- pretask 与普通 task 共用 `taskOrder` 和 `taskChecked`。
- 运行计划中转换为 `MaaFWPretaskRunPlan`。
- 桌面项目执行顺序为：根据 MAS `Game.Path` 启动应用，执行已选择的 pretask，再执行 MaaFW task。
- AUTO-MAS 不为 pretask 增加独立持久化页面。

### 4.4 调用示例：加载、校验和生成默认快照

```python
from pathlib import Path

interface_service = require_plugin_service("maafw.interface.v1")
project_path = Path(r"D:\MaaEnd")

interface = interface_service.load(project_path)
report = interface_service.validate(interface)
if not report.ok:
    raise ValueError(report.message)

snapshot = interface_service.build_default_snapshot(
    interface,
    preset="日常任务",
)

print(snapshot.taskOrder)
print(snapshot.taskChecked)
print(snapshot.taskOptions)
```

### 4.5 调用示例：归一化实际执行任务

```python
task_names, task_options = interface_service.normalize_execution_payload(
    interface,
    tasks=["StartUp", "Psychube"],
    options={
        "Psychube": {
            "difficulty": "hard",
        },
    },
    controller="adb",
    resource="resource",
)
```

### 4.6 调用示例：重新扫描 `scan_select`

```python
choices = interface_service.rescan_option(
    project_path,
    option_name="account",
)

# 返回示例：[{"name": "account-1", "label": "账号一"}]
```

校验失败或 `interface.json` 无法加载时可能抛出 `MaaFWInterfaceLoadError`；`validate()` 会把模型校验异常转换为 `ok=False` 的报告。

## 5. `maafw.project_update.v1`

### 5.1 公开方法

```python
list_providers() -> list[MaaFWUpdateProviderInfo]

await discover_update(
    interface,
    *,
    current_version=None,
    source_config=None,
    proxy=None,
    send_log=None,
) -> MaaFWProjectUpdateDiscovery | None

await check_update(
    interface,
    *,
    current_version=None,
    source_config=None,
    proxy=None,
    send_log=None,
) -> MaaFWProjectUpdateCandidate | None

await apply_update(
    project_path,
    candidate,
    *,
    proxy=None,
    send_log=None,
) -> None

await update_if_needed(
    project_path,
    interface,
    *,
    mirror_cdk="",
    channel="stable",
    proxy=None,
    send_log=None,
    source_config=None,
) -> MaaFWProjectUpdateResult
```

### 5.2 输入字段

`source_config` 常用字段：

```text
source: mirrorchyan | github_release
channel
mirror_cdk / cdk
github_repo
github_tag
github_token
github_asset_pattern
sha256
```

### 5.3 输出模型

```text
MaaFWUpdateProviderInfo:
  name: str
  label: str
  description: str

MaaFWProjectUpdateDiscovery:
  source: str
  version: str
  candidate: MaaFWProjectUpdateCandidate | None
  unavailable_reason: str

MaaFWProjectUpdateCandidate:
  source: str
  version: str
  download_url: str | None
  sha256: str | None

MaaFWProjectUpdateResult:
  checked: bool
  updated: bool
  current_version: str
  latest_version: str | None
  source: str | None
  message: str
  update_available: bool
  installable: bool
```

`discover_update()` 返回非空但 `candidate=None`，表示提供者确认有更新版本，
但没有给出可安装下载地址。`check_update()` 仅兼容旧的 candidate-only 调用，
遇到这种状态会抛出可诊断错误，绝不会返回伪 candidate。

### 5.4 调用示例：检查并应用更新

```python
update_service = require_plugin_service("maafw.project_update.v1")

candidate = await update_service.check_update(
    interface,
    current_version="v2.19.0-beta.5",
    source_config={
        "source": "mirrorchyan",
        "channel": "stable",
        "mirror_cdk": "",
    },
    send_log=print,
)

if candidate is not None:
    await update_service.apply_update(
        project_path,
        candidate,
        send_log=print,
    )
```

简化调用：

```python
result = await update_service.update_if_needed(
    project_path,
    interface,
    channel="stable",
    source_config={"source": "mirrorchyan"},
    send_log=print,
)

if result.updated:
    print(f"已更新至 {result.latest_version}")
```

更新检查或应用失败抛出 `MaaFWProjectUpdateError`。调用方不得在更新失败后把项目版本写成候选版本。

## 6. `maafw.agent_env.v1`

### 6.1 公开方法

```python
classify(agent) -> str

build_command_plans(
    project_path,
    interface_or_agent,
    *,
    managed_env_root=None,
) -> list[MaaFWAgentCommandPlan]

prepare_env(
    project_path,
    interface_or_agent,
    *,
    managed_env_root=None,
    send_log=None,
    bootstrap_python=None,
    install_dependencies=True,
) -> MaaFWAgentEnvPrepareResult
```

运行类型：

```text
embedded
project_python
project_binary
isolated_venv
external
```

### 6.2 输出字段

```text
MaaFWAgentCommandPlan:
  childExec: str
  executable: str
  executableExists: bool | None
  fallbackReason: str | None
  runtimeKind: str | None
  isolatedVenvPath: str | None
  childArgs: list[str]
  command: list[str]
  cwd: str
  identifier: str | None
  embedded: bool

MaaFWAgentEnvPrepareResult:
  projectPath: str
  plans: list[MaaFWAgentCommandPlan]
  preparedVenvs: list[str]
  skipped: list[str]
  messages: list[str]
```

`interface_or_agent` 可以传完整 `MaaFWInterface`，也可以传单个 agent 或 agent 列表。单个 `MaaFWAgent` 的标准字段为：

```text
child_exec: str
child_args: list[str] | None
identifier: str | None
embedded: bool | None
```

### 6.3 调用示例

```python
agent_service = require_plugin_service("maafw.agent_env.v1")

plans = agent_service.build_command_plans(
    project_path,
    interface,
    managed_env_root=r"D:\AUTO-MAS\agent-envs",
)

for plan in plans:
    print(plan.runtimeKind, plan.command)

prepare_result = agent_service.prepare_env(
    project_path,
    interface,
    managed_env_root=r"D:\AUTO-MAS\agent-envs",
    bootstrap_python=r"C:\Python312\python.exe",
    install_dependencies=True,
    send_log=print,
)
```

只想预览命令时应调用 `build_command_plans()`，不要调用会创建环境和安装依赖的 `prepare_env()`。

## 7. `maafw.controller.adb`

### 7.1 Provider 契约

```text
key: adb
displayName: ADB
controllerTypes: ["Adb"]
capabilities:
  - device_spec
  - emulator_service_consumption
```

### 7.2 公开方法

```python
get_provider_definition() -> dict[str, Any]

build_device_spec(
    *,
    adb_path=None,
    address=None,
    screencap_methods=0,
    input_methods=0,
    config=None,
) -> dict[str, Any]
```

设备输出字段：

```text
type: "Adb"
adbPath
address
screencapMethods
inputMethods
config
```

### 7.3 调用示例

```python
adb_service = require_plugin_service("maafw.controller.adb")

device_spec = adb_service.build_device_spec(
    adb_path=r"C:\Android\platform-tools\adb.exe",
    address="127.0.0.1:5555",
    screencap_methods=0,
    input_methods=0,
    config={
        "extras": {
            "mumu": {},
        },
    },
)
```

## 8. `maafw.controller.win32`

### 8.1 Provider 契约

```text
key: win32
displayName: Win32
controllerTypes: ["Win32"]
capabilities:
  - window_scan
  - device_spec
```

### 8.2 公开方法

```python
get_provider_definition() -> dict[str, Any]

list_windows() -> list[MaaFWWin32Window]

match_controller_windows(
    controller,
    windows=None,
) -> list[MaaFWWindowMatch]

build_device_spec(
    *,
    h_wnd=None,
    screencap_method=0,
    mouse_method=0,
    keyboard_method=0,
) -> dict[str, Any]
```

输出字段：

```text
MaaFWWin32Window:
  hWnd: int
  className: str
  windowName: str

MaaFWWindowMatch:
  hWnd: int
  className: str
  windowName: str
  controllerName: str
  controllerType: str
```

### 8.3 调用示例：按 PI controller 匹配窗口

```python
win32_service = require_plugin_service("maafw.controller.win32")

matches = win32_service.match_controller_windows(
    {
        "name": "endfield",
        "label": "终末地",
        "type": "Win32",
        "win32": {
            "class_regex": ".*",
            "window_regex": ".*Endfield.*",
        },
    }
)

if not matches:
    raise RuntimeError("未找到匹配的游戏窗口")

device_spec = win32_service.build_device_spec(
    h_wnd=matches[0].hWnd,
    screencap_method=0,
    mouse_method=0,
    keyboard_method=0,
)
```

`list_windows()` 仅支持 Windows。窗口正则有长度和嵌套量词限制，非法或高风险表达式会抛出 `RuntimeError`。

## 9. `maafw.runner.v1`

### 9.1 公开方法

```python
build_plan(
    project_path,
    interface,
    *,
    controller_name=None,
    resource_name=None,
    selected_preset=None,
    task_snapshot=None,
    task_names=None,
    task_options=None,
    managed_env_root=None,
) -> MaaFWRunPlan

create_job_payload(
    plan,
    device_config,
) -> MaaFWRunnerJobPayload

prepare_environment(
    project_path,
    *,
    managed_env_root=None,
    import_paths=None,
    send_log=None,
) -> MaaFWRunnerEnvironment

write_job_file(
    payload,
    work_dir,
    *,
    job_name=None,
) -> Path

run_worker(
    payload,
    *,
    work_dir,
    worker_command=None,
    send_log=None,
    timeout=None,
) -> MaaFWRunResult
```

### 9.2 运行计划字段

```text
MaaFWRunPlan:
  path: str
  projectName: str
  projectLabel: str | None
  controllerName: str
  controllerType: str
  resourceName: str
  resource: MaaFWResourceBundlePlan
  agents: list[MaaFWAgentCommandPlan]
  pretasks: list[MaaFWPretaskRunPlan]
  piEnv: dict[str, str]
  tasks: list[MaaFWTaskRunPlan]
  skippedTasks: list[MaaFWSkippedTaskPlan]

MaaFWResolvedPath:
  raw: str
  resolved: str
  exists: bool
  isFile: bool
  isDir: bool

MaaFWResourceBundlePlan:
  name: str
  label: str | None
  paths: list[MaaFWResolvedPath]
  attachedPaths: list[MaaFWResolvedPath]

MaaFWPretaskRunPlan:
  name: str
  label: str | None
  executable: str
  args: list[str]
  options: dict[str, Any]

MaaFWTaskRunPlan:
  name: str
  label: str | None
  entry: str
  options: dict[str, Any]
  pipelineOverride: dict[str, Any]
  logOptions: dict[str, Any]
  overrideNodes: list[str]

MaaFWSkippedTaskPlan:
  name: str
  label: str | None
  entry: str | None
  reason: str
```

设备模型：

```text
MaaFWDeviceConfig:
  type: Adb | Win32
  adbPath: str | None
  address: str | None
  hWnd: int | None
  screencapMethods: int
  inputMethods: int
  screencapMethod: int
  mouseMethod: int
  keyboardMethod: int
  config: dict[str, Any]
```

运行结果：

```text
MaaFWRunResult:
  success: bool
  projectName: str
  controllerName: str
  resourceName: str
  completedTasks: list[str]
  failedTask: str | None
  errorMessage: str | None
```

`prepare_environment()` 返回：

```text
MaaFWRunnerEnvironment:
  python_executable: Path
  venv_path: Path
  env: dict[str, str]
  packages: tuple[str, ...]
  maafw_version: str | None
```

### 9.3 调用示例：构建并执行 worker job

```python
runner_service = require_plugin_service("maafw.runner.v1")
adb_service = require_plugin_service("maafw.controller.adb")

plan = runner_service.build_plan(
    project_path,
    interface,
    controller_name="adb",
    resource_name="resource",
    task_snapshot={
        "taskOrder": ["StartUp", "Psychube"],
        "taskChecked": {
            "StartUp": True,
            "Psychube": True,
        },
        "taskOptions": {},
    },
)

device_config = adb_service.build_device_spec(
    adb_path=r"C:\Android\platform-tools\adb.exe",
    address="127.0.0.1:5555",
)

payload = runner_service.create_job_payload(
    plan,
    device_config,
)

result = runner_service.run_worker(
    payload,
    work_dir=r"D:\AUTO-MAS\runtime",
    send_log=print,
    timeout=3600,
)

if not result.success:
    raise RuntimeError(result.errorMessage or "MaaFW 运行失败")
```

### 9.4 worker JSON Lines 协议

```json
{"type": "log", "message": "正在连接设备"}
{"type": "result", "data": {"success": true}}
{"type": "error", "message": "运行异常"}
```

退出码：

```text
0  正常成功
2  worker 正常结束，但任务结果失败
1  worker 未处理异常
64 参数错误
```

PI 环境变量：

```text
PI_INTERFACE_VERSION=v2.8.1
PI_CLIENT_NAME
PI_CLIENT_VERSION
PI_CLIENT_LANGUAGE=zh_cn
PI_CLIENT_MAAFW_VERSION
PI_VERSION
PI_CONTROLLER
PI_RESOURCE
```

## 10. `maafw.registry.v1`

该服务由 `automas-script-maafw` 提供，用于动态组合 controller provider 和 project pack。

### 10.1 公开方法

```python
register_controller_provider(definition) -> None
unregister_controller_provider(key) -> None
list_controller_providers() -> list[dict[str, Any]]
get_controller_provider(key) -> dict[str, Any] | None

register_project_pack(definition) -> None
unregister_project_pack(key) -> None
list_project_packs() -> list[dict[str, Any]]
get_project_pack(key) -> dict[str, Any] | None
```

### 10.2 调用示例：读取已注册能力

```python
registry = require_plugin_service("maafw.registry.v1")

for provider in registry.list_controller_providers():
    print(provider["key"], provider["capabilities"])

m9a_pack = registry.get_project_pack("m9a")
if m9a_pack is not None:
    print(m9a_pack["default_controller"])
```

### 10.3 调用示例：插件注册 controller provider

```python
class CustomControllerPlugin:
    def __init__(self, ctx):
        self.ctx = ctx
        self.key = "custom-controller"

    async def on_start(self) -> None:
        registry = self.ctx.get("maafw.registry.v1")
        if registry is None:
            return

        registry.register_controller_provider(
            {
                "key": self.key,
                "displayName": "Custom Controller",
                "controllerTypes": ["Custom"],
                "capabilities": ["device_spec"],
            }
        )

    async def on_stop(self, reason: str) -> None:
        registry = self.ctx.get("maafw.registry.v1")
        if registry is not None:
            registry.unregister_controller_provider(self.key)
```

注册项必须有非空 `key`；相同 `key` 的后一次注册会覆盖前一次定义。插件卸载时必须注销自己注册的 provider 或 pack。

## 11. MaaFW 脚本配置契约

`automas-script-maafw` 注册脚本类型 `MaaFW`，适配器生命周期为：

```python
await check(runtime) -> str
await prepare(runtime) -> None
run_auto_proxy(runtime) -> TaskExecuteBase
await finalize(runtime) -> None
await on_crash(runtime, error) -> None
```

脚本配置字段：

```text
Info:
  Name, ProjectLabel, Path, Controller, Resource

Emulator:
  Id, Index

Device:
  AdbPath, AdbAddress
  AdbScreencapMethods, AdbInputMethods
  HWnd
  Win32ScreencapMethod, Win32MouseMethod, Win32KeyboardMethod
  GamepadType
  PlayCoverAddress, PlayCoverUuid

Game:
  Path, Arguments, WaitTime, CloseOnFinish

Update:
  IfAutoUpdate, Source, Channel
  MirrorChyanCDK
  GitHubRepo, GitHubTag, GitHubAssetPattern

Run:
  ProxyTimesLimit, RunTimesLimit, RunTimeLimit
  DailyOnceTasks, WeeklyOnceTasks, MonthlyOnceTasks
```

用户配置字段：

```text
Info:
  Name, Status, RemainedDay
  IfScriptBeforeTask, ScriptBeforeTask
  IfScriptAfterTask, ScriptAfterTask
  Notes, Account, Password
  Controller, Resource

Task:
  SelectedPreset, TaskSnapshot

Device:
  AdbAddress, HWnd
  PlayCoverAddress, PlayCoverUuid

Data:
  LastProxyDate, ProxyTimes, IfPassCheck
  LastProxyStatus, PeriodTaskRecords

Notify:
  Enabled, IfSendStatistic
  IfSendMail, ToAddress
  IfServerChan, ServerChanKey
  CustomWebhooks
```

脚本适配器通常由 AUTO-MAS 的任务管理器创建，其他插件不应直接实例化内部 `MaaFWPluginAutoProxyTask`。

## 12. `maafw.pack.m9a.v1`

### 12.1 公开方法

```python
get_definition() -> M9APackDefinition

describe_resource(source_path) -> dict

plan_resource_upgrade(
    old_interface,
    new_interface,
    config,
) -> dict

translate_notification(
    result,
    *,
    script_name="M9A",
    user_name="",
    started_at=None,
    ended_at=None,
) -> M9ANotificationContent
```

### 12.2 Pack 字段

```text
M9APackDefinition:
  key
  display_name
  project_repo
  interface_path
  supported_controllers
  default_controller
  default_resource
  default_preset
  default_task_queue
  period_rules
  reserved_task_semantics
  icon
  notes
  framework
  capabilities
  resource_contract_version
  resource_version_source
  resource_service_key
  resource_upgrade_mode
```

默认周期规则：

```text
Psychube: daily
Limbo: monthly
Lucidscape: monthly
```

通知输出：

```text
M9ANotificationContent:
  title: str
  text: str
  html: str | None
```

迁移输出：

```text
M9AMigrationDraft:
  script: dict[str, Any]
  users: list[dict[str, Any]]
  warnings: list[str]
```

### 12.3 调用示例：读取 M9A 默认约定

```python
m9a_service = require_plugin_service("maafw.pack.m9a.v1")
definition = m9a_service.get_definition()

print(definition.project_repo)
print(definition.default_task_queue)
print(definition.period_rules)
```

### 12.4 调用示例：生成通知内容

```python
notification = m9a_service.translate_notification(
    {
        "success": False,
        "completedTasks": ["StartUp"],
        "failedTask": "Psychube",
        "errorMessage": "任务执行失败",
    },
    script_name="M9A 日常",
    user_name="账号一",
    started_at="2026-07-11 09:00:00",
    ended_at="2026-07-11 09:05:00",
)

print(notification.title)
print(notification.text)
```

## 13. `automas-m9a` 聚合包

`automas-m9a` 没有运行时代码、插件 entry point 或独立服务。它只用于一次安装完整的 M9A 依赖集合。

发布到 PyPI 后可通过聚合包安装：

```powershell
python -m pip install automas-m9a
```

当前 pretask 支持相关的最低版本应为：

```text
automas-maafw-interface >= 0.2.0
automas-maafw-runner >= 0.3.3
automas-script-maafw >= 0.1.9
```

## 14. 完整调用示例

以下示例串联 PI 加载、项目更新、agent 环境、运行计划和 worker：

```python
from pathlib import Path

from app.plugins.manager import PluginManager


def require_service(name: str):
    service = PluginManager.service.get(name)
    if service is None:
        raise RuntimeError(f"插件服务未加载: {name}")
    return service


async def run_maafw_project() -> None:
    project_path = Path(r"D:\MaaEnd")

    interface_service = require_service("maafw.interface.v1")
    update_service = require_service("maafw.project_update.v1")
    agent_service = require_service("maafw.agent_env.v1")
    adb_service = require_service("maafw.controller.adb")
    runner_service = require_service("maafw.runner.v1")

    interface = interface_service.load(project_path)
    validation = interface_service.validate(interface)
    if not validation.ok:
        raise ValueError(validation.message)

    update_result = await update_service.update_if_needed(
        project_path,
        interface,
        channel="stable",
        source_config={"source": "mirrorchyan"},
        send_log=print,
    )
    if update_result.updated:
        interface = interface_service.load(
            project_path,
            force_reload=True,
        )

    agent_service.prepare_env(
        project_path,
        interface,
        install_dependencies=True,
        send_log=print,
    )

    snapshot = interface_service.build_default_snapshot(interface)
    plan = runner_service.build_plan(
        project_path,
        interface,
        controller_name="adb",
        resource_name="resource",
        task_snapshot=snapshot.model_dump(mode="json"),
    )

    device_spec = adb_service.build_device_spec(
        adb_path=r"C:\Android\platform-tools\adb.exe",
        address="127.0.0.1:5555",
    )
    payload = runner_service.create_job_payload(plan, device_spec)
    result = runner_service.run_worker(
        payload,
        work_dir=r"D:\AUTO-MAS\runtime",
        send_log=print,
        timeout=3600,
    )

    if not result.success:
        raise RuntimeError(result.errorMessage or "MaaFW 运行失败")
```

## 15. `maafw.project_store.v1`

项目存储只接受本地目录或 ZIP，不负责下载。ZIP 会在 Store 自己的 `.staging`
中安全展开，并拒绝越界/绝对路径、大小写碰撞、链接和设备项、加密项及超过
条目数、单文件、总展开体积或压缩比限制的输入。导入后只保留
ProjectInterface 可达资源和运行内容，不保留可识别的外部 UI 壳。

```python
import_project(
    source_path,
    project_id,
    version=None,
    *,
    runtime_constraint=None,
    platform=None,
    arch=None,
    runtime_binding=None,
    reference=None,
    pinned=False,
    activate=True,
) -> dict

update_project(source_path, project_id, version, **kwargs) -> dict
resolve_project(project_id, version=None, *, touch=True) -> dict
list_projects() -> list[dict]
list_versions(project_id) -> list[dict]
switch_version(project_id, version) -> dict
bind_runtime(project_id, version=None, *, binding=None, reference=None, pinned=None) -> dict
release_runtime(project_id, version=None, *, reference=None, clear_binding=False) -> dict
set_references(project_id, version, references) -> dict
acquire_lease(project_id, version=None, *, owner, ttl_seconds=300, lease_id=None) -> dict
release_lease(project_id, version=None, *, lease_id) -> dict
delete_version(project_id, version) -> dict
collect_garbage(*, project_id=None, dry_run=True, grace_seconds=86400, keep_latest=1) -> dict
resource_lifecycle_transaction() -> AsyncContextManager[None]
```

显式 `version` 与 `ProjectInterface.version` 同时存在时必须语义等价；最终使用
ProjectInterface 的原始版本拼写。未显式给版本时必须能从 ProjectInterface
推断，否则拒绝导入。

`resolve_project()` 至少返回：

```text
projectId
version
dataPath
runtimeConstraint
manifest
summary
```

`summary` 是供本地资源管理界面消费的 JSON 摘要，包含 `interfaceVersion`、
`sourceKind`、`runtimeConstraint`、`agents`/`agentCount`、`capabilities`、
`shells`、`size`、`flags` 和 `warningCount`。`shells` 记录被剥离壳的类别与路径；
`size` 同时给出输入、原树、最终投影与节省字节数。

`dataPath` 内含根级 `interface.json[c]` 与私有
`.auto_mas_maafw_project.json`。Project Store 不会覆盖已提交的投影 payload；
私有 manifest 的引用、固定、runtime binding 和最后使用时间可以原子更新。
Runner 运行时仍会在 `dataPath` 下创建 `debug/`、`logs/`、`temp/`，并更新
`config/maa_option.json`；这些运行产物不参与版本内容身份。

删除操作只处理 Project Store 自己的根目录，并拒绝当前版本、固定版本和有引用
版本。`collect_garbage()` 默认 dry-run。跨多个异步调用的资源引用新增/释放、
全量引用对账、版本绑定与实际 GC 必须进入共享的
`resource_lifecycle_transaction()`；同一 asyncio task 可以重入，ContextVar
即使被子 task 继承，子 task 尝试重入也会被拒绝，其他 task 等待。它是同进程、
同一服务实例上的 Python 协调接口，
不是 JSON action 或跨进程文件锁，也不替代 reference、pin、lease 删除守卫和
同步方法内部的单调用 `RLock`。典型用法：

```python
async with project_store.resource_lifecycle_transaction():
    records = await load_all_managed_script_records()
    await reconcile_project_references(records)
    await collect_garbage(dry_run=False)
```

### 15.1 `MaaFWManaged` 资源升级事务

`ImportProjectId` 只用于首次导入，成功绑定后清空。`ProjectId` 和 `Version`
由 Project Store 写入并在配置页只读展示。绑定后的运行、升级校验、运行时安装
和引用对账以私有不可变 manifest 中的 `projectId`/`version` 为权威身份，
不以可编辑表单载荷重定义项目归属。

本地目录或 ZIP 升级先以非活动版本导入；选择已安装版本也进入相同流程，不提供
直接或强制切换旁路。Managed 调用项目 pack 的 `plan_resource_upgrade()` 分别
规划脚本记录与当时存在的全部用户记录，并持久化一个带 `planId` 的 journal。
脚本记录只保存用户计划摘要；每个用户的完整 source/target 配置只保存在自己的
隐藏记录中。

应用前必须同时匹配确认令牌、`planId`、源/目标资源哈希、脚本/用户配置哈希和
用户 ID 集合。应用的是已持久化计划，不在确认时重新规划；所有配置写入成功后
才激活目标版本。JSON object 字段以原子替换语义写入，使配置应用和回滚都不会
因宿主深合并保留旧键。`applying`/`committing` 等中断状态会阻断运行，并在插件
启动或再次应用时恢复源版本和源配置。规划错误、人工动作、CAS 失配或回滚失败
都不会把目标版本报告为已生效。

## 16. `maafw.runtime_pool.v1`

```python
list_runtimes() -> list[dict]
resolve_runtime(request) -> dict | None
ensure_runtime(request) -> dict
touch(runtime_id) -> dict
pin(runtime_id, pinned=True) -> dict
set_references(runtime_id, references) -> dict
acquire_lease(runtime_id, lease_id, *, owner="", ttl_seconds=None) -> dict
release_lease(runtime_id, lease_id) -> dict
delete(runtime_id) -> dict
collect_garbage(*, dry_run=True, grace_seconds=604800, keep_latest=1) -> dict
```

`request` 可以是 MaaFW requirement 字符串，或包含 `requirements`/`packages`
完整依赖集合的字典。结果至少包含：

```text
runtimeId
pythonExecutable
venvPath
packages
selectorRequirements
resolvedRequirements
maafwRequirement
maafwVersion
identity
references
leases
```

runtime identity 由规范化 requirement selector 集合、Python ABI、操作系统和架构
组成，不含项目路径。`resolvedRequirements` 是安装后的 `pip freeze --all` 审计
快照，不参与 identity；因此范围 selector 对应的 runtime 删除后重建时，可能解析
到范围内更新的依赖版本。包含本地路径、editable 或递归 requirements 的依赖不能
安全跨项目共享，服务会拒绝创建共享 identity。删除与 GC 会保护固定、引用和活动
lease。

每个完整 requirement selector 仍对应一个独立 venv；环境之间不共享
`site-packages`，也不拼接 `PYTHONPATH`。使用 uv 时，它们共享 pool 内的
下载/解包缓存，并以 hardlink 安装可复用文件，因此依赖隔离与磁盘复用可以同时
成立。每次 GC 返回 `cachePrune`：dry-run 只统计缓存并展示命令，实际 GC 在删除
符合条件的 runtime 后执行 `uv cache prune --cache-dir <pool-cache>`。uv 缺失、
缓存路径不安全或命令失败会返回显式状态，不会直接递归删除缓存目录。

## 17. Runner 路由补充

`prepare_environment()` 新增可选参数：

```python
runtime_pool_root=None
runtime_requirement=None
runtime_id=None
lease_owner="automas-maafw-runner"
lease_ttl_seconds=86400
```

托管项目未显式传参时，Runner 会读取项目根
`.auto_mas_maafw_project.json` 的 `runtime.constraint` 和
`runtime.binding.runtimeId`。binding 必须与当前 selector identity 一致；托管项目
既无 binding 又无版本约束时会拒绝运行，不会静默安装 `latest`。已绑定 runtime
丢失但 binding 记录了 `maafwVersion` 时，Managed gateway 会按精确
`maafw==<version>` 重建环境并持久化新 binding。

`prepare_environment()` 返回的 `MaaFWRunnerEnvironment` 带 `runtime_id`、
`runtime_pool_root` 和 `lease_id`。调用方必须在 worker 退出的 `finally` 中调用
`release_environment(environment)`；超时 lease 只用于进程异常退出后的兜底回收。
`MaaFWManaged` 的项目与 runtime lease 至少为 24 小时，若脚本运行时限更长则按
“时限 + 10 分钟”延长；首版没有 heartbeat，超长无上限任务应显式配置运行时限。

`MaaFWRunPlan.nativePluginPaths` 描述项目原生插件目录。worker 在创建 Resource、
Controller 与 Tasker 前加载这些目录；任一路径越出项目根、缺失或加载失败都会
阻断运行。

托管项目中原本指向被剥离 `python/` 的 Python Agent，只有在私有 manifest 明确
设置 `runtime.sharedAgentDependenciesComplete: true` 时才会复用 worker 当前的
`sys.executable`。该标志要求根 requirements 能完整、平面地表达 Agent 依赖；
否则继续使用隔离环境。项目二进制 Agent、显式外部解释器和非托管项目保持原行为。

`MaaFWManaged` 在实际 GC 前从全部脚本配置对账 `maafw-script:*` 项目引用，并从
现存项目 binding 对账 `maafw-project:*` runtime 引用。dry-run 不删除任何目录，
但也会修正项目引用 manifest。当前项目版本始终受保护；应先切换到其他版本，再
删除旧版本。声明式动作同时提供项目版本和 runtime 列表，便于在通用 SchemaForm
内完成选择、切换与删除。HTTP 动作与运行 Hooks 通过同一个 Project Store 服务
实例共享资源生命周期事务；各路径按需取得锁时，顺序固定为“资源生命周期 →
单脚本升级 → 宿主配置事务 → 运行锁”，因此引用快照、对账和回收不会与
stage/apply/prepare 交错。
Managed 0.2.0 在 Project Store 缺少该 Python 协调接口时失败关闭，不提供无锁
兼容路径；它同时要求宿主提供 `Config.script_config_transaction()`、
`Config.script_config_write_scope()` 和 ScriptConfigStore
`write_transaction()`，宿主事务改动合入前不得启用或发布。

## 18. 变更规则

以下改动可以保留当前服务版本：

- 为返回模型增加有默认值的可选字段。
- 增加新的公开方法。
- 增加新的 provider capability。
- 支持新的 ProjectInterface 字段，同时保留未知字段容错。

以下改动必须升级服务版本：

- 删除或重命名公开方法。
- 修改现有参数含义、必填性或返回类型。
- 删除现有模型字段。
- 改变 worker JSON Lines 事件结构或退出码语义。
- 将原本只警告的未知 PI 字段改为阻断加载。

调用方迁移到新版本前，可以同时注册并保留旧服务，例如同时提供 `maafw.interface.v1` 和 `maafw.interface.v2`。
