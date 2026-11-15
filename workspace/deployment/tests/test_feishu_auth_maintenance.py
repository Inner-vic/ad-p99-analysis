import copy
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import feishu_auth_maintenance as maintenance


class AuthMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)
        home = self.root / "lark-home"
        (home / ".lark-cli").mkdir(parents=True)
        (home / ".lark-cli" / "config.json").write_text("{}", encoding="utf-8")
        self.settings = {
            "project_id": "cc-connect-chassis", "project_isolation_required": True,
            "lark_home": str(home), "lark_cli": sys.executable,
            "lark_profile": "cc-connect-chassis-user", "report_lark_profile": "cc-connect-chassis-report-bot",
            "expected_lark_app_id": "cli_expected", "expected_dispatch_open_id": "ou_expected",
        }
        settings_path = self.root / "settings.json"
        settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        self.manifest = {
            "active_mode": "project_home", "project_id": "cc-connect-chassis",
            "workspace_root": str(self.root), "settings_path": str(settings_path),
            "lark_home": str(home), "lark_cli": sys.executable, "expected_cli_version": "1.0.97",
            "profiles": {"dispatch_user": self.settings["lark_profile"], "report_bot": self.settings["report_lark_profile"]},
            "expected_lark_app_id": "cli_expected", "expected_dispatch_open_id": "ou_expected",
            "required_user_scopes": ["im:message", "docs:secure_label:readonly", "docs:secure_label:write_only"],
        }
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.health_path = self.root / "health.json"
        self.worker = SimpleNamespace(
            ensure_user_authorization=Mock(return_value={"open_id": "ou_expected"}),
            ensure_report_bot_authorization=Mock(return_value={"available": True}),
            ensure_secure_label_authorization=Mock(),
        )
        self.sleep = Mock()
        self.locked = False
        self.notifications = []
        self.lifecycle_events = []
        self.auth_guard = SimpleNamespace(commit=Mock(side_effect=self.commit))
        self.version_reader = Mock(return_value="1.0.97")
        self.fingerprint_patch = patch("feishu_auth_maintenance.credential_store_fingerprint", side_effect=self.fingerprint)
        self.fingerprint_mock = self.fingerprint_patch.start()
        self.addCleanup(self.fingerprint_patch.stop)

    def fingerprint(self, _settings):
        self.assertTrue(self.locked)
        return "a" * 64

    def commit(self):
        self.assertTrue(self.locked)
        self.lifecycle_events.append("commit")

    def status(self, *, access_minutes=120, refresh_hours=168, verified=None,
               app_id="cli_expected", open_id="ou_expected", include_refresh=True):
        user = {
            "available": True, "tokenStatus": "valid", "openId": open_id,
            "expiresAt": (self.now + timedelta(minutes=access_minutes)).isoformat(),
            "scope": "offline_access im:message docs:secure_label:readonly docs:secure_label:write_only",
            "verified": verified,
            "accessToken": "SECRET_ACCESS", "refreshToken": "SECRET_REFRESH",
        }
        if include_refresh:
            user["refreshExpiresAt"] = (self.now + timedelta(hours=refresh_hours)).isoformat()
        return {"appId": app_id, "identities": {"user": user}, "appSecret": "SECRET_APP"}

    @contextmanager
    def lock(self, settings, timeout=60):
        self.assertEqual(settings["lark_home"], self.settings["lark_home"])
        self.assertEqual(timeout, 60)
        self.locked = True
        try:
            yield self.auth_guard
        finally:
            self.locked = False

    def notify(self, manifest, settings, health, now):
        self.assertTrue(self.locked or health["reason"] in {"credential_replica_unavailable", "credential_lock_timeout"})
        self.lifecycle_events.append("notify")
        self.notifications.append(copy.deepcopy(health))
        return {"status": "suppressed", "reason": "test"}

    def run_maintenance(self, reader, **kwargs):
        def wrapped(settings, profile, verify=False):
            self.assertTrue(self.locked)
            self.assertEqual(profile, "cc-connect-chassis-user")
            return reader(settings, profile, verify)
        notifier = kwargs.pop("notifier", self.notify)
        return maintenance.maintain(
            self.manifest_path, self.health_path, now=self.now, status_reader=wrapped,
            sleeper=self.sleep, lock_factory=self.lock, worker_loader=lambda _: self.worker,
            version_reader=self.version_reader, notifier=notifier, **kwargs,
        )

    def assert_no_secrets(self):
        for path in self.root.glob("health*.json"):
            raw = path.read_text(encoding="utf-8")
            for secret in ("SECRET_ACCESS", "SECRET_REFRESH", "SECRET_APP", "RAW_EXCEPTION_SECRET"):
                self.assertNotIn(secret, raw)

    def test_fresh_credentials_skip_remote_verification_and_cross_check_worker(self):
        calls = []
        def reader(_settings, _profile, verify):
            calls.append(verify)
            return self.status()
        result = self.run_maintenance(reader)
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [False])
        self.assertFalse(result["verificationTriggered"])
        self.assertTrue(result["businessPreflightPassed"])
        effective_settings = dict(self.settings, auth_refresh_margin_seconds=900, auth_verify_attempts=3)
        self.worker.ensure_user_authorization.assert_called_once_with(effective_settings, "ou_expected", now=self.now)
        self.worker.ensure_report_bot_authorization.assert_called_once()
        self.worker.ensure_secure_label_authorization.assert_called_once()
        self.assertEqual(result["consecutiveFailures"], 0)
        self.assert_no_secrets()

    def test_due_credentials_are_verified_and_persisted_expiries_must_advance(self):
        calls = []
        fresh = False
        def reader(_settings, _profile, verify):
            nonlocal fresh
            calls.append(verify)
            if verify:
                fresh = True
            return self.status(access_minutes=120 if fresh else 5, verified=True if verify else None)
        result = self.run_maintenance(reader)
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [False, True, False, False])
        self.assertTrue(result["tokenRotationObserved"])
        self.assertEqual(result["verificationAttempts"], 1)

    def test_verified_response_without_persisted_renewal_cannot_claim_success(self):
        def reader(_settings, _profile, verify):
            return self.status(access_minutes=120 if verify else 5, verified=True if verify else None)
        result = self.run_maintenance(reader)
        self.assertFalse(result["ok"])
        self.assertFalse(result["tokenRotationObserved"])
        self.assertEqual(result["reason"], "verification_required")
        self.assertFalse(result["requiresReauthorization"])
        self.assertEqual(result["verificationAttempts"], 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2, 4])
        self.worker.ensure_user_authorization.assert_not_called()

    def test_refresh_warning_survives_unchanged_remote_verification(self):
        result = self.run_maintenance(lambda _s, _p, verify: self.status(refresh_hours=24, verified=True if verify else None))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["reason"], "refresh_near_expiry")
        self.assertTrue(result["businessPreflightPassed"])
        self.assertFalse(result["tokenRotationObserved"])
        self.assertFalse(result["requiresReauthorization"])
        self.assertEqual(result["verificationAttempts"], 3)

    def test_force_verify_fresh_unchanged_credentials_are_checked_not_rotated(self):
        result = self.run_maintenance(lambda _s, _p, verify: self.status(verified=True if verify else None), force_verify=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["verificationTriggered"])
        self.assertFalse(result["tokenRotationObserved"])

    def test_refresh_expiry_must_be_parseable_even_with_valid_access(self):
        result = self.run_maintenance(lambda _s, _p, verify: self.status(include_refresh=False, verified=True if verify else None))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "refresh_expiry_unknown")
        self.assertFalse(result["requiresReauthorization"])
        self.assertEqual(result["verificationAttempts"], 3)

    def test_confirmed_refresh_expiry_requires_reauthorization(self):
        result = self.run_maintenance(lambda _s, _p, verify: self.status(refresh_hours=-1, verified=True if verify else None))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "reauthorization_required")
        self.assertTrue(result["requiresReauthorization"])
        self.assertEqual(result["verificationAttempts"], 3)

    def test_expired_refresh_with_missing_remote_user_still_requires_reauthorization(self):
        def reader(_settings, _profile, verify):
            status = self.status(refresh_hours=-1, verified=False if verify else None)
            status["identities"]["user"].update(available=False, status="missing", tokenStatus="missing",
                                               message="refresh token expired")
            return status
        result = self.run_maintenance(reader)
        self.assertEqual(result["reason"], "reauthorization_required")
        self.assertTrue(result["requiresReauthorization"])
        self.assertEqual(result["verificationAttempts"], 3)

    def test_unknown_local_scopes_do_not_alone_claim_authorization_revoked(self):
        def reader(_settings, _profile, verify):
            status = self.status(verified=True if verify else None)
            status["identities"]["user"].pop("scope")
            return status
        result = self.run_maintenance(reader)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "verification_required")
        self.assertFalse(result["requiresReauthorization"])

    def test_transient_verification_retries_then_recovers(self):
        calls = []
        fresh = False
        attempts = 0
        def reader(_settings, _profile, verify):
            nonlocal fresh, attempts
            calls.append(verify)
            if verify:
                attempts += 1
                if attempts == 1:
                    raise maintenance.MaintenanceError("verification_unavailable")
                fresh = True
            return self.status(access_minutes=120 if fresh else 5, verified=True if verify else None)
        result = self.run_maintenance(reader)
        self.assertTrue(result["ok"])
        self.assertEqual(result["verificationAttempts"], 2)
        self.sleep.assert_called_once_with(2)

    def test_wrong_user_or_application_is_never_refreshed(self):
        for fields in ({"open_id": "ou_different"}, {"app_id": "cli_different"}):
            with self.subTest(fields=fields):
                calls = []
                def reader(_settings, _profile, verify):
                    calls.append(verify)
                    return self.status(**fields)
                result = self.run_maintenance(reader)
                self.assertEqual(result["reason"], "identity_mismatch")
                self.assertEqual(calls, [False])
                self.assertFalse(result["requiresReauthorization"])

    def test_settings_drift_fails_before_any_cli_call(self):
        changed = dict(self.settings, lark_cli="global-lark-cli")
        Path(self.manifest["settings_path"]).write_text(json.dumps(changed), encoding="utf-8")
        reader = Mock()
        result = self.run_maintenance(reader)
        self.assertEqual(result["reason"], "manifest_settings_mismatch")
        reader.assert_not_called()

    def test_encrypted_replica_path_must_match_and_stay_under_private_project_home(self):
        private = Path(self.settings["lark_home"]) / ".lark-cli"
        replica = private / "cc-connect-chassis-user.dpapi.json"
        cases = (
            (str(replica), str(replica), None),
            (str(replica), str(private / "different.dpapi.json"), "manifest_settings_mismatch"),
            (str(self.root / "outside.dpapi.json"), str(self.root / "outside.dpapi.json"), "credential_replica_invalid"),
            (str(private), str(private), "credential_replica_invalid"),
            ("", "", "credential_replica_invalid"),
        )
        for manifest_value, settings_value, expected_reason in cases:
            with self.subTest(manifest_value=manifest_value, settings_value=settings_value):
                self.manifest["auth_credential_replica_path"] = manifest_value
                self.settings["auth_credential_replica_path"] = settings_value
                self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
                Path(self.manifest["settings_path"]).write_text(json.dumps(self.settings), encoding="utf-8")
                reader = Mock(side_effect=lambda _s, _p, _v: self.status())
                result = self.run_maintenance(reader)
                if expected_reason is None:
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["executionContext"]["credentialStoreFingerprint"], "a" * 64)
                else:
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["reason"], expected_reason)
                    reader.assert_not_called()

    def test_execution_context_fingerprint_is_captured_only_inside_credential_lock(self):
        result = self.run_maintenance(lambda _s, _p, _v: self.status())
        self.assertTrue(result["ok"])
        self.assertEqual(result["executionContext"]["credentialStoreFingerprint"], "a" * 64)

    def test_failed_replica_commit_never_notifies_success_or_advances_latest_success(self):
        class LarkAuthStoreError(Exception):
            pass
        self.auth_guard.commit.side_effect = LarkAuthStoreError("RAW_EXCEPTION_SECRET")
        result = self.run_maintenance(lambda _s, _p, _v: self.status())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "credential_replica_unavailable")
        saved = json.loads(self.health_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["ok"])
        self.assertNotIn("latestSuccess", saved)
        self.assertFalse(any(health["ok"] for health in self.notifications))
        self.assert_no_secrets()

    def test_failed_replica_commit_preserves_previous_success_without_recovery_notification(self):
        class LarkAuthStoreError(Exception):
            pass
        healthy = self.run_maintenance(lambda _s, _p, _v: self.status())
        previous_success = copy.deepcopy(healthy["latestSuccess"])
        self.notifications.clear()
        self.auth_guard.commit.side_effect = LarkAuthStoreError("RAW_EXCEPTION_SECRET")
        result = self.run_maintenance(lambda _s, _p, _v: self.status())
        self.assertFalse(result["ok"])
        self.assertEqual(result["latestSuccess"], previous_success)
        self.assertFalse(any(health["ok"] for health in self.notifications))
        self.assert_no_secrets()

    def test_success_commit_precedes_notification_and_persistence(self):
        original = maintenance.persist_health
        def persist(path, result):
            self.lifecycle_events.append("persist")
            return original(path, result)
        with patch("feishu_auth_maintenance.persist_health", side_effect=persist):
            result = self.run_maintenance(lambda _s, _p, _v: self.status())
        self.assertTrue(result["ok"])
        self.assertEqual(self.lifecycle_events, ["commit", "notify", "persist"])

    def test_enabled_replica_rejects_lock_without_commit_api(self):
        replica = str(Path(self.settings["lark_home"]) / ".lark-cli" / "user.dpapi.json")
        self.settings["auth_credential_replica_path"] = replica
        self.manifest["auth_credential_replica_path"] = replica
        Path(self.manifest["settings_path"]).write_text(json.dumps(self.settings), encoding="utf-8")
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.auth_guard = None
        result = self.run_maintenance(lambda _s, _p, _v: self.status())
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "credential_replica_unavailable")
        self.assertFalse(any(health["ok"] for health in self.notifications))
        self.assertNotIn("latestSuccess", result)

    def test_broken_or_busy_user_store_sends_only_independent_bot_notice_after_three_failures(self):
        from lark_auth_alerts import notify_auth_health
        class LarkAuthStoreError(Exception):
            pass
        for reason in ("credential_lock_timeout", "credential_replica_unavailable"):
            with self.subTest(reason=reason):
                workspace = self.root / reason
                workspace.mkdir()
                self.health_path = workspace / "health.json"
                self.manifest["workspace_root"] = str(workspace)
                self.manifest["auth_maintenance"] = {"notifications": {
                    "enabled": True, "recipient_open_id": "ou_expected",
                    "profile": self.settings["report_lark_profile"], "reminder_hours": 24,
                }}
                self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
                @contextmanager
                def unavailable(_settings, timeout=60):
                    if reason == "credential_lock_timeout":
                        raise TimeoutError("RAW_EXCEPTION_SECRET")
                    raise LarkAuthStoreError("RAW_EXCEPTION_SECRET")
                    yield
                self.lock = unavailable
                commands = []
                def bot_runner(command, **_kwargs):
                    commands.append(command)
                    self.assertIn(self.settings["report_lark_profile"], command)
                    self.assertNotIn(self.settings["lark_profile"], command)
                    self.assertNotIn("--verify", command)
                    if command[1:3] == ["auth", "status"]:
                        payload = {"appId": "cli_expected", "identities": {
                            "bot": {"available": True}, "user": {"available": False},
                        }}
                    else:
                        self.assertEqual(command[1:3], ["im", "+messages-send"])
                        self.assertEqual(command[command.index("--as") + 1], "bot")
                        payload = {"ok": True, "identity": "bot", "data": {"message_id": "om_alert1"}}
                    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
                def notify(manifest, settings, health, now):
                    return notify_auth_health(manifest, settings, health, now=now, runner=bot_runner)
                reader = Mock()
                self.fingerprint_mock.reset_mock()
                results = [self.run_maintenance(reader, notifier=notify) for _ in range(3)]
                self.assertEqual([result["consecutiveFailures"] for result in results], [1, 2, 3])
                self.assertEqual([result["notification"]["status"] for result in results], ["suppressed", "suppressed", "sent"])
                self.assertEqual(len(commands), 2)
                self.assertEqual(results[-1]["reason"], reason)
                self.assertNotIn("credentialStoreFingerprint", results[-1]["executionContext"])
                reader.assert_not_called()
                self.fingerprint_mock.assert_not_called()

    def test_critical_failover_checks_cli_version_before_notifying(self):
        @contextmanager
        def unavailable(_settings, timeout=60):
            raise TimeoutError()
            yield
        self.lock = unavailable
        self.version_reader.return_value = "1.0.96"
        result = self.run_maintenance(Mock())
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "credential_lock_timeout")
        self.assertEqual(result["notification"]["reason"], "cli_version_mismatch")
        self.assertEqual(self.notifications, [])
        self.fingerprint_mock.assert_not_called()

    def test_critical_failover_check_only_is_silent_and_configuration_errors_never_notify(self):
        @contextmanager
        def unavailable(_settings, timeout=60):
            raise TimeoutError()
            yield
        self.lock = unavailable
        result = self.run_maintenance(Mock(), check_only=True)
        self.assertEqual(result["notification"]["reason"], "check_only")
        self.assertEqual(self.notifications, [])
        changed = dict(self.settings, expected_lark_app_id="cli_different")
        Path(self.manifest["settings_path"]).write_text(json.dumps(changed), encoding="utf-8")
        result = self.run_maintenance(Mock())
        self.assertEqual(result["reason"], "manifest_settings_mismatch")
        self.assertEqual(self.notifications, [])
        self.fingerprint_mock.assert_not_called()

    def test_alert_health_lock_uses_independent_key_without_replica_or_user_fields(self):
        lock = Mock(return_value=SimpleNamespace())
        with patch("feishu_auth_maintenance.lark_auth_lock", lock):
            maintenance.alert_health_lock(dict(self.settings, auth_credential_replica_path="private-encrypted.json"))
        lock_settings = lock.call_args.args[0]
        self.assertEqual(set(lock_settings), {"lark_home", "expected_lark_app_id"})
        self.assertEqual(lock_settings["lark_home"], self.settings["lark_home"])
        self.assertNotEqual(lock_settings["expected_lark_app_id"], self.settings["expected_lark_app_id"])

    def test_concurrent_health_failures_keep_serial_failure_count_under_alert_lock(self):
        barrier = threading.Barrier(2)
        def fail(_index):
            result = {"checkedAt": self.now.isoformat(), "ok": False, "status": "error",
                      "reason": "credential_replica_unavailable", "checkOnly": True}
            barrier.wait(timeout=5)
            return maintenance.finish_health(self.health_path, result, settings=self.settings, now=self.now,
                                             notifier=self.notify, capture_fingerprint=False)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(fail, range(2)))
        self.assertEqual(sorted(result["consecutiveFailures"] for result in results), [1, 2])
        saved = json.loads(self.health_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["consecutiveFailures"], 2)
        self.fingerprint_mock.assert_not_called()
        self.assertEqual(self.notifications, [])

    def test_offline_access_is_required_even_when_manifest_does_not_list_it(self):
        def reader(_settings, _profile, verify):
            status = self.status(verified=True if verify else None)
            status["identities"]["user"]["scope"] = "im:message docs:secure_label:readonly docs:secure_label:write_only"
            return status
        result = self.run_maintenance(reader)
        self.assertEqual(result["missingScopes"], ["offline_access"])
        self.assertEqual(result["reason"], "required_scopes_missing")
        self.assertTrue(result["requiresReauthorization"])

    def test_check_only_never_verifies_or_runs_potentially_refreshing_preflight_when_due(self):
        calls = []
        def reader(_settings, _profile, verify):
            calls.append(verify)
            return self.status(access_minutes=5)
        result = self.run_maintenance(reader, check_only=True)
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [False])
        self.worker.ensure_user_authorization.assert_not_called()
        self.assertEqual(result["notification"]["reason"], "check_only")
        self.assertEqual(self.notifications, [])

    def test_policy_is_applied_and_invalid_policy_is_rejected(self):
        self.manifest["auth_maintenance"] = {
            "check_interval_minutes": 720, "access_refresh_margin_seconds": 1800,
            "auth_verify_attempts": 2, "retry_delay_seconds": 3,
        }
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        result = self.run_maintenance(lambda _s, _p, verify: self.status(access_minutes=20, verified=True if verify else None))
        self.assertFalse(result["ok"])
        self.assertEqual(result["verificationAttempts"], 2)
        self.sleep.assert_called_once_with(3)
        self.assertEqual(result["policy"]["access_refresh_margin_seconds"], 1800)
        self.manifest["auth_maintenance"]["auth_verify_attempts"] = 4
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        reader = Mock()
        result = self.run_maintenance(reader)
        self.assertEqual(result["reason"], "maintenance_policy_invalid")
        reader.assert_not_called()

    def test_cli_version_mismatch_prevents_auth_calls(self):
        self.manifest["expected_cli_version"] = "1.0.98"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        reader = Mock()
        result = self.run_maintenance(reader)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "cli_version_mismatch")
        reader.assert_not_called()

    def test_business_preflight_failure_does_not_leak_arbitrary_exception(self):
        self.worker.ensure_report_bot_authorization.side_effect = RuntimeError("RAW_EXCEPTION_SECRET")
        result = self.run_maintenance(lambda _s, _p, _v: self.status())
        self.assertEqual(result["reason"], "business_preflight_failed")
        self.assertFalse(result["ok"])
        self.assert_no_secrets()

    def test_success_failure_history_retains_last_health_and_clears_warning(self):
        result = self.run_maintenance(lambda _s, _p, _v: self.status())
        self.assertEqual(result["latestSuccess"]["status"], "healthy")
        result = self.run_maintenance(lambda _s, _p, _v: self.status(open_id="ou_changed"))
        self.assertEqual(result["consecutiveFailures"], 1)
        self.assertEqual(result["latestSuccess"]["status"], "healthy")
        self.assertEqual(result["latestFailure"]["reason"], "identity_mismatch")
        result = self.run_maintenance(lambda _s, _p, _v: self.status(open_id="ou_changed"))
        self.assertEqual(result["consecutiveFailures"], 2)
        result = self.run_maintenance(lambda _s, _p, _v: self.status())
        self.assertEqual(result["consecutiveFailures"], 0)
        warning = json.loads(self.root.joinpath("health-warning.json").read_text(encoding="utf-8"))
        self.assertFalse(warning["active"])
        self.assertEqual(result["latestFailure"]["reason"], "identity_mismatch")
        history = json.loads(self.root.joinpath("health-history.json").read_text(encoding="utf-8"))["transitions"]
        self.assertEqual([item["status"] for item in history], ["healthy", "error", "healthy"])
        self.assert_no_secrets()

    def test_previous_metadata_is_allowlisted_before_carrying_into_health(self):
        self.health_path.write_text(json.dumps({"latestFailure": {
            "checkedAt": "SECRET_ACCESS", "status": "SECRET_APP", "reason": "SECRET_REFRESH",
            "refreshExpiresAt": "RAW_EXCEPTION_SECRET", "accessToken": "SECRET_ACCESS",
        }}), encoding="utf-8")
        self.run_maintenance(lambda _s, _p, _v: self.status())
        self.assert_no_secrets()


