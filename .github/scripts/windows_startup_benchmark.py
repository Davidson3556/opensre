"""Compare frozen Windows launch time without changing release behavior."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_STARTUP_ARGS = ("--skip-onboarding", "--no-interactive")
_BENCHMARK_ENV = {
    "DO_NOT_TRACK": "1",
    "NO_COLOR": "1",
    "OPENSRE_ANALYTICS_DISABLED": "1",
    "OPENSRE_CICD": "1",
    "OPENSRE_NO_UPDATE_CHECK": "1",
    "OPENSRE_SENTRY_DISABLED": "1",
    "TERM": "dumb",
}


@dataclass(frozen=True)
class BinaryTarget:
    """One packaging mode and the frozen executable built for it."""

    mode: str
    path: Path


@dataclass(frozen=True)
class Measurement:
    """One complete child-process launch."""

    mode: str
    phase: str
    iteration: int
    elapsed_ms: float


def nearest_rank_percentile(values: list[float], percentile: float) -> float:
    """Return the nearest-rank percentile for a non-empty sample."""
    if not values:
        raise ValueError("percentile requires at least one sample")
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be greater than 0 and at most 100")
    ordered = sorted(values)
    rank = math.ceil((percentile / 100) * len(ordered))
    return ordered[rank - 1]


def parse_binary_target(value: str) -> BinaryTarget:
    """Parse ``MODE=PATH`` from the command line."""
    mode, separator, raw_path = value.partition("=")
    if not separator or not mode.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("binary must use MODE=PATH")
    return BinaryTarget(mode=mode.strip(), path=Path(raw_path.strip()).resolve())


def _benchmark_environment(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(_BENCHMARK_ENV)
    env["OPENSRE_HOME"] = str(home)
    return env


def _windows_facts() -> dict[str, Any]:
    """Return runner and best-effort Defender state for interpreting timings."""
    facts: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "defender_antivirus_enabled": None,
        "defender_realtime_enabled": None,
    }
    command = (
        "$status = Get-MpComputerStatus -ErrorAction Stop; "
        "[ordered]@{"
        "defender_antivirus_enabled=[bool]$status.AntivirusEnabled; "
        "defender_realtime_enabled=[bool]$status.RealTimeProtectionEnabled"
        "} | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8-sig",
            errors="replace",
            timeout=15,
        )
        if result.returncode == 0:
            defender = json.loads(result.stdout)
            if isinstance(defender, dict):
                facts.update(defender)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return facts


def _binary_inventory(target: BinaryTarget) -> dict[str, Any]:
    """Describe the executable or complete onedir tree after timing finishes."""
    bundle_root = target.path.parent if (target.path.parent / "_internal").is_dir() else target.path
    if bundle_root.is_file():
        return {"path": str(target.path), "file_count": 1, "size_bytes": bundle_root.stat().st_size}
    files = [path for path in bundle_root.rglob("*") if path.is_file()]
    return {
        "path": str(target.path),
        "file_count": len(files),
        "size_bytes": sum(path.stat().st_size for path in files),
    }


def _run_once(target: BinaryTarget, *, home: Path, timeout_seconds: float) -> float:
    started_ns = time.perf_counter_ns()
    try:
        result = subprocess.run(
            [str(target.path), *_STARTUP_ARGS],
            cwd=home,
            env=_benchmark_environment(home),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{target.mode} launch exceeded {timeout_seconds:g}s") from exc
    elapsed_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
    if result.returncode != 0:
        stdout = result.stdout[-2_000:]
        stderr = result.stderr[-2_000:]
        raise RuntimeError(
            f"{target.mode} launch exited {result.returncode}\n"
            f"stdout (tail):\n{stdout}\nstderr (tail):\n{stderr}"
        )
    return elapsed_ms


def measure_targets(
    targets: list[BinaryTarget],
    *,
    warm_runs: int,
    timeout_seconds: float,
    home_root: Path,
) -> list[Measurement]:
    """Measure one first launch and interleaved warm launches for each target."""
    if not targets:
        raise ValueError("at least one binary target is required")
    if warm_runs < 1:
        raise ValueError("warm_runs must be at least 1")

    seen_modes: set[str] = set()
    homes: dict[str, Path] = {}
    for target in targets:
        if target.mode in seen_modes:
            raise ValueError(f"duplicate packaging mode: {target.mode}")
        seen_modes.add(target.mode)
        if not target.path.is_file():
            raise FileNotFoundError(target.path)
        home = home_root / target.mode
        home.mkdir(parents=True, exist_ok=True)
        homes[target.mode] = home

    measurements: list[Measurement] = []
    for target in targets:
        measurements.append(
            Measurement(
                mode=target.mode,
                phase="first",
                iteration=0,
                elapsed_ms=_run_once(
                    target,
                    home=homes[target.mode],
                    timeout_seconds=timeout_seconds,
                ),
            )
        )

    for iteration in range(1, warm_runs + 1):
        # Reverse every other round so runner drift does not always favor the
        # mode measured second.
        round_targets = targets if iteration % 2 else list(reversed(targets))
        for target in round_targets:
            measurements.append(
                Measurement(
                    mode=target.mode,
                    phase="warm",
                    iteration=iteration,
                    elapsed_ms=_run_once(
                        target,
                        home=homes[target.mode],
                        timeout_seconds=timeout_seconds,
                    ),
                )
            )
    return measurements


def summarize_measurements(measurements: list[Measurement]) -> dict[str, dict[str, Any]]:
    """Summarize first launch and warm distributions by packaging mode."""
    modes = list(dict.fromkeys(measurement.mode for measurement in measurements))
    summary: dict[str, dict[str, Any]] = {}
    for mode in modes:
        first = [
            measurement.elapsed_ms
            for measurement in measurements
            if measurement.mode == mode and measurement.phase == "first"
        ]
        warm = [
            measurement.elapsed_ms
            for measurement in measurements
            if measurement.mode == mode and measurement.phase == "warm"
        ]
        if len(first) != 1 or not warm:
            raise ValueError(f"incomplete measurements for {mode}")
        summary[mode] = {
            "first_ms": round(first[0], 3),
            "warm_median_ms": round(statistics.median(warm), 3),
            "warm_p95_ms": round(nearest_rank_percentile(warm, 95), 3),
            "warm_samples_ms": [round(value, 3) for value in warm],
        }
    return summary


def comparison_for(summary: dict[str, dict[str, Any]]) -> dict[str, float] | None:
    """Return the onedir-versus-onefile warm comparison when both are present."""
    if not {"onefile", "onedir"} <= summary.keys():
        return None
    onefile_ms = float(summary["onefile"]["warm_median_ms"])
    onedir_ms = float(summary["onedir"]["warm_median_ms"])
    if onedir_ms <= 0:
        return None
    return {
        "onedir_saved_ms": round(onefile_ms - onedir_ms, 3),
        "onedir_speedup": round(onefile_ms / onedir_ms, 3),
    }


def render_markdown(
    summary: dict[str, dict[str, Any]],
    comparison: dict[str, float] | None,
    runner: dict[str, Any] | None = None,
) -> str:
    """Render a concise GitHub step summary."""
    lines = [
        "# Windows frozen startup benchmark",
        "",
        "| Packaging | First launch (ms) | Warm median (ms) | Warm p95 (ms) | Warm samples (ms) |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for mode, values in summary.items():
        samples = ", ".join(f"{float(value):.1f}" for value in values["warm_samples_ms"])
        lines.append(
            f"| {mode} | {float(values['first_ms']):.1f} | "
            f"{float(values['warm_median_ms']):.1f} | "
            f"{float(values['warm_p95_ms']):.1f} | {samples} |"
        )
    if comparison is not None:
        saved_ms = comparison["onedir_saved_ms"]
        if saved_ms >= 0:
            comparison_text = (
                f"Onedir saved **{saved_ms:.1f} ms** at the warm median "
                f"(**{comparison['onedir_speedup']:.2f}x** onefile/onedir ratio)."
            )
        else:
            comparison_text = (
                f"Onedir was **{-saved_ms:.1f} ms slower** at the warm median "
                f"(**{comparison['onedir_speedup']:.2f}x** onefile/onedir ratio)."
            )
        lines.extend(["", comparison_text])
    if runner is not None:
        lines.extend(
            [
                "",
                f"Runner: `{runner['platform']}`; Defender real-time protection: "
                f"`{runner['defender_realtime_enabled']}`.",
            ]
        )
    lines.extend(
        [
            "",
            "> This is a same-runner packaging comparison of `opensre --skip-onboarding "
            "--no-interactive`. The first sample is the first process launch after the build; "
            "it is not a reboot-cold measurement. It is also a startup proxy, not a ConPTY "
            "time-to-prompt result. Hosted-runner timings must not be used as a release gate.",
            "",
        ]
    )
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary",
        action="append",
        required=True,
        type=parse_binary_target,
        dest="targets",
        help="Frozen executable as MODE=PATH; repeat for each packaging mode",
    )
    parser.add_argument("--warm-runs", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--home-root", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if sys.platform != "win32":
        raise SystemExit("This benchmark must run on native Windows.")
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be greater than 0")

    targets: list[BinaryTarget] = args.targets
    measurements = measure_targets(
        targets,
        warm_runs=args.warm_runs,
        timeout_seconds=args.timeout_seconds,
        home_root=args.home_root.resolve(),
    )
    summary = summarize_measurements(measurements)
    comparison = comparison_for(summary)
    runner = _windows_facts()
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "commit": os.getenv("GITHUB_SHA", ""),
        "runner": runner,
        "command": list(_STARTUP_ARGS),
        "warm_runs": args.warm_runs,
        "binaries": {target.mode: _binary_inventory(target) for target in targets},
        "measurements": [asdict(measurement) for measurement in measurements],
        "summary": summary,
        "comparison": comparison,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown = render_markdown(summary, comparison, runner)
    args.output_markdown.write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
