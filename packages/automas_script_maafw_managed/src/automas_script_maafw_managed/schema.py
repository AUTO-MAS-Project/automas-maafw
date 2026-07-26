from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.plugins.fields import PluginField
from automas_script_maafw.schema import (
    SCRIPT_GROUPS as MAAFW_SCRIPT_GROUPS,
    USER_GROUPS as MAAFW_USER_GROUPS,
)


schema = {
    "__no_plugin_config__": {
        "type": "boolean",
        "default": True,
        "hidden": True,
        "configurable": False,
        "title": "No plugin-level configuration",
    },
}


def _action(
    label: str,
    path: str,
    payload: dict[str, Any],
    *,
    refresh: bool = True,
) -> dict[str, Any]:
    return {
        "label": label,
        "path": path,
        "method": "POST",
        "payload": payload,
        "refresh": refresh,
    }


PROJECT_GROUP = PluginField.group(
    "Managed",
    "托管项目",
    [
        PluginField.string(
            "ProjectId",
            "项目 ID",
            "",
            required=True,
            placeholder="例如 my-maafw-project",
            help="项目在资源仓中的稳定标识；不保存完整发行包。",
        ),
        PluginField.string(
            "Version",
            "资源版本",
            "",
            placeholder="留空时使用当前版本",
        ),
        PluginField.json(
            "AvailableVersions",
            "已安装资源版本",
            "[]",
            json_type="array",
            readonly=True,
            size="large",
        ),
        PluginField.select(
            "Channel",
            "更新通道",
            "stable",
            ["stable", "beta", "nightly"],
        ),
        PluginField.select(
            "UpdateSource",
            "更新源",
            "auto",
            ["auto", "mirrorchyan", "github_release"],
            option_labels={
                "auto": "按 ProjectInterface 自动选择",
                "mirrorchyan": "MirrorChyan",
                "github_release": "GitHub Release",
            },
        ),
        PluginField.string(
            "MirrorChyanCDK",
            "MirrorChyan CDK",
            "",
            sensitive=True,
        ),
        PluginField.string("GitHubRepo", "GitHub 仓库", ""),
        PluginField.string("GitHubTag", "GitHub Tag", ""),
        PluginField.string(
            "GitHubAssetPattern",
            "GitHub 资源匹配",
            r"\.zip$",
        ),
        PluginField.string(
            "GitHubToken",
            "GitHub Token",
            "",
            sensitive=True,
        ),
        PluginField.string(
            "RuntimeConstraint",
            "MaaFW 运行时约束",
            "",
            placeholder="例如 >=4.5,<5；留空继承资源清单",
        ),
        PluginField.folder(
            "SourcePath",
            "待导入资源目录",
            "",
            size="large",
            help="仅用于导入。资源仓会过滤 UI、更新器、缓存和内置运行时。",
        ),
        PluginField.string(
            "DeleteConfirmation",
            "删除确认",
            "",
            placeholder="输入 projectId@version 后才能删除",
            help="删除当前版本、被引用版本或固定版本时，资源仓仍会拒绝操作。",
        ),
        PluginField.string(
            "Status",
            "解析状态",
            "尚未解析",
            readonly=True,
            size="large",
        ),
        PluginField.json(
            "ProjectManifest",
            "资源清单",
            "{}",
            json_type="object",
            readonly=True,
            size="large",
        ),
    ],
)


