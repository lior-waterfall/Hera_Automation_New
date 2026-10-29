import copy
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import requests

import load_test

SETTINGS = {
    "ip_source": "proxmox",
    "client_assets_factor": 1,
    "account_email_user": "user",
    "account_email_domain": "example.com",
    "asset_name": "Asset",
    "account_first_name": "F",
    "account_last_name": "L",
    "account_pass": "pw",
    "admin_credentials": "a:b",
    "server_ip": "1.1.1.1",
    "asset_protocol": "rdp",
    "asset_port": 3389,
    "asset_pass": "x",
    "api_port": 1,
    "mtls_port": 2,
    "proxmox_vms_by_ip": {},
}


def live(counter, client, asset):
    return {
        "counter": counter, "client_ip": client, "asset_ip": asset,
        "email": f"user{counter}@example.com", "status": "created",
    }


class PlanSetupAssignmentsTests(unittest.TestCase):
    def plan(self, clients, assets, live_records=(), taken=(), per_client=1):
        return load_test._plan_setup_assignments(
            SETTINGS, clients, assets, per_client, list(live_records), set(taken)
        )

    def test_fresh_run_numbers_from_one_in_client_order(self):
        new, reused, plan, skipped = self.plan(["c1", "c2", "c3"], ["a1", "a2", "a3"])
        self.assertEqual(new, ["c1", "c2", "c3"])
        self.assertEqual(reused, [])
        self.assertEqual([(a["counter"], a["client_ip"], a["asset_ip"]) for a in plan],
                         [(1, "c1", "a1"), (2, "c2", "a2"), (3, "c3", "a3")])
        self.assertEqual(skipped, [])

    def test_assets_per_client_matches_the_old_layout(self):
        _new, _reused, plan, _skipped = self.plan(["c1", "c2"], ["a1", "a2", "a3", "a4"], per_client=2)
        self.assertEqual([(a["client_ip"], a["asset_ip"]) for a in plan],
                         [("c1", "a1"), ("c1", "a3"), ("c2", "a2"), ("c2", "a4")])

    def test_added_vm_in_the_middle_does_not_renumber_existing_accounts(self):
        live_records = [live(1, "c1", "a1"), live(2, "c2", "a2"), live(3, "c3", "a3")]
        new, reused, plan, _skipped = self.plan(
            ["c1", "cNEW", "c2", "c3", "c4"], ["a1", "a2", "a3", "a4", "a5"], live_records
        )
        self.assertEqual(new, ["cNEW", "c4"])
        self.assertEqual(reused, ["c1", "c2", "c3"])
        self.assertEqual([(a["counter"], a["client_ip"]) for a in plan], [(4, "cNEW"), (5, "c4")])
        self.assertEqual([a["asset_ip"] for a in plan], ["a4", "a5"])

    def test_numbers_of_users_on_the_server_are_never_reused(self):
        new, _reused, plan, skipped = self.plan(
            ["c1", "c2", "c3"], ["a1", "a2", "a3"], taken={"user2@example.com"}
        )
        self.assertEqual([a["counter"] for a in plan], [1, 3, 4])
        self.assertEqual(skipped, ["user2@example.com"])

    def test_numbers_freed_by_teardown_are_used_again(self):
        _new, _reused, plan, _skipped = self.plan(
            ["c1", "c2", "c3"], ["a1", "a2", "a3"],
            live_records=[live(2, "c2", "a2")], taken={"user2@example.com"},
        )
        self.assertEqual([(a["counter"], a["client_ip"]) for a in plan], [(1, "c1"), (3, "c3")])

    def test_assets_in_use_are_avoided_for_new_clients(self):
        _new, _reused, plan, _skipped = self.plan(
            ["c1", "c2"], ["a1", "a2", "a3"], live_records=[live(1, "c1", "a1")]
        )
        self.assertEqual([a["asset_ip"] for a in plan], ["a2"])

    def test_assets_are_reused_only_when_there_are_not_enough(self):
        _new, _reused, plan, _skipped = self.plan(
            ["c1", "c2", "c3"], ["a1"], live_records=[live(9, "c9", "a1")]
        )
        self.assertEqual([a["asset_ip"] for a in plan], ["a1", "a1", "a1"])

    def test_all_selected_clients_already_have_accounts(self):
        new, reused, plan, _skipped = self.plan(["c1"], ["a1"], live_records=[live(1, "c1", "a1")])
        self.assertEqual((new, reused, plan), ([], ["c1"], []))


class LiveSetupRecordsTests(unittest.TestCase):
    def test_only_created_records_of_proxmox_runs_newest_per_email(self):
        old = live(1, "c-old", "a")
        new = live(1, "c-new", "a")
        inventory = {"source": "proxmox", "setup_runs": [
            {"source": "proxmox", "assignments": [old, {**live(2, "c2", "a"), "status": "cleaned"}]},
            {"source": "proxmox", "assignments": [new, {**live(3, "c3", "a"), "status": "failed"}]},
            {"source": "terraform", "assignments": [live(4, "c4", "a")]},
        ]}
        records = load_test._live_setup_records(inventory)
        self.assertEqual([(r["counter"], r["client_ip"]) for r in records], [(1, "c-new")])


