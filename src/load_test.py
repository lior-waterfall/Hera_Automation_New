import base64
import configparser
import os
import paramiko
import socket
import sqlite3
import subprocess
import json
import tempfile
import time
import ipaddress
import re
import requests
import urllib3
import uuid
from datetime import datetime, timezone
from urllib.parse import quote
from server_cleanup import asset_display_name, erase_web_user_and_asset
from tx_cert_upload import delete_tx_certificate, export_client_tx_certificate, extract_tx_certificate, tx_upload_configured, upload_tx_certificate

# Configuration
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SRC_DIR)
ENV_PATH = os.path.join(PROJECT_DIR, '.env')
TERRAFORM_DIR = os.path.join(SRC_DIR, 'aws')
IP_INVENTORY_PATH = os.path.join(PROJECT_DIR, "load_test_ip_lists.json")
DEFAULT_PROXMOX_IP = "10.101.28.213"

load_test_settings_cache = None
load_test_counter = 0
selected_client_limit = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _proxmox_api_base(settings):
    """Base URL for the Proxmox REST API."""
    return f"https://{settings['proxmox_ip'] or DEFAULT_PROXMOX_IP}:8006/api2/json"


def _proxmox_headers(settings):
    """Authorization header for the Proxmox API token."""
    return {
        "Authorization": (
            f"PVEAPIToken={settings['proxmox_token_id']}="
            f"{settings['proxmox_token_secret']}"
        )
    }


def _encode_ps_command(script):
    """Base64-encode a PowerShell script for powershell.exe -EncodedCommand."""
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def _load_ip_inventory():
    if not os.path.exists(IP_INVENTORY_PATH):
        return {"version": 1, "setup_runs": []}
    with open(IP_INVENTORY_PATH, "r", encoding="utf-8") as inventory_file:
        inventory = json.load(inventory_file)
    inventory.setdefault("setup_runs", [])
    return inventory


def _save_ip_inventory(inventory):
    with open(IP_INVENTORY_PATH, "w", encoding="utf-8") as inventory_file:
        json.dump(inventory, inventory_file, indent=2)
        inventory_file.write("\n")


def _inventory_vm(ip, settings):
    metadata = settings.get("proxmox_vms_by_ip", {}).get(ip, {})
    return {
        "ip": ip,
        "name": metadata.get("name", ""),
        "node": metadata.get("node", ""),
        "vmid": metadata.get("vmid"),
    }

def _load_dotenv():
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]
                    if key not in os.environ:
                        os.environ[key] = value

def _read_load_test_settings():
    """Lazily read load test configuration."""
    global load_test_settings_cache

    if load_test_settings_cache is None:
        _load_dotenv()
        
        project_namespace = os.environ.get('PROJECT_NAMESPACE', 'WATERFALL')

        load_test_settings_cache = {
            "ip_source": os.environ.get('IP_SOURCE', 'terraform').strip().lower(),
            "client_pass": os.environ.get('CLIENT_PASS', ''),
            "client_assets_factor": int(os.environ.get('CLIENT_ASSETS_FACTOR', '0')),
            "client_username": os.environ.get('CLIENT_USERNAME', ''),
            "server_ip": os.environ.get(f'{project_namespace}_SERVER_IP', ''),
            # Web login and the API stay on RX-In (SERVER_IP). The site
            # certificate SAN is a separate address; the client stores accounts
            # under that SAN, so --auto-login must use it as the lookup key.
            "client_site_ip": (
                os.environ.get('CLIENT_SITE_IP', '').strip()
                or os.environ.get(f'{project_namespace}_SERVER_IP', '')
            ),
            "server_pass": os.environ.get('SERVER_PASS', ''),
            "account_pass": os.environ.get('ACCOUNT_PASS', ''),
            "account_first_name": os.environ.get('ACCOUNT_FIRST_NAME', ''),
            "account_last_name": os.environ.get('ACCOUNT_LAST_NAME', ''),
            "account_email_user": os.environ.get('ACCOUNT_EMAIL_USER', ''),
            "account_email_domain": os.environ.get('ACCOUNT_EMAIL_DOMAIN', ''),
            "asset_name": os.environ.get('ASSET_NAME', ''),
            "asset_user": os.environ.get('ASSET_USER', ''),
            "asset_pass": os.environ.get('ASSET_PASS', ''),
            "asset_encoded_pass": quote(os.environ.get('ASSET_PASS', ''), safe=''),
            "asset_rendering_mode": os.environ.get('ASSET_RENDERING_MODE', 'low').strip().lower(),
            "low_rendering_url": os.environ.get('LOW_RENDERING_URL', 'https://time.is/').strip(),
            "high_rendering_url": os.environ.get('HIGH_RENDERING_URL', 'https://www.msn.com/').strip(),
            "asset_protocol": os.environ.get('ASSET_PROTOCOL', ''),
            "asset_port": int(os.environ.get('ASSET_PORT', '3389')),
            "api_port": int(os.environ.get(f'{project_namespace}_API_PORT', '0')),
            "mtls_port": int(os.environ.get(f'{project_namespace}_MTLS_PORT', '0')),
            "mtls_issue_sleep_time": int(os.environ.get('MTLS_ISSUE_SLEEP_TIME', '9')),
            "admin_credentials": os.environ.get('ADMIN_CREDENTIALS', ''),
            "api_scheme": os.environ.get('API_SCHEME', 'https'),
            "hera_web_url": os.environ.get('HERA_WEB_URL', '').rstrip('/'),
            "tx_host": os.environ.get('TX_HOST', ''),
            "tx_ssh_user": os.environ.get('TX_SSH_USER', ''),
            "tx_ssh_pass": os.environ.get('TX_SSH_PASS', ''),
            "tx_ssh_key": os.environ.get('TX_SSH_KEY', ''),
            "tx_ssh_port": int(os.environ.get('TX_SSH_PORT', '22')),
            "tx_cert_folder": os.environ.get('TX_CERT_FOLDER', ''),
            "tx_rehash_command": os.environ.get('TX_REHASH_COMMAND', ''),
            "proxmox_ip": os.environ.get('PROXMOX_IP', ''),
            "proxmox_token_id": os.environ.get('TOKEN_ID', ''),
            "proxmox_token_secret": os.environ.get('SECRET', ''),
            "proxmox_ca_cert": os.environ.get('PROXMOX_CA_CERT', ''),
            "proxmox_name_prefix": os.environ.get('PROXMOX_VM_NAME_PREFIX', '').strip().strip('"').strip("'").lower(),
            "proxmox_exclude_vms": {
                item.strip().strip('"').strip("'").lower()
                for item in os.environ.get('PROXMOX_EXCLUDE_VMS', '').split(',')
                if item.strip()
            },
        }
        # ASSET_USER/ASSET_PASS stay for EC2; Proxmox assets use their own pair.
        proxmox_asset_user = os.environ.get('PROXMOX_ASSET_USER', '').strip()
        proxmox_asset_pass = os.environ.get('PROXMOX_ASSET_PASS', '')
        settings = load_test_settings_cache
        settings["proxmox_asset_creds"] = bool(
            settings["ip_source"] == "proxmox" and proxmox_asset_user and proxmox_asset_pass
        )
        if settings["proxmox_asset_creds"]:
            settings["asset_user"] = proxmox_asset_user
            settings["asset_pass"] = proxmox_asset_pass
            settings["asset_encoded_pass"] = quote(proxmox_asset_pass, safe='')

    return load_test_settings_cache


def _get_terraform_ips():
    """Retrieve client and asset IPs from Terraform outputs."""
    try:
        result = subprocess.run(
            ['terraform', 'output', '-json'],
            cwd=TERRAFORM_DIR,
            capture_output=True,
            text=True,
            check=True
        )
        outputs = json.loads(result.stdout)
        
        client_ips = outputs.get('client_ips', {}).get('value', [])
        asset_ips = outputs.get('asset_ips', {}).get('value', [])
        
        return client_ips, asset_ips
    except subprocess.CalledProcessError as e:
        print(f"Error running terraform output: {e.stderr}")
        return [], []
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Error parsing Terraform outputs: {e}")
        return [], []
    except FileNotFoundError:
        print("Error: 'terraform' command not found. Please ensure Terraform is installed and in PATH.")
        return [], []


