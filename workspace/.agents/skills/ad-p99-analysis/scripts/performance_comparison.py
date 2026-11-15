#!/usr/bin/env python3
"""Combine Sheets AD metrics with per-state PCM performance logs."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ad_p99 import InputError, SCENARIOS, comparison_context, read_local
from lark_source import read_online
from state_performance import extract as extract_state_performance


METRIC_SPECS = (
    ("p99_infer_delay_ms", "P99 Infer Delay", "p99_ms", None, "mean", "lower", 2),
    ("frame_rate_hz", "Frame Rate", "frame_rate_hz", None, "mean", "higher", 2),
    ("system_cpu_percent", "Sys CPU Usage", None, "system_cpu_percent", "mean", "lower", 2),
    ("pcm_p99_infer_delay_ms", "PCM P99 Infer Delay", None, "pcm_p99_infer_delay_ms", "p99", "lower", 2),
    ("pcm_frame_rate_hz", "PCM Frame Rate", None, "pcm_frame_rate_hz", "mean", "higher", 2),
    ("pcm_cpu_percent", "PCM CPU Usage", None, "pcm_cpu_percent", "mean", "lower", 2),
)
REPORT_ORDER = ("slow_driver", "fast_driver", "parking", "CNOA")
SCENARIO_LABELS = {key: label for key, label, _, _ in SCENARIOS}
SCENARIO_LABELS["CNOA"] = "CNOA"


def finite_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def sheet_value(result, scenario, field):
    row = result.get("metrics", {}).get(scenario, {})
    value = finite_number(row.get(field))
    status_field = "status" if field == "p99_ms" else "frame_rate_status"
    return value if row.get(status_field) == "ok" else None


def state_value(result, scenario, field, statistic="mean"):
    if not isinstance(result, dict):
        return None, 0
    metric = result.get("metrics", {}).get(scenario, {}).get(field, {})
    if not isinstance(metric, dict):
        return None, 0
    value = finite_number(metric.get(statistic))
    count = metric.get("count")
    count = count if type(count) is int and count >= 0 else 0
    return (value if count else None), count


def compare_pair(before, after, *, direction):
    pair = {"baseline": before, "replacement": after, "delta": None, "status": "missing", "tone": "neutral"}
    if before is None or after is None:
        return pair
    delta = float(Decimal(str(after)) - Decimal(str(before)))
    pair.update(delta=delta, status="ok")
    if delta:
        improved = delta < 0 if direction == "lower" else delta > 0
        pair["tone"] = "improved" if improved else "regressed"
    return pair


def display_number(value, digits):
    return f"{value:.{digits}f}"


def display_pair(pair, digits):
    if pair["status"] != "ok":
        return {"baseline": "—", "replacement": "—", "tone": "neutral"}
    return {
        "baseline": display_number(pair["baseline"], digits),
        "replacement": f"{display_number(pair['replacement'], digits)} ({pair['delta']:+.{digits}f})",
        "tone": pair["tone"],
    }


def safe_source(source):
    """Keep reproducibility fields while omitting report URLs and local paths."""
    if not isinstance(source, dict):
        return {}
    return {key: source[key] for key in ("kind", "revision", "snapshot_sha256", "identity", "name", "sha256") if key in source}


def report_title(vehicle_label, baseline_start):
    try:
        day = datetime.fromisoformat(baseline_start.replace("Z", "+00:00")).strftime("%Y%m%d")
    except ValueError:
        day = "日期未知"
    label = str(vehicle_label or "").strip()
    if label and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,31}", label) is None:
        raise InputError("报告车辆标识格式无效。")
    prefix = label or "AD性能对比"
    return f"{prefix}（{day}基线）走查测试"


def compare_performance(baseline_sheet, replacement_sheet, *, baseline_metadata, replacement_metadata,
                        baseline_state=None, replacement_state=None,
                        baseline_sheet_url=None, replacement_sheet_url=None,
                        vehicle_label=None):
    baseline_context = comparison_context(baseline_metadata, "Baseline")
    replacement_context = comparison_context(replacement_metadata, "PCM更新")
    if baseline_context["vin"] != replacement_context["vin"]:
        raise InputError("Baseline 与 PCM更新 metadata 的 vin 不一致，拒绝生成跨 VIN 对比。")

    comparisons, issues, display_rows = {}, [], []
    coverage = {"baseline": {}, "replacement": {}}
    complete_pairs = 0
    for scenario in REPORT_ORDER:
        label = SCENARIO_LABELS[scenario]
        metric_rows = {}
        baseline_coverage = {
            "p99_available": sheet_value(baseline_sheet, scenario, "p99_ms") is not None,
            "frame_rate_available": sheet_value(baseline_sheet, scenario, "frame_rate_hz") is not None,
        }
        replacement_coverage = {
            "p99_available": sheet_value(replacement_sheet, scenario, "p99_ms") is not None,
            "frame_rate_available": sheet_value(replacement_sheet, scenario, "frame_rate_hz") is not None,
        }
        display_row = [label]
        for metric_key, metric_label, sheet_field, state_field, statistic, direction, digits in METRIC_SPECS:
            if sheet_field:
                before = sheet_value(baseline_sheet, scenario, sheet_field)
                after = sheet_value(replacement_sheet, scenario, sheet_field)
                before_count = after_count = 1 if before is not None and after is not None else None
            else:
                before, before_count = state_value(baseline_state, scenario, state_field, statistic)
                after, after_count = state_value(replacement_state, scenario, state_field, statistic)
                baseline_coverage[f"{metric_key}_samples"] = before_count
                replacement_coverage[f"{metric_key}_samples"] = after_count
            pair = compare_pair(before, after, direction=direction)
            metric_rows[metric_key] = pair
            shown = display_pair(pair, digits)
            display_row.extend((shown["baseline"], shown["replacement"]))
            if pair["status"] == "ok":
                complete_pairs += 1
            else:
                missing = []
                if before is None:
                    missing.append("Baseline")
                if after is None:
                    missing.append("PCM更新")
                issues.append({
                    "scenario": scenario,
                    "metric": metric_key,
                    "message": f"{'、'.join(missing)} 缺少可用的 {metric_label} 数据。",
                })
        coverage["baseline"][scenario] = baseline_coverage
        coverage["replacement"][scenario] = replacement_coverage
        comparisons[scenario] = {"label": label, "metrics": metric_rows}
        display_rows.append(display_row)

    total_pairs = len(REPORT_ORDER) * len(METRIC_SPECS)
    for side, state in (("baseline", baseline_state), ("replacement", replacement_state)):
        state_coverage = state.get("coverage", {}) if isinstance(state, dict) else {}
        coverage[side]["unattributed_fps_samples"] = state_coverage.get("fps_samples_unattributed", 0)
        coverage[side]["performance_status"] = state.get("status", "not_provided") if isinstance(state, dict) else "not_provided"
        if isinstance(state, dict):
            coverage[side]["ctx_validation"] = dict(state.get("ctx_validation") or {})
            coverage[side]["frame_diagnostics"] = dict(state.get("frame_diagnostics") or {})
            coverage[side]["fps_diagnostics"] = dict(state.get("fps_diagnostics") or {})
        if isinstance(state, dict) and state.get("error"):
            coverage[side]["performance_error"] = str(state["error"])
            issues.append({
                "scenario": None,
                "metric": "cpu_log_source",
                "message": f"{side} 的 log_fsdA_service 获取或解析失败：{state['error']}",
            })

    columns = ["车辆模式"]
    for _, label, _, _, _, _, _ in METRIC_SPECS:
        columns.extend((f"{label} Base", f"{label} PCM更新"))
    result = {
        "schema_version": 1,
        "kind": "integrated_test_performance_comparison",
        "status": "ok" if complete_pairs == total_pairs else "partial" if complete_pairs else "failed",
        "title": report_title(vehicle_label, baseline_context["start"]),
        "vehicle_label": vehicle_label,
        "baseline": {"time_range": {"start": baseline_context["start"], "end": baseline_context["end"]},
                     "source": safe_source(baseline_sheet.get("source"))},
        "replacement": {"time_range": {"start": replacement_context["start"], "end": replacement_context["end"]},
                        "source": safe_source(replacement_sheet.get("source"))},
        "metric_rules": {
            "p99_infer_delay_ms": "Sheets P99; lower is better",
            "frame_rate_hz": "Sheets mean; higher is better",
            "system_cpu_percent": "mean(sys_cpu); lower is better",
            "pcm_p99_infer_delay_ms": "nearest-rank P99(Post timing - Pre start), same PID/frame/ctx; lower is better",
            "pcm_frame_rate_hz": "mean(FPSlast10frames), only homogeneous validated 10-frame state windows; higher is better",
            "pcm_cpu_percent": "mean(infer_expert_pc_cpu / 19); lower is better",
        },
        "comparisons": comparisons,
        "table": {"columns": columns, "rows": display_rows},
        "coverage": coverage,
        "issues": issues,
    }
    if bool(baseline_sheet_url) != bool(replacement_sheet_url):
        raise InputError("报告来源链接必须同时提供 Baseline 与 PCM更新。")
    if baseline_sheet_url and replacement_sheet_url:
        from feishu_report import build_report_document
        result["report_document"] = build_report_document(
            result,
            baseline_sheet_url=baseline_sheet_url,
            replacement_sheet_url=replacement_sheet_url,
        )
    return result


def load_json_object(path, label):
    try:
        value = json.loads(read_local(path).decode("utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise InputError(f"{label} 不是有效 JSON。") from exc
    if not isinstance(value, dict):
        raise InputError(f"{label} 必须是 JSON 对象。")
    return value


def load_state(*, log_dir, json_path, metadata, label):
    if json_path:
        return load_json_object(json_path, label)
    if log_dir:
        start, end = metadata.get("start"), metadata.get("end")
        if not isinstance(start, str) or not start.strip() or not isinstance(end, str) or not end.strip():
            raise InputError(f"{label} metadata 缺少有效 start/end。")
        try:
            return extract_state_performance(log_dir, start, end)
        except (InputError, OSError) as exc:
            return {"status": "failed", "metrics": {}, "coverage": {}, "error": str(exc)}
    return None


def load_pair_states(*, baseline_metadata, replacement_metadata,
                     baseline_log_dir=None, baseline_performance_json=None,
                     replacement_log_dir=None, replacement_performance_json=None,
                     cloud_env="auto", settings_path=None, skip_cpu=False):
    """Use explicit local inputs, or fetch both log_fsdA_service windows automatically."""
    explicit = (
        baseline_log_dir, baseline_performance_json,
        replacement_log_dir, replacement_performance_json,
    )
    if skip_cpu:
        if any(explicit):
            raise InputError("--skip-cpu 不能与本地 log_fsdA_service 性能输入同时使用。")
        return None, None
    if any(explicit):
        return (
            load_state(log_dir=baseline_log_dir, json_path=baseline_performance_json,
                       metadata=baseline_metadata, label="Baseline performance"),
            load_state(log_dir=replacement_log_dir, json_path=replacement_performance_json,
                       metadata=replacement_metadata, label="PCM更新 performance"),
        )

    baseline_context = comparison_context(baseline_metadata, "Baseline")
    replacement_context = comparison_context(replacement_metadata, "PCM更新")
    if baseline_context["vin"] != replacement_context["vin"]:
        raise InputError("Baseline 与 PCM更新 metadata 的 vin 不一致，拒绝查询日志。")
    try:
        from performance_log_source import extract_pair
        baseline, replacement, _ = extract_pair(
            vin=baseline_context["vin"],
            baseline_start=baseline_context["start"],
            baseline_end=baseline_context["end"],
            replacement_start=replacement_context["start"],
            replacement_end=replacement_context["end"],
            environment=cloud_env,
            settings_path=settings_path,
        )
        return baseline, replacement
    except (OSError, RuntimeError, ValueError) as exc:
        # Sheets values remain useful even when the independent performance
        # source is unavailable. Preserve the reason and render log metrics as —.
        failed = {"status": "failed", "metrics": {}, "coverage": {}, "error": str(exc)}
        return dict(failed), dict(failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-sheet-url", required=True)
    parser.add_argument("--replacement-sheet-url", required=True)
    parser.add_argument("--baseline-metadata", type=Path, required=True)
    parser.add_argument("--replacement-metadata", type=Path, required=True)
    parser.add_argument("--identity", choices=("bot", "user"), required=True)
    parser.add_argument("--lark-cli")
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument("--baseline-log-dir", type=Path)
    baseline.add_argument("--baseline-performance-json", type=Path)
    replacement = parser.add_mutually_exclusive_group()
    replacement.add_argument("--replacement-log-dir", type=Path)
    replacement.add_argument("--replacement-performance-json", type=Path)
    parser.add_argument("--cloud-env", choices=("auto", "prod", "test"), default="auto",
                        help="自动性能日志环境；auto 仅在查询成功但数据不足时尝试 test")
    parser.add_argument("--settings", type=Path, help="AD Skill 本地 settings.json；默认使用 Skill 根目录")
    parser.add_argument("--skip-cpu", action="store_true", help="跳过全部 log_fsdA_service 指标；仅用于诊断 Sheets")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output and args.output.exists():
            raise InputError("输出文件已存在；请换一个文件名以保留原结果。")
        baseline_metadata = load_json_object(args.baseline_metadata, "Baseline metadata")
        replacement_metadata = load_json_object(args.replacement_metadata, "PCM更新 metadata")
        baseline_sheet = read_online(args.baseline_sheet_url, identity=args.identity,
                                     executable=args.lark_cli, metadata=baseline_metadata)
        replacement_sheet = read_online(args.replacement_sheet_url, identity=args.identity,
                                        executable=args.lark_cli, metadata=replacement_metadata)
        baseline_state, replacement_state = load_pair_states(
            baseline_metadata=baseline_metadata,
            replacement_metadata=replacement_metadata,
            baseline_log_dir=args.baseline_log_dir,
            baseline_performance_json=args.baseline_performance_json,
            replacement_log_dir=args.replacement_log_dir,
            replacement_performance_json=args.replacement_performance_json,
            cloud_env=args.cloud_env,
            settings_path=args.settings,
            skip_cpu=args.skip_cpu,
        )
        result = compare_performance(
            baseline_sheet, replacement_sheet,
            baseline_metadata=baseline_metadata, replacement_metadata=replacement_metadata,
            baseline_state=baseline_state, replacement_state=replacement_state,
            baseline_sheet_url=args.baseline_sheet_url,
            replacement_sheet_url=args.replacement_sheet_url,
        )
        payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        return {"ok": 0, "partial": 2, "failed": 1}[result["status"]]
    except (InputError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, InputError) else f"性能对比失败（{type(exc).__name__}）。"
        print(json.dumps({"status": "failed", "error": message}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
