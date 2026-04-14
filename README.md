# AWS Load Test Infrastructure

This project provisions AWS EC2 instances using Terraform and runs automated load tests against them using a Python script over SSH.

---

## How It Works

```
main.tf          → Provisions EC2 instances + Elastic IPs on AWS
load_test.py     → SSHes into client machines and runs Tauri commands
load_test.ini    → Holds all credentials and test parameters (you create this)
```

**The workflow is always:**
1. `terraform apply` → spins up machines
2. `python3 load_test.py` → runs tests (reads IPs live from Terraform state)
3. `terraform destroy` → tears everything down

---

## Prerequisites

Install all of the following before proceeding.

### 1. Terraform

Add the HashiCorp DNF repository and install Terraform:

```bash
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo
sudo dnf install -y terraform
```

Verify:
```bash
terraform -version
```

### 2. Python 3.8+

Rocky Linux ships with Python 3. To install or update:

```bash
sudo dnf install -y python3 python3-pip
```

Verify:
```bash
python3 --version
```

### 3. Python Dependencies

```bash
pip3 install paramiko
```

### 4. AWS CLI & Credentials

Install the AWS CLI:

```bash
sudo dnf install -y unzip
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install
```

Configure your credentials:
```bash
aws configure
```
Enter your **AWS Access Key ID**, **Secret Access Key**, region (`il-central-1`), and output format (`json`).

Verify:
```bash
aws sts get-caller-identity
```

### 5. AWS Key Pair
The Terraform config uses a key pair named `deployer_key`. This must already exist in the `il-central-1` AWS region.

To verify it exists:
```bash
aws ec2 describe-key-pairs --key-names deployer_key --region il-central-1
```

---

## Project Structure

```
newAWS/
├── main.tf             # Terraform infrastructure definition
├── load_test.py        # Python load test executor
├── load_test.ini       # Your configuration file (YOU MUST CREATE THIS)
└── README.md           # This file
```

---

## Step 1 — Create the Configuration File

The Python script reads all credentials and test parameters from `load_test.ini` in the project directory.

Create this file and fill in your values:

```ini
[DEFAULT]
PROJECT_NAMESPACE = my_project

CLIENT_ASSETS_FACTOR = 1
CLIENT_USERNAME = Administrator
CLIENT_PASS = your-client-rdp-password

SERVER_PASS = your-server-password

ACCOUNT_PASS = your-account-password
ACCOUNT_FIRST_NAME = John
ACCOUNT_LAST_NAME = Doe
ACCOUNT_EMAIL_USER = testuser
ACCOUNT_EMAIL_DOMAIN = example.com

ASSET_NAME = my-asset
ASSET_USER = assetuser
ASSET_PASS = your-asset-password
ASSET_PROTOCOL = rdp
ASSET_PORT = 3389

MTLS_ISSUE_SLEEP_TIME = 5

[my_project]
SERVER_IP = 10.0.0.1
API_PORT = 8443
MTLS_PORT = 8444
```

**Key settings explained:**

| Setting | Description |
|---|---|
| `PROJECT_NAMESPACE` | Must match the section name in brackets below (e.g. `[my_project]`) |
| `CLIENT_ASSETS_FACTOR` | How many assets each client handles. Set to `0` to distribute all assets evenly across clients |
| `CLIENT_USERNAME` | Windows username to SSH into client machines (usually `Administrator`) |
| `CLIENT_PASS` | Password for the client machines |
| `ACCOUNT_EMAIL_USER` | Email prefix for test accounts — counter is appended (e.g. `testuser1`, `testuser2`) |
| `ASSET_PROTOCOL` | Protocol for connecting to assets (e.g. `rdp`, `ssh`) |
| `MTLS_ISSUE_SLEEP_TIME` | Seconds to wait between connections to let mTLS recover |
| `SERVER_IP` | IP of the Hera server |
| `API_PORT` | Hera API port |
| `MTLS_PORT` | Hera mTLS port |

---

## Step 2 — Initialize Terraform

Run this **once** in the project directory to download the AWS provider plugin:

```bash
cd ~/newAWS
terraform init
```

Expected output ends with:
```
Terraform has been successfully initialized!
```

---

## Step 3 — Plan Your Infrastructure

Preview what Terraform will create **without actually creating anything**:

```bash
terraform plan -var="client_instance_count=2" -var="asset_instance_count=5"
```

Read the output and confirm the number of resources to be added matches your expectations.

---

## Step 4 — Apply (Create Instances)

Create the instances on AWS:

```bash
terraform apply -var="client_instance_count=2" -var="asset_instance_count=5"
```

Terraform will show the plan and ask for confirmation. Type `yes` and press Enter.

