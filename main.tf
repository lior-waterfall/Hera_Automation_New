terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# Input Variables
variable "region" {
  description = "AWS region"
  type        = string
  default     = "eu-central-1"
}

variable "ami_id_client" {
  description = "AMI ID for client instances"
  type        = string
  default     = "ami-066443b0b8e6e2058"
}

variable "ami_id_asset" {
  description = "AMI ID for asset instances"
  type        = string
  default     = "ami-0916617956d59d651"
}

variable "instance_type_client" {
  description = "Instance type for client machines"
  type        = string
  default     = "g5.xlarge"
}

variable "instance_type_asset" {
  description = "Instance type for asset machines"
  type        = string
  default     = "t3.large"
}

variable "key_name" {
  description = "EC2 Key Pair name for SSH access"
  type        = string
  default     = "deployer_key"
}

variable "security_group_ids" {
  description = "Security group IDs for instances"
  type        = list(string)
  default     = ["sg-02e7f1452c2ac4207"]
}

variable "client_instance_count" {
  description = "Number of client instances to create"
  type        = number
  default     = 1
}

variable "asset_instance_count" {
  description = "Number of asset instances to create"
  type        = number
  default     = 1
}

# Create a dynamic Security Group in the current region
resource "aws_security_group" "hera_sg" {
  name        = "hera_security_group"
  description = "Allow inbound traffic for Hera instances"

  # RDP access for Windows
  ingress {
    from_port   = 3389
    to_port     = 3389
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"] # Note: restrict this to your IP for better security
  }

  # SSH access (if needed for asset machines)
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Allow all outbound traffic
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Client Instances
resource "aws_instance" "clients" {
  count                       = var.client_instance_count
  ami                         = var.ami_id_client
  instance_type               = var.instance_type_client
  associate_public_ip_address = true
  key_name                    = var.key_name
  vpc_security_group_ids      = [aws_security_group.hera_sg.id]

  tags = {
    Name = "Hera Tauri Client ${count.index + 1}"
  }
  // Auto-login configuration for Windows instances
  user_data = <<-EOF
    <powershell>
    $Password = "T6H)ejHYULYtLo4EXfXpoo8h!)VCy4ZT"
    $Username = "Administrator"
    
    Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "AutoAdminLogon" -Value "1"
    Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "DefaultUserName" -Value $Username
    Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "DefaultPassword" -Value $Password
    </powershell>
  EOF
}

# Asset Instances
resource "aws_instance" "assets" {
  count                       = var.asset_instance_count
  ami                         = var.ami_id_asset
  instance_type               = var.instance_type_asset
  associate_public_ip_address = true
  key_name                    = var.key_name
  vpc_security_group_ids      = [aws_security_group.hera_sg.id]

  tags = {
    Name = "Hera Asset ${count.index + 1}"
  }
}

# Outputs
output "client_ips" {
  description = "Public IPs of client instances"
  value       = [for instance in aws_instance.clients : instance.public_ip]
}

output "asset_ips" {
  description = "Public IPs of asset instances"
  value       = [for instance in aws_instance.assets : instance.public_ip]
}

output "client_instances" {
  description = "Client instance IDs and details"
  value = [
    for i, instance in aws_instance.clients : {
      instance_id = instance.id
      public_ip   = instance.public_ip
      private_ip  = instance.private_ip
    }
  ]
}

output "asset_instances" {
  description = "Asset instance IDs and details"
  value = [
    for i, instance in aws_instance.assets : {
      instance_id = instance.id
      public_ip   = instance.public_ip
      private_ip  = instance.private_ip
    }
  ]
}
