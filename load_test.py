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
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
TERRAFORM_DIR = os.path.dirname(os.path.abspath(__file__))
IP_INVENTORY_PATH = os.path.join(TERRAFORM_DIR, "load_test_ip_lists.json")

load_test_settings_cache = None
load_test_counter = 0
selected_client_limit = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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
        }

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
    proxmox_ip = settings["proxmox_ip"] or "10.101.28.213"
    api_url = f"https://{proxmox_ip}:8006/api2/json"
    token_id = settings["proxmox_token_id"]
    token_secret = settings["proxmox_token_secret"]
    ca_cert = settings["proxmox_ca_cert"]

    if not token_id or not token_secret:
        print("Proxmox IP discovery requires TOKEN_ID and SECRET in .env.")
        return [], []

    def request_json(path):
        response = requests.get(
            f"{api_url}/{path}",
            headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
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

    for vm in running_vms:
        name = str(vm.get("name", ""))
        vm_name = name.lower()
        is_client = re.search(r"(^|[^a-z0-9])hera([^a-z0-9]|$)", vm_name) is not None
        is_asset = re.search(r"(^|[^a-z0-9])asset([^a-z0-9]|$)", vm_name) is not None
        if is_client == is_asset:
            continue

        node = quote(str(vm.get("node", "")), safe="")
        vmid = quote(str(vm.get("vmid", "")), safe="")
        try:
            result_data = request_json(
                f"nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
            ).get("data", {}).get("result", [])
        except Exception as exc:
            print(f"Could not read guest-agent IP for VM '{name}': {exc}")
            continue

        if not isinstance(result_data, list):
            print(f"Guest agent is not ready for VM '{name}'.")
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
            print(f"No usable IPv4 address reported by the guest agent for VM '{name}'.")
            continue

        settings["proxmox_vms_by_ip"][vm_ip] = {
            "node": str(vm.get("node", "")),
            "vmid": int(vm.get("vmid", 0)),
            "name": name,
        }
        (client_ips if is_client else asset_ips).append(vm_ip)

    print(f"Discovered {len(client_ips)} Hera client(s) and {len(asset_ips)} asset(s) from Proxmox.")
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


def _execute_proxmox_command(settings, client_ip, command, timeout_seconds=None):
    vm = settings.get("proxmox_vms_by_ip", {}).get(client_ip)
    if not vm:
        return "", f"No Proxmox VM metadata found for {client_ip}.", -1, False

    api_base = f"https://{settings['proxmox_ip'] or '10.101.28.213'}:8006/api2/json"
    node = quote(vm["node"], safe="")
    vmid = vm["vmid"]
    agent_url = f"{api_base}/nodes/{node}/qemu/{vmid}/agent"
    headers = {
        "Authorization": (
            f"PVEAPIToken={settings['proxmox_token_id']}="
            f"{settings['proxmox_token_secret']}"
        )
    }
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
        for attempt in range(3):
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
                details = (response.text if response is not None else "").lower()
                retryable = response is not None and response.status_code == 500 and (
                    "has been disabled" in details
                    or "failed to execute helper program" in details
                )
                if not retryable or attempt == 2:
                    raise
                time.sleep(attempt + 1)

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
    api_base = f"https://{settings['proxmox_ip'] or '10.101.28.213'}:8006/api2/json"
    node = quote(vm["node"], safe="")
    vmid = vm["vmid"]
    headers = {
        "Authorization": (
            f"PVEAPIToken={settings['proxmox_token_id']}="
            f"{settings['proxmox_token_secret']}"
        )
    }
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
    response.raise_for_status()


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
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
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
    cache_by_ip = settings.setdefault("windows_logon_by_ip", {})
    cached = cache_by_ip.get(client_ip)
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
    if not match or exit_status != 0:
        detail = _readable_powershell_error(error_output) or (output or "").strip()
        raise RuntimeError(
            "None of the configured passwords can log on to an enabled Windows account. "
            + detail[:300]
        )
    found = {"user": match.group(1), "password": passwords[int(match.group(2))]}
    cache_by_ip[client_ip] = found
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


def _execute_proxmox_interactive_command(settings, client_ip, command, timeout_seconds=None):
    """Run a command as the Windows desktop user.

    A console session is used when one exists. Otherwise the VM username and
    password log the account on, because Tauri only needs that user profile.
    """
    if isinstance(command, str):
        command = ["cmd.exe", "/c", command]
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
    $activeUser = (Get-CimInstance Win32_ComputerSystem).UserName
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
        $stdoutText = ''
        $stderrText = ''
        if (Test-Path -LiteralPath $stdoutPath) {{ $stdoutText = [IO.File]::ReadAllText($stdoutPath) }}
        if (Test-Path -LiteralPath $stderrPath) {{ $stderrText = [IO.File]::ReadAllText($stderrPath) }}
        $result = @{{
            stdout=$stdoutText;
            stderr=$stderrText;
            exitcode=[int]([IO.File]::ReadAllText($exitPath));
            timed_out=$false
        }}
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


def _desktop_tauri_account_present(settings, client_ip, email):
    """Return whether the logged-in desktop user's Tauri database contains email."""
    script = (
        "$p=Join-Path $env:USERPROFILE '.hera\\client_db.sqlite'; "
        "Write-Output ('PROFILE=' + $env:USERPROFILE); "
        "if ($env:USERPROFILE -match 'systemprofile') { Write-Output 'ACCOUNT_WRONG_PROFILE'; exit 3 }; "
        "if (-not (Test-Path -LiteralPath $p)) { Write-Output 'ACCOUNT_MISSING'; exit 2 }; "
        "[Convert]::ToBase64String([IO.File]::ReadAllBytes($p))"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
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
        return False, f"Timed out checking the desktop Tauri account database. {profile}".strip()
    if "ACCOUNT_WRONG_PROFILE" in (output or ""):
        return False, profile or "Account check ran in the SYSTEM profile."
    if exit_status == 2 or "ACCOUNT_MISSING" in (output or ""):
        return False, profile or "Desktop Tauri database does not exist."
    try:
        emails = _desktop_tauri_account_emails(output)
    except Exception as exc:
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
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
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
        "$upstream=Get-NetTCPConnection -OwningProcess $listener.OwningProcess -State Established -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.RemoteAddress -eq '{settings['client_site_ip']}' -and $_.RemotePort -eq {int(settings['mtls_port'])} }}; "
        "if ($process.ProcessName -eq 'TpmMtlsProxy' -and $upstream) { exit 0 } }; "
        "Start-Sleep -Seconds 1 }; exit 1"
    )
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script]
    _output, _error_output, exit_status, timed_out = _execute_proxmox_command(
        settings, client_ip, command, timeout_seconds + 5
    )
    return not timed_out and exit_status == 0


