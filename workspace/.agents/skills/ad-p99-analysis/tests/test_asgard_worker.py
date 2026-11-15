import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ad_p99 import InputError
from asgard_worker import (
    WORKSPACE_ROOT,
    UserAuthorizationError,
    configure_report_secure_label,
    create_report,
    deliver,
    ensure_report_bot_authorization,
    ensure_secure_label_authorization,
    ensure_user_authorization,
    lark_environment,
    load_settings,
    apply_report_secure_label,
    grant_secure_label_operator,
    grant_source_chat_view,
    resolve_report_secure_label,
    report_requires_secure_label,
    revoke_secure_label_operator,
    run_lark,
    restrict_collaborator_management,
    send_asgard,
    start_job,
    transfer_report_owner,
    vehicle_label_from_topic,
)


REQUEST_ID = "om_version_request"
VIN = "DEM0VEH1CLEX00001"
ASGARD_ID = "ou_asgard"


def payload(content, *, sender=ASGARD_ID):
    return {
        "data": {
            "messages": [{
                "message_id": REQUEST_ID,
                "content": f"@阿斯加德 查询车辆版本 {VIN} CCUFSD",
                "thread_replies": [{
                    "message_id": "om_version_response",
                    "sender": {"open_bot_id": sender},
                    "content": content,
                }],
            }],
        },
    }


class ProjectIsolationTests(unittest.TestCase):
    def isolated_settings(self, root: Path) -> dict:
        return {
            "asgard_open_id": ASGARD_ID,
            "state_dir": str(root / "state"),
            "runtime_state_dir": str(root / "runtime"),
            "lark_cli": sys.executable,
            "lark_profile": "cc-connect-chassis-user",
            "report_lark_profile": "cc-connect-chassis-report-bot",
            "project_isolation_required": True,
            "project_id": "cc-connect-chassis",
            "lark_home": str(root / "lark-home"),
        }

    def test_load_settings_requires_initialized_namespaced_lark_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "lark-home" / ".lark-cli"
            home.mkdir(parents=True)
            (home / "config.json").write_text('{"apps": []}\n', encoding="utf-8")
            settings = self.isolated_settings(root)
            path = root / "settings.json"
            path.write_text(json.dumps(settings), encoding="utf-8")

            loaded = load_settings(path)
            environment = lark_environment(loaded)

            self.assertEqual(environment["USERPROFILE"], str((root / "lark-home").resolve()))
            self.assertEqual(environment["HOME"], str((root / "lark-home").resolve()))

    def test_load_settings_rejects_profile_outside_project_namespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "lark-home" / ".lark-cli"
            home.mkdir(parents=True)
            (home / "config.json").write_text('{"apps": []}\n', encoding="utf-8")
            settings = self.isolated_settings(root)
            settings["lark_profile"] = "shared-user"
            path = root / "settings.json"
            path.write_text(json.dumps(settings), encoding="utf-8")

            with self.assertRaisesRegex(InputError, "project_id 命名空间"):
                load_settings(path)

    def test_start_job_does_not_pass_activated_lark_home_to_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host_home = str((root / "host-home").resolve())
            lark_home = str((root / "lark-home").resolve())
            settings = dict(
                self.isolated_settings(root),
                asgard_platform="CCUFSD",
            )
            args = SimpleNamespace(
                ccconnect_context='[cc-connect request_context_v=1 sender_id=ou_requester platform=feishu chat_id=oc_source message_id=om_source thread_id=]',
                chat_id="oc_source",
                sender_id="ou_requester",
                message_id="om_source",
                text="ignored by test",
                settings=root / "settings.json",
            )
            request = {
                "vin": VIN,
                "baseline_metadata": {
                    "vin": VIN,
                    "start": "2026-09-18T09:48:00",
                    "end": "2026-09-18T10:24:00",
                },
                "replacement_metadata": {
                    "vin": VIN,
                    "start": "2026-09-18T10:37:00",
                    "end": "2026-09-18T11:11:00",
                },
            }

            def activate(_settings):
                os.environ["HOME"] = lark_home
                os.environ["USERPROFILE"] = lark_home

            with patch.dict(os.environ, {"HOME": host_home, "USERPROFILE": host_home}, clear=False), \
                    patch("asgard_worker.load_settings", return_value=settings), \
                    patch("asgard_worker.activate_lark_environment", side_effect=activate), \
                    patch("asgard_worker.ensure_report_bot_authorization"), \
                    patch("asgard_worker.ensure_user_authorization", return_value={"open_id": "ou_developer"}), \
                    patch("asgard_worker.ensure_secure_label_authorization"), \
                    patch("asgard_worker.resolve_report_secure_label", return_value={"id": "1002", "name": "L3"}), \
                    patch("asgard_worker.parse_comparison_request", return_value=request), \
                    patch("asgard_worker.subprocess.Popen") as popen:
                result = start_job(args)
                worker_env = popen.call_args.kwargs["env"]
                self.assertEqual(worker_env["HOME"], host_home)
                self.assertEqual(worker_env["USERPROFILE"], host_home)
                self.assertNotEqual(worker_env["HOME"], lark_home)
                self.assertEqual(result["status"], "queued")
                popen.call_args.kwargs["stdout"].close()


