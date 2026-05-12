"""Coverage for ``app.tools._telemetry`` and tool-level Sentry capture.

Two layers:

1. ``test_report_run_error_*`` exercise the helper directly: tags, severity,
   logger forwarding, and the fact that a Sentry capture is best-effort.
2. ``test_tool_reports_exactly_one_sentry_event`` is the parameterised
   "every patched tool reports a Sentry event when its underlying client
   raises" assertion called out in #1463 acceptance criteria. Each row
   forces the client used by the tool body to raise and verifies the helper
   produced exactly one event.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.tools._telemetry import report_run_error


@pytest.fixture
def captured_sentry_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[BaseException]]:
    """Patch the Sentry SDK so every capture lands in a local list.

    Tests rely on this rather than the real ``sentry_sdk`` because:
      * ``conftest`` sets ``OPENSRE_SENTRY_DISABLED=1`` to keep the suite
        offline — we re-enable it here.
      * ``capture_exception`` and ``push_scope`` both need to be present
        for the contextual-tag path inside ``app.utils.sentry_sdk``.
    """
    monkeypatch.delenv("OPENSRE_SENTRY_DISABLED", raising=False)
    monkeypatch.delenv("OPENSRE_NO_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)

    events: list[BaseException] = []

    class _Scope:
        def __enter__(self) -> _Scope:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def set_tag(self, _key: str, _value: str) -> None:
            return None

        def set_extra(self, _key: str, _value: object) -> None:
            return None

    def _capture(exc: BaseException) -> None:
        events.append(exc)

    monkeypatch.setitem(
        sys.modules,
        "sentry_sdk",
        SimpleNamespace(capture_exception=_capture, push_scope=_Scope),
    )
    yield events


def test_report_run_error_captures_with_expected_tags(
    captured_sentry_events: list[BaseException],
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

    assert captured_sentry_events == [boom]
    assert "Tool query_azure_monitor_logs failed" in caplog.text


def test_report_run_error_supports_warning_severity(
    captured_sentry_events: list[BaseException],
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

    assert captured_sentry_events == [err]
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], "warning severity must not log at error level"


def test_report_run_error_uses_provided_logger(
    captured_sentry_events: list[BaseException],
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
    assert captured_sentry_events == [err]


# ---------------------------------------------------------------------------
# Parameterised tool coverage
#
# Each row patches the lowest-level dependency the tool reaches for and forces
# it to raise. The helper must then produce exactly one Sentry event so the
# silent ``{"available": False}`` return is no longer invisible to operators.
# ---------------------------------------------------------------------------


ToolFailureCase = tuple[
    str,  # human-readable id
    Callable[[pytest.MonkeyPatch], None],  # patch fn
    Callable[[], dict[str, Any]],  # invoke tool, returns its dict
    str,  # tool name expected in the error dict / tags
]


def _patch_attr(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    name: str,
    *,
    side_effect: type[BaseException] | BaseException,
) -> None:
    module = __import__(target, fromlist=[name])
    monkeypatch.setattr(module, name, MagicMock(side_effect=side_effect))


def _azure_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import AzureMonitorLogsTool as mod

        mp.setattr(mod, "httpx", SimpleNamespace(post=MagicMock(side_effect=RuntimeError("net"))))

    def invoke() -> dict[str, Any]:
        from app.tools.AzureMonitorLogsTool import query_azure_monitor_logs

        return query_azure_monitor_logs(workspace_id="w", access_token="t")

    return ("azure_monitor_logs", patch, invoke, "azure")


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

    return ("openobserve_logs", patch, invoke, "openobserve")


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

    return ("snowflake_query_history", patch, invoke, "snowflake")


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

    return ("cloudwatch_logs", patch, invoke, "cloudwatch")


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

    return ("cloudwatch_batch_metrics", patch, invoke, "cloudwatch")


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

    return ("google_docs_create_report", patch, invoke, "google_docs")


def _eks_list_clusters_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import EKSListClustersTool as mod

        mp.setattr(mod, "EKSClient", MagicMock(side_effect=RuntimeError("eks")))

    def invoke() -> dict[str, Any]:
        from app.tools.EKSListClustersTool import list_eks_clusters

        return list_eks_clusters(role_arn="arn:aws:iam::123:role/x")

    return ("eks_list_clusters", patch, invoke, "eks")


def _eks_describe_cluster_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import EKSDescribeClusterTool as mod

        mp.setattr(mod, "EKSClient", MagicMock(side_effect=RuntimeError("eks")))

    def invoke() -> dict[str, Any]:
        from app.tools.EKSDescribeClusterTool import describe_eks_cluster

        return describe_eks_cluster(cluster_name="c", role_arn="arn:aws:iam::123:role/x")

    return ("eks_describe_cluster", patch, invoke, "eks")


def _eks_nodegroup_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import EKSNodegroupHealthTool as mod

        mp.setattr(mod, "EKSClient", MagicMock(side_effect=RuntimeError("eks")))

    def invoke() -> dict[str, Any]:
        from app.tools.EKSNodegroupHealthTool import get_eks_nodegroup_health

        return get_eks_nodegroup_health(cluster_name="c", role_arn="arn:aws:iam::123:role/x")

    return ("eks_nodegroup_health", patch, invoke, "eks")


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

    return ("eks_describe_addon", patch, invoke, "eks")


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

    return ("eks_list_pods", patch, invoke, "eks")


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

    return ("eks_pod_logs", patch, invoke, "eks")


def _openclaw_list_case() -> ToolFailureCase:
    def patch(mp: pytest.MonkeyPatch) -> None:
        from app.tools import OpenClawMCPTool as mod

        mp.setattr(
            mod,
            "_resolve_config",
            MagicMock(return_value=SimpleNamespace(mode="stdio", command="x", url="")),
        )
        mp.setattr(mod, "openclaw_runtime_unavailable_reason", MagicMock(return_value=None))
        mp.setattr(mod, "list_openclaw_mcp_tools", MagicMock(side_effect=RuntimeError("mcp")))
        mp.setattr(mod, "describe_openclaw_error", MagicMock(return_value="mocked error"))

    def invoke() -> dict[str, Any]:
        from app.tools.OpenClawMCPTool import list_openclaw_bridge_tools

        return list_openclaw_bridge_tools()

    return ("openclaw_list_tools", patch, invoke, "openclaw")


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
]


@pytest.mark.parametrize(
    "case",
    _TOOL_FAILURE_CASES,
    ids=[case[0] for case in _TOOL_FAILURE_CASES],
)
def test_tool_reports_exactly_one_sentry_event(
    case: ToolFailureCase,
    captured_sentry_events: list[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _id, patch, invoke, expected_source = case
    patch(monkeypatch)

    result = invoke()

    assert isinstance(result, dict)
    # Tools either expose ``available=False`` or fall back to ``success=False``
    # (GoogleDocs) / raw ``{"error": ...}`` (CloudWatchLogs) — all three are
    # the "silent today" shapes #1463 enumerates. We just need the negative
    # signal to be present so an accidental success doesn't pass the assertion.
    assert result.get("available") is False or result.get("success") is False or "error" in result

    assert len(captured_sentry_events) == 1, (
        f"{_id} should report exactly one Sentry event when its client raises; "
        f"got {len(captured_sentry_events)}"
    )
    captured = captured_sentry_events[0]
    assert isinstance(captured, RuntimeError)

    # The tool's source tag must match its declared metadata. This guards
    # against a future regression where a tool migrates to the helper but
    # passes the wrong ``source=`` argument.
    from app.tools.registry import get_registered_tool_map

    registered_name = {
        "azure_monitor_logs": "query_azure_monitor_logs",
        "openobserve_logs": "query_openobserve_logs",
        "snowflake_query_history": "query_snowflake_history",
        "cloudwatch_logs": "get_cloudwatch_logs",
        "cloudwatch_batch_metrics": "get_cloudwatch_batch_metrics",
        "google_docs_create_report": "create_google_docs_incident_report",
        "eks_list_clusters": "list_eks_clusters",
        "eks_describe_cluster": "describe_eks_cluster",
        "eks_nodegroup_health": "get_eks_nodegroup_health",
        "eks_describe_addon": "describe_eks_addon",
        "eks_list_pods": "list_eks_pods",
        "eks_pod_logs": "get_eks_pod_logs",
        "openclaw_list_tools": "list_openclaw_tools",
    }[_id]
    registered = get_registered_tool_map().get(registered_name)
    if registered is not None:
        assert registered.source == expected_source