class ExecuteOnClientsIncrementalTests(unittest.TestCase):
    def run_setup(self, discovered_clients, assets, inventory, server_emails, run_result):
        saved = []
        commands = []

        def fake_run(_settings, client_ip, command, _timeout=None, **_kwargs):
            commands.append((client_ip, command))
            return run_result(client_ip, command)

        def get_command(counter, asset_ip):
            return [
                "tauri.exe",
                f"--setup-account=F:L:user{counter}@example.com@1.1.1.1:pw",
                f"--setup-assets=Asset-{counter}:rdp:{asset_ip}:3389:x",
            ]

        settings = copy.deepcopy(SETTINGS)
        load_test.load_test_counter = 0
        with patch.object(load_test, "_get_target_ips", return_value=(discovered_clients, assets)), \
                patch.object(load_test, "_load_ip_inventory", return_value=inventory), \
                patch.object(load_test, "_save_ip_inventory",
                             side_effect=lambda inv: saved.append(copy.deepcopy(inv))), \
                patch.object(load_test, "_server_setup_emails", return_value=server_emails), \
                patch.object(load_test, "_execute_proxmox_interactive_command", side_effect=fake_run), \
                patch.object(load_test, "_desktop_tauri_account_present", return_value=(True, "")), \
                patch.object(load_test, "_apply_client_credentials_to_asset"), \
                patch.object(load_test.time, "sleep"), \
                redirect_stdout(io.StringIO()) as output:
            load_test._execute_on_clients(get_command, settings, record_setup=True)
        return commands, saved, output.getvalue(), inventory

    def inventory(self, records):
        return {"source": "proxmox", "setup_runs": [{"source": "proxmox", "assignments": records}]}

    def ok(self, _client, _command):
        return "ACCOUNT SETUP SUCCESSFUL", "", 0, False

    def test_only_new_clients_get_accounts_and_numbers_continue(self):
        inventory = self.inventory([live(1, "c1", "a1"), live(2, "c2", "a2")])
        commands, saved, output, inventory = self.run_setup(
            ["c0", "c1", "c2", "c3"], ["a1", "a2", "a3", "a4"], inventory,
            {"user1@example.com", "user2@example.com"}, self.ok,
        )
        self.assertEqual([client for client, _ in commands], ["c0", "c3"])
        self.assertIn("--setup-account=F:L:user3@example.com", commands[0][1][1])
        self.assertIn("--setup-account=F:L:user4@example.com", commands[1][1][1])
        new_run = inventory["setup_runs"][-1]
        self.assertEqual([(a["counter"], a["status"]) for a in new_run["assignments"]],
                         [(3, "created"), (4, "created")])
        self.assertIn("2 of the 4 selected client(s) already have an account", output)
        self.assertEqual(inventory["setup_runs"][0]["assignments"][0]["status"], "created")

    def test_nothing_to_do_when_every_client_has_an_account(self):
        inventory = self.inventory([live(1, "c1", "a1")])
        commands, _saved, output, inventory = self.run_setup(
            ["c1"], ["a1"], inventory, {"user1@example.com"}, self.ok
        )
        self.assertEqual(commands, [])
        self.assertEqual(len(inventory["setup_runs"]), 1)
        self.assertIn("nothing new to create", output)

    def test_account_deleted_on_the_server_is_created_again(self):
        inventory = self.inventory([live(1, "c1", "a1")])
        commands, _saved, output, _inventory = self.run_setup(
            ["c1"], ["a1"], inventory, set(), self.ok
        )
        self.assertEqual([client for client, _ in commands], ["c1"])
        self.assertIn("no longer exists on the HERA server", output)

    def test_server_check_failure_trusts_the_inventory(self):
        inventory = self.inventory([live(1, "c1", "a1")])
        commands, _saved, _output, _inventory = self.run_setup(
            ["c1", "c2"], ["a1", "a2"], inventory, None, self.ok
        )
        self.assertEqual([client for client, _ in commands], ["c2"])

    def test_one_failing_vm_is_recorded_and_the_next_one_still_runs(self):
        def result(client_ip, _command):
            if client_ip == "c1":
                raise requests.HTTPError(
                    "500 Server Error: Agent error: Command guest-file-open has been disabled: "
                    "the command is not allowed"
                )
            return "ACCOUNT SETUP SUCCESSFUL", "", 0, False

        commands, _saved, output, inventory = self.run_setup(
            ["c1", "c2"], ["a1", "a2"], self.inventory([]), set(), result
        )
        self.assertEqual([client for client, _ in commands], ["c1", "c2"])
        records = inventory["setup_runs"][-1]["assignments"]
        self.assertEqual([r["status"] for r in records], ["failed", "created"])
        self.assertIn("does not allow file transfers", output)
        self.assertNotIn("Traceback", output)

    def test_failure_output_is_plain_language(self):
        stderr = "=== ACCOUNT SETUP FAILED ===\nError: Failed to create account: Account already exists.\n=== END ==="
        _commands, _saved, output, inventory = self.run_setup(
            ["c1"], ["a1"], self.inventory([]), set(), lambda *_: ("", stderr, 1, False)
        )
        self.assertIn("already exists in the HERA client app", output)
        self.assertNotIn("Exit Code", output)
        self.assertIn("ACCOUNT SETUP FAILED", inventory["setup_runs"][-1]["assignments"][0]["error"])


if __name__ == "__main__":
    unittest.main()
