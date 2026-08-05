from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.plugins.fields import PluginField
from automas_script_maafw.schema import (
    SCRIPT_GROUPS as MAAFW_SCRIPT_GROUPS,
    USER_GROUPS as MAAFW_USER_GROUPS,
)
from pydantic import BaseModel, ConfigDict


class Config(BaseModel):
    """Host-level plugin instance configuration."""

    model_config = ConfigDict(extra="allow")


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
            "ImportProjectId",
            "首次导入项目 ID",
            "",
            placeholder="例如 my-maafw-project",
            help="仅用于首次导入；成功绑定后自动清空。",
        ),
        PluginField.string(
            "ProjectId",
            "已绑定项目 ID",
            "",
            readonly=True,
            help="由 Project Store 版本清单写入，不能作为导入输入修改。",
        ),
        PluginField.string(
            "StoreId",
            "Project Store 身份",
            "",
            readonly=True,
            hidden=True,
            help="用于阻止目录切换后误绑定同名项目。",
        ),
        PluginField.string(
            "RunRootId",
            "脱壳运行目录身份",
            "",
            readonly=True,
            hidden=True,
        ),
        PluginField.string(
            "Version",
            "当前资源版本",
            "",
            readonly=True,
            help="由当前脚本绑定的受保护 Project Store 版本写入。",
        ),
        PluginField.string(
            "ImportVersion",
            "导入版本（可选）",
            "",
            placeholder="通常留空，读取 ProjectInterface.version",
            help="如手动填写，必须与 ProjectInterface.version 语义一致。",
        ),
        PluginField.string(
            "TargetVersion",
            "操作目标版本",
            "",
            placeholder="用于强制切换或删除已安装版本",
        ),
        PluginField.json(
            "AvailableProjects",
            "全部托管资源",
            "[]",
            json_type="array",
            readonly=True,
            size="large",
        ),
        PluginField.json(
            "AvailableVersions",
            "已安装资源版本",
            "[]",
            json_type="array",
            readonly=True,
            size="large",
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
            help="选择正在使用或已解压的 MaaFW 项目目录；与 ZIP 二选一。",
        ),
        PluginField.file(
            "SourceArchive",
            "待导入资源 ZIP",
            "",
            size="large",
            help="选择本地 ZIP；插件不联网下载。资源仓会安全解压并过滤 UI、更新器、缓存和内置运行时。",
        ),
        PluginField.string(
            "ResourceVersion",
            "资源声明版本",
            "",
            readonly=True,
        ),
        PluginField.string(
            "InterfaceVersion",
            "ProjectInterface 版本",
            "",
            readonly=True,
        ),
        PluginField.number(
            "ResourceCount",
            "资源数量",
            0,
            readonly=True,
        ),
        PluginField.number(
            "TaskCount",
            "任务数量",
            0,
            readonly=True,
        ),
        PluginField.number(
            "AgentCount",
            "Agent 数量",
            0,
            readonly=True,
        ),
        PluginField.json(
            "Agents",
            "Agent 详情",
            "[]",
            json_type="array",
            readonly=True,
            size="large",
        ),
        PluginField.json(
            "Shells",
            "检测到并剥离的前端壳",
            "{}",
            json_type="object",
            readonly=True,
            size="large",
        ),
        PluginField.json(
            "Capabilities",
            "资源能力摘要",
            "{}",
            json_type="object",
            readonly=True,
            size="large",
        ),
        PluginField.number(
            "SourceSizeBytes",
            "导入源大小（字节）",
            0,
            readonly=True,
        ),
        PluginField.number(
            "ManagedSizeBytes",
            "托管资源大小（字节）",
            0,
            readonly=True,
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
        PluginField.json(
            "ConversionJournal",
            "MaaFW/M9A 原地转换 marker",
            "{}",
            json_type="object",
            readonly=True,
            hidden=True,
        ),
        PluginField.string(
            "PendingVersion",
            "待确认资源版本",
            "",
            readonly=True,
            help="本地升级先导入为非活动版本；应用配置计划后才会替换当前绑定。",
        ),
        PluginField.string(
            "UpgradeConfirmation",
            "应用升级确认",
            "",
            placeholder="输入 projectId@待确认版本",
            help="仅在脚本和全部用户配置计划均无人工阻塞时才会切换。",
        ),
        PluginField.string(
            "PendingPlanId",
            "待确认计划 ID",
            "",
            readonly=True,
        ),
        PluginField.string(
            "UpgradeToken",
            "应用确认令牌",
            "",
            readonly=True,
            size="large",
            help="完整复制到“应用升级确认”，避免旧页面或并发重规划误应用。",
        ),
        PluginField.json(
            "PendingUpgrade",
            "持久化升级 journal",
            "{}",
            json_type="object",
            readonly=True,
            hidden=True,
        ),
        PluginField.boolean(
            "UpgradeReady",
            "配置升级计划无人工阻塞",
            False,
            readonly=True,
            help="当前阶段仅生成并保存计划，不自动覆盖配置；存在人工动作时必须先确认。",
        ),
        PluginField.string(
            "UpgradePlanStatus",
            "配置升级计划状态",
            "尚未生成",
            readonly=True,
            size="large",
        ),
        PluginField.json(
            "UpgradePlan",
            "配置升级计划与 orphan",
            "{}",
            json_type="object",
            readonly=True,
            size="large",
        ),
        PluginField.json(
            "LastUpgrade",
            "最近一次已应用升级",
            "{}",
            json_type="object",
            readonly=True,
        ),
    ],
)


