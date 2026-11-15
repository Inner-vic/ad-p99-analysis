import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ad_p99


ORDER = "slow_driver/parking/HNOA/PGear/fast_driver/CNOA/switch_parking_driving/user_RGear"
SOURCE_HEADERS = (
    ("VLA", "B52", "header_delay", "ms", "P99", "D56"),
    ("VLA Parking", "B72", "vla_parking_header_delay", "ms", "P99", "D76"),
    ("VLA", "B22", "VlaModel_FPS", "Hz", "mean", "D22"),
    ("VLA Parking", "B12", "VlaParkingDLM_FPS", "Hz", "mean", "D12"),
)


def sample_book():
    """Synthetic workbook based on the user screenshots, not an upstream export."""
    book = Workbook()
    vla = book.active
    vla.title = "VLA"
    parking = book.create_sheet("VLA Parking")
    for sheet, metric, first, values in (
        (vla, "header_delay", 52, "281.16/-/-/-/194.0/180.0/-/-"),
        (parking, "vla_parking_header_delay", 72, "609.6/377.0/-/-/-/-/-/-"),
    ):
        sheet.merge_cells(start_row=first, start_column=2, end_row=first + 4, end_column=2)
        sheet.cell(first, 2, f"{metric}(ms)\n({ORDER})")
        for offset, statistic in enumerate(("mean", "max", "min", "P90", "P99")):
            sheet.cell(first + offset, 3, statistic)
            sheet.cell(first + offset, 4, values if statistic == "P99" else "999/999/999/999/999/999/999/999")
        # A neighboring P99 must never be mistaken for the header metric.
        sheet.cell(first + 5, 2, f"{metric.replace('header', 'pipeline')}(ms)\n({ORDER})")
        sheet.cell(first + 5, 3, "P99")
        sheet.cell(first + 5, 4, "1/2/3/4/5/6/7/8")
    for sheet, metric, first, values in (
        (vla, "VlaModel_FPS", 22, "4.99/-/-/-/8.25/8.58/-/-"),
        (parking, "VlaParkingDLM_FPS", 12, "4.02/8.03/-/-/-/-/-/-"),
    ):
        sheet.merge_cells(start_row=first, start_column=2, end_row=first + 4, end_column=2)
        sheet.cell(first, 2, f"{metric}(Hz)\n({ORDER})")
        for offset, statistic in enumerate(("mean", "max", "min", "P90", "P99")):
            sheet.cell(first + offset, 3, statistic)
            sheet.cell(first + offset, 4, values if statistic == "mean" else "9/9/9/9/9/9/9/9")
    return book


def as_bytes(book):
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def parse(book):
    return ad_p99.parse_workbook(as_bytes(book))


