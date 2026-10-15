# HERA Client Load-Test Automation

This repository automates test-account setup, HERA client login, and cleanup across Windows client VMs. It supports two ways to find and manage those VMs:

1. **Proxmox (recommended for this guide):** discovers running VMs through the Proxmox API and runs Windows commands through the QEMU Guest Agent.
2. **AWS with Terraform:** provisions EC2 clients and assets and connects to Windows clients over SSH.

The Python runner is interactive. It creates accounts and asset assignments in HERA; it does **not** install the HERA client application or prepare guest operating systems. Prepare the VMs first.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Proxmox: prepare and run](#proxmox-prepare-and-run)
- [AWS and Terraform](#aws-and-terraform)
- [Configuration reference](#configuration-reference)
- [Run the automation](#run-the-automation)
- [Setup inventory and recovery](#setup-inventory-and-recovery)
- [Optional TX certificate upload](#optional-tx-certificate-upload)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)

## How it works

```text
                         HERA Web/API
                             ^
                             | HTTPS / mTLS
                             |
 Runner ---> Windows HERA clients ---> target assets
              (Tauri app)               (for example, RDP)
```

The runner discovers clients and assets using the selected source, assigns assets to clients, and invokes the installed Tauri app on each client. Setup creates HERA accounts and asset records. Login opens Tauri and starts the configured asset connection. Teardown removes the generated HERA data and local accounts.

| Environment | Discovery | Remote client commands | Notes |
|---|---|---|---|
| Proxmox | Proxmox API and each VM's guest-agent network information | QEMU Guest Agent | Uses a local inventory file to remember exact setup assignments and support safe teardown. |
| AWS/Terraform | `terraform output -json` | SSH to each Windows client | Terraform provisions the instances; the runner does not create infrastructure itself. |

## Requirements

### On the machine running the script

- Python 3.8 or newer.
- `paramiko` and `requests`.
- Network access to the selected VM-management interface and the HERA services.
- For AWS/Terraform: Terraform 1.0 or newer and AWS credentials accepted by the AWS provider.
- For Proxmox: access to the Proxmox API from the runner.

Create a virtual environment and install the Python dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install paramiko requests
```

On Windows, activate the virtual environment with `.venv\Scripts\activate` instead of the `source` command.

### Prepare the HERA VMs

Both environments need Windows client VMs with:

- The HERA Tauri client installed at `C:\Program Files\hera-client\tauri-client-app.exe`.
- A Windows account and password the runner can use for the required commands.
- Network access to the configured HERA API and target assets.

For AWS, the runner connects to each client over SSH. For Proxmox, the runner uses the QEMU Guest Agent API; enable and run the guest agent inside the VMs.

## Proxmox: prepare and run

Use this path when your VMs are managed by Proxmox. It does not provision VMs; it discovers already-running ones.

### 1. Prepare the Proxmox VMs

In Proxmox:

- Start the client and asset VMs and enable the QEMU Guest Agent for each VM the script must inspect or control.
- Ensure the guest agent reports a usable IPv4 address.
- Name clients with `hera` as a separate word (for example, `Hera Client 01`) and assets with `asset` as a separate word (for example, `Hera Asset 01`). Names containing both markers or neither are ignored.
- Install PsExec at `C:\Windows\System32\PsExec.exe` on each client.
- Install the HERA client at the path listed above.
- Ensure a Windows desktop session can be opened on each client. The script uses the logged-in desktop user when present; otherwise, it attempts to log on with configured VM credentials. Tauri setup and local account deletion must run in the user's profile, not as `SYSTEM`.
- If you want the script to open Edge rendering on assets during login, those assets also need a usable Windows desktop session, the guest agent, and Microsoft Edge. Set `ASSET_RENDERING_MODE=off` to skip rendering.

Create a Proxmox API token that can enumerate VMs and use the guest agent for those VMs. The token needs the appropriate Proxmox permissions, including `VM.GuestAgent.Unrestricted` for guest commands.

### 2. Configure the runner

Create a local environment file:

```bash
cp .env.example .env
```

Edit `.env` and set at least:

```dotenv
IP_SOURCE=proxmox
PROXMOX_IP=your-proxmox-host
TOKEN_ID=your_user@pam!your_token_id
SECRET=your_proxmox_api_token_secret

PROJECT_NAMESPACE=WATERFALL
WATERFALL_SERVER_IP=your-hera-api-address
WATERFALL_API_PORT=31643
WATERFALL_MTLS_PORT=31888

CLIENT_USERNAME=Administrator
CLIENT_PASS=your-windows-client-password
ACCOUNT_PASS=your-generated-test-account-password
ACCOUNT_FIRST_NAME=Load
ACCOUNT_LAST_NAME=Test
ACCOUNT_EMAIL_USER=loadtest
ACCOUNT_EMAIL_DOMAIN=example.invalid

ASSET_NAME=LoadTestAsset
ASSET_PROTOCOL=rdp
ASSET_PORT=3389
ASSET_USER=your-asset-user
ASSET_PASS=your-asset-password
ADMIN_CREDENTIALS=your-hera-admin:your-hera-admin-password
```

Replace example values with credentials and addresses for your own lab. Do not commit `.env`.

`PROJECT_NAMESPACE` selects the server/API settings: with `WATERFALL`, the script reads `WATERFALL_SERVER_IP`, `WATERFALL_API_PORT`, and `WATERFALL_MTLS_PORT`; with `DEV`, it reads the corresponding `DEV_*` values.

If Proxmox assets use a different Windows account from the client, set both `PROXMOX_ASSET_USER` and `PROXMOX_ASSET_PASS`. Otherwise, the runner uses the client desktop username and configured client password for Proxmox asset connections.

### 3. Start the runner

From the repository directory, with the virtual environment active:

```bash
python load_test.py
```

Choose **Run client setup** first. Setup discovers the VMs, creates accounts and assets, and records successful assignments in `load_test_ip_lists.json`. Then choose **Run client login** to use those recorded Proxmox assignments.

## AWS and Terraform

Choose this path when Terraform should provision EC2 instances. Set `IP_SOURCE=terraform` (the default) in `.env`.

### 1. Prepare AWS settings and images

- Configure AWS provider credentials and select the intended region.
- Review the AMI IDs, instance types, key pair, and counts in [`main.tf`](./main.tf).
- Prepare a Windows client AMI containing the HERA Tauri client, PsExec, and the required Windows/graphics setup.
- Prepare an asset AMI suitable for the asset protocol and workload you plan to test.
- Make sure the runner can reach client SSH and that clients can reach the HERA server and assets.

The Terraform configuration's current defaults create `g5.xlarge` clients and `t3.large` assets. Adjust these and the AMIs for your environment before provisioning.

### 2. Provide the Windows administrator password

Terraform needs the sensitive `client_password` input. `.env` is read by the Python runner; Terraform does **not** load it automatically. Set `TF_VAR_client_password` in your shell or use another protected Terraform variable mechanism. Avoid putting passwords directly in command-line arguments or committing them to a `.tfvars` file.

For example, in Bash, enter the password without echoing it:

```bash
read -rsp "Windows client password: " TF_VAR_client_password
echo
export TF_VAR_client_password
```

### 3. Provision the instances

```bash
terraform init
terraform plan -var="region=il-central-1" -var="client_instance_count=5" -var="asset_instance_count=5"
terraform apply -var="region=il-central-1" -var="client_instance_count=5" -var="asset_instance_count=5"
```

After Terraform operations that need the password, remove it from the current shell with `unset TF_VAR_client_password`.

After provisioning, configure `.env` with `CLIENT_USERNAME`, `CLIENT_PASS`, the HERA server settings, and account/asset details. Then run `python load_test.py` and use the interactive menu.

To remove the EC2 infrastructure after cleaning up test data:

```bash
terraform destroy -var="region=il-central-1" -var="client_instance_count=5" -var="asset_instance_count=5"
```

Use the same region and resource parameters as for `terraform apply`. Review Terraform's destroy plan before confirming.

## Configuration reference

The complete example is in [`.env.example`](./.env.example). These are the main settings:

| Setting | Purpose |
|---|---|
| `IP_SOURCE` | `proxmox` or `terraform`; defaults to `terraform`. |
| `PROXMOX_IP`, `TOKEN_ID`, `SECRET` | Proxmox host and API token. Required for Proxmox discovery and guest commands. |
| `PROJECT_NAMESPACE` | Chooses the prefix for the server, API-port, and mTLS-port environment variables. |
| `<NAMESPACE>_SERVER_IP` | HERA API/server address used for account setup. |
| `<NAMESPACE>_API_PORT`, `<NAMESPACE>_MTLS_PORT` | Ports passed to the Tauri client. |
| `CLIENT_SITE_IP` | Optional account lookup address for AWS login; defaults to `<NAMESPACE>_SERVER_IP`. Use the address the client account is stored under if it differs. Proxmox login reads this address from the local account database. |
| `API_SCHEME`, `HERA_WEB_URL` | Web URL settings. `HERA_WEB_URL` overrides the generated/default web URL. |
| `CLIENT_USERNAME`, `CLIENT_PASS` | Windows client login/SSH credentials. Proxmox may also try other configured lab passwords if the first credential is rejected. |
| `ACCOUNT_PASS`, `ACCOUNT_FIRST_NAME`, `ACCOUNT_LAST_NAME` | Password and names used for generated test accounts. |
| `ACCOUNT_EMAIL_USER`, `ACCOUNT_EMAIL_DOMAIN` | Email prefix and domain. The runner appends an incrementing number to the prefix. Use an address range/domain safe for your test environment. |
| `ASSET_NAME`, `ASSET_PROTOCOL`, `ASSET_PORT` | Base asset display name and connection protocol/port. Generated assets receive a numbered suffix. |
| `ASSET_USER`, `ASSET_PASS` | Credentials used for assets in the standard/AWS path; also the fallback credentials for Proxmox assets. |
| `PROXMOX_ASSET_USER`, `PROXMOX_ASSET_PASS` | Optional Proxmox-specific asset credentials; set both to use this pair. |
| `CLIENT_ASSETS_FACTOR` | Assets assigned per client. `0` or a negative value means one; a positive value requests that many per client. If there are too few assets, they are reused; extras are left unused. |
| `ASSET_RENDERING_MODE` | Proxmox login workload: `low` opens `LOW_RENDERING_URL`, `high` opens `HIGH_RENDERING_URL`, and `off` skips asset rendering. |
| `LOW_RENDERING_URL`, `HIGH_RENDERING_URL` | URLs opened in Edge for the corresponding rendering mode. |
| `MTLS_ISSUE_SLEEP_TIME` | AWS login delay between commands to allow the local mTLS proxy to recover; default in `.env.example` is 9 seconds. Proxmox login instead waits for the guest-agent/proxy checks. |
| `ADMIN_CREDENTIALS` | HERA admin credentials in `username:password` form for cleanup and existing-user checks. |

Optional TX certificate upload settings are described in [Optional TX certificate upload](#optional-tx-certificate-upload). Terraform also requires `TF_VAR_client_password` independently of `.env`.

## Run the automation

The interactive menu in `load_test.py` has these actions:

1. **Run client setup** — Creates numbered HERA accounts and asset records on the selected clients. If TX upload is configured, setup also exports and uploads the certificate.
2. **Run client login** — Opens Tauri and starts the account-to-asset connection. In Proxmox mode it uses assignments recorded by the latest setup run and retries failed logins and rendering once. In AWS mode it retries the launch once if the local mTLS proxy does not start.
3. **Close client apps** — Stops Tauri and its mTLS proxy on selected clients. It keeps accounts, assets, and certificates.
4. **Run client teardown** — Deletes generated web users/assets, local Tauri accounts, and configured TX certificates, then closes the clients.
5. **Exit** — Exits the menu and offers an optional teardown.

At the client-count prompt, enter a positive number to limit work or `all` to select all applicable clients. Press Enter to accept the prompt's displayed default. Login and close reuse the last setup count when available; teardown defaults to that count, or to one client if no setup count has been selected.

The runner processes assignments sequentially. For Proxmox, login and rendering require additional guest-agent and Windows-session checks, so startup and retry times depend on the VMs and workload.

## Setup inventory and recovery

The runner writes `load_test_ip_lists.json` in the repository directory. This gitignored file stores the discovered VM inventory and setup assignments, including client/asset IPs, generated email addresses, and operation statuses.

- Keep this file between setup, login, and teardown. Proxmox login depends on the latest setup records.
- Proxmox teardown uses only recorded successful or partially completed assignments. If the inventory is missing or unreadable, it refuses broad teardown instead of guessing which accounts to delete.
- Proxmox teardown continues to the next assignment when a cleanup operation fails, including guest-agent failures during local account deletion. It saves per-step results and errors after each assignment; failed or unconfirmed cleanup is marked `teardown_partial` and remains available for a later retry.
- After closing the selected clients, teardown prints a cleanup summary for the selected assignments: VM IP/VMID/node, cleaned and attention-needed counts, and the accounts/assets and failed steps requiring manual cleanup. These are cleanup results, not a general VM-health check. Review the separate client-close output for shutdown failures.
- The inventory can contain internal addresses and test-account details. Keep it private and back it up securely if you need recovery after a runner-machine failure.

AWS teardown does not use the Proxmox setup manifest in the same way. Check the generated account and asset selection carefully before running cleanup.

## Optional TX certificate upload

To copy the External IP client certificate to a TX host after setup, configure:

```dotenv
TX_HOST=your-tx-host
TX_SSH_USER=your-ssh-user
TX_CERT_FOLDER=/path/to/mtls/certificates
```

Provide either `TX_SSH_KEY` or `TX_SSH_PASS`, and optionally set `TX_SSH_PORT` (default `22`) and `TX_REHASH_COMMAND`. If `TX_HOST` or `TX_CERT_FOLDER` is empty, upload is skipped. When configured, the runner uploads the certificate over SSH/SFTP and rebuilds the OpenSSL hash links.

## Troubleshooting

| Symptom | Things to check |
|---|---|
| No clients/assets found | Check `IP_SOURCE`. For Proxmox, confirm VMs are running QEMU VMs with names containing exactly one recognized marker (`hera` or `asset`), a working guest agent, and a reported IPv4 address. For Terraform, run `terraform output -json` in this repository after applying. |
| Proxmox API or guest-agent errors | Check API reachability, token ID/secret, VM permissions, and that the guest agent service is running inside the VM. |
| Setup/login cannot find Tauri or PsExec | Confirm the exact Windows paths in the Proxmox requirements and that the client AMI/image has finished starting. |
| Account setup is reported unsuccessful | Check the HERA API settings and credentials. In Proxmox, setup must write the account to the logged-in desktop user's Tauri database; ensure a valid desktop/logon account and profile. |
| Login says no account was found | Confirm setup succeeded and the lookup address matches the address stored for that account. Set `CLIENT_SITE_IP` for the AWS path when it differs from the server IP; Proxmox reads the stored address from the local database. |
| Proxmox login has no desktop or rendering fails | Confirm Windows desktop sessions can be opened. Rendering also needs a Windows asset VM with the guest agent, an active HERA session, and Edge; use `ASSET_RENDERING_MODE=off` if rendering is not part of the test. |
| TX upload fails | Verify TX host SSH access, key/password, remote certificate folder ownership/permissions, and the configured rehash command. |
| Terraform cannot create instances | Check AWS credentials/permissions, region and AMI availability, key-pair name, account service quotas, and the `terraform plan` output. |

The script prints per-operation errors and summaries. Resolve the reported cause before retrying setup or teardown; avoid rerunning setup blindly if accounts may already have been created.

## Security notes

- Keep `.env`, Terraform state, generated inventory, and credentials out of version control. Terraform state can contain sensitive values; protect it accordingly.
- The current Proxmox API calls disable TLS certificate verification (`verify=False`). Use this only on a trusted lab network. Although `PROXMOX_CA_CERT` appears in the example environment file, the current script does not use it to verify the Proxmox certificate.
- The AWS security group in `main.tf` currently allows inbound RDP (`3389`) and SSH (`22`) from `0.0.0.0/0`. Restrict inbound access to trusted source addresses and required client-to-asset traffic before applying it in a shared or internet-connected environment.
- The Terraform client bootstrap configures Windows auto-logon with the administrator password. Treat the value and resulting Terraform state as sensitive; use short-lived test infrastructure and rotate credentials as appropriate.
- Review both Terraform plans and cleanup targets before confirming destructive actions.
