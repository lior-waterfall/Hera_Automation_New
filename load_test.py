import configparser
import os
import paramiko
import socket
import subprocess
import json
import time
import math
from urllib.parse import quote
from server_cleanup import asset_display_name, erase_web_user_and_asset
from tx_cert_upload import delete_tx_certificate, export_client_tx_certificate, tx_upload_configured, upload_tx_certificate

# Configuration
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
TERRAFORM_DIR = os.path.dirname(os.path.abspath(__file__))

load_test_settings_cache = None
load_test_counter = 0

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
            "client_assets_factor": int(os.environ.get('CLIENT_ASSETS_FACTOR', '0')),
            "client_pass": os.environ.get('CLIENT_PASS', ''),
            "client_username": os.environ.get('CLIENT_USERNAME', ''),
            "server_ip": os.environ.get(f'{project_namespace}_SERVER_IP', ''),
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
            "asset_protocol": os.environ.get('ASSET_PROTOCOL', ''),
            "asset_port": int(os.environ.get('ASSET_PORT', '3389')),
            "api_port": int(os.environ.get(f'{project_namespace}_API_PORT', '0')),
            "mtls_port": int(os.environ.get(f'{project_namespace}_MTLS_PORT', '0')),
            "mtls_issue_sleep_time": int(os.environ.get('MTLS_ISSUE_SLEEP_TIME', '9')),
            "admin_credentials": os.environ.get('ADMIN_CREDENTIALS', ''),
            "api_scheme": os.environ.get('API_SCHEME', 'https'),
            "tx_host": os.environ.get('TX_HOST', ''),
            "tx_ssh_user": os.environ.get('TX_SSH_USER', ''),
            "tx_ssh_pass": os.environ.get('TX_SSH_PASS', ''),
            "tx_ssh_key": os.environ.get('TX_SSH_KEY', ''),
            "tx_ssh_port": int(os.environ.get('TX_SSH_PORT', '22')),
            "tx_cert_folder": os.environ.get('TX_CERT_FOLDER', ''),
            "tx_rehash_command": os.environ.get('TX_REHASH_COMMAND', ''),
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


def _execute_on_clients(get_tauri_command, settings, max_assets=None, sleep_seconds=None, on_success=None, command_timeout=None):
    """Execute a Tauri command against each client machine using Paramiko."""
    global load_test_counter
    sleep_seconds = sleep_seconds if sleep_seconds is not None else 1

    client_ips, asset_ips = _get_terraform_ips()
    if max_assets is not None:
        try:
            max_assets_int = int(max_assets)
            if max_assets_int > 0:
                asset_ips = asset_ips[:max_assets_int]
        except ValueError:
            print(f"Invalid max_assets value '{max_assets}', ignoring limit.")

    if not client_ips or not asset_ips:
        print("Could not proceed without client and asset IPs from Terraform.")
        return

    # If factor is 0, distribute assets evenly across clients (ceil to cover remainders).
    client_assets_factor = settings["client_assets_factor"]
    if client_assets_factor == 0:
        client_assets_factor = math.ceil(len(asset_ips) / len(client_ips))

    asset_ip_iter = iter(asset_ips)
    assets_processed = 0

    print(f"Starting process for {len(client_ips)} clients.")

    for client_ip in client_ips:
        if max_assets is not None and assets_processed >= max_assets:
            break

        print(f"\nProcessing client IP: {client_ip}")

        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            print(f"  - Connecting to {client_ip}...")
            ssh_client.connect(
                hostname=client_ip,
                username=settings["client_username"],
                password=settings["client_pass"],
                timeout=10
            )
            print("  - Connection successful.")

            for _ in range(int(client_assets_factor)):
                if max_assets is not None and assets_processed >= max_assets:
                    break
                try:
                    load_test_counter += 1
                    asset_ip = next(asset_ip_iter)

                    tauri_command = get_tauri_command(load_test_counter, asset_ip)

                    print(f"    - Executing for asset {asset_ip}: {tauri_command}")

                    stdin, stdout, stderr = ssh_client.exec_command(tauri_command)
                    output, error_output, exit_status, timed_out = _read_remote_command(stdout, stderr, command_timeout)
                    if timed_out:
                        print("    - Windows command timed out. Closing the Hera window so teardown can continue.")
                        print("    - The installed client does not support --delete-account yet, so the local account was not erased.")
                        kill_stdin, kill_stdout, kill_stderr = ssh_client.exec_command('taskkill /F /IM tauri-client-app.exe')
                        _read_remote_command(kill_stdout, kill_stderr, 10)
                        assets_processed += 1
                        continue

                    # psexec -d returns a Process ID (positive integer) rather than 0.
                    is_detach = "psexec" in tauri_command and "-d" in tauri_command
                    
                    if exit_status == 0 or (is_detach and exit_status > 0):
                        if on_success and not on_success(load_test_counter, asset_ip, output, client_ip):
                            print(f"    - Setup command succeeded, but the TX certificate was not uploaded for asset {asset_ip}.")
                        print(f"    - Success! (Exit Code / PID: {exit_status})")
                        assets_processed += 1
                    else:
                        print(f"    - Error executing command for asset {asset_ip}.")
                        print(f"    - Exit Code: {exit_status}")
                        print(f"    - Stderr: {error_output}")
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
            ssh_client.close()
            print(f"  - Connection to {client_ip} closed.")


