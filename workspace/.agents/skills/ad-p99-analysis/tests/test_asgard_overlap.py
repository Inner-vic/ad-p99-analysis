"""Small, offline regression checks for performance work during Asgard wait."""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import asgard_worker
import half_auto
from ad_p99 import InputError
from performance_comparison import compare_performance


VIN = "DEM0VEH1CLEX00001"
REQUEST = f"VIN：{VIN}；20260401120000/20260401130000；20260402120000/20260402130000"
BASELINE_URL = "https://example.feishu.cn/sheets/Fixture0000000000001?sheet=VLA"
REPLACEMENT_URL = "https://example.feishu.cn/sheets/Fixture0000000000000002?sheet=VLA"
ROLES = ("baseline", "replacement")
SCENARIOS = ("slow_driver", "fast_driver", "parking", "CNOA")


def sheet_result(url):
    before = url == BASELINE_URL
    return {
        "source": {"url": url, "revision": "offline-r1"},
        "metrics": {
            scenario: {
                "status": "ok", "p99_ms": 10 if before else 8,
                "frame_rate_status": "ok", "frame_rate_hz": 20 if before else 21,
            }
            for scenario in SCENARIOS
        },
    }


def pair_states():
    states = []
    for before in (True, False):
        states.append({
            "status": "ok",
            "metrics": {
                scenario: {
                    "system_cpu_percent": {"count": 3, "mean": 60 if before else 55},
                    "pcm_p99_infer_delay_ms": {"count": 3, "p99": 6 if before else 5},
                    "pcm_frame_rate_hz": {"count": 3, "mean": 19 if before else 20},
                    "pcm_cpu_percent": {"count": 3, "mean": 12 if before else 10},
                }
                for scenario in SCENARIOS
            },
            "coverage": {"fps_samples_unattributed": 0},
        })
    return tuple(states)


class AsgardOverlapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = {
            "state_dir": str(self.root / "state"),
            'asgard_open_id':'ou_offline_asgard',
            "identity": "user",
            "lark_cli": "offline-cli-do-not-execute",
            "cpu_log_cloud_env": "prod",
            "ad_p99_settings_path": str(self.root / "offline-performance-settings.json"),
            "lark_profile": "cc-connect-chassis-user",
            "report_lark_profile": "cc-connect-chassis-report-bot",
        }
        public = half_auto.create_job(
            REQUEST, state_dir=self.settings["state_dir"], asgard_platform="CCUFSD"
        )
        self.key = public["comparison_key"]
        self.job_path, self.job = half_auto.load_job(self.settings["state_dir"], self.key)
        self.job["dispatch"] = {
            "chat_id": "oc_offline_source",
            "sender_id": "ou_offline_requester",
            "asgard_id": "ou_offline_asgard",
            "sent": [
                {"role": "vehicle_version", "message_id": "om_offlineversion"},
                {"role": "baseline", "message_id": "om_offlinebaseline"},
                {"role": "replacement", "message_id": "om_offlinereplacement"},
            ],
        }
        half_auto.write_json(self.job_path, self.job)
        self.record = {
            "comparison_key": self.key,
            "vin": VIN,
            "source_chat_id": "oc_offline_source",
            "source_message_id": "om_offline_source",
            "sender_id": "ou_offline_requester",
            "created_at": "2026-04-03T01:00:00+00:00",
        }
        self.record_path = self.root / "offline-runtime.json"
        half_auto.write_json(self.record_path, self.record)
        self.received = {
            "baseline": {"sheet_url": BASELINE_URL},
            "replacement": {"sheet_url": REPLACEMENT_URL},
        }
        self.artifact = {"label": "MCB123", "source_message_id": "om_offline_version_reply"}
        self.states = pair_states()

    def patches(self, *, prefetch=None, wait=None, reader=None, validator=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.prefetch = stack.enter_context(patch(
            "asgard_worker.load_pair_states", create=True,
            side_effect=prefetch, return_value=deepcopy(self.states),
        ))
        self.serial = stack.enter_context(patch(
            "performance_comparison.load_pair_states", return_value=deepcopy(self.states),
        ))

        def validate_snapshot(*args, **kwargs):
            self.assertEqual(self.reader.call_count, 2, "Snapshot check ran before both result tables were read")
            if isinstance(validator, Exception):
                raise validator
            return True if validator is None else validator

        self.validate = stack.enter_context(patch(
            "asgard_worker.validate_pair_snapshot", create=True, side_effect=validate_snapshot,
        ))
        self.wait = stack.enter_context(patch(
            "asgard_worker.wait_for_cards", side_effect=wait, return_value=self.received,
        ))
        stack.enter_context(patch("asgard_worker.wait_for_vehicle_label", return_value=self.artifact))
        stack.enter_context(patch("lark_source.LarkReader"))
        self.reader = stack.enter_context(patch(
            "lark_source.read_online", side_effect=reader or (lambda url, **_: sheet_result(url)),
        ))
        self.dispatch = stack.enter_context(patch("asgard_worker.dispatch"))
        self.send = stack.enter_context(patch("asgard_worker.send_asgard"))
        self.publish = stack.enter_context(patch("asgard_worker.create_report"))
        self.deliver = stack.enter_context(patch("asgard_worker.deliver"))
        return stack

    def collect(self):
        return asgard_worker.collect_and_analyze_job(
            self.record_path, self.settings, self.record, self.job
        )

    def assert_no_external_writes(self):
        self.dispatch.assert_not_called()
        self.send.assert_not_called()
        self.publish.assert_not_called()
        self.deliver.assert_not_called()

    def early_then_late(self, early):
        """An early incomplete upload becomes available only after both Sheets."""
        calls = []
        main_thread = threading.get_ident()

        def loader(**kwargs):
            calls.append(deepcopy(kwargs))
            if len(calls) == 1:
                self.assertNotEqual(threading.get_ident(), main_thread)
                if isinstance(early, Exception):
                    raise early
                return deepcopy(early)
            self.assertEqual(len(calls), 2, "Preparation was retried more than once")
            self.assertEqual(threading.get_ident(), main_thread)
            self.assertEqual(self.reader.call_count, 2, "Late fallback ran before result tables were read")
            self.assertEqual(calls[0], calls[1], "Late fallback changed the bound metadata")
            return deepcopy(self.states)

        return loader

    def serial_after_sheets(self, **_):
        self.assertTrue(self.wait.called)
        self.assertEqual(self.reader.call_count, 2, "Serial performance work must remain after both Sheet reads")
        return deepcopy(self.states)

    def test_work_really_overlaps_card_wait_and_sheet_reads_do_not_join_early(self):
        started = threading.Event()
        finish = threading.Event()
        ended = threading.Event()
        main_thread = threading.get_ident()
        loader_thread = []
        read_urls = []

        def prefetch(**_):
            loader_thread.append(threading.get_ident())
            started.set()
            if not finish.wait(3):
                raise AssertionError("Sheet reads were blocked by joining prefetch too early")
            ended.set()
            return deepcopy(self.states)

        def wait(*_, **__):
            self.assertTrue(started.wait(2), "Performance prefetch did not start while waiting")
            self.assertFalse(ended.is_set(), "Wait did not overlap active performance work")
            return self.received

        def reader(url, **_):
            read_urls.append(url)
            self.assertTrue(started.is_set())
            self.assertFalse(ended.is_set(), "Performance was joined before reading Sheets")
            if len(read_urls) == 2:
                finish.set()
            return sheet_result(url)

        self.patches(prefetch=prefetch, wait=wait, reader=reader)
        try:
            result, refreshed = self.collect()
        finally:
            finish.set()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(refreshed["comparison_key"], self.key)
        self.assertEqual(read_urls, [BASELINE_URL, REPLACEMENT_URL])
        self.assertNotEqual(loader_thread, [main_thread])
        self.prefetch.assert_called_once()
        self.serial.assert_not_called()
        self.assertTrue(ended.is_set())
        self.assert_no_external_writes()

    def test_reuse_once_with_exact_bound_context_and_unchanged_report_metrics(self):
        self.patches()
        actual, _ = self.collect()
        self.prefetch.assert_called_once_with(
            baseline_metadata=self.job["baseline_metadata"],
            replacement_metadata=self.job["replacement_metadata"],
            cloud_env="prod", settings_path=self.settings["ad_p99_settings_path"], skip_cpu=False,
        )
        self.validate.assert_called_once_with(self.states[0], self.states[1], **self.prefetch.call_args.kwargs)
        self.serial.assert_not_called()
        self.assertEqual(self.reader.call_count, 2)
        for call, role in zip(self.reader.call_args_list, ROLES):
            self.assertEqual(call.kwargs["metadata"], self.job[f"{role}_metadata"])
            self.assertEqual(call.kwargs["identity"], "user")
        with tempfile.TemporaryDirectory() as directory:
            baseline = half_auto.create_job(REQUEST, state_dir=directory, asgard_platform="CCUFSD")
            key = baseline["comparison_key"]
            half_auto.attach_result(directory, key, role="baseline", sheet_url=BASELINE_URL)
            half_auto.attach_result(directory, key, role="replacement", sheet_url=REPLACEMENT_URL)
            expected = half_auto.run_job(
                directory, key, identity="user", executable=self.settings["lark_cli"],
                read_fn=lambda url, **_: sheet_result(url), cloud_env="prod",
                settings_path=self.settings["ad_p99_settings_path"], vehicle_label=self.artifact["label"],
            )
        self.assertEqual(actual, expected)
        self.assertEqual(half_auto.load_job(self.settings["state_dir"], self.key)[1]["status"], "completed")
        self.assert_no_external_writes()

    def test_missing_dispatch_evidence_keeps_original_serial_order(self):
        self.job["dispatch"]["sent"] = self.job["dispatch"]["sent"][:2]
        half_auto.write_json(self.job_path, self.job)

        def wait(*_, **__):
            self.prefetch.assert_not_called()
            self.serial.assert_not_called()
            return self.received

        self.patches(wait=wait)
        self.serial.side_effect = self.serial_after_sheets
        result, _ = self.collect()
        self.assertEqual(result["status"], "ok")
        self.prefetch.assert_not_called()
        self.serial.assert_called_once()
        self.assertEqual(self.reader.call_count, 2)
        self.assert_no_external_writes()

    def test_same_message_id_for_two_roles_does_not_enable_prefetch(self):
        self.job["dispatch"]["sent"][-1]["message_id"] = "om_offlinebaseline"
        half_auto.write_json(self.job_path, self.job)
        self.patches()
        self.serial.side_effect = self.serial_after_sheets
        result, _ = self.collect()
        self.assertEqual(result["status"], "ok")
        self.prefetch.assert_not_called()
        self.serial.assert_called_once()
        self.assert_no_external_writes()

    def test_switch_off_restores_serial_path_without_changing_result(self):
        self.settings["performance_prefetch_enabled"] = False
        self.patches()
        self.serial.side_effect = self.serial_after_sheets
        result, _ = self.collect()
        self.assertEqual(result["status"], "ok")
        self.prefetch.assert_not_called()
        self.serial.assert_called_once()
        self.assert_no_external_writes()

    def test_duplicate_role_dispatch_ids_restore_serial_order(self):
        self.job["dispatch"]["sent"].append({"role": "baseline", "message_id": "om_offlineambiguousbaseline"})
        half_auto.write_json(self.job_path, self.job)
        self.patches()
        self.serial.side_effect = self.serial_after_sheets
        result, _ = self.collect()
        self.assertEqual(result["status"], "ok")
        self.prefetch.assert_not_called()
        self.serial.assert_called_once()
        self.assert_no_external_writes()

    def test_empty_or_malformed_dispatch_id_does_not_enable_prefetch(self):
        original = deepcopy(self.job)
        for invalid in (None, "", "  ", "local-placeholder-not-a-message-id"):
            with self.subTest(message_id=invalid):
                self.job = deepcopy(original)
                self.job["dispatch"]["sent"][-1]["message_id"] = invalid
                half_auto.write_json(self.job_path, self.job)
                with self.patches():
                    self.serial.side_effect = self.serial_after_sheets
                    result, _ = self.collect()
                    self.assertEqual(result["status"], "ok")
                    self.prefetch.assert_not_called()
                    self.serial.assert_called_once()
                    self.assert_no_external_writes()

    def test_caller_cannot_supply_another_tasks_record_binding(self):
        original = deepcopy(self.record)
        for changed in ({"comparison_key": "0123456789abcdef"}, {"vin": "DEM0VEH1CLEX00002"}):
            with self.subTest(binding=changed):
                self.record = dict(original, **changed)
                with self.patches():
                    with self.assertRaises(InputError):
                        self.collect()
                    self.prefetch.assert_not_called()
                    self.wait.assert_not_called()
                    self.reader.assert_not_called()
                    self.serial.assert_not_called()
                    self.assert_no_external_writes()

    def test_source_scope_mismatch_cannot_collect_or_prefetch(self):
        for field,value in [('chat_id','oc_another'),('sender_id','ou_another'),('asgard_id','ou_other_bot')]:
            with self.subTest(field=field):
                original=self.job['dispatch'][field]
                self.job['dispatch'][field]=value
                with self.patches():
                    with self.assertRaisesRegex(InputError,'来源会话'):
                        self.collect()
                    self.prefetch.assert_not_called()
                    self.wait.assert_not_called()
                self.job['dispatch'][field]=original

    def test_changed_metadata_cannot_reuse_snapshot_or_publish_a_wrong_report(self):
        def wait(*_, **__):
            path, persisted = half_auto.load_job(self.settings["state_dir"], self.key)
            persisted["replacement_metadata"]["end"] = "2026-04-02T13:00:01"
            half_auto.write_json(path, persisted)
            return self.received

        self.patches(wait=wait)
        with self.assertRaises(InputError):
            self.collect()
        self.serial.assert_not_called()
        self.assert_no_external_writes()
        self.assertNotEqual(half_auto.load_job(self.settings["state_dir"], self.key)[1]["status"], "completed")

    def test_prefetch_loader_gets_copy_not_live_job_metadata(self):
        seen = []

        def prefetch(**kwargs):
            seen.append(kwargs)
            kwargs["baseline_metadata"]["vin"] = "NOT-THE-BOUND-VIN"
            return deepcopy(self.states)

        self.patches(prefetch=prefetch)
        # Mutating a loader's private input must never mutate the bound task.
        # A strict implementation may reject the changed snapshot or reuse the
        # result after comparing a separate immutable original snapshot.
        try:
            self.collect()
        except InputError:
            pass
        self.assertEqual(self.job["baseline_metadata"]["vin"], VIN)
        self.assertEqual(half_auto.load_job(self.settings["state_dir"], self.key)[1]["baseline_metadata"]["vin"], VIN)
        self.assertEqual(len(seen), 1)
        self.assert_no_external_writes()

    def test_incomplete_prefetch_retries_after_cards_and_keeps_full_metrics(self):
        for status in ("failed", "partial"):
            with self.subTest(status=status):
                # Reset result attachment between independent runs of the job.
                job = deepcopy(self.job)
                job["received"] = {"baseline": None, "replacement": None}
                job["status"] = "waiting_cards"
                half_auto.write_json(self.job_path, job)
                self.job = job
                incomplete = deepcopy(self.states)
                incomplete[0]["status"] = status
                with self.patches(prefetch=self.early_then_late(incomplete)):
                    result, _ = self.collect()
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(self.prefetch.call_count, 2)
                    self.serial.assert_not_called()
                    self.assertEqual(self.reader.call_count, 2)
                    self.assert_no_external_writes()

    def test_empty_metrics_prefetch_retries_instead_of_rendering_missing_metrics(self):
        incomplete = deepcopy(self.states)
        incomplete[0]["metrics"] = {}
        self.patches(prefetch=self.early_then_late(incomplete))
        result, _ = self.collect()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.prefetch.call_count, 2)
        self.serial.assert_not_called()
        self.assert_no_external_writes()

    def test_partial_from_real_log_source_reuses_covered_scenarios_without_recalculation(self):
        # "partial" also means that the task did not drive every scenario.
        # Real files were read successfully: this must not force downloading
        # and parsing the same logs twice just because one mode was absent.
        partial = deepcopy(self.states)
        for state in partial:
            state["status"] = "partial"
            state["coverage"]["files_read"] = 1
            state["source"] = {"kind": "log_fsdA_service", "selected_files": 1}
            state["metrics"].pop("parking")
        self.patches(prefetch=lambda **_: partial)
        actual, _ = self.collect()
        expected = compare_performance(
            sheet_result(BASELINE_URL), sheet_result(REPLACEMENT_URL),
            baseline_metadata=self.job["baseline_metadata"],
            replacement_metadata=self.job["replacement_metadata"],
            baseline_state=partial[0], replacement_state=partial[1],
            baseline_sheet_url=BASELINE_URL, replacement_sheet_url=REPLACEMENT_URL,
            vehicle_label=self.artifact["label"],
        )
        self.assertEqual(actual, expected)
        self.assertEqual(actual["status"], "partial")
        self.assertEqual(actual["coverage"]["baseline"]["performance_status"], "partial")
        self.prefetch.assert_called_once()
        self.serial.assert_not_called()
        self.assert_no_external_writes()

    def test_prefetch_exception_falls_back_without_resending_or_publishing(self):
        self.patches(prefetch=self.early_then_late(RuntimeError("offline simulated early log upload failure")))
        result, _ = self.collect()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.prefetch.call_count, 2)
        self.serial.assert_not_called()
        self.assert_no_external_writes()

    def test_changed_or_uncheckable_snapshot_falls_back_after_results_without_reuse(self):
        original = deepcopy(self.job)
        for validation in (False, RuntimeError("offline simulated manifest validation failure")):
            with self.subTest(validation=validation):
                self.job = deepcopy(original)
                half_auto.write_json(self.job_path, self.job)
                with self.patches(prefetch=self.early_then_late(self.states), validator=validation):
                    result, _ = self.collect()
                    self.assertEqual(result["status"], "ok")
                    self.validate.assert_called_once_with(
                        self.states[0], self.states[1], **self.prefetch.call_args_list[0].kwargs,
                    )
                    self.assertEqual(self.prefetch.call_count, 2)
                    self.serial.assert_not_called()
                    self.assert_no_external_writes()

    def test_wait_failure_drains_active_worker_without_reading_or_publishing(self):
        started = threading.Event()
        release = threading.Event()
        ended = threading.Event()

        def prefetch(**_):
            started.set()
            if not release.wait(3):
                raise AssertionError("Background performance worker did not receive test release")
            ended.set()
            return deepcopy(self.states)

        def wait(*_, **__):
            self.assertTrue(started.wait(2))
            release.set()
            raise InputError("offline simulated Asgard result timeout")

        self.patches(prefetch=prefetch, wait=wait)
        with patch.dict(asgard_worker.os.environ), \
                patch("asgard_worker.ensure_report_bot_authorization", return_value={"profile": "offline-bot"}):
            with self.assertRaisesRegex(InputError, "Asgard result timeout"):
                asgard_worker.finish_dispatched_job(self.record_path, self.settings, self.record)
        self.assertTrue(ended.is_set(), "Failed collection left an active background worker")
        self.reader.assert_not_called()
        self.serial.assert_not_called()
        self.assert_no_external_writes()
        self.assertNotEqual(half_auto.load_job(self.settings["state_dir"], self.key)[1]["status"], "completed")

    def test_state_write_failure_does_not_hide_original_wait_failure(self):
        self.patches(wait=InputError("offline original card timeout"))
        with patch("asgard_worker.atomic_json", side_effect=OSError("offline simulated failed state write")):
            with self.assertRaisesRegex(InputError, "original card timeout"):
                self.collect()
        self.reader.assert_not_called()
        self.assert_no_external_writes()

    def test_finish_still_creates_and_delivers_one_report_without_upstream_dispatch(self):
        self.patches()
        self.deliver.return_value = {'message_id':'om_offline_delivered','identity':'bot',
            'source_message_id':self.record['source_message_id'],
            'source_chat_id':self.record['source_chat_id'],
            'report_url':'https://li.feishu.cn/docx/OfflineReportToken', 'profile':'offline-bot'}
        self.publish.return_value = "https://li.feishu.cn/docx/OfflineReportToken"
        with ExitStack() as stack:
            stack.enter_context(patch.dict(asgard_worker.os.environ))
            stack.enter_context(patch("asgard_worker.ensure_report_bot_authorization", return_value={"profile": "offline-bot"}))
            management = stack.enter_context(patch("asgard_worker.restrict_collaborator_management"))
            chat_view = stack.enter_context(patch("asgard_worker.grant_source_chat_view"))
            owner = stack.enter_context(patch("asgard_worker.transfer_report_owner"))
            secure_label = stack.enter_context(patch("asgard_worker.configure_report_secure_label"))
            result = asgard_worker.finish_dispatched_job(self.record_path, self.settings, self.record)
        self.assertEqual(result, 0)
        self.dispatch.assert_not_called()
        self.send.assert_not_called()
        self.prefetch.assert_called_once()
        self.serial.assert_not_called()
        self.publish.assert_called_once()
        self.deliver.assert_called_once()
        management.assert_called_once()
        chat_view.assert_called_once()
        owner.assert_called_once()
        secure_label.assert_called_once()
        self.assertEqual(self.record["status"], "completed")

    def test_run_job_skip_cpu_never_consumes_custom_state_loader(self):
        for role, url in zip(ROLES, (BASELINE_URL, REPLACEMENT_URL)):
            half_auto.attach_result(self.settings["state_dir"], self.key, role=role, sheet_url=url)
        with patch("performance_comparison.load_pair_states", return_value=(None, None)) as default_loader:
            loader = Mock(side_effect=AssertionError("skip_cpu consumed a loader"))
            result = half_auto.run_job(
                self.settings["state_dir"], self.key, identity="user",
                read_fn=lambda url, **_: sheet_result(url), skip_cpu=True, state_loader=loader,
            )
        loader.assert_not_called()
        self.assertEqual(result["status"], "partial")
        # Preserve the historical loader call where no custom loader is used;
        # this test only requires that the custom prefetch is not consumed.
        if default_loader.called:
            self.assertTrue(default_loader.call_args.kwargs["skip_cpu"])


if __name__ == "__main__":
    unittest.main()
