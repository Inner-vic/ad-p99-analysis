import contextlib
import csv
import io
import json
import subprocess
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from test_ad_p99 import SOURCE_HEADERS, sample_book
import ad_p99
import lark_source


class FakeReader:
    """Local CLI-contract simulation only, with no access to real Feishu data."""
    def __init__(self):
        self.book = sample_book()
        self.calls = []
        self.revisions = 0
        self.change_revision = False
        self.truncate_above = 1000
        self.drop_last_row = False
        self.grid_rows = 80

    def call(self, command, *flags):
        self.calls.append((command, flags))
        if command == "+revision-get":
            self.revisions += 1
            return {"revision": 11 if self.change_revision and self.revisions == 2 else 10}
        if command == "+workbook-info":
            return {"sheets": [{"sheet_id": name, "sheet_name": name, "row_count": self.grid_rows,
                                "column_count": 5} for name in self.book.sheetnames]}
        sheet = self.book[flags[flags.index("--sheet-id") + 1]]
        if command == "+sheet-info":
            return {"merged_cells": [str(merged) for merged in sheet.merged_cells.ranges]}
        if command == "+csv-get":
            from openpyxl.utils.cell import range_boundaries
            self.assert_flag(flags, "--include-row-prefix=false")
            self.assert_flag(flags, "--skip-hidden=false")
            requested = flags[flags.index("--range") + 1]
            _, first, _, last = range_boundaries(requested)
            if last - first + 1 > self.truncate_above:
                return {"has_more": True, "annotated_csv": "bad partial data"}
            out = io.StringIO(newline="")
            writer = csv.writer(out)
            for row in range(first, last + 1):
                writer.writerow([sheet.cell(row, col).value for col in range(1, 6)])
            return {"annotated_csv": out.getvalue(), "row_indices": list(range(first, last if self.drop_last_row else last + 1)),
                    "col_indices": list("ABCDE"), "actual_range": requested, "has_more": False}
        raise AssertionError(f"Unexpected command: {command}")

    @staticmethod
    def assert_flag(flags, expected):
        if expected not in flags:
            raise AssertionError(expected)


URL = "https://example.feishu.cn/sheets/sample?secret=not-persisted"


