import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import requests

import load_test


def response(status_code=200, text="", json_data=None):
    fake = MagicMock()
    fake.status_code = status_code
    fake.text = text
    fake.json.return_value = json_data or {}
    if status_code >= 400:
        fake.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code} Server Error: {text}", response=fake
        )
    return fake


FILE_BLOCKED = "Agent error: Command guest-file-open has been disabled: the command is not allowed"
AGENT_DOWN = "QEMU guest agent is not running"


class QaReportedMessagesTests(unittest.TestCase):
    """The failures QA reported, as the operator now sees them."""

    def describe(self, text):
        return load_test._describe_failure("VM", text)[0]

    def test_clr_failure_is_not_blamed_on_passwords(self):
        old_text = (
            "None of the configured passwords can log on to an enabled Windows account. "
            "Starting the CLR failed with HRESULT 80004005."
        )
        message = self.describe(old_text)
        self.assertIn("PowerShell could not start", message)
        self.assertNotIn("passwords", message)

    def test_wrong_passwords_point_to_the_env_file(self):
        message = self.describe("None of the configured passwords can log on to an enabled Windows account.")
        self.assertIn("CLIENT_PASS", message)

    def test_blocked_file_transfer_is_explained(self):
        message = self.describe(f"500 Server Error: {FILE_BLOCKED} for url: https://10.1.1.1:8006/x/file-write")
        self.assertIn("does not allow file transfers", message)
        self.assertNotIn("https://", message)

    def test_hera_server_error_during_cleanup_is_explained(self):
        error = 'Hera API cleanup: DELETE /api/connections/d6ab failed with status 500: {"detail":"Internal Server Error"}'
        message = load_test._friendly_cleanup_error(error)
        self.assertEqual(message.split(":")[0], "HERA server")
        self.assertIn("returned an error", message)
        self.assertNotIn("/api/connections", message)

    def test_cleanup_errors_without_a_known_label_still_read_well(self):
        self.assertIn("still in the HERA client app", load_test._friendly_cleanup_error(
            "Local Tauri cleanup: Account still present in the desktop Tauri database. PROFILE=x"))

    def test_exception_with_response_text_is_translated(self):
        exc = requests.HTTPError("500 Server Error", response=response(500, FILE_BLOCKED))
        message, detail = load_test._describe_exception("Client 1", exc)
        self.assertIn("does not allow file transfers", message)
        self.assertIn("guest-file-open", detail)


class WriteFileRetryTests(unittest.TestCase):
    SETTINGS = {
        "proxmox_ip": "p", "proxmox_token_id": "t", "proxmox_token_secret": "s",
        "proxmox_vms_by_ip": {"c": {"node": "pve1", "vmid": 1}},
    }

    def write(self, responses):
        with patch.object(load_test.requests, "post", side_effect=responses) as post, \
                patch.object(load_test.time, "sleep"):
            try:
                load_test._write_proxmox_file(self.SETTINGS, "c", r"C:\x\y.ps1", "data")
                return post.call_count, None
            except requests.HTTPError as exc:
                return post.call_count, exc

    def test_blocked_command_that_clears_up_is_retried(self):
        calls, error = self.write([response(500, FILE_BLOCKED), response(500, FILE_BLOCKED), response(200)])
        self.assertEqual((calls, error), (3, None))

    def test_agent_not_running_is_retried(self):
        calls, error = self.write([response(500, AGENT_DOWN), response(200)])
        self.assertEqual((calls, error), (2, None))

    def test_timeouts_are_retried(self):
        calls, error = self.write([requests.Timeout("slow"), response(200)])
        self.assertEqual((calls, error), (2, None))

    def test_gives_up_after_four_attempts(self):
        calls, error = self.write([response(500, FILE_BLOCKED)] * 4)
        self.assertEqual(calls, 4)
        self.assertIsInstance(error, requests.HTTPError)

    def test_other_errors_are_not_retried(self):
        calls, error = self.write([response(403, "permission denied")])
        self.assertEqual(calls, 1)
        self.assertIsInstance(error, requests.HTTPError)