REMOTE_GROUP = PluginField.group(
    "ManagedRemote",
    "远程资源",
    [
        PluginField.select(
            "Source",
            "远程来源",
            "MirrorChyan",
            ["MirrorChyan", "GitHub"],
        ),
        PluginField.select(
            "Channel",
            "更新渠道",
            "stable",
            ["stable", "beta"],
        ),
        PluginField.string(
            "MirrorChyanRID",
            "MirrorChyan RID",
            "",
            placeholder="首次远程导入时填写；升级可继承 ProjectInterface",
        ),
        PluginField.string(
            "MirrorChyanCDK",
            "Mirror 酱 CDK",
            "",
            sensitive=True,
        ),
        PluginField.string(
            "GitHubRepo",
            "GitHub 仓库",
            "",
            placeholder="owner/repository；升级可继承 ProjectInterface",
        ),
        PluginField.string("GitHubTag", "GitHub Tag", ""),
        PluginField.string(
            "GitHubAssetPattern",
            "GitHub Asset 匹配",
            r"\.zip$",
        ),
        PluginField.string(
            "LatestVersion",
            "发现的远程版本",
            "",
            readonly=True,
        ),
        PluginField.boolean(
            "Installable",
            "存在可下载候选",
            False,
            readonly=True,
        ),
        PluginField.string(
            "Status",
            "远程检查状态",
            "尚未检查",
            readonly=True,
            size="large",
        ),
        PluginField.json(
            "Discovery",
            "远程发现结果",
            "{}",
            json_type="object",
            readonly=True,
            size="large",
        ),
        PluginField.json(
            "LastDownload",
            "最近下载包校验信息",
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
            "PoolId",
            "Runtime Pool 身份",
            "",
            readonly=True,
            hidden=True,
        ),
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
            "自动回收无引用资源",
            True,
            readonly=True,
            hidden=True,
            help="无引用资源固定自动回收；需要保留时请显式固定资源。",
        ),
        PluginField.number(
            "GCGraceDays",
            "回收宽限期（天）",
            0,
            min=0,
            max=3650,
            step=1,
            readonly=True,
            hidden=True,
        ),
        PluginField.number(
            "KeepLatest",
            "无引用时额外保留最新版本数",
            0,
            min=0,
            max=100,
            step=1,
            readonly=True,
            hidden=True,
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
            "CheckRemote",
            "检查远程资源",
            _action(
                "检查远程资源",
                "/plugin/maafw-managed/remote/check",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ImportProjectId}}",
                    "source": "{{formModel.ManagedRemote.Source}}",
                    "channel": "{{formModel.ManagedRemote.Channel}}",
                    "mirrorChyanRid": "{{formModel.ManagedRemote.MirrorChyanRID}}",
                    "mirrorChyanCDK": "{{formModel.ManagedRemote.MirrorChyanCDK}}",
                    "githubRepo": "{{formModel.ManagedRemote.GitHubRepo}}",
                    "githubTag": "{{formModel.ManagedRemote.GitHubTag}}",
                    "githubAssetPattern": (
                        "{{formModel.ManagedRemote.GitHubAssetPattern}}"
                    ),
                },
            ),
        ),
        PluginField.button(
            "ImportRemote",
            "首次下载并导入远程资源",
            _action(
                "首次下载并导入远程资源",
                "/plugin/maafw-managed/remote/import",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ImportProjectId}}",
                    "runtimeConstraint": (
                        "{{formModel.Managed.RuntimeConstraint}}"
                    ),
                    "source": "{{formModel.ManagedRemote.Source}}",
                    "channel": "{{formModel.ManagedRemote.Channel}}",
                    "mirrorChyanRid": "{{formModel.ManagedRemote.MirrorChyanRID}}",
                    "mirrorChyanCDK": "{{formModel.ManagedRemote.MirrorChyanCDK}}",
                    "githubRepo": "{{formModel.ManagedRemote.GitHubRepo}}",
                    "githubTag": "{{formModel.ManagedRemote.GitHubTag}}",
                    "githubAssetPattern": (
                        "{{formModel.ManagedRemote.GitHubAssetPattern}}"
                    ),
                },
            ),
        ),
        PluginField.button(
            "UpgradeRemote",
            "下载远程升级并生成计划",
            _action(
                "下载远程升级并生成计划",
                "/plugin/maafw-managed/remote/upgrade",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "runtimeConstraint": (
                        "{{formModel.Managed.RuntimeConstraint}}"
                    ),
                    "source": "{{formModel.ManagedRemote.Source}}",
                    "channel": "{{formModel.ManagedRemote.Channel}}",
                    "mirrorChyanRid": "{{formModel.ManagedRemote.MirrorChyanRID}}",
                    "mirrorChyanCDK": "{{formModel.ManagedRemote.MirrorChyanCDK}}",
                    "githubRepo": "{{formModel.ManagedRemote.GitHubRepo}}",
                    "githubTag": "{{formModel.ManagedRemote.GitHubTag}}",
                    "githubAssetPattern": (
                        "{{formModel.ManagedRemote.GitHubAssetPattern}}"
                    ),
                },
            ),
        ),
        PluginField.button(
            "ImportProject",
            "导入资源版本",
            _action(
                "导入资源版本",
                "/plugin/maafw-managed/import",
                {
                    "scriptId": "{{scriptId}}",
                    "sourcePath": "{{formModel.Managed.SourcePath}}",
                    "sourceArchive": "{{formModel.Managed.SourceArchive}}",
                    "projectId": "{{formModel.Managed.ImportProjectId}}",
                    "version": "{{formModel.Managed.ImportVersion}}",
                    "runtimeConstraint": "{{formModel.Managed.RuntimeConstraint}}",
                },
            ),
        ),
        PluginField.button(
            "UpgradeLocal",
            "导入本地升级并生成计划",
            _action(
                "导入本地升级并生成计划",
                "/plugin/maafw-managed/upgrade-local",
                {
                    "scriptId": "{{scriptId}}",
                    "sourcePath": "{{formModel.Managed.SourcePath}}",
                    "sourceArchive": "{{formModel.Managed.SourceArchive}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "version": "{{formModel.Managed.ImportVersion}}",
                    "runtimeConstraint": "{{formModel.Managed.RuntimeConstraint}}",
                },
            ),
        ),
        PluginField.button(
            "ApplyUpgrade",
            "应用配置计划并切换",
            _action(
                "应用配置计划并切换",
                "/plugin/maafw-managed/upgrade-apply",
                {
                    "scriptId": "{{scriptId}}",
                    "planId": "{{formModel.Managed.PendingPlanId}}",
                    "confirmation": (
                        "{{formModel.Managed.UpgradeConfirmation}}"
                    ),
                },
            ),
        ),
        PluginField.button(
            "CancelUpgrade",
            "取消待确认升级",
            _action(
                "取消待确认升级",
                "/plugin/maafw-managed/upgrade-cancel",
                {
                    "scriptId": "{{scriptId}}",
                },
            ),
        ),
        PluginField.button(
            "ListProjects",
            "刷新全部托管资源",
            _action(
                "刷新全部托管资源",
                "/plugin/maafw-managed/projects/list",
                {
                    "scriptId": "{{scriptId}}",
                },
            ),
        ),
        PluginField.button(
            "SwitchVersion",
            "为已安装版本生成切换计划",
            _action(
                "为已安装版本生成切换计划",
                "/plugin/maafw-managed/switch",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "version": "{{formModel.Managed.TargetVersion}}",
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
                    "version": "{{formModel.Managed.TargetVersion}}",
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
                    "graceDays": 0,
                    "keepLatest": 0,
                },
            ),
        ),
        PluginField.button(
            "RunGC",
            "立即回收无引用资源",
            _action(
                "立即回收无引用资源",
                "/plugin/maafw-managed/gc",
                {
                    "scriptId": "{{scriptId}}",
                    "projectId": "{{formModel.Managed.ProjectId}}",
                    "dryRun": False,
                    "confirmation": "{{formModel.ManagedRuntime.GCConfirmation}}",
                    "graceDays": 0,
                    "keepLatest": 0,
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
                    validator="script-root",
                    help="由 MAS Project Store 的当前项目版本注入。",
                )
            elif group.key == "Info" and field.name == "ProjectLabel":
                field = replace(field, readonly=True)
            fields.append(field)
        groups.append(replace(group, fields=tuple(fields)))
    return tuple(groups)


SCRIPT_GROUPS = (
    PROJECT_GROUP,
    *_managed_maafw_groups(),
    REMOTE_GROUP,
    RUNTIME_GROUP,
    ACTION_GROUP,
)

USER_UPGRADE_GROUP = PluginField.group(
    "ManagedUpgrade",
    "资源升级事务",
    [
        PluginField.json(
            "PendingPlan",
            "用户配置升级 journal",
            "{}",
            json_type="object",
            readonly=True,
            hidden=True,
        ),
    ],
)

# ProjectInterface 的动态 option 结构目前不足以无损表达任务参数，继续保存 JSON 快照。
USER_GROUPS = (*MAAFW_USER_GROUPS, USER_UPGRADE_GROUP)