class CliStatusTests(unittest.TestCase):
    def test_error_classification_requires_explicit_refresh_evidence(self):
        for message, expected in (
            ("refresh_token expired SECRET", "reauthorization_required"),
            ("refresh token revoked SECRET", "reauthorization_required"),
            ("need_user_authorization", "verification_unavailable"),
            ("network timeout", "verification_unavailable"),
        ):
            error = maintenance.classify_cli_error({"error": {"message": message}})
            self.assertEqual(error.reason, expected)
            self.assertNotIn("SECRET", str(error))
        error = maintenance.classify_cli_error({"error": {"code": 20140}})
        self.assertEqual(error.reason, "administrator_action_required")
        self.assertFalse(error.reauthorize)

    @patch("feishu_auth_maintenance.subprocess.run")
    def test_cli_uses_pinned_home_profile_and_does_not_return_error_output(self, run):
        settings = {"lark_home": str(Path(tempfile.gettempdir()) / "project-home"), "lark_cli": "pinned.exe"}
        run.return_value = SimpleNamespace(returncode=0, stdout='{"appId":"cli_expected","identities":{}}', stderr="")
        maintenance.cli_status(settings, "cc-connect-chassis-user", True)
        args = run.call_args.args[0]
        self.assertEqual(args[0], "pinned.exe")
        self.assertIn("cc-connect-chassis-user", args)
        self.assertIn("--verify", args)
        self.assertEqual(run.call_args.kwargs["env"]["USERPROFILE"], str(Path(settings["lark_home"]).resolve()))
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        run.return_value = SimpleNamespace(returncode=1, stdout="", stderr='{"ok":false,"error":{"message":"refresh_token expired SECRET"}}')
        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.cli_status(settings, "cc-connect-chassis-user", True)
        self.assertEqual(caught.exception.reason, "reauthorization_required")
        self.assertNotIn("SECRET", str(caught.exception))

    @patch("feishu_auth_maintenance.subprocess.run")
    def test_zero_exit_error_envelope_is_not_success(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout='{"ok":false,"error":{"code":20140}}', stderr="")
        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.cli_status({"lark_home": str(Path.cwd()), "lark_cli": "pinned.exe"}, "user")
        self.assertEqual(caught.exception.reason, "administrator_action_required")

    @patch("feishu_auth_maintenance.subprocess.run")
    def test_nonzero_exit_can_have_json_only_in_stdout(self, run):
        run.return_value = SimpleNamespace(returncode=1, stdout='{"ok":false,"error":{"code":20140}}', stderr="")
        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.cli_status({"lark_home": str(Path.cwd()), "lark_cli": "pinned.exe"}, "user")
        self.assertEqual(caught.exception.reason, "administrator_action_required")


if __name__ == "__main__":
    unittest.main()
