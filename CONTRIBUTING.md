# Contributing

1. 从默认分支创建功能或修复分支。
2. 只修改职责相关的 package，保持依赖方向从上层适配器指向底层 MaaFW 服务。
3. 新增或修复行为时补充测试。
4. 若 distribution 内容或依赖元数据变化，更新对应 `pyproject.toml` 版本。
5. 提交使用 Conventional Commits，例如 `fix(controller-win32): ...`。

本地验证：

```powershell
uv sync --all-packages --group dev
uv run python -m unittest discover -s tests -v
uv run python scripts/build_all.py
```

