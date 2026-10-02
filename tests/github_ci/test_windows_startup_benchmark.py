"""Contracts for the native Windows packaging benchmark."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "windows-startup-benchmark.yml"
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "windows_startup_benchmark.py"


def _load_benchmark_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("windows_startup_benchmark", _SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_benchmark = _load_benchmark_module()
Measurement = _benchmark.Measurement
comparison_for = _benchmark.comparison_for
nearest_rank_percentile = _benchmark.nearest_rank_percentile
render_markdown = _benchmark.render_markdown
summarize_measurements = _benchmark.summarize_measurements


def test_nearest_rank_percentile_uses_observed_sample() -> None:
    samples = [37.0, 7.0, 13.0, 12.0, 15.0]

    assert nearest_rank_percentile(samples, 50) == 13.0
    assert nearest_rank_percentile(samples, 95) == 37.0


def test_nearest_rank_percentile_rejects_empty_sample() -> None:
    with pytest.raises(ValueError, match="at least one sample"):
        nearest_rank_percentile([], 95)


def test_summary_keeps_first_launch_separate_from_warm_distribution() -> None:
    measurements = [
        Measurement("onefile", "first", 0, 40.0),
        Measurement("onedir", "first", 0, 12.0),
        Measurement("onefile", "warm", 1, 38.0),
        Measurement("onedir", "warm", 1, 8.0),
        Measurement("onefile", "warm", 2, 36.0),
        Measurement("onedir", "warm", 2, 6.0),
        Measurement("onefile", "warm", 3, 37.0),
        Measurement("onedir", "warm", 3, 7.0),
    ]

    summary = summarize_measurements(measurements)
    comparison = comparison_for(summary)

    assert summary["onefile"] == {
        "first_ms": 40.0,
        "warm_median_ms": 37.0,
        "warm_p95_ms": 38.0,
        "warm_samples_ms": [38.0, 36.0, 37.0],
    }
    assert summary["onedir"]["warm_median_ms"] == 7.0
    assert comparison == {"onedir_saved_ms": 30.0, "onedir_speedup": 5.286}


def test_markdown_does_not_overstate_the_measurement() -> None:
    summary = {
        "onefile": {
            "first_ms": 40.0,
            "warm_median_ms": 37.0,
            "warm_p95_ms": 38.0,
            "warm_samples_ms": [36.0, 37.0, 38.0],
        },
        "onedir": {
            "first_ms": 12.0,
            "warm_median_ms": 7.0,
            "warm_p95_ms": 8.0,
            "warm_samples_ms": [6.0, 7.0, 8.0],
        },
    }

    markdown = render_markdown(
        summary,
        comparison_for(summary),
        {
            "platform": "Windows-11",
            "defender_realtime_enabled": True,
        },
    )

    assert "not a reboot-cold measurement" in markdown
    assert "not a ConPTY time-to-prompt result" in markdown
    assert "must not be used as a release gate" in markdown
    assert "Defender real-time protection: `True`" in markdown


def test_markdown_reports_onedir_regression_without_calling_it_a_saving() -> None:
    summary = {
        "onefile": {
            "first_ms": 6.0,
            "warm_median_ms": 5.0,
            "warm_p95_ms": 6.0,
            "warm_samples_ms": [4.0, 5.0, 6.0],
        },
        "onedir": {
            "first_ms": 12.0,
            "warm_median_ms": 10.0,
            "warm_p95_ms": 12.0,
            "warm_samples_ms": [8.0, 10.0, 12.0],
        },
    }

    markdown = render_markdown(summary, comparison_for(summary))

    assert "Onedir was **5.0 ms slower**" in markdown
    assert "Onedir saved" not in markdown


def test_workflow_builds_both_modes_on_one_windows_runner_without_a_timing_gate() -> None:
    raw = _WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    benchmark = workflow["jobs"]["benchmark"]

    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    assert benchmark["runs-on"] == "windows-latest"
    assert "OPENSRE_PYINSTALLER_MODE = $mode" in raw
    assert '"onefile=benchmark-dist\\onefile\\opensre.exe"' in raw
    assert '"onedir=benchmark-dist\\onedir\\opensre\\opensre.exe"' in raw
    assert "continue-on-error" not in raw
    assert "threshold" not in raw.lower()
    assert "actions/upload-artifact@v4" in raw
