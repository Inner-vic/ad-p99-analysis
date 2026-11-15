import http.client
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cloud_log_client
from cloud_log_client import CloudNetworkError, QueryResult, build_query_params
import performance_log_source


def query_result(environment, names):
    items = [
        {"fileName": name, "downloadURL": f"https://download.example/{name}", "collectTime": str(index)}
        for index, name in enumerate(names)
    ]
    return QueryResult(
        items=items, total=len(items), environment=environment,
        provider_environment=environment, origin=f"https://{environment}.example",
        log_class="log_fsdA_service", request_host=f"{environment}.example",
        download_hosts=["https://download.example"],
    )


class PerformanceLogSourceTests(unittest.TestCase):
    def test_query_contract_is_narrow_and_fixed(self):
        params = build_query_params(
            vin="SYNTHETICVIN000001", start="2026-09-11 10:00:00", end="2026-09-11 10:30:00",
            log_class="log_fsdA_service", username="synthetic-user",
        )
        self.assertEqual(params["logClass"], "log_fsdA_service")
        self.assertEqual(params["eeaPlatform"], "EEA3.0")
        self.assertEqual(params["remoteMode"], "false")

    def test_filter_only_selects_avm_non_netbw_archives(self):
        result = performance_log_source.filter_avm_files(query_result("prod", [
            "AVM_Service-001.zst", "netbw-AVM_Service-002.zst",
            "AVM_Service-netbw-003.zst", "Other_Service-004.zst", "AVM_Service-005.log",
        ]).items)
        self.assertEqual([item["fileName"] for item in result], ["AVM_Service-001.zst"])

    def test_auto_falls_back_only_after_successful_no_data_queries(self):
        calls = []
        def query_fn(**kwargs):
            calls.append(kwargs["environment"])
            names = [] if kwargs["environment"] == "prod" else ["AVM_Service-001.zst"]
            return query_result(kwargs["environment"], names)
        environment, _, _ = performance_log_source.resolve_pair_queries(
            vin="SYNTHETICVIN000001", baseline_start="a", baseline_end="b",
            replacement_start="c", replacement_end="d", username="user", query_fn=query_fn,
        )
        self.assertEqual(environment, "test")
        self.assertEqual(calls, ["prod", "prod", "test", "test"])

    def test_network_error_does_not_trigger_environment_fallback(self):
        calls = []
        def query_fn(**kwargs):
            calls.append(kwargs["environment"])
            raise CloudNetworkError("synthetic failure")
        with self.assertRaises(CloudNetworkError):
            performance_log_source.resolve_pair_queries(
                vin="SYNTHETICVIN000001", baseline_start="a", baseline_end="b",
                replacement_start="c", replacement_end="d", username="user", query_fn=query_fn,
            )
        self.assertEqual(calls, ["prod"])

    def test_download_extracts_cpu_then_removes_temporary_logs(self):
        roots = []
        def download_fn(url, destination, *, environment):
            roots.append(destination.parents[1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"synthetic-zstd")
            return destination.stat().st_size
        def decompress_fn(source, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                "x +0800 2026-09-11-10_00_01_000 perception_state=0 "
                "infer_expert_pc_cpu=95% sys_cpu=60%\n",
                encoding="utf-8",
            )
        result = performance_log_source.extract_window(
            query_result("prod", ["AVM_Service-001.zst"]),
            start="2026-09-11 10:00:00", end="2026-09-11 10:30:00",
            download_fn=download_fn, decompress_fn=decompress_fn,
        )
        self.assertEqual(result["metrics"]["slow_driver"]["pcm_cpu_percent"]["mean"], 5.0)
        self.assertEqual(result["metrics"]["slow_driver"]["system_cpu_percent"]["mean"], 60.0)
        self.assertEqual(result["source"]["selected_files"], 1)
        self.assertTrue(roots)
        self.assertFalse(roots[0].exists())

    def test_download_retries_incomplete_read_and_replaces_only_complete_file(self):
        class Response:
            def __init__(self, *, fail=False):
                self.fail = fail
                self.headers = {"Content-Length": "8"}
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return "https://download.example/AVM_Service-001.zst"

            def read(self, _size):
                if self.fail:
                    raise http.client.IncompleteRead(b"partial", 1)
                self.reads += 1
                return b"complete" if self.reads == 1 else b""

        class Opener:
            def __init__(self):
                self.calls = 0

            def open(self, _request, timeout):
                self.calls += 1
                self.timeout = timeout
                return Response(fail=self.calls == 1)

        opener = Opener()
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(cloud_log_client, "validate_download_url", return_value=("https", "download.example")), \
                mock.patch.object(cloud_log_client.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(cloud_log_client.time, "sleep") as sleep:
            destination = Path(root) / "AVM_Service-001.zst"
            size = cloud_log_client.download_to_file(
                "https://download.example/AVM_Service-001.zst",
                destination,
                environment="prod",
            )
            self.assertEqual(size, 8)
            self.assertEqual(destination.read_bytes(), b"complete")
            self.assertFalse(destination.with_name(destination.name + ".part").exists())
        self.assertEqual(opener.calls, 2)
        sleep.assert_called_once_with(cloud_log_client.DOWNLOAD_RETRY_DELAY_SECONDS)


if __name__ == "__main__":
    unittest.main()