class CommandRetryTests(unittest.TestCase):
    def test_agent_hiccup_while_polling_is_retried(self):
        settings = {
            "proxmox_ip": "p", "proxmox_token_id": "t", "proxmox_token_secret": "s",
            "proxmox_vms_by_ip": {"c": {"node": "pve1", "vmid": 1}},
        }
        sequence = [
            response(200, json_data={"data": {"pid": 7}}),
            response(500, AGENT_DOWN),
            response(200, json_data={"data": {"exited": 1, "exitcode": 0, "out-data": "done"}}),
        ]
        with patch.object(load_test.requests, "request", side_effect=sequence), \
                patch.object(load_test.time, "sleep"):
            result = load_test._execute_proxmox_command(settings, "c", ["cmd.exe"], 30)
        self.assertEqual(result, ("done", "", 0, False))


class WindowsLogonCheckTests(unittest.TestCase):
    def resolve(self, probe_results):
        results = iter(probe_results)

        def fake_command(_settings, _ip, command, _timeout=None):
            if command[0] == "powershell.exe":
                return next(results)
            return "", "", 0, False

        settings = {"client_pass": "pw"}
        with patch.object(load_test, "_execute_proxmox_command", side_effect=fake_command), \
                patch.object(load_test, "_write_proxmox_file"), \
                patch.object(load_test.time, "sleep"):
            return load_test._resolve_windows_logon(settings, "c")

    CLR = ("", "Starting the CLR failed with HRESULT 80004005.", 1, False)

    def test_powershell_crash_then_success_is_retried(self):
        self.assertEqual(self.resolve([self.CLR, ("MATCH admin 0\n", "", 0, False)]), ("admin", "pw"))

    def test_repeated_powershell_crash_reports_a_vm_problem(self):
        with self.assertRaises(RuntimeError) as caught:
            self.resolve([self.CLR, self.CLR])
        text = str(caught.exception)
        self.assertIn("PowerShell could not run the password check", text)
        self.assertNotIn("None of the configured passwords", text)
        self.assertIn("PowerShell could not start", load_test._describe_failure("VM", text)[0])

    def test_real_password_mismatch_is_still_reported_as_such(self):
        with self.assertRaises(RuntimeError) as caught:
            self.resolve([("NOMATCH\n", "", 1, False)])
        self.assertIn("None of the configured passwords", str(caught.exception))


class DiscoveryRetryTests(unittest.TestCase):
    VMS = [{"type": "qemu", "status": "running", "node": "pve1", "vmid": 10, "name": "win11-hera-01"}]
    GOOD = {"result": [{"name": "eth0", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "10.0.0.5"}]}]}

    def discover(self, interface_answers):
        answers = iter(interface_answers)
        settings = {
            "proxmox_ip": "p", "proxmox_token_id": "t", "proxmox_token_secret": "s",
            "proxmox_ca_cert": "",
        }

        def fake_get(url, **_kwargs):
            fake = MagicMock()
            fake.json.return_value = {"data": self.VMS if url.endswith("type=vm") else next(answers)}
            return fake

        output = io.StringIO()
        with patch.object(load_test.requests, "get", side_effect=fake_get), \
                patch.object(load_test.time, "sleep"), redirect_stdout(output):
            return load_test._get_proxmox_ips(settings), output.getvalue()

    def test_agent_that_answers_late_is_still_discovered(self):
        (clients, _assets), _output = self.discover([{"result": None}, {"result": None}, self.GOOD])
        self.assertEqual(clients, ["10.0.0.5"])

    def test_vm_that_never_answers_is_listed_in_a_warning(self):
        (clients, _assets), output = self.discover([{"result": None}] * 3)
        self.assertEqual(clients, [])
        self.assertIn("WARNING: 1 VM(s) were left out", output)
        self.assertIn("win11-hera-01", output.split("WARNING")[1])
        self.assertNotIn("Skipping", output)


