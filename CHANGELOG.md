# Changelog

## Unreleased

- 将通用 MaaFW 插件从 AUTO-MAS 主仓迁移到独立工作区仓库。
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

## Current package versions

- `automas-maafw-interface`: 0.2.0
- `automas-maafw-agent-env`: 0.1.1
- `automas-maafw-controller-adb`: 0.1.0
- `automas-maafw-controller-win32`: 0.1.1
- `automas-maafw-project-update`: 0.1.1
- `automas-maafw-project-store`: 0.1.0
- `automas-maafw-runtime-pool`: 0.1.1
- `automas-maafw-runner`: 0.3.1
- `automas-script-maafw`: 0.1.7
- `automas-script-maafw-managed`: 0.1.4
