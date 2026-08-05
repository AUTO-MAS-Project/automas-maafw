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
| `automas-maafw-project-update` | 0.2.3 | `maafw.project_update.v1` | MirrorChyan/GitHub Release 版本发现、受限下载、临时包安全释放、可安装候选与更新 |
| `automas-maafw-agent-env` | 0.1.4 | `maafw.agent_env.v1` | agent 运行方式识别、命令规划和 Python 环境准备 |
| `automas-maafw-controller-adb` | 0.1.1 | `maafw.controller.adb` | ADB provider 与设备参数构建 |
| `automas-maafw-controller-win32` | 0.1.2 | `maafw.controller.win32` | Win32 provider、窗口扫描与设备参数构建 |
| `automas-maafw-project-store` | 0.2.3 | `maafw.project_store.v1` | 本地目录/ZIP 资源导入、不可变版本、Python 约束、隔离 checkout、全局盘点、引用和 GC |
| `automas-maafw-runtime-pool` | 0.2.0 | `maafw.runtime_pool.v1` | 可配置根目录、CP312/CP313 解释器、按完整 requirement selector 隔离 venv 并复用 uv cache |
| `automas-maafw-runner` | 0.4.0 | `maafw.runner.v1` | 运行计划、worker job、可信 runtime 路由、环境预热和结果模型 |
| `automas-script-maafw` | 0.1.12 | `maafw.registry.v1`、`maafw.configuration_reuse.v1` | MaaFW 脚本适配、能力注册、原生配置导入、用户复制和 Pool 路由 |
| `automas-script-maafw-managed` | 0.3.1 | `maafw.managed.environment.v1` | 单一 MaaFW 入口的原地托管转换、全局资源盘点、本地/远程资源管理、环境准备、运行绑定与 pack 升级计划 |
| `automas-script-maafw-pack-m9a` | 0.1.5 | `maafw.pack.m9a.v1` | M9A 默认约定、资源 profile/升级规划和通知翻译 |
| `automas-m9a` | 0.1.5 | 无 | 聚合安装上述 MaaFW/M9A 插件 |

### 3.1 发布依赖顺序

`publish.yml` 每次只发布一个 distribution。当前版本按以下层级发布；同层可并行，
下一层必须等待依赖版本已经可从 PyPI 安装：

1. `automas-maafw-interface` 0.2.0、`automas-maafw-project-store` 0.2.3、
   `automas-maafw-runtime-pool` 0.2.0。
2. `automas-maafw-agent-env` 0.1.4、`automas-maafw-project-update` 0.2.3。
3. `automas-maafw-runner` 0.4.0。
4. `automas-script-maafw` 0.1.12。
5. `automas-script-maafw-managed` 0.3.1。
6. 在独立 M9A 仓库发布 `automas-script-maafw-pack-m9a` 0.1.5，随后发布
   聚合包 `automas-m9a` 0.1.5；二者不得早于前五层依赖在 PyPI 可安装。

首次发布 Project Store、Runtime Pool 和 Managed 前，仍须创建
`pypi-project-store`、`pypi-runtime-pool`、`pypi-script-maafw-managed` GitHub
Environments，并为三个包配置与仓库、workflow、environment 精确匹配的 PyPI
pending trusted publishers；这些是外部发布前置，不由本地代码或构建代替。

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
    project_path=None,
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
    progress=None,
) -> None

await download_package(
    download_root,
    candidate,
    *,
    proxy=None,
    send_log=None,
    max_download_bytes=4 * 1024 * 1024 * 1024,
    progress=None,
) -> dict[str, Any]

await release_download_package(
    download_root,
    package,
) -> dict[str, Any]

await update_if_needed(
    project_path,
    interface,
    *,
    mirror_cdk="",
    channel="stable",
    proxy=None,
    send_log=None,
    source_config=None,
    progress=None,
) -> MaaFWProjectUpdateResult
```

### 5.2 输入字段

`source_config` 常用字段：

```text
source: ""（自动） | mirrorchyan | github_release
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

MaaFWDownloadedProjectPackage:
  source: str
  version: str
  path: str
  size: int
  sha256: str

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