This will:
- Launch 2 client EC2 instances (`g5.xlarge`) tagged `Hera Tauri Client 1`, `Hera Tauri Client 2`
- Launch 5 asset EC2 instances (`t3.large`) tagged `Hera Asset 1` through `Hera Asset 5`
- Allocate and attach Elastic IPs to all 7 instances

Wait for it to finish — this can take 2–5 minutes.

**To verify the IPs were assigned:**
```bash
terraform output -json
```

You should see `client_ips` and `asset_ips` arrays populated with public IPs.

### Available Variables

You can override any of these on the command line with `-var="name=value"`:

| Variable | Default | Description |
|---|---|---|
| `client_instance_count` | `1` | Number of client machines |
| `asset_instance_count` | `1` | Number of asset machines |
| `instance_type_client` | `g5.xlarge` | EC2 type for clients |
| `instance_type_asset` | `t3.large` | EC2 type for assets |
| `ami_id_client` | `ami-0557a707271e7a405` | AMI for clients |
| `ami_id_asset` | `ami-01bb4fbfc33109299` | AMI for assets |
| `key_name` | `deployer_key` | EC2 key pair name |
| `region` | `il-central-1` | AWS region |

---

## Step 5 — Run the Load Tests

From the project directory, run:

```bash
python3 load_test.py
```

You will see this menu:

```
Load Test Executor
1. Run client setup (load test)
2. Run client login (load test)
3. Exit
```

### Option 1 — Client Setup

Run this **first**, on a fresh deployment. It creates accounts and registers assets on the Hera server.

- Press `1` then Enter
- The script will SSH into each client machine and run the setup command for each asset

### Option 2 — Client Login

Run this **after setup**. It logs each test account into its corresponding asset.

- Press `2` then Enter
- You will be prompted:
  ```
  How many assets do you want to log in? (Enter a number or press enter for all)
  ```
  - Enter a number (e.g. `10`) to log in to only the first 10 assets
  - Press Enter with no input to log in all assets

### Option 3 — Exit

Exits the script.

---

## Step 6 — Tear Down (Destroy Instances)

When you are done testing, destroy all resources to avoid AWS charges:

```bash
terraform destroy -var="client_instance_count=2" -var="asset_instance_count=5"
```

> **Important:** Pass the same `count` values you used during `apply`, otherwise Terraform may not know about all instances.

Type `yes` when prompted. This will:
- Terminate all EC2 instances
- Release all Elastic IPs

---

## Common Workflows

### Full Test Run from Scratch

```bash
# 1. Initialize (first time only)
terraform init

# 2. Create 3 clients and 15 assets
terraform apply -var="client_instance_count=3" -var="asset_instance_count=15"

# 3. Run setup (creates accounts and assets on the server)
python3 load_test.py   # choose option 1

# 4. Run login load test
python3 load_test.py   # choose option 2

# 5. Clean up
terraform destroy -var="client_instance_count=3" -var="asset_instance_count=15"
```

### Re-run Login Test Without Re-provisioning

If the instances are already running, just run:

```bash
python3 load_test.py   # choose option 2
```

The script always fetches current IPs from Terraform state — no manual IP management needed.

---

## Troubleshooting

### `terraform: command not found`
Terraform is not in your PATH. Re-install using the HashiCorp DNF repository as described in the Prerequisites section, or move the binary to a system path:
```bash
sudo mv terraform /usr/local/bin/
```

### `Error: No valid credential sources found`
Your AWS credentials are not configured. Run `aws configure` and enter your access key and secret key. Alternatively, verify that `~/.aws/credentials` exists and is correctly populated.

### `Error: InvalidKeyPair.NotFound`
The key pair `deployer_key` does not exist in the `il-central-1` region. Create it via:
```bash
aws ec2 create-key-pair --key-name deployer_key --region il-central-1 --query 'KeyMaterial' --output text > ~/.ssh/deployer_key.pem
chmod 400 ~/.ssh/deployer_key.pem
```

### `Authentication failed` in the Python script
The `CLIENT_PASS` in `load_test.ini` is wrong for one or more client machines. Double-check the password for each machine in the AWS Console under **EC2 → Instances → Get Windows Password**.

### `terraform command not found` in Python script
The `terraform` binary is not available in the PATH that Python sees. Ensure Terraform is installed system-wide (e.g. in `/usr/local/bin`) and not only in a specific shell session.

### `Load test config not found`
The file `load_test.ini` is missing from the project directory. Create it by following [Step 1](#step-1--create-the-configuration-file).

### Instances created but no IPs in `terraform output`
Elastic IPs may still be associating. Wait 30 seconds and run `terraform refresh` followed by `terraform output -json`.
