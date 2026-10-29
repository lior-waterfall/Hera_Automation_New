import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import load_test  # noqa: E402

# Output captured from a real failed Edge launch; PowerShell wraps every line in NativeCommandError.
EDGE_NOISE = r"""powershell.exe : Edge did not stay open on the asset.

At C:\ProgramData\HeraLoadTest\669fafd2f31c4a08b3042c9b0b73bc9d\task.ps1:26 char:9
+         & $program @argv 1> 'C:\ProgramData\HeraLoadTest\669fafd2f31c ...
+         ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : NotSpecified: (Edge did not stay open on the asset.
:String) [], RemoteException
    + FullyQualifiedErrorId : NativeCommandError

powershell.exe : At line:10 char:64
powershell.exe : + ... n SilentlyContinue)) { throw 'Edge did not stay open on the asset.' }
powershell.exe :     + CategoryInfo          : OperationStopped: (Edge did not stay open on the
asset.:String) [], Run
powershell.exe :    timeException
"""

AGENT_DOWN = (
    "Proxmox guest-agent command failed: 500 Server Error: QEMU guest agent is not "
    "running for url: https://10.101.28.213:8006/api2/json/nodes/pve5/qemu/5024/agent/exec-status?pid=14768"
)


class DescribeFailureTests(unittest.TestCase):
    def test_edge_noise_becomes_one_plain_sentence(self):
        message, detail = load_test._describe_failure("Asset 10.101.28.156", EDGE_NOISE)
        self.assertEqual(
            message,
            "Asset 10.101.28.156: Edge did not open on the asset. "
            "Check that the asset VM is responsive.",
        )
        self.assertNotIn("CategoryInfo", message)
        self.assertIn("NativeCommandError", detail)

    def test_guest_agent_down_is_explained_without_urls(self):
        message, _detail = load_test._describe_failure("Asset 10.101.28.156", AGENT_DOWN)
        self.assertIn("guest agent stopped responding", message)
        self.assertNotIn("https://", message)

    def test_known_failures_have_actionable_text(self):
        cases = {
            "500 Server Error: VM 8010 is not running for url: x": "powered off",
            "Configuration file 'nodes/pve1/qemu-server/8011.conf' does not exist": "no longer exists",
            "No interactive Windows user is logged in.": "nobody is logged in",
            "TpmMtlsProxy did not listen on 127.0.0.1:5555.": "mTLS proxy",
            "Existing Tauri or mTLS proxy processes could not be stopped.": "still running",
            "Timed out.": "did not respond in time",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                message, _detail = load_test._describe_failure("Client 1", raw)
                self.assertIn(expected, message)

    def test_unknown_failure_is_short_and_free_of_wrapper_lines(self):
        raw = "powershell.exe : Something odd\nAt C:\\x\\task.ps1:26 char:9\n+ CategoryInfo : X\n" + "y" * 500
        message, _detail = load_test._describe_failure("Client 1", raw)
        self.assertTrue(message.startswith("Client 1: unexpected problem (Something odd"))
        self.assertNotIn("task.ps1", message)
        self.assertLess(len(message), 300)


class OpenAssetRenderingTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "asset_rendering_mode": "low",
            "low_rendering_url": "https://time.is/",
            "high_rendering_url": "https://www.msn.com/",
        }

    def run_with(self, result, user="HOST\\user"):
        with patch.object(load_test, "_asset_session_user", return_value=user), patch.object(
            load_test, "_execute_proxmox_interactive_command", return_value=result
        ):
            return load_test._open_asset_rendering(self.settings, "10.0.0.5")

    def test_success_returns_user(self):
        self.assertEqual(
            self.run_with(("ASSET_RENDERING_STARTED\n", "", 0, False)), "HOST\\user"
        )

    def test_marker_error_is_reported_without_powershell_noise(self):
        with self.assertRaises(load_test.StepError) as caught:
            self.run_with(("ASSET_RENDERING_ERROR: Edge did not stay open on the asset.\n", EDGE_NOISE, 1, False))
        self.assertEqual(
            str(caught.exception),
            "Asset 10.0.0.5: Edge did not open on the asset. Check that the asset VM is responsive.",
        )

    def test_timeout_is_reported_as_no_response(self):
        with self.assertRaises(load_test.StepError) as caught:
            self.run_with(("", "", -1, True))
        self.assertIn("did not respond in time", str(caught.exception))

    def test_missing_session_is_explained(self):
        with self.assertRaises(load_test.StepError) as caught:
            self.run_with(("", "", 0, False), user="")
        self.assertIn("did not open a remote session", str(caught.exception))


