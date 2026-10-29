import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import load_test


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": self._data}


class ProxmoxExcludeTests(unittest.TestCase):
    VMS = [
        {"type": "qemu", "status": "running", "node": "pve1", "vmid": 1080, "name": "lior-hera-vm"},
        {"type": "qemu", "status": "running", "node": "pve1", "vmid": 1081, "name": "other-asset-vm"},
        {"type": "qemu", "status": "running", "node": "pve3", "vmid": 3010, "name": "win11-pve3-hera-01"},
        {"type": "qemu", "status": "running", "node": "pve3", "vmid": 3020, "name": "unrelated-vm"},
    ]
    IPS = {"1080": "10.0.0.29", "1081": "10.0.0.81", "3010": "10.0.0.44"}

    def discover(self, exclude, prefix=""):
        settings = {
            "proxmox_ip": "p", "proxmox_token_id": "t", "proxmox_token_secret": "s",
            "proxmox_ca_cert": "", "proxmox_exclude_vms": exclude, "proxmox_name_prefix": prefix,
        }

        def fake_get(url, **_kwargs):
            if url.endswith("cluster/resources?type=vm"):
                return FakeResponse(self.VMS)
            vmid = url.split("/qemu/")[1].split("/")[0]
            return FakeResponse({"result": [
                {"name": "eth0", "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": self.IPS[vmid]}
                ]}
            ]})

        with patch.object(load_test.requests, "get", side_effect=fake_get), redirect_stdout(io.StringIO()):
            return load_test._get_proxmox_ips(settings)

    def test_nothing_excluded_keeps_name_matches(self):
        clients, assets = self.discover(set())
        self.assertEqual(clients, ["10.0.0.29", "10.0.0.44"])
        self.assertEqual(assets, ["10.0.0.81"])

    def test_excluded_name_is_skipped(self):
        clients, assets = self.discover({"lior-hera-vm"})
        self.assertEqual(clients, ["10.0.0.44"])
        self.assertEqual(assets, ["10.0.0.81"])

    def test_excluded_vmid_and_ip_are_skipped(self):
        clients, assets = self.discover({"1080", "10.0.0.81"})
        self.assertEqual(clients, ["10.0.0.44"])
        self.assertEqual(assets, [])

    def test_name_prefix_keeps_only_matching_vms(self):
        clients, assets = self.discover(set(), prefix="win11")
        self.assertEqual(clients, ["10.0.0.44"])
        self.assertEqual(assets, [])

    def test_prefix_is_case_insensitive_and_combines_with_exclusions(self):
        clients, _assets = self.discover({"3010"}, prefix="WIN11")
        self.assertEqual(clients, [])

    def test_env_setting_is_parsed(self):
        env = {"PROXMOX_EXCLUDE_VMS": " Lior-Hera-VM, 1081 ,", "PROXMOX_VM_NAME_PREFIX": ' "Win11" '}
        with patch.dict(load_test.os.environ, env):
            with patch.object(load_test, "load_test_settings_cache", None), patch.object(
                load_test, "_load_dotenv"
            ):
                settings = load_test._read_load_test_settings()
                self.assertEqual(settings["proxmox_exclude_vms"], {"lior-hera-vm", "1081"})
                self.assertEqual(settings["proxmox_name_prefix"], "win11")
        load_test.load_test_settings_cache = None


if __name__ == "__main__":
    unittest.main()
