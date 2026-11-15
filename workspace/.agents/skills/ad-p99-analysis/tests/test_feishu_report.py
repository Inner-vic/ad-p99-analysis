import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from feishu_report import METRICS, build_report_document
from performance_comparison import METRIC_SPECS, compare_pair


def pair(before=None, after=None, delta=None, status="ok", tone="neutral"):
    return {"baseline": before, "replacement": after, "delta": delta, "status": status, "tone": tone}


class FeishuReportTests(unittest.TestCase):
    def test_renders_compact_two_level_table_with_colors_and_links(self):
        metrics = {
            "p99_infer_delay_ms": pair(318.5, 308, -10.5, tone="improved"),
            "frame_rate_hz": pair(4.98, 5.01, 0.03, tone="improved"),
            "system_cpu_percent": pair(66.03, 65.39, -0.64, tone="improved"),
            "pcm_p99_infer_delay_ms": pair(126.5, 118.2, -8.3, tone="improved"),
            "pcm_frame_rate_hz": pair(9.2, 9.5, 0.3, tone="improved"),
            "pcm_cpu_percent": pair(5.24, 4.93, -0.31, tone="improved"),
        }
        result = {
            "title": "9A04（20260914基线）走查测试",
            "comparisons": {
                "slow_driver": {"label": "低速人驾", "metrics": metrics},
                "fast_driver": {"label": "高速人驾", "metrics": {
                    **metrics, "p99_infer_delay_ms": pair(347.88, 355.33, 7.45, tone="regressed"),
                }},
                "parking": {"label": "泊车", "metrics": {
                    key: pair(status="missing") for key in metrics
                }},
                "CNOA": {"label": "CNOA", "metrics": metrics},
            },
        }
        document = build_report_document(
            result,
            baseline_sheet_url="https://example.feishu.cn/sheets/baseline",
            replacement_sheet_url="https://example.feishu.cn/sheets/replacement",
        )
        root = ET.fromstring("<root>" + document["xml"] + "</root>")
        table = root.find("table")
        self.assertIsNotNone(table)
        self.assertEqual(table.find("thead/tr/th").attrib["rowspan"], "2")
        self.assertEqual(len(table.findall("thead/tr")[0].findall("th")), 7)
        self.assertEqual(
            ["".join(cell.itertext()) for cell in table.findall("thead/tr")[0].findall("th")],
            ["车辆模式", "P99 Infer Delay", "Frame Rate", "Sys CPU Usage",
             "PCM P99 Infer Delay", "PCM Frame Rate", "PCM CPU Usage"],
        )
        self.assertEqual(METRICS, tuple((spec[0], spec[1]) for spec in METRIC_SPECS))
        self.assertTrue(all(cell.attrib.get("colspan") == "2" for cell in table.findall("thead/tr")[0].findall("th")[1:]))
        self.assertEqual(len(table.findall("thead/tr")[1].findall("th")), 12)
        self.assertEqual(len(table.findall("tbody/tr")), 4)
        self.assertIn('<span text-color="green">(-10.5)</span>', document["xml"])
        self.assertIn('<span text-color="red">(+7.45)</span>', document["xml"])
        self.assertIn("<p align=\"center\">-</p>", document["xml"])
        self.assertIn("Baseline 表格", document["xml"])
        self.assertIn("PCM更新表格", document["xml"])
        self.assertIn("PCM P99 Infer Delay", document["xml"])
        self.assertIn("PCM Frame Rate", document["xml"])
        legend = "Δ = PCM更新 − base；绿色表示改善，红色表示退化；时延和 CPU 占用越低越好，帧率越高越好；0 表示无变化。"
        self.assertIn(legend, document["xml"])
        self.assertLess(document["xml"].index("</table>"), document["xml"].index(legend))
        self.assertLess(document["xml"].index(legend), document["xml"].index("数据来源："))
        self.assertNotIn("coverage", document["xml"])
        self.assertNotIn("issues", document["xml"])

    def test_six_metric_directions_color_improvements_and_regressions_with_neutral_zero_missing(self):
        lower_is_better = {
            "p99_infer_delay_ms", "system_cpu_percent",
            "pcm_p99_infer_delay_ms", "pcm_cpu_percent",
        }
        scenarios = (
            ("slow_driver", 100, 99, "-1"),
            ("fast_driver", 100, 101, "+1"),
            ("parking", 100, 100, "0"),
            ("CNOA", None, 100, None),
        )
        comparisons = {}
        for scenario, before, after, _ in scenarios:
            comparisons[scenario] = {"metrics": {
                spec[0]: compare_pair(before, after, direction=spec[5])
                for spec in METRIC_SPECS
            }}
        document = build_report_document(
            {"comparisons": comparisons},
            baseline_sheet_url="https://example.feishu.cn/sheets/baseline",
            replacement_sheet_url="https://example.feishu.cn/sheets/replacement",
        )
        root = ET.fromstring("<root>" + document["xml"] + "</root>")
        for row, (scenario, _, _, delta) in zip(root.findall("table/tbody/tr"), scenarios):
            for cell, spec in zip(row.findall("td")[2::2], METRIC_SPECS):
                metric = spec[0]
                with self.subTest(scenario=scenario, metric=metric):
                    span = cell.find("p/span")
                    if delta in {"0", None}:
                        self.assertIsNone(span)
                        self.assertEqual("".join(cell.itertext()), "100 (0)" if delta == "0" else "-")
                    else:
                        improved = (delta == "-1") == (metric in lower_is_better)
                        self.assertEqual(span.attrib["text-color"], "green" if improved else "red")
                        self.assertEqual(span.text, f"({delta})")

    def test_rounded_zero_delta_is_neutral_despite_metric_direction_tone(self):
        for after in (100.001, 99.999):
            with self.subTest(after=after):
                metrics = {
                    spec[0]: compare_pair(100, after, direction=spec[5])
                    for spec in METRIC_SPECS
                }
                document = build_report_document(
                    {"comparisons": {"slow_driver": {"metrics": metrics}}},
                    baseline_sheet_url="https://example.feishu.cn/sheets/baseline",
                    replacement_sheet_url="https://example.feishu.cn/sheets/replacement",
                )
                root = ET.fromstring("<root>" + document["xml"] + "</root>")
                row = root.findall("table/tbody/tr")[0]
                for cell in row.findall("td")[2::2]:
                    self.assertEqual("".join(cell.itertext()), "100 (0)")
                    self.assertIsNone(cell.find("p/span"))


if __name__ == "__main__":
    unittest.main()
