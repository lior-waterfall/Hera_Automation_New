import base64
import copy
import io
import sqlite3
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

import requests

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import load_test  # noqa: E402


class ProxmoxTeardownTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            {
                "client_ip": f"192.0.2.{index}",
                "asset_ip": f"198.51.100.{index}",
                "email": f"test{index}@example.com",
                "asset_display_name": f"Asset {index}",
                "status": "created",
            }
            for index in range(1, 4)
        ]
        self.inventory = {
            "setup_runs": [{"source": "proxmox", "assignments": self.records}]
        }
        self.settings = {"proxmox_vms_by_ip": {}}
        self.saved = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.mocks = {}
        defaults = {
            "_load_ip_inventory": self.inventory,
            "_get_target_ips": ([], []),
            "_stop_proxmox_tauri_windows": None,
            "erase_web_user_and_asset": None,
            "delete_tx_certificate": True,
            "tx_upload_configured": True,
            "_execute_proxmox_interactive_command": (
                "ACCOUNT DELETE SUCCESSFUL", "", 0, False
            ),
            "_desktop_tauri_account_present": (False, "PROFILE=test"),
            "run_client_close": None,
        }
        for name, value in defaults.items():
            self.mocks[name] = self.stack.enter_context(
                patch.object(load_test, name, return_value=value)
            )
        self.stack.enter_context(
            patch.object(
                load_test, "_save_ip_inventory",
                side_effect=lambda inventory: self.saved.append(copy.deepcopy(inventory)),
            )
        )
        self.output = io.StringIO()

    def run_teardown(self, max_clients=None):
        with redirect_stdout(self.output):
            result = load_test._teardown_recorded_proxmox_pairs(
                self.settings, max_clients
            )
        self.assertTrue(result)

    def test_agent_exception_is_saved_and_next_vm_is_processed(self):
        self.mocks["_execute_proxmox_interactive_command"].side_effect = [
            requests.HTTPError("500: cannot find the path specified"),
            ("ACCOUNT DELETE SUCCESSFUL", "", 0, False),
            ("ACCOUNT DELETE SUCCESSFUL", "", 0, False),
        ]
        self.run_teardown()
        self.assertEqual(
            [record["status"] for record in self.records],
            ["teardown_partial", "cleaned", "cleaned"],
        )
        self.assertIn("cannot find the path", self.records[0]["cleanup_errors"][0])
        self.assertEqual(len(self.saved), 3)
        self.assertEqual(
            self.saved[0]["setup_runs"][0]["assignments"][0]["status"],
            "teardown_partial",
        )
        self.mocks["run_client_close"].assert_called_once_with(
            client_ips_override=["192.0.2.1", "192.0.2.2", "192.0.2.3"]
        )
        self.assertIn("Manual cleanup required", self.output.getvalue())
        self.assertIn("192.0.2.1", self.output.getvalue())
        self.assertIn("test1@example.com", self.output.getvalue())
        self.assertIn("2 fully cleaned; 1 need attention", self.output.getvalue())
        self.assertEqual(
            self.records[0]["cleanup_results"],
            {"web": True, "tx": True, "local": False},
        )
        self.assertIn("Web user/asset: OK", self.output.getvalue())
        self.assertIn("Local Tauri account: FAILED/UNCONFIRMED", self.output.getvalue())

    def test_timeout_failure_and_tx_failure_are_reported(self):
        self.mocks["_execute_proxmox_interactive_command"].side_effect = [
            ("", "", -1, True),
            ("", "Agent unavailable", -1, False),
            ("ACCOUNT DELETE SUCCESSFUL", "", 0, False),
        ]
        self.mocks["delete_tx_certificate"].side_effect = [True, True, False]
        self.run_teardown()
        self.assertTrue(all(r["status"] == "teardown_partial" for r in self.records))
        self.assertIn("Timed out", self.records[0]["cleanup_errors"][0])
        self.assertIn("Agent unavailable", self.records[1]["cleanup_errors"][0])
        self.assertIn("TX certificate", self.records[2]["cleanup_errors"][0])

    def test_server_error_and_stop_exception_do_not_abort(self):
        self.mocks["_stop_proxmox_tauri_windows"].side_effect = [
            RuntimeError("Agent unavailable"), None, None
        ]
        self.mocks["erase_web_user_and_asset"].side_effect = [
            None, RuntimeError("API unavailable"), None
        ]
        self.run_teardown()
        self.assertEqual(
            [r["status"] for r in self.records],
            ["teardown_partial", "teardown_partial", "cleaned"],
        )
        self.assertEqual(self.mocks["delete_tx_certificate"].call_count, 3)

    def test_same_vm_with_partial_account_counts_once(self):
        self.records[1]["client_ip"] = self.records[0]["client_ip"]
        self.mocks["_execute_proxmox_interactive_command"].side_effect = [
            ("ACCOUNT DELETE SUCCESSFUL", "", 0, False),
            ("", "Delete failed", 1, False),
            ("ACCOUNT DELETE SUCCESSFUL", "", 0, False),
        ]
        self.run_teardown()
        self.assertIn("1 fully cleaned; 1 need attention", self.output.getvalue())

    def test_limit_and_success(self):
        self.run_teardown(max_clients=1)
        self.assertEqual(
            [r["status"] for r in self.records], ["cleaned", "created", "created"]
        )
        self.assertIn("1 fully cleaned; 0 need attention", self.output.getvalue())

    def test_database_check_failure_is_not_cleaned(self):
        self.mocks["_desktop_tauri_account_present"].side_effect = RuntimeError(
            "Could not read desktop database"
        )
        self.run_teardown(max_clients=1)
        self.assertEqual(self.records[0]["status"], "teardown_partial")

    def test_partial_assignment_is_retried_and_errors_cleared(self):
        self.records[0]["status"] = "teardown_partial"
        self.records[0]["cleanup_errors"] = ["Previous agent failure"]
        self.run_teardown(max_clients=1)
        self.assertEqual(self.records[0]["status"], "cleaned")
        self.assertEqual(self.records[0]["cleanup_errors"], [])

    def test_account_still_present_requires_manual_cleanup(self):
        self.mocks["_desktop_tauri_account_present"].return_value = (
            True, "PROFILE=test"
        )
        self.run_teardown(max_clients=1)
        self.assertEqual(self.records[0]["status"], "teardown_partial")
        self.assertIn("still present", self.records[0]["cleanup_errors"][0])