class ParserTests(unittest.TestCase):
    def test_compact_ignores_invisible_sheet_title_characters(self):
        self.assertEqual(ad_p99.compact("VLA\u200b"), "vla")
        self.assertEqual(ad_p99.compact("VLA\ufeff Parking"), "vlaparking")

    def test_screenshot_values_and_evidence(self):
        data = as_bytes(sample_book())
        result = ad_p99.parse_workbook(data, metadata={"release": "synthetic-example"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual({key: value["p99_ms"] for key, value in result["metrics"].items()}, {
            "slow_driver": 281.16, "fast_driver": 194.0, "CNOA": 180.0, "parking": 377.0,
        })
        self.assertEqual(result["metrics"]["parking"]["evidence"]["value_cell"], "D76")
        self.assertEqual(result["metrics"]["slow_driver"]["evidence"]["value_cell"], "D56")
        self.assertEqual(result["metrics"]["parking"]["evidence"]["scenario_index"], 1)
        self.assertEqual({key: value["frame_rate_hz"] for key, value in result["metrics"].items()}, {
            "slow_driver": 4.99, "fast_driver": 8.25, "CNOA": 8.58, "parking": 8.03,
        })
        self.assertEqual(result["metrics"]["parking"]["frame_rate_evidence"]["value_cell"], "D12")
        self.assertEqual(result["frame_rate_statistic"], "mean")
        self.assertEqual(result["metadata"]["release"], "synthetic-example")
        self.assertEqual(len(result["source"]["sha256"]), 64)

    def test_reordered_scenarios_moved_rows_and_case(self):
        book = sample_book()
        sheet = book["VLA"]
        sheet.unmerge_cells("B52:B56")
        sheet.title = " vLa "
        sheet["B52"] = "header_delay（ms）\n(CNOA/fast_driver/slow_driver)"
        sheet["D56"] = "180/194/281.16"
        sheet.move_range("B52:D57", rows=-40, cols=2)
        result = parse(book)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["metrics"]["slow_driver"]["p99_ms"], 281.16)
        self.assertEqual(result["metrics"]["slow_driver"]["evidence"]["value_cell"], "F16")

    def test_header_threshold_metadata_is_separate_from_scenarios(self):
        for suffix in ("", "\nmax_threshold=1000", "max_threshold=0",
                       "\r\n max_threshold = +1000.5 ", "\nmax_threshold=.5"):
            with self.subTest(suffix=suffix):
                header = f"header_delay(ms)\n({ORDER}){suffix}"
                self.assertEqual(ad_p99.header_labels(header, "header_delay"),
                                 ORDER.casefold().split("/"))

    def test_threshold_header_extracts_p99_and_keeps_frame_rates(self):
        for values, expected in (
            ("251.0/-/-/-/301.36/-/-/-", (251.0, 301.36)),
            ("279.0/-/-/-/299.0/-/-/320.0", (279.0, 299.0)),
        ):
            with self.subTest(values=values):
                book = sample_book()
                book["VLA"]["B52"] = f"header_delay(ms)\n({ORDER})\nmax_threshold=1000"
                book["VLA"]["D56"] = values
                result = parse(book)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["metrics"]["slow_driver"]["p99_ms"], expected[0])
                self.assertEqual(result["metrics"]["fast_driver"]["p99_ms"], expected[1])
                self.assertIsNone(result["metrics"]["CNOA"]["p99_ms"])
                self.assertEqual(result["metrics"]["parking"]["p99_ms"], 377.0)
                for key in ("slow_driver", "fast_driver"):
                    evidence = result["metrics"][key]["evidence"]
                    self.assertEqual(evidence["header_cell"], "B52")
                    self.assertEqual(evidence["statistic_cell"], "C56")
                    self.assertEqual(evidence["value_cell"], "D56")
                    self.assertEqual(evidence["scenario_order"], ORDER.casefold().split("/"))
                self.assertEqual(result["frame_rate_status"], "ok")
                self.assertEqual([value["frame_rate_hz"] for value in result["metrics"].values()],
                                 [4.99, 8.25, 8.58, 8.03])

    def test_threshold_metadata_cannot_supply_missing_or_invalid_p99(self):
        book = sample_book()
        book["VLA"]["B52"] = f"header_delay(ms)\n({ORDER})\nmax_threshold=1000"
        book["VLA"]["D56"] = "0/-/-/-/-1/-/-/-"
        result = parse(book)
        self.assertEqual(result["metrics"]["slow_driver"]["p99_ms"], 0.0)
        self.assertIsNone(result["metrics"]["fast_driver"]["p99_ms"])
        self.assertIsNone(result["metrics"]["CNOA"]["p99_ms"])

    def test_all_metric_headers_ignore_unknown_annotations_and_threshold_values(self):
        suffixes = (
            "\nmax_threshold=", "\nmax_threshold=-1", "\nmax_threshold=nan",
            "\nmax_threshold=inf", "\nmax_threshold=1e3", "\nmax_threshold=1000ms",
            "\nmax_threshold=" + "9" * 400, "\nunknown=1000", "\nextra text",
            "\nmax_threshold=1000\nmax_threshold=1000", "\nmax_threshold=1000\nunknown=1",
            "\nmax_threshold=1000/another_mode", "\nnotes(build)\nsource(foo/bar)",
        )
        for _, _, metric, unit, _, _ in SOURCE_HEADERS:
            for suffix in suffixes:
                with self.subTest(metric=metric, suffix=suffix):
                    header = f"指标说明： {metric.upper()} （ {unit.upper()} ）\n({ORDER}){suffix}"
                    self.assertEqual(ad_p99.header_labels(header, metric, unit),
                                     ORDER.casefold().split("/"))

    def test_all_metric_blocks_accept_prefixes_and_future_metadata(self):
        book = sample_book()
        for sheet, cell, metric, unit, _, _ in SOURCE_HEADERS:
            book[sheet][cell] = (
                f"统计项目： {metric.upper()} （ {unit.upper()} ）\n"
                f"meta(foo/bar)\n({ORDER})\nmax_threshold=upstream-change\nnotes(build)"
            )
        result = parse(book)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["frame_rate_status"], "ok")
        self.assertEqual([item["p99_ms"] for item in result["metrics"].values()],
                         [281.16, 194.0, 180.0, 377.0])
        self.assertEqual([item["frame_rate_hz"] for item in result["metrics"].values()],
                         [4.99, 8.25, 8.58, 8.03])
        for sheet, cell, metric, unit, statistic, value_cell in SOURCE_HEADERS:
            matching = [item["evidence" if unit == "ms" else "frame_rate_evidence"]
                        for item in result["metrics"].values()]
            self.assertTrue(any(evidence["header_cell"] == cell
                                and evidence["sheet"] == sheet
                                and evidence["value_cell"] == value_cell
                                and evidence["statistic"] == statistic
                                for evidence in matching))

    def test_marker_requires_complete_name_and_unit_for_every_metric(self):
        for _, _, metric, unit, _, _ in SOURCE_HEADERS:
            invalid_markers = (f"xxx_{metric}({unit})", f"xxx_ {metric}({unit})",
                               f"{metric}_extra({unit})",
                               f"{metric}({'s' if unit == 'ms' else 'ms'})")
            for marker in invalid_markers:
                with self.subTest(metric=metric, marker=marker), self.assertRaises(ad_p99.InputError):
                    ad_p99.header_labels(f"说明：{marker}({ORDER})", metric, unit)
        with self.assertRaises(ad_p99.InputError):
            ad_p99.header_labels(f"vla_parking_header_delay(ms)({ORDER})", "header_delay")
        with self.assertRaises(ad_p99.InputError):
            ad_p99.header_labels(f"vla_parking_ header_delay(ms)({ORDER})", "header_delay")

    def test_single_scenarios_and_metadata_parentheses_are_distinguished(self):
        for _, _, metric, unit, _, _ in SOURCE_HEADERS:
            with self.subTest(metric=metric):
                header = f"{metric}({unit})\nmeta(foo/bar)\n(parking)\nnotes(build)"
                self.assertEqual(ad_p99.header_labels(header, metric, unit), ["parking"])

    def test_all_metric_headers_reject_ambiguous_or_invalid_scenarios(self):
        for _, _, metric, unit, _, _ in SOURCE_HEADERS:
            marker = f"{metric}({unit})"
            for suffix in ("(slow_driver/slow_driver)", "(slow_driver/)",
                           "(slow_driver/fast_driver)(parking/CNOA)",
                           f"({ORDER})\n{marker}"):
                with self.subTest(metric=metric, suffix=suffix), self.assertRaises(ad_p99.InputError):
                    ad_p99.header_labels(marker + suffix, metric, unit)

    def test_metric_mentions_in_notes_do_not_create_blocks(self):
        book = sample_book()
        for index, (sheet, _, metric, unit, _, _) in enumerate(SOURCE_HEADERS, 1):
            book[sheet].cell(index, 1, f"备注：{metric}({unit}) 的结果位于下方；source(foo/bar)")
        result = parse(book)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["frame_rate_status"], "ok")

    def test_all_metric_blocks_preserve_duplicate_and_statistic_guards(self):
        for sheet_name, cell, metric, unit, statistic, value_cell in SOURCE_HEADERS:
            for failure in ("duplicate", "missing_statistic", "duplicate_statistic", "count"):
                with self.subTest(metric=metric, failure=failure):
                    book = sample_book()
                    sheet = book[sheet_name]
                    anchor = sheet[cell]
                    if failure == "duplicate":
                        sheet.cell(90, 2, f"说明：{metric}({unit})({ORDER})\nnew_metadata=yes")
                    elif failure == "missing_statistic":
                        sheet.cell(sheet[value_cell].row, 3, "other")
                    elif failure == "duplicate_statistic":
                        row = anchor.row + (1 if statistic == "mean" else 2)
                        sheet.cell(row, 3, statistic)
                    else:
                        sheet[value_cell] = "1/2"
                    with self.assertRaises(ad_p99.InputError):
                        ad_p99.find_block(sheet, metric, unit=unit, statistic=statistic)
                    book.close()

    def test_threshold_header_keeps_ambiguity_checks(self):
        header = f"header_delay(ms)\n({ORDER})\nmax_threshold=1000"
        for target, value in (
            ("B60", header), ("C54", "P99"), ("D56", "251/301.36"),
            ("B52", "header_delay(ms)(slow_driver/slow_driver)\nmax_threshold=1000"),
        ):
            with self.subTest(target=target):
                book = sample_book()
                book["VLA"]["B52"] = header
                book["VLA"][target] = value
                result = parse(book)
                self.assertIsNone(result["metrics"]["slow_driver"]["p99_ms"])
                self.assertEqual(result["metrics"]["parking"]["p99_ms"], 377.0)

    def test_missing_value_is_null_and_zero_is_valid(self):
        book = sample_book()
        book["VLA"]["D56"] = "0/-/-/-/-/180/-/-"
        result = parse(book)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["metrics"]["slow_driver"]["p99_ms"], 0)
        self.assertIsNone(result["metrics"]["fast_driver"]["p99_ms"])
        self.assertEqual(result["metrics"]["parking"]["p99_ms"], 377)

    def test_missing_frame_rate_is_independent_from_p99(self):
        book = sample_book()
        book["VLA"]["D22"] = "4.99/-/-/-/-/8.58/-/-"
        result = parse(book)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["frame_rate_status"], "partial")
        self.assertIsNone(result["metrics"]["fast_driver"]["frame_rate_hz"])
        self.assertEqual(result["metrics"]["fast_driver"]["p99_ms"], 194)

    def test_missing_sheet_preserves_other_metrics(self):
        book = sample_book()
        del book["VLA Parking"]
        result = parse(book)
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["metrics"]["parking"]["p99_ms"])
        self.assertEqual(result["metrics"]["slow_driver"]["p99_ms"], 281.16)

    def test_ambiguous_sheet_is_not_selected(self):
        book = sample_book()
        book.create_sheet("VLA  Parking")
        result = parse(book)
        self.assertIsNone(result["metrics"]["parking"]["p99_ms"])

    def test_does_not_fall_through_to_pipeline_p99(self):
        for merged in (True, False):
            with self.subTest(merged=merged):
                book = sample_book()
                if not merged:
                    book["VLA"].unmerge_cells("B52:B56")
                book["VLA"]["C56"] = "other"
                result = parse(book)
                self.assertIsNone(result["metrics"]["slow_driver"]["p99_ms"])

    def test_duplicate_metric_or_p99_is_ambiguous(self):
        for target, value in (("B60", f"header_delay(ms)({ORDER})"), ("C54", "P99")):
            with self.subTest(target=target):
                book = sample_book()
                book["VLA"][target] = value
                result = parse(book)
                self.assertIsNone(result["metrics"]["slow_driver"]["p99_ms"])

    def test_mismatched_count_and_duplicate_labels(self):
        for target, value in (("D56", "281.16/194/180"),
                              ("B52", "header_delay(ms)(slow_driver/slow_driver)")):
            with self.subTest(target=target):
                book = sample_book()
                book["VLA"][target] = value
                result = parse(book)
                self.assertIsNone(result["metrics"]["slow_driver"]["p99_ms"])

    def test_reject_bad_units_nonfinite_and_negative(self):
        for target, value in (("B52", f"header_delay(s)({ORDER})"),
                              ("D56", "nan/-/-/-/inf/-3/-/-")):
            with self.subTest(target=target):
                book = sample_book()
                book["VLA"][target] = value
                result = parse(book)
                self.assertTrue(all(result["metrics"][key]["p99_ms"] is None
                                    for key in ("slow_driver", "fast_driver", "CNOA")))

    def test_formula_without_cache_is_not_evaluated(self):
        book = sample_book()
        book["VLA"]["D56"] = '=CONCAT("281.16/-/-/-/194/180/-/-")'
        result = parse(book)
        self.assertIsNone(result["metrics"]["slow_driver"]["p99_ms"])
        self.assertIn("缓存", result["issues"][0]["message"])

    def test_merged_statistic_columns(self):
        book = sample_book()
        sheet = book["VLA"]
        sheet["E56"] = sheet["D56"].value
        sheet.merge_cells("C56:D56")
        result = parse(book)
        self.assertEqual(result["metrics"]["slow_driver"]["p99_ms"], 281.16)
        self.assertEqual(result["metrics"]["slow_driver"]["evidence"]["value_cell"], "E56")

    def test_missing_scenario_cannot_be_replaced_by_hnoa(self):
        book = sample_book()
        book["VLA"]["B52"] = book["VLA"]["B52"].value.replace("CNOA", "another_mode")
        result = parse(book)
        self.assertIsNone(result["metrics"]["CNOA"]["p99_ms"])
        self.assertEqual(result["metrics"]["fast_driver"]["p99_ms"], 194)

    def test_invalid_file_and_no_target_sheets(self):
        with self.assertRaises(ad_p99.InputError):
            ad_p99.parse_workbook(b"<html>login</html>")
        self.assertEqual(parse(Workbook())["status"], "failed")