class OnlineTests(unittest.TestCase):
    def test_online_reads_without_download_or_export(self):
        reader = FakeReader()
        with patch("ad_p99.load_excel", side_effect=AssertionError("no Excel input")), \
                patch("openpyxl.workbook.workbook.Workbook.save", side_effect=AssertionError("no local workbook")):
            result = lark_source.read_online(URL, reader=reader)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([item["p99_ms"] for item in result["metrics"].values()], [281.16, 194, 180, 377])
        self.assertEqual(result["source"]["kind"], "lark_sheet")
        self.assertEqual(result["source"]["revision"], 10)
        self.assertNotIn("not-persisted", json.dumps(result))
        self.assertEqual(result["metrics"]["parking"]["evidence"]["sheet_id"], "VLA Parking")
        self.assertEqual(result["metrics"]["parking"]["frame_rate_hz"], 8.03)
        self.assertEqual(result["metrics"]["parking"]["frame_rate_evidence"]["sheet_id"], "VLA Parking")
        self.assertNotIn("raw_p99", json.dumps(result))
        self.assertNotIn("609.6", json.dumps(result))
        self.assertTrue(all(command in lark_source.READ_COMMANDS for command, _ in reader.calls))

    def test_online_header_threshold_metadata_keeps_p99_values_and_evidence(self):
        reader = FakeReader()
        reader.book["VLA"]["B52"] = reader.book["VLA"]["B52"].value + "\nmax_threshold=1000"
        reader.book["VLA"]["D56"] = "251.0/-/-/-/301.36/-/-/-"
        result = lark_source.read_online(URL, reader=reader)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["metrics"]["slow_driver"]["p99_ms"], 251.0)
        self.assertEqual(result["metrics"]["fast_driver"]["p99_ms"], 301.36)
        self.assertIsNone(result["metrics"]["CNOA"]["p99_ms"])
        self.assertEqual(result["metrics"]["parking"]["p99_ms"], 377.0)
        self.assertEqual(result["metrics"]["slow_driver"]["evidence"], {
            "sheet": "VLA", "header_cell": "B52", "statistic_cell": "C56",
            "value_cell": "D56", "scenario_index": 0, "sheet_id": "VLA",
        })
        self.assertEqual(result["frame_rate_status"], "ok")
        self.assertEqual(result["metrics"]["slow_driver"]["frame_rate_hz"], 4.99)
        self.assertEqual(result["metrics"]["fast_driver"]["frame_rate_hz"], 8.25)
        self.assertNotIn("max_threshold", json.dumps(result))

    def test_online_all_metric_headers_accept_future_metadata(self):
        reader = FakeReader()
        for sheet, cell, metric, unit, _, _ in SOURCE_HEADERS:
            reader.book[sheet][cell] = (
                f"项目：{metric.upper()} （{unit.upper()}）\n"
                "meta(foo/bar)\n"
                + reader.book[sheet][cell].value.split("\n", 1)[1]
                + "\nunknown=1000\nmax_threshold=invalid\nnotes(build)"
            )
        result = lark_source.read_online(URL, reader=reader)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["frame_rate_status"], "ok")
        self.assertEqual([item["p99_ms"] for item in result["metrics"].values()],
                         [281.16, 194.0, 180.0, 377.0])
        self.assertEqual([item["frame_rate_hz"] for item in result["metrics"].values()],
                         [4.99, 8.25, 8.58, 8.03])
        self.assertNotIn("max_threshold", json.dumps(result))
        self.assertNotIn("unknown=1000", json.dumps(result))

    def test_truncated_pages_are_split_and_all_rows_read(self):
        reader = FakeReader()
        reader.grid_rows = 300
        reader.truncate_above = 35
        result = lark_source.read_online(URL, reader=reader)
        self.assertEqual(result["status"], "ok")
        requests = [flags[flags.index("--range") + 1] for command, flags in reader.calls if command == "+csv-get"]
        self.assertTrue(any(request.endswith("300") for request in requests))
        self.assertGreater(len(requests), 4)

    def test_duplicate_header_at_tail_is_not_missed(self):
        reader = FakeReader()
        reader.grid_rows = 300
        reader.book["VLA"]["B290"] = reader.book["VLA"]["B52"].value
        result = lark_source.read_online(URL, reader=reader)
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["metrics"]["slow_driver"]["p99_ms"])

    def test_revision_change_rejects_mixed_results(self):
        reader = FakeReader()
        reader.change_revision = True
        with self.assertRaisesRegex(ad_p99.InputError, "版本发生变化"):
            lark_source.read_online(URL, reader=reader)

    def test_incomplete_coordinates_and_single_row_truncation_fail(self):
        reader = FakeReader()
        reader.drop_last_row = True
        with self.assertRaisesRegex(ad_p99.InputError, "行列与请求范围不一致"):
            lark_source.read_online(URL, reader=reader)
        reader = FakeReader()
        reader.truncate_above = 0
        with self.assertRaisesRegex(ad_p99.InputError, "单行读取仍被截断"):
            lark_source.read_online(URL, reader=reader)

    def test_missing_sheet_returns_partial(self):
        reader = FakeReader()
        del reader.book["VLA Parking"]
        result = lark_source.read_online(URL, reader=reader)
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["metrics"]["parking"]["p99_ms"])

    def test_cli_identity_and_read_only_subprocess(self):
        with patch("lark_source.cli_executable", return_value="lark-cli.exe"):
            reader = lark_source.LarkReader(URL, identity="user")
        with patch("lark_source.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"ok": True, "data": {"revision": 10}}), "")) as run:
            self.assertEqual(reader.call("+revision-get"), {"revision": 10})
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--as") + 1], "user")
            self.assertFalse(run.call_args.kwargs.get("shell", False))
            with self.assertRaises(ad_p99.InputError):
                reader.call("+cells-set")
            self.assertEqual(run.call_count, 1)

    def test_configuration_failure_does_not_auto_authenticate(self):
        with patch("lark_source.cli_executable", return_value="lark-cli.exe"):
            reader = lark_source.LarkReader(URL)
        failure = {"ok": False, "error": {"type": "config", "subtype": "not_configured", "hint": "secret"}}
        with patch("lark_source.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 1, "", json.dumps(failure))) as run:
            with self.assertRaisesRegex(ad_p99.InputError, "尚未配置"):
                reader.call("+revision-get")
            self.assertEqual(run.call_count, 1)

    def test_project_sheet_reader_locks_and_pins_profile_home_and_identity(self):
        settings = {"lark_cli": "lark-cli.exe", "lark_home": str(Path.cwd() / "fake-project-home"),
                    "lark_profile": "project-user", "report_lark_profile": "project-bot"}
        for identity, profile in (("user", "project-user"), ("bot", "project-bot")):
            with patch("lark_source.cli_executable", return_value="lark-cli.exe"), \
                    patch.dict(os.environ, {"LARK_CLI_PROFILE": "wrong-global-profile"}, clear=False):
                reader = lark_source.LarkReader(URL, identity=identity, auth_settings=settings)
            with patch("lark_source.lark_auth_lock") as lock, \
                    patch("lark_source.subprocess.run", return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps({"ok": True, "data": {"revision": 10}}), "")) as run:
                reader.call("+revision-get")
                lock.assert_called_once_with(settings, timeout=60)
                argv = run.call_args.args[0]
                self.assertEqual(argv[argv.index("--as") + 1], identity)
                self.assertEqual(argv[argv.index("--profile") + 1], profile)
                self.assertEqual(run.call_args.kwargs["env"]["HOME"], settings["lark_home"])
                self.assertEqual(run.call_args.kwargs["env"]["USERPROFILE"], settings["lark_home"])

    def test_project_sheet_reader_rejects_cli_drift_and_missing_profile(self):
        settings = {"lark_cli": "other.exe", "lark_home": str(Path.cwd() / "fake-project-home")}
        with patch("lark_source.cli_executable", return_value="lark-cli.exe"):
            with self.assertRaisesRegex(ad_p99.InputError, "CLI"):
                lark_source.LarkReader(URL, identity="user", auth_settings=settings)
            settings["lark_cli"] = "lark-cli.exe"
            reader = lark_source.LarkReader(URL, identity="user", auth_settings=settings)
        with patch("lark_source.subprocess.run") as run:
            with self.assertRaisesRegex(ad_p99.InputError, "profile"):
                reader.call("+revision-get")
            run.assert_not_called()

    def test_wiki_and_docx_routing(self):
        with patch("lark_source.cli_executable", return_value="lark-cli.exe"):
            lark_source.LarkReader("https://example.feishu.cn/wiki/sample")
            with self.assertRaisesRegex(ad_p99.InputError, "Docx"):
                lark_source.LarkReader("https://example.feishu.cn/docx/sample")

    def test_main_online_entry(self):
        with patch("lark_source.LarkReader", return_value=FakeReader()), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ad_p99.main(["--sheet-url", URL, "--identity", "bot"]), 0)
        self.assertEqual(json.loads(out.getvalue())["source"]["kind"], "lark_sheet")

    def test_online_requires_explicit_identity_before_access(self):
        with patch("lark_source.LarkReader") as reader, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                ad_p99.main(["--sheet-url", URL])
            reader.assert_not_called()

    def test_online_cli_incomplete_statuses(self):
        for remove_names, expected in ((["VLA Parking"], 2), (["VLA Parking", "VLA"], 1)):
            reader = FakeReader()
            for name in remove_names:
                del reader.book[name]
            with patch("lark_source.LarkReader", return_value=reader), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ad_p99.main(["--sheet-url", URL, "--identity", "bot"]), expected)


if __name__ == "__main__":
    unittest.main()