class VehicleLabelTests(unittest.TestCase):
    def test_extracts_product_label_from_verified_topic_response(self):
        result = vehicle_label_from_topic(
            payload("<card>\n**相关制品信息:** 9A1A, 9.2.0-4305\n</card>"),
            request_message_id=REQUEST_ID,
            expected_vin=VIN,
            expected_asgard_id=ASGARD_ID,
        )
        self.assertEqual(result["label"], "9A1A")
        self.assertEqual(result["artifact_info"], "9A1A, 9.2.0-4305")

    def test_ignores_untrusted_sender_and_rejects_wrong_topic_query(self):
        result = vehicle_label_from_topic(
            payload("**相关制品信息:** FAKE, 1.0", sender="ou_other"),
            request_message_id=REQUEST_ID,
            expected_vin=VIN,
            expected_asgard_id=ASGARD_ID,
        )
        self.assertIsNone(result)
        wrong = payload("**相关制品信息:** 9A1A, 9.2.0-4305")
        wrong["data"]["messages"][0]["content"] = "查询车辆版本 OTHER CCUFSD"
        with self.assertRaisesRegex(InputError, "任务 VIN 不匹配"):
            vehicle_label_from_topic(
                wrong,
                request_message_id=REQUEST_ID,
                expected_vin=VIN,
                expected_asgard_id=ASGARD_ID,
            )

    def test_rejects_ambiguous_or_unsafe_product_labels(self):
        ambiguous = payload("**相关制品信息:** 9A1A, 9.2.0-4305")
        ambiguous["data"]["messages"][0]["thread_replies"].append({
            "message_id": "om_second",
            "sender": {"open_bot_id": ASGARD_ID},
            "content": "**相关制品信息:** 9A11, 9.2.0-4285",
        })
        with self.assertRaisesRegex(InputError, "多个不同"):
            vehicle_label_from_topic(
                ambiguous,
                request_message_id=REQUEST_ID,
                expected_vin=VIN,
                expected_asgard_id=ASGARD_ID,
            )
        with self.assertRaisesRegex(InputError, "格式无效"):
            vehicle_label_from_topic(
                payload("**相关制品信息:** ../../bad, 1.0"),
                request_message_id=REQUEST_ID,
                expected_vin=VIN,
                expected_asgard_id=ASGARD_ID,
            )


class AuthorizationPreflightTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
        self.settings = {
            "lark_cli": "lark-cli",
            "lark_profile": "cc-connect-chassis",
            "asgard_open_id": ASGARD_ID,
        }
        self.open_id = "ou_requester"

    def completed(self, payload, returncode=0):
        import json
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr="")

    def status(self, *, expires_at, verified=None, open_id=None, available=True, token_status="valid",
               refresh_expires_at=None, refresh_status=None):
        payload = {
            "identities": {
                "user": {
                    "available": available,
                    "verified": verified,
                    "tokenStatus": token_status,
                    "openId": open_id or self.open_id,
                    "expiresAt": expires_at.isoformat(),
                }
            }
        }
        if refresh_expires_at is not None:
            payload["identities"]["user"]["refreshExpiresAt"] = refresh_expires_at.isoformat()
        if refresh_status is not None:
            payload["identities"]["user"]["refreshTokenStatus"] = refresh_status
        return payload

    @patch("asgard_worker.subprocess.run")
    def test_valid_token_skips_network_verify(self, run):
        run.return_value = self.completed(self.status(expires_at=self.now + timedelta(hours=2)))
        result = ensure_user_authorization(self.settings, self.open_id, now=self.now)
        self.assertFalse(result["refreshed"])
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("--verify", run.call_args.args[0])

    @patch("asgard_worker.subprocess.run")
    def test_accepts_fixed_developer_profile_for_a_different_requester(self, run):
        run.return_value = self.completed(self.status(
            expires_at=self.now + timedelta(hours=2), open_id="ou_developer"
        ))
        result = ensure_user_authorization(self.settings, now=self.now)
        self.assertEqual(result["open_id"], "ou_developer")
        self.assertFalse(result["refreshed"])

    @patch("asgard_worker.subprocess.run")
    def test_near_expiry_refreshes_and_verifies(self, run):
        local = self.status(expires_at=self.now + timedelta(minutes=5))
        verified = self.status(expires_at=self.now + timedelta(hours=2), verified=True)
        run.side_effect = [self.completed(local), self.completed(verified)]
        result = ensure_user_authorization(self.settings, self.open_id, now=self.now)
        self.assertTrue(result["refreshed"])
        self.assertEqual(run.call_count, 2)
        self.assertIn("--verify", run.call_args.args[0])
        self.assertIn("cc-connect-chassis", run.call_args.args[0])

    @patch("asgard_worker.time.sleep")
    @patch("asgard_worker.subprocess.run")
    def test_transient_invalid_verify_is_confirmed_before_reauth(self, run, sleep):
        local = self.status(expires_at=self.now + timedelta(minutes=5))
        transient = self.status(
            expires_at=self.now + timedelta(minutes=5), verified=False,
            available=False, token_status="missing",
        )
        recovered = self.status(expires_at=self.now + timedelta(hours=2), verified=True)
        run.side_effect = [self.completed(local), self.completed(transient), self.completed(recovered)]
        result = ensure_user_authorization(self.settings, self.open_id, now=self.now)
        self.assertTrue(result["refreshed"])
        self.assertEqual(run.call_count, 3)
        sleep.assert_called_once_with(1.0)

    @patch("asgard_worker.time.sleep")
    @patch("asgard_worker.subprocess.run")
    def test_reauth_is_requested_after_explicit_refresh_expiry_is_confirmed(self, run, sleep):
        local = self.status(expires_at=self.now + timedelta(minutes=5))
        invalid = self.status(
            expires_at=self.now + timedelta(minutes=5), verified=False,
            available=False, token_status="missing",
            refresh_expires_at=self.now - timedelta(days=1),
        )
        run.side_effect = [self.completed(local), self.completed(invalid), self.completed(invalid)]
        with self.assertRaisesRegex(UserAuthorizationError, "连续校验失败"):
            ensure_user_authorization(self.settings, self.open_id, now=self.now)
        self.assertEqual(run.call_count, 3)
        sleep.assert_called_once_with(1.0)

    @patch("asgard_worker.time.sleep")
    @patch("asgard_worker.subprocess.run")
    def test_missing_status_does_not_claim_oauth_is_required(self, run, sleep):
        local = self.status(expires_at=self.now + timedelta(minutes=5))
        missing = self.status(
            expires_at=self.now + timedelta(minutes=5), verified=False,
            available=False, token_status="missing", refresh_status="missing",
        )
        run.side_effect = [self.completed(local), self.completed(missing), self.completed(missing)]
        with self.assertRaises(InputError) as raised:
            ensure_user_authorization(self.settings, self.open_id, now=self.now)
        self.assertNotIsInstance(raised.exception, UserAuthorizationError)
        self.assertNotIn("请本人", str(raised.exception))
        self.assertEqual(run.call_count, 3)

    @patch("asgard_worker.time.sleep")
    @patch("asgard_worker.subprocess.run")
    def test_transient_timeout_is_retried_and_returns_safe_expiry_metadata(self, run, sleep):
        local = self.status(expires_at=self.now + timedelta(minutes=5))
        verified = self.status(
            expires_at=self.now + timedelta(hours=2), verified=True,
            refresh_expires_at=self.now + timedelta(days=7),
        )
        run.side_effect = [self.completed(local), subprocess.TimeoutExpired("lark-cli", 30), self.completed(verified)]
        result = ensure_user_authorization(self.settings, self.open_id, now=self.now)
        self.assertEqual(result["refresh_expires_at"], (self.now + timedelta(days=7)).isoformat())
        self.assertEqual(set(result), {"refreshed", "open_id", "expires_at", "refresh_expires_at"})
        self.assertEqual(run.call_count, 3)

    @patch("asgard_worker.time.sleep")
    @patch("asgard_worker.subprocess.run")
    def test_repeated_timeouts_remain_retryable_diagnostics(self, run, sleep):
        local = self.status(expires_at=self.now + timedelta(minutes=5))
        run.side_effect = [self.completed(local), subprocess.TimeoutExpired("lark-cli", 30), subprocess.TimeoutExpired("lark-cli", 30)]
        with self.assertRaises(InputError) as raised:
            ensure_user_authorization(self.settings, self.open_id, now=self.now)
        self.assertNotIsInstance(raised.exception, UserAuthorizationError)
        self.assertNotIn("请本人", str(raised.exception))

    @patch("asgard_worker.subprocess.run")
    def test_wrong_app_is_rejected_without_refresh(self, run):
        payload = self.status(expires_at=self.now + timedelta(hours=2))
        payload["appId"] = "cli_other"
        run.return_value = self.completed(payload)
        with self.assertRaisesRegex(InputError, "App ID"):
            ensure_user_authorization(dict(self.settings, expected_lark_app_id="cli_project"), now=self.now)
        self.assertEqual(run.call_count, 1)

    @patch("asgard_worker.time.sleep")
    @patch("asgard_worker.subprocess.run")
    def test_missing_app_is_retryable_rather_than_reauthorization(self, run, sleep):
        payload = self.status(expires_at=self.now + timedelta(hours=2), verified=True)
        run.return_value = self.completed(payload)
        with self.assertRaises(InputError) as raised:
            ensure_user_authorization(dict(self.settings, expected_lark_app_id="cli_project"), now=self.now)
        self.assertNotIsInstance(raised.exception, UserAuthorizationError)
        self.assertEqual(run.call_count, 3)

    @patch("asgard_worker.time.sleep")
    @patch("asgard_worker.subprocess.run")
    def test_verified_status_without_expiry_remains_diagnostic(self, run, sleep):
        payload = self.status(expires_at=self.now + timedelta(hours=2), verified=True)
        del payload["identities"]["user"]["expiresAt"]
        run.return_value = self.completed(payload)
        with self.assertRaises(InputError) as raised:
            ensure_user_authorization(self.settings, self.open_id, now=self.now)
        self.assertNotIsInstance(raised.exception, UserAuthorizationError)
        self.assertEqual(run.call_count, 3)

    @patch("asgard_worker.time.sleep")
    @patch("asgard_worker.subprocess.run")
    def test_structured_expired_refresh_error_requires_oauth(self, run, sleep):
        local = self.status(expires_at=self.now + timedelta(minutes=5))
        error = {"ok": False, "error": {"type": "refresh_token_expired"}}
        failure = SimpleNamespace(returncode=1, stdout=json.dumps(error), stderr="")
        run.side_effect = [self.completed(local), failure, failure]
        with self.assertRaises(UserAuthorizationError):
            ensure_user_authorization(self.settings, self.open_id, now=self.now)

    @patch("asgard_worker.subprocess.run")
    def test_unsupported_auth_is_an_administrator_error(self, run):
        local = self.status(expires_at=self.now + timedelta(minutes=5))
        error = {"ok": False, "error": {"code": 20140, "message": "The auth method is not supported"}}
        run.side_effect = [self.completed(local), SimpleNamespace(returncode=1, stdout="", stderr=json.dumps(error))]
        with self.assertRaisesRegex(InputError, "应用管理员") as raised:
            ensure_user_authorization(self.settings, self.open_id, now=self.now)
        self.assertNotIsInstance(raised.exception, UserAuthorizationError)
        self.assertEqual(run.call_count, 2)

    @patch("asgard_worker.lark_auth_lock")
    @patch("asgard_worker.subprocess.run")
    def test_auth_status_and_business_calls_share_credential_lock(self, run, lock):
        run.side_effect = [
            self.completed(self.status(expires_at=self.now + timedelta(hours=2))),
            self.completed({"ok": True, "data": {"message_id": "om_sent"}}),
        ]
        ensure_user_authorization(self.settings, self.open_id, now=self.now)
        run_lark(self.settings, ["im", "+messages-send", "--as", "user"])
        self.assertEqual(lock.call_count, 2)
        self.assertEqual(lock.call_args_list[0].args[0], self.settings)
        self.assertEqual(lock.call_args_list[1].args[0], self.settings)

    @patch("asgard_worker.subprocess.run")
    def test_business_structured_auth_error_is_detected_in_stdout(self, run):
        error = {"ok": False, "error": {"type": "need_user_authorization", "message": "user login required"}}
        run.return_value = SimpleNamespace(returncode=1, stdout=json.dumps(error), stderr="")
        with self.assertRaises(UserAuthorizationError):
            run_lark(self.settings, ["im", "+messages-send", "--as", "user"])

    @patch("asgard_worker.subprocess.run")
    def test_rejects_changed_pinned_dispatch_user_without_refresh(self, run):
        run.return_value = self.completed(self.status(
            expires_at=self.now + timedelta(hours=2), open_id="ou_other"
        ))
        with self.assertRaisesRegex(InputError, "派发 profile 的授权用户已变化"):
            ensure_user_authorization(self.settings, self.open_id, now=self.now)
        self.assertEqual(run.call_count, 1)

    @patch("asgard_worker.ensure_user_authorization")
    @patch("asgard_worker.run_lark")
    def test_first_dispatch_auth_failure_refreshes_and_retries_once(self, run_lark, ensure):
        run_lark.side_effect = [
            UserAuthorizationError("need_user_authorization"),
            {"data": {"message_id": "om_sent"}},
        ]
        sender = send_asgard(
            self.settings, "om_source", "oc_chat", thread_id="om_thread",
            dispatch_user_open_id=self.open_id,
        )
        result = sender("查询车辆版本 VIN CCUFSD", role="vehicle_version")
        self.assertEqual(result["message_id"], "om_sent")
        self.assertEqual(run_lark.call_count, 2)
        self.assertEqual(run_lark.call_args_list[0].args[1], run_lark.call_args_list[1].args[1])
        ensure.assert_called_once_with(self.settings, self.open_id, force_verify=True)

    @patch("asgard_worker.ensure_user_authorization")
    @patch("asgard_worker.run_lark")
    def test_does_not_refresh_after_any_upstream_message_id(self, run_lark, ensure):
        run_lark.side_effect = [
            {"data": {"message_id": "om_version"}},
            UserAuthorizationError("need_user_authorization"),
        ]
        sender = send_asgard(
            self.settings, "om_source", "oc_chat", dispatch_user_open_id=self.open_id,
        )
        self.assertEqual(sender("查询车辆版本 VIN CCUFSD", role="vehicle_version")["message_id"], "om_version")
        with self.assertRaises(UserAuthorizationError):
            sender("fsdlog ...", role="baseline")
        ensure.assert_not_called()


class ReportBotTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "lark_cli": "lark-cli",
            "lark_profile": "cc-connect-chassis-user",
            "report_lark_profile": "cc-connect-chassis-report-bot",
            "asgard_open_id": ASGARD_ID,
        }

    def completed(self, payload, returncode=0):
        import json
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr="")

    @patch("asgard_worker.auth_status")
    def test_replica_secure_label_scopes_use_controlled_project_oauth(self, status):
        status.return_value = {"identities": {"user": {"scope": "offline_access"}}}
        with self.assertRaises(UserAuthorizationError) as raised:
            ensure_secure_label_authorization(dict(self.settings, auth_credential_replica_path="enabled.json"))
        message = str(raised.exception)
        self.assertIn("deployment/feishu_project_oauth.py --initiate", message)
        self.assertIn("--complete", message)
        self.assertNotIn("lark-cli auth login", message)

    @patch("asgard_worker.subprocess.run")
    def test_requires_available_bot_only_profile(self, run):
        run.return_value = self.completed({
            "identities": {
                "bot": {"available": True, "status": "valid"},
                "user": {"available": False, "status": "missing"},
            }
        })
        result = ensure_report_bot_authorization(self.settings)
        self.assertEqual(result["profile"], "cc-connect-chassis-report-bot")
        self.assertIn("cc-connect-chassis-report-bot", run.call_args.args[0])

    @patch("asgard_worker.subprocess.run")
    def test_rejects_report_profile_with_available_user(self, run):
        run.return_value = self.completed({
            "identities": {
                "bot": {"available": True},
                "user": {"available": True},
            }
        })
        with self.assertRaisesRegex(InputError, "bot-only"):
            ensure_report_bot_authorization(self.settings)

    @patch("asgard_worker.run_lark")
    def test_grants_chat_view_and_transfers_owner_as_bot(self, run_lark):
        run_lark.return_value = {"ok": True}
        grant_source_chat_view(self.settings, "https://li.feishu.cn/docx/docx_report", "oc_chat")
        transfer_report_owner(self.settings, "https://li.feishu.cn/docx/docx_report", "ou_requester")

        chat_args = run_lark.call_args_list[0].args[1]
        requester_args = run_lark.call_args_list[1].args[1]
        self.assertEqual(chat_args[chat_args.index("--as") + 1], "bot")
        self.assertEqual(chat_args[chat_args.index("--member-type") + 1], "openchat")
        self.assertEqual(chat_args[chat_args.index("--perm") + 1], "view")
        self.assertEqual(requester_args[:3], ["drive", "permission.members", "transfer_owner"])
        self.assertEqual(requester_args[requester_args.index("--old-owner-perm") + 1], "full_access")
        self.assertIn("--stay-put", requester_args)
        self.assertEqual(
            json.loads(requester_args[requester_args.index("--data") + 1]),
            {"member_id": "ou_requester", "member_type": "openid"},
        )
        for item in run_lark.call_args_list:
            self.assertEqual(item.kwargs["profile"], "cc-connect-chassis-report-bot")

    @patch("asgard_worker.run_lark")
    def test_restricts_collaborator_management_to_full_access(self, run_lark):
        run_lark.return_value = {"ok": True}
        restrict_collaborator_management(self.settings, "https://li.feishu.cn/docx/docx_report")
        args = run_lark.call_args.args[1]
        self.assertEqual(args[:3], ["drive", "permission.public", "patch"])
        self.assertEqual(args[args.index("--token") + 1], "docx_report")
        self.assertEqual(args[args.index("--type") + 1], "docx")
        self.assertEqual(json.loads(args[args.index("--data") + 1]), {"share_entity": "only_full_access"})
        self.assertIn("--yes", args)
        self.assertEqual(run_lark.call_args.kwargs["profile"], "cc-connect-chassis-report-bot")

    @patch("asgard_worker.run_lark")
    def test_resolves_and_applies_l3_with_requester_profile(self, run_lark):
        run_lark.side_effect = [
            {"data": {"items": [
                {"id": "1001", "name": "重要（L2）"},
                {"id": "1002", "name": "敏感（L3）"},
            ]}},
            {"ok": True},
        ]
        label = resolve_report_secure_label(self.settings)
        self.assertEqual(label, {"id": "1002", "name": "敏感（L3）"})
        apply_report_secure_label(self.settings, "https://li.feishu.cn/docx/docx_report", label)
        list_args = run_lark.call_args_list[0].args[1]
        update_args = run_lark.call_args_list[1].args[1]
        self.assertEqual(list_args[:2], ["drive", "+secure-label-list"])
        self.assertEqual(update_args[:2], ["drive", "+secure-label-update"])
        self.assertEqual(list_args[list_args.index("--as") + 1], "user")
        self.assertEqual(update_args[update_args.index("--as") + 1], "user")
        self.assertEqual(update_args[update_args.index("--label-id") + 1], "1002")
        for item in run_lark.call_args_list:
            self.assertEqual(item.kwargs["profile"], "cc-connect-chassis-user")

    @patch("asgard_worker.run_lark")
    def test_temporary_secure_label_operator_access_uses_bot_and_is_revocable(self, run_lark):
        run_lark.return_value = {"ok": True}
        report_url = "https://li.feishu.cn/docx/docx_report"
        grant_secure_label_operator(self.settings, report_url, "ou_developer")
        revoke_secure_label_operator(self.settings, report_url, "ou_developer")

        grant_args = run_lark.call_args_list[0].args[1]
        revoke_args = run_lark.call_args_list[1].args[1]
        self.assertEqual(grant_args[:2], ["drive", "+member-add"])
        self.assertEqual(grant_args[grant_args.index("--perm") + 1], "full_access")
        self.assertEqual(grant_args[grant_args.index("--member-id") + 1], "ou_developer")
        self.assertEqual(revoke_args[:2], ["api", "DELETE"])
        self.assertIn("/docx_report/members/ou_developer", revoke_args[2])
        self.assertEqual(
            json.loads(revoke_args[revoke_args.index("--params") + 1]),
            {"type": "docx", "member_type": "openid"},
        )
        for item in run_lark.call_args_list:
            self.assertEqual(item.kwargs["profile"], "cc-connect-chassis-report-bot")

    @patch("asgard_worker.revoke_secure_label_operator")
    @patch("asgard_worker.grant_secure_label_operator")
    @patch("asgard_worker.apply_report_secure_label")
    def test_l3_workflow_grants_and_revokes_access_when_owner_differs(
        self, apply_label, grant_operator, revoke_operator
    ):
        with tempfile.TemporaryDirectory() as temporary:
            record_path = Path(temporary) / "job.json"
            record = {
                "report_secure_label_required": True,
                "report_secure_label": {"id": "1002", "name": "敏感（L3）"},
                "dispatch_user_open_id": "ou_developer",
                "sender_id": "ou_requester",
            }
            permissions = {}
            configure_report_secure_label(
                self.settings, record_path, record,
                "https://li.feishu.cn/docx/docx_report", permissions,
            )

            grant_operator.assert_called_once_with(
                self.settings, "https://li.feishu.cn/docx/docx_report", "ou_developer"
            )
            apply_label.assert_called_once()
            revoke_operator.assert_called_once_with(
                self.settings, "https://li.feishu.cn/docx/docx_report", "ou_developer"
            )
            self.assertIn("secure_label", permissions)
            self.assertFalse(permissions["secure_label_operator"]["active"])

    @patch("asgard_worker.revoke_secure_label_operator")
    @patch("asgard_worker.grant_secure_label_operator")
    @patch("asgard_worker.apply_report_secure_label", side_effect=InputError("denied"))
    def test_l3_workflow_revokes_temporary_access_when_label_update_fails(
        self, apply_label, grant_operator, revoke_operator
    ):
        with tempfile.TemporaryDirectory() as temporary:
            record_path = Path(temporary) / "job.json"
            record = {
                "report_secure_label_required": True,
                "report_secure_label": {"id": "1002", "name": "敏感（L3）"},
                "dispatch_user_open_id": "ou_developer",
                "sender_id": "ou_requester",
            }
            permissions = {}
            with self.assertRaisesRegex(InputError, "denied"):
                configure_report_secure_label(
                    self.settings, record_path, record,
                    "https://li.feishu.cn/docx/docx_report", permissions,
                )
            grant_operator.assert_called_once()
            revoke_operator.assert_called_once()
            self.assertNotIn("secure_label", permissions)
            self.assertFalse(permissions["secure_label_operator"]["active"])

    @patch("asgard_worker.auth_status")
    def test_secure_label_scope_preflight_accepts_minimal_scopes(self, auth_status):
        auth_status.return_value = {
            "identities": {"user": {"scope": (
                "docs:secure_label:readonly docs:secure_label:write_only"
            )}}
        }
        ensure_secure_label_authorization(self.settings)
        auth_status.assert_called_once_with(self.settings, verify=False)

    @patch("asgard_worker.auth_status")
    def test_secure_label_scope_preflight_requests_only_incremental_scopes(self, auth_status):
        auth_status.return_value = {"identities": {"user": {"scope": "im:message"}}}
        with self.assertRaises(UserAuthorizationError) as raised:
            ensure_secure_label_authorization(self.settings)
        message = str(raised.exception)
        self.assertIn("docs:secure_label:readonly", message)
        self.assertIn("docs:secure_label:write_only", message)
        self.assertIn("--scope", message)
        self.assertNotIn("--recommend", message.split("：")[-1])

    def test_secure_label_requirement_is_not_retroactive(self):
        self.assertFalse(report_requires_secure_label({"report_url": "https://li.feishu.cn/docx/legacy"}))
        self.assertTrue(report_requires_secure_label({"report_secure_label_required": True}))

    @patch("asgard_worker.run_lark")
    def test_delivers_report_as_bot(self, run_lark):
        run_lark.return_value = {"ok": True, "identity":"bot", "data":{"message_id":"om_delivered"}}
        deliver(self.settings, {
            "source_message_id": "om_source",
            "source_thread_id": "om_thread",
            "source_chat_id": "oc_chat",
            "vin": VIN,
        }, "https://li.feishu.cn/docx/docx_report")
        args = run_lark.call_args.args[1]
        self.assertEqual(args[args.index("--as") + 1], "bot")
        self.assertEqual(run_lark.call_args.kwargs["profile"], "cc-connect-chassis-report-bot")
        self.assertEqual(args[:2], ["im", "+messages-reply"])
        self.assertEqual(args[args.index("--message-id") + 1], "om_thread")
        self.assertIn("--reply-in-thread", args)
        self.assertEqual(args[args.index("--msg-type") + 1], "post")
        self.assertNotIn("--markdown", args)
        post = json.loads(args[args.index("--content") + 1])["zh_cn"]
        self.assertEqual(post, {
            "title": "时延分析已完成",
            "content": [
                [{"tag": "text", "text": f"VIN：{VIN}"}],
                [{"tag": "a", "text": "查看飞书报告", "href": "https://li.feishu.cn/docx/docx_report"}],
            ],
        })
        self.assertNotIn("\\n", json.dumps(post, ensure_ascii=False))
        run_lark.assert_called_once()

    @patch("asgard_worker.run_lark")
    def test_report_notice_without_thread_keeps_source_chat_and_native_link(self, run_lark):
        run_lark.return_value = {"ok": True, "identity":"bot", "data":{"message_id":"om_delivered"}}
        url = "https://li.feishu.cn/docx/docx_report?from=notice&label=(report)"
        record = {"source_message_id": "om_source", "source_chat_id": "chat:oc_chat", "vin": VIN}
        deliver(self.settings, record, url)
        args = run_lark.call_args.args[1]
        self.assertEqual(args[:2], ["im", "+messages-send"])
        self.assertEqual(args[args.index("--chat-id") + 1], "oc_chat")
        self.assertNotIn("--reply-in-thread", args)
        self.assertEqual(args[args.index("--msg-type") + 1], "post")
        post = json.loads(args[args.index("--content") + 1])["zh_cn"]
        self.assertEqual(post["content"][1][0]["href"], url)
        self.assertNotIn("[查看飞书报告]", json.dumps(post, ensure_ascii=False))
        self.assertEqual(args[args.index("--as") + 1], "bot")
        self.assertEqual(run_lark.call_args.kwargs["profile"], "cc-connect-chassis-report-bot")

    @patch("asgard_worker.run_lark")
    def test_report_notice_preserves_existing_idempotency_key(self, run_lark):
        run_lark.return_value = {"ok": True, "identity":"bot", "data":{"message_id":"om_delivered"}}
        import hashlib
        record = {"source_message_id": "om_source", "source_thread_id": "om_thread", "vin": VIN}
        url = "https://li.feishu.cn/docx/docx_report"
        deliver(self.settings, record, url)
        deliver(self.settings, record, url)
        expected = hashlib.sha256(b"om_source:report").hexdigest()[:32]
        for call in run_lark.call_args_list:
            args = call.args[1]
            self.assertEqual(args[args.index("--idempotency-key") + 1], expected)

    def test_creates_report_in_bot_drive_folder(self):
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as runtime:
            settings = dict(self.settings, runtime_state_dir=runtime, report_parent_token="fld_report")
            response = {"data": {"document": {"url": "https://li.feishu.cn/docx/docx_report"}}}
            with patch("asgard_worker.build_report_document", return_value={"xml": "<doc><p>ok</p></doc>"}), \
                    patch("asgard_worker.run_lark", return_value=response) as run_lark:
                url = create_report(settings, {
                    "received": {
                        "baseline": {"sheet_url": "https://li.feishu.cn/sheets/base"},
                        "replacement": {"sheet_url": "https://li.feishu.cn/sheets/repl"},
                    }
                }, {})
            self.assertEqual(url, "https://li.feishu.cn/docx/docx_report")
            args = run_lark.call_args.args[1]
            self.assertEqual(args[args.index("--as") + 1], "bot")
            self.assertEqual(args[args.index("--parent-token") + 1], "fld_report")
            self.assertEqual(run_lark.call_args.kwargs["profile"], "cc-connect-chassis-report-bot")

if __name__ == "__main__":
    unittest.main()