RUNTIME_GROUP = PluginField.group(
    "ManagedRuntime",
    "共享运行时与空间策略",
    [
        PluginField.string("RuntimeId", "运行时 ID", "", readonly=True),
        PluginField.string(
            "PythonExecutable",
            "Python 可执行文件",
            "",
            readonly=True,
            size="large",
        ),
        PluginField.string(
            "VenvPath",
            "共享环境目录",
            "",
            readonly=True,
            size="large",
        ),
        PluginField.json(
            "RuntimeBinding",
            "运行时绑定",
            "{}",
            json_type="object",
            readonly=True,
            size="large",
        ),
        PluginField.json(
            "AvailableRuntimes",
            "可用共享运行时",
            "[]",
            json_type="array",
            readonly=True,
            size="large",
        ),
        PluginField.string(
            "TargetRuntimeId",
            "待删除运行时 ID",
            "",
            placeholder="从可用运行时列表复制一个未绑定的旧 runtimeId",
            size="large",
        ),
        PluginField.string(
            "RuntimeDeleteConfirmation",
            "运行时删除确认",
            "",
            placeholder="完整输入上方运行时 ID 后才能删除",
            size="large",
        ),
        PluginField.boolean(
            "AutoGC",
            "运行完成后自动回收过期资源",
            False,
            help="默认关闭。启用后仍受固定、引用、运行 lease、宽限期和保留数量保护。",
        ),
        PluginField.number(
            "GCGraceDays",
            "回收宽限期（天）",
            30,
            min=1,
            max=3650,
            step=1,
        ),
        PluginField.number(
            "KeepLatest",
            "每个项目保留最新版本数",
            2,
            min=1,
            max=100,
            step=1,
        ),
        PluginField.string(
            "GCConfirmation",
            "立即回收确认",
            "",
            placeholder="输入 DELETE UNUSED 后执行实际回收",
        ),
    ],
)


