import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lark_auth_store as store
from lark_auth_lock import lark_auth_lock


def encrypted(value):
    # Test ciphertext is opaque and never contains actual credentials.
    return base64.b64encode(store.DPAPI_HEADER + bytes([value]) * 80).decode("ascii")


class CredentialReplicaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "project-home"
        self.path = self.home / ".lark-cli" / "user.dpapi.json"
        self.settings = {
            "project_isolation_required": True, "project_id": "cc-connect-chassis",
            "lark_home": str(self.home), "lark_profile": "cc-connect-chassis-user",
            "expected_lark_app_id": "cli_project", "expected_dispatch_open_id": "ou_dispatcher",
            "auth_credential_replica_path": str(self.path),
        }
        self.views = {"business": encrypted(1), "scheduled": encrypted(2)}
        self.view = "business"
        self.writes = []
        self.reader = patch("lark_auth_store._read_registry", side_effect=lambda settings: self.views.get(self.view))
        self.writer = patch("lark_auth_store._write_registry", side_effect=self.write_registry)
        self.reader.start()
        self.writer.start()
        self.addCleanup(self.reader.stop)
        self.addCleanup(self.writer.stop)

    def write_registry(self, settings, value):
        self.writes.append((self.view, value))
        self.views[self.view] = value

    def read_record(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_two_registry_views_share_refreshed_encrypted_replica(self):
        store.seed_replica(self.settings)
        self.view = "scheduled"
        with lark_auth_lock(self.settings):
            self.assertEqual(self.views["scheduled"], encrypted(1))
            self.views["scheduled"] = encrypted(3)
        migrated = json.loads(self.path.with_suffix(".json.registry.previous").read_text(encoding="utf-8"))
        self.assertEqual(migrated["ciphertext"], encrypted(2))
        self.assertEqual(self.read_record()["ciphertext"], encrypted(3))
        backup = json.loads(self.path.with_suffix(".json.previous").read_text(encoding="utf-8"))
        self.assertEqual(backup["ciphertext"], encrypted(1))
        self.view = "business"
        with lark_auth_lock(self.settings):
            self.assertEqual(self.views["business"], encrypted(3))

    def test_missing_canonical_never_bootstraps_from_current_registry(self):
        with self.assertRaises(store.LarkAuthStoreError):
            with lark_auth_lock(self.settings):
                self.fail("must not enter")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.writes, [])

    def test_existing_seed_is_never_overwritten(self):
        store.seed_replica(self.settings)
        original = self.path.read_bytes()
        self.views["business"] = encrypted(4)
        with self.assertRaises(store.LarkAuthStoreError):
            store.seed_replica(self.settings)
        self.assertEqual(self.path.read_bytes(), original)

    def test_malformed_ciphertext_or_checksum_never_overwrites_registry(self):
        store.seed_replica(self.settings)
        for changed in ({"ciphertext": "plaintext-is-forbidden"}, {"ciphertextSha256": "wrong"}):
            record = store._new_record(store._configuration(self.settings)[1], encrypted(1))
            record.update(changed)
            self.path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(store.LarkAuthStoreError):
                with lark_auth_lock(self.settings):
                    self.fail("must not enter")
        self.assertEqual(self.writes, [])

    def test_wrong_pinned_account_never_overwrites_registry(self):
        store.seed_replica(self.settings)
        for key, value in (("openId", "ou_other"), ("appId", "cli_other"), ("profile", "other-user")):
            record = store._new_record(store._configuration(self.settings)[1], encrypted(1))
            record[key] = value
            self.path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(store.LarkAuthStoreError):
                with lark_auth_lock(self.settings):
                    self.fail("must not enter")
        self.assertEqual(self.writes, [])

    def test_unchanged_registry_does_not_rewrite_canonical(self):
        store.seed_replica(self.settings)
        original = self.path.read_bytes()
        with patch("lark_auth_store._atomic_record", wraps=store._atomic_record) as atomic:
            with lark_auth_lock(self.settings):
                pass
        self.assertTrue(all(call.args[0] != self.path for call in atomic.call_args_list))
        self.assertEqual(self.path.read_bytes(), original)

    def test_explicit_commit_updates_guard_and_final_exit_is_idempotent(self):
        store.seed_replica(self.settings)
        with lark_auth_lock(self.settings) as guard:
            self.views["business"] = encrypted(4)
            self.assertTrue(guard.commit()["changed"])
            self.assertEqual(self.read_record()["ciphertext"], encrypted(4))
            with lark_auth_lock(self.settings) as nested:
                self.assertIs(nested, guard)
                self.views["business"] = encrypted(5)
                nested.commit()
        self.assertEqual(self.read_record()["ciphertext"], encrypted(5))
        self.assertFalse(store._journal_path(self.path).exists())

    def test_canonical_replace_failure_retains_prepared_ciphertext_for_recovery(self):
        store.seed_replica(self.settings)
        original_atomic = store._atomic_record

        def failing_atomic(path, record):
            if path == self.path:
                raise store.LarkAuthStoreError("simulated unavailable canonical file")
            original_atomic(path, record)

        with patch("lark_auth_store._atomic_record", side_effect=failing_atomic):
            with self.assertRaises(store.LarkAuthStoreError):
                with lark_auth_lock(self.settings) as guard:
                    self.views["business"] = encrypted(6)
                    guard.commit()
        self.assertEqual(self.read_record()["ciphertext"], encrypted(1))
        pending = json.loads(store._journal_path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(pending["state"], "prepared")
        self.assertEqual(pending["ciphertext"], encrypted(6))
        self.view = "scheduled"
        with patch("lark_auth_store._PROCESS_INSTANCE", "replacement-process"):
            with lark_auth_lock(self.settings):
                self.assertEqual(self.views["scheduled"], encrypted(6))
        self.assertEqual(self.read_record()["ciphertext"], encrypted(6))

    def test_prepared_write_failure_blocks_stale_import_in_other_process(self):
        store.seed_replica(self.settings)
        original_atomic = store._atomic_record

        def failing_atomic(path, record):
            if path == store._journal_path(self.path) and record.get("state") == "prepared":
                raise store.LarkAuthStoreError("simulated disk full")
            original_atomic(path, record)

        with patch("lark_auth_store._atomic_record", side_effect=failing_atomic):
            with self.assertRaises(store.LarkAuthStoreError):
                with lark_auth_lock(self.settings):
                    self.views["business"] = encrypted(7)
        self.view = "scheduled"
        writes_before = len(self.writes)
        with patch("lark_auth_store._PROCESS_INSTANCE", "replacement-process"):
            with self.assertRaises(store.LarkAuthStoreError):
                with lark_auth_lock(self.settings):
                    self.fail("unfinished refresh must fail closed")
        self.assertEqual(len(self.writes), writes_before)
        self.assertEqual(self.views["business"], encrypted(7))
        self.view = "business"
        with lark_auth_lock(self.settings):
            pass
        self.assertEqual(self.read_record()["ciphertext"], encrypted(7))

    def test_kill_between_native_refresh_and_export_preserves_native_credentials(self):
        store.seed_replica(self.settings)
        store.import_replica(self.settings)  # active journal persisted before CLI
        self.views["business"] = encrypted(8)  # native refresh finished; process dies
        self.view = "scheduled"
        writes_before = len(self.writes)
        with patch("lark_auth_store._PROCESS_INSTANCE", "replacement-process"):
            with self.assertRaises(store.LarkAuthStoreError):
                with lark_auth_lock(self.settings):
                    self.fail("crash window must not import stale canonical")
        self.assertEqual(len(self.writes), writes_before)
        self.assertEqual(self.views["business"], encrypted(8))
        self.assertEqual(self.read_record()["ciphertext"], encrypted(1))

    def test_invalid_refreshed_ciphertext_cannot_replace_canonical(self):
        store.seed_replica(self.settings)
        original = self.path.read_bytes()
        with self.assertRaises(store.LarkAuthStoreError):
            with lark_auth_lock(self.settings):
                self.views["business"] = "invalid-data"
        self.assertEqual(self.path.read_bytes(), original)

    def test_missing_registry_preserves_canonical(self):
        store.seed_replica(self.settings)
        original = self.path.read_bytes()
        with lark_auth_lock(self.settings):
            self.views["business"] = None
        self.assertEqual(self.path.read_bytes(), original)

    def test_refresh_on_business_error_is_atomically_preserved(self):
        store.seed_replica(self.settings)
        with self.assertRaisesRegex(RuntimeError, "business failed"):
            with lark_auth_lock(self.settings):
                self.views["business"] = encrypted(5)
                raise RuntimeError("business failed")
        self.assertEqual(self.read_record()["ciphertext"], encrypted(5))
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_reentrant_lock_imports_and_exports_once(self):
        store.seed_replica(self.settings)
        with patch("lark_auth_lock.import_replica", wraps=store.import_replica) as importer, \
                patch("lark_auth_lock.export_replica", wraps=store.export_replica) as exporter:
            with lark_auth_lock(self.settings):
                with lark_auth_lock(self.settings):
                    pass
        self.assertEqual(importer.call_count, 1)
        self.assertEqual(exporter.call_count, 1)

    def test_path_outside_project_home_is_rejected(self):
        settings = dict(self.settings, auth_credential_replica_path=str(Path(self.temporary.name) / "other.json"))
        with self.assertRaises(store.LarkAuthStoreError):
            store.seed_replica(settings)
        self.assertFalse(self.path.exists())

    def test_path_symlink_escape_is_rejected(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        self.home.mkdir()
        try:
            (self.home / ".lark-cli").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlink requires local Windows privilege")
        with self.assertRaises(store.LarkAuthStoreError):
            store.seed_replica(self.settings)

    def test_resolved_symlink_escape_is_rejected_on_all_platforms(self):
        outside = Path(self.temporary.name) / "outside"
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == self.home / ".lark-cli":
                return outside
            if path == self.path:
                return outside / "user.dpapi.json"
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve):
            with self.assertRaises(store.LarkAuthStoreError):
                store.seed_replica(self.settings)

    def test_invalid_dpapi_data_header_is_rejected(self):
        self.views["business"] = base64.b64encode(b"not-dpapi" * 12).decode("ascii")
        with self.assertRaises(store.LarkAuthStoreError):
            store.seed_replica(self.settings)
        self.assertFalse(self.path.exists())

    def test_no_opt_in_keeps_original_lock_behavior(self):
        settings = dict(self.settings)
        settings.pop("auth_credential_replica_path")
        with lark_auth_lock(settings) as guard:
            self.assertEqual(guard.commit(), {"changed": False})
            self.views["business"] = encrypted(9)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.writes, [])

    def test_canonical_change_outside_lock_is_not_overwritten(self):
        store.seed_replica(self.settings)
        with self.assertRaises(store.LarkAuthStoreError):
            with lark_auth_lock(self.settings):
                changed = store._new_record(store._configuration(self.settings)[1], encrypted(8))
                self.path.write_text(json.dumps(changed), encoding="utf-8")
                self.views["business"] = encrypted(6)
        self.assertEqual(self.read_record()["ciphertext"], encrypted(8))


if __name__ == "__main__":
    unittest.main()
