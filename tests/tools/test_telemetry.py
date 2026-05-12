"""Coverage for ``app.tools._telemetry`` and tool-level Sentry capture.

Three layers:

1. ``test_report_run_error_*`` exercise the helper directly: tags, severity,
   logger forwarding, and the fact that a Sentry capture is best-effort.
2. ``test_tool_reports_exactly_one_sentry_event`` is the parameterised
   "every migrated tool reports a Sentry event when its underlying client
   raises" assertion called out in #1463 acceptance criteria. Each row
   forces the client used by the tool body to raise and verifies the helper
   produced exactly one event with the expected ``surface=tool``,
   ``tool_name``, and ``source`` tags.
3. ``test_eks_client_error_path_uses_warning_severity`` exercises the EKS
   ``except ClientError`` branch (the whole reason for the severity split)
   by patching the underlying client to raise ``botocore.exceptions.ClientError``
   and asserting the helper logged at ``WARNING``, not ``ERROR``.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.tools._telemetry import report_run_error


@dataclass
class CapturedSentryEvent:
    """One Sentry capture, with the scope extras that were attached.

    ``report_exception`` flattens tags into ``extra`` with a ``tag.`` prefix
    (see ``app/utils/errors.py``), so a tag set via
    ``report_run_error(tool_name="X")`` shows up here as
    ``extras["tag.tool_name"] == "X"``.
    """

    exc: BaseException
    extras: dict[str, Any]


@pytest.fixture
def captured_sentry_events(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[CapturedSentryEvent]]:
    """Patch the Sentry SDK so every capture lands in a local list.

    Tests rely on this rather than the real ``sentry_sdk`` because:
      * ``conftest`` sets ``OPENSRE_SENTRY_DISABLED=1`` to keep the suite
        offline — we re-enable it here.
      * ``capture_exception`` and ``push_scope`` both need to be present
        for the contextual-tag path inside ``app.utils.sentry_sdk``.

    The mock ``push_scope`` returns a per-call ``_Scope`` instance that
    records every ``set_extra`` and ``set_tag`` call. ``capture_exception``
    snapshots the current scope's extras alongside the exception so tests
    can assert on the tags that reached Sentry.
    """
    monkeypatch.delenv("OPENSRE_SENTRY_DISABLED", raising=False)
    monkeypatch.delenv("OPENSRE_NO_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)

    events: list[CapturedSentryEvent] = []
    scope_stack: list[_RecordingScope] = []

    class _RecordingScope:
        def __init__(self) -> None:
            self.extras: dict[str, Any] = {}

        def __enter__(self) -> _RecordingScope:
            scope_stack.append(self)
            return self

        def __exit__(self, *_args: object) -> None:
            if scope_stack and scope_stack[-1] is self:
                scope_stack.pop()
            return None

        def set_tag(self, key: str, value: str) -> None:
            # Mirror the existing ``report_exception`` convention so tests
            # see a single flat extras dict regardless of whether a value
            # was attached via set_tag or set_extra.
            self.extras[f"tag.{key}"] = value

        def set_extra(self, key: str, value: object) -> None:
            self.extras[key] = value

    def _capture(exc: BaseException) -> None:
        current_extras = dict(scope_stack[-1].extras) if scope_stack else {}
        events.append(CapturedSentryEvent(exc=exc, extras=current_extras))

    monkeypatch.setitem(
        sys.modules,
        "sentry_sdk",
        SimpleNamespace(capture_exception=_capture, push_scope=_RecordingScope),
    )
    yield events


def test_report_run_error_captures_with_expected_tags(
    captured_sentry_events: list[CapturedSentryEvent],
    caplog: pytest.LogCaptureFixture,
) -> None:
    boom = RuntimeError("boom")
    with caplog.at_level(logging.ERROR, logger="app.tools"):
        report_run_error(
            boom,
            tool_name="query_azure_monitor_logs",
            source="azure",
            component="app.tools.AzureMonitorLogsTool",
            method="httpx.post",
            extras={"workspace_id": "w"},
        )

    assert len(captured_sentry_events) == 1
    event = captured_sentry_events[0]
    assert event.exc is boom
    assert event.extras["tag.surface"] == "tool"
    assert event.extras["tag.tool_name"] == "query_azure_monitor_logs"
    assert event.extras["tag.source"] == "azure"
    assert event.extras["tag.component"] == "app.tools.AzureMonitorLogsTool"
    assert event.extras["tag.method"] == "httpx.post"
    assert event.extras["workspace_id"] == "w"
    assert "Tool query_azure_monitor_logs failed" in caplog.text


def test_report_run_error_supports_warning_severity(
    captured_sentry_events: list[CapturedSentryEvent],
    caplog: pytest.LogCaptureFixture,
) -> None:
    err = RuntimeError("recoverable")
    with caplog.at_level(logging.WARNING, logger="app.tools"):
        report_run_error(
            err,
            tool_name="describe_eks_cluster",
            source="eks",
            component="app.tools.EKSDescribeClusterTool",
            severity="warning",
        )

    assert len(captured_sentry_events) == 1
    assert captured_sentry_events[0].exc is err
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], "warning severity must not log at error level"
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "warning severity must produce a WARNING log record"


def test_report_run_error_uses_provided_logger(
    captured_sentry_events: list[CapturedSentryEvent],
) -> None:
    custom_logger = MagicMock(spec=logging.Logger)
    err = ValueError("nope")

    report_run_error(
        err,
        tool_name="list_eks_pods",
        source="eks",
        component="app.tools.EKSListPodsTool",
        logger=custom_logger,
    )

    custom_logger.error.assert_called_once()
    assert len(captured_sentry_events) == 1
    assert captured_sentry_events[0].exc is err


# ---------------------------------------------------------------------------
# Parameterised tool coverage
#
# Each case patches the lowest-level dependency the tool reaches for and forces
# it to raise. The helper must then produce exactly one Sentry event so the
# silent ``{"available": False}`` return is no longer invisible to operators.
# ---------------------------------------------------------------------------


@dataclass
class ToolFailureCase:
    id: str
    patch: Callable[[pytest.MonkeyPatch], None]
    invoke: Callable[[], dict[str, Any]]
    expected_tool_name: str
    expected_source: str


def _azure_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import AzureMonitorLogsTool as mod

        mp.setattr(mod, "httpx", SimpleNamespace(post=MagicMock(side_effect=RuntimeError("net"))))

    def invoke() -> dict[str, Any]:
        from app.tools.AzureMonitorLogsTool import query_azure_monitor_logs

        return query_azure_monitor_logs(workspace_id="w", access_token="t")

    return ToolFailureCase("azure_monitor_logs", patch, invoke, "query_azure_monitor_logs", "azure")


def _openobserve_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import OpenObserveLogsTool as mod

        mp.setattr(mod, "httpx", SimpleNamespace(post=MagicMock(side_effect=RuntimeError("net"))))

    def invoke() -> dict[str, Any]:
        from app.tools.OpenObserveLogsTool import query_openobserve_logs

        return query_openobserve_logs(
            base_url="https://oo.example",
            org="default",
            stream="default",
            query="*",
            api_token="t",
        )

    return ToolFailureCase(
        "openobserve_logs", patch, invoke, "query_openobserve_logs", "openobserve"
    )


def _snowflake_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import SnowflakeQueryHistoryTool as mod

        mp.setattr(mod, "httpx", SimpleNamespace(post=MagicMock(side_effect=RuntimeError("net"))))

    def invoke() -> dict[str, Any]:
        from app.tools.SnowflakeQueryHistoryTool import query_snowflake_history

        return query_snowflake_history(
            account_identifier="acc",
            token="tok",
            query="select 1",
        )

    return ToolFailureCase(
        "snowflake_query_history", patch, invoke, "query_snowflake_history", "snowflake"
    )


def _cloudwatch_logs_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import CloudWatchLogsTool as mod

        mp.setattr(
            mod,
            "boto3",
            SimpleNamespace(client=MagicMock(side_effect=RuntimeError("aws"))),
        )

    def invoke() -> dict[str, Any]:
        from app.tools.CloudWatchLogsTool import get_cloudwatch_logs

        return get_cloudwatch_logs(log_group="/aws/lambda/test")

    return ToolFailureCase("cloudwatch_logs", patch, invoke, "get_cloudwatch_logs", "cloudwatch")


def _cloudwatch_batch_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import CloudWatchBatchMetricsTool as mod

        mp.setattr(
            mod,
            "get_metric_statistics",
            MagicMock(side_effect=RuntimeError("aws")),
        )

    def invoke() -> dict[str, Any]:
        from app.tools.CloudWatchBatchMetricsTool import get_cloudwatch_batch_metrics

        return get_cloudwatch_batch_metrics(job_queue="q", metric_type="cpu")

    return ToolFailureCase(
        "cloudwatch_batch_metrics",
        patch,
        invoke,
        "get_cloudwatch_batch_metrics",
        "cloudwatch",
    )


def _google_docs_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import GoogleDocsCreateReportTool as mod

        mp.setattr(
            mod,
            "GoogleDocsClient",
            MagicMock(side_effect=RuntimeError("google")),
        )

    def invoke() -> dict[str, Any]:
        from app.tools.GoogleDocsCreateReportTool import create_google_docs_incident_report

        return create_google_docs_incident_report(
            title="t",
            summary="s",
            root_cause="rc",
            severity="low",
            credentials_file="/tmp/missing.json",
            folder_id="f",
        )

    return ToolFailureCase(
        "google_docs_create_report",
        patch,
        invoke,
        "create_google_docs_incident_report",
        "google_docs",
    )


def _eks_list_clusters_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import EKSListClustersTool as mod

        mp.setattr(mod, "EKSClient", MagicMock(side_effect=RuntimeError("eks")))

    def invoke() -> dict[str, Any]:
        from app.tools.EKSListClustersTool import list_eks_clusters

        return list_eks_clusters(role_arn="arn:aws:iam::123:role/x")

    return ToolFailureCase("eks_list_clusters", patch, invoke, "list_eks_clusters", "eks")


def _eks_describe_cluster_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import EKSDescribeClusterTool as mod

        mp.setattr(mod, "EKSClient", MagicMock(side_effect=RuntimeError("eks")))

    def invoke() -> dict[str, Any]:
        from app.tools.EKSDescribeClusterTool import describe_eks_cluster

        return describe_eks_cluster(cluster_name="c", role_arn="arn:aws:iam::123:role/x")

    return ToolFailureCase("eks_describe_cluster", patch, invoke, "describe_eks_cluster", "eks")


def _eks_nodegroup_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import EKSNodegroupHealthTool as mod

        mp.setattr(mod, "EKSClient", MagicMock(side_effect=RuntimeError("eks")))

    def invoke() -> dict[str, Any]:
        from app.tools.EKSNodegroupHealthTool import get_eks_nodegroup_health

        return get_eks_nodegroup_health(cluster_name="c", role_arn="arn:aws:iam::123:role/x")

    return ToolFailureCase("eks_nodegroup_health", patch, invoke, "get_eks_nodegroup_health", "eks")


def _eks_addon_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import EKSDescribeAddonTool as mod

        mp.setattr(mod, "EKSClient", MagicMock(side_effect=RuntimeError("eks")))

    def invoke() -> dict[str, Any]:
        from app.tools.EKSDescribeAddonTool import describe_eks_addon

        return describe_eks_addon(
            cluster_name="c",
            addon_name="coredns",
            role_arn="arn:aws:iam::123:role/x",
        )

    return ToolFailureCase("eks_describe_addon", patch, invoke, "describe_eks_addon", "eks")


def _eks_list_pods_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import EKSListPodsTool as mod

        mp.setattr(mod, "build_k8s_clients", MagicMock(side_effect=RuntimeError("k8s")))

    def invoke() -> dict[str, Any]:
        from app.tools.EKSListPodsTool import list_eks_pods

        return list_eks_pods(
            cluster_name="c",
            namespace="default",
            role_arn="arn:aws:iam::123:role/x",
        )

    return ToolFailureCase("eks_list_pods", patch, invoke, "list_eks_pods", "eks")


def _eks_pod_logs_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import EKSPodLogsTool as mod

        mp.setattr(mod, "build_k8s_clients", MagicMock(side_effect=RuntimeError("k8s")))

    def invoke() -> dict[str, Any]:
        from app.tools.EKSPodLogsTool import get_eks_pod_logs

        return get_eks_pod_logs(
            cluster_name="c",
            namespace="default",
            pod_name="p",
            role_arn="arn:aws:iam::123:role/x",
        )

    return ToolFailureCase("eks_pod_logs", patch, invoke, "get_eks_pod_logs", "eks")


def _patch_openclaw_runtime(mp: pytest.MonkeyPatch) -> None:
    """Shared patches for all OpenClaw cases — bypass the config/runtime guards.

    Each test still patches the specific failure point afterwards.
    """
    from app.tools import OpenClawMCPTool as mod

    mp.setattr(
        mod,
        "_resolve_config",
        MagicMock(return_value=SimpleNamespace(mode="stdio", command="x", url="")),
    )
    mp.setattr(mod, "openclaw_runtime_unavailable_reason", MagicMock(return_value=None))
    mp.setattr(mod, "describe_openclaw_error", MagicMock(return_value="mocked error"))


def _openclaw_list_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import OpenClawMCPTool as mod

        _patch_openclaw_runtime(mp)
        mp.setattr(mod, "list_openclaw_mcp_tools", MagicMock(side_effect=RuntimeError("mcp")))

    def invoke() -> dict[str, Any]:
        from app.tools.OpenClawMCPTool import list_openclaw_bridge_tools

        return list_openclaw_bridge_tools()

    return ToolFailureCase("openclaw_list_tools", patch, invoke, "list_openclaw_tools", "openclaw")


def _openclaw_search_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import OpenClawMCPTool as mod

        _patch_openclaw_runtime(mp)
        mp.setattr(mod, "invoke_openclaw_mcp_tool", MagicMock(side_effect=RuntimeError("mcp")))

    def invoke() -> dict[str, Any]:
        from app.tools.OpenClawMCPTool import search_openclaw_conversations

        return search_openclaw_conversations(search="db error")

    return ToolFailureCase(
        "openclaw_search_conversations",
        patch,
        invoke,
        "search_openclaw_conversations",
        "openclaw",
    )


def _openclaw_get_conversation_case() -> ToolFailureCase:
    """Exercises ``_normalize_named_bridge_call`` via ``get_openclaw_conversation``.

    Verifies the helper's ``surface_tool_name`` plumbing — the Sentry
    ``tool_name`` tag must be ``get_openclaw_conversation`` (the registered
    surface name), not ``conversations_get`` (the MCP-side tool id).
    """

    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import OpenClawMCPTool as mod

        _patch_openclaw_runtime(mp)
        mp.setattr(mod, "invoke_openclaw_mcp_tool", MagicMock(side_effect=RuntimeError("mcp")))

    def invoke() -> dict[str, Any]:
        from app.tools.OpenClawMCPTool import get_openclaw_conversation

        return get_openclaw_conversation(conversation_id="conv-1")

    return ToolFailureCase(
        "openclaw_get_conversation",
        patch,
        invoke,
        "get_openclaw_conversation",
        "openclaw",
    )


def _openclaw_call_tool_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import OpenClawMCPTool as mod

        _patch_openclaw_runtime(mp)
        mp.setattr(mod, "invoke_openclaw_mcp_tool", MagicMock(side_effect=RuntimeError("mcp")))

    def invoke() -> dict[str, Any]:
        from app.tools.OpenClawMCPTool import call_openclaw_bridge_tool

        return call_openclaw_bridge_tool(tool_name="permissions_grant", arguments={})

    return ToolFailureCase(
        "openclaw_call_tool",
        patch,
        invoke,
        "call_openclaw_tool",
        "openclaw",
    )


_TOOL_FAILURE_CASES: list[ToolFailureCase] = [
    _azure_case(),
    _openobserve_case(),
    _snowflake_case(),
    _cloudwatch_logs_case(),
    _cloudwatch_batch_case(),
    _google_docs_case(),
    _eks_list_clusters_case(),
    _eks_describe_cluster_case(),
    _eks_nodegroup_case(),
    _eks_addon_case(),
    _eks_list_pods_case(),
    _eks_pod_logs_case(),
    _openclaw_list_case(),
    _openclaw_search_case(),
    _openclaw_get_conversation_case(),
    _openclaw_call_tool_case(),
]


@pytest.mark.parametrize(
    "case",
    _TOOL_FAILURE_CASES,
    ids=[case.id for case in _TOOL_FAILURE_CASES],
)
def test_tool_reports_exactly_one_sentry_event(
    case: ToolFailureCase,
    captured_sentry_events: list[CapturedSentryEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case.patch(monkeypatch)

    result = case.invoke()

    # Tools either expose ``available=False`` or fall back to ``success=False``
    # (GoogleDocs) / raw ``{"error": ...}`` (CloudWatchLogs) — all three are
    # the "silent today" shapes #1463 enumerates. We just need the negative
    # signal to be present so an accidental success doesn't pass the assertion.
    assert isinstance(result, dict)
    assert result.get("available") is False or result.get("success") is False or "error" in result

    assert len(captured_sentry_events) == 1, (
        f"{case.id} should report exactly one Sentry event when its client raises; "
        f"got {len(captured_sentry_events)}"
    )
    event = captured_sentry_events[0]
    assert isinstance(event.exc, RuntimeError)
    assert event.extras["tag.surface"] == "tool"
    assert event.extras["tag.tool_name"] == case.expected_tool_name
    assert event.extras["tag.source"] == case.expected_source

    # Guard against a future regression where a tool migrates to the helper
    # but passes a ``tool_name=`` / ``source=`` that no longer matches its
    # declared metadata.
    from app.tools.registry import get_registered_tool_map

    registered = get_registered_tool_map().get(case.expected_tool_name)
    if registered is not None:
        assert registered.source == case.expected_source


def test_eks_client_error_path_uses_warning_severity(
    captured_sentry_events: list[CapturedSentryEvent],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The EKS ``except ClientError`` branch must report at WARNING, not ERROR.

    The broad ``except Exception`` branch in every EKS tool reports at the
    default severity (``error``); the dedicated ``ClientError`` branch
    intentionally degrades to ``warning`` because a missing-permission or
    not-found response is operationally useful but not a code defect. The
    parameterised cases above patch ``EKSClient`` to raise plain
    ``RuntimeError``, which exercises only the ``Exception`` branch — this
    test fills the gap by raising a real ``botocore.exceptions.ClientError``.
    """
    from botocore.exceptions import ClientError

    from app.tools import EKSListClustersTool as mod

    client_error = ClientError(
        error_response={
            "Error": {"Code": "ResourceNotFoundException", "Message": "cluster missing"},
        },
        operation_name="ListClusters",
    )

    instance = MagicMock()
    instance.list_clusters.side_effect = client_error
    monkeypatch.setattr(mod, "EKSClient", MagicMock(return_value=instance))

    with caplog.at_level(logging.WARNING, logger="app.tools"):
        result = mod.list_eks_clusters(role_arn="arn:aws:iam::123:role/x")

    assert result["available"] is False
    assert len(captured_sentry_events) == 1
    event = captured_sentry_events[0]
    assert isinstance(event.exc, ClientError)
    assert event.extras["tag.tool_name"] == "list_eks_clusters"

    warning_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "list_eks_clusters" in r.getMessage()
    ]
    assert warning_records, (
        "EKS ClientError branch must log at WARNING via severity='warning'; "
        f"got levels {[r.levelname for r in caplog.records]}"
    )
    error_records_for_tool = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and "list_eks_clusters" in r.getMessage()
    ]
    assert error_records_for_tool == [], "ClientError severity='warning' must not also log at ERROR"