省略 `source` 或传空字符串表示自动模式。PI 有 MirrorChyan RID 时，MirrorChyan
始终是版本元数据权威；即使 CDK 为空、响应没有下载地址，也会保留已发现版本，
但不会暴露可安装候选。自动模式不再静默回退 GitHub，避免 GitHub API 限流和
来源漂移；显式 `mirrorchyan` 不回退，显式 `github_release` 不查询 MirrorChyan；
PI 没有 RID 的自动模式才直接使用 GitHub。脚本级空 `cdk`/`mirror_cdk` 会继承
宿主传入的非空 `mirror_cdk`。

GitHub 包选择只接受 Release assets，不把仓库 `zipball` 当作发行包。显式
`github_asset_pattern` 命中多个文件或按项目名、Windows/x64 约定仍无法得到唯一
ZIP 时，发现结果保持不可安装，绝不会拿列表中的第一个 ZIP 猜测。

`download_package()` 只接受 HTTPS，逐跳校验重定向，流式限制下载大小，并在
ZIP/SHA256 校验后以内容哈希文件名原子发布到调用方管理的目录。并发下载不共享
可变临时文件；相同内容复用同一归档，不同内容不会因版本名相同而互相覆盖。
该方法不解压、不修改活动项目，Project Store 仍是安全解包与不可变导入的唯一
权威。

`release_download_package(download_root, package)` 只释放同一根目录下严格匹配
`<24hex>/<package.sha256>.zip` 的普通文件。释放前重新校验完整 SHA-256，并拒绝
越界路径、额外目录层级、符号链接、Windows reparse point、身份变化和非普通文件；
缺失文件按已释放幂等返回。成功后只对已为空的 24hex 直接父目录调用 `rmdir()`，
绝不递归删除。下载失败或取消同样只清理本次 UUID temp/provisional 文件和经相同
边界校验确认的空目录，且清理不会吞掉原始异常或取消。

`progress(event)` 是 best-effort、JSON-friendly 的观察回调；回调异常不会中断
更新事务。`event.stage` 可能为 `checking`、`downloading`、`validating`、
`extracting`、`switching`、`completed` 或 `failed`。下载事件提供真实
`downloaded_bytes`、`total_bytes` 和 `percent`：已知总长按 200 ms 或 1% 节流，
未知总长按 250 ms 或 1 MiB 节流。整个下载（含重试）共用 300 秒墙钟 deadline；
超时会删除临时文件并发出 `failed/download_timeout`，不会进入校验、解压或切换。
版本发现事件同时给出 `metadata_source` 和 `package_source`；后者仅在确有可安装候选
时出现，用于区分版本元数据来源与实际安装包来源，不代表提供者之间会自动回退。

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
若提供者响应带有结构化错误码，异常的 `provider_error_code` 保留原始整数码；
MirrorChyan 的 CDK 错误（如 `7001`--`7005`）不得被改写成通用 HTTP 错误，宿主
接口应同时在响应数据的 `providerErrorCode` 中透传该值。

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
    progress=None,
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

`progress(event)` 为 best-effort 回调。Agent 环境事件始终包含 `stage`、`status`、
`message`、`percent`、`completed`、`total`；当前 `stage` 为
`preparing_agents`，百分比是按已完成计划数计算的确定值。回调异常不会影响环境
准备。项目发行包自带的 `project_python` 只检查解释器能否启动并导入
`maa.agent.agent_server.AgentServer`，不要求提供 pip/ensurepip，也不会由 AUTO-MAS
修改；缺模块时应重新取得完整发行包。只有 AUTO-MAS 自建的 `isolated_venv` 才会
执行依赖安装和 pip 健康检查。

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
    runtime_pool_root=None,
    runtime_pool=None,
    runtime_installer=None,
    runtime_requirement=None,
    runtime_requirements=None,
    runtime_id=None,
    runtime_pool_id=None,
    runtime_python_constraint=None,
    lease_owner="automas-maafw-runner",
    lease_ttl_seconds=86400,
    import_paths=None,
    send_log=None,
    progress=None,
) -> MaaFWRunnerEnvironment

release_environment(
    environment,
    *,
    runtime_pool=None,
) -> dict | None

