import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ad_p99
import performance_comparison
from test_ad_p99 import parse, sample_book


def state_result(offset=0):
    metrics = {}
    for index, key in enumerate(("slow_driver", "fast_driver", "parking", "CNOA")):
        metrics[key] = {
            "pcm_cpu_percent": {"mean": 4.0 + index + offset, "count": 3},
            "system_cpu_percent": {"mean": 60.0 + index + offset, "count": 3},
            "pcm_p99_infer_delay_ms": {"p99": 120.0 + index + offset, "count": 20},
            "pcm_frame_rate_hz": {"mean": 9.0 + index + offset, "count": 3},
        }
    return {"status": "ok", "metrics": metrics, "coverage": {"fps_samples_unattributed": 2}}


class PerformanceComparisonTests(unittest.TestCase):
    def setUp(self):
        self.baseline = parse(sample_book())
        replacement_book = sample_book()
        replacement_book["VLA"]["D22"] = "5.01/-/-/-/8.37/8.49/-/-"
        replacement_book["VLA Parking"]["D12"] = "4.02/8.11/-/-/-/-/-/-"
        self.replacement = parse(replacement_book)
        self.baseline_metadata = {
            "vin": "DEM0VEH1CLEX00003", "start": "2026-09-11T10:00:00+08:00", "end": "2026-09-11T10:30:00+08:00",
        }
        self.replacement_metadata = {
            "vin": "DEM0VEH1CLEX00003", "start": "2026-09-11T11:00:00+08:00", "end": "2026-09-11T11:30:00+08:00",
        }

    def test_combines_sheet_fps_and_log_cpu_in_fixed_order(self):
        result = performance_comparison.compare_performance(
            self.baseline, self.replacement,
            baseline_metadata=self.baseline_metadata,
            replacement_metadata=self.replacement_metadata,
            baseline_state=state_result(), replacement_state=state_result(-0.1),
            vehicle_label="9A1A",
        )
        self.assertEqual(result["status"], "ok")
        metric_labels = [
            "P99 Infer Delay", "Frame Rate", "Sys CPU Usage",
            "PCM P99 Infer Delay", "PCM Frame Rate", "PCM CPU Usage",
        ]
        self.assertEqual(
            result["table"]["columns"],
            ["车辆模式"] + [f"{label} {side}" for label in metric_labels for side in ("Base", "PCM更新")],
        )
        self.assertEqual([row[0] for row in result["table"]["rows"]], ["低速人驾", "高速人驾", "泊车", "CNOA"])
        slow = result["comparisons"]["slow_driver"]["metrics"]
        self.assertAlmostEqual(slow["frame_rate_hz"]["delta"], 0.02)
        self.assertEqual(slow["frame_rate_hz"]["tone"], "improved")
        self.assertAlmostEqual(slow["pcm_cpu_percent"]["delta"], -0.1)
        self.assertEqual(slow["pcm_cpu_percent"]["tone"], "improved")
        self.assertAlmostEqual(slow["pcm_p99_infer_delay_ms"]["delta"], -0.1)
        self.assertEqual(slow["pcm_p99_infer_delay_ms"]["tone"], "improved")
        self.assertAlmostEqual(slow["pcm_frame_rate_hz"]["delta"], -0.1)
        self.assertEqual(slow["pcm_frame_rate_hz"]["tone"], "regressed")
        self.assertEqual(result["vehicle_label"], "9A1A")
        self.assertEqual(result["title"], "9A1A（20260911基线）走查测试")
        self.assertNotIn("99F3", result["title"])
        self.assertNotIn("url", str(result).lower())

    def test_missing_one_side_shows_both_cells_as_dash_without_hiding_other_metrics(self):
        self.baseline["metrics"]["parking"].update(frame_rate_hz=None, frame_rate_status="missing")
        result = performance_comparison.compare_performance(
            self.baseline, self.replacement,
            baseline_metadata=self.baseline_metadata,
            replacement_metadata=self.replacement_metadata,
            baseline_state=None, replacement_state=None,
        )
        self.assertEqual(result["status"], "partial")
        parking = result["comparisons"]["parking"]["metrics"]["frame_rate_hz"]
        self.assertEqual(parking["status"], "missing")
        frame_rate_issue = next(
            issue for issue in result["issues"]
            if issue["scenario"] == "parking" and issue["metric"] == "frame_rate_hz"
        )
        self.assertEqual(frame_rate_issue["message"], "Baseline 缺少可用的 Frame Rate 数据。")
        self.assertEqual(result["table"]["rows"][2][3:5], ["—", "—"])
        self.assertEqual(result["comparisons"]["slow_driver"]["metrics"]["p99_infer_delay_ms"]["status"], "ok")

    def test_existing_two_url_cli_now_uses_integrated_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline_path = Path(temporary) / "baseline.json"
            replacement_path = Path(temporary) / "replacement.json"
            baseline_path.write_text(json.dumps(self.baseline_metadata), encoding="utf-8")
            replacement_path.write_text(json.dumps(self.replacement_metadata), encoding="utf-8")
            out = io.StringIO()
            with patch("lark_source.read_online", side_effect=[self.baseline, self.replacement]), \
                    contextlib.redirect_stdout(out):
                code = ad_p99.main([
                    "--baseline-sheet-url", "https://example.feishu.cn/sheets/base",
                    "--replacement-sheet-url", "https://example.feishu.cn/sheets/replacement",
                    "--baseline-metadata", str(baseline_path),
                    "--replacement-metadata", str(replacement_path),
                    "--identity", "user",
                    "--skip-cpu",
                ])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["kind"], "integrated_test_performance_comparison")
        self.assertEqual(payload["table"]["rows"][0][3:5], ["4.99", "5.01 (+0.02)"])

    def test_default_cpu_source_fetches_both_metadata_windows(self):
        with patch("performance_log_source.extract_pair", return_value=(state_result(), state_result(-0.1), "prod")) as fetch:
            baseline, replacement = performance_comparison.load_pair_states(
                baseline_metadata=self.baseline_metadata,
                replacement_metadata=self.replacement_metadata,
            )
        self.assertEqual(baseline["status"], "ok")
        self.assertEqual(replacement["status"], "ok")
        self.assertEqual(fetch.call_args.kwargs["vin"], "DEM0VEH1CLEX00003")
        self.assertEqual(fetch.call_args.kwargs["environment"], "auto")

    def test_cpu_source_failure_preserves_sheet_report_as_partial(self):
        with patch("performance_log_source.extract_pair", side_effect=RuntimeError("synthetic unavailable")):
            baseline_state, replacement_state = performance_comparison.load_pair_states(
                baseline_metadata=self.baseline_metadata,
                replacement_metadata=self.replacement_metadata,
            )
        result = performance_comparison.compare_performance(
            self.baseline, self.replacement,
            baseline_metadata=self.baseline_metadata,
            replacement_metadata=self.replacement_metadata,
            baseline_state=baseline_state,
            replacement_state=replacement_state,
        )
        self.assertEqual(result["status"], "partial")
        self.assertIn("synthetic unavailable", result["coverage"]["baseline"]["performance_error"])
        self.assertEqual(result["table"]["rows"][0][5:13], ["—"] * 8)


if __name__ == "__main__":
    unittest.main()