class DesktopAccountVerificationTests(unittest.TestCase):
    def test_strict_verification_reads_real_database_export(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE accounts (user_registered_email TEXT)")
            connection.execute(
                "INSERT INTO accounts VALUES (?)", ("test@example.com",)
            )
            encoded = base64.b64encode(connection.serialize()).decode("ascii")
        finally:
            connection.close()
        with patch.object(
            load_test, "_execute_proxmox_interactive_command",
            return_value=(f"PROFILE=test\n{encoded}", "", 0, False),
        ):
            present, _detail = load_test._desktop_tauri_account_present(
                {}, "192.0.2.1", "test@example.com", strict=True
            )
            absent, _detail = load_test._desktop_tauri_account_present(
                {}, "192.0.2.1", "other@example.com", strict=True
            )
        self.assertTrue(present)
        self.assertFalse(absent)

    def test_strict_verification_rejects_unavailable_database(self):
        cases = [
            ("", "", -1, True),
            ("PROFILE=systemprofile\nACCOUNT_WRONG_PROFILE", "", 3, False),
            ("", "Agent unavailable", -1, False),
            ("PROFILE=test", "", 0, False),
            ("PROFILE=test\ninvalid sqlite export", "", 0, False),
        ]
        for result in cases:
            with self.subTest(result=result), patch.object(
                load_test, "_execute_proxmox_interactive_command",
                return_value=result,
            ):
                with self.assertRaises(RuntimeError):
                    load_test._desktop_tauri_account_present(
                        {}, "192.0.2.1", "test@example.com", strict=True
                    )

    def test_missing_database_confirms_account_absent(self):
        with patch.object(
            load_test, "_execute_proxmox_interactive_command",
            return_value=("PROFILE=test\nACCOUNT_MISSING", "", 2, False),
        ):
            present, _detail = load_test._desktop_tauri_account_present(
                {}, "192.0.2.1", "test@example.com", strict=True
            )
        self.assertFalse(present)

    def test_default_verification_preserves_existing_callers(self):
        with patch.object(
            load_test, "_execute_proxmox_interactive_command",
            return_value=("", "", -1, True),
        ):
            present, detail = load_test._desktop_tauri_account_present(
                {}, "192.0.2.1", "test@example.com"
            )
        self.assertFalse(present)
        self.assertIn("Timed out", detail)


if __name__ == "__main__":
    unittest.main()