class IntakeTests(unittest.TestCase):
    def test_nested_card_url_and_callback(self):
        card = {"elements": [
            {"tag": "button", "text": {"content": "Log下载"}, "url": "https://example.test/log"},
            {"tag": "button", "text": {"content": "查看结果"},
             "multi_url": {"url": "https://example.test/report", "pc_url": "https://example.test/report"}},
            {"tag": "button", "text": "查看结果", "value": {"secret": "not-output"}},
        ]}
        result = ad_p99.inspect_card({"body": {"content": json.dumps(card)}})
        self.assertEqual(result["result_buttons"], [
            {"urls": ["https://example.test/report"], "has_callback": False},
            {"urls": [], "has_callback": True},
        ])
        self.assertNotIn("not-output", json.dumps(result))

    def test_v2_card_and_missing_button(self):
        result = ad_p99.inspect_card({"tag": "button", "text": "查看结果", "behaviors": [
            {"type": "open_url", "default_url": "https://example.test/result.xlsx"},
            {"type": "callback", "value": "ignored"},
        ]})
        self.assertEqual(result["result_buttons"][0]["urls"], ["https://example.test/result.xlsx"])
        self.assertTrue(result["result_buttons"][0]["has_callback"])
        self.assertEqual(ad_p99.inspect_card({})["result_buttons"], [])

    def test_url_boundaries(self):
        for url in ("http://example.test/x", "file:///tmp/x", "https://u:p@example.test/x",
                    "https://example.test:bad/x"):
            with self.subTest(url=url), self.assertRaises(ad_p99.InputError):
                ad_p99.checked_url(url)

    def test_card_exit_code_and_output_refuses_overwrite(self):
        with patch("ad_p99.read_local", return_value=b"{}"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ad_p99.main(["--card-json", "synthetic.json"]), 2)
        with patch("ad_p99.read_local") as read, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ad_p99.main(["--card-json", __file__, "--output", __file__]), 1)
            read.assert_not_called()

    def test_file_inputs_removed_from_cloud_entry(self):
        for flag in ("--excel", "--result-url"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                ad_p99.main([flag, "not-a-real-input"])
        self.assertFalse(hasattr(ad_p99, "download_result"))


class ComparisonTests(unittest.TestCase):
    def test_copied_request_respects_explicit_roles_and_defaults_to_input_order(self):
        request = """刷包后 VIN：DEM0VEH1CLE000001，20260402150000/20260402160000
        Baseline VIN: dem0veh1cle000001，2026-04-01 12:00:00 - 2026-04-01 13:00:00"""
        result = ad_p99.parse_comparison_request(request)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["vin"], "DEM0VEH1CLE000001")
        self.assertEqual(result["baseline_metadata"]["start"], "2026-04-01T12:00:00")
        self.assertEqual(result["replacement_metadata"]["start"], "2026-04-02T15:00:00")
        self.assertEqual(result["role_assignment_source"], "explicit")

        reversed_roles = ad_p99.parse_comparison_request(
            "VIN：DEM0VEH1CLE000001；base：20260402150000/20260402160000；"
            "替换后：20260401120000/20260401130000"
        )
        self.assertEqual(reversed_roles["baseline_metadata"]["start"], "2026-04-02T15:00:00")
        self.assertEqual(reversed_roles["replacement_metadata"]["start"], "2026-04-01T12:00:00")
        self.assertEqual(reversed_roles["input_roles"], ["baseline", "replacement"])

        one_vin = ad_p99.parse_comparison_request(
            "VIN：DEM0VEH1CLE000001；20260402150000/20260402160000；20260401120000/20260401130000"
        )
        self.assertEqual(one_vin["baseline_metadata"]["vin"], one_vin["replacement_metadata"]["vin"])
        self.assertEqual(one_vin["baseline_metadata"]["start"], "2026-04-02T15:00:00")
        self.assertEqual(one_vin["replacement_metadata"]["start"], "2026-04-01T12:00:00")
        self.assertEqual(one_vin["input_roles"], ["baseline", "replacement"])
        self.assertEqual(one_vin["role_assignment_source"], "input_order_default")

    def test_default_roles_and_comparison_key_follow_input_not_chronology(self):
        later = "20260402150000/20260402160000"
        earlier = "20260401120000/20260401130000"
        first_later = ad_p99.parse_comparison_request(f"VIN: DEM0VEH1CLE000001 {later}; {earlier}")
        first_earlier = ad_p99.parse_comparison_request(f"VIN: DEM0VEH1CLE000001 {earlier}; {later}")
        self.assertEqual(first_later["baseline_metadata"], first_earlier["replacement_metadata"])
        self.assertEqual(first_later["replacement_metadata"], first_earlier["baseline_metadata"])
        self.assertNotEqual(first_later["comparison_key"], first_earlier["comparison_key"])

    def test_pcm_update_label_overrides_input_order(self):
        request = ad_p99.parse_comparison_request(
            "VIN: DEM0VEH1CLE000001 PCM更新：20260402150000/20260402160000；"
            "20260401120000/20260401130000"
        )
        self.assertEqual(request["input_roles"], ["replacement", "baseline"])
        self.assertEqual(request["baseline_metadata"]["start"], "2026-04-01T12:00:00")
        self.assertEqual(request["replacement_metadata"]["start"], "2026-04-02T15:00:00")
        self.assertEqual(request["role_assignment_source"], "explicit")

    def test_copied_request_supports_ordinal_roles_and_rejects_conflicts(self):
        ordinal = ad_p99.parse_comparison_request(
            "VIN：DEM0VEH1CLE000001；20260401120000/20260401130000；"
            "20260402150000/20260402160000；第一个是替换后，第二个是 base"
        )
        self.assertEqual(ordinal["baseline_metadata"]["start"], "2026-04-02T15:00:00")
        self.assertEqual(ordinal["replacement_metadata"]["start"], "2026-04-01T12:00:00")
        trailing = ad_p99.parse_comparison_request(
            "VIN：DEM0VEH1CLE000001；20260402150000/20260402160000 为 base；"
            "20260401120000/20260401130000 为替换后"
        )
        self.assertEqual(trailing["baseline_metadata"]["start"], "2026-04-02T15:00:00")
        self.assertEqual(trailing["replacement_metadata"]["start"], "2026-04-01T12:00:00")
        latter = ad_p99.parse_comparison_request(
            "VIN：DEM0VEH1CLE000001；20260401120000/20260401130000；"
            "20260402150000/20260402160000；后一段是 base，前一段是替换后"
        )
        self.assertEqual(latter["baseline_metadata"]["start"], "2026-04-02T15:00:00")
        with self.assertRaisesRegex(ad_p99.InputError, "不能都标记"):
            ad_p99.parse_comparison_request(
                "VIN：DEM0VEH1CLE000001；base：20260401120000/20260401130000；"
                "base：20260402150000/20260402160000"
            )

    def test_copied_request_rejects_cross_vin_overlapping_or_invalid_windows(self):
        with self.assertRaisesRegex(ad_p99.InputError, "多个 VIN"):
            ad_p99.parse_comparison_request(
                "Baseline VIN: DEM0VEH1CLE000001 20260401120000/20260401130000; "
                "替换后 VIN: DEM0VEH1CLE000002 20260402120000/20260402130000"
            )
        with self.assertRaisesRegex(ad_p99.InputError, "重叠"):
            ad_p99.parse_comparison_request(
                "VIN: DEM0VEH1CLE000001 20260401120000/20260401140000; 20260401130000/20260401150000"
            )
        with self.assertRaisesRegex(ad_p99.InputError, "重叠"):
            ad_p99.parse_comparison_request(
                "VIN: DEM0VEH1CLE000001 20260401130000/20260401150000; 20260401120000/20260401140000"
            )
        with self.assertRaisesRegex(ad_p99.InputError, "起点必须早于终点"):
            ad_p99.parse_comparison_request(
                "VIN: DEM0VEH1CLE000001 20260401140000/20260401130000; 20260402120000/20260402130000"
            )

    def test_comparison_request_cli_only_normalizes_context(self):
        copied = b"VIN: DEM0VEH1CLE000001 20260402120000/20260402130000; 20260401120000/20260401130000"
        stdout = io.StringIO()
        with patch("ad_p99.read_local", return_value=copied), contextlib.redirect_stdout(stdout):
            self.assertEqual(ad_p99.main(["--comparison-request", "synthetic.txt"]), 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["kind"], "ad_p99_comparison_request")
        self.assertEqual(payload["baseline_metadata"]["start"], "2026-04-02T12:00:00")
        self.assertEqual(payload["replacement_metadata"]["start"], "2026-04-01T12:00:00")

    def test_compares_four_p99_metrics_and_exposes_table_rows(self):
        baseline = parse(sample_book())
        replacement_book = sample_book()
        replacement_book["VLA"]["D56"] = "280/-/-/-/195/178/-/-"
        replacement_book["VLA Parking"]["D76"] = "600/400/-/-/-/-/-/-"
        replacement = parse(replacement_book)
        context = {"vin": "VIN-001", "start": "2026-09-01T00:00:00+08:00", "end": "2026-09-01T01:00:00+08:00"}
        result = ad_p99.compare_results(
            baseline, replacement,
            baseline_metadata=context,
            replacement_metadata={**context, "start": "2026-09-02T00:00:00+08:00", "end": "2026-09-02T01:00:00+08:00"},
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["comparisons"]["slow_driver"]["delta_ms"], -1.16)
        self.assertEqual(result["comparisons"]["fast_driver"]["delta_ms"], 1.0)
        self.assertEqual(result["comparisons"]["CNOA"]["delta_ms"], -2.0)
        self.assertEqual(result["comparisons"]["parking"]["delta_ms"], 23.0)
        self.assertEqual(result["comparisons"]["slow_driver"]["frame_rate_hz"]["delta"], 0.0)
        self.assertEqual(result["table"]["columns"], ["场景", "指标", "Baseline", "替换小包", "Δ（替换 − Baseline）"])
        self.assertEqual(result["table"]["rows"][0][0], "slow_driver（低速人驾）")

    def test_requires_matching_vin_and_complete_time_ranges(self):
        result = parse(sample_book())
        with self.assertRaisesRegex(ad_p99.InputError, "vin 不一致"):
            ad_p99.compare_results(
                result, result,
                baseline_metadata={"vin": "VIN-001", "start": "a", "end": "b"},
                replacement_metadata={"vin": "VIN-002", "start": "c", "end": "d"},
            )
        with self.assertRaisesRegex(ad_p99.InputError, "缺少非空 end"):
            ad_p99.compare_results(
                result, result,
                baseline_metadata={"vin": "VIN-001", "start": "a", "end": "b"},
                replacement_metadata={"vin": "VIN-001", "start": "c"},
            )


if __name__ == "__main__":
    unittest.main()