ACTION_GROUP = PluginField.group(
    "ManagedActions",
    "资源操作",
    [
        PluginField.button(
            "ImportProject",
            "导入资源版本",
            _action(
                "导入资源版本",
                "/plugin/maafw-managed/import",
                {
                    "scriptId": "{{scriptId}}",
                    "sourcePath": "{{formModel.Managed.SourcePath}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "version": "{{formModel.Managed.Version}}",
                    "channel": "{{formModel.Managed.Channel}}",
                    "runtimeConstraint": "{{formModel.Managed.RuntimeConstraint}}",
                },
            ),
        ),
        PluginField.button(
            "CheckUpdate",
            "检查更新",
            _action(
                "检查更新",
                "/plugin/maafw-managed/check-update",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "version": "{{formModel.Managed.Version}}",
                    "channel": "{{formModel.Managed.Channel}}",
                    "sourceConfig": {
                        "source": "{{formModel.Managed.UpdateSource}}",
                        "mirror_cdk": "{{formModel.Managed.MirrorChyanCDK}}",
                        "repo": "{{formModel.Managed.GitHubRepo}}",
                        "tag": "{{formModel.Managed.GitHubTag}}",
                        "asset_pattern": "{{formModel.Managed.GitHubAssetPattern}}",
                        "token": "{{formModel.Managed.GitHubToken}}",
                    },
                },
            ),
        ),
        PluginField.button(
            "UpdateLatest",
            "更新到最新版",
            _action(
                "更新到最新版",
                "/plugin/maafw-managed/update",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "version": "{{formModel.Managed.Version}}",
                    "channel": "{{formModel.Managed.Channel}}",
                    "runtimeConstraint": "{{formModel.Managed.RuntimeConstraint}}",
                    "sourceConfig": {
                        "source": "{{formModel.Managed.UpdateSource}}",
                        "mirror_cdk": "{{formModel.Managed.MirrorChyanCDK}}",
                        "repo": "{{formModel.Managed.GitHubRepo}}",
                        "tag": "{{formModel.Managed.GitHubTag}}",
                        "asset_pattern": "{{formModel.Managed.GitHubAssetPattern}}",
                        "token": "{{formModel.Managed.GitHubToken}}",
                    },
                },
            ),
        ),
        PluginField.button(
            "SwitchVersion",
            "切换到所选版本",
            _action(
                "切换到所选版本",
                "/plugin/maafw-managed/switch",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "version": "{{formModel.Managed.Version}}",
                },
            ),
        ),
        PluginField.button(
            "ListVersions",
            "刷新项目版本列表",
            _action(
                "刷新项目版本列表",
                "/plugin/maafw-managed/versions/list",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                },
            ),
        ),
        PluginField.button(
            "DeleteVersion",
            "删除所选版本",
            _action(
                "删除所选版本",
                "/plugin/maafw-managed/delete",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "version": "{{formModel.Managed.Version}}",
                    "confirmation": "{{formModel.Managed.DeleteConfirmation}}",
                },
            ),
        ),
        PluginField.button(
            "InstallRuntime",
            "安装或复用运行时",
            _action(
                "安装或复用运行时",
                "/plugin/maafw-managed/runtime/install",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "version": "{{formModel.Managed.Version}}",
                    "channel": "{{formModel.Managed.Channel}}",
                    "runtimeConstraint": "{{formModel.Managed.RuntimeConstraint}}",
                },
            ),
        ),
        PluginField.button(
            "ListRuntimes",
            "刷新运行时列表",
            _action(
                "刷新运行时列表",
                "/plugin/maafw-managed/runtime/list",
                {
                    "scriptId": "{{scriptId}}",
                },
            ),
        ),
        PluginField.button(
            "DeleteRuntime",
            "删除所选运行时",
            _action(
                "删除所选运行时",
                "/plugin/maafw-managed/runtime/delete",
                {
                    "scriptId": "{{scriptId}}",
                    "runtimeId": "{{formModel.ManagedRuntime.TargetRuntimeId}}",
                    "confirmation": (
                        "{{formModel.ManagedRuntime.RuntimeDeleteConfirmation}}"
                    ),
                },
            ),
        ),
        PluginField.button(
            "PinResources",
            "固定项目与运行时",
            _action(
                "固定项目与运行时",
                "/plugin/maafw-managed/pin",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "version": "{{formModel.Managed.Version}}",
                    "runtimeId": "{{formModel.ManagedRuntime.RuntimeId}}",
                    "pinned": True,
                },
            ),
        ),
        PluginField.button(
            "UnpinResources",
            "取消固定",
            _action(
                "取消固定",
                "/plugin/maafw-managed/pin",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "version": "{{formModel.Managed.Version}}",
                    "runtimeId": "{{formModel.ManagedRuntime.RuntimeId}}",
                    "pinned": False,
                },
            ),
        ),
        PluginField.button(
            "PreviewGC",
            "预览空间回收",
            _action(
                "预览空间回收",
                "/plugin/maafw-managed/gc",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "dryRun": True,
                    "graceDays": "{{formModel.ManagedRuntime.GCGraceDays}}",
                    "keepLatest": "{{formModel.ManagedRuntime.KeepLatest}}",
                },
            ),
        ),
        PluginField.button(
            "RunGC",
            "立即回收过期资源",
            _action(
                "立即回收过期资源",
                "/plugin/maafw-managed/gc",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "dryRun": False,
                    "confirmation": "{{formModel.ManagedRuntime.GCConfirmation}}",
                    "graceDays": "{{formModel.ManagedRuntime.GCGraceDays}}",
                    "keepLatest": "{{formModel.ManagedRuntime.KeepLatest}}",
                },
            ),
        ),
    ],
)


def _managed_maafw_groups():
    groups = []
    for group in MAAFW_SCRIPT_GROUPS:
        fields = []
        for field in group.fields:
            if group.key == "Info" and field.name == "Path":
                field = replace(
                    field,
                    hidden=True,
                    readonly=True,
                    required=False,
                    help="运行前由 maafw.project_store.v1 注入。",
                )
            elif group.key == "Info" and field.name == "ProjectLabel":
                field = replace(field, readonly=True)
            elif group.key == "Update":
                field = replace(
                    field,
                    default=False if field.name == "IfAutoUpdate" else field.default,
                    hidden=True,
                    readonly=True,
                    help="托管项目使用不可变版本；更新由资源操作完成。",
                )
            fields.append(field)
        groups.append(replace(group, fields=tuple(fields)))
    return tuple(groups)


SCRIPT_GROUPS = (
    PROJECT_GROUP,
    *_managed_maafw_groups(),
    RUNTIME_GROUP,
    ACTION_GROUP,
)

# ProjectInterface 的动态 option 结构目前不足以无损表达任务参数，继续保存 JSON 快照。
USER_GROUPS = tuple(MAAFW_USER_GROUPS)
