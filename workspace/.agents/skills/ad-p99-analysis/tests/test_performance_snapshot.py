import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cloud_log_client import CloudNetworkError, QueryResult
import performance_log_source as source


BASELINE = {"vin": "SYNTHETICVIN000001", "start": "2026-10-12T10:00:00", "end": "2026-10-12T10:01:00"}
REPLACEMENT = {"vin": BASELINE["vin"], "start": "2026-10-12T11:00:00", "end": "2026-10-12T11:01:00"}


def file_record(key="synthetic-object-1", name="AVM_Service-001.zst"):
    # These are the record fields consumed by the existing cloud client.
    return {"fileKey": key, "fileName": name, "collectTime": "2026-10-12 10:00:00",
            "downloadURL": "https://download.example/archive?token=synthetic-expiring-token"}


def query_result(items, environment="prod"):
    return QueryResult(items=items, total=len(items), environment=environment,
                       provider_environment=environment, origin="https://query.example",
                       log_class=source.LOG_CLASS, request_host="query.example",
                       download_hosts=["https://download.example"])


def prepared_state(items, environment="prod", status="partial"):
    return {"status": status, "metrics": {"slow_driver": {"system_cpu_percent": {"count": 1}}},
            "coverage": {"files_read": len(items)},
            "source": {"kind": source.LOG_CLASS, "environment": environment,
                       "selected_files": len(items),
                       "selected_files_snapshot_sha256": source._selected_files_snapshot_sha256(items)}}


