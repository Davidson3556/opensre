"""Contracts for the native Windows packaging benchmark."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

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
BinaryTarget = _benchmark.BinaryTarget
comparison_for = _benchmark.comparison_for
measure_targets = _benchmark.measure_targets
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


def test_summary_keeps_initial_launches_separate_from_warm_distribution() -> None:
    measurements = [
        Measurement("onefile", "initial", 1, 40.0),
        Measurement("onedir", "initial", 1, 12.0),
        Measurement("onedir", "initial", 2, 10.0),
        Measurement("onefile", "initial", 2, 38.0),
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
        "initial_median_ms": 39.0,
        "initial_samples_ms": [40.0, 38.0],
        "warm_median_ms": 37.0,
        "warm_p95_ms": 38.0,
        "warm_samples_ms": [38.0, 36.0, 37.0],
    }
    assert summary["onedir"]["warm_median_ms"] == 7.0
    assert comparison == {"onedir_saved_ms": 30.0, "onedir_speedup": 5.286}


def test_measure_targets_balances_order_and_isolates_homes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    targets = [
        BinaryTarget("onefile", tmp_path / "onefile.exe"),
        BinaryTarget("onedir", tmp_path / "onedir.exe"),
    ]
    for target in targets:
        target.path.touch()
    calls: list[tuple[str, Path]] = []

    def _record_run(target: Any, *, home: Path, timeout_seconds: float) -> float:
        assert timeout_seconds == 120
        calls.append((target.mode, home))
        return float(len(calls))

    monkeypatch.setattr(_benchmark, "_run_once", _record_run)

    measurements = measure_targets(
        targets,
        warm_runs=20,
        timeout_seconds=120,
        home_root=tmp_path / "homes",
    )

    assert [measurement.mode for measurement in measurements[:4]] == [
        "onefile",
        "onedir",
        "onedir",
        "onefile",
    ]
    assert [measurement.mode for measurement in measurements[4:]] == [
        "onefile",
        "onedir",
        "onedir",
        "onefile",
    ] * 10
    assert calls[:4] == [
        ("onefile", tmp_path / "homes" / "initial" / "1" / "onefile"),
        ("onedir", tmp_path / "homes" / "initial" / "1" / "onedir"),
        ("onedir", tmp_path / "homes" / "initial" / "2" / "onedir"),
        ("onefile", tmp_path / "homes" / "initial" / "2" / "onefile"),
    ]
    warm_homes = {
        "onefile": tmp_path / "homes" / "initial" / "2" / "onefile",
        "onedir": tmp_path / "homes" / "initial" / "2" / "onedir",
    }
    assert dict(calls[4:]) == warm_homes
    assert all((mode, home) in calls[:4] for mode, home in warm_homes.items())


@pytest.mark.parametrize(
    ("warm_runs", "message"),
    [(18, "at least 20"), (21, "must be even")],
)
def test_measure_targets_rejects_unrepresentative_sample_counts(
    warm_runs: int,
    message: str,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "opensre.exe"
    binary.touch()
    with pytest.raises(ValueError, match=message):
        measure_targets(
            [BinaryTarget("onefile", binary)],
            warm_runs=warm_runs,
            timeout_seconds=120,
            home_root=tmp_path / "homes",
        )


def test_markdown_does_not_overstate_the_measurement() -> None:
    summary = {
        "onefile": {
            "initial_median_ms": 39.0,
            "initial_samples_ms": [40.0, 38.0],
            "warm_median_ms": 37.0,
            "warm_p95_ms": 38.0,
            "warm_samples_ms": [36.0, 37.0, 38.0],
        },
        "onedir": {
            "initial_median_ms": 11.0,
            "initial_samples_ms": [12.0, 10.0],
            "warm_median_ms": 7.0,
            "warm_p95_ms": 8.0,
            "warm_samples_ms": [6.0, 7.0, 8.0],
        },
    }

    markdown = render_markdown(
        summary,
        comparison_for(summary),
        {
            "onefile": {"file_count": 1, "size_bytes": 100 * 1024 * 1024},
            "onedir": {"file_count": 4_900, "size_bytes": 180 * 1024 * 1024},
        },
        {
            "platform": "Windows-11",
            "defender_realtime_enabled": True,
        },
    )

    assert "not reboot-cold measurements" in markdown
    assert "not a ConPTY time-to-prompt result" in markdown
    assert "must not be used as a release gate" in markdown
    assert "Defender real-time protection: `True`" in markdown
    assert "| onefile | 39.0 | 37.0 | 38.0 | 1 | 100.0 |" in markdown
    assert "| onedir | 11.0 | 7.0 | 8.0 | 4900 | 180.0 |" in markdown


def test_markdown_reports_onedir_regression_without_calling_it_a_saving() -> None:
    summary = {
        "onefile": {
            "initial_median_ms": 6.0,
            "initial_samples_ms": [6.0, 6.0],
            "warm_median_ms": 5.0,
            "warm_p95_ms": 6.0,
            "warm_samples_ms": [4.0, 5.0, 6.0],
        },
        "onedir": {
            "initial_median_ms": 12.0,
            "initial_samples_ms": [12.0, 12.0],
            "warm_median_ms": 10.0,
            "warm_p95_ms": 12.0,
            "warm_samples_ms": [8.0, 10.0, 12.0],
        },
    }

    markdown = render_markdown(
        summary,
        comparison_for(summary),
        {
            "onefile": {"file_count": 1, "size_bytes": 1},
            "onedir": {"file_count": 2, "size_bytes": 2},
        },
    )

    assert "Onedir was **5.0 ms slower**" in markdown
    assert "Onedir saved" not in markdown


def test_workflow_builds_both_modes_on_one_windows_runner_without_a_timing_gate() -> None:
    raw = _WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    benchmark = workflow["jobs"]["benchmark"]

    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    assert benchmark["runs-on"] == "windows-latest"
    assert "OPENSRE_PYINSTALLER_MODE = $mode" in raw
    assert 'default: "20"' in raw
    assert 'Join-Path $env:RUNNER_TEMP "opensre-startup-binaries"' in raw
    assert '--binary "onefile=$onefileBinary"' in raw
    assert '--binary "onedir=$onedirBinary"' in raw
    assert "$onefileVersion -ne $onedirVersion" in raw
    assert "if: always()" not in raw
    assert "continue-on-error" not in raw
    assert "threshold" not in raw.lower()
    assert "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5" in raw
    assert "astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d" in raw
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in raw
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in raw
