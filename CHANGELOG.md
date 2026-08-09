# Changelog

## Unreleased

- 将通用 MaaFW 插件从 AUTO-MAS 主仓迁移到独立工作区仓库。
- Script MaaFW 0.1.13 将普通项目更新、Agent 环境预热、进度 WebSocket 与配置
  复用 transport 收敛到插件通用 gateway，并用 Runtime Pool 身份绑定的 sidecar
  复用已验证环境；宿主只保留通用桥接。
- Runtime Pool 0.2.0 接收宿主通过 `AUTO_MAS_UV_INDEX_URL` 提供的首选镜像，
  同时保留用户显式设置的 `UV_INDEX_URL`/`UV_DEFAULT_INDEX`；并统一规范化
  pool 内 Python 与 uv cache 路径，拒绝越出所属 pool 的路径。
- Project Update 0.2.3 在取消归档应用时等待后台工作线程结束后再传播取消，
  避免请求结束后仍有后台写入跨越项目更新生命周期。
- Project Store 0.2.2 在脱壳时从 ProjectInterface 或唯一的
  `python3XY._pth` 记录 `runtime.python` 硬约束，并把该元数据纳入不可变来源
  身份；显式约束与打包解释器冲突时失败关闭。
- Runtime Pool 0.2.0 支持 CP312/CP313 解释器族。每个 ABI 可复用已配置、宿主或
  pool-local uv-managed Python，每个完整规范化依赖 selector 仍使用独立 venv，
  仅共享 uv 下载/解包缓存与 hardlink 文件。
- Runner 0.4.0 校验 Managed 提供的完整 selector、`poolId`、`runtimeId` 与 Python
  约束后复用可信绑定，不再用宿主 Python ABI 重算跨 ABI runtime；准备与实际运行
  共用同一条路由。Agent Env 0.1.4 同步识别共享 runtime，并原子写入兼容 shim。
- Script MaaFW 0.1.11 强制使用已注册 Runtime Pool 的实际 root/poolId；Managed
  0.3.0 新增 `maafw.managed.environment.v1`，按 `scriptId` 在不启动 Agent、controller
  或游戏的前提下完成 Store 解析、绑定与精确预热。
- Project Store 0.2.0 支持从本地目录或 ZIP 安全导入不可变资源版本；可从
  `ProjectInterface.version` 推断版本，并记录 agent、能力、被剥离前端壳与体积
  摘要；新增进程内共享资源生命周期事务，供 Managed 串行多调用引用对账、
  版本绑定与 GC。
- Runtime Pool 0.1.4 让不同依赖选择器保持独立 venv，同时通过 pool 内 uv cache
  和 hardlink 复用下载及解包内容；实际 GC 交由 `uv cache prune` 清理过时缓存，
  dry-run 只返回统计和待执行命令。
- Managed 0.2.0 改为纯本地资源管理：支持目录/ZIP 导入、版本升级/切换/删除、
  脚本与 runtime 引用保护及能力查看，不再下载远程发行版。升级先导入非活动
  版本，再为脚本和全部用户持久化带 `planId` 的 pack 计划；确认时校验资源、
  配置和用户集合后应用同一份计划，最后才切换资源。中断会回滚，人工动作、
  规划失败或过期计划都保持旧资源生效，已安装版本也不能绕过该流程。共享
  生命周期事务会串行资源引用、运行绑定、版本切换与 GC，避免并发对账误删
  新导入版本；运行时安装只接受脚本当前已保存的资源绑定，旧 Project Store
  缺少事务能力时拒绝降级到无锁执行。首次导入输入与已绑定项目身份分离；
  绑定后只认不可变 Project Store manifest 中的 `projectId`/`version`，
  表单身份改为只读并自动清空首次导入字段。
- Project Update 0.1.3、Runner 0.3.3 与 Script MaaFW 0.1.9 修复异步更新阻塞、
  Runtime Pool 环境准备、外部项目路径锁与首次运行状态衔接问题。
- 兼容跨插件来源的 Win32 controller Pydantic 模型。
- 为 MaaFW 脚本类型提供插件内置图标。
- 将 MaaFW controller、resource 与设备配置收敛至脚本级，用户配置不再覆盖。
- 新增托管 MaaFW 项目原型：以版本化 Project Store 只保存脚本资源 Bundle，
  并通过声明式 `MaaFWManaged` 脚本类型替代项目自带 UI 壳。
- 新增按规范化 requirement selector、Python ABI、平台和架构共享的多版本
  MaaFW Runtime Pool；安装后记录 `resolvedRequirements` 供审计，并支持固定、
  引用/租约保护、显式删除和陈旧环境 dry-run GC。
- MaaFW Runner 0.2.0 改为使用显式 runtime binding、活动 lease 与项目原生插件
  路径；仅依赖清单被判定为完备的托管 Python Agent 才复用共享 worker 环境。
- ProjectInterface 解析器 0.2.0 完整保留 PI v2.8.1 的 `setting` 分区，
  支持从 `import` 按协议顺序追加设置页，并新增 `hotkey` 字段、默认值、
  预设与预览数据。
- MaaFW Runner 0.3.0 会在构建 pipeline override 时，按当前 Win32/ADB
  controller 将人类可读快捷键转换为对应虚拟键码；缺失修饰键、歧义 controller
  或无法映射的按键会显式失败，不再静默输出无效占位符。
- MaaFW Runtime Pool 0.1.1 在创建共享 runtime 前探测引导解释器是否带 venv
  模块；便携包的 embeddable Python 不合格时改用 uv 兜底，并在写 manifest 前
  用新环境实测 ABI/Python 版本与 pool identity 对账。
- 托管 MaaFW 网关 0.1.4 把同步服务方法放到线程池执行，不再在宿主事件循环上
  内联跑 venv 创建、pip install 与整树哈希；`MaaFWManaged` 不再声明与
  `MaaFW` 相同的 legacy 配置类名。
- MaaFW Project Update 0.1.2 将“发现新版本”与“存在可安装候选”拆成独立
  契约；MirrorChyan/GitHub 未返回下载地址时保留版本发现信息，但不再返回
  可安装 candidate，也不会在自动更新流程中进入下载阶段。
- Agent Env 提升为 0.1.2，避免已发布但不含 embeddable Python fallback 的
  旧 0.1.1 wheel 满足依赖；Runner、Script 与 Managed 同步提升内部最低依赖，
  确保正式 dev_v2 不会解析到缺 Runtime Pool/Agent Env 修复的旧 wheel。
- MaaFW Runtime Pool 0.1.2 从 bootstrap Python 同级 `Scripts/uv.exe` 定位
  便携版 uv，不再依赖进程当前目录；普通 Runner 在当前 runtime lease 生效后，
  每个 pool root、每个进程执行一次受 pin/reference/lease、7 天宽限期和
  keep-latest 保护的过时环境清理，清理失败只告警、不阻断首跑。

## Current package versions

- `automas-maafw-interface`: 0.2.0
- `automas-maafw-agent-env`: 0.1.4
- `automas-maafw-controller-adb`: 0.1.1
- `automas-maafw-controller-win32`: 0.1.2
- `automas-maafw-project-update`: 0.2.3
- `automas-maafw-project-store`: 0.2.3
- `automas-maafw-runtime-pool`: 0.2.0
- `automas-maafw-runner`: 0.4.0
- `automas-script-maafw`: 0.1.13
- `automas-script-maafw-managed`: 0.3.2