class LoginRunResilienceTests(unittest.TestCase):
    def run_login(self, inventory, login_side_effect, **kwargs):
        settings = {
            "ip_source": "proxmox", "asset_rendering_mode": "off",
            "hera_web_url": "", "account_pass": "p", "asset_user": "u",
            "asset_encoded_pass": "p", "asset_name": "Asset", "api_port": 1, "mtls_port": 2,
        }
        with patch.object(load_test, "_read_load_test_settings", return_value=settings), \
                patch.object(load_test, "_load_ip_inventory", return_value=inventory), \
                patch.object(load_test, "_save_ip_inventory"), \
                patch.object(load_test, "_execute_proxmox_login_with_proxy_retry",
                             side_effect=login_side_effect), \
                patch.object(load_test, "_desktop_tauri_account_site_address", return_value="1.2.3.4"), \
                patch.object(load_test, "_apply_client_credentials_to_asset"), \
                patch.object(load_test, "_asset_session_user", return_value="H\\u"), \
                patch.object(load_test.time, "sleep"), redirect_stdout(io.StringIO()) as output:
            load_test.run_client_login(**kwargs)
        return output.getvalue()

    @staticmethod
    def record(counter, status="created", **extra):
        return {"counter": counter, "client_ip": f"c{counter}", "asset_ip": f"a{counter}",
                "email": f"e{counter}", "status": status, **extra}

    def test_one_vm_error_does_not_stop_the_other_vms(self):
        records = [self.record(1), self.record(2)]
        inventory = {"source": "proxmox", "setup_runs": [{"source": "proxmox", "assignments": records}]}

        def login(_settings, client_ip, _command):
            if client_ip == "c1":
                raise requests.HTTPError("500", response=response(500, FILE_BLOCKED))
            return "", "", 0, False

        output = self.run_login(inventory, login, retry_failed_only=False)
        self.assertEqual([r["login_status"] for r in records], ["failed", "connected"])
        self.assertIn("does not allow file transfers", records[0]["login_error"])
        self.assertIn("guest-file-open", records[0]["login_detail"])
        self.assertIn("What needs attention", output)
        self.assertNotIn("Traceback", output)

    def test_accounts_from_every_setup_run_are_logged_in(self):
        first, second = self.record(1), self.record(2)
        inventory = {"source": "proxmox", "setup_runs": [
            {"source": "proxmox", "assignments": [first, self.record(5, "cleaned")]},
            {"source": "proxmox", "assignments": [second]},
        ]}
        seen = []
        self.run_login(inventory, lambda _s, ip, _c: seen.append(ip) or ("", "", 0, False),
                       retry_failed_only=False)
        self.assertEqual(seen, ["c1", "c2"])

    def test_new_accounts_only_when_the_earlier_ones_already_logged_in(self):
        done = self.record(1, login_status="connected")
        new = self.record(2)
        inventory = {"source": "proxmox", "setup_runs": [{"source": "proxmox", "assignments": [done, new]}]}
        seen = []

        def no_prompt():
            raise AssertionError("the client count must not be asked when only new VMs are logged in")

        with patch("builtins.input", return_value=""):
            self.run_login(inventory, lambda _s, ip, _c: seen.append(ip) or ("", "", 0, False),
                           ask_client_limit=no_prompt)
        self.assertEqual(seen, ["c2"])

    def test_client_count_is_asked_when_everything_is_logged_in_again(self):
        records = [self.record(1), self.record(2)]
        inventory = {"source": "proxmox", "setup_runs": [{"source": "proxmox", "assignments": records}]}
        seen = []
        self.run_login(inventory, lambda _s, ip, _c: seen.append(ip) or ("", "", 0, False),
                       ask_client_limit=lambda: 1)
        self.assertEqual(seen, ["c1"])


if __name__ == "__main__":
    unittest.main()
