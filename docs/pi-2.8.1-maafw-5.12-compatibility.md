# ProjectInterface 2.8.1 与 MaaFW 5.12 兼容矩阵

## 范围与来源

本报告区分三类来源：

- MaaFramework 官方文档和稳定 release 用于定义协议与公开 API。
- `reference` 下的项目源码/发行包只作为黑盒兼容样本，不成为运行时依赖。
- `plugins/automas-maafw` 是 AUTO-MAS 通用 MaaFW 能力的源码权威。

核对基线：

- AUTO-MAS 集成工作树仍固定 `maafw==5.8.1`。
- MaaFramework 当前核对的稳定版本为 5.12.2。
- 本地 `reference/MaaFramework` 停留在 5.11.1+1 附近，不能代替当前官方资料。
- M9A 稳定发行样本包含 MaaFW 5.10.4/5.11.1 与打包 Python；M9A `main`
  已声明 MaaFW 5.12.2 和 Python 3.13，但未发布源码不能冒充稳定发行包。

## ProjectInterface v2.8.1

| 能力 | 0.2.0/0.3.0 状态 | 证据 |
|---|---|---|
| `pretask` 单个/数组 | 已支持 | loader、run-plan、既有测试 |
| `pretask.controller/resource` | 已支持 | PI v2.8.1 过滤与测试 |
| imported `global_option` | 已支持 | 去重并保留首次出现顺序 |
| root/imported `setting` | 已支持 | 根声明优先，按 import 遍历顺序追加 |
| `setting.option` 引用校验 | 已支持 | 不存在的 option 会阻止加载 |
| `hotkey` 模型与预览 | 已支持 | `hotkeys` 字段、默认值和 preview 数据 |
| `hotkey` task snapshot | 已支持 | 始终保存可读字符串，不保存键码 |
| `hotkey` preset | 已支持 | `record<string,string>` 校验与归一化 |
| Win32 键码替换 | 已支持 | Windows Virtual-Key 整数 |
| ADB 键码替换 | 已支持 | Android KeyEvent 整数 |
| `.primary/.modifier1/.modifier2` | 已支持 | 缺失修饰键会显式失败 |
| WlRoots/MacOS 键码替换 | 暂缓 | AUTO-MAS Direct 当前不运行这些 controller |
| 前端设置分区与捕获控件 | 待宿主集成 | backend preview 已提供 `settings/hotkeys` |

`hotkey` 的 controller-specific 转换放在 runner 而不是 interface parser：
解析、配置存储和预设保持可读字符串，只有选定 controller 并生成 pipeline override
时才转换为整数。这避免把 Win32 键码错误复用于 ADB。

## MaaFW 5.8.1 → 5.12.2

| 变化 | 当前判断 | 处理 |
|---|---|---|
| Win32 新输入方式（含 Interception） | 可向后兼容，但宿主 UI 未暴露全部选项 | 建议升级后补设备配置枚举测试 |
| 相对鼠标移动等新 controller API | 现有项目不依赖 | 暂不新增宿主抽象 |
| Agent socket 析构等待修复 | 对多 Agent 稳定性有价值 | 宿主基础版本应升级到 5.12.2 |
| Batch OCR 缓存修复 | 对任务稳定性有价值 | 随基础版本升级获得 |
| 新 controller 类型 | AUTO-MAS Direct 明确只支持 Adb/Win32 | 保持显式错误，不伪装支持 |
| wait-freezes/action/recognition detail 扩展 | sink 对未知 raw notification 可容忍 | 升级前后各跑任务失败诊断测试 |
| Python binding 类型收紧 | 当前 DTO 以 int/JSON 边界传递 | 运行时 5.12.2 回归验证 |
| M9A Python 3.13 Agent | 脱壳后由 Store `runtime.python` 路由到 Pool CP313 | 禁止改用 AUTO-MAS Python 3.12 强行加载 |

## 多 ABI 托管运行约束

Project Store 0.2.2 会在移除发行包内置解释器前，从显式 ProjectInterface 声明或
唯一 `python3XY._pth` 静态固化 `runtime.python`；M9A 的 CP313 约束因此不会随
脱壳丢失。Runtime Pool 0.2.0 支持 CP312/CP313：每个 ABI 可复用基础解释器和 pool
内 uv 缓存，但每个完整规范化 dependency selector 仍有独立 venv，不共享
`site-packages`。

Runner 0.4.0 对完整 selector、`poolId`、`runtimeId` 和 Python 约束做一致性校验，
再复用 Store/Pool 的可信 binding；准备和真实运行使用相同 identity，不能以宿主
CP312 重算 CP313 runtime。M9A 集成包的发布下限必须同步为
`automas-maafw-runner>=0.4.0` 与 `automas-script-maafw>=0.1.11`。

## Reference 样本结果

已观察到以下样本可由当前 parser 解析：

- M9A interface 4.0.1：25 tasks、67 options、4 presets。
- M9A interface 4.5.0：25 tasks、67 options、4 presets。
- M9A interface 4.5.1：25 tasks、67 options、4 presets。
- Maa_bbb `assets/interface.json`：36 tasks、131 options、3 presets；导入时由
  project-store 将 `assets` 项目提升为规范项目根。
- MaaEnd 1.16.0-beta.1：41 tasks、353 options、1 setting、2 hotkey
  option；PI 解析、preview 与无启动 Win32 run-plan 均通过，实际默认按键已转换为
  Windows Virtual-Key 整数。
- MaaKes、MaaYYs、MaaFramework sample 以及其余完整 M9A/Maa_bbb 发行样本的
  无启动 run-plan 均通过。

完整扫描共发现 22 个 interface 根：20 个完整样本通过 PI + preview + no-launch
run-plan；两个 M9A 临时解压目录缺少自身声明的
`resource/tasks/preset/Daily.json`，因此按不完整快照失败，不属于兼容性回归。

这些结果证明解析与计划生成，不等价于真实游戏、controller、Agent 或资源执行通过。

## 收口优先级

### 必须

1. 将新 interface/runner wheel 集成到宿主并生成对应前端类型。
2. 实现设置分区和快捷键捕获 UI，禁止用户直接编辑整数键码。
3. 在 Pool CP313 的独立 selector venv 中验证 MaaFW 5.12.2，再更新宿主 pin、
   runtime lock 和 wheelhouse。
4. 对 M9A 稳定发行包执行 project-store → preview → run-plan → Agent command plan
   的无启动黑盒回归。

### 建议

1. 为 Win32 Interception 与 BackgroundManagedKeys 增加能力探测和 UI 描述。
2. 对 5.8.1 与 5.12.2 各跑失败回调、停止、Agent 退出和重复启动测试。
3. 在前端把 setting/global options 表示为共享值，避免多个 task 保存副本后漂移。

### 暂缓

1. MacOS、PlayCover、Gamepad、WlRoots 的 Direct controller。
2. 直接运行 M9A 未发布 `main` 源码。
3. 把 `reference` 中二进制包复制为正式插件源码或发布权威。
