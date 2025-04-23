# EBS Volume Auto-scaler

A simple tool that automatically increases your AWS EBS volumes when they're running out of space. Perfect for managing disk space on your EC2 instances without manual intervention.

## Table of Contents
1. [Quick Start](#quick-start)
2. [How It Works](#how-it-works)
3. [Installation Options](#installation-options)
4. [Configuration Guide](#configuration-guide)
5. [Running the Tool](#running-the-tool)
6. [Monitoring & Logging](#monitoring--logging)
7. [Troubleshooting](#troubleshooting)
8. [CI/CD Integration](#cicd-integration)

## Quick Start

### Prerequisites
- AWS account with EC2 and EBS access
- Python 3.7 or higher
- Ansible 2.15 or higher (for automated deployment)

### Installation (Choose One Method)

#### Method 1: Quick Manual Setup
```bash
# Clone the repository
git clone <repository-url>
cd ebs-autoscaler

# Install requirements
pip install -r requirements.txt

# Create config directory
sudo mkdir -p /opt/ebs-autoscaler
```

#### Method 2: Automated Setup with Ansible
```bash
# Clone the repository
git clone <repository-url>
cd ebs-autoscaler

# Install Ansible and AWS plugin
pip install ansible
ansible-galaxy collection install amazon.aws

# Run the playbook
ansible-playbook -i ansible/inventory/ playbook.yml
```

## How It Works

The tool consists of several components working together:

### 1. Main Components
- **`ebs-scaler.py`**: The core Python script that:
  - Monitors disk usage
  - Detects EBS volumes
  - Manages volume resizing
  - Handles notifications

- **`ebs-scaler.service`**: Systemd service that:
  - Runs the script in the background
  - Automatically restarts if it fails
  - Manages logging

- **Ansible Playbooks**: For automated deployment:
  - `playbook.yml`: Main deployment playbook
  - Inventory files: Define target servers
  - Group variables: Server-specific settings

### 2. Configuration Files
- **`config.ini`**: Main configuration file with:
  - Monitoring settings
  - Threshold values
  - Notification settings
  - Volume exclusions

- **Inventory Files**: Define your servers:
  - `all-inventory.aws_ec2.yml`: All servers
  - `jenkins-inventory.aws_ec2.yml`: Jenkins servers
  - `mongodb-inventory.aws_ec2.yml`: MongoDB servers

### 3. Monitoring Process
1. **Volume Detection**
   - Scans for mounted EBS volumes
   - Identifies volume IDs
   - Stores info in `volume_info.json`

2. **Usage Monitoring**
   - Checks disk usage every 5 minutes
   - Tracks total and used space
   - Compares against threshold

3. **Scaling Process**
   When usage exceeds threshold:
   - Increases volume size
   - Expands filesystem
   - Sends notification
   - Updates volume info

## Installation Options

### Manual Installation
1. Install required packages:
   ```bash
   # For Debian/Ubuntu
   sudo apt-get install util-linux xfsprogs e2fsprogs

   # For RedHat/Amazon Linux
   sudo yum install util-linux cloud-utils-growpart xfsprogs e2fsprogs
   ```

2. Set up Python environment:
   ```bash
   python3 -m venv /opt/ebs-autoscaler/venv
   source /opt/ebs-autoscaler/venv/bin/activate
   pip install boto3 psutil typer
   ```

### Ansible Installation
The Ansible playbook handles:
- Package installation
- Python environment setup
- Script deployment
- Service configuration

## Configuration Guide

### Main Configuration (`config.ini`)
```ini
[general]
interval = 300    # Check every 5 minutes
threshold = 80    # Expand at 80% usage
increase_gb = 10  # Add 10GB each time

[notification]
enabled = true
email-sender = your-email@example.com
email-recipients = admin@example.com

[exclude]
volumes = vol-12345678  # Volumes to ignore
```

### Inventory Configuration
Edit the appropriate inventory file in `ansible/inventory/`:
```yaml
plugin: aws_ec2
regions:
  - eu-west-1
filters:
  tag:Environment: non-prod
  instance-state-name: running
```

## Running the Tool

### Start the Service
```bash
# Start in daemon mode
python ebs-scaler.py -d

# Or run as a background service
nohup python ebs-scaler.py -d >>/opt/ebs-autoscaler/scale.log 2>&1 &
```

### Check Status
```bash
# View logs
tail -f /opt/ebs-autoscaler/scale.log

# Check service status
systemctl status ebs-scaler
```

## Monitoring & Logging

### Log Files
- Main log: `/opt/ebs-autoscaler/logs/scale.log`
- Volume info: `/opt/ebs-autoscaler/volume_info.json`

### What to Monitor
- Disk usage trends
- Scaling events
- Error messages
- Notification status

## CI/CD Integration

The tool includes Jenkins pipeline support:
1. **Dry Run Mode**: Test changes without applying
2. **Automated Deployment**: Push changes to servers
3. **Inventory Validation**: Check server configurations

Configure in `Jenkinsfile`:
```groovy
environment {
    ANSIBLE_SERVER = "<your_ansible_server>"
    REMOTE_DIR = "/etc/ansible/playbooks/ebs-autoscaler"
}
```

## Troubleshooting

### Common Issues
1. **Permission Errors**
   - Check AWS credentials
   - Verify IAM permissions
   - Check file permissions

2. **Volume Not Expanding**
   - Verify required tools are installed
   - Check filesystem type
   - Review AWS API limits

3. **Email Notifications**
   - Verify SES configuration
   - Check email verification
   - Review SES quotas

### Getting Help
- Check logs at `/opt/ebs-autoscaler/logs/scale.log`
- Review AWS CloudWatch metrics
- Check systemd service status

## Contributing
Feel free to submit issues and pull requests to improve the tool!

## Directory Structure

### On EC2 Instance (After Installation)
```
/opt/ebs-autoscaler/
├── ebs-scaler.py          # Main script
├── config.ini            # Configuration file
├── volume_info.json      # Auto-generated volume info (volume IDs, device names, mount points)
├── logs/                 # Log directory
│   └── scale.log        # Application logs
└── venv/                # Python virtual environment
    ├── bin/
    ├── include/
    ├── lib/
    └── pip-selfcheck.json
```

### Ansible Project Structure
```
ebs-autoscaler/
├── ansible/
│   ├── inventory/           # Inventory files
│   │   ├── all-inventory.aws_ec2.yml
│   │   ├── jenkins-inventory.aws_ec2.yml
│   │   ├── mongodb-inventory.aws_ec2.yml
│   │   └── group_vars/      # Group-specific variables
│   │       ├── jenkins_non_prod.yml
│   │       └── mongodb_non_prod.yml
├── resources/              # Template and script files
│   ├── ebs-scaler.py     # Main script
│   ├── config.ini        # Configuration template
│   └── ebs-scaler.service.j2 # Systemd service template
└── playbook.yml          # Main Ansible playbook
```
