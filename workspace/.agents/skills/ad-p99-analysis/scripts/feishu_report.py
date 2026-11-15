#!/usr/bin/env python3
"""Render the compact AD comparison as Feishu Docx XML."""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
from html import escape
from pathlib import Path

from ad_p99 import InputError


METRICS = (
    ("p99_infer_delay_ms", "P99 Infer Delay"),
    ("frame_rate_hz", "Frame Rate"),
    ("system_cpu_percent", "Sys CPU Usage"),
    ("pcm_p99_infer_delay_ms", "PCM P99 Infer Delay"),
    ("pcm_frame_rate_hz", "PCM Frame Rate"),
    ("pcm_cpu_percent", "PCM CPU Usage"),
)
SCENARIOS = ("slow_driver", "fast_driver", "parking", "CNOA")


def _number(value) -> str:
    try:
        number = round(float(value), 2)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(number):
        return "-"
    if number == 0:
        number = 0.0
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _source_url(value: str, label: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise InputError(f"{label} 表格链接格式无效。") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise InputError(f"{label} 表格链接必须是无内嵌凭据的 HTTPS URL。")
    if port not in (None, 443) or "/sheets/" not in parsed.path:
        raise InputError(f"{label} 必须是飞书 Sheets 链接。")
    return value


def _replacement_cell(pair: dict) -> str:
    if pair.get("status") != "ok":
        return "<p align=\"center\">-</p>"
    value = escape(_number(pair.get("replacement")))
    delta = _number(pair.get("delta"))
    if delta != "-" and not delta.startswith("-") and delta != "0":
        delta = "+" + delta
    # Tone accounts for each metric's direction: improved green, regressed red.
    tone = pair.get("tone")
    if delta not in {"-", "0"} and tone in {"improved", "regressed"}:
        color = "green" if tone == "improved" else "red"
        delta_xml = f'<span text-color="{color}">({escape(delta)})</span>'
    else:
        delta_xml = f"({escape(delta)})"
    return f'<p align="center">{value} {delta_xml}</p>'


def _baseline_cell(pair: dict) -> str:
    value = _number(pair.get("baseline")) if pair.get("status") == "ok" else "-"
    return f'<p align="center">{escape(value)}</p>'


def build_report_document(result: dict, *, baseline_sheet_url: str, replacement_sheet_url: str) -> dict:
    """Return a minimal two-level-header document matching the agreed layout."""
    baseline_url = _source_url(baseline_sheet_url, "Baseline")
    replacement_url = _source_url(replacement_sheet_url, "PCM更新")
    title = str(result.get("title") or "AD 性能对比")
    comparisons = result.get("comparisons") or {}

    first_header = ['<th rowspan="2" background-color="light-gray" vertical-align="middle"><p align="center"><b>车辆模式</b></p></th>']
    second_header = []
    for _, label in METRICS:
        first_header.append(
            f'<th colspan="2" background-color="light-gray" vertical-align="middle">'
            f'<p align="center"><b>{escape(label)}</b></p></th>'
        )
        second_header.extend((
            '<th background-color="light-gray"><p align="center"><b>base</b></p></th>',
            '<th background-color="light-gray"><p align="center"><b>PCM更新</b></p></th>',
        ))

    body = []
    for scenario in SCENARIOS:
        row = comparisons.get(scenario) or {}
        label = escape(str(row.get("label") or scenario))
        cells = [f'<td vertical-align="middle"><p align="center"><b>{label}</b></p></td>']
        metrics = row.get("metrics") or {}
        for metric, _ in METRICS:
            pair = metrics.get(metric) or {}
            cells.append(f'<td vertical-align="middle">{_baseline_cell(pair)}</td>')
            cells.append(f'<td vertical-align="middle">{_replacement_cell(pair)}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")

    xml = "".join((
        f"<title>{escape(title)}</title>",
        f'<h2>{escape(title)}</h2>',
        "<table>",
        '<colgroup><col width="100"/><col span="12" width="115"/></colgroup>',
        "<thead><tr>", "".join(first_header), "</tr><tr>", "".join(second_header), "</tr></thead>",
        "<tbody>", "".join(body), "</tbody></table>",
        "<p>Δ = PCM更新 − base；绿色表示改善，红色表示退化；时延和 CPU 占用越低越好，帧率越高越好；0 表示无变化。</p>",
        '<p><b>数据来源：</b>',
        f'<a type="url-preview" href="{escape(baseline_url, quote=True)}">Baseline 表格</a>　',
        f'<a type="url-preview" href="{escape(replacement_url, quote=True)}">PCM更新表格</a></p>',
    ))
    return {
        "format": "lark_docx_xml",
        "title": title,
        "xml": xml,
        "source_links": [
            {"label": "Baseline 表格", "url": baseline_url},
            {"label": "PCM更新表格", "url": replacement_url},
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison-json", type=Path, required=True)
    parser.add_argument("--baseline-sheet-url", required=True)
    parser.add_argument("--replacement-sheet-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise InputError("报告 XML 已存在；请更换输出文件名。")
        if args.comparison_json.stat().st_size > 16 * 1024 * 1024:
            raise InputError("对比 JSON 超过 16 MiB 上限。")
        result = json.loads(args.comparison_json.read_text(encoding="utf-8-sig"))
        if not isinstance(result, dict):
            raise InputError("对比结果必须是 JSON 对象。")
        document = build_report_document(
            result,
            baseline_sheet_url=args.baseline_sheet_url,
            replacement_sheet_url=args.replacement_sheet_url,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(document["xml"] + "\n")
        print(json.dumps({"status": "ok", "output": str(args.output), "title": document["title"]}, ensure_ascii=False))
        return 0
    except (InputError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