class LoginRetryScopeTests(unittest.TestCase):
    def records(self, *states):
        result = []
        for index, state in enumerate(states, 1):
            record = {"counter": index}
            if state == "ok":
                record.update(login_status="connected", asset_render_status="low")
            elif state == "render_failed":
                record.update(login_status="connected", asset_render_status="failed")
            elif state == "login_failed":
                record.update(login_status="failed")
            result.append(record)
        return result

    def test_login_state_classification(self):
        states = [
            load_test._login_state(r, "low")
            for r in self.records("ok", "render_failed", "login_failed", "new")
        ]
        self.assertEqual(states, ["ok", "failed", "failed", "not_tried"])
        self.assertEqual(load_test._login_state({"login_status": "connected"}, "off"), "ok")

    def ask(self, records, answers):
        output = io.StringIO()
        with patch("builtins.input", side_effect=answers), redirect_stdout(output):
            return load_test._ask_login_retry_scope(records, "low"), output.getvalue()

    def test_mixed_results_default_to_a_fresh_count_run(self):
        result, output = self.ask(self.records("ok", "ok", "render_failed", "login_failed"), [""])
        self.assertFalse(result)
        self.assertIn("2 logged in and 2 failed", output)
        self.assertIn("restarted since", output)

    def test_user_can_choose_to_reuse_results_and_retry_failed(self):
        result, _output = self.ask(self.records("ok", "login_failed"), ["2"])
        self.assertTrue(result)

    def test_invalid_answer_is_asked_again(self):
        result, output = self.ask(self.records("ok", "login_failed"), ["x", "1"])
        self.assertFalse(result)
        self.assertIn("Invalid input", output)

    def test_no_question_when_nothing_to_choose(self):
        for states in (("ok", "ok"), ("login_failed", "new"), ("new", "new")):
            with self.subTest(states=states), patch("builtins.input", side_effect=AssertionError):
                self.assertFalse(
                    load_test._ask_login_retry_scope(self.records(*states), "low")
                )


class RunClientLoginRetryOnlyTests(unittest.TestCase):
    def test_only_incomplete_vms_are_processed_and_results_saved(self):
        records = [
            {"counter": 1, "client_ip": "c1", "asset_ip": "a1", "email": "e1", "status": "created",
             "login_status": "connected", "asset_render_status": "low"},
            {"counter": 2, "client_ip": "c2", "asset_ip": "a2", "email": "e2", "status": "created",
             "login_status": "connected", "asset_render_status": "failed"},
            {"counter": 3, "client_ip": "c3", "asset_ip": "a3", "email": "e3", "status": "created",
             "login_status": "failed", "login_error": "old"},
        ]
        inventory = {
            "source": "proxmox", "clients": [], "assets": [],
            "setup_runs": [{"source": "proxmox", "started_at": "t", "assignments": records}],
        }
        settings = {
            "ip_source": "proxmox", "asset_rendering_mode": "low",
            "hera_web_url": "", "account_pass": "p", "asset_user": "u",
            "asset_encoded_pass": "p", "asset_name": "Asset", "api_port": 1, "mtls_port": 2,
        }
        rendered, logged_in = [], []

        def fake_render(_settings, asset_ip):
            rendered.append(asset_ip)

        def fake_login(_settings, client_ip, _command):
            logged_in.append(client_ip)
            return "", "", 0, False

        with patch.object(load_test, "_read_load_test_settings", return_value=settings), patch.object(
            load_test, "_load_ip_inventory", return_value=inventory
        ), patch.object(load_test, "_save_ip_inventory"), patch.object(
            load_test, "_open_asset_rendering", side_effect=fake_render
        ), patch.object(
            load_test, "_execute_proxmox_login_with_proxy_retry", side_effect=fake_login
        ), patch.object(
            load_test, "_desktop_tauri_account_site_address", return_value="1.2.3.4"
        ), patch.object(load_test, "_apply_client_credentials_to_asset"), patch.object(
            load_test, "_disable_asset_setup_screen"
        ), patch.object(load_test, "_asset_session_user", return_value="H\\u"):
            with redirect_stdout(io.StringIO()):
                load_test.run_client_login(max_clients=1, retry_failed_only=True)

        self.assertEqual(logged_in, ["c3"])
        self.assertEqual(sorted(rendered), ["a2", "a3"])
        self.assertEqual(
            [r["login_status"] for r in records], ["connected"] * 3
        )
        self.assertEqual([r["asset_render_status"] for r in records], ["low"] * 3)


if __name__ == "__main__":
    unittest.main()