class PerformanceSnapshotTests(unittest.TestCase):
    def validate(self, baseline, replacement, *, items=None, environment="prod", cloud_env="auto"):
        listing = query_result(items if items is not None else [file_record()], environment)
        with mock.patch.object(source, "load_settings", return_value={"username": "synthetic-user"}), \
                mock.patch.object(source, "query_files", return_value=listing) as query, \
                mock.patch.object(source, "download_to_file") as download, \
                mock.patch.object(source, "decompress_zst") as decompress, \
                mock.patch.object(source, "extract_window") as extract:
            result = source.validate_pair_snapshot(
                baseline, replacement, baseline_metadata=BASELINE,
                replacement_metadata=REPLACEMENT, cloud_env=cloud_env,
                settings_path=Path("synthetic-settings.json"),
            )
        download.assert_not_called()
        decompress.assert_not_called()
        extract.assert_not_called()
        return result, query

    def test_snapshot_is_order_independent_and_ignores_signed_urls(self):
        items = [file_record(), file_record("synthetic-object-2", "AVM_Service-002.zst")]
        changed = copy.deepcopy(list(reversed(items)))
        for item in changed:
            item["downloadURL"] = "https://download.example/archive?token=different-expiring-token"
            item["url"] = "https://download.example/alternate?secret=synthetic"
        self.assertEqual(source._selected_files_snapshot_sha256(items),
                         source._selected_files_snapshot_sha256(changed))

    def test_snapshot_changes_for_same_count_file_replacement_or_metadata_change(self):
        original = [file_record()]
        replacement = [file_record("different-object")]
        changed = copy.deepcopy(original)
        changed[0]["collectTime"] = "2026-10-12 10:00:01"
        self.assertNotEqual(source._selected_files_snapshot_sha256(original),
                            source._selected_files_snapshot_sha256(replacement))
        self.assertNotEqual(source._selected_files_snapshot_sha256(original),
                            source._selected_files_snapshot_sha256(changed))

    def test_snapshot_requires_known_stable_identity_and_valid_metadata(self):
        for field in ("fileKey", "fileName", "collectTime"):
            with self.subTest(field=field):
                missing = file_record()
                missing.pop(field)
                self.assertIsNone(source._selected_files_snapshot_sha256([missing]))
        self.assertIsNone(source._selected_files_snapshot_sha256([]))
        self.assertIsNone(source._selected_files_snapshot_sha256([file_record(), file_record()]))
        invalid = file_record()
        invalid["unexpected_non_json_field"] = object()
        self.assertIsNone(source._selected_files_snapshot_sha256([invalid]))

    def test_partial_pair_reuses_when_both_file_lists_are_unchanged(self):
        state = prepared_state([file_record()])
        result, query = self.validate(state, copy.deepcopy(state))
        self.assertTrue(result)
        self.assertEqual(query.call_count, 2)
        self.assertEqual(query.call_args_list[0].kwargs["start"], "2026-10-12 10:00:00")
        self.assertEqual(query.call_args_list[1].kwargs["start"], "2026-10-12 11:00:00")

    def test_changed_or_added_files_and_missing_stable_identity_do_not_reuse(self):
        state = prepared_state([file_record()])
        unidentified = file_record()
        unidentified.pop("fileKey")
        for listing in ([file_record("different-object")],
                        [file_record(), file_record("synthetic-object-2", "AVM_Service-002.zst")],
                        [unidentified], []):
            with self.subTest(listing_size=len(listing)):
                result, _ = self.validate(state, copy.deepcopy(state), items=listing)
                self.assertFalse(result)

    def test_only_avm_selected_files_affect_the_snapshot(self):
        state = prepared_state([file_record()])
        listing = [file_record(), file_record("other-object", "netbw-AVM_Service-002.zst")]
        result, _ = self.validate(state, copy.deepcopy(state), items=listing)
        self.assertTrue(result)

    def test_auto_environment_change_does_not_reuse_test_results(self):
        state = prepared_state([file_record()], environment="test")
        result, query = self.validate(state, copy.deepcopy(state), environment="prod")
        self.assertFalse(result)
        self.assertEqual(query.call_count, 2)

    def test_auto_reuses_test_after_successful_empty_prod_listings(self):
        items = [file_record()]
        state = prepared_state(items, environment="test")
        with mock.patch.object(source, "load_settings", return_value={"username": "synthetic-user"}), \
                mock.patch.object(source, "query_files", side_effect=lambda **kw: query_result(
                    [] if kw["environment"] == "prod" else items, kw["environment"])) as query:
            result = source.validate_pair_snapshot(
                state, copy.deepcopy(state), baseline_metadata=BASELINE,
                replacement_metadata=REPLACEMENT,
            )
        self.assertTrue(result)
        self.assertEqual([call.kwargs["environment"] for call in query.call_args_list],
                         ["prod", "prod", "test", "test"])

    def test_untrusted_failed_or_empty_prepared_states_do_not_query(self):
        state = prepared_state([file_record()])
        mutations = (("status", "failed"), ("error", "synthetic failure"),
                     ("metrics", {}), ("coverage", {"files_read": 0}),
                     ("coverage", {"files_read": 2}),
                     ("source", {"kind": source.LOG_CLASS, "environment": "prod", "selected_files": 1}))
        for field, value in mutations:
            with self.subTest(field=field):
                invalid = copy.deepcopy(state)
                invalid[field] = value
                result, query = self.validate(invalid, state)
                self.assertFalse(result)
                query.assert_not_called()

    def test_prepared_files_without_stable_key_do_not_query(self):
        item = file_record()
        item.pop("fileKey")
        state = prepared_state([item])
        self.assertIsNone(state["source"]["selected_files_snapshot_sha256"])
        result, query = self.validate(state, copy.deepcopy(state))
        self.assertFalse(result)
        query.assert_not_called()

    def test_query_failure_returns_false_without_environment_fallback(self):
        state = prepared_state([file_record()])
        with mock.patch.object(source, "load_settings", return_value={"username": "synthetic-user"}), \
                mock.patch.object(source, "query_files", side_effect=CloudNetworkError("synthetic failure")) as query:
            self.assertFalse(source.validate_pair_snapshot(
                state, state, baseline_metadata=BASELINE, replacement_metadata=REPLACEMENT,
            ))
        self.assertEqual(query.call_count, 1)

    def test_skip_and_invalid_binding_do_not_query(self):
        state = prepared_state([file_record()])
        for skip_cpu, replacement in ((True, REPLACEMENT),
                                      (False, dict(REPLACEMENT, vin="OTHER-SYNTHETIC-VIN")),
                                      (False, dict(REPLACEMENT, start="not-a-time"))):
            with self.subTest(skip_cpu=skip_cpu, replacement=replacement), \
                    mock.patch.object(source, "load_settings", return_value={"username": "synthetic-user"}), \
                    mock.patch.object(source, "query_files") as query:
                self.assertFalse(source.validate_pair_snapshot(
                    state, state, baseline_metadata=BASELINE,
                    replacement_metadata=replacement, skip_cpu=skip_cpu,
                ))
                query.assert_not_called()

    def test_extraction_adds_only_hash_not_file_records_or_download_urls(self):
        items = [file_record()]

        def download(_url, destination, *, environment):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"synthetic-archive")

        def decompress(_archive, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"synthetic-log")

        state = source.extract_window(
            query_result(items), start=BASELINE["start"], end=BASELINE["end"],
            download_fn=download, decompress_fn=decompress,
            extract_fn=lambda *_args: {"status": "partial", "metrics": {"synthetic": {}},
                                      "coverage": {"files_read": 1}},
        )
        self.assertEqual(state["source"]["selected_files_snapshot_sha256"],
                         source._selected_files_snapshot_sha256(items))
        self.assertEqual(set(state["source"]),
                         {"kind", "environment", "selected_files", "selected_files_snapshot_sha256"})
        self.assertNotIn("synthetic-object", str(state))
        self.assertNotIn("synthetic-expiring-token", str(state))


if __name__ == "__main__":
    unittest.main()
