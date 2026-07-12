# Agent Instructions

本仓库维护 AUTO-MAS 的通用 MaaFW 插件工作区。

- `packages/` 下每个目录都是独立 Python distribution，包名和公开 entry point 不得随意变更。
- 通用 MaaFW 包不得依赖 M9A、MaaEnd 等具体项目适配包。
- 跨插件服务边界优先传递 JSON 兼容字典或稳定 DTO，不直接依赖另一插件创建的 Pydantic 实例身份。
- 修改包代码时同步更新对应测试；修改发行契约时提升受影响包版本和内部最低依赖。
- 提交前运行 `python -m unittest discover -s tests -v` 和 `python scripts/build_all.py`。
- 不提交 `dist/`、`build/`、`*.egg-info`、虚拟环境或 Python 缓存。