prepare_project_environment(
    project_path,
    interface,
    *,
    runtime_pool_root=None,
    runtime_pool=None,
    runtime_installer=None,
    runtime_requirement=None,
    runtime_requirements=None,
    runtime_id=None,
    runtime_pool_id=None,
    runtime_python_constraint=None,
    agent_env_root=None,
    import_paths=None,
    send_log=None,
    bootstrap_python=None,
    install_agent_dependencies=True,
    progress=None,
) -> dict[str, Any]

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
  runtime_id: str | None
  maafw_requirement: str | None
  runtime_pool_root: Path | None
  lease_id: str | None

prepare_project_environment result:
  status: "ready"
  runtime:
    runtimeId: str
    pythonExecutable: str
    venvPath: str
    packages: list[str]
    maafwRequirement: str | None
    maafwVersion: str | None
  agents:
    projectPath: str
    plans: list[MaaFWAgentCommandPlan]
    preparedVenvs: list[str]
    skipped: list[str]
    messages: list[str]
```

`prepare_project_environment()` 是安装/升级后可直接调用的完整预热入口。它与实际
运行调用同一个 `prepare_environment()`，因此 canonical requirement selector、
Python ABI 和 Runtime Pool fingerprint 完全一致；项目名、PI 版本等元数据不会
制造另一套 runtime。预热只持有短期独立 lease，并在成功或失败的 `finally` 中
释放；后续实际运行重新取得同一个 `runtimeId` 的执行 lease，已安装依赖不会重装。

预热 `progress(event)` 始终含 `stage`、`status`、`message`，可选 `percent` 和
`runtime_id`。阶段依次为 `resolving`、`runtime_check`、仅首次创建时出现的
`creating_runtime`/`installing_runtime`、`runtime_ready`、`preparing_agents`、
`completed`；失败终态为 `failed` 且不伪造百分比。`runtime_ready.status` 为
`created` 或 `reused`。Agent 阶段映射到总进度 75–95%，附带 `agent_percent`、
`completed`、`total`；完成态为 100%。依赖安装器目前没有可信字节级回调，因此
Runtime 只报告确定阶段里程碑，不虚构下载字节或细分安装百分比。

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

### 11.1 `maafw.configuration_reuse.v1`

普通 MaaFW 项目向导与新增用户页通过以下插件 HTTP 端点使用配置复用控制器：

```text
POST /plugin/maafw/config-reuse/sources
POST /plugin/maafw/config-reuse/plan/external
POST /plugin/maafw/config-reuse/plan/copy
POST /plugin/maafw/config-reuse/apply
```

外部来源必须由用户显式选择，当前识别 MFAAvalonia 旧 `instances/*.json`、
MFAAvalonia 与 MFW/CFA 共用的 `multi_config.json + configs/*.json`，以及 MXU
`mxu-*.json`。导入计划按当前 ProjectInterface 归一化 controller、resource、
游戏路径、ADB/Win32 控制参数、
任务顺序与 option；外部模拟器名称、路径和实例索引不冒充宿主稳定 UUID，向导
会把 ADB 提示带到下一步要求用户确认宿主模拟器/实例。旧窗口句柄也不会复制。

计划只向前端返回摘要、人工动作与脱敏 orphan，不暴露待写入配置。应用前重新
校验来源 fingerprint、脚本配置 hash、用户集合 hash 以及内部来源用户 hash；
创建用户、写入用户配置和最后写入脚本配置均在
`Config.script_config_transaction()` 内完成。新增用户导入不会覆盖脚本级项目
绑定；复制内部用户只复制 `Info/Task/Notify` 业务配置，并重置 `Data`、journal、
lease 与资源引用。计划有效期为 30 分钟。

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
automas-maafw-runner >= 0.4.0
automas-script-maafw >= 0.1.12
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

    prewarm = runner_service.prepare_project_environment(
        project_path,
        interface,
        install_agent_dependencies=True,
        send_log=print,
        progress=lambda event: print("env progress", event),
    )
    print("prepared runtime", prewarm["runtime"]["runtimeId"])

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
    project_id=None,
    version=None,
    *,
    runtime_constraint=None,
    platform=None,
    arch=None,
    runtime_binding=None,
    remote_source=None,
    reference=None,
    pinned=False,
    activate=True,
) -> dict

update_project(source_path, project_id, version, **kwargs) -> dict
resolve_project(project_id, version=None, *, touch=True) -> dict
checkout_project(project_id, version, script_id) -> dict
list_projects() -> list[dict]
list_versions(project_id) -> list[dict]
inventory() -> dict
storage_info() -> dict
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

本地首次导入的 `project_id` 可省略。Project Store 按以下层级确定不可变项目身份：
优先使用 ProjectInterface 的正式 `projectId`/`project_id`（二者同时声明时必须一致）；
若未声明正式 ID，再使用调用方显式 `project_id` 作为兼容 alias；仍未提供时依次
回退到 ProjectInterface 的 `name` 和项目目录名。调用方显式 ID 与正式 ID 不一致时
拒绝导入，所有解析结果仍经过项目 ID 的安全组件校验。

显式 `version` 与 `ProjectInterface.version` 同时存在时必须语义等价；最终使用
ProjectInterface 的原始版本拼写。未显式给版本时必须能从 ProjectInterface
推断，否则拒绝导入。

Managed 远程导入可传入不含凭据的 `remote_source`。Project Store 只接受
GitHub repo/tag/asset selector 或 MirrorChyan RID/multiplatform 身份，将其写入
私有不可变 manifest 并通过 `summary.remote` 返回。包内 ProjectInterface 的同名声明
优先，调用方身份只补齐缺失字段；同一项目版本若以不同远程身份再次导入则拒绝。
CDK、token 和未知字段不属于该接口，不能进入 Store。

MaaFW 约束优先使用调用方、ProjectInterface 或 requirements 的有效声明；均未钉
版本时，可从发行包内某个 `MaaFramework.dll` 自身唯一的静态 `vX.Y.Z` 标记推导
精确 `==X.Y.Z`。服务不得加载项目 DLL；含多个历史版本字符串的 DLL 不作为
版本证据，临时/更新目录和 pip `~*` 卸载残留也必须忽略。显式约束不包含唯一
推导版本或多份唯一二进制版本不一致时失败关闭。

从 Project Store 0.2.2 起，私有 manifest 的 `runtime.python` 中保存
`implementation=cpython`、硬版本 `constraint` 与 `sources`。优先读取
ProjectInterface 的显式声明；脱壳项目没有声明时，只静态读取被剥离解释器同目录
唯一的 `python3XY._pth`，或 Windows 发行包根/声明解释器目录唯一的
`python3XY.dll` 推导 `==3.X.*`，不执行项目代码。显式约束与 marker 冲突、
多个 minor marker 或实现不是 CPython 时失败关闭。该元数据参与 projected source
hash，避免 CP312/CP313 发行包被误判为同一不可变版本。

`resolve_project()` 至少返回：

```text
projectId
version
dataPath
storeId
runtimeConstraint
manifest
summary
```

`summary` 是供本地资源管理界面消费的 JSON 摘要，包含 `interfaceVersion`、`remote`、
`sourceKind`、`runtimeConstraint`、`pythonConstraint`、`pythonImplementation`、
`agents`/`agentCount`、`capabilities`、
`shells`、`size`、`flags` 和 `warningCount`。`shells` 记录被剥离壳的类别与路径；
`size` 同时给出输入、原树、最终投影与节省字节数。

`resolve_project().dataPath` 是不可变 Store payload，内含根级
`interface.json[c]` 与私有 `.auto_mas_maafw_project.json`；Runner 不得向该目录写
`debug/`、`logs/`、`temp/` 或配置。实际运行必须调用
`checkout_project(project_id, version, script_id)`，它在独立 RunRoot 中按
`storeId + projectId + version + sourceHash + payloadHash + scriptId` 完整复制并原子发布可写副本，
不使用 hardlink，也不复制私有 Store manifest。同一身份重复解析会复用 checkout 并
保留运行产物；marker、接口或身份损坏时失败关闭且不覆盖原目录。Managed 0.3.1
无条件要求 Project Store 提供 checkout，并要求稳定 `scriptId`、`runRootId` 和
`payloadHash`；缺少任一能力或身份都拒绝运行，绝不回落到不可变 Store 路径。

导入完成时还会为实际投影后的 Store payload 计算独立 `store-payload` SHA-256；
checkout identity 同时绑定该 `payloadHash`，复制前后都重新校验。Store 文件被外部
修改、复制期间发生变化或旧 manifest 缺少可验证 payload hash 时均拒绝发布运行目录，
必须以新不可变版本重新导入。

Project Store 插件实例的 `Root` 和 `RunRoot` 均可配置为空或绝对路径；为空分别使用
`data/maafw_project_store` 和 `data/maafw_project_runs`。两棵路径树不得相交，非空
自定义目录必须带有效 marker 或保持为空。`storage_info()` 返回实际路径、稳定
`storeId`/`runRootId` 和默认位置标记；服务存活期间 marker 身份变化会失败关闭。
脚本配置持久化 `StoreId`，目录切换后不能把同名 `projectId/version` 当作原绑定；
旧绑定只在不可变 source hash 完全一致时兼容确认。

`inventory()` 返回 `complete/items/checkouts/errors`，不会把损坏 manifest、异常
Store 结构、损坏 checkout 或已失去 Store 版本的孤儿 checkout 静默藏掉。统一资源页
可在没有任何脚本记录时调用无 `scriptId` 的
`POST /plugin/maafw-managed/inventory`，查看 Project Store、RunRoot、Runtime Pool、
全部版本、引用、租约和孤儿资源；该全局接口只读，不提供无脚本上下文的删除旁路。

`POST /plugin/maafw-managed/gc` 接受可选的 `scriptId`：非空值保持指定托管脚本上下文；
缺失、空字符串或空白值则执行全局 GC，不要求仍存在任何 Managed 脚本记录。`dryRun`
默认 `true`，只生成预览而不删除；只有 `dryRun=false` 且 `confirmation` 精确为
`DELETE UNUSED` 时才允许实际回收。全局 GC 与任意脚本上下文 GC 在服务端双向排他，
冲突返回活动操作而不能由客户端进度或 session 状态绕过。两条路径都在同一个
Project Store 生命周期事务和宿主配置写锁内完成全量引用对账与回收，并继续受
`refs`、`pin`、`lease`、完整盘点和失败关闭保护。

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

### 15.1 普通 MaaFW 原地转换

`GET`/`POST /plugin/maafw-managed/capabilities` 不需要 `scriptId`，返回
`apiVersion`、`distributionVersion` 和宿主门控的 `features`。只有宿主同时
提供 storage 态快照和原子换型 API 时，`inPlaceConversion` 才为 `true`。

`POST /plugin/maafw-managed/convert` 只接受现存普通 `MaaFW` 的 `scriptId`，
项目目录从该记录的权威 `Info.Path` 读取，不能由 HTTP payload 改写。插件先在短
`Config.script_config_transaction()` 中读取 source storage 快照并释放宿主全局写锁，
快照可选地提供宿主锁定状态 `scriptLocked: bool`；缺少该字段的旧宿主按
`false` 兼容。若字段为 `true`，插件在任何 source path reservation、Project Store
资源事务或项目导入前立即返回 HTTP 400、`errorCode: "script_locked"` 和
“脚本正在运行或配置被占用，暂时无法转换”，不得调用导入或取得资源锁。随后
再按“Project Store 资源生命周期锁 → 单脚本锁”进入资源阶段，短暂复核 snapshot
未变化后完成 interface 识别、非活动的不可变导入和稳定引用
`maafw-script:<scriptId>`；转换不会改写 Project Store 的全局 current，托管绑定始终
使用记录中的确切 projectId/version。进入任何 interface 读取或导入前，转换还必须
取得普通 MaaFW 运行/准备/更新共用的 source path fail-fast 预留；冲突则拒绝转换，
并在资源导入、配置提交或补偿完成后才释放；
最终只在上述资源锁内取得第二个短宿主事务并调用
`Config.convert_plugin_script_type()` 原地替换整条宿主记录；禁止通过 add/delete
或复制脚本模拟转换。

宿主在最终 CAS 提交阶段仍必须自行复核脚本锁；若锁竞争发生在插件预检之后，插件
保留该宿主兜底，并将可识别的锁拒绝归一为相同的 `script_locked` 错误，而不报告为
无信息的 500。

宿主以加密 storage 快照做 CAS，并在一次原子 `ScriptConfig.json` replace 中更新
父记录和全部用户的 `PluginTypeKey`/目标配置。脚本 UUID、用户 UUID/order、名称、
普通 MaaFW 用户配置和运行历史保持不变。journal 只保存 operationId、hash、项目
身份和 `project_imported`/`committed` 状态，不保存解密后的用户配置；确切目标
storage artifact 由宿主整体加密保护。operationId 同时绑定 source snapshot 与目标
projectId/version/runtimeConstraint：相同目标重试复用完全相同的 storage artifact，
改变目标身份则得到不同 operationId。明确未提交的失败会释放项目引用；提交结果
不确定时保留引用与 journal，供同一 operationId 幂等恢复。缺少任一宿主原语时
capability 为 false，转换动作失败关闭。

`MaaFWManaged` provider 自身标记为 `creatable=false`，不出现在创建/复制入口；
其 `editor_kind` 复用 `plugin:automas_script_maafw`，因此托管前后仍进入同一个 MaaFW
Vue 编辑页和统一资源管理器。

### 15.2 `MaaFWManaged` 资源升级事务

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

### 15.3 统一管理动作进度

当 capability `features.operationProgress=true` 时，统一资源管理器的 convert、
本地/远程导入与升级、版本切换/应用/取消、runtime 安装/删除、项目版本删除、pin 和
GC 动作必须携带新的 `progressId`。`POST /plugin/maafw-managed/progress` 使用
`{scriptId, operationId}` 读取有界内存快照；二者必须与创建操作时完全匹配，禁止
跨脚本读取或复用已用 ID。

进度 DTO 至少包含 `operationId`、`scriptId`、`operation`、`status`、`stage`、
`message`、`percent`、`downloadedBytes`、`totalBytes` 和有界 `logs`。相同快照以
`maafw.managed.progress` 类型 best-effort 发布到 `id=scriptId` 与
`id=operationId` 两个 WebSocket 通道；断线或 Publisher 不可用不得影响资源事务。
远程下载百分比映射为完整操作的子区间，并透传下载器真实字节数；没有内部回调的
Project Store/Runtime Pool 只发布真实阶段边界。外层操作在全部资源/配置锁退出后
恰好写入一次 `success` 或 `error` 终态，迟到的内部回调不能覆盖终态。请求 task
被取消时不得提前释放锁：同步线程 mutation 与受保护的 inner operation 都必须真正
结束（包括转换提交/补偿），再按真实结果写入终态并向上重抛 `CancelledError`。因此
请求取消后实际成功的动作仍是 `success`；只有 inner operation 自身被取消时才写入
“操作已取消”的 `error`，且缓存不能永久留在 `running`。

当 `activeOperationLookup=true` 时，前端必须以
`POST /plugin/maafw-managed/operations/active` 的 `{scriptId}` 结果为权威状态，不把
`sessionStorage` 当作锁。返回值含非空 `serverEpoch` 和当前 `activeOperation`；页面
重载或 409 冲突后校验 epoch/scriptId 并接管真实进度。后端在同一锁内登记操作、
写入终态并释放 active 槽，同一个 `scriptId` 的远程检查和所有 mutation 服务端排他；
插件停止时先进入 draining、拒绝新操作并等待已登记任务越过资源/配置事务和终态。

## 16. `maafw.runtime_pool.v1`

```python
list_runtimes() -> list[dict]
inventory() -> dict
storage_info() -> dict
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
完整依赖集合的字典。Runtime Pool 0.2.0 还接受可选 Python 约束：

```json
{
  "requirements": ["maafw==5.12.2", "json5==0.12.1"],
  "python": {"implementation": "cpython", "constraint": ">=3.13,<3.14"}
}
```

当前受管 minor family 为 CP312 与 CP313。`resolve_runtime()` 只查找已经可用的
解释器/runtime，不触发下载；`ensure_runtime()` 可以通过 uv 在 pool 的 `python/`
目录准备缺失的受管解释器。结果至少包含：

```text
runtimeId
poolId
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

显式多 ABI runtime identity 由完整规范化 requirement selector 集合、选中的
Python ABI、实际 patch 版本、操作系统和架构组成，不含项目路径；精确
`==/===X.Y.Z` 约束会让 uv 查找或安装对应 patch，不能复用同 minor 的其他 patch。
不带显式 Python identity 的宿主 selector 也记录完整实际 patch；宿主与显式解释器
的 ABI、patch、平台、架构和完整 requirement selector 全部一致时复用同一
runtime。`resolvedRequirements` 是安装后的 `pip freeze --all` 审计
快照，不参与 identity；因此范围 selector 对应的 runtime 删除后重建时，可能解析
到范围内更新的依赖版本。包含本地路径、editable 或递归 requirements 的依赖不能
安全跨项目共享，服务会拒绝创建共享 identity。删除与 GC 会保护固定、引用和活动
lease。

Runtime Pool 插件实例的 `Root` 可留空或配置绝对路径；留空使用
`config/maafw_runtime_pool`。根目录带稳定 `poolId` marker，未知的非空自定义目录、
重解析点、运行期 marker 换身以及损坏 runtime 均失败关闭。`inventory()` 通过
`complete/items/errors` 显式报告损坏条目；Managed 在任何真实 GC 删除开始前同时
预检 Project Store、checkout 与 Runtime Pool，盘点不完整时整次拒绝删除。

每个 ABI 解释器族可为多个 runtime 提供建 venv 的基础解释器，但每个完整
requirement selector + 解释器 identity（含显式路由的实际 patch）仍对应一个独立
venv；环境之间不共享
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
runtime_requirements=None
runtime_id=None
runtime_pool_id=None
runtime_python_constraint=None
lease_owner="automas-maafw-runner"
lease_ttl_seconds=86400
progress=None
```

托管项目未显式传参时，Runner 会读取项目根
`.auto_mas_maafw_project.json` 的 `runtime.constraint` 和
`runtime.binding.runtimeId`。binding 必须与当前 selector identity 一致；托管项目
既无 binding 又无版本约束时会拒绝运行，不会静默安装 `latest`。已绑定 runtime
丢失但 binding 记录了 `maafwVersion` 时，Managed gateway 会按精确
`maafw==<version>` 重建环境并持久化新 binding。

Runner 0.4.0 的可信 Managed 路由同时传入完整 `runtime_requirements`、`runtime_id`、
`runtime_pool_id` 与 Store `runtime.python.constraint`。Runner 必须校验当前 Pool
marker、runtime manifest 的完整 selector 和 Python identity；校验成功后直接复用并
租用该绑定，不得用 AUTO-MAS 宿主的 CP312 身份重算 CP313 runtimeId。缺字段、
selector 不一致、Pool 换身或 Python 约束不匹配均失败关闭。普通 MaaFW 仍由相同
Pool 服务解析默认 selector。

Managed 0.3.1 另提供 `maafw.managed.environment.v1`：

```python
await prepare_script_environment(
    script_id,
    requested_path,
    *,
    send_log=None,
    progress=None,
) -> dict | None
```

宿主按 `scriptId` 调用该服务。服务在 Project Store 生命周期事务和宿主脚本配置
事务中重读权威 `MaaFWManaged` 记录，解析 Store checkout、校验/持久化 runtime
binding、预留项目路径，再调用 Runner 的同一完整 selector 路由做预热；普通
`MaaFW` 返回 `None`。该流程不会启动 Agent、controller 或游戏，短 lease 和路径
预留在结束时释放，因此准备页建立的环境就是首次真实运行会重新租用的环境。

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
否则继续使用隔离环境。托管项目的 `project_binary` Agent 使用同一完整
`runtimeId` 选定的 MaaFW native runtime：Runner 会在可写 checkout 的
`maafw/` 下准备带哈希标记的 overlay，递归投影 `maa/bin`（含 `plugins/`）和
`MaaAgentBinary` 资产，并优先保留项目自带 runtime；只加 PATH 不作为 native
Agent 的替代方案。显式外部解释器和非托管项目保持原行为。

`MaaFWManaged` 在实际 GC 前从全部脚本配置对账 `maafw-script:*` 项目引用，并从
现存项目 binding 对账 `maafw-project:*` runtime 引用。dry-run 不删除任何目录，
但也会修正项目引用 manifest。当前项目版本始终受保护；应先切换到其他版本，再
删除旧版本。资源动作同时提供项目版本和 runtime 列表，供统一 MaaFW 资源管理器
完成选择、切换与删除。HTTP 动作与运行 Hooks 通过同一个 Project Store 服务
实例共享资源生命周期事务；各路径按需取得锁时，顺序固定为“资源生命周期 →
单脚本升级 → 宿主配置事务 → 运行锁”，因此引用快照、对账和回收不会与
stage/apply/prepare 交错。原地转换的只读 source snapshot 是特例：它先单独取得并
释放短宿主事务，随后资源导入与最终 CAS 提交仍严格按“资源生命周期 → 宿主配置”
顺序执行，且不会在全局配置锁内做 Project Store I/O。
Managed 0.3.1 在 Project Store 缺少该 Python 协调接口时失败关闭，不提供无锁
兼容路径；它同时要求宿主提供 `Config.script_config_transaction()`、
`Config.script_config_write_scope()` 和 ScriptConfigStore
`write_transaction()`。原地转换还要求
`Config.get_plugin_script_type_conversion_snapshot()` 与
`Config.convert_plugin_script_type()`；宿主事务和原子换型改动合入前不得启用或
发布。

Managed 的远程检查把“发现新版本”与“存在可安装候选”分开；只有候选带有效
下载地址时才允许导入/升级。下载发生在宿主配置事务之外，校验后的本地 ZIP 再
进入 Project Store 的不可变导入和既有升级计划流程。首次导入可使用用户填写的
`ImportProjectId` 作为显式 alias；未填写时由 Project Store 按 ProjectInterface 正式
ID、`name` 和目录名分层解析。绑定后升级只信任 Project Store manifest
和活动 ProjectInterface。下载 URL（可能包含短期签名或凭据）不会写入脚本配置，
持久化发现结果只保留来源、版本、hash 与是否可下载。

远程 ZIP 只作为本次请求的 staging：Project Store 导入/升级计划与宿主配置事务均
成功后，Managed 通过 Project Update 0.2.3 的 `release_download_package()` 释放
该内容寻址包。后续 apply、cancel 和启动恢复只解析不可变 Store 版本，不依赖 ZIP。
`ManagedRemote.LastDownload` 仅保留来源、版本、大小、SHA-256、`retained` 和
`cleanupStatus`，不保存本地 staging 路径；远程首次导入也会把
`Managed.SourceArchive` 保持为空，本地手选 ZIP 的路径行为不变。释放失败只告警并
持久化 `retained=true`，不能把已完成的资源导入改判失败。下载观察元数据
`ManagedRemote` 不参与升级配置 CAS，真实脚本/任务/用户配置变化仍会使计划失效。

Managed 管理 HTTP 与自动更新都只使用宿主全局 `Update.Source` 和
`Update.MirrorChyanCDK`；请求及旧脚本字段仅兼容接收，不能覆盖全局值。全局来源为
AutoSite/CNB 时远程 Managed 操作失败关闭，必须先保存 MirrorChyan 或 GitHub。
每个项目的 stable/beta 渠道只由 `/maafw-managed/settings` 写入并由远程检查/下载读取，
首次导入也不得从请求回写渠道。绑定后的 repo/RID/tag/asset selector 只取活动
ProjectInterface 或 Project Store 的不可变 `summary.remote`。GitHub 路径从不读取或
携带 CDK。CDK/token/Authorization、Bearer 和带签名下载地址在 HTTP、WebSocket
进度、日志及持久化 DTO 中统一脱敏；全局没有 CDK 时仍可发现 MirrorChyan 版本元数据，
但没有可安装 URL 就不能执行导入或升级。

普通脚本 0.1.12 的配置复用同样依赖上述宿主事务 API。M9A 集成下限必须为
`automas-maafw-runner>=0.4.0` 与 `automas-script-maafw>=0.1.12`。

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
