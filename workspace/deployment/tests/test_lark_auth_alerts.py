"""No test calls a real CLI or sends a real Feishu message."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lark_auth_alerts import STATE_FILENAME, notify_auth_health


class AuthAlertTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.now = datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)
        self.manifest = {
            "workspace_root": str(self.workspace), "project_id": "cc-connect-chassis",
            "expected_dispatch_open_id": "ou_fixed_user", "expected_lark_app_id": "cli_fixed_app",
            "lark_cli": "/project/lark-cli", "lark_home": "/project/lark-home",
            "profiles": {"dispatch_user": "cc-connect-chassis-user", "report_bot": "cc-connect-chassis-report-bot"},
            "auth_maintenance": {"notifications": {"enabled": True, "recipient_open_id": "ou_fixed_user",
                "profile": "cc-connect-chassis-report-bot", "reminder_hours": 24}},
        }
        self.settings = {
            "project_id": "cc-connect-chassis", "expected_dispatch_open_id": "ou_fixed_user",
            "expected_lark_app_id": "cli_fixed_app", "lark_cli": "/project/lark-cli", "lark_home": "/project/lark-home",
            "lark_profile": "cc-connect-chassis-user", "report_lark_profile": "cc-connect-chassis-report-bot",
        }
        self.calls = []
        self.bot_status = {"appId": "cli_fixed_app", "identities": {"bot": {"available": True}, "user": {"available": False}}}
        self.send_payload = {"ok": True, "identity": "bot", "data": {"message_id": "om_alert_1"}}
        self.send_returncode = 0

    def runner(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[1:3] == ["auth", "status"]:
            return CompletedProcess(command, 0, json.dumps(self.bot_status), "")
        self.assertEqual(command[1:3], ["im", "+messages-send"])
        return CompletedProcess(command, self.send_returncode, json.dumps(self.send_payload), "raw-secret-error-never-save")

    def notify(self, health=None, now=None, runner=None):
        return notify_auth_health(self.manifest, self.settings,
            health or {"ok": False, "status": "reauthorization_required"},
            now=now or self.now, runner=runner or self.runner)

    def state(self):
        return json.loads((self.workspace / "data" / STATE_FILENAME).read_text(encoding="utf-8"))

    def text(self):
        command = self.calls[-1][0]
        return command[command.index("--text") + 1]

    def test_first_healthy_does_not_send(self):
        result = self.notify({"ok": True, "status": "healthy"})
        self.assertEqual(result["status"], "suppressed")
        self.assertEqual(self.calls, [])

    def test_warning_is_immediate_even_when_business_ok(self):
        result = self.notify({"ok": True, "status": "warning", "reason": "refresh_near_expiry",
                              "refreshExpiresAt": "2026-10-13T12:30:00Z"})
        self.assertEqual(result, {"status": "sent", "reason": "refresh_near_expiry", "message_id": "om_alert_1"})
        self.assertIn("2026-10-13 20:30:00（北京时间）", self.text())

    def test_send_pins_recipient_bot_profile_and_project_home(self):
        self.assertEqual(self.notify()["status"], "sent")
        command, kwargs = self.calls[-1]
        for flag, value in (("--user-id", "ou_fixed_user"), ("--as", "bot"),
                            ("--profile", "cc-connect-chassis-report-bot"), ("--format", "json")):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertLessEqual(len(command[command.index("--idempotency-key") + 1]), 50)
        self.assertEqual(kwargs["env"]["USERPROFILE"], self.manifest["lark_home"])
        self.assertEqual(kwargs["env"]["HOME"], self.manifest["lark_home"])
        self.assertEqual(kwargs["cwd"], str(self.workspace))
        self.assertNotIn("--verify", self.calls[0][0])

    def test_same_status_suppressed_until_24_hours(self):
        self.notify()
        first_key = self.calls[-1][0][self.calls[-1][0].index("--idempotency-key") + 1]
        self.calls.clear()
        self.assertEqual(self.notify(now=self.now + timedelta(hours=23, minutes=59))["status"], "suppressed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.notify(now=self.now + timedelta(hours=24))["status"], "sent")
        reminder_key = self.calls[-1][0][self.calls[-1][0].index("--idempotency-key") + 1]
        self.assertNotEqual(first_key, reminder_key)

    def test_status_change_sends_immediately_and_recovery_only_once(self):
        self.notify({"status": "warning", "reason": "refresh_near_expiry"})
        self.assertEqual(self.notify(now=self.now + timedelta(minutes=1))["status"], "sent")
        self.assertEqual(self.notify({"ok": True, "status": "healthy"}, now=self.now + timedelta(minutes=2))["reason"], "recovery")
        self.calls.clear()
        self.assertEqual(self.notify({"ok": True, "status": "healthy"}, now=self.now + timedelta(days=2))["status"], "suppressed")
        self.assertEqual(self.calls, [])
        self.assertFalse(self.state()["incident_open"])

    def test_transient_failure_requires_three_consecutive_checks(self):
        for count in (0, 1, 2, "malformed-secret"):
            result = self.notify({"status": "error", "reason": "verification_unavailable", "consecutiveFailures": count})
            self.assertEqual(result["status"], "suppressed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.notify({"status": "error", "reason": "verification_unavailable", "consecutiveFailures": 3})["status"], "sent")

    def test_configuration_problem_is_immediate_but_message_is_sanitized(self):
        health = {"status": "error", "reason": "identity_mismatch", "error": "u-access-token-secret",
                  "refreshExpiresAt": "https://oauth.example/secret", "accessToken": "raw-token-secret"}
        self.assertEqual(self.notify(health)["status"], "sent")
        text = self.text()
        state = json.dumps(self.state())
        for secret in ("u-access-token-secret", "https://oauth.example/secret", "raw-token-secret"):
            self.assertNotIn(secret, text + state)
        self.assertIn("身份或必需权限", text)

    def test_recipient_or_settings_identity_mismatch_refused(self):
        self.manifest["auth_maintenance"]["notifications"]["recipient_open_id"] = "ou_other"
        self.assertEqual(self.notify()["reason"], "recipient_identity_mismatch")
        self.assertEqual(self.calls, [])
        self.manifest["auth_maintenance"]["notifications"]["recipient_open_id"] = "ou_fixed_user"
        self.settings["expected_dispatch_open_id"] = "ou_other"
        self.assertEqual(self.notify()["reason"], "recipient_identity_mismatch")

    def test_user_or_app_mismatch_bot_refused(self):
        self.bot_status["identities"]["user"]["available"] = True
        self.assertEqual(self.notify()["reason"], "report_bot_unavailable")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("pending", self.state())
        self.bot_status["identities"]["user"]["available"] = False
        self.bot_status["appId"] = "cli_other"
        self.assertEqual(self.notify()["reason"], "report_bot_unavailable")

    def test_report_profile_and_project_cli_mismatch_refused(self):
        self.manifest["auth_maintenance"]["notifications"]["profile"] = "cc-connect-chassis-user"
        self.assertEqual(self.notify()["reason"], "report_profile_mismatch")
        self.manifest["auth_maintenance"]["notifications"]["profile"] = "cc-connect-chassis-report-bot"
        self.settings["lark_cli"] = "global-cli"
        self.assertEqual(self.notify()["reason"], "project_cli_mismatch")
        self.assertEqual(self.calls, [])

    def test_wrapped_bot_status_and_persist_before_external_write(self):
        self.bot_status = {"ok": True, "identity": "bot", "data": self.bot_status}
        def persisted_runner(command, **kwargs):
            if command[1:3] == ["im", "+messages-send"]:
                self.assertEqual(self.state()["pending"]["idempotency_key"],
                                 command[command.index("--idempotency-key") + 1])
            return self.runner(command, **kwargs)
        self.assertEqual(self.notify(runner=persisted_runner)["status"], "sent")

    def test_interval_less_than_24_hours_or_nonfinite_refused(self):
        for interval in (1, "nan", "inf", "secret-malformed"):
            self.manifest["auth_maintenance"]["notifications"]["reminder_hours"] = interval
            self.assertEqual(self.notify()["reason"], "reminder_interval_invalid")
        self.assertEqual(self.calls, [])

    def test_send_failure_not_marked_delivered_and_reuses_key(self):
        self.send_payload = {"ok": False, "error": {"message": "raw-token-secret"}}
        self.send_returncode = 1
        self.assertEqual(self.notify()["status"], "failed")
        state = self.state()
        self.assertNotIn("message_id", state)
        self.assertNotIn("last_delivered_at", state)
        first_key = state["pending"]["idempotency_key"]
        self.assertNotIn("raw-token-secret", json.dumps(state))
        self.send_payload = {"ok": True, "identity": "bot", "data": {"message_id": "om_alert_retry"}}
        self.send_returncode = 0
        self.assertEqual(self.notify(now=self.now + timedelta(minutes=2))["status"], "sent")
        command = self.calls[-1][0]
        self.assertEqual(command[command.index("--idempotency-key") + 1], first_key)
        self.assertEqual(self.state()["message_id"], "om_alert_retry")

    def test_missing_message_id_or_wrong_response_identity_is_failure(self):
        self.send_payload = {"ok": True, "identity": "bot", "data": {}}
        self.assertEqual(self.notify()["status"], "failed")
        self.send_payload = {"ok": True, "identity": "user", "data": {"message_id": "om_wrong"}}
        self.assertEqual(self.notify()["status"], "failed")
        self.assertNotIn("last_delivered_at", self.state())

    def test_timeout_and_invalid_json_do_not_leak_secrets(self):
        def fail(command, **kwargs):
            raise TimeoutExpired(command + ["raw-token-secret"], 30)
        self.assertEqual(self.notify(runner=fail)["status"], "failed")
        self.assertNotIn("raw-token-secret", json.dumps(self.state()))
        def bad_json(command, **kwargs):
            return CompletedProcess(command, 0, "raw-token-secret", "")
        self.assertEqual(self.notify(runner=bad_json)["reason"], "report_bot_unavailable")

    def test_disabled_does_not_touch_state_or_run_cli(self):
        self.manifest["auth_maintenance"]["notifications"]["enabled"] = False
        self.assertEqual(self.notify()["status"], "disabled")
        self.assertEqual(self.calls, [])
        self.assertFalse((self.workspace / "data" / STATE_FILENAME).exists())

    def test_failed_alert_then_healthy_has_no_recovery_message(self):
        self.bot_status["identities"]["bot"]["available"] = False
        self.notify()
        self.calls.clear()
        self.assertEqual(self.notify({"ok": True, "status": "healthy"})["status"], "suppressed")
        self.assertEqual(self.calls, [])
        self.assertNotIn("pending", self.state())

    def test_corrupt_state_fails_without_overwriting_or_sending(self):
        directory = self.workspace / "data"
        directory.mkdir()
        path = directory / STATE_FILENAME
        path.write_text("corrupt", encoding="utf-8")
        self.assertEqual(self.notify()["reason"], "alert_state_unreadable")
        self.assertEqual(self.calls, [])
        self.assertEqual(path.read_text(encoding="utf-8"), "corrupt")

    def test_recovery_send_failure_remains_pending_for_retry(self):
        self.notify()
        self.send_returncode = 1
        self.assertEqual(self.notify({"ok": True, "status": "healthy"}, now=self.now + timedelta(minutes=1))["status"], "failed")
        self.assertTrue(self.state()["incident_open"])
        self.send_returncode = 0
        self.assertEqual(self.notify({"ok": True, "status": "healthy"}, now=self.now + timedelta(minutes=2))["reason"], "recovery")


if __name__ == "__main__":
    unittest.main()