def _get_proxmox_ips(settings):
    """Retrieve running Proxmox VM IPs, separating Hera clients from assets by name."""
    proxmox_ip = settings["proxmox_ip"] or DEFAULT_PROXMOX_IP
    api_url = _proxmox_api_base(settings)
    token_id = settings["proxmox_token_id"]
    token_secret = settings["proxmox_token_secret"]
    ca_cert = settings["proxmox_ca_cert"]

    if not token_id or not token_secret:
        print("Proxmox IP discovery requires TOKEN_ID and SECRET in .env.")
        return [], []

    def request_json(path):
        response = requests.get(
            f"{api_url}/{path}",
            headers=_proxmox_headers(settings),
            verify=False,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    try:
        resources = request_json("cluster/resources?type=vm").get("data", [])
    except Exception as exc:
        print(f"Could not retrieve Proxmox VM list: {exc}")
        return [], []

    client_ips = []
    asset_ips = []
    settings["proxmox_vms_by_ip"] = {}
    running_vms = [
        vm for vm in resources
        if vm.get("type") == "qemu" and vm.get("status") == "running"
    ]
    running_vms.sort(key=lambda vm: vm.get("node", ""))

    excluded = {str(item).lower() for item in settings.get("proxmox_exclude_vms", ())}
    name_prefix = str(settings.get("proxmox_name_prefix", "") or "").lower()
    skipped_vms = []
    ignored_count = 0
    for vm in running_vms:
        name = str(vm.get("name", ""))
        vm_name = name.lower()
        if name_prefix and not vm_name.startswith(name_prefix):
            ignored_count += 1
            continue
        if vm_name in excluded or str(vm.get("vmid", "")) in excluded:
            ignored_count += 1
            continue
        is_client = re.search(r"(^|[^a-z0-9])hera([^a-z0-9]|$)", vm_name) is not None
        is_asset = re.search(r"(^|[^a-z0-9])asset([^a-z0-9]|$)", vm_name) is not None
        if is_client == is_asset:
            continue

        node = quote(str(vm.get("node", "")), safe="")
        vmid = quote(str(vm.get("vmid", "")), safe="")
        result_data = None
        read_error = None
        # The guest agent can answer late right after boot or under load, so ask a few times.
        for attempt in range(3):
            try:
                result_data = request_json(
                    f"nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
                ).get("data", {}).get("result", [])
                read_error = None
            except Exception as exc:
                result_data = None
                read_error = exc
            if isinstance(result_data, list):
                break
            if attempt < 2:
                time.sleep(3)

        if not isinstance(result_data, list):
            skipped_vms.append(name)
            continue

        vm_ip = None
        for interface in result_data:
            if not isinstance(interface, dict) or interface.get("name") == "lo":
                continue
            for address_info in interface.get("ip-addresses", []):
                if not isinstance(address_info, dict):
                    continue
                if address_info.get("ip-address-type") != "ipv4":
                    continue
                try:
                    address = ipaddress.ip_address(address_info.get("ip-address", ""))
                except ValueError:
                    continue
                if not address.is_loopback and not address.is_link_local and not address.is_unspecified:
                    vm_ip = str(address)
                    break
            if vm_ip:
                break

        if not vm_ip:
            skipped_vms.append(name)
            continue
        if vm_ip in excluded:
            ignored_count += 1
            continue
        settings["proxmox_vms_by_ip"][vm_ip] = {
            "node": str(vm.get("node", "")),
            "vmid": int(vm.get("vmid", 0)),
            "name": name,
        }
        (client_ips if is_client else asset_ips).append(vm_ip)

    ignored_note = f" ({ignored_count} other VM(s) ignored)" if ignored_count else ""
    print(f"Discovered {len(client_ips)} Hera client(s) and {len(asset_ips)} asset(s) from Proxmox{ignored_note}.")
    if skipped_vms:
        print(
            f"WARNING: {len(skipped_vms)} VM(s) were left out because their guest agent did not answer: "
            f"{', '.join(skipped_vms)}. Check that they are powered on and Windows has finished starting, "
            "then run the step again."
        )
    return client_ips, asset_ips


def _get_target_ips(settings):
    """Select the configured IP source while keeping Terraform as the default."""
    source = settings["ip_source"]
    if source == "terraform":
        return _get_terraform_ips()
    if source == "proxmox":
        return _get_proxmox_ips(settings)
    print(f"Unsupported IP_SOURCE '{source}'. Choose 'terraform' or 'proxmox'.")
    return [], []


def _is_transient_agent_error(status_code, text):
    """Guest-agent hiccups that usually clear within seconds (agent restarting, busy, or just booted)."""
    if status_code != 500:
        return False
    details = (text or "").lower()
    return any(
        marker in details
        for marker in (
            "has been disabled",
            "failed to execute helper program",
            "guest agent is not running",
            "guest agent is not ready",
        )
    )


def _execute_proxmox_command(settings, client_ip, command, timeout_seconds=None):
    vm = settings.get("proxmox_vms_by_ip", {}).get(client_ip)
    if not vm:
        return "", f"No Proxmox VM metadata found for {client_ip}.", -1, False

    api_base = _proxmox_api_base(settings)
    node = quote(vm["node"], safe="")
    vmid = vm["vmid"]
    agent_url = f"{api_base}/nodes/{node}/qemu/{vmid}/agent"
    headers = _proxmox_headers(settings)
    process_id = None

    def terminate_process():
        if process_id is None:
            return
        try:
            requests.post(
                f"{agent_url}/exec",
                headers=headers,
                data=[
                    ("command", part)
                    for part in ("taskkill.exe", "/PID", str(process_id), "/T", "/F")
                ],
                verify=False,
                timeout=10,
            )
        except requests.RequestException:
            pass

    def request_agent(method, endpoint, **kwargs):
        attempts = 4
        for attempt in range(attempts):
            try:
                response = requests.request(
                    method,
                    f"{agent_url}/{endpoint}",
                    headers=headers,
                    verify=False,
                    **kwargs,
                )
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                response = getattr(exc, "response", None)
                retryable = response is not None and _is_transient_agent_error(
                    response.status_code, response.text
                )
                if not retryable or attempt == attempts - 1:
                    raise
                time.sleep(2 * (attempt + 1))

    try:
        response = request_agent(
            "POST",
            "exec",
            data=[("command", str(part)) for part in command],
            timeout=15,
        )
        process_id = response.json().get("data", {}).get("pid")
        if process_id is None:
            return "", "Proxmox guest agent did not return a process ID.", -1, False

        deadline = time.monotonic() + (timeout_seconds or 180)
        while time.monotonic() < deadline:
            status_response = request_agent(
                "GET",
                "exec-status",
                params={"pid": process_id},
                timeout=15,
            )
            status = status_response.json().get("data", {})
            if status.get("exited"):
                return (
                    status.get("out-data", ""),
                    status.get("err-data", ""),
                    status.get("exitcode", -1),
                    False,
                )
            time.sleep(0.5)
    except requests.RequestException as exc:
        terminate_process()
        response = getattr(exc, "response", None)
        details = ""
        if response is not None:
            try:
                details = str(response.json().get("errors", ""))
            except ValueError:
                details = response.text[:500]
        return "", f"Proxmox guest-agent command failed: {exc} {details}".strip(), -1, False

    terminate_process()
    return "", "Timed out waiting for the Proxmox guest-agent command.", -1, True


def _write_proxmox_file(settings, client_ip, remote_path, content):
    """Write a file through the guest agent so the following command can stay short."""
    vm = settings.get("proxmox_vms_by_ip", {}).get(client_ip)
    if not vm:
        raise RuntimeError(f"No Proxmox VM metadata found for {client_ip}.")
    api_base = _proxmox_api_base(settings)
    node = quote(vm["node"], safe="")
    vmid = vm["vmid"]
    headers = _proxmox_headers(settings)
    attempts = 4
    for attempt in range(attempts):
        last_attempt = attempt == attempts - 1
        try:
            response = requests.post(
                f"{api_base}/nodes/{node}/qemu/{vmid}/agent/file-write",
                headers=headers,
                data={
                    "file": remote_path,
                    "content": content,
                },
                verify=False,
                timeout=20,
            )
        except (requests.ConnectionError, requests.Timeout):
            if last_attempt:
                raise
            time.sleep(2 * (attempt + 1))
            continue
        # The guest agent sometimes drops the mkdir that should have created the folder.
        if response.status_code == 500 and "cannot find the path" in response.text.lower() and not last_attempt:
            folder = remote_path.rsplit("\\", 1)[0]
            _execute_proxmox_command(settings, client_ip, ["cmd.exe", "/c", f"mkdir {folder}"], 15)
            time.sleep(attempt + 1)
            continue
        if _is_transient_agent_error(response.status_code, response.text) and not last_attempt:
            time.sleep(2 * (attempt + 1))
            continue
        response.raise_for_status()
        return


def _windows_logon_candidates(settings):
    """Return enabled-account names and passwords that may log on to a client VM."""
    usernames = []
    for value in ("admin", settings.get("client_username", "")):
        value = (value or "").strip()
        if value and value.lower() not in {item.lower() for item in usernames}:
            usernames.append(value)
    passwords = []
    for value in (
        settings.get("client_pass", ""),
        settings.get("asset_pass", ""),
        os.environ.get("TF_VAR_client_password", ""),
        os.environ.get("TX_SSH_PASS", ""),
    ):
        value = (value or "").strip().strip('"').strip("'")
        if value and value not in passwords:
            passwords.append(value)
    return usernames, passwords


def _windows_console_user(settings, client_ip):
    """Return the console username, or an empty string when the login screen is idle."""
    script = "(Get-CimInstance Win32_ComputerSystem).UserName"
    encoded = _encode_ps_command(script)
    output, error_output, exit_status, timed_out = _execute_proxmox_command(
        settings, client_ip,
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        25,
    )
    if timed_out or exit_status != 0:
        return "", _readable_powershell_error(error_output) or output or "Could not read the Windows console user."
    return (output or "").strip(), ""


def _resolve_windows_logon(settings, client_ip):
    """Find a username and password that can log on to this Windows client."""
    cached = settings.get("windows_logon_shared")
    if cached:
        return cached["user"], cached["password"]
    usernames, passwords = _windows_logon_candidates(settings)
    if not passwords:
        raise RuntimeError("No Windows password is configured.")
    payload = base64.b64encode(json.dumps({
        "usernames": usernames,
        "passwords": passwords,
    }).encode("utf-8")).decode("ascii")
    script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class HeraLogonProbe {{
  [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool LogonUser(string user, string domain, string pass, int logonType, int provider, out IntPtr token);
  [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
}}
'@
$spec = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) | ConvertFrom-Json
$names = New-Object System.Collections.Generic.List[string]
foreach ($name in @($spec.usernames)) {{
  if ($name -and -not $names.Contains([string]$name)) {{ $names.Add([string]$name) }}
}}
Get-LocalUser | Where-Object {{ $_.Enabled }} | ForEach-Object {{
  if ($_.Name -notin @('Guest','DefaultAccount','WDAGUtilityAccount') -and -not $names.Contains($_.Name)) {{
    $names.Add($_.Name)
  }}
}}
$index = 0
foreach ($pass in @($spec.passwords)) {{
  foreach ($name in $names) {{
    $token = [IntPtr]::Zero
    $ok = [HeraLogonProbe]::LogonUser([string]$name, '.', [string]$pass, 2, 0, [ref]$token)
    if ($token -ne [IntPtr]::Zero) {{ [HeraLogonProbe]::CloseHandle($token) | Out-Null }}
    if ($ok) {{
      Write-Output ('MATCH ' + $name + ' ' + $index)
      exit 0
    }}
  }}
  $index++
}}
Write-Output 'NOMATCH'
exit 1
""".strip()
    remote = r"C:\ProgramData\HeraLoadTest\logon-probe.ps1"
    _execute_proxmox_command(
        settings, client_ip,
        ["cmd.exe", "/c", "mkdir C:\\ProgramData\\HeraLoadTest"],
        15,
    )
    match = None
    detail = ""
    verdict_reached = False
    # PowerShell sometimes fails to start on a busy VM, so a missing verdict is retried once.
    for attempt in range(2):
        try:
            _write_proxmox_file(settings, client_ip, remote, script)
            output, error_output, exit_status, timed_out = _execute_proxmox_command(
                settings, client_ip,
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", remote],
                40,
            )
        finally:
            _execute_proxmox_command(
                settings, client_ip,
                ["cmd.exe", "/c", "del /f /q C:\\ProgramData\\HeraLoadTest\\logon-probe.ps1"],
                15,
            )
        if timed_out:
            raise RuntimeError("Timed out while checking the Windows account password.")
        match = re.search(r"MATCH\s+(\S+)\s+(\d+)", output or "")
        verdict_reached = bool(match) or "NOMATCH" in (output or "")
        detail = _readable_powershell_error(error_output) or (output or "").strip()
        if verdict_reached:
            break
        if attempt == 0:
            time.sleep(5)
    if not verdict_reached:
        raise RuntimeError("PowerShell could not run the password check on this VM. " + detail[:300])
    if not match or exit_status != 0:
        raise RuntimeError(
            "None of the configured passwords can log on to an enabled Windows account."
        )
    found = {"user": match.group(1), "password": passwords[int(match.group(2))]}
    settings["windows_logon_shared"] = found
    return found["user"], found["password"]


def _windows_cmd_script(command):
    lines = ["@echo off"]
    quoted = []
    for part in command:
        escaped = str(part).replace("%", "%%").replace('"', '""')
        quoted.append(f'"{escaped}"')
    lines.append(" ".join(quoted))
    return "\r\n".join(lines) + "\r\n"


def _execute_proxmox_credential_command(settings, client_ip, command, timeout_seconds=None):
    """Run a command as a Windows user logged on with a password, without a console session."""
    if isinstance(command, str):
        command = ["cmd.exe", "/c", command]
    try:
        username, password = _resolve_windows_logon(settings, client_ip)
    except Exception as exc:
        return "", str(exc), -1, False

    timeout = int(timeout_seconds or 180)
    task_id = uuid.uuid4().hex
    work_dir = rf"C:\ProgramData\HeraLoadTest\{task_id}"
    stdout_path = work_dir + r"\stdout.txt"
    stderr_path = work_dir + r"\stderr.txt"
    cmd_path = work_dir + r"\run.cmd"
    script_path = work_dir + r"\launch.ps1"
    payload = base64.b64encode(json.dumps({
        "user": username,
        "password": password,
    }).encode("utf-8")).decode("ascii")
    script = f"""
$ErrorActionPreference = 'Stop'
$spec = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) | ConvertFrom-Json
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class HeraUserLaunch {{
  [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool LogonUser(string user, string domain, string pass, int logonType, int provider, out IntPtr token);
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct PROFILEINFO {{
    public int dwSize; public int dwFlags; public string lpUserName; public string lpProfilePath;
    public string lpDefaultPath; public string lpServerName; public string lpPolicyPath; public IntPtr hProfile;
  }}
  [DllImport("userenv.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool LoadUserProfile(IntPtr token, ref PROFILEINFO profile);
  [DllImport("userenv.dll", SetLastError=true)]
  public static extern bool CreateEnvironmentBlock(out IntPtr env, IntPtr token, bool inherit);
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct STARTUPINFO {{
    public int cb; public string lpReserved; public string lpDesktop; public string lpTitle;
    public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
    public short wShowWindow, cbReserved2; public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
  }}
  [StructLayout(LayoutKind.Sequential)]
  public struct PROCESS_INFORMATION {{ public IntPtr hProcess, hThread; public int dwProcessId, dwThreadId; }}
  [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool CreateProcessAsUser(IntPtr token, string app, string cmd, IntPtr pa, IntPtr ta, bool inherit, uint flags, IntPtr env, string dir, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
  [DllImport("kernel32.dll")] public static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
  [DllImport("kernel32.dll")] public static extern bool GetExitCodeProcess(IntPtr handle, out uint code);
  [DllImport("kernel32.dll")] public static extern bool TerminateProcess(IntPtr handle, uint code);
  [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
}}
'@
$token = [IntPtr]::Zero
if (-not [HeraUserLaunch]::LogonUser([string]$spec.user, '.', [string]$spec.password, 2, 0, [ref]$token)) {{
  throw ('Windows logon failed: ' + [Runtime.InteropServices.Marshal]::GetLastWin32Error())
}}
$profile = New-Object HeraUserLaunch+PROFILEINFO
$profile.dwSize = [Runtime.InteropServices.Marshal]::SizeOf([type][HeraUserLaunch+PROFILEINFO])
$profile.lpUserName = [string]$spec.user
if (-not [HeraUserLaunch]::LoadUserProfile($token, [ref]$profile)) {{
  throw ('Could not load the Windows user profile: ' + [Runtime.InteropServices.Marshal]::GetLastWin32Error())
}}
$envBlock = [IntPtr]::Zero
[void][HeraUserLaunch]::CreateEnvironmentBlock([ref]$envBlock, $token, $false)
$si = New-Object HeraUserLaunch+STARTUPINFO
$si.cb = [Runtime.InteropServices.Marshal]::SizeOf([type][HeraUserLaunch+STARTUPINFO])
$pi = New-Object HeraUserLaunch+PROCESS_INFORMATION
$cmd = '/c {cmd_path} 1> {stdout_path} 2> {stderr_path}'
$ok = [HeraUserLaunch]::CreateProcessAsUser($token, 'C:\\Windows\\System32\\cmd.exe', $cmd, [IntPtr]::Zero, [IntPtr]::Zero, $false, 0x400, $envBlock, 'C:\\Windows\\System32', [ref]$si, [ref]$pi)
if (-not $ok) {{ throw ('Could not start the command as the Windows user: ' + [Runtime.InteropServices.Marshal]::GetLastWin32Error()) }}
$wait = [HeraUserLaunch]::WaitForSingleObject($pi.hProcess, {timeout * 1000})
$timedOut = $wait -ne 0
if ($timedOut) {{ [void][HeraUserLaunch]::TerminateProcess($pi.hProcess, 1) }}
$code = 0
[void][HeraUserLaunch]::GetExitCodeProcess($pi.hProcess, [ref]$code)
$stdoutText = ''
$stderrText = ''
if (Test-Path -LiteralPath '{stdout_path}') {{ $stdoutText = [IO.File]::ReadAllText('{stdout_path}') }}
if (Test-Path -LiteralPath '{stderr_path}') {{ $stderrText = [IO.File]::ReadAllText('{stderr_path}') }}
@{{ stdout=$stdoutText; stderr=$stderrText; exitcode=[int]$code; timed_out=[bool]$timedOut }} | ConvertTo-Json -Compress
""".strip()
    _execute_proxmox_command(
        settings, client_ip,
        ["cmd.exe", "/c", "mkdir C:\\ProgramData\\HeraLoadTest"],
        15,
    )
    try:
        _execute_proxmox_command(
            settings, client_ip,
            ["cmd.exe", "/c", f"mkdir {work_dir}"],
            15,
        )
        _write_proxmox_file(settings, client_ip, cmd_path, _windows_cmd_script(command))
        _write_proxmox_file(settings, client_ip, script_path, script)
        _execute_proxmox_command(
            settings, client_ip,
            ["icacls.exe", work_dir, "/grant", "Users:(OI)(CI)RX"],
            15,
        )
        output, error_output, exit_status, timed_out = _execute_proxmox_command(
            settings, client_ip,
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path],
            timeout + 30,
        )
    finally:
        _execute_proxmox_command(
            settings, client_ip,
            ["cmd.exe", "/c", f"rmdir /s /q {work_dir}"],
            15,
        )
    if timed_out:
        return "", "Timed out while running the command as the Windows user.", -1, True
    if exit_status != 0:
        return output, _readable_powershell_error(error_output) or output, exit_status, False
    try:
        result = json.loads((output or "").strip())
    except json.JSONDecodeError as exc:
        return "", f"Could not parse Windows logon command result: {exc}; {(output or '')[:500]}", -1, False
    return (
        result.get("stdout", ""),
        _readable_powershell_error(result.get("stderr", "")),
        int(result.get("exitcode", -1)),
        bool(result.get("timed_out", False)),
    )


def _readable_powershell_error(text):
    """Return the error text from a PowerShell CLIXML stderr blob."""
    if not text or "#< CLIXML" not in text:
        return text or ""
    messages = re.findall(r'<S S="Error">([^<]*)</S>', text)
    decoded = [
        message.replace("_x000D__x000A_", "\n").replace("_x000D_", "\r").replace("_x000A_", "\n")
        for message in messages
    ]
    cleaned = "\n".join(line for line in decoded if line.strip())
    return cleaned or text


class StepError(RuntimeError):
    """A failure with a message QA can act on and the raw technical detail for developers."""

    def __init__(self, message, detail=""):
        super().__init__(message)
        self.detail = detail


_WRAPPER_NOISE = re.compile(
    r"^(\+|At\s|At line:|:String\)|\s*~+)|CategoryInfo\s*:|FullyQualifiedErrorId\s*:"
    r"|\[\],\s*\w*Exception|^(timeException|Run)$"
)

# Checked in order; the first match decides the message shown to the operator.
_FAILURE_PATTERNS = [
    (r"guest agent is not running",
     "the VM's guest agent stopped responding. Check that the VM is powered on and Windows has finished starting."),
    (r"vm \d+ is not running",
     "the VM is powered off. Start it in Proxmox."),
    (r"configuration file .* does not exist",
     "the VM no longer exists in Proxmox. Run step 1 again to refresh the VM list."),
    (r"guest-file-\w+ has been disabled",
     "the VM's guest agent does not allow file transfers right now. Restart the VM; "
     "if it keeps happening, ask the administrator to allow file access in the guest-agent settings."),
    (r"has been disabled",
     "the Proxmox guest agent is not allowed to run commands. Ask the administrator to enable it."),
    (r"No Proxmox VM metadata",
     "this VM is not in the saved VM list. Run step 1 again to refresh it."),
    (r"Starting the CLR failed|80004005",
     "PowerShell could not start on this VM. Restart the VM and try again."),
    (r"PowerShell could not run the password check",
     "the Windows password check could not run on this VM. Restart the VM and try again."),
    (r"None of the configured passwords",
     "none of the passwords in the .env file can log on to Windows on this VM. "
     "Check CLIENT_PASS and ASSET_PASS."),
    (r"Microsoft Edge is not installed",
     "Microsoft Edge is not installed on the asset."),
    (r"Edge did not (stay open|open)",
     "Edge did not open on the asset. Check that the asset VM is responsive."),
    (r"No interactive Windows user",
     "nobody is logged in to Windows on the VM."),
    (r"No desktop session appeared|did not open a session",
     "HERA did not open a remote session on the asset."),
    (r"could not be stopped",
     "a previous HERA client is still running and could not be closed."),
    (r"TpmMtlsProxy",
     "the HERA client started, but its secure-connection component (mTLS proxy) did not start."),
    (r"Tauri (is not visible|exited)|Tauri or TpmMtlsProxy",
     "the HERA client window closed or did not appear."),
    (r"Account already exists",
     "this account already exists in the HERA client app on the VM (left over from an earlier run). "
     "Run step 4 first, or delete it in the HERA client app."),
    (r"user already exists|409 conflict",
     "this user already exists on the HERA server. Run step 4 first, or delete it in the HERA UI."),
    (r"did not print ACCOUNT SETUP SUCCESSFUL",
     "the HERA client did not confirm the account setup. Open the HERA client app on that VM and check it."),
    (r"live accounts:",
     "the account was not found in the HERA client app on the VM after setup."),
    (r"still present in the desktop",
     "the account is still in the HERA client app on the VM. Delete it there manually."),
    (r"delete or rehash failed",
     "the TX server did not confirm that the certificate was deleted. Check the TX server."),
    (r"Could not reach the Hera API",
     "the HERA server could not be reached. Check the server address and that the server is running."),
    (r"failed with status 5\d\d",
     "the HERA server returned an error. Wait a minute and run step 4 again; "
     "if it keeps failing, delete it in the HERA UI."),
    (r"timed out",
     "the VM did not respond in time. Check that it is running and not overloaded."),
]


def _clean_command_error(text):
    """Drop PowerShell's wrapper lines (NativeCommandError, script positions) and keep the real message."""
    lines = []
    for raw_line in (text or "").replace("\r", "").split("\n"):
        line = re.sub(r"^\s*powershell\.exe\s*:\s*", "", raw_line).strip()
        if line and not _WRAPPER_NOISE.search(line) and line not in lines:
            lines.append(line)
    return " ".join(lines)


def _describe_failure(subject, text):
    """Return (message for QA, raw technical detail) for a failed step."""
    raw = (text or "").strip()
    prefix = f"{subject}: " if subject else ""
    for pattern, reason in _FAILURE_PATTERNS:
        if re.search(pattern, raw, re.IGNORECASE | re.DOTALL):
            return f"{prefix}{reason}", raw
    cleaned = _clean_command_error(_readable_powershell_error(raw)) or "no details were returned"
    if len(cleaned) > 200:
        cleaned = cleaned[:200].rstrip() + "..."
    return f"{prefix}unexpected problem ({cleaned}).", raw


def _describe_exception(subject, exc):
    """Like _describe_failure for an exception; keeps messages already written for QA."""
    if isinstance(exc, StepError):
        return str(exc), exc.detail or str(exc)
    response = getattr(exc, "response", None)
    text = str(exc)
    if response is not None:
        text = f"{text} {getattr(response, 'text', '')[:500]}"
    return _describe_failure(subject, text)


_CLEANUP_LABELS = {
    "Client session shutdown": "Closing the HERA client",
    "Hera API cleanup": "HERA server",
    "TX certificate cleanup": "TX certificate",
    "Local Tauri cleanup": "HERA client app",
}


def _friendly_cleanup_error(error):
    """Turn a stored teardown error like 'Hera API cleanup: DELETE … 500' into a sentence for QA."""
    label, separator, rest = (error or "").partition(": ")
    if not separator or label not in _CLEANUP_LABELS:
        return _describe_failure("", error)[0]
    return f"{_CLEANUP_LABELS[label]}: {_describe_failure('', rest)[0]}"


def _execute_proxmox_interactive_command(settings, client_ip, command, timeout_seconds=None, run_as_user=None):
    """Run a command as the Windows desktop user.

    A console session is used when one exists. Otherwise the VM username and
    password log the account on, because Tauri only needs that user profile.
    run_as_user targets a known logged-on user, e.g. an RDP session.
    """
    if isinstance(command, str):
        command = ["cmd.exe", "/c", command]
    if not run_as_user:
        console_user, console_error = _windows_console_user(settings, client_ip)
        if console_error and "guest-exec" in console_error.lower():
            return "", console_error, -1, False
        if not console_user:
            print(f"    - No console session on {client_ip}; logging on with the VM account.")
            return _execute_proxmox_credential_command(
                settings, client_ip, command, timeout_seconds
            )

    task_id = uuid.uuid4().hex
    work_dir = rf"C:\ProgramData\HeraLoadTest\{task_id}"
    stdout_path = work_dir + r"\stdout.txt"
    stderr_path = work_dir + r"\stderr.txt"
    exit_path = work_dir + r"\exitcode.txt"
    command_payload = base64.b64encode(
        json.dumps([str(part) for part in command]).encode("utf-8")
    ).decode("ascii")
    inner_script = f"""
$ErrorActionPreference = 'Stop'
$spec = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{command_payload}')) | ConvertFrom-Json
$program = [string]$spec[0]
$argv = @($spec | Select-Object -Skip 1 | ForEach-Object {{ [string]$_ }})
# tauri-client-app.exe is a GUI-subsystem binary. PowerShell's call operator
# starts it without CLI arguments, so --setup-account never runs and the app
# opens with an empty account list. cmd.exe delivers those arguments.
$leaf = [IO.Path]::GetFileName($program).ToLowerInvariant()
# Only the GUI-subsystem client drops arguments under PowerShell's call operator.
# Leave PsExec alone so its detached process ID still comes back as the exit code.
if ($leaf -eq 'tauri-client-app.exe') {{
    $quoted = @('"' + $program.Replace('"', '""') + '"')
    foreach ($arg in $argv) {{ $quoted += '"' + $arg.Replace('"', '""') + '"' }}
    $program = 'cmd.exe'
    $argv = @('/d', '/s', '/c', ($quoted -join ' '))
}}
try {{
    $ErrorActionPreference = 'Continue'
    # Keep redirection inside cmd.exe. PowerShell's 2> operator turns native
    # stderr into a RemoteException and replaces the real Tauri error.
    if ($program -eq 'cmd.exe') {{
        $cmdline = [string]$argv[-1]
        $argv = @('/d', '/s', '/c', ($cmdline + ' 1> "{stdout_path}" 2> "{stderr_path}" & exit /b %ERRORLEVEL%'))
        & $program @argv
    }} else {{
        & $program @argv 1> '{stdout_path}' 2> '{stderr_path}'
    }}
    $result = $LASTEXITCODE
    if ($null -eq $result) {{ $result = 0 }}
}} catch {{
    $detail = ($_ | Out-String).Trim()
    if (-not $detail) {{ $detail = $_.Exception.ToString() }}
    Add-Content -LiteralPath '{stderr_path}' -Value $detail -Encoding utf8
    $result = 1
}}
[IO.File]::WriteAllText('{exit_path}', [string]$result)
""".strip()
    inner_script_path = work_dir + r"\task.ps1"
    timeout = timeout_seconds or 180
    outer_script = f"""
$ErrorActionPreference = 'Stop'
$taskName = 'HeraLoadTest_{task_id}'
$workDir = '{work_dir}'
$stdoutPath = '{stdout_path}'
$stderrPath = '{stderr_path}'
$exitPath = '{exit_path}'
$deadline = (Get-Date).AddSeconds({int(timeout)})
$taskRegistered = $false
$taskType = 'powershell'
try {{
    $activeUser = {("'" + run_as_user.replace("'", "''") + "'") if run_as_user else '(Get-CimInstance Win32_ComputerSystem).UserName'}
    if ([string]::IsNullOrWhiteSpace($activeUser)) {{ throw 'No interactive Windows user is logged in.' }}
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null
    & icacls.exe $workDir /grant ($activeUser + ':(OI)(CI)M') | Out-Null
    if ($LASTEXITCODE -ne 0) {{ throw 'Could not grant the interactive user access to the temporary command folder.' }}
    try {{
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File {inner_script_path}'
    }} catch {{
        if ($_ -notmatch 'Invalid class') {{ throw }}
        $taskType = 'schtasks'
        $runTime = (Get-Date).AddMinutes(1).ToString('HH:mm')
        $taskCommand = 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File {inner_script_path}'
        $createOutput = & schtasks.exe /Create /TN $taskName /SC ONCE /ST $runTime /TR $taskCommand /RU $activeUser /IT /F 2>&1
        if ($LASTEXITCODE -ne 0) {{ throw ('Could not create interactive task: ' + ($createOutput | Out-String).Trim()) }}
        $taskRegistered = $true
        $runOutput = & schtasks.exe /Run /TN $taskName 2>&1
        if ($LASTEXITCODE -ne 0) {{ throw ('Could not start interactive task: ' + ($runOutput | Out-String).Trim()) }}
    }}
    if ($taskType -eq 'powershell') {{
        $principal = New-ScheduledTaskPrincipal -UserId $activeUser -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Force | Out-Null
        $taskRegistered = $true
        Start-ScheduledTask -TaskName $taskName
    }}
    while ((Get-Date) -lt $deadline -and -not (Test-Path -LiteralPath $exitPath)) {{ Start-Sleep -Milliseconds 250 }}
    if (-not (Test-Path -LiteralPath $exitPath)) {{
        if ($taskType -eq 'schtasks') {{ & schtasks.exe /End /TN $taskName 2>&1 | Out-Null }}
        else {{ Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }}
        $result = @{{stdout=''; stderr='Timed out waiting for the interactive-user command.'; exitcode=-1; timed_out=$true}}
    }} else {{
        # The task may still hold the result files open for a moment after creating them.
        $result = $null
        for ($i = 0; $i -lt 40 -and $null -eq $result; $i++) {{
            try {{
                $stdoutText = ''
                $stderrText = ''
                if (Test-Path -LiteralPath $stdoutPath) {{ $stdoutText = [IO.File]::ReadAllText($stdoutPath) }}
                if (Test-Path -LiteralPath $stderrPath) {{ $stderrText = [IO.File]::ReadAllText($stderrPath) }}
                $exitText = [IO.File]::ReadAllText($exitPath).Trim()
                if (-not $exitText) {{ throw 'exit code not written yet' }}
                $result = @{{
                    stdout=$stdoutText;
                    stderr=$stderrText;
                    exitcode=[int]$exitText;
                    timed_out=$false
                }}
            }} catch {{ Start-Sleep -Milliseconds 250 }}
        }}
        if ($null -eq $result) {{ throw 'Could not read the interactive command result files.' }}
    }}
    $result | ConvertTo-Json -Compress | Write-Output
}} finally {{
    if ($taskRegistered) {{
        if ($taskType -eq 'schtasks') {{ & schtasks.exe /Delete /TN $taskName /F 2>&1 | Out-Null }}
        else {{ Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue }}
    }}
    Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction SilentlyContinue
}}
""".strip()
    outer_script_path = rf"C:\ProgramData\HeraLoadTest\interactive_{task_id}.ps1"
    _execute_proxmox_command(
        settings, client_ip,
        ["cmd.exe", "/c", "mkdir C:\\ProgramData\\HeraLoadTest"],
        15,
    )
    try:
        _execute_proxmox_command(
            settings, client_ip,
            ["cmd.exe", "/c", f"mkdir {work_dir}"],
            15,
        )
        _write_proxmox_file(settings, client_ip, inner_script_path, inner_script)
        _write_proxmox_file(settings, client_ip, outer_script_path, outer_script)
        output, error_output, exit_status, timed_out = _execute_proxmox_command(
            settings,
            client_ip,
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", outer_script_path],
            timeout + 30,
        )
    finally:
        _execute_proxmox_command(
            settings, client_ip,
            ["cmd.exe", "/c", "del", "/f", "/q", outer_script_path],
            15,
        )
    if timed_out or exit_status != 0:
        return output, _readable_powershell_error(error_output), exit_status, timed_out
    try:
        result = json.loads(output.strip())
    except json.JSONDecodeError as exc:
        return "", f"Could not parse interactive command result: {exc}; {output[:500]}", -1, False
    return (
        result.get("stdout", ""),
        _readable_powershell_error(result.get("stderr", "")),
        int(result.get("exitcode", -1)),
        bool(result.get("timed_out", False)),
    )


def _email_from_setup_command(command_text):
    match = re.search(
        r"--setup-account=[^:\s]+:[^:\s]+:([^@\s]+@[^@:\s]+)@",
        command_text,
    )
    if not match:
        raise ValueError("Could not read the account email from the setup command")
    return match.group(1)


def _desktop_tauri_account_emails(output):
    """Return live account emails from a desktop-user database export."""
    b64 = "".join(
        line.strip()
        for line in (output or "").splitlines()
        if line.strip() and not line.startswith("PROFILE=")
    )
    if not b64:
        return []
    raw = base64.b64decode(b64)
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as handle:
        handle.write(raw)
        handle.flush()
        connection = sqlite3.connect(handle.name)
        try:
            rows = connection.execute(
                "SELECT user_registered_email FROM accounts"
            ).fetchall()
        finally:
            connection.close()
    return [row[0] for row in rows if row and row[0]]


def _desktop_tauri_account_present(settings, client_ip, email, strict=False):
    """Return whether the logged-in desktop user's Tauri database contains email."""
    script = (
        "$p=Join-Path $env:USERPROFILE '.hera\\client_db.sqlite'; "
        "Write-Output ('PROFILE=' + $env:USERPROFILE); "
        "if ($env:USERPROFILE -match 'systemprofile') { Write-Output 'ACCOUNT_WRONG_PROFILE'; exit 3 }; "
        "if (-not (Test-Path -LiteralPath $p)) { Write-Output 'ACCOUNT_MISSING'; exit 2 }; "
        "[Convert]::ToBase64String([IO.File]::ReadAllBytes($p))"
    )
    encoded = _encode_ps_command(script)
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", encoded,
    ]
    output, error_output, exit_status, timed_out = _execute_proxmox_interactive_command(
        settings, client_ip, command, 40
    )
    detail = " ".join(
        line.strip()
        for line in f"{output}\n{error_output}".splitlines()
        if line.strip() and not line.strip().startswith("U0") and "PROFILE=" in line or line.strip().startswith("ACCOUNT_")
    )
    profile = next((line.strip() for line in (output or "").splitlines() if line.startswith("PROFILE=")), "")
    if timed_out:
        if strict:
            raise RuntimeError(f"Timed out checking the desktop Tauri account database. {profile}".strip())
        return False, f"Timed out checking the desktop Tauri account database. {profile}".strip()
    if "ACCOUNT_WRONG_PROFILE" in (output or ""):
        if strict:
            raise RuntimeError(f"Account check ran in the SYSTEM profile. {profile}".strip())
        return False, profile or "Account check ran in the SYSTEM profile."
    if exit_status == 2 or "ACCOUNT_MISSING" in (output or ""):
        return False, profile or "Desktop Tauri database does not exist."
    if strict and exit_status != 0:
        raise RuntimeError(
            f"Could not check the desktop Tauri account database: "
            f"{error_output.strip() or output.strip() or f'exit {exit_status}'}"
        )
    try:
        if strict and not any(
            line.strip() and not line.startswith("PROFILE=")
            for line in (output or "").splitlines()
        ):
            raise ValueError("Database export was empty.")
        emails = _desktop_tauri_account_emails(output)
    except Exception as exc:
        if strict:
            raise RuntimeError(f"Could not read the desktop Tauri database. {profile} {exc}".strip()) from exc
        return False, f"Could not read the desktop Tauri database. {profile} {exc}".strip()
    wanted = email.strip().lower()
    if any((stored or "").strip().lower() == wanted for stored in emails):
        return True, profile or "ACCOUNT_PRESENT"
    return False, f"{profile} live accounts: {', '.join(emails) or '<none>'}".strip()


def _desktop_tauri_account_site_address(settings, client_ip, email):
    """Read the site address actually stored for one local Tauri account."""
    script = r"""
$p = Join-Path $env:USERPROFILE '.hera\client_db.sqlite'
if (-not (Test-Path -LiteralPath $p)) { throw 'Desktop Tauri database is missing.' }
$fs = [IO.File]::Open($p, 'Open', 'Read', 'ReadWrite')
try {
    $bytes = New-Object byte[] $fs.Length
    [void]$fs.Read($bytes, 0, $bytes.Length)
    Write-Output ('PROFILE=' + $env:USERPROFILE)
    Write-Output ([Convert]::ToBase64String($bytes))
} finally { $fs.Close() }
""".strip()
    encoded = _encode_ps_command(script)
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", encoded,
    ]
    output, error_output, exit_status, timed_out = _execute_proxmox_credential_command(
        settings, client_ip, command, 40
    )
    if timed_out or exit_status != 0:
        detail = _readable_powershell_error(error_output) or output or f"exit {exit_status}"
        raise RuntimeError(f"Could not inspect the local Tauri account database: {detail}")
    encoded_database = "".join(
        line.strip()
        for line in (output or "").splitlines()
        if line.strip() and not line.startswith("PROFILE=")
    )
    if not encoded_database:
        raise RuntimeError("The local Tauri account database was empty.")
    try:
        raw_database = base64.b64decode(encoded_database, validate=True)
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as handle:
            handle.write(raw_database)
            handle.flush()
            connection = sqlite3.connect(handle.name)
            try:
                row = connection.execute(
                    "SELECT site_address FROM accounts WHERE lower(user_registered_email)=lower(?)",
                    (email.strip(),),
                ).fetchone()
            finally:
                connection.close()
    except (ValueError, sqlite3.Error) as exc:
        raise RuntimeError(f"Could not query the local Tauri account database: {exc}") from exc
    if not row or not row[0]:
        raise RuntimeError(f"{email} is not present in the local Tauri account database on {client_ip}.")
    return str(row[0]).strip()


def _wait_for_proxmox_local_mtls_proxy(settings, client_ip, timeout_seconds=30):
    script = (
        f"$deadline=(Get-Date).AddSeconds({int(timeout_seconds)}); "
        "while ((Get-Date) -lt $deadline) { "
        "$listeners=Get-NetTCPConnection -LocalPort 5555 -State Listen -ErrorAction SilentlyContinue; "
        "foreach ($listener in $listeners) { "
        "$process=Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue; "
        "if ($process.ProcessName -eq 'TpmMtlsProxy') { exit 0 } }; "
        "Start-Sleep -Seconds 1 }; exit 1"
    )
    encoded_script = _encode_ps_command(script)
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script]
    _output, _error_output, exit_status, timed_out = _execute_proxmox_command(
        settings, client_ip, command, timeout_seconds + 5
    )
    return not timed_out and exit_status == 0


def _account_delete_confirmed(output, error_output):
    text = f"{output}\n{error_output}".lower()
    return (
        "account delete successful" in text
        or "account_delete_successful" in text
        or "no local tauri account found" in text
    )


def _proxmox_tauri_visible_in_session(settings, client_ip, session_id=1):
    """Return whether Tauri is open in the desktop session as the logged-in user."""
    script = (
        "$ok=$false; $lines=@(); "
        "Get-CimInstance Win32_Process -Filter \"Name='tauri-client-app.exe'\" | ForEach-Object { "
        "$owner=Invoke-CimMethod -InputObject $_ -MethodName GetOwner; "
        "$user=[string]$owner.User; $lines += ('PID=' + $_.ProcessId + ' SESSION=' + $_.SessionId + ' USER=' + $user); "
        f"if ($_.SessionId -eq {int(session_id)} -and $user -and $user -ne 'SYSTEM') {{ $ok=$true }} "
        "}; "
        "if ($ok) { Write-Output 'TAURI_VISIBLE'; $lines | Write-Output; exit 0 }; "
        "Write-Output 'TAURI_NOT_VISIBLE'; $lines | Write-Output; exit 1"
    )
    encoded = _encode_ps_command(script)
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    output, error_output, exit_status, timed_out = _execute_proxmox_command(
        settings, client_ip, command, 20
    )
    visible = (not timed_out) and exit_status == 0 and "TAURI_VISIBLE" in (output or "")
    detail = " ".join(part.strip() for part in (output, error_output) if part and part.strip())
    return visible, detail


def _proxmox_tauri_processes_stopped(settings, client_ip):
    """Confirm both Tauri and its proxy exited before a retry can launch again."""
    # tasklist avoids PowerShell start-up, which can exceed the timeout on busy VMs.
    output, _error_output, exit_status, timed_out = _execute_proxmox_command(
        settings, client_ip, ["tasklist.exe", "/FO", "CSV", "/NH"], 30
    )
    if timed_out or exit_status != 0 or not output:
        return False
    running = output.lower()
    return '"tauri-client-app.exe"' not in running and '"tpmmtlsproxy.exe"' not in running


def _stop_proxmox_tauri_windows(settings, client_ip):
    """Close Tauri and its proxy before another login attempt."""
    for process_name in ("tauri-client-app.exe", "TpmMtlsProxy.exe", "msedgewebview2.exe"):
        _execute_proxmox_command(
            settings,
            client_ip,
            ["taskkill.exe", "/F", "/IM", process_name, "/T"],
            10,
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _proxmox_tauri_processes_stopped(settings, client_ip):
            return
        time.sleep(1)


def _proxmox_reboot_vm(settings, client_ip):
    """Reboot a Proxmox VM so Windows auto-logon can create a desktop session."""
    vm = settings.get("proxmox_vms_by_ip", {}).get(client_ip)
    if not vm:
        raise RuntimeError(f"No Proxmox VM metadata found for {client_ip}.")
    api_base = _proxmox_api_base(settings)
    node = quote(vm["node"], safe="")
    headers = _proxmox_headers(settings)
    response = requests.post(
        f"{api_base}/nodes/{node}/qemu/{vm['vmid']}/status/reboot",
        headers=headers,
        data={},
        verify=False,
        timeout=30,
    )
    response.raise_for_status()


def _enable_windows_autologon(settings, client_ip):
    """Store the VM account so the next boot opens a desktop session."""
    username, password = _resolve_windows_logon(settings, client_ip)
    payload = base64.b64encode(json.dumps({
        "user": username,
        "password": password,
    }).encode("utf-8")).decode("ascii")
    script = f"""
$ErrorActionPreference = 'Stop'
$spec = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) | ConvertFrom-Json
$path = 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon'
Set-ItemProperty -Path $path -Name AutoAdminLogon -Value '1'
Set-ItemProperty -Path $path -Name DefaultUserName -Value ([string]$spec.user)
Set-ItemProperty -Path $path -Name DefaultDomainName -Value $env:COMPUTERNAME
Set-ItemProperty -Path $path -Name DefaultPassword -Value ([string]$spec.password)
Set-ItemProperty -Path $path -Name ForceAutoLogon -Value '1' -ErrorAction SilentlyContinue
Remove-ItemProperty -Path $path -Name AutoLogonCount -ErrorAction SilentlyContinue
Write-Output ('AUTOLOGON=' + $spec.user)
""".strip()
    remote = r"C:\ProgramData\HeraLoadTest\autologon.ps1"
    _execute_proxmox_command(
        settings, client_ip,
        ["cmd.exe", "/c", "mkdir C:\\ProgramData\\HeraLoadTest"],
        15,
    )
    try:
        _write_proxmox_file(settings, client_ip, remote, script)
        output, error_output, exit_status, timed_out = _execute_proxmox_command(
            settings, client_ip,
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", remote],
            30,
        )
    finally:
        _execute_proxmox_command(
            settings, client_ip,
            ["cmd.exe", "/c", "del /f /q C:\\ProgramData\\HeraLoadTest\\autologon.ps1"],
            15,
        )
    if timed_out or exit_status != 0:
        detail = _readable_powershell_error(error_output) or output or f"exit {exit_status}"
        raise RuntimeError(f"Could not enable Windows auto-logon on {client_ip}: {detail}")
    return username


def _ensure_windows_console_session(settings, client_ip, timeout_seconds=210):
    """Return the desktop user, rebooting into auto-logon when the login screen is idle."""
    user, error = _windows_console_user(settings, client_ip)
    if user:
        return user
    if error and "guest-exec" in error.lower():
        raise RuntimeError(error)
    print(f"    - No Windows desktop on {client_ip}. Signing in admin and rebooting.")
    _enable_windows_autologon(settings, client_ip)
    _proxmox_reboot_vm(settings, client_ip)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(8)
        user, _error = _windows_console_user(settings, client_ip)
        if user:
            print(f"    - Desktop session ready for {user}.")
            time.sleep(8)
            return user
    raise RuntimeError(f"Windows desktop did not appear on {client_ip} after reboot.")


def _proxmox_login_script(parts):
    """Build a short desktop launcher. The guest-agent command must stay small."""
    payload = base64.b64encode(json.dumps([str(part) for part in parts]).encode("utf-8")).decode("ascii")
    return f"""
$ErrorActionPreference = 'Stop'
$spec = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) | ConvertFrom-Json
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = [string]$spec[0]
$psi.UseShellExecute = $true
$psi.Arguments = (@($spec | Select-Object -Skip 1 | ForEach-Object {{ [string]$_ }}) -join ' ')
[void][Diagnostics.Process]::Start($psi)
Write-Output 'TAURI_STARTED'
exit 0
""".strip()


def _execute_proxmox_login_with_proxy_retry(settings, client_ip, command):
    """Open Tauri on the logged-in desktop. Do not launch it as SYSTEM."""
    if isinstance(command, str):
        command = ["cmd.exe", "/c", command]
    parts = [str(part) for part in command]
    executable = parts[0].replace("\\", "/").rsplit("/", 1)[-1].lower() if parts else ""
    if executable == "psexec.exe":
        parts = parts[1:]
        while parts and parts[0].lower() in {"-i", "/accepteula", "-accepteula", "-d"}:
            parts = parts[2:] if parts[0].lower() == "-i" and len(parts) > 1 else parts[1:]
    try:
        _ensure_windows_console_session(settings, client_ip)
    except Exception as exc:
        return "", str(exc), -1, False

    remote_script = r"C:\ProgramData\HeraLoadTest\login.ps1"
    _execute_proxmox_command(
        settings, client_ip,
        ["cmd.exe", "/c", "mkdir C:\\ProgramData\\HeraLoadTest"],
        15,
    )
    _execute_proxmox_command(
        settings, client_ip,
        ["icacls.exe", r"C:\ProgramData\HeraLoadTest", "/grant", "Users:(OI)(CI)M"],
        15,
    )
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", remote_script,
    ]
    _stop_proxmox_tauri_windows(settings, client_ip)
    for _attempt in range(2):
        if _proxmox_tauri_processes_stopped(settings, client_ip):
            break
        _stop_proxmox_tauri_windows(settings, client_ip)
    else:
        if not _proxmox_tauri_processes_stopped(settings, client_ip):
            return "", "Existing Tauri or mTLS proxy processes could not be stopped.", -1, False
    output = error_output = ""
    exit_status = -1
    timed_out = False
    for attempt in range(2):
        _write_proxmox_file(
            settings, client_ip, remote_script,
            _proxmox_login_script(parts),
        )
        output, error_output, exit_status, timed_out = _execute_proxmox_interactive_command(
            settings, client_ip, command, 40
        )
        if timed_out or exit_status != 0:
            return output, error_output, exit_status, timed_out
        failure_detail = "TpmMtlsProxy did not listen on 127.0.0.1:5555."
        if _wait_for_proxmox_local_mtls_proxy(settings, client_ip, 60):
            visible, detail = _proxmox_tauri_visible_in_session(settings, client_ip)
            if visible:
                time.sleep(5)
                visible, detail = _proxmox_tauri_visible_in_session(settings, client_ip)
                proxy_ready = _wait_for_proxmox_local_mtls_proxy(settings, client_ip, 1)
                if visible and proxy_ready:
                    return output, error_output, exit_status, False
                if not visible:
                    failure_detail = detail or "Tauri exited shortly after launch."
                elif not proxy_ready:
                    failure_detail = "Tauri remained visible, but TpmMtlsProxy stopped listening on 127.0.0.1:5555."
                else:
                    failure_detail = "Tauri or TpmMtlsProxy exited shortly after launch."
            else:
                failure_detail = detail or "Tauri is not visible in session 1 as the desktop user."
        _stop_proxmox_tauri_windows(settings, client_ip)
        if attempt == 0 and _proxmox_tauri_processes_stopped(settings, client_ip):
            print(f"    - {failure_detail} Retrying once.")
            continue
        return output, failure_detail, -1, False

    return output, "Tauri or TpmMtlsProxy did not remain healthy after retry.", -1, False


def _read_remote_command(stdout, stderr, timeout_seconds=None):
    channel = stdout.channel
    if timeout_seconds:
        channel.settimeout(timeout_seconds)
    try:
        output = stdout.read().decode('utf-8', errors='replace')
        error_output = stderr.read().decode('utf-8', errors='replace')
        return output, error_output, channel.recv_exit_status(), False
    except socket.timeout:
        channel.close()
        return "", "timed out waiting for the Windows command", -1, True


def _wait_for_local_mtls_proxy(ssh_client, timeout_seconds=30):
    command = (
        'powershell -NoProfile -NonInteractive -Command '
        f'"$deadline=(Get-Date).AddSeconds({int(timeout_seconds)}); '
        'while ((Get-Date) -lt $deadline) { '
        'if (Get-NetTCPConnection -LocalPort 5555 -State Listen -ErrorAction SilentlyContinue) { exit 0 }; '
        'Start-Sleep -Seconds 1 }; exit 1"'
    )
    _stdin, stdout, stderr = ssh_client.exec_command(command)
    _output, _error_output, exit_status, timed_out = _read_remote_command(
        stdout, stderr, timeout_seconds + 5
    )
    return not timed_out and exit_status == 0


def _stop_client_process_tree(ssh_client, process_id):
    _stdin, stdout, stderr = ssh_client.exec_command(
        f"taskkill /PID {int(process_id)} /T /F"
    )
    _read_remote_command(stdout, stderr, 10)


def _execute_login_with_proxy_retry(ssh_client, command):
    for attempt in range(2):
        _stdin, stdout, stderr = ssh_client.exec_command(command)
        output, error_output, exit_status, timed_out = _read_remote_command(
            stdout, stderr, 20
        )
        if timed_out:
            return output, error_output, exit_status, timed_out
        if exit_status < 0:
            return output, error_output, exit_status, False
        if _wait_for_local_mtls_proxy(ssh_client):
            return output, error_output, exit_status, False

        if attempt == 0 and exit_status > 0:
            print("    - Local mTLS proxy did not start; restarting Tauri once.")
            _stop_client_process_tree(ssh_client, exit_status)
            time.sleep(2)
            continue

        message = "TpmMtlsProxy did not listen on 127.0.0.1:5555."
        if attempt > 0:
            message = "TpmMtlsProxy did not listen on 127.0.0.1:5555 after retry."
        error_output = f"{error_output}\n{message}".strip()
        return output, error_output, -1, False

    return "", "TpmMtlsProxy did not listen on 127.0.0.1:5555.", -1, False


def _proxmox_desktop_username(settings, client_ip):
    """Return the Windows user logged into the client desktop, without a password."""
    cache = settings.setdefault("desktop_username_by_ip", {})
    if cache.get(client_ip):
        return cache[client_ip]
    output, _error_output, exit_status, timed_out = _execute_proxmox_interactive_command(
        settings, client_ip, ["whoami.exe"], 20
    )
    username = ""
    if not timed_out and exit_status == 0 and output:
        username = output.strip().splitlines()[-1].strip()
        if "\\" in username:
            username = username.split("\\")[-1].strip()
    if username:
        cache[client_ip] = username
    return username


def _apply_client_credentials_to_asset(settings, client_ip):
    """Use the client desktop login for the asset. Proxmox assets share that account."""
    if settings.get("ip_source") != "proxmox" or settings.get("proxmox_asset_creds"):
        return
    username = _proxmox_desktop_username(settings, client_ip)
    password = settings.get("client_pass") or settings.get("asset_pass") or ""
    if username:
        settings["asset_user"] = username
        print(f"    - Asset login user is the client desktop user {username}.")
    if password:
        settings["asset_pass"] = password
        settings["asset_encoded_pass"] = quote(password, safe="")


def _asset_session_user(settings, asset_ip, timeout_seconds=120):
    """Wait for the active HERA RDP session on the asset and return its DOMAIN\\user."""
    script = (
        "$active = & quser.exe 2>$null | Select-Object -Skip 1 | ForEach-Object { "
        "if ($_ -match '\\s(\\d+)\\s+Active\\s') { [int]$matches[1] } } | Select-Object -First 1; "
        "if ($null -eq $active) { exit 1 }; "
        "$p=Get-CimInstance Win32_Process -Filter (\"Name='explorer.exe' AND SessionId=\" + $active) | Select-Object -First 1; "
        "if ($p) { $o=Invoke-CimMethod -InputObject $p -MethodName GetOwner; "
        "if ($o.User) { Write-Output ($o.Domain + '\\' + $o.User); exit 0 } }; exit 1"
    )
    encoded = _encode_ps_command(script)
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        output, _error, status, timed_out = _execute_proxmox_command(settings, asset_ip, command, 20)
        user = (output or "").strip().splitlines()[-1].strip() if (output or "").strip() else ""
        if not timed_out and status == 0 and user:
            return user
        time.sleep(5)
    return ""


def _open_asset_rendering(settings, asset_ip):
    """Open Edge full screen in the asset's HERA session: a clock (low) or moving content (high)."""
    mode = settings["asset_rendering_mode"]
    url = settings["low_rendering_url"] if mode == "low" else settings["high_rendering_url"]
    user = _asset_session_user(settings, asset_ip)
    if not user:
        message, detail = _describe_failure(
            f"Asset {asset_ip}", "No desktop session appeared on the asset."
        )
        raise StepError(message, detail)
    payload = base64.b64encode(url.encode("utf-8")).decode("ascii")
    # Errors are printed as one marker line: letting PowerShell throw makes the
    # interactive runner's stderr redirect wrap every line in NativeCommandError noise.
    script = f"""
$ErrorActionPreference = 'Stop'
try {{
    $url = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))
    $edge = @('C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe', 'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe') | Where-Object {{ Test-Path -LiteralPath $_ }} | Select-Object -First 1
    if (-not $edge) {{ throw 'Microsoft Edge is not installed on the asset.' }}
    $started = $false
    for ($attempt = 1; $attempt -le 2 -and -not $started; $attempt++) {{
        # Kiosk flags are ignored when Edge is already running.
        Get-Process msedge -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
        Start-Process -FilePath $edge -ArgumentList @('--kiosk', $url, '--edge-kiosk-type=fullscreen', '--no-first-run', '--autoplay-policy=no-user-gesture-required')
        # Edge can take a while to start on a busy VM, so wait for it instead of checking once.
        $waitUntil = (Get-Date).AddSeconds(20)
        while ((Get-Date) -lt $waitUntil -and -not (Get-Process msedge -ErrorAction SilentlyContinue)) {{ Start-Sleep -Seconds 1 }}
        Start-Sleep -Seconds 4
        $started = [bool](Get-Process msedge -ErrorAction SilentlyContinue)
    }}
    if (-not $started) {{ throw 'Edge did not stay open on the asset.' }}
    Write-Output 'ASSET_RENDERING_STARTED'
}} catch {{
    Write-Output ('ASSET_RENDERING_ERROR: ' + $_.Exception.Message)
    exit 1
}}
""".strip()
    encoded = _encode_ps_command(script)
    output, error, status, timed_out = _execute_proxmox_interactive_command(
        settings, asset_ip,
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        120, run_as_user=user,
    )
    if not timed_out and status == 0 and "ASSET_RENDERING_STARTED" in (output or ""):
        return user
    marker = re.search(r"ASSET_RENDERING_ERROR:\s*(.+)", output or "")
    if marker:
        raw = marker.group(1).strip()
    else:
        raw = (error or output or f"exit {status}").strip()
        if timed_out:
            raw = f"Timed out. {raw}"
    message, detail = _describe_failure(f"Asset {asset_ip}", raw)
    raise StepError(message, detail)


def _disable_asset_setup_screen(settings, asset_ip):
    """Turn off the Windows "almost done setting up your PC" screen, which covers the HERA session.

    Windows reads this only at sign-in, so it must run before HERA opens the RDP session.
    """
    payload = base64.b64encode(settings["asset_user"].encode("utf-8")).decode("ascii")
    script = f"""
$ErrorActionPreference = 'Stop'
$user = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))
$sid = (New-Object Security.Principal.NTAccount($env:COMPUTERNAME, $user)).Translate([Security.Principal.SecurityIdentifier]).Value
$root = "Registry::HKEY_USERS\\$sid"
$loaded = $false
if (-not (Test-Path $root)) {{
    $profilePath = (Get-ItemProperty "HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList\\$sid" -ErrorAction SilentlyContinue).ProfileImagePath
    if (-not $profilePath -or -not (Test-Path "$profilePath\\NTUSER.DAT")) {{ Write-Output 'NO_PROFILE'; exit 0 }}
    & reg.exe load 'HKU\\HeraLoadTest' "$profilePath\\NTUSER.DAT" | Out-Null
    $root = 'Registry::HKEY_USERS\\HeraLoadTest'
    $loaded = $true
}}
try {{
    $engagement = "$root\\Software\\Microsoft\\Windows\\CurrentVersion\\UserProfileEngagement"
    New-Item -Path $engagement -Force | Out-Null
    Set-ItemProperty -Path $engagement -Name ScoobeSystemSettingEnabled -Value 0 -Type DWord
    $content = "$root\\Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager"
    New-Item -Path $content -Force | Out-Null
    Set-ItemProperty -Path $content -Name 'SubscribedContent-310093Enabled' -Value 0 -Type DWord
}} finally {{
    if ($loaded) {{ [gc]::Collect(); & reg.exe unload 'HKU\\HeraLoadTest' | Out-Null }}
}}
# HERA would otherwise reconnect to an old session that already shows the setup screen.
$ErrorActionPreference = 'Continue'
$sessionIds = @(& quser.exe 2>$null | Select-Object -Skip 1 | ForEach-Object {{
    if ($_ -match '^\\s*>?(\\S+)\\s.*?\\s(\\d+)\\s+(Active|Disc)') {{ if ($matches[1] -ieq $user) {{ $matches[2] }} }}
}})
foreach ($id in $sessionIds) {{ & logoff.exe $id 2>$null }}
if ($sessionIds.Count -gt 0) {{ Start-Sleep -Seconds 5 }}
Write-Output 'SETUP_SCREEN_DISABLED'
""".strip()
    encoded = _encode_ps_command(script)
    output, error, status, timed_out = _execute_proxmox_command(
        settings, asset_ip,
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        60,
    )
    if timed_out or status != 0:
        message, _detail = _describe_failure(
            f"Asset {asset_ip}",
            _readable_powershell_error(error) or output or ("Timed out." if timed_out else f"exit {status}"),
        )
        print(f"    - {message} The Windows setup screen could not be turned off; continuing.")


def _server_setup_emails(settings):
    """Return the test users on the HERA server, or None when the server cannot be checked."""
    try:
        from server_cleanup import _login, _request, split_admin_credentials
        username, password = split_admin_credentials(settings.get("admin_credentials"))
        token = settings.get("admin_access_token") or _login(settings, username, password)
        settings["admin_access_token"] = token
        users = _request(settings, "GET", "/api/admin/users", token=token) or []
    except Exception as exc:
        message, _detail = _describe_failure("", str(exc))
        print(f"Could not check the existing HERA users ({message}). Accounts recorded earlier are assumed to still exist.")
        return None
    emails = set()
    prefix = settings.get("account_email_user", "").strip().lower()
    domain = settings.get("account_email_domain", "").strip().lower()
    for user in users:
        email = (user.get("email") or user.get("username") or "").strip().lower()
        if prefix and domain and email.startswith(prefix) and email.endswith("@" + domain):
            emails.add(email)
    return emails


def _live_setup_records(inventory):
    """Accounts recorded as created and not torn down: the newest `created` record for each email."""
    live = {}
    for run in inventory.get("setup_runs", []):
        if run.get("source", inventory.get("source")) != "proxmox":
            continue
        for record in run.get("assignments", []):
            email = (record.get("email") or "").strip().lower()
            if email and record.get("status") == "created":
                live[email] = record
    return sorted(live.values(), key=lambda record: int(record.get("counter") or 0))


def _plan_setup_assignments(settings, client_ips, asset_ips, assets_per_client, live_records, taken_emails):
    """Plan accounts only for clients that have none, without renumbering the existing ones.

    Returns (new_clients, reused_clients, assignments, skipped_emails). Numbers already used by a live
    account or by a user on the server are never handed out again, so adding VMs cannot shift existing
    accounts. skipped_emails are server users without a recorded VM whose numbers fall inside the plan.
    """
    prefix = settings.get("account_email_user", "").strip().lower()
    domain = settings.get("account_email_domain", "").strip().lower()
    used_numbers = {int(record["counter"]) for record in live_records if record.get("counter")}
    number_pattern = re.compile(rf"^{re.escape(prefix)}(\d+)@{re.escape(domain)}$")
    for email in taken_emails:
        match = number_pattern.match(email)
        if match:
            used_numbers.add(int(match.group(1)))

    live_clients = {record.get("client_ip") for record in live_records}
    new_clients = [ip for ip in client_ips if ip not in live_clients]
    reused_clients = [ip for ip in client_ips if ip in live_clients]
    # Prefer assets that no live account uses; with the same count as before this matches the old layout.
    used_assets = {record.get("asset_ip") for record in live_records}
    pool = [ip for ip in asset_ips if ip not in used_assets] or list(asset_ips)

    assignments = []
    number = 0
    for client_index, client_ip in enumerate(new_clients):
        for asset_index in range(assets_per_client):
            number += 1
            while number in used_numbers:
                number += 1
            asset_ip = pool[(client_index + asset_index * len(new_clients)) % len(pool)]
            assignments.append({
                "counter": number,
                "client_ip": client_ip,
                "asset_ip": asset_ip,
                "email": f'{settings["account_email_user"]}{number}@{settings["account_email_domain"]}',
                "asset_display_name": asset_display_name(settings, number),
                "status": "pending",
                "tx_certificate_uploaded": None,
            })
    live_numbers = {int(record["counter"]) for record in live_records if record.get("counter")}
    highest = max((assignment["counter"] for assignment in assignments), default=0)
    skipped_emails = sorted(
        email for email in taken_emails
        if (match := number_pattern.match(email))
        and int(match.group(1)) not in live_numbers and int(match.group(1)) < highest
    )
    return new_clients, reused_clients, assignments, skipped_emails


def _execute_on_clients(get_tauri_command, settings, max_assets=None, sleep_seconds=None, on_success=None, command_timeout=None, max_clients=None, record_setup=False):
    """Execute a Tauri command over SSH or the Proxmox QEMU Guest Agent."""
    global load_test_counter
    sleep_seconds = sleep_seconds if sleep_seconds is not None else 1

    client_ips, asset_ips = _get_target_ips(settings)
    discovered_client_ips = list(client_ips)
    discovered_asset_ips = list(asset_ips)
    max_assets_int = None
    if max_assets is not None:
        try:
            max_assets_int = int(max_assets)
            if max_assets_int > 0:
                asset_ips = asset_ips[:max_assets_int]
        except ValueError:
            print(f"Invalid max_assets value '{max_assets}', ignoring limit.")

    if max_clients is not None:
        try:
            max_clients_int = int(max_clients)
            if max_clients_int > 0:
                client_ips = client_ips[:max_clients_int]
        except ValueError:
            print(f"Invalid client limit '{max_clients}', ignoring limit.")

    if not client_ips or not asset_ips:
        print("Could not proceed without client and asset IPs from the selected source.")
        return

    use_proxmox_agent = settings["ip_source"] == "proxmox"
    client_assets_per_client = settings["client_assets_factor"]
    if client_assets_per_client <= 0:
        client_assets_per_client = 1
    if max_assets_int is not None and max_assets_int > 0:
        client_count = max(1, (max_assets_int + client_assets_per_client - 1) // client_assets_per_client)
        client_ips = client_ips[:client_count]
    client_assets_by_client = [
        [
            asset_ips[(client_index + asset_index * len(client_ips)) % len(asset_ips)]
            for asset_index in range(client_assets_per_client)
        ]
        for client_index in range(len(client_ips))
    ]
    setup_run = None
    existing_emails = set()
    if record_setup:
        inventory = _load_ip_inventory()
        inventory.update({
            "version": 1,
            "source": settings["ip_source"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "clients": [_inventory_vm(ip, settings) for ip in discovered_client_ips],
            "assets": [_inventory_vm(ip, settings) for ip in discovered_asset_ips],
        })
        if use_proxmox_agent:
            server_emails = _server_setup_emails(settings)
            live_records = _live_setup_records(inventory)
            if server_emails is not None:
                for record in live_records:
                    if record["email"].strip().lower() not in server_emails:
                        print(
                            f"{record['email']} is recorded as created but no longer exists on the HERA server "
                            "(deleted manually?). It will be created again."
                        )
                live_records = [r for r in live_records if r["email"].strip().lower() in server_emails]
            live_emails = {r["email"].strip().lower() for r in live_records}
            existing_emails = set(server_emails or ()) | live_emails
            new_clients, reused_clients, planned_assignments, skipped_emails = _plan_setup_assignments(
                settings, client_ips, asset_ips, client_assets_per_client, live_records, existing_emails
            )
            if reused_clients:
                print(
                    f"{len(reused_clients)} of the {len(client_ips)} selected client(s) already have an account "
                    "and are left unchanged."
                )
            if skipped_emails:
                print(
                    "These users already exist on the HERA server without a recorded VM, so their numbers are skipped: "
                    f"{', '.join(skipped_emails)}. Delete them in the HERA UI if they are leftovers."
                )
            if not new_clients:
                print("Every selected client already has an account. There is nothing new to create.")
                _save_ip_inventory(inventory)
                return
            client_ips = new_clients
            client_assets_by_client = [
                [a["asset_ip"] for a in planned_assignments if a["client_ip"] == client_ip]
                for client_ip in client_ips
            ]
        else:
            planned_assignments = []
            counter = load_test_counter
            for client_index, client_ip in enumerate(client_ips):
                for asset_ip in client_assets_by_client[client_index]:
                    counter += 1
                    email = f'{settings["account_email_user"]}{counter}@{settings["account_email_domain"]}'
                    planned_assignments.append({
                        "counter": counter,
                        "client_ip": client_ip,
                        "asset_ip": asset_ip,
                        "email": email,
                        "asset_display_name": asset_display_name(settings, counter),
                        "status": "pending",
                        "tx_certificate_uploaded": None,
                    })
            server_emails = _server_setup_emails(settings)
            existing_emails = set(server_emails or ())
            for run in inventory.get("setup_runs", []):
                for record in run.get("assignments", []):
                    if record.get("status") == "created" and record.get("email"):
                        existing_emails.add(record["email"].strip().lower())
            if existing_emails:
                print(f"Leaving {len(existing_emails)} existing account(s) in place.")
        setup_run = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "source": settings["ip_source"],
            "status": "in_progress",
            "client_assets_factor": client_assets_per_client,
            "assignments": planned_assignments,
        }
        inventory.setdefault("setup_runs", []).append(setup_run)
        _save_ip_inventory(inventory)
        print(f"Saved ordered IP inventory and setup plan to {IP_INVENTORY_PATH}.")
    setup_assignment_index = 0
    assets_processed = 0

    requested_assets = len(client_ips) * client_assets_per_client
    if len(asset_ips) < requested_assets:
        print(f"Reusing {len(asset_ips)} asset IP(s) to provide {client_assets_per_client} asset(s) per client.")
    elif len(asset_ips) > requested_assets:
        print(f"Using {requested_assets} asset(s); {len(asset_ips) - requested_assets} discovered asset(s) will be unused.")

    print(f"Starting process for {len(client_ips)} client(s), {client_assets_per_client} asset(s) per client.")

    for client_index, client_ip in enumerate(client_ips):
        if max_assets is not None and assets_processed >= max_assets:
            break

        print(f"\nProcessing client IP: {client_ip}")

        ssh_client = None

        try:
            if use_proxmox_agent:
                print("  - Using the Proxmox QEMU Guest Agent.")
            else:
                ssh_client = paramiko.SSHClient()
                ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                print(f"  - Connecting to {client_ip}...")
                ssh_client.connect(
                    hostname=client_ip,
                    username=settings["client_username"],
                    password=settings["client_pass"],
                    timeout=10
                )
                print("  - Connection successful.")

            for asset_ip in client_assets_by_client[client_index]:
                if max_assets is not None and assets_processed >= max_assets:
                    break
                setup_assignment = None
                try:
                    if setup_run is not None:
                        setup_assignment = setup_run["assignments"][setup_assignment_index]
                        # Planned numbers can skip existing accounts, so use the planned one.
                        load_test_counter = setup_assignment["counter"]
                    else:
                        load_test_counter += 1
                    setup_assignment_index += 1
                    existing_email = (setup_assignment or {}).get("email", "").strip().lower()
                    if existing_email and existing_email in existing_emails:
                        print(f"    - {setup_assignment['email']} already exists. Leaving that account in place.")
                        setup_assignment["status"] = "already_created"
                        _save_ip_inventory(inventory)
                        assets_processed += 1
                        continue

                    _apply_client_credentials_to_asset(settings, client_ip)
                    tauri_command = get_tauri_command(load_test_counter, asset_ip)
                    command_text = (
                        tauri_command if isinstance(tauri_command, str)
                        else " ".join(tauri_command)
                    )

                    print(f"    - Executing command for asset {asset_ip} on client {client_ip}.")

                    is_client_login = "psexec" in command_text.lower() and "--auto-login=" in command_text
                    if is_client_login:
                        if use_proxmox_agent:
                            output, error_output, exit_status, timed_out = _execute_proxmox_login_with_proxy_retry(
                                settings, client_ip, tauri_command
                            )
                        else:
                            output, error_output, exit_status, timed_out = _execute_login_with_proxy_retry(
                                ssh_client, tauri_command
                            )
                    elif use_proxmox_agent:
                        output, error_output, exit_status, timed_out = _execute_proxmox_interactive_command(
                            settings, client_ip, tauri_command, command_timeout
                        )
                    else:
                        stdin, stdout, stderr = ssh_client.exec_command(tauri_command)
                        output, error_output, exit_status, timed_out = _read_remote_command(
                            stdout, stderr, command_timeout
                        )
                    if timed_out:
                        message, _detail = _describe_failure(
                            f"Client {client_ip} (asset {asset_ip})", "Timed out."
                        )
                        print(f"    - Failed. {message} The HERA window on the VM is being closed.")
                        print("    - The account may be half-created or not deleted; check this VM before retrying.")
                        if use_proxmox_agent:
                            _execute_proxmox_command(
                                settings, client_ip,
                                ["taskkill.exe", "/F", "/IM", "tauri-client-app.exe"], 10
                            )
                        else:
                            kill_stdin, kill_stdout, kill_stderr = ssh_client.exec_command('taskkill /F /IM tauri-client-app.exe')
                            _read_remote_command(kill_stdout, kill_stderr, 10)
                        assets_processed += 1
                        if setup_assignment is not None:
                            setup_assignment["status"] = "failed"
                            setup_assignment["error"] = "command timed out"
                            _save_ip_inventory(inventory)
                        continue

                    # psexec -d returns a Process ID (positive integer) rather than 0.
                    is_detach = "psexec" in command_text.lower() and "-d" in command_text
                    is_account_delete = "--delete-account=" in command_text
                    is_account_setup = "--setup-account=" in command_text
                    detached_process_started = is_detach and exit_status >= 4
                    launcher_missing = "is not recognized" in f"{output}\n{error_output}".lower()
                    command_succeeded = exit_status == 0 or detached_process_started
                    local_account_detail = ""
                    if launcher_missing:
                        command_succeeded = False
                    if is_account_setup and not launcher_missing and "ACCOUNT SETUP SUCCESSFUL" in f"{output}\n{error_output}":
                        command_succeeded = True
                    if is_account_delete and _account_delete_confirmed(output, error_output):
                        command_succeeded = True
                    elif is_account_delete and not _account_delete_confirmed(output, error_output):
                        command_succeeded = False
                    if command_succeeded and is_account_setup:
                        if "ACCOUNT SETUP SUCCESSFUL" not in f"{output}\n{error_output}":
                            command_succeeded = False
                            local_account_detail = "Setup did not print ACCOUNT SETUP SUCCESSFUL."
                        elif use_proxmox_agent:
                            try:
                                setup_email = _email_from_setup_command(command_text)
                                present, local_account_detail = _desktop_tauri_account_present(
                                    settings, client_ip, setup_email
                                )
                            except Exception as exc:
                                present = False
                                setup_email = ""
                                local_account_detail = str(exc)
                            if not present:
                                command_succeeded = False
                                print("    - Account was not created in the desktop user's Tauri app database.")
                                if local_account_detail:
                                    print(f"    - {local_account_detail}")
                            else:
                                print(f"    - Verified local Tauri account {setup_email} in the desktop user profile.")

                    if command_succeeded:
                        tx_uploaded = None
                        if is_account_delete:
                            if output.strip():
                                print(f"    - {output.strip()}")
                            elif use_proxmox_agent:
                                print("    - Local account delete command exited successfully.")
                        if on_success and not on_success(load_test_counter, asset_ip, output, client_ip):
                            print(f"    - Setup command succeeded, but the TX certificate was not uploaded for asset {asset_ip}.")
                            tx_uploaded = False
                        elif on_success:
                            tx_uploaded = True
                        if setup_assignment is not None:
                            setup_assignment["status"] = "created"
                            setup_assignment["tx_certificate_uploaded"] = tx_uploaded
                            setup_assignment["local_account_verified"] = bool(
                                is_account_setup and use_proxmox_agent
                            )
                            _save_ip_inventory(inventory)
                        print(f"    - Success! (Exit Code / PID: {exit_status})")
                        assets_processed += 1
                    else:
                        failure_text = (
                            local_account_detail
                            or error_output.strip()
                            or output.strip()
                            or f"exit {exit_status}"
                        )
                        if setup_assignment is not None:
                            setup_assignment["status"] = "failed"
                            setup_assignment["local_account_verified"] = False
                            setup_assignment["error"] = failure_text
                            _save_ip_inventory(inventory)
                        message, _detail = _describe_failure(
                            f"Client {client_ip} (asset {asset_ip})", failure_text
                        )
                        print(f"    - Failed. {message}")
                        if is_account_delete and not _account_delete_confirmed(output, error_output):
                            print("    - The HERA client did not confirm that the account was deleted.")
                        assets_processed += 1
                        print(f"  - Sleep for {sleep_seconds} seconds to let mtls recover from the previous execution.")
                        time.sleep(sleep_seconds)
                        continue

                    print(f"  - Sleep for {sleep_seconds} seconds to let mtls recover from the previous execution.")
                    time.sleep(sleep_seconds)

                except StopIteration:
                    print("  - No more asset IPs available.")
                    break
                except Exception as exc:
                    # One failing VM must not stop the run for the remaining VMs.
                    message, detail = _describe_exception(f"Client {client_ip} (asset {asset_ip})", exc)
                    print(f"    - Failed. {message}")
                    if setup_assignment is not None:
                        setup_assignment["status"] = "failed"
                        setup_assignment["local_account_verified"] = False
                        setup_assignment["error"] = detail
                        _save_ip_inventory(inventory)
                    assets_processed += 1
                    continue

        except paramiko.AuthenticationException:
            print(f"  - Authentication failed for {client_ip}. Please check credentials.")
            continue
        except paramiko.SSHException as e:
            print(f"  - SSH connection error for {client_ip}: {e}")
            continue
        except Exception as e:
            print(f"  - An unexpected error occurred for client {client_ip}: {e}")
            continue
        finally:
            if ssh_client is not None:
                ssh_client.close()
                print(f"  - Connection to {client_ip} closed.")

    if setup_run is not None:
        statuses = [assignment["status"] for assignment in setup_run["assignments"]]
        setup_run["status"] = "completed" if all(status != "pending" for status in statuses) else "partial"
        _save_ip_inventory(inventory)


def run_client_setup(max_clients=None):
    """Configure clients and assets for setup workflow using load test settings."""
    global selected_client_limit
    selected_client_limit = max_clients
    try:
        settings = _read_load_test_settings()
    except Exception as exc:
        print(f"Could not load test settings: {exc}")
        return

    print(
        f"Web login is {settings['api_scheme']}://{settings['server_ip']}:"
        f"{settings['api_port']}/hera-web on RX-In. "
        f"Account lookup key is {settings['client_site_ip']}."
    )

    def get_setup_command(counter, asset_ip):
        if settings["ip_source"] == "proxmox":
            return [
                r"C:\Program Files\hera-client\tauri-client-app.exe",
                f'--admin-credentials={settings["admin_credentials"]}',
                f'--setup-account={settings["account_first_name"]}:{settings["account_last_name"]}:{settings["account_email_user"]}{counter}@{settings["account_email_domain"]}@{settings["server_ip"]}:{settings["account_pass"]}',
                f'--setup-assets={settings["asset_name"]}-{counter}:{settings["asset_protocol"]}:{asset_ip}:{settings["asset_port"]}:{settings["asset_pass"]}',
                f'--api-port={settings["api_port"]}',
                f'--mtls-port={settings["mtls_port"]}',
            ]
        return (
            f'cd "C:\\Program Files\\hera-client" && '
            f'tauri-client-app.exe '
            f'--admin-credentials={settings["admin_credentials"]} '
            f'--setup-account={settings["account_first_name"]}:{settings["account_last_name"]}:{settings["account_email_user"]}{counter}@{settings["account_email_domain"]}@{settings["server_ip"]}:{settings["account_pass"]} '
            f'--setup-assets={settings["asset_name"]}-{counter}:{settings["asset_protocol"]}:{asset_ip}:{settings["asset_port"]}:{settings["asset_pass"]} '
            f'--api-port={settings["api_port"]} '
            f'--mtls-port={settings["mtls_port"]}'
        )

    def upload_setup_certificate(counter, asset_ip, output, client_ip):
        if not tx_upload_configured(settings): # Just check if TX folder is configured
            print("    - TX upload skipped: set TX_HOST and TX_CERT_FOLDER to copy the External IP certificate.")
            return True
        email = f'{settings["account_email_user"]}{counter}@{settings["account_email_domain"]}'
        try:
            execute = None
            if settings["ip_source"] == "proxmox":
                execute = lambda command, timeout: _execute_proxmox_interactive_command(
                    settings, client_ip, command, timeout
                )
            certificate = extract_tx_certificate(output)
            if not certificate:
                certificate = export_client_tx_certificate(
                    settings, client_ip, email, execute=execute
                )
            upload_tx_certificate(settings, email, certificate)
            return True
        except Exception as exc:
            print(f"    - Failed to upload TX certificate for {email} after asset {asset_ip}: {exc}")
            return False

    _execute_on_clients(
        get_setup_command,
        settings,
        max_clients=max_clients,
        on_success=upload_setup_certificate,
        record_setup=True,
    )


def _teardown_recorded_proxmox_pairs(settings, max_clients=None):
    try:
        inventory = _load_ip_inventory()
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read {IP_INVENTORY_PATH}; refusing broad Proxmox teardown: {exc}")
        return True

    setup_runs = [
        run for run in inventory.get("setup_runs", [])
        if run.get("source", inventory.get("source")) == "proxmox"
    ]
    records = [
        record
        for run in setup_runs
        for record in run.get("assignments", [])
        if record.get("status") in {"created", "teardown_partial"}
    ]
    if not records:
        if setup_runs:
            print("No successfully created Proxmox setup records remain in the IP inventory; nothing to tear down.")
            return True
        print("No Proxmox setup manifest found; refusing to delete users from a freshly discovered IP list.")
        return True

    selected_clients = []
    for record in records:
        client_ip = record.get("client_ip")
        if client_ip and client_ip not in selected_clients:
            selected_clients.append(client_ip)
    if max_clients is not None:
        try:
            selected_clients = selected_clients[:max(0, int(max_clients))]
        except ValueError:
            print(f"Invalid client limit '{max_clients}', ignoring limit.")
    selected_records = [record for record in records if record.get("client_ip") in selected_clients]
    if not selected_records:
        print("No recorded setup pairs match the selected client limit.")
        return True

    settings["ip_source"] = "proxmox"
    _get_target_ips(settings)
    completed_clients = []
    for record in selected_records:
        email = record["email"]
        asset_ip = record["asset_ip"]
        display_name = record["asset_display_name"]
        client_ip = record["client_ip"]
        print(f"\nTearing down recorded pair: {client_ip} -> {asset_ip} ({email})")
        # A live HERA session on the asset makes the server reject the asset delete.
        cleanup_errors = []
        try:
            _stop_proxmox_tauri_windows(settings, client_ip)
        except Exception as exc:
            cleanup_errors.append(f"Client session shutdown: {exc}")
            print(f"    - {_describe_exception(f'Client {client_ip}', exc)[0]} (the HERA client could not be closed first)")

        web_cleanup_ok = True
        try:
            erase_web_user_and_asset(settings, email, display_name, asset_ip)
        except Exception as exc:
            web_cleanup_ok = False
            cleanup_errors.append(f"Hera API cleanup: {exc}")
            print(
                f"    - {email}: could not delete the web user/asset - {_describe_exception('', exc)[0]} "
                f"(asset to delete manually: {display_name} at {asset_ip})"
            )
        tx_cleanup_ok = True
        try:
            tx_result = delete_tx_certificate(settings, email)
            if tx_upload_configured(settings) and tx_result is False:
                tx_cleanup_ok = False
                cleanup_errors.append("TX certificate cleanup: delete or rehash failed; check the TX output.")
                print(f"    - {email}: the TX server did not confirm that the certificate was deleted; check the TX output.")
        except Exception as exc:
            tx_cleanup_ok = False
            cleanup_errors.append(f"TX certificate cleanup: {exc}")
            print(f"    - {email}: could not delete the TX certificate - {_describe_exception('', exc)[0]}")

        command = [
            r"C:\Program Files\hera-client\tauri-client-app.exe",
            f"--delete-account={email}",
        ]
        local_deleted = False
        local_error = ""
        try:
            output, error_output, exit_status, timed_out = _execute_proxmox_interactive_command(
                settings, client_ip, command, 45
            )
            local_deleted = (not timed_out) and _account_delete_confirmed(output, error_output)
            if local_deleted:
                still_present, detail = _desktop_tauri_account_present(
                    settings, client_ip, email, strict=True
                )
                if still_present:
                    local_deleted = False
                    local_error = f"Account still present in the desktop Tauri database. {detail}"
            else:
                local_error = error_output.strip() or output.strip() or f"exit {exit_status}"
                if timed_out:
                    local_error = f"Timed out deleting the local account. {local_error}"
        except Exception as exc:
            local_deleted = False
            local_error = str(exc)
        if local_deleted:
            print(f"    - Local Tauri account delete command succeeded for {email}.")
        else:
            cleanup_errors.append(f"Local Tauri cleanup: {local_error}")
            print(
                f"    - {email}: the account in the HERA client app on {client_ip} could not be deleted "
                f"or confirmed - {_describe_failure('', local_error)[0]}"
            )

        pair_cleaned = not cleanup_errors and web_cleanup_ok and tx_cleanup_ok and local_deleted
        record["status"] = "cleaned" if pair_cleaned else "teardown_partial"
        record["cleanup_errors"] = cleanup_errors
        record["cleanup_results"] = {
            "web": web_cleanup_ok,
            "tx": tx_cleanup_ok,
            "local": local_deleted,
        }
        record["teardown_finished_at"] = datetime.now(timezone.utc).isoformat()
        _save_ip_inventory(inventory)
        if client_ip not in completed_clients:
            completed_clients.append(client_ip)

    run_client_close(client_ips_override=completed_clients)
    _print_proxmox_teardown_summary(selected_records, settings)
    return True


def _print_proxmox_teardown_summary(records, settings):
    records_by_client = {}
    for record in records:
        records_by_client.setdefault(record["client_ip"], []).append(record)
    attention_count = sum(
        any(record["status"] != "cleaned" for record in client_records)
        for client_records in records_by_client.values()
    )
    cleaned_count = sum(record["status"] == "cleaned" for record in records)
    print("\nProxmox teardown summary (selected assignments only)")
    print(
        f"VMs: {len(records_by_client) - attention_count} fully cleaned; "
        f"{attention_count} need attention"
    )
    print(f"Accounts: {cleaned_count} cleaned; {len(records) - cleaned_count} partial/unconfirmed")
    for client_ip, client_records in records_by_client.items():
        vm = settings.get("proxmox_vms_by_ip", {}).get(client_ip, {})
        identity = f"{client_ip} (VMID {vm.get('vmid', 'unknown')}, node {vm.get('node', 'unknown')})"
        needs_attention = any(record["status"] != "cleaned" for record in client_records)
        print(f"  - {identity}: {'NEEDS ATTENTION' if needs_attention else 'CLEANED'}")
        for record in client_records:
            if record["status"] == "cleaned":
                continue
            results = record["cleanup_results"]
            print(
                f"    Manual cleanup required: {record['email']} -> "
                f"{record['asset_display_name']} ({record['asset_ip']})"
            )
            print(
                "      " + "; ".join(
                    f"{label}: {'OK' if results[key] else 'FAILED/UNCONFIRMED'}"
                    for key, label in (
                        ("web", "Web user/asset"),
                        ("tx", "TX certificate"),
                        ("local", "Local Tauri account"),
                    )
                )
            )
            for error in record["cleanup_errors"]:
                print(f"      - {_friendly_cleanup_error(error)}")
    print(f"Results saved in {IP_INVENTORY_PATH}; partial assignments can be retried with teardown.")


def run_client_teardown(max_clients=None):
    """Clean up users and assets created during the setup phase."""
    global load_test_counter
    load_test_counter = 0
    try:
        settings = _read_load_test_settings()
    except Exception as exc:
        print(f"Could not load test settings: {exc}")
        return

    if settings["ip_source"] == "proxmox" and _teardown_recorded_proxmox_pairs(settings, max_clients):
        return

    def get_teardown_command(counter, asset_ip):
        email = f'{settings["account_email_user"]}{counter}@{settings["account_email_domain"]}'
        print(f"    - Erasing web user, asset, and TX certificate for {email}")
        try:
            erase_web_user_and_asset(settings, email, asset_display_name(settings, counter), asset_ip)
        except Exception as exc:
            print(f"    - {email}: could not delete the web user/asset - {_describe_exception('', exc)[0]}")
        try:
            delete_tx_certificate(settings, email)
        except Exception as exc:
            print(f"    - {email}: could not delete the TX certificate - {_describe_exception('', exc)[0]}")
        if settings["ip_source"] == "proxmox":
            return [
                r"C:\Program Files\hera-client\tauri-client-app.exe",
                f"--delete-account={email}",
            ]
        return (
            f'"C:\\Program Files\\hera-client\\tauri-client-app.exe" '
            f'--delete-account="{email}"'
        )

    try:
        _execute_on_clients(
            get_teardown_command,
            settings,
            max_clients=max_clients,
            command_timeout=45,
        )
    finally:
        load_test_counter = 0
        run_client_close(max_clients=max_clients)


def run_client_close(max_clients=None, client_ips_override=None):
    """Close Tauri and its child processes without deleting accounts or certificates."""
    try:
        settings = _read_load_test_settings()
    except Exception as exc:
        print(f"Could not load test settings: {exc}")
        return

    client_ips, _asset_ips = _get_target_ips(settings)
    if client_ips_override is not None:
        client_ips = list(client_ips_override)
    if max_clients is not None:
        try:
            max_clients = int(max_clients)
            if max_clients > 0:
                client_ips = client_ips[:max_clients]
        except ValueError:
            print(f"Invalid client limit '{max_clients}', ignoring limit.")
    if not client_ips:
        print("Could not close client apps without client IPs from the selected source.")
        return

    close_script = (
        "$ErrorActionPreference='Stop'; "
        "$processes=@(Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -in @('tauri-client-app.exe','TpmMtlsProxy.exe') }); "
        "if ($processes.Count -eq 0) { Write-Output 'No Tauri client app running'; exit 0 }; "
        "foreach ($process in $processes) { "
        "Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }; "
        "Start-Sleep -Seconds 1; "
        "$remaining=@(Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -in @('tauri-client-app.exe','TpmMtlsProxy.exe') }); "
        "if ($remaining.Count -gt 0) { "
        "Write-Output ('Processes still running: ' + (($remaining | ForEach-Object Name) -join ', ')); exit 1 }; "
        "Write-Output 'Closed Tauri client and child processes'; exit 0"
    )
    encoded_script = _encode_ps_command(close_script)
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script]

    for client_ip in client_ips:
        ssh_client = None
        try:
            print(f"\nClosing Tauri client on {client_ip}...")
            if settings["ip_source"] == "proxmox":
                output, error_output, exit_status, timed_out = _execute_proxmox_command(
                    settings, client_ip, command, 20
                )
            else:
                ssh_client = paramiko.SSHClient()
                ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh_client.connect(
                    hostname=client_ip,
                    username=settings["client_username"],
                    password=settings["client_pass"],
                    timeout=10,
                )
                _stdin, stdout, stderr = ssh_client.exec_command(command)
                output, error_output, exit_status, timed_out = _read_remote_command(
                    stdout, stderr, 20
                )
            if timed_out:
                print(f"  - Timed out closing Tauri on {client_ip}.")
            elif exit_status == 0:
                print(f"  - {output.strip()}")
            else:
                print(f"  - Failed to close Tauri on {client_ip}: {error_output.strip() or output.strip()}")
        except paramiko.AuthenticationException:
            print(f"  - Authentication failed for {client_ip}.")
        except paramiko.SSHException as exc:
            print(f"  - SSH connection error for {client_ip}: {exc}")
        except Exception as exc:
            print(f"  - Could not close Tauri on {client_ip}: {exc}")
        finally:
            if ssh_client is not None:
                ssh_client.close()


def _login_state(record, rendering_mode):
    """Classify a recorded login result as 'ok', 'failed' or 'not_tried'."""
    status = record.get("login_status")
    if not status:
        return "not_tried"
    if status != "connected":
        return "failed"
    if rendering_mode != "off" and record.get("asset_render_status") != rendering_mode:
        return "failed"
    return "ok"


def _ask_login_retry_scope(records, rendering_mode):
    """Return True to log in only the VMs that failed last run, False to choose a number of VMs.

    The stored login results describe the previous run, not what is connected now; the VMs may have
    been restarted since. So the default is a normal run on a chosen number of VMs, and reusing the
    previous results to skip the ones already logged in is an explicit choice.
    """
    states = [_login_state(record, rendering_mode) for record in records]
    ok_count = states.count("ok")
    failed_count = states.count("failed")
    not_tried_count = states.count("not_tried")
    incomplete = failed_count + not_tried_count
    if ok_count == 0 or incomplete == 0:
        return False
    print(
        f"\nThe last login run recorded {ok_count} logged in and {failed_count} failed. "
        "These are results from that run; if the VMs were restarted since, they are no longer connected."
    )
    print("  1. Log in a chosen number of VMs (you choose how many next) - recommended")
    print(f"  2. Reuse those results and log in only the {incomplete} VM(s) that failed or were not tried")
    while True:
        choice = input("Enter 1 or 2 (Enter = 1): ").strip()
        if choice in ("", "1"):
            return False
        if choice == "2":
            return True
        print("Invalid input. Enter 1 or 2.")


def run_client_login(max_clients=None, retry_failed_only=None, ask_client_limit=None):
    """Log clients into assets using load test settings.

    retry_failed_only: True skips VMs that already completed, False logs in all,
    None asks when a previous run left both successes and failures.
    ask_client_limit: optional callable that asks for the client count, only when it is needed.
    """
    global load_test_counter
    load_test_counter = 0
    try:
        settings = _read_load_test_settings()
    except Exception as exc:
        print(f"Could not load test settings: {exc}")
        return

    if settings["ip_source"] == "proxmox":
        try:
            inventory = _load_ip_inventory()
            records = _live_setup_records(inventory)
            if not records:
                print("There are no created accounts to log in. Run client setup (step 1) first.")
                return
            settings["proxmox_vms_by_ip"] = {
                vm["ip"]: {
                    "name": vm.get("name", ""),
                    "node": vm.get("node", ""),
                    "vmid": vm.get("vmid"),
                }
                for vm in inventory.get("clients", []) + inventory.get("assets", [])
                if vm.get("ip") and vm.get("node") and vm.get("vmid") is not None
            }
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            print(f"Could not load the Proxmox setup manifest: {exc}")
            return

        rendering_mode = settings["asset_rendering_mode"]
        if rendering_mode not in {"low", "high", "off"}:
            print("ASSET_RENDERING_MODE must be 'low', 'high' or 'off'.")
            return

        if retry_failed_only is None:
            retry_failed_only = _ask_login_retry_scope(records, rendering_mode)
        if retry_failed_only:
            records = [r for r in records if _login_state(r, rendering_mode) != "ok"]
            print(
                f"Logging in only the {len(records)} VM(s) that are not logged in yet "
                "(the client limit is not applied)."
            )
        else:
            if ask_client_limit is not None:
                max_clients = ask_client_limit()
            clients = list(dict.fromkeys(record.get("client_ip") for record in records if record.get("client_ip")))
            if max_clients is not None:
                clients = clients[:max(0, int(max_clients))]
            selected_clients = set(clients)
            records = [record for record in records if record.get("client_ip") in selected_clients]
            print(f"Using {len(records)} recorded account-to-client assignment(s).")

        def open_rendering(record):
            if rendering_mode == "off":
                return True
            try:
                _open_asset_rendering(settings, record["asset_ip"])
            except Exception as exc:
                message, detail = _describe_exception(f"Asset {record['asset_ip']}", exc)
                record["asset_render_status"] = "failed"
                record["asset_render_error"] = message
                record["asset_render_detail"] = detail[:2000]
                print(f"  - Rendering failed. {message}")
                return False
            record["asset_render_status"] = rendering_mode
            record.pop("asset_render_error", None)
            record.pop("asset_render_detail", None)
            print(f"  - {rendering_mode} rendering opened on asset {record['asset_ip']}.")
            return True

        def fail_login(record, subject, raw_error):
            message, detail = _describe_failure(subject, raw_error)
            record["login_status"] = "failed"
            record["login_error"] = message
            record["login_detail"] = detail[:2000]
            record["login_finished_at"] = datetime.now(timezone.utc).isoformat()
            print(f"  - Login failed. {message}")

        def login_once(record, retry=False):
            counter = int(record["counter"])
            client_ip = record["client_ip"]
            asset_ip = record["asset_ip"]
            email = record["email"]
            label = "Retrying" if retry else "Logging in"
            print(f"\n{label} {email} on {client_ip} -> asset {asset_ip}...")
            for stale_key in ("asset_render_status", "asset_render_error", "asset_render_detail", "login_detail"):
                record.pop(stale_key, None)
            try:
                account_site_ip = _desktop_tauri_account_site_address(
                    settings, client_ip, email
                )
            except Exception as exc:
                fail_login(record, f"Client {client_ip} ({email})", str(exc))
                _save_ip_inventory(inventory)
                return
            _apply_client_credentials_to_asset(settings, client_ip)
            if rendering_mode != "off":
                _disable_asset_setup_screen(settings, asset_ip)
            # The page runs in the server-side browser; an RX-In URL bypasses mTLS and breaks the session.
            web_url = settings["hera_web_url"] or "http://hera-hera-web:3000/hera-web"
            command = [
                r"C:\\Windows\\System32\\PsExec.exe",
                "-i", "1", "/accepteula", "-d",
                r"C:\\Program Files\\hera-client\\tauri-client-app.exe",
                f'--auto-login={email}:{settings["account_pass"]}@{account_site_ip}',
                f'--url={web_url}?auto_connect={settings["asset_user"]}:{settings["asset_encoded_pass"]}@{settings["asset_name"]}-{counter}',
                f'--api-port={settings["api_port"]}',
                f'--mtls-port={settings["mtls_port"]}',
            ]
            output, error_output, exit_status, timed_out = _execute_proxmox_login_with_proxy_retry(
                settings, client_ip, command
            )
            succeeded = not timed_out and exit_status == 0
            record["login_status"] = "connected" if succeeded else "failed"
            record["login_finished_at"] = datetime.now(timezone.utc).isoformat()
            if succeeded:
                record.pop("login_error", None)
                print(f"  - Tauri opened and asset auto-connect was started for {email}.")
                # HERA sometimes logs in but never starts the auto-connect; count that as a failed login.
                if not _asset_session_user(settings, asset_ip):
                    fail_login(
                        record, f"Asset {asset_ip}",
                        "HERA did not open a session on the asset.",
                    )
                else:
                    print(f"  - Asset session is open on {asset_ip}.")
                    open_rendering(record)
            else:
                raw_error = "Timed out." if timed_out else (error_output or output or f"exit {exit_status}")
                fail_login(record, f"Client {client_ip} ({email})", raw_error)
            _save_ip_inventory(inventory)

        def attempt_login(record, retry=False):
            # One broken VM must never stop the run for the remaining VMs.
            try:
                login_once(record, retry)
            except Exception as exc:
                message, detail = _describe_exception(
                    f"Client {record.get('client_ip')} ({record.get('email')})", exc
                )
                record["login_status"] = "failed"
                record["login_error"] = message
                record["login_detail"] = detail[:2000]
                record["login_finished_at"] = datetime.now(timezone.utc).isoformat()
                print(f"  - Login failed. {message}")
                _save_ip_inventory(inventory)

        def process(record):
            if retry_failed_only and record.get("login_status") == "connected":
                print(f"\nRetrying {rendering_mode} rendering on asset {record['asset_ip']}...")
                open_rendering(record)
                _save_ip_inventory(inventory)
            else:
                attempt_login(record, retry=bool(retry_failed_only and record.get("login_status")))

        for record in records:
            process(record)

        failed_logins = [r for r in records if r.get("login_status") != "connected"]
        failed_renders = [
            r for r in records
            if rendering_mode != "off"
            and r.get("login_status") == "connected" and r.get("asset_render_status") != rendering_mode
        ]
        if failed_logins or failed_renders:
            print(
                f"\nRetrying once: {len(failed_logins)} failed login(s), "
                f"{len(failed_renders)} failed rendering(s)."
            )
        for record in failed_logins:
            attempt_login(record, retry=True)
        for record in failed_renders:
            print(f"\nRetrying {rendering_mode} rendering on asset {record['asset_ip']}...")
            open_rendering(record)
            _save_ip_inventory(inventory)

        connected = [r for r in records if r.get("login_status") == "connected"]
        print(f"\nLogin summary: {len(connected)}/{len(records)} client(s) connected to their asset.")
        if rendering_mode != "off":
            rendered = [r for r in connected if r.get("asset_render_status") == rendering_mode]
            print(f"Rendering summary: {len(rendered)}/{len(records)} asset(s) running {rendering_mode} rendering.")
        problems = []
        for record in records:
            if record.get("login_status") != "connected":
                problems.append(record.get("login_error") or f"Client {record['client_ip']}: login did not complete.")
            elif rendering_mode != "off" and record.get("asset_render_status") != rendering_mode:
                problems.append(record.get("asset_render_error") or f"Asset {record['asset_ip']}: rendering did not start.")
        if problems:
            print("\nWhat needs attention:")
            for problem in problems:
                print(f"  - {problem}")
            print(
                "\nTo log in only the VMs that need attention, run step 2 again. "
                "It will ask how to choose the VMs; pick option 2 to reuse these results "
                "and log in only the ones that failed."
            )
        return

    def get_login_command(counter, asset_ip):
        web_url = settings["hera_web_url"] or (
            f'{settings["api_scheme"]}://{settings["server_ip"]}:'
            f'{settings["api_port"]}/hera-web'
        )
        if settings["ip_source"] == "proxmox":
            return [
                r"C:\Windows\System32\PsExec.exe",
                "-i", "1", "/accepteula", "-d",
                r"C:\Program Files\hera-client\tauri-client-app.exe",
                f'--auto-login={settings["account_email_user"]}{counter}@{settings["account_email_domain"]}:{settings["account_pass"]}@{settings["client_site_ip"]}',
                f'--url={web_url}?auto_connect={settings["asset_user"]}:{settings["asset_encoded_pass"]}@{settings["asset_name"]}-{counter}',
                f'--api-port={settings["api_port"]}',
                f'--mtls-port={settings["mtls_port"]}',
            ]
        return (
            f'psexec -i 1 /accepteula -d '
            f'"C:\\Program Files\\hera-client\\tauri-client-app.exe" '
            f'--auto-login={settings["account_email_user"]}{counter}@{settings["account_email_domain"]}:{settings["account_pass"]}@{settings["client_site_ip"]} '
            f'--url={web_url}?auto_connect={settings["asset_user"]}:{settings["asset_encoded_pass"]}@{settings["asset_name"]}-{counter} '
            f'--api-port={settings["api_port"]} '
            f'--mtls-port={settings["mtls_port"]} '
        )

    if ask_client_limit is not None:
        max_clients = ask_client_limit()
    try:
        _execute_on_clients(
            get_login_command,
            settings,
            max_clients=max_clients,
            sleep_seconds=settings["mtls_issue_sleep_time"],
        )
    finally:
        load_test_counter = 0


def main():
    """
    Main function to run the load test executor.
    """
    while True:
        global load_test_counter
        load_test_counter = 0
        print("\nLoad Test Executor")
        print("1. Run client setup (load test)")
        print("2. Run client login (load test)")
        print("3. Close client apps (keep users and certificates)")
        print("4. Run client teardown (delete accounts)")
        print("5. Exit")
        
        choice = input("Enter your choice: ")
        
        if choice == '1':
            run_client_setup(_prompt_client_limit("How many client VMs should setup run on?"))
        elif choice == '2':
            run_client_login(ask_client_limit=lambda: _prompt_client_limit(
                "How many client VMs should log in?",
                default=selected_client_limit,
            ))
        elif choice == '3':
            run_client_close(_prompt_client_limit(
                "How many client VMs should be closed?",
                default=selected_client_limit,
            ))
        elif choice == '4':
            run_client_teardown(_prompt_client_limit(
                "How many client VMs should be torn down?",
                default=selected_client_limit if selected_client_limit is not None else 1,
            ))
        elif choice == '5':
            confirm = input("Do you want to delete the created test users before exiting? (y/n): ").strip().lower()
            if confirm in ('y', 'yes'):
                run_client_teardown(_prompt_client_limit(
                    "How many client VMs should be torn down?",
                    default=selected_client_limit if selected_client_limit is not None else 1,
                ))
            print("Exiting.")
            break
        else:
            print("Invalid choice. Please try again.")


def _prompt_client_limit(prompt, default=None):
    default_text = f"; Enter uses {default}" if default is not None else "; Enter uses all"
    while True:
        value = input(f"{prompt} (positive number{default_text}; type 'all' for all): ").strip()
        if not value:
            return default
        if value.lower() == "all":
            return None
        try:
            limit = int(value)
            if limit > 0:
                return limit
        except ValueError:
            pass
        print("Invalid input. Enter a positive number or press enter for all.")


if __name__ == "__main__":
    main()