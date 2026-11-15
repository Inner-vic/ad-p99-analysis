"""Exercise split-flow OAuth with all native CLI and lock/store operations mocked."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import feishu_project_oauth as oauth
from lark_auth_lock import LarkAuthLockTimeout
from lark_auth_store import LarkAuthStoreError


class ProjectOAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.private = self.root / "lark-home" / ".lark-cli"
        self.private.mkdir(parents=True)
        (self.private / "config.json").write_text("{}", encoding="utf-8")
        self.replica = self.private / "user.dpapi.json"
        self.replica.write_text("encrypted-placeholder", encoding="utf-8")
        self.manifest_path = self.root / "manifest.json"
        self.pending = self.private / oauth.PENDING_FILENAME
        self.now = datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)
        self.settings = {
            "project_id": "cc-connect-chassis", "project_isolation_required": True,
            "lark_home": str(self.private.parent), "lark_cli": sys.executable,
            "lark_profile": "cc-connect-chassis-user", "report_lark_profile": "cc-connect-chassis-report-bot",
            "expected_lark_app_id": "cli_expected", "expected_dispatch_open_id": "ou_expected",
            "auth_credential_replica_path": str(self.replica),
        }
        self.settings_path = self.root / "settings.json"
        self.manifest = {
            "active_mode": "project_home", "project_id": "cc-connect-chassis",
            "workspace_root": str(self.root), "settings_path": str(self.settings_path),
            "lark_home": str(self.private.parent), "lark_cli": sys.executable,
            "profiles": {"dispatch_user": self.settings["lark_profile"], "report_bot": self.settings["report_lark_profile"]},
            "expected_lark_app_id": "cli_expected", "expected_dispatch_open_id": "ou_expected",
            "required_user_scopes": ["im:message.send_as_user", "offline_access", "docs:secure_label:readonly", "docs:secure_label:write_only"],
            "auth_credential_replica_path": str(self.replica), "auth_maintenance": {"lock_timeout_seconds": 60},
        }
        self.save_configuration()
        self.calls = []
        self.events = []
        self.locked = False
        self.payload = {"ok": True, "data": {"device_code": "secret-device-code", "expires_in": 600,
            "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify?flow_id=fresh&user_code=user-code"}}
        self.native_code = 0
        self.user_status = {"appId": "cli_expected", "identities": {"user": {
            "available": True, "openId": "ou_expected", "scope": " ".join(self.manifest["required_user_scopes"]),
            "accessToken": "raw-access-secret", "refreshToken": "raw-refresh-secret"}}}
        self.worker = SimpleNamespace(ensure_user_authorization=Mock(side_effect=self.ensure_user),
                                      auth_status=Mock(side_effect=self.status))
        self.export_error = False
        self.after_export = None

    def save_configuration(self):
        self.settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    @contextmanager
    def lock(self, settings, timeout=60):
        self.assertFalse(self.locked)
        self.assertEqual(settings["auth_credential_replica_path"], str(self.replica))
        self.events.append("import")
        self.locked = True
        try:
            yield
        finally:
            self.events.append("export")
            self.locked = False
            if self.export_error:
                raise LarkAuthStoreError("raw-private-ciphertext-secret")
            if self.after_export:
                callback, self.after_export = self.after_export, None
                callback()

    def runner(self, command, **kwargs):
        self.assertTrue(self.locked)
        self.events.append("native")
        self.calls.append((command, kwargs))
        return CompletedProcess(command, self.native_code, json.dumps(self.payload), "raw-error-secret")

    def ensure_user(self, settings, expected, *, force_verify, now):
        self.assertTrue(self.locked)
        self.assertEqual(expected, "ou_expected")
        self.assertTrue(force_verify)
        self.events.append("preflight")
        return {"open_id": "ou_expected"}

    def status(self, settings, *, verify):
        self.assertTrue(self.locked)
        self.assertFalse(verify)
        self.events.append("local_status")
        return self.user_status

    def call(self, mode, **kwargs):
        return oauth.project_oauth(mode, self.manifest_path, now=kwargs.pop("now", self.now),
            runner=kwargs.pop("runner", self.runner), lock_factory=kwargs.pop("lock_factory", self.lock),
            worker_loader=kwargs.pop("worker_loader", lambda root: self.worker), **kwargs)

    def active(self):
        return json.loads(self.pending.read_text(encoding="utf-8"))

    def test_initiate_exact_scope_and_profile_never_prints_device_code(self):
        result = self.call("initiate")
        self.assertTrue(result["ok"])
        self.assertEqual(result["verification_url"], self.payload["data"]["verification_url"])
        self.assertEqual(result["expires_in"], 600)
        self.assertNotIn("secret-device-code", json.dumps(result))
        command, options = self.calls[0]
        self.assertEqual(command[1:3], ["auth", "login"])
        self.assertIn("--no-wait", command)
        self.assertIn("--json", command)
        self.assertNotIn("--recommend", command)
        self.assertNotIn("--domain", command)
        self.assertNotIn("--device-code", command)
        self.assertEqual(command[command.index("--profile") + 1], "cc-connect-chassis-user")
        self.assertEqual(set(command[command.index("--scope") + 1].split()), set(self.manifest["required_user_scopes"]))
        self.assertEqual(options["env"]["USERPROFILE"], str(self.private.parent))
        self.assertEqual(options["env"]["HOME"], str(self.private.parent))
        self.assertEqual(options["cwd"], str(self.root))
        self.assertEqual(self.events, ["import", "native", "export"])
        self.assertEqual(self.active()["device_code"], "secret-device-code")
        self.assertNotIn("verification_url", self.active())

    def test_complete_validates_user_scopes_then_exports_before_delete(self):
        self.call("initiate")
        self.events.clear()
        self.payload = {"ok": True, "identity": "user", "data": {"loggedIn": True, "accessToken": "raw-secret"}}
        self.after_export = lambda: self.assertTrue(self.pending.exists())
        result = self.call("complete", now=self.now + timedelta(seconds=10))
        self.assertEqual(result["status"], "authorized")
        self.assertFalse(self.pending.exists())
        self.assertEqual(self.events, ["import", "native", "preflight", "local_status", "export", "import", "export"])
        command = self.calls[-1][0]
        self.assertEqual(command[command.index("--device-code") + 1], "secret-device-code")
        self.assertNotIn("--no-wait", command)
        self.assertNotIn("raw-secret", json.dumps(result))

    def test_expired_flow_refused_without_cli_or_delete(self):
        self.call("initiate")
        count = len(self.calls)
        result = self.call("complete", now=self.now + timedelta(seconds=600))
        self.assertEqual(result["reason"], "active_flow_expired")
        self.assertEqual(len(self.calls), count)
        self.assertTrue(self.pending.exists())

    def test_missing_flow_cannot_complete(self):
        self.assertEqual(self.call("complete")["reason"], "active_flow_missing")
        self.assertEqual(self.calls, [])

    def test_pending_identity_or_scope_mismatch_refused(self):
        for key, value in (("open_id", "ou_other"), ("app_id", "cli_other"), ("profile", "global"), ("scopes", ["all"])):
            self.call("initiate")
            active = self.active()
            active[key] = value
            self.pending.write_text(json.dumps(active), encoding="utf-8")
            count = len(self.calls)
            result = self.call("complete")
            self.assertEqual(result["reason"], "active_flow_identity_mismatch")
            self.assertEqual(len(self.calls), count)

    def test_corrupt_or_oversized_flow_refused(self):
        for value in ("corrupt", "a" * (oauth.MAX_PENDING_BYTES + 1), "[]"):
            self.pending.write_text(value, encoding="utf-8")
            self.assertEqual(self.call("complete")["reason"], "active_flow_invalid")
        self.assertEqual(self.calls, [])

    def test_new_initiation_replaces_old_flow_and_does_not_reuse_link(self):
        first = self.call("initiate")
        first_id = self.active()["flow_id"]
        self.payload["data"]["verification_url"] = "https://accounts.feishu.cn/oauth/v1/device/verify?flow_id=new"
        self.payload["data"]["device_code"] = "new-secret-code"
        second = self.call("initiate", now=self.now + timedelta(seconds=5))
        self.assertNotEqual(first["verification_url"], second["verification_url"])
        self.assertNotEqual(first_id, self.active()["flow_id"])
        self.assertEqual(self.active()["device_code"], "new-secret-code")

    def test_failed_new_initiation_invalidates_old_active_flow(self):
        self.call("initiate")
        self.native_code = 1
        self.payload = {"ok": False, "error": {"message": "raw-token-secret"}}
        result = self.call("initiate")
        self.assertEqual(result["reason"], "oauth_command_failed")
        self.assertFalse(self.pending.exists())
        self.assertNotIn("raw-token-secret", json.dumps(result))

    def test_ttl_clamped_to_native_expiry_and_maximum(self):
        self.payload["data"]["expires_in"] = 1200
        self.assertEqual(self.call("initiate")["expires_in"], 600)
        self.payload["data"]["expires_in"] = 90
        self.assertEqual(self.call("initiate")["expires_in"], 90)
        for ttl in (0, -1, "600", None, True):
            self.payload["data"]["expires_in"] = ttl
            self.assertEqual(self.call("initiate")["reason"], "oauth_response_invalid")

    def test_nonofficial_or_nonhttps_urls_refused(self):
        for url in ("http://accounts.feishu.cn/verify", "https://attacker.example/verify", "https://accounts.feishu.cn/verify\nsecret"):
            self.payload["data"]["verification_url"] = url
            self.assertEqual(self.call("initiate")["reason"], "oauth_response_invalid")

    def test_wrong_authenticated_user_keeps_pending_and_never_succeeds(self):
        self.call("initiate")
        self.worker.ensure_user_authorization = Mock(return_value={"open_id": "ou_other"})
        self.assertEqual(self.call("complete")["reason"], "authorized_identity_mismatch")
        self.assertTrue(self.pending.exists())

    def test_wrong_app_or_missing_scope_refused(self):
        self.call("initiate")
        self.user_status["appId"] = "cli_other"
        self.assertEqual(self.call("complete")["reason"], "authorized_identity_mismatch")
        self.user_status["appId"] = "cli_expected"
        self.user_status["identities"]["user"]["scope"] = "offline_access"
        self.assertEqual(self.call("complete")["reason"], "authorized_scopes_missing")
        self.assertTrue(self.pending.exists())

    def test_verification_failure_is_sanitized_and_keeps_pending(self):
        self.call("initiate")
        self.worker.ensure_user_authorization = Mock(side_effect=RuntimeError("raw-access-secret"))
        result = self.call("complete")
        self.assertEqual(result["reason"], "authorization_verification_failed")
        self.assertNotIn("raw-access-secret", json.dumps(result))
        self.assertTrue(self.pending.exists())

    def test_canonical_export_failure_preserves_flow(self):
        self.call("initiate")
        self.export_error = True
        result = self.call("complete")
        self.assertEqual(result["reason"], "credential_replica_unavailable")
        self.assertTrue(self.pending.exists())
        self.assertNotIn("ciphertext", json.dumps(result))

    def test_new_flow_between_export_and_cleanup_is_preserved(self):
        self.call("initiate")
        def replace():
            active = self.active()
            active["flow_id"] = "a" * 32
            active["device_code"] = "next-flow-code"
            self.pending.write_text(json.dumps(active), encoding="utf-8")
        self.after_export = replace
        self.assertTrue(self.call("complete")["ok"])
        self.assertEqual(self.active()["device_code"], "next-flow-code")

    def test_missing_canonical_or_lock_timeout_never_calls_cli(self):
        @contextmanager
        def missing(settings, timeout):
            raise LarkAuthStoreError("raw-secret")
            yield
        self.assertEqual(self.call("initiate", lock_factory=missing)["reason"], "credential_replica_unavailable")
        @contextmanager
        def timeout(settings, timeout):
            raise LarkAuthLockTimeout("raw-secret")
            yield
        self.assertEqual(self.call("initiate", lock_factory=timeout)["reason"], "credential_lock_timeout")
        self.assertEqual(self.calls, [])

    def test_missing_replica_configuration_or_manifest_mismatch_refused(self):
        self.manifest.pop("auth_credential_replica_path")
        self.settings.pop("auth_credential_replica_path")
        self.save_configuration()
        self.assertEqual(self.call("initiate")["reason"], "credential_replica_not_configured")
        self.settings["expected_dispatch_open_id"] = "ou_other"
        self.save_configuration()
        self.assertEqual(self.call("initiate")["reason"], "project_configuration_invalid")
        self.assertEqual(self.calls, [])

    def test_native_timeout_or_invalid_json_never_prints_raw_error(self):
        def timeout(command, **kwargs):
            raise TimeoutExpired(command, 60, output="raw-access-secret")
        result = self.call("initiate", runner=timeout)
        self.assertEqual(result["reason"], "oauth_command_unavailable")
        def invalid(command, **kwargs):
            return CompletedProcess(command, 0, "raw-secret-token", "")
        result = self.call("initiate", runner=invalid)
        self.assertEqual(result["reason"], "oauth_response_invalid")
        self.assertNotIn("raw-secret", json.dumps(result))

    def test_cli_stdout_only_safe_result(self):
        result = self.call("initiate")
        output = io.StringIO()
        with patch.object(oauth, "project_oauth", return_value=result) as operation, contextlib.redirect_stdout(output):
            self.assertEqual(oauth.main(["--initiate", "--manifest", str(self.manifest_path)]), 0)
        operation.assert_called_once_with("initiate", self.manifest_path)
        self.assertNotIn("device_code", output.getvalue())
        self.assertNotIn("secret-device-code", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["verification_url"], result["verification_url"])


if __name__ == "__main__":
    unittest.main()
