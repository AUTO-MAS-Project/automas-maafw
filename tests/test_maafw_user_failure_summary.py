"""Focused contracts for MaaFW failure summaries and framework-log layering."""

from __future__ import annotations

import ast
import re
import threading
import unittest
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Any


RUNNER_TASK = (
    Path(__file__).parents[1]
    / "packages"
    / "automas_script_maafw"
    / "src"
    / "automas_script_maafw"
    / "runner_task.py"
)


def _load_failure_helpers() -> dict[str, Any]:
    source = RUNNER_TASK.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(RUNNER_TASK))
    wanted = {
        "_FRAMEWORK_UI_LOG_MAX_CHARS",
        "_ANSI_ESCAPE_RE",
        "_VERBOSE_FRAMEWORK_LOG_MARKERS",
        "_NATIVE_FRAMEWORK_LOG_MARKERS",
        "_FRAMEWORK_DEBUG_PAYLOAD_MARKERS",
        "_RAW_FAILURE_UI_LOG_MARKERS",
        "_NATIVE_FRAMEWORK_STATUS_RE",
        "_FRAMEWORK_COORDINATE_RE",
        "_clean_framework_output",
        "_should_forward_framework_log",
        "_failed_task_user_summary",
        "_task_display_name",
    }
    body: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in wanted:
                body.append(node)
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id in wanted for target in targets):
                body.append(node)
    future_annotations = ast.parse("from __future__ import annotations").body[0]
    module = ast.Module(body=[future_annotations, *body], type_ignores=[])
    namespace: dict[str, Any] = {
        "Any": Any,
        "Iterable": Iterable,
        "Mapping": Mapping,
        "Path": Path,
        "Queue": Queue,
        "datetime": datetime,
        "re": re,
        "threading": threading,
    }
    exec(compile(module, str(RUNNER_TASK), "exec"), namespace)
    return namespace


class _Task:
    def __init__(self, name: str, entry: str, label: str) -> None:
        self.name = name
        self.entry = entry
        self.label = label


class _Plan:
    def __init__(self, *tasks: _Task) -> None:
        self.tasks = tasks


class _Result:
    def __init__(self, failed_task: str | None) -> None:
        self.failedTask = failed_task


class MaaFWUserFailureSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.helpers = _load_failure_helpers()

    def test_failed_task_uses_i18n_display_label_and_hides_details(self) -> None:
        summary = self.helpers["_failed_task_user_summary"](
            _Result("Psychube"),
            _Plan(_Task("Psychube", "Psychube", "棱晶")),
        )
        self.assertEqual(summary, "棱晶：任务执行失败")

    def test_missing_failed_task_uses_first_plan_task_label(self) -> None:
        summary = self.helpers["_failed_task_user_summary"](
            _Result(None),
            _Plan(_Task("daily", "Daily", "每日任务")),
        )
        self.assertEqual(summary, "每日任务：任务执行失败")

    def test_only_confirmed_framework_failure_lines_are_hidden_from_ui(self) -> None:
        should_forward = self.helpers["_should_forward_framework_log"]
        self.assertFalse(should_forward("[MaaFW Tasker] 失败: entry=Psychube"))
        self.assertFalse(
            should_forward("任务失败，将继续后续任务: 棱晶: 任务执行失败: entry=Psychube")
        )
        self.assertFalse(should_forward("MaaFW 任务执行失败: entry=Psychube"))
        self.assertFalse(
            should_forward(
                "任务执行失败: <entry=Psychube, task_id=2, status=Failed, last_nodes=['x']>"
            )
        )
        self.assertTrue(should_forward("自定义任务执行失败: 用户取消操作"))
        self.assertTrue(should_forward("失败事件: 业务规则拒绝本次操作"))

    def test_framework_log_write_precedes_ui_filter(self) -> None:
        source = RUNNER_TASK.read_text(encoding="utf-8")
        read_stdout = source[source.index("def read_stdout"):]
        self.assertLess(
            read_stdout.index("write_framework_log("),
            read_stdout.index("_should_forward_framework_log("),
        )


if __name__ == "__main__":
    unittest.main()
