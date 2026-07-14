"""Tests for integrations._validation_helpers."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from integrations._validation_helpers import (
    report_classify_failure,
    report_validation_failure,
)
from integrations.config_models import DiscordBotConfig


def _mock_logger() -> MagicMock:
    return MagicMock(spec=logging.Logger)


class TestReportValidationFailure:
    def test_default_severity_is_warning(self) -> None:
        mock_log = _mock_logger()
        exc = RuntimeError("boom")
        with patch("platform.observability.errors.boundary.capture_exception"):
            report_validation_failure(
                exc,
                logger=mock_log,
                integration="trello",
                method="validate_trello_config",
            )
        mock_log.warning.assert_called_once()
        mock_log.error.assert_not_called()

    def test_message_includes_integration_and_method(self) -> None:
        mock_log = _mock_logger()
        with patch("platform.observability.errors.boundary.capture_exception"):
            report_validation_failure(
                RuntimeError("x"),
                logger=mock_log,
                integration="kafka",
                method="get_topic_health",
            )
        message = mock_log.warning.call_args[0][1]
        assert message == "[kafka] get_topic_health validation failed"

    def test_tags_have_expected_shape(self) -> None:
        mock_log = _mock_logger()
        exc = RuntimeError("boom")
        with patch("platform.observability.errors.boundary.capture_exception") as mock_cap:
            report_validation_failure(
                exc,
                logger=mock_log,
                integration="postgresql",
                method="get_server_status",
            )
        extra = mock_cap.call_args[1]["extra"]
        assert extra["tag.surface"] == "integration"
        assert extra["tag.integration"] == "postgresql"
        assert extra["tag.event"] == "validation_failed"
        assert extra["tag.method"] == "get_server_status"

    def test_extras_pass_through_unprefixed(self) -> None:
        mock_log = _mock_logger()
        with patch("platform.observability.errors.boundary.capture_exception") as mock_cap:
            report_validation_failure(
                RuntimeError("x"),
                logger=mock_log,
                integration="airflow",
                method="get_recent_airflow_failures.task_instances",
                extras={"dag_id": "dag-42", "dag_run_id": "run-7"},
            )
        extra = mock_cap.call_args[1]["extra"]
        assert extra["dag_id"] == "dag-42"
        assert extra["dag_run_id"] == "run-7"
        # extras should NOT be prefixed with "tag." (they're not Sentry tags)
        assert "tag.dag_id" not in extra
        assert "tag.dag_run_id" not in extra

    def test_severity_override_routes_to_logger(self) -> None:
        mock_log = _mock_logger()
        with patch("platform.observability.errors.boundary.capture_exception"):
            report_validation_failure(
                RuntimeError("x"),
                logger=mock_log,
                integration="mongodb",
                method="get_server_status",
                severity="error",
            )
        mock_log.error.assert_called_once()
        mock_log.warning.assert_not_called()

    def test_default_suppresses_terminal_traceback(self) -> None:
        """Validator failures must not dump a stack trace into the REPL by default."""
        mock_log = _mock_logger()
        with patch("platform.observability.errors.boundary.capture_exception"):
            report_validation_failure(
                RuntimeError("boom"),
                logger=mock_log,
                integration="github_mcp",
                method="validate_github_mcp_config",
            )
        assert mock_log.warning.call_args.kwargs["exc_info"] is False

    def test_traceback_included_when_explicitly_requested(self) -> None:
        mock_log = _mock_logger()
        exc = RuntimeError("boom")
        with patch("platform.observability.errors.boundary.capture_exception"):
            report_validation_failure(
                exc,
                logger=mock_log,
                integration="github_mcp",
                method="validate_github_mcp_config",
                include_traceback=True,
            )
        assert mock_log.warning.call_args.kwargs["exc_info"] is exc

    def test_captures_to_sentry_exactly_once(self) -> None:
        mock_log = _mock_logger()
        exc = RuntimeError("once")
        with patch("platform.observability.errors.boundary.capture_exception") as mock_cap:
            report_validation_failure(
                exc,
                logger=mock_log,
                integration="mysql",
                method="validate_mysql_config",
            )
        mock_cap.assert_called_once()
        assert mock_cap.call_args[0][0] is exc


def _validation_error_with_secret(secret: str) -> ValidationError:
    """Build a real pydantic ValidationError whose string embeds ``secret``."""
    try:
        DiscordBotConfig.model_validate({"bot_token": "ok", "public_key": secret})
    except ValidationError as exc:
        assert secret in str(exc), "test premise: pydantic embeds the input value"
        return exc
    raise AssertionError("expected ValidationError")


class TestSentrySafeSanitization:
    """ValidationError strings embed secret field values; both reporters must
    replace them with a model-name-only ValueError before capture."""

    def test_classify_failure_sanitizes_validation_error(self) -> None:
        secret = "leaked-non-hex-secret"
        exc = _validation_error_with_secret(secret)
        with patch("integrations._validation_helpers.report_exception") as mock_report:
            report_classify_failure(
                exc, logger=_mock_logger(), integration="discord", record_id="rec-1"
            )
        reported = mock_report.call_args.args[0]
        assert not isinstance(reported, ValidationError)
        assert str(reported) == "DiscordBotConfig validation failed"
        assert secret not in str(reported)

    def test_validation_failure_sanitizes_validation_error(self) -> None:
        secret = "leaked-non-hex-secret"
        exc = _validation_error_with_secret(secret)
        with patch("integrations._validation_helpers.report_exception") as mock_report:
            report_validation_failure(
                exc, logger=_mock_logger(), integration="discord", method="classify"
            )
        reported = mock_report.call_args.args[0]
        assert not isinstance(reported, ValidationError)
        assert str(reported) == "DiscordBotConfig validation failed"
        assert secret not in str(reported)

    def test_non_validation_error_passes_through_unchanged(self) -> None:
        exc = RuntimeError("boom")
        with patch("integrations._validation_helpers.report_exception") as mock_report:
            report_classify_failure(
                exc, logger=_mock_logger(), integration="discord", record_id="rec-1"
            )
        assert mock_report.call_args.args[0] is exc
