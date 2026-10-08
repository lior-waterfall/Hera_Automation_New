# AWS Load Test Infrastructure

This project provisions AWS EC2 instances (Windows Tauri clients and Linux assets) using Terraform and runs automated load testing against them via a Python execution script.

---

## Table of Contents
1. [Architecture Overview](#architecture-overview)
2. [Prerequisites](#prerequisites)
3. [Step 1 — Environment Configuration (`.env`)](#step-1--environment-configuration-env)
4. [Step 2 — Provision Infrastructure via Terraform](#step-2--provision-infrastructure-via-terraform)
5. [Step 3 — Run the Load Tests](#step-3--run-the-load-tests)
6. [Step 4 — Tear Down Resources](#step-4--tear-down-resources)
7. [Deep Dive: EC2, AMIs, and Security Groups](#deep-dive-ec2-amis-and-security-groups)
    * [How to Create Custom AMIs](#how-to-create-custom-amis)
    * [Choosing & Managing Security Groups (SG)](#choosing--managing-security-groups-sg)

---

## Architecture Overview

```
                          +-------------------------+
                          |   Hera Web/API Server   |
                          |   (k8s API & Web App)   |
                          +------------+------------+
                                       ^
                                       | TLS / HTTPS APIs
                                       |
                   +-------------------+-------------------+
                   |                                       |
    +--------------+--------------+         +--------------+--------------+
    |   Hera Tauri Client 1       |         |   Hera Tauri Client N       |
    |   - Windows Server          |   ...   |   - Windows Server          |
    |   - Runs tauri-client-app   |         |   - Runs tauri-client-app   |
    +--------------+--------------+         +--------------+--------------+
                   |                                       |
                   | RDP Session                           | RDP Session
                   v                                       v
    +--------------+--------------+         +--------------+--------------+
    |   Hera Asset 1              |   ...   |   Hera Asset M              |
    |   - Linux Target Node       |         |   - Linux Target Node       |
    +-----------------------------+         +-----------------------------+
```

---

## Prerequisites

Before starting, ensure the following are installed and configured on your host machine:

### 1. Terraform (v1.0+)
Install Terraform from the official HashiCorp website.
Verify installation:
```bash
terraform -version
```

### 2. AWS CLI & Credentials
Configure your default AWS region and access keys:
```bash
aws configure
```
Verify connectivity:
```bash
aws sts get-caller-identity
```

### 3. Python 3 & Dependencies
Ensure Python 3.8+ and the `paramiko` and `requests` libraries are installed:
```bash
sudo dnf install -y python3 python3-pip
pip3.12 install paramiko requests
```

---

## Step 1 — Environment Configuration (`.env`)

Copy the configuration template to create your `.env` file:
```bash
cp .env.example .env
```

Open `.env` and fill in your environment parameters:
* `CLIENT_ASSETS_FACTOR`: Set to `0` to assign one asset to each selected client. Set to a positive number to request that many assets per selected client. Extra assets are unused; when there are fewer asset IPs than requested assignments, IPs are reused in order.
* `CLIENT_PASS` & `CLIENT_USERNAME`: SSH credentials for AWS Windows clients. Proxmox client commands use the QEMU Guest Agent API instead of SSH.
* `ACCOUNT_EMAIL_USER` & `ACCOUNT_EMAIL_DOMAIN`: Prefix and domain for generating test accounts (e.g. `yossilior1@oeek.com`).
* `ASSET_PROTOCOL` & `ASSET_PORT`: Details for connecting to the target assets (e.g., `rdp` on port `3389`).
* `ASSET_RENDERING_MODE`: `low` opens `LOW_RENDERING_URL` in Edge kiosk mode on each connected Proxmox asset; `high` opens Edge's new-tab page.
* `LOW_RENDERING_URL`: Clock page used by low-rendering mode; defaults to `https://time.is/`.
* `PROJECT_NAMESPACE`: Variable prefix selector (e.g. `WATERFALL` or `DEV`).

To use Proxmox VM IPs instead of AWS Terraform outputs, set `IP_SOURCE=proxmox` in `.env`. Configure `PROXMOX_IP`, `TOKEN_ID`, and `SECRET` there; `TOKEN_ID` should use Proxmox's `user@realm!token-id` format and needs `VM.GuestAgent.Unrestricted` permission for guest commands. Proxmox requests currently use `verify=False` for self-signed lab certificates, which disables TLS certificate verification and should only be used on a trusted network; rotate the API token if it has been exposed. Keep `IP_SOURCE=terraform` (the default) to use AWS. VM names must contain `hera` as a separate word for clients or `asset` as a separate word for assets; each VM must be running with the QEMU Guest Agent enabled and report an IPv4 address. Proxmox client VMs also need PsExec at `C:\Windows\System32\PsExec.exe`, an active Windows session 1, and the Tauri executable at `C:\Program Files\hera-client\tauri-client-app.exe`. Setup and local account deletion run as the Windows desktop user, not as SYSTEM, and setup is successful only when that user's Tauri database contains the new account. If nobody is logged into the console, the script logs on with the VM account password and runs the command in that user profile. `CLIENT_PASS` is tried first; other configured lab passwords are tried if that logon is rejected. Login opens Tauri as that same desktop user in session 1. Do not launch it from the guest agent as SYSTEM; WebView2 then fails because it tries to write under `C:\\Windows\\System32\\config\\systemprofile`. By default, the login URL is built as `{API_SCHEME}://{<PROJECT_NAMESPACE>_SERVER_IP}:{<PROJECT_NAMESPACE>_API_PORT}/hera-web`; set `HERA_WEB_URL` to override it. Web login and user creation stay on `<PROJECT_NAMESPACE>_SERVER_IP` (RX-In). The site certificate on that server currently has SAN `10.101.28.42`, and the client stores each account under that certificate address. `--auto-login` must use that stored address as the lookup key, or the window reports `No account found`. The page it opens is still `{API_SCHEME}://<RX-In>:<API_PORT>/hera-web`. A desktop SSH tunnel is only for a workstation outside this subnet and is not used by the clients.

Discovery writes the full ordered client/asset inventory to the gitignored `load_test_ip_lists.json`. Each setup appends its exact client, asset, email, and display-name assignments; only successfully created pairs are eligible for Proxmox teardown. Partial teardown records remain available for retry, so teardown does not reconstruct accounts from a changing IP list.

---

## Step 2 — Provision Infrastructure via Terraform

1. Initialize Terraform (downloads provider plugins):
   ```bash
   terraform init
   ```
2. Apply the configuration to spin up machines (override the defaults via variables if needed):
   ```bash
   terraform apply -var="region=il-central-1" -var="client_instance_count=5" -var="asset_instance_count=5"
   ```
   *Type `yes` when prompted.*

---

## Step 3 — Run the Load Tests

Execute the interactive test suite control script:
```bash
python3.12 load_test.py
```

### Script Menu Options:
* **Option 1 (Run client setup)**: Prompts for the number of client VMs, then assigns assets according to `CLIENT_ASSETS_FACTOR`. Commands run over SSH for AWS or QEMU Guest Agent for Proxmox. Setup exports the TX certificate through the selected client transport, then uploads it to the TX host over SSH and rehashes the certificate folder.
* **Option 2 (Run client login)**: Prompts for a client count; pressing Enter reuses the count chosen during setup. It launches Tauri in Windows session 1 using PsExec with `--auto-login` and `--url` parameters, opens the configured rendering workload on the assigned Proxmox asset, then retries failed account-to-asset connections once.
* **Option 3 (Close client apps)**: Prompts for a client count; Enter reuses the setup count. It closes Tauri and its child mTLS proxy processes while keeping Hera users, assets, and certificates intact.
* **Option 4 (Run client teardown)**: Prompts for a client count; Enter reuses the setup count, or defaults to one client if no setup count is remembered. It deletes generated accounts/assets, removes TX certificates, then closes those client apps. Type `all` to operate on every discovered client.
* **Option 5 (Exit)**: Exits the menu; optionally runs teardown before exiting.

---

## Step 4 — Tear Down Resources

To avoid ongoing AWS charges, terminate all infrastructure resources:
```bash
terraform destroy -var="region=il-central-1" -var="client_instance_count=5" -var="asset_instance_count=5"
```
> **Warning:** Make sure to pass the same region and count parameters that you used during `terraform apply`.

---

## Deep Dive: EC2, AMIs, and Security Groups

This section details how the AWS instances are configured, how to build your own AMIs, and how to manage security groups safely.

### 1. EC2 Instance Selection
* **Clients (`g5.xlarge`)**: Windows Tauri client applications are GPU-accelerated and require graphics resources to launch successfully. The `g5.xlarge` instance class provides NVIDIA A10G GPUs, enabling proper rendering of Tauri's Webview2 frame.
* **Assets (`t3.large`)**: Assets act as connection targets (RDP/SSH hosts) and do not run heavy client applications, so lighter compute-optimized/burstable general-purpose instances like `t3.large` are selected to save costs.

---

### How to Create Custom AMIs

To avoid setting up dependencies (like installing the Tauri Client App, configuring drivers, and setting registry values) every time you boot an instance, you should capture configured machines into custom AMIs.

#### A. Creating a Windows Tauri Client AMI
1. **Launch a clean base instance**: Start a Windows Server 2022 instance (e.g. `g5.xlarge`) on AWS.
2. **Install NVIDIA Drivers**: Install the matching NVIDIA grid/gaming drivers so the GPU is recognized.
3. **Configure Tauri Client**:
   * Create the folder `C:\Program Files\hera-client\`.
   * Place the `tauri-client-app.exe` binary in it.
4. **Install Sysinternals PsExec**:
   * Place `psexec.exe` in `C:\Windows\System32\` or a folder in your System PATH so it can be called globally.
5. **Configure Sysprep / AutoLogon** (Optional but recommended):
   * Run the Sysprep utility or configure Windows Winlogon registry keys to automatically sign in the `Administrator` user on startup (this initializes the GUI desktop session needed for PsExec's `-i 2` parameter).
6. **Create the AMI on AWS Console**:
   * Right-click your running client instance -> **Image and templates** -> **Create image**.
   * Give it a name (e.g., `Hera-Windows-Client-v1`) and click create.
   * Copy the resulting **AMI ID** (e.g., `ami-0d67...`) and set it as `ami_id_client` in `main.tf`.

#### B. Creating a Linux Asset AMI
1. Launch a general Linux instance (e.g., Ubuntu/Rocky Linux).
2. Install RDP host (`xrdp`) or ensure SSH server is running.
3. Right-click the instance -> **Image and templates** -> **Create image**.
4. Save the resulting **AMI ID** and set it as `ami_id_asset` in `main.tf`.

---

### Choosing & Managing Security Groups (SG)

Security Groups act as a virtual firewall for your EC2 instances to control inbound and outbound traffic.

#### Required Inbound Ports:
* **Port `3389` (RDP)**: Required for Windows Client GUI visualization, RDP target connections for assets, and manual administrator logons.
* **Port `22` (SSH)**: Required by the `load_test.py` script to connect to the client machines and trigger Tauri/PsExec command lines.

#### Restricting Access (Best Practices):
In `main.tf`, the default security group rule allows traffic from anywhere:
```hcl
ingress {
  from_port   = 3389
  to_port     = 3389
  protocol    = "tcp"
  cidr_blocks = ["0.0.0.0/0"] # Open to the world
}
```

> **Security Warning:** Leaving RDP (`3389`) and SSH (`22`) open to `0.0.0.0/0` exposes your test instances to brute-force attacks. 

To secure your deployment:
1. **Restrict CIDR to your specific IP address**:
   Replace `["0.0.0.0/0"]` with your specific corporate or local public IP subnet (e.g., `["203.0.113.50/32"]`).
2. **Restrict Client-to-Asset communication**:
   Instead of opening port `3389` to the whole world, you can set the asset's security group to only accept incoming connections originating from the Security Group ID of the Client instances.
