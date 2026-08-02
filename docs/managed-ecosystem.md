# MaaFW 托管生态原型

## 目标

内部的 `MaaFWManaged` 用 AUTO-MAS 的统一 MaaFW Vue 编辑页和资源管理器替代
项目自带的 MFAAvalonia、MXU、MFW 等 UI 壳。项目导入后只保存 ProjectInterface、资源
Bundle、Agent 和无法安全重建的项目文件；MaaFW 与 Python 依赖由共享运行时池
按版本解析。

用户创建时只看到一个 `MaaFW` 类型。它默认直接运行用户自行维护的完整项目目录；
用户确认后可保持原 script/user UUID 与配置，原地转成由 AUTO-MAS 管理安装、
更新、切换和回收项目版本的内部 `MaaFWManaged` provider。

## 三层职责

1. `maafw.project_store.v1`
   - 将本地目录或已解压发行包导入不可覆盖的 `project/version` 投影目录。
   - 发现项目根或 `assets` 下的 `interface.json[c]`。
   - 保持 `resource.path` 和 `attach_resource_path` 的顺序及 Bundle 目录层级。
   - 管理 current 版本、引用、固定、删除和陈旧版本 GC。
2. `maafw.runtime_pool.v1`
   - 用规范化 requirement selector、Python ABI、操作系统和架构生成身份。
   - 相同身份共用一个隔离环境；不同 MaaFW 版本不会混用 site-packages。
   - 安装后记录 `pip freeze --all` 的 `resolvedRequirements` 供审计。
   - 管理引用、运行租约、固定、显式删除和带宽限期的 GC。
3. `MaaFWManaged`
   - 通过 `ScriptAdapterDefinition` 注册为不可创建类型，并复用普通 MaaFW 的
     Vue editor；创建向导和项目列表仍只暴露一个 MaaFW 入口。
   - 先用短宿主事务读取转换快照，释放全局配置锁后导入资源，再在资源锁内用第二个
     短事务调用原子换型 API；不通过新增/删除脚本模拟转换。
   - 运行前解析项目版本及 runtime binding，再复用现有 MaaFW hooks、runner、
     controller 和 Agent 环境服务。
   - 统一资源管理动作提供 HTTP 轮询与 WebSocket 双通道阶段进度；远程下载透传真实
     字节数，Project Store/Runtime Pool 只报告可验证的外层事务边界。

## 资源投影边界

MaaFW 的安全最小资源单位是 Bundle，而不是单个 Pipeline JSON。首版采用保守
投影：

- 保留 ProjectInterface、递归 import、languages 及其引用的本地元数据；
- 完整保留每个 `resource.path` 与 controller `attach_resource_path` 指向的目录；
- 保留 Agent 入口、源码、requirements/lock、项目专用二进制扩展和旁加载文件；
- 保留项目原生插件目录；
- 排除明确的 UI 壳、MaaFW runtime、嵌入式 Python、更新器、日志、缓存和临时目录。

检测到 `Custom`、`Command`、二进制 Agent 或其他无法静态分析的边界时，清单会
标记为 conservative/opaque，并扩大保留范围。原型不会对 Bundle 内部做图片级
可达性裁剪，因为 Pipeline override 和 Agent 可能在运行时动态引用文件。

过滤后的资源内容不再匹配上游 `resource.hash`。导入器不会冒用原哈希；需要在
选定 MaaFW runtime 下重新计算时，应记录新哈希及对应 runtime 版本。

## 运行时路由与共用依赖

ProjectInterface 没有 MaaFW 版本与 Python ABI 字段，因此每个托管版本另有私有
manifest。至少记录：

- 来源与内容哈希；
- ProjectInterface 路径和项目版本；
- MaaFW requirement selector、安装后的解析审计及精确 `maafwVersion`；
- 操作系统、架构、Python ABI 和 Agent 运行方式；
- runtime binding、引用、固定与最后使用时间；
- 已复制、已排除和因不透明边界扩大保留的项目。

共享粒度是“相同的规范化 selector identity”。`resolvedRequirements` 不参与
identity；范围 selector 对应的 runtime 被删除后重建时，可能解析到范围内更新的
版本。不同 selector 不会直接共用一个 venv，以免升级一个项目时破坏另一个项目。
下载缓存可以共用；跨版本 site-packages 文件级去重不属于首版保证。

Python Agent 只有在私有 manifest 明确标记
`runtime.sharedAgentDependenciesComplete: true` 时才复用共享 worker。该标志仅在
根 `requirements.txt` 能平面、完整表达依赖时生成；存在嵌套 requirements、lock、
本地/URL 依赖或其他不确定来源时继续使用隔离 Agent 环境。

运行时选择顺序为：

1. 托管项目版本已持久化的 runtime binding；
2. binding 丢失时记录的精确 `maafwVersion`，重建后写回新 binding；
3. 私有 manifest 或项目 requirements 中的 MaaFW requirement selector；
4. 无法确定时拒绝静默选择 `latest`，要求用户明确版本或先做兼容探测。

范围 selector 首次解析后由精确 runtime binding 固定；日常执行不会每次漂移到
范围内的新版本。

## 删除和自动回收

项目版本与 runtime 都只能删除各自 store root 下的托管目录。删除和 GC 必须保护：

- 当前脚本引用的版本/runtime；
- 用户显式固定的对象；
- 正在运行的租约；
- 宽限期内最近使用的对象；
- Project Store 为每个项目保留最近 N 个版本；Runtime Pool 另保留全局最近 N 个
  未被其他保护条件覆盖的环境。

GC 前会从全部 `MaaFWManaged` 脚本对账 `maafw-script:*` 项目引用，再从现存项目
binding 对账 `maafw-project:*` runtime 引用。GC 默认先提供 dry-run 结果；dry-run
不删除目录，但可能修正引用 manifest。当前项目版本始终受保护，必须先切换版本
才能删除旧版本。外部项目目录、外部 Python 和项目自带但未导入的 runtime 从不
在自动删除范围内。

## 实包验证

使用工作区 `M9A-win-x86_64-v4.5.0`（ProjectInterface 版本 `v4.5.1`）验证：

- 原发行目录：456,418,672 B；投影目录：74,325,832 B；节省 83.72%；
- 保留 `agent/bootstrap.py`、资源 Bundle、requirements 与任务脚本；
- 移除嵌入 `python/python.exe`、`MaaFramework.dll`、UI/更新器/缓存目录；
- 清单记录被剥离解释器与保留入口，并判定该包的根 requirements 足以安全共享
  Python Agent 依赖。

## 首版限制

- 统一资源管理器承载导入、列出/切换版本、更新、删除、固定和 GC；任务配置仍由
  MaaFW Vue 编辑器保存 `TaskSnapshot` JSON。
- 远程目录发现与发行包下载继续复用 `maafw.project_update.v1`，Project Store
  负责对下载完成的目录做安全投影和版本提交。
- Runner 会在托管 `dataPath` 下重新创建 `debug/`、`logs/`、`temp/` 并写入
  `config/maa_option.json`。首版保证 Store 不覆盖投影 payload，不承诺执行目录
  在运行期间完全只读。
- 项目和 runtime lease 至少覆盖 24 小时，配置更长运行时限时会延长，但首版没有
  heartbeat。完整卸载仅剩的 current 项目版本尚未做成声明式动作。
- 没有有效 ProjectInterface 的 Python 项目不应伪装成 MaaFW Bundle；它们需要
  专用脚本适配器。