def _stop_proxmox_client_process_tree(settings, client_ip, process_id):
    _execute_proxmox_command(
        settings,
        client_ip,
        ["taskkill.exe", "/PID", str(int(process_id)), "/T", "/F"],
        10,
    )


def _account_delete_confirmed(output, error_output):
    text = f"{output}\n{error_output}".lower()
    return (
        "account delete successful" in text
        or "account_delete_successful" in text
        or "no local tauri account found" in text
    )


def _proxmox_desktop_login_command(command):
    """Start Tauri detached as the desktop user. Guest-agent PsExec runs as SYSTEM."""
    if isinstance(command, str):
        return ["cmd.exe", "/c", 'start "" ' + command]
    parts = [str(part) for part in command]
    executable = parts[0].replace("\\", "/").rsplit("/", 1)[-1].lower() if parts else ""
    if executable == "psexec.exe":
        parts = parts[1:]
        while parts and parts[0].lower() in {"-i", "/accepteula", "-accepteula", "-d"}:
            parts = parts[2:] if parts[0].lower() == "-i" and len(parts) > 1 else parts[1:]
    quoted = " ".join('"' + part.replace('"', '""') + '"' for part in parts)
    return ["cmd.exe", "/c", f'start "" {quoted}']


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
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    output, error_output, exit_status, timed_out = _execute_proxmox_command(
        settings, client_ip, command, 20
    )
    visible = (not timed_out) and exit_status == 0 and "TAURI_VISIBLE" in (output or "")
    detail = " ".join(part.strip() for part in (output, error_output) if part and part.strip())
    return visible, detail