def run_client_setup():
    """Configure clients and assets for setup workflow using load test settings."""
    try:
        settings = _read_load_test_settings()
    except Exception as exc:
        print(f"Could not load test settings: {exc}")
        return

    def get_setup_command(counter, asset_ip):
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
        if not tx_upload_configured(settings):
            print("    - TX upload skipped: set TX_HOST and TX_CERT_FOLDER to copy the External IP certificate.")
            return True
        email = f'{settings["account_email_user"]}{counter}@{settings["account_email_domain"]}'
        try:
            certificate = export_client_tx_certificate(settings, client_ip, email)
            upload_tx_certificate(settings, email, certificate)
            return True
        except Exception as exc:
            print(f"    - Failed to upload TX certificate for {email} after asset {asset_ip}: {exc}")
            return False

    _execute_on_clients(get_setup_command, settings, on_success=upload_setup_certificate)


def run_client_teardown():
    """Clean up users and assets created during the setup phase."""
    global load_test_counter
    load_test_counter = 0
    try:
        settings = _read_load_test_settings()
    except Exception as exc:
        print(f"Could not load test settings: {exc}")
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
        print("    - Local Windows account was not erased. That step needs the new client.")
        return "echo local-delete-skipped"

    try:
        _execute_on_clients(get_teardown_command, settings, command_timeout=45)
    finally:
        load_test_counter = 0


def run_client_login(max_assets=None):
    """Log clients into assets using load test settings."""
    global load_test_counter
    load_test_counter = 0
    try:
        settings = _read_load_test_settings()
    except Exception as exc:
        print(f"Could not load test settings: {exc}")
        return

    def get_login_command(counter, asset_ip):
        return (
            f'psexec -i 1 /accepteula -d '
            f'"C:\\Program Files\\hera-client\\tauri-client-app.exe" '
            f'--auto-login={settings["account_email_user"]}{counter}@{settings["account_email_domain"]}:{settings["account_pass"]}@{settings["server_ip"]} '
            f'--url=http://hera-hera-web:3000/hera-web?auto_connect={settings["asset_user"]}:{settings["asset_encoded_pass"]}@{settings["asset_name"]}-{counter} '
            f'--api-port={settings["api_port"]} '
            f'--mtls-port={settings["mtls_port"]} '
        )

    try:
        _execute_on_clients(
            get_login_command,
            settings,
            max_assets,
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
        print("3. Run client teardown (delete accounts)")
        print("4. Exit")
        
        choice = input("Enter your choice: ")
        
        if choice == '1':
            run_client_setup()
        elif choice == '2':
            while True:
                max_assets_str = input("How many assets do you want to log in? (Enter a number or press enter for all) ")
                if max_assets_str.strip():
                    try:
                        max_assets = int(max_assets_str)
                        run_client_login(max_assets)
                        break
                    except ValueError:
                        print("Invalid input. Please enter a valid number.")
                else:
                    run_client_login()
                    break
        elif choice == '3':
            run_client_teardown()
        elif choice == '4':
            confirm = input("Do you want to delete the created test users before exiting? (y/n): ").strip().lower()
            if confirm in ('y', 'yes'):
                run_client_teardown()
            print("Exiting.")
            break
        else:
            print("Invalid choice. Please try again.")


if __name__ == "__main__":
    main()