import multiprocessing
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "deployment"))

from lark_auth_lock import LarkAuthLockTimeout, _lock_path, lark_auth_lock


def _hold_auth_lock(settings, ready):
    with lark_auth_lock(settings, timeout=5):
        ready.set()
        threading.Event().wait(60)


class AuthCredentialLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = {"lark_home": self.temporary.name, "expected_lark_app_id": "cli_project"}

    def test_same_thread_can_reenter_without_relocking_os_handle(self):
        with lark_auth_lock(self.settings):
            with lark_auth_lock(self.settings, timeout=0):
                pass

    def test_other_thread_waits_instead_of_bypassing_process_lock(self):
        errors = []

        def contender():
            try:
                with lark_auth_lock(self.settings, timeout=0.05):
                    errors.append("unexpected acquisition")
            except LarkAuthLockTimeout:
                pass
            except Exception as exc:
                errors.append(exc)

        with lark_auth_lock(self.settings):
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_same_home_and_app_serialize_profiles_and_normalize_paths(self):
        alias = dict(self.settings, lark_home=str(Path(self.temporary.name) / "unused" / ".."), lark_profile="other")
        self.assertEqual(_lock_path(self.settings), _lock_path(alias))
        self.assertNotEqual(_lock_path(self.settings), _lock_path(dict(self.settings, expected_lark_app_id="cli_other")))

    def test_explicit_project_lock_path_does_not_depend_on_temp_environment(self):
        with patch("lark_auth_lock.tempfile.gettempdir", return_value="first-temp"):
            first = _lock_path(self.settings)
        with patch("lark_auth_lock.tempfile.gettempdir", return_value="second-temp"):
            second = _lock_path(self.settings)
        self.assertEqual(first, second)
        self.assertEqual(first.parent, Path(self.temporary.name) / ".lark-cli" / "locks" / "cc-connect")

    def test_crashed_process_releases_os_lock(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        process = context.Process(target=_hold_auth_lock, args=(self.settings, ready))
        process.start()
        try:
            self.assertTrue(ready.wait(10), "child did not acquire its lock")
            with self.assertRaises(LarkAuthLockTimeout):
                with lark_auth_lock(self.settings, timeout=0.05):
                    pass
        finally:
            process.terminate()
            process.join(10)
            if process.is_alive():
                process.kill()
                process.join(10)
        with lark_auth_lock(self.settings, timeout=2):
            pass


if __name__ == "__main__":
    unittest.main()