def _proxmox_tauri_processes_stopped(settings, client_ip):
    """Confirm both Tauri and its proxy exited before a retry can launch again."""
    script = (
        "$p=@(Get-Process tauri-client-app,TpmMtlsProxy -ErrorAction SilentlyContinue); "
        "Write-Output ('PROCESS_COUNT=' + $p.Count); "
        "if ($p.Count -eq 0) { exit 0 }; exit 1"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    output, _error_output, exit_status, timed_out = _execute_proxmox_command(
        settings, client_ip, command, 20
    )
    return not timed_out and exit_status == 0 and "PROCESS_COUNT=0" in (output or "")


def _stop_proxmox_tauri_windows(settings, client_ip):
    """Close Tauri and its proxy before another login attempt."""
    for process_name in ("tauri-client-app.exe", "TpmMtlsProxy.exe", "msedgewebview2.exe"):
        _execute_proxmox_command(
            settings,
            client_ip,
            ["taskkill.exe", "/F", "/IM", process_name, "/T"],
            10,
        )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _proxmox_tauri_processes_stopped(settings, client_ip):
            return
        time.sleep(1)


def _proxmox_reboot_vm(settings, client_ip):
    """Reboot a Proxmox VM so Windows auto-logon can create a desktop session."""
    vm = settings.get("proxmox_vms_by_ip", {}).get(client_ip)
    if not vm:
        raise RuntimeError(f"No Proxmox VM metadata found for {client_ip}.")
    api_base = f"https://{settings['proxmox_ip'] or '10.101.28.213'}:8006/api2/json"
    node = quote(vm["node"], safe="")
    headers = {
        "Authorization": (
            f"PVEAPIToken={settings['proxmox_token_id']}="
            f"{settings['proxmox_token_secret']}"
        )
    }
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
                time.sleep(30)
                visible, detail = _proxmox_tauri_visible_in_session(settings, client_ip)
                proxy_ready = _wait_for_proxmox_local_mtls_proxy(settings, client_ip, 1)
                if visible and proxy_ready:
                    return output, error_output, exit_status, False
                failure_detail = detail or "Tauri or TpmMtlsProxy exited shortly after launch."
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


def _apply_client_credentials_to_asset(settings, client_ip, asset_ip=None):
    """Use credentials verified against the assigned asset for auto-connect."""
    if settings.get("ip_source") != "proxmox":
        return
    username, password = _resolve_windows_logon(settings, asset_ip or client_ip)
    if username:
        settings["asset_user"] = username
        print(f"    - Asset login user is {username}.")
    if password:
        settings["asset_pass"] = password
        settings["asset_encoded_pass"] = quote(password, safe="")


def _existing_setup_emails(settings):
    """Return test emails that already exist and must not be created again."""
    emails = set()
    try:
        inventory = _load_ip_inventory()
    except (OSError, json.JSONDecodeError):
        inventory = {}
    for run in inventory.get("setup_runs", []):
        for record in run.get("assignments", []):
            if record.get("status") == "created" and record.get("email"):
                emails.add(record["email"].strip().lower())
    try:
        from server_cleanup import _login, _request, split_admin_credentials
        username, password = split_admin_credentials(settings.get("admin_credentials"))
        token = settings.get("admin_access_token") or _login(settings, username, password)
        settings["admin_access_token"] = token
        users = _request(settings, "GET", "/api/admin/users", token=token) or []
        prefix = settings.get("account_email_user", "").strip().lower()
        domain = settings.get("account_email_domain", "").strip().lower()
        for user in users:
            email = (user.get("email") or user.get("username") or "").strip().lower()
            if prefix and domain and email.startswith(prefix) and email.endswith("@" + domain):
                emails.add(email)
    except Exception as exc:
        print(f"Could not check existing web users; a rerun may try to recreate them: {exc}")
    return emails


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
    if record_setup:
        inventory = _load_ip_inventory()
        inventory.update({
            "version": 1,
            "source": settings["ip_source"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "clients": [_inventory_vm(ip, settings) for ip in discovered_client_ips],
            "assets": [_inventory_vm(ip, settings) for ip in discovered_asset_ips],
        })
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
    existing_emails = _existing_setup_emails(settings) if record_setup else set()
    if existing_emails:
        print(f"Leaving {len(existing_emails)} existing account(s) in place.")
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
                try:
                    load_test_counter += 1
                    setup_assignment = None
                    if setup_run is not None:
                        setup_assignment = setup_run["assignments"][setup_assignment_index]
                    setup_assignment_index += 1
                    existing_email = (setup_assignment or {}).get("email", "").strip().lower()
                    if existing_email and existing_email in existing_emails:
                        print(f"    - {setup_assignment['email']} already exists. Leaving that account in place.")
                        setup_assignment["status"] = "already_created"
                        _save_ip_inventory(inventory)
                        assets_processed += 1
                        continue

                    _apply_client_credentials_to_asset(settings, client_ip, asset_ip)
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
                        print("    - Windows command timed out. Closing the Hera window so teardown can continue.")
                        print("    - The local Tauri account may not have been deleted.")
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
                        if setup_assignment is not None:
                            setup_assignment["status"] = "failed"
                            setup_assignment["local_account_verified"] = False
                            setup_assignment["error"] = (
                                local_account_detail
                                or error_output.strip()
                                or f"exit {exit_status}"
                            )
                            _save_ip_inventory(inventory)
                        print(f"    - Error executing command for asset {asset_ip}.")
                        print(f"    - Exit Code: {exit_status}")
                        print(f"    - Stderr: {error_output}")
                        if local_account_detail and local_account_detail not in error_output:
                            print(f"    - Local account check: {local_account_detail}")
                        if is_account_delete and not _account_delete_confirmed(output, error_output):
                            print("    - Delete success marker was not returned by the client.")
                        assets_processed += 1
                        print(f"  - Sleep for {sleep_seconds} seconds to let mtls recover from the previous execution.")
                        time.sleep(sleep_seconds)
                        continue

                    print(f"  - Sleep for {sleep_seconds} seconds to let mtls recover from the previous execution.")
                    time.sleep(sleep_seconds)

                except StopIteration:
                    print("  - No more asset IPs available.")
                    break

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

        cleanup_errors = []
        web_cleanup_ok = True
        try:
            erase_web_user_and_asset(settings, email, display_name, asset_ip)
        except Exception as exc:
            web_cleanup_ok = False
            cleanup_errors.append(f"Hera API cleanup: {exc}")
            print(f"    - Failed to erase web user or asset for {email}: {exc}")
        tx_cleanup_ok = True
        try:
            tx_result = delete_tx_certificate(settings, email)
            if tx_upload_configured(settings) and tx_result is False:
                tx_cleanup_ok = False
        except Exception as exc:
            tx_cleanup_ok = False
            cleanup_errors.append(f"TX certificate cleanup: {exc}")
            print(f"    - Failed to erase TX certificate for {email}: {exc}")

        command = [
            r"C:\Program Files\hera-client\tauri-client-app.exe",
            f"--delete-account={email}",
        ]
        output, error_output, exit_status, timed_out = _execute_proxmox_interactive_command(
            settings, client_ip, command, 45
        )
        local_deleted = (not timed_out) and _account_delete_confirmed(output, error_output)
        if local_deleted:
            still_present, detail = _desktop_tauri_account_present(settings, client_ip, email)
            if still_present:
                local_deleted = False
                error_output = (
                    f"{error_output}\nAccount still present in the desktop Tauri database. {detail}"
                ).strip()
        if local_deleted:
            print(f"    - Local Tauri account delete command succeeded for {email}.")
        else:
            cleanup_errors.append(
                f"Local Tauri cleanup: {error_output.strip() or output.strip() or f'exit {exit_status}'}"
            )
            print(f"    - Local Tauri account delete failed for {email}: {error_output.strip() or output.strip()}")

        pair_cleaned = web_cleanup_ok and tx_cleanup_ok and local_deleted
        record["status"] = "cleaned" if pair_cleaned else "teardown_partial"
        record["cleanup_errors"] = cleanup_errors
        record["teardown_finished_at"] = datetime.now(timezone.utc).isoformat()
        _save_ip_inventory(inventory)
        if client_ip not in completed_clients:
            completed_clients.append(client_ip)

    run_client_close(client_ips_override=completed_clients)
    return True


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
            print(f"    - Failed to erase web user or asset for {email}: {exc}")
        try:
            delete_tx_certificate(settings, email)
        except Exception as exc:
            print(f"    - Failed to erase TX certificate for {email}: {exc}")
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
    encoded_script = base64.b64encode(close_script.encode("utf-16le")).decode("ascii")
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


def run_client_login(max_clients=None):
    """Log clients into assets using load test settings."""
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
            setup_runs = [
                run for run in inventory.get("setup_runs", [])
                if run.get("source", inventory.get("source")) == "proxmox"
            ]
            if not setup_runs:
                print("No Proxmox setup records found. Run client setup before login.")
                return
            latest_setup = setup_runs[-1]
            records = [
                record for record in latest_setup.get("assignments", [])
                if record.get("status") == "created"
            ]
            if not records:
                print("The latest Proxmox setup has no successfully created accounts to log in.")
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

        clients = list(dict.fromkeys(record.get("client_ip") for record in records if record.get("client_ip")))
        if max_clients is not None:
            clients = clients[:max(0, int(max_clients))]
        selected_clients = set(clients)
        records = [record for record in records if record.get("client_ip") in selected_clients]
        print(
            f"Using {len(records)} recorded account-to-client assignment(s) from "
            f"{latest_setup.get('started_at', 'the latest setup')}."
        )

        if settings["asset_rendering_mode"] not in {"low", "high"}:
            print("ASSET_RENDERING_MODE must be 'low' or 'high'.")
            return

        def open_asset_rendering(asset_ip):
            user, _error = _windows_console_user(settings, asset_ip)
            deadline = time.monotonic() + 30
            while not user and time.monotonic() < deadline:
                time.sleep(2)
                user, _error = _windows_console_user(settings, asset_ip)
            if not user:
                raise RuntimeError(f"No interactive desktop appeared on asset {asset_ip}.")

            payload = base64.b64encode(json.dumps({
                "mode": settings["asset_rendering_mode"],
                "url": settings["low_rendering_url"],
            }).encode("utf-8")).decode("ascii")
            script = f"""
$ErrorActionPreference = 'Stop'
$spec = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) | ConvertFrom-Json
$edge = @('C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe', 'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe') | Where-Object {{ Test-Path -LiteralPath $_ }} | Select-Object -First 1
if (-not $edge) {{ throw 'Microsoft Edge is not installed on the asset.' }}
$arguments = @('--new-window', '--no-first-run')
if ($spec.mode -eq 'low') {{
    if (-not $spec.url) {{ throw 'LOW_RENDERING_URL is empty.' }}
    $arguments += @('--kiosk', [string]$spec.url, '--edge-kiosk-type=fullscreen')
}}
Start-Process -FilePath $edge -ArgumentList $arguments
Start-Sleep -Seconds 2
if (-not (Get-Process msedge -ErrorAction SilentlyContinue)) {{ throw 'Edge did not remain open on the asset.' }}
Write-Output ('ASSET_RENDERING_STARTED=' + $spec.mode)
""".strip()
            encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            output, error, status, timed_out = _execute_proxmox_interactive_command(
                settings,
                asset_ip,
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                30,
            )
            if timed_out or status != 0 or "ASSET_RENDERING_STARTED=" not in output:
                raise RuntimeError((error or output or f"exit {status}").strip())

        def attempt_login(record, retry=False):
            counter = int(record["counter"])
            client_ip = record["client_ip"]
            asset_ip = record["asset_ip"]
            email = record["email"]
            label = "Retrying" if retry else "Logging in"
            print(f"\n{label} {email} on {client_ip} -> asset {asset_ip}...")
            try:
                account_site_ip = _desktop_tauri_account_site_address(
                    settings, client_ip, email
                )
            except Exception as exc:
                record["login_status"] = "failed"
                record["login_error"] = str(exc)
                record["login_finished_at"] = datetime.now(timezone.utc).isoformat()
                _save_ip_inventory(inventory)
                print(f"  - Login skipped: {exc}")
                return False
            _apply_client_credentials_to_asset(settings, client_ip, asset_ip)
            web_url = settings["hera_web_url"] or (
                f'{settings["api_scheme"]}://{settings["server_ip"]}:'
                f'{settings["api_port"]}/hera-web'
            )
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
            proxy_started = not timed_out and exit_status == 0
            rendering_started = False
            if proxy_started:
                print(f"  - Tauri opened and asset auto-connect was started for {email}.")
                try:
                    open_asset_rendering(asset_ip)
                    record["asset_render_status"] = "started"
                    record.pop("asset_render_error", None)
                    rendering_started = True
                    print(f"  - {settings['asset_rendering_mode']} rendering opened on asset {asset_ip}.")
                except Exception as exc:
                    record["asset_render_status"] = "failed"
                    record["asset_render_error"] = str(exc)
                    print(f"  - Asset rendering failed on {asset_ip}: {exc}")
            else:
                record["login_error"] = (error_output or output or f"exit {exit_status}").strip()
                print(f"  - Login failed for {email}: {record['login_error']}")
            succeeded = proxy_started and rendering_started
            record["login_status"] = "connected" if succeeded else "failed"
            record["login_finished_at"] = datetime.now(timezone.utc).isoformat()
            if succeeded:
                record.pop("login_error", None)
            elif proxy_started:
                record["login_error"] = record.get("asset_render_error", "Asset rendering did not start.")
            _save_ip_inventory(inventory)
            return succeeded

        for record in records:
            attempt_login(record)

        failed_records = [
            record for record in records
            if record.get("login_status") == "failed" or record.get("asset_render_status") == "failed"
        ]
        if failed_records:
            print(f"\nRetrying {len(failed_records)} failed account-to-asset connection(s) once.")
            for record in failed_records:
                attempt_login(record, retry=True)
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
            run_client_login(_prompt_client_limit(
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