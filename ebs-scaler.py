#!/usr/bin/env python3

import json
import math
import os
import subprocess
import time
import sys
from typing import List
import configparser
import logging
from dataclasses import dataclass
from botocore.exceptions import ClientError

try:
    import boto3
    import psutil
    import typer
except ImportError as e:
    subprocess.check_call([sys.executable, "-m", 'pip', 'install', 'boto3', 'psutil', 'typer']) 
    import boto3
    import psutil
    import typer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = typer.Typer()

AWS_REGION = 'eu-west-1'

@dataclass
class VolumeInfo:
    volume_id: str
    device_name: str
    mountpoint: str
    partition_path: str

class EBSAutoscaler:
    def __init__(self):
        self.config_file = os.path.expanduser("/opt/ebs-autoscaler/config.ini")
        self.volume_info_file = os.path.expanduser("/opt/ebs-autoscaler/volume_info.json")
        self.config = configparser.ConfigParser()
        self.ec2_client = boto3.client('ec2', region_name=AWS_REGION)
        self.ses_client = boto3.client('ses', region_name=AWS_REGION)
        
        # Config values
        # General
        self.interval = 0
        self.threshold = 0
        self.increase_type = ""
        self.increase_gb = 0
        # Notification
        self.notification_enabled = False
        self.email_sender = ""
        self.email_recipients = []
        # Exclude
        self.excluded_volumes = []
        
        # Volume tracking
        self.partition_stats = {}  # volume_id -> {'total_gb': float, 'used_gb': float}
        
    def load_config(self) -> bool:
        """Load and validate configuration"""
        if not os.path.exists(self.config_file):
            logger.error("Configuration file not found")
            return False
            
        self.config.read(self.config_file)
        
        # Validate required sections and keys
        required_sections = ['general', 'notification']
        for section in required_sections:
            if section not in self.config:
                logger.error(f"Missing required section: {section}")
                return False
                
        # Validate general section
        general_keys = ['interval', 'threshold', 'increase_type', 'increase_gb']
        for key in general_keys:
            if key not in self.config['general'] or not self.config['general'][key]:
                logger.error(f"Missing or empty value for general.{key}")
                return False
                
        # Validate increase_type
        if self.config['general']['increase_type'].lower().strip() != 'size':
            logger.error(f"Only 'size' increase type is supported: {self.config['general']['increase_type']}")
            return False
            
        # Validate notification section if enabled
        if self.config['notification'].getboolean('enabled', False):
            notification_keys = ['email-sender', 'email-recipients']
            for key in notification_keys:
                if key not in self.config['notification'] or not self.config['notification'][key]:
                    logger.error(f"Missing or empty value for notification.{key}")
                    return False
        
        # Load all config values into class members
        try:
            # General settings
            self.interval = int(self.config['general']['interval'])
            self.threshold = float(self.config['general']['threshold'])
            self.increase_type = self.config['general']['increase_type']
            self.increase_gb = int(self.config['general']['increase_gb'])
            
            # Notification settings
            self.notification_enabled = self.config['notification'].getboolean('enabled', False)
            if self.notification_enabled:
                self.email_sender = self.config['notification']['email-sender']
                self.email_recipients = [r.strip() for r in self.config['notification']['email-recipients'].split(',')]
            
            # Exclude settings (optional)
            if 'volumes' in self.config['exclude'] and self.config['exclude']['volumes']:
                self.excluded_volumes = [v.strip() for v in self.config['exclude']['volumes'].split(',') if v.strip()]
            
            return True
        except (ValueError, KeyError) as e:
            logger.error(f"Error loading config values: {e}")
            return False

    def get_instance_id(self) -> str:
        """Get current instance ID"""
        try:
            logger.info("Getting instance ID using IMDSv2 metadata service...")
            # Get the token
            token = subprocess.check_output(
                [
                    "curl", "-s", "-X", "PUT",
                    "http://169.254.169.254/latest/api/token",
                    "-H", "X-aws-ec2-metadata-token-ttl-seconds: 21600"
                ],
                text=True
            ).strip()
            # Use the token to get instance ID
            instance_id = subprocess.check_output(
                [
                    "curl", "-s",
                    "-H", f"X-aws-ec2-metadata-token: {token}",
                    "http://169.254.169.254/latest/meta-data/instance-id"
                ],
                text=True
            ).strip()

            if not instance_id:
                logger.error("Got empty instance ID")
                return "unknown"
            
            return instance_id
        except Exception as e:
            logger.error(f"Error getting instance ID: {e}")
            return "unknown"

    def get_volume_info(self) -> List[VolumeInfo]:
        """Get volume information using lsblk command"""
        try:
            result = subprocess.run(['lsblk', '-b', '-o', 'NAME,PATH,MOUNTPOINT', '-J'], 
                                  capture_output=True, text=True)
            if result.returncode != 0:
                logger.error("Failed to get volume information using 'lsblk' tool")
                return []
            
            data = json.loads(result.stdout)
            volumes = []
            
            for device in data.get('blockdevices', []):
                # Handle both partitioned and non-partitioned volumes
                if device.get('children'):
                    # Handle partitioned volumes
                    max_size = -1
                    selected_partition = None
                    selected_mountpoint = None
                    
                    # Calculate total partition size
                    for child in device['children']:
                        try:                            
                            # Only consider mounted partitions
                            if child.get('mountpoint'):
                                usage = psutil.disk_usage(child['mountpoint'])
                                if usage.total > max_size:
                                    max_size = usage.total
                                    selected_partition = child['path']
                                    selected_mountpoint = child['mountpoint']
                            else:
                                logger.error(f"Partition {child.get('name')} is not mounted. Skipping...")
                        except Exception as e:
                            logger.error(f"Error processing partition {child.get('name')}: {e}")
                    
                    if selected_partition and selected_mountpoint:
                        try:
                            result = subprocess.run(['ebsnvme-id', '-v', selected_partition],
                                                 capture_output=True, text=True)
                            if result.returncode != 0:
                                logger.error(f"Failed to get volume ID for {selected_partition}. The partition is not an EBS volume.")
                                continue
                                
                            volume_id = result.stdout.split('Volume ID: ', 1)[1].strip()
                            
                            volumes.append(VolumeInfo(
                                volume_id=volume_id,
                                device_name=device['name'],
                                mountpoint=selected_mountpoint,
                                partition_path=selected_partition
                            ))
                        except Exception as e:
                            logger.error(f"Error getting volume ID for {selected_partition}: {e}")
                else:
                    # Handle non-partitioned volumes (root devices)
                    if device.get('mountpoint'):
                        try:
                            result = subprocess.run(['ebsnvme-id', device['path']],
                                                 capture_output=True, text=True)
                            if result.returncode != 0:
                                logger.error(f"Failed to get volume ID for {device['path']}")
                                continue
                                
                            volume_id = result.stdout.strip()
                            
                            volumes.append(VolumeInfo(
                                volume_id=volume_id,
                                device_name=device['name'],
                                mountpoint=device['mountpoint'],
                                partition_path=device['path']
                            ))
                        except Exception as e:
                            logger.error(f"Error getting volume ID for {device['path']}: {e}")
            
            return volumes
            
        except Exception as e:
            logger.error(f"Error getting volume information: {e}")
            return []

    def save_volume_info(self, volumes) -> None:
        """Save volume information to JSON file"""
        try:
            volume_data = [vars(volume) for volume in volumes]
            with open(self.volume_info_file, 'w') as f:
                json.dump(volume_data, f, indent=2)
            
        except Exception as e:
            logger.error(f"Error saving volume information: {e}")

    def load_volume_info(self) -> List[VolumeInfo]:
        """Load volume information from JSON file"""
        if not os.path.exists(self.volume_info_file):
            logger.info("No volume information found. Syncing system volume info...")
            volumes = self.get_volume_info()
            if volumes:
                self.save_volume_info(volumes)
            return volumes
            
        with open(self.volume_info_file, 'r') as f:
            data = json.load(f)
            if not data:  # If file is empty
                logger.info("Volume information file is empty. Syncing system volume info...")
                volumes = self.get_volume_info()
                if volumes:
                    self.save_volume_info(volumes)
                return volumes
            return [VolumeInfo(**volume) for volume in data]

    def get_device_size(self, device_path: str) -> int:
        """Get the size of a block device in bytes"""
        try:
            result = subprocess.run(['blockdev', '--getsize64', device_path],
                                 capture_output=True, text=True)
            if result.returncode == 0:
                return int(result.stdout.strip())
            return 0
        except Exception as e:
            logger.error(f"Error getting device size for {device_path}: {e}")
            return 0

    def resize_volume(self, volume_id: str, new_size: int) -> bool:
        """Resize EBS volume using boto3"""
        try:
            # First check current volume size and state
            response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
            current_size = response['Volumes'][0]['Size']
            volume_state = response['Volumes'][0]['State']
            
            logger.info(f"Volume {volume_id} current state: size={current_size}GB, state={volume_state}")
            
            # If volume is already at desired size and stable, no action needed
            if current_size == new_size and volume_state == 'in-use':
                logger.info(f"Volume {volume_id} is already at desired size {new_size}GB and in stable state. Trying to expand filesystem...")
                return True
            
            # If we need a resize and volume is not already being modified
            if current_size != new_size and volume_state != 'modifying':
                logger.info(f"Volume {volume_id} needs resize: current={current_size}GB, target={new_size}GB")
                logger.info(f"Initiating volume resize for {volume_id} from {current_size}GB to {new_size}GB")
                response = self.ec2_client.modify_volume(
                    VolumeId=volume_id,
                    Size=new_size
                )
                
                if response['ResponseMetadata']['HTTPStatusCode'] != 200:
                    logger.error(f"Failed to initiate volume resize for {volume_id}. Response: {response}")
                    return False
                logger.info(f"Volume modification request accepted for {volume_id}. Waiting for completion...")
            else:
                logger.info(f"Volume {volume_id} is already being modified. Waiting for completion...")
            
            # Wait for volume modification to complete
            logger.info(f"Waiting for volume {volume_id} modification to complete")
            max_attempts = 30
            delay = 60
            
            for attempt in range(max_attempts):
                try:
                    response = self.ec2_client.describe_volumes_modifications(VolumeIds=[volume_id])
                    if not response['VolumesModifications']:
                        logger.error(f"No modification found for volume {volume_id}")
                        return False
                        
                    modification = response['VolumesModifications'][0]
                    if modification['ModificationState'] == 'completed':
                        logger.info(f"Volume {volume_id} modification completed. Verifying final size...")
                        break
                    elif modification['ModificationState'] == 'failed':
                        logger.error(f"Volume {volume_id} modification failed: {modification.get('StatusMessage', 'Unknown error')}")
                        return False
                        
                    logger.info(f"Volume modification in progress (attempt {attempt + 1}/{max_attempts})")
                    time.sleep(delay)
                except Exception as e:
                    logger.error(f"Error checking volume modification status: {str(e)}")
                    return False
                    
            if attempt == max_attempts - 1:
                logger.error(f"Volume {volume_id} modification did not complete within {max_attempts * delay} seconds")
                return False
                
            # Verify final size matches our desired size
            response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
            if response['Volumes'][0]['Size'] == new_size:
                logger.info(f"Volume {volume_id} successfully resized to {new_size}GB")
                return True
            elif response['Volumes'][0]['Size'] > new_size:
                logger.info(f"Volume {volume_id} is now at {response['Volumes'][0]['Size']}GB, which is larger than the desired size {new_size}GB. No action needed.")
                return True
            else:
                logger.error(f"Volume {volume_id} modification completed but size {response['Volumes'][0]['Size']}GB does not match desired size {new_size}GB")
                return False
            
        except ClientError as e:
            logger.error(f"Error resizing volume {volume_id}: {str(e)}")
            return False

    def expand_filesystem(self, volume: VolumeInfo, expected_volume_total_size_gb: float) -> bool:
        """Expand filesystem after volume resize to the fully available size of the EBS volume"""
        try:
            partition_path = volume.partition_path
            mountpoint = volume.mountpoint
            device_path = f"/dev/{volume.device_name}"
            logger.info(f"Starting filesystem expansion for {partition_path}")
            
            # Wait for root device size to update
            logger.info(f"Checking if AWS volume scaling reflected on root device {device_path} with desired size {expected_volume_total_size_gb}GB")
            for attempt in range(12):  # Check for up to 1 minute (12 * 5s)
                try:
                    current_size_gb = math.ceil(self.get_device_size(device_path)/(1024 ** 3))
                    if current_size_gb == expected_volume_total_size_gb:
                        logger.info(f"EBS volume at root {device_path} have size synced to desired {expected_volume_total_size_gb}GB. Proceeding to expand filesystem...")
                        break
                    logger.info(f"Device size check attempt {attempt + 1}/12: Current size {current_size_gb}GB, waiting for {expected_volume_total_size_gb}GB")
                except Exception as e:
                    logger.error(f"Error checking device size: {e}")
                
                if attempt == 11:  # Last attempt
                    logger.error(f"Root device {device_path} did not reach expected size after 12 attempts")
                    return False
                time.sleep(5)
            
            # Check if this is a partition (path contains p{number})
            is_partition = 'p' in os.path.basename(partition_path)
            
            logger.info(f"Checking if {partition_path} is a partition and growable")
            if is_partition:
                # For partitions, we need to grow the partition first
                logger.info(f"Growing partition {partition_path} on device {device_path}")
                partition_number = os.path.basename(partition_path).split('p')[-1]
                
                growpart_result = subprocess.run(
                    ['growpart', device_path, partition_number],
                    capture_output=True,
                    text=True
                )
                
                if growpart_result.returncode != 0:
                    logger.error(f"Failed to grow partition '{partition_path}': {growpart_result.stderr}")
                    return False
                    
                logger.info(f"Successfully grew partition: {partition_path}")
            
            # Check filesystem type
            logger.info(f"Checking filesystem type for {partition_path} to expand")
            result = subprocess.run(['blkid', '-o', 'value', '-s', 'TYPE', partition_path],
                                 capture_output=True, text=True)
            fs_type = result.stdout.strip()
            
            # Expand filesystem
            try:
                logger.info(f"Expanding {fs_type} filesystem on {partition_path}")
                if fs_type == 'xfs':
                    # xfs_growfs uses mountpoint to expand the filesystem
                    result = subprocess.run(['xfs_growfs', '-d', mountpoint], 
                                         capture_output=True, text=True)
                    if result.returncode != 0:
                        logger.error(f"Failed to expand XFS filesystem: {result.stderr}")
                        return False
                else:
                    # resize2fs uses partition path to expand the filesystem
                    result = subprocess.run(['resize2fs', partition_path],
                                         capture_output=True, text=True)
                    if result.returncode != 0:
                        logger.error(f"Failed to expand ext filesystem: {result.stderr}")
                        return False
                
                logger.info(f"Successfully expanded {fs_type} filesystem on {partition_path}")
                return True
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to expand filesystem on {partition_path}: {e}")
                return False
                    
        except Exception as e:
            logger.error(f"Error expanding filesystem on {partition_path}: {e}")
            return False

    def send_notification(self, volumes_scaled: List[dict]) -> None:
        """Send email notification about volume resize using SES"""
        if not self.notification_enabled:
            return
            
        try:
            logger.info("Getting instance id for notification...")
            instance_id = self.get_instance_id()
            if not volumes_scaled:
                logger.error("No scale info provided for sending notification. Skipping...")
                return
            
            # Build HTML table of volume information
            html_table = """
            <table style="border-collapse: collapse; width: 100%;">
                <thead>
                    <tr style="background-color: #f2f2f2;">
                        <th style="border: 1px solid #ddd; padding: 8px; text-align: left;">Volume ID</th>
                        <th style="border: 1px solid #ddd; padding: 8px; text-align: left;">Mount Point</th>
                        <th style="border: 1px solid #ddd; padding: 8px; text-align: left;">Device Name</th>
                        <th style="border: 1px solid #ddd; padding: 8px; text-align: left;">Partition Path</th>
                        <th style="border: 1px solid #ddd; padding: 8px; text-align: right;">Scale Threshold</th>
                        <th style="border: 1px solid #ddd; padding: 8px; text-align: right;">Expanded by size(GB)</th>
                        <th style="border: 1px solid #ddd; padding: 8px; text-align: right;">Previous Device size(GB)</th>
                        <th style="border: 1px solid #ddd; padding: 8px; text-align: right;">New Device size(GB)</th>
                        <th style="border: 1px solid #ddd; padding: 8px; text-align: right;">New Overall Volume size(GB)</th>
                    </tr>
                </thead>
                <tbody>"""
            
            for scaled_volume in volumes_scaled:
                logger.info(f"Found scaled volume: {scaled_volume['volume'].volume_id}, Preparing to send notification...")
                stats = self.partition_stats.get(scaled_volume['volume'].volume_id, {})
                html_table += f"""
                    <tr>
                        <td style="border: 1px solid #ddd; padding: 8px;">{scaled_volume['volume'].volume_id}</td>
                        <td style="border: 1px solid #ddd; padding: 8px;">{scaled_volume['volume'].mountpoint or 'N/A'}</td>
                        <td style="border: 1px solid #ddd; padding: 8px;">{scaled_volume['volume'].device_name or 'N/A'}</td>
                        <td style="border: 1px solid #ddd; padding: 8px;">{scaled_volume['volume'].partition_path or 'N/A'}</td>
                        <td style="border: 1px solid #ddd; padding: 8px; text-align: right;">{self.threshold}%</td>
                        <td style="border: 1px solid #ddd; padding: 8px; text-align: right;">{scaled_volume['expanded_size_gb']}</td>
                        <td style="border: 1px solid #ddd; padding: 8px; text-align: right;">{scaled_volume['last_device_size_gb']}</td>
                        <td style="border: 1px solid #ddd; padding: 8px; text-align: right;">{scaled_volume['new_device_size_total_gb']}</td>
                        <td style="border: 1px solid #ddd; padding: 8px; text-align: right;">{scaled_volume['new_volume_size_gb']}</td>
                    </tr>"""
            
            html_table += """
                </tbody>
            </table>"""
            
            subject = f"⚠️📈 EBS Volume Scaling Alert: Multiple Volumes Resized on Instance {instance_id}"
            
            html_body = f"""
            <html>
            <body>
                <p>Hello,</p>
                <p>EBS Volume Auto-scaling has been triggered for multiple volumes on instance: <b>{instance_id}</b> with the following details:</p>
                {html_table}
                <br><br>
                <p>Regards,</p>
                <p>EBS Volume Auto-scaler, Jenkins & Ansible</p>
            </body>
            </html>"""
            
            # Send email using SES
            response = self.ses_client.send_email(
                Source=self.email_sender,
                Destination={
                    'ToAddresses': self.email_recipients
                },
                Message={
                    'Subject': {
                        'Data': subject,
                        'Charset': 'UTF-8'
                    },
                    'Body': {
                        'Html': {
                            'Data': html_body,
                            'Charset': 'UTF-8'
                        }
                    }
                }
            )
            
            if response['ResponseMetadata']['HTTPStatusCode'] == 200:
                logger.info(f"Notification sent for volumes scaled on instance {instance_id} to {len(self.email_recipients)} recipients")
            else:
                logger.error(f"Failed to send notification for scaled volumes list")
            
        except ClientError as e:
            logger.error(f"Error sending notification: {e}")
        except Exception as e:
            logger.error(f"Unexpected error sending notification: {e}")

    def is_scaling_required(self, volume: VolumeInfo) -> tuple[bool, float, float]:
        """Check disk usage and determine if scaling is needed"""
        try:
            usage = psutil.disk_usage(volume.mountpoint)

            do_scale = False
            current_total_gb = (usage.total / (1024 ** 3))
            usage_percent = usage.percent

            size_to_scale = 0
            if usage_percent > self.threshold:
                do_scale = True
                size_to_scale = current_total_gb + self.increase_gb

            return do_scale, usage_percent, size_to_scale
        except Exception as e:
            logger.error(f"Error checking disk usage for {volume.mountpoint}: {e}")
            return False, 0, 0
            
    def perform_scaling(self, volume: VolumeInfo, total_new_size_quote_gb: float) -> bool:
        """Perform scaling operation"""
        try:
            # Step 1: Get volume total size
            root_device_name = volume.device_name
            root_device_path = f"/dev/{root_device_name}" # /dev/nvme0n1
            volume_total_size_bytes = self.get_device_size(root_device_path)
            volume_total_size_gb = volume_total_size_bytes / (1024 ** 3)

            # Step 2: Get partition sizes and its sum of all
            partitions = [
                p.device for p in psutil.disk_partitions(all=True)
                if p.device.startswith(root_device_path)
            ]
            partition_sum_bytes = 0
            for partition in partitions:
                partition_sum_bytes += self.get_device_size(partition)

            # Step 3: Calculate free space in the volume
            free_space_bytes = volume_total_size_bytes - partition_sum_bytes
            free_space_gb = free_space_bytes / (1024 ** 3)

            # Step 4: Calculate final size to scale
            additional_volume_needed_gb = math.ceil(self.increase_gb - free_space_gb)

            # Step 5: Check if we have enough free space to scale
            if additional_volume_needed_gb > 0:
                try:
                    expected_new_volume_total_size_gb = math.ceil(volume_total_size_gb + additional_volume_needed_gb)
                    if additional_volume_needed_gb < self.increase_gb:
                        logger.info(f"Combining available unused space of {free_space_gb:.2f}GB on volume {volume.volume_id} for scaling to reach {expected_new_volume_total_size_gb}GB")
                    if self.resize_volume(volume.volume_id, expected_new_volume_total_size_gb):
                        logger.info(f"EBS Volume {volume.volume_id} resized to {expected_new_volume_total_size_gb}GB from {volume_total_size_gb}GB")
                    else:
                        logger.error(f"Failed to resize volume {volume.volume_id} to {additional_volume_needed_gb}GB")
                        return False
                except Exception as e:
                    logger.error(f"Error resizing volume: {e}")
                    return False
            else:
                # If no scale only expand then the volume size remains the same
                logger.info(f'Found enough free space to expand the volume to {volume_total_size_gb}GB')
                expected_new_volume_total_size_gb = math.ceil(volume_total_size_gb)
            
            logger.info(f"Starting to expand filesystem to the fully available size of the EBS volume")
            if self.expand_filesystem(volume, expected_new_volume_total_size_gb):
                logger.info(f"EBS Volume {volume.volume_id} expanded to {math.ceil(total_new_size_quote_gb):.2f}GB")
            else:
                logger.error(f"Failed to expand filesystem on volume {volume.volume_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error performing scaling operation: {e}")
            return False
        
    def validate_prerequisites(self) -> bool:
        """Validate all prerequisites before starting the service"""
        try:
            # 1. Check required tools
            required_tools = {
                'lsblk': 'Get block device information',
                'ebsnvme-id': 'Get EBS volume ID',
                'blockdev': 'Get device size',
                'growpart': 'Grow partitions',
                'blkid': 'Get filesystem type',
                'xfs_growfs': 'Expand XFS filesystem',
                'resize2fs': 'Expand ext filesystem'
            }
            
            for tool, purpose in required_tools.items():
                result = subprocess.run(['which', tool], capture_output=True, text=True)
                if result.returncode != 0:
                    logger.error(f"Required tool '{tool}' ({purpose}) not found in PATH")
                    return False
                logger.info(f"Tool '{tool}' found: {result.stdout.strip()}")

            # 2. Check config file permissions
            if not os.path.exists(self.config_file):
                logger.error(f"Config file not found: {self.config_file}")
                return False
                
            if not os.access(self.config_file, os.R_OK):
                logger.error(f"Config file not readable: {self.config_file}")
                return False
                
            # 3. Check volume info directory permissions
            volume_info_dir = os.path.dirname(self.volume_info_file)
            if not os.path.exists(volume_info_dir):
                try:
                    os.makedirs(volume_info_dir, mode=0o755)
                except Exception as e:
                    logger.error(f"Failed to create volume info directory: {e}")
                    return False
                    
            if not os.access(volume_info_dir, os.W_OK):
                logger.error(f"Volume info directory not writable: {volume_info_dir}")
                return False

            # 4. Check AWS credentials
            try:
                self.ec2_client.describe_volumes(MaxResults=10)
            except Exception as e:
                logger.error(f"AWS credentials validation failed: {e}")
                return False

            # 5. Check if running as root (required for device operations)
            if os.geteuid() != 0:
                logger.error("Service must be run as root for device operations")
                return False

            logger.info("All prerequisites validated successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error during prerequisite validation: {e}")
            return False

@app.command()
def monitor(
    daemon: bool = typer.Option(False, "--daemon", "-d", help="Run in daemon mode")
):
    """
    Monitor and scale EBS volumes
    Note: Config and Volume info is loaded only once at start, not updated in between. Needs service restart to reload values.
    """
    logger.info("Starting EBS Volume Auto-scaling service")
    scaler = EBSAutoscaler()
    
    # Validate prerequisites first
    if not scaler.validate_prerequisites():
        logger.error("Prerequisite validation failed. Exiting...")
        sys.exit(1)
        
    logger.info(f"Loading configuration from {scaler.config_file}...")
    if not scaler.load_config():
        logger.error("Failed to load configuration")
        sys.exit(1)
    logger.info("Configuration loaded successfully")
        
    logger.info(f"Loading volume information from {scaler.volume_info_file}...")
    volumes = scaler.load_volume_info()
    if not volumes:
        logger.error("No EBS volumes found attached to this instance to monitor")
        sys.exit(1)
    logger.info("Volume information loaded successfully")
    
    logger.info(f"Monitoring {len(volumes)} EBS volumes...")
    
    while True:
        # Track the volumes scaled in this interval
        volumes_scaled = []
        
        for volume in volumes:
            if volume.volume_id in scaler.excluded_volumes:
                logger.info(f"Skipping excluded volume {volume.volume_id}")
                continue
            

            # Check disk usage and determine if scaling is needed
            do_scale, usage_percent, new_device_size_total_gb = scaler.is_scaling_required(volume)
            
            if do_scale:
                logger.info(f"Partition {volume.partition_path} is at {usage_percent}% usage, above the set threshold {scaler.threshold}% . Initiating scaling operation.")
                try:
                    if scaler.perform_scaling(volume, new_device_size_total_gb):
                        volumes_scaled.append({
                            'volume': volume,
                            'last_device_size_gb': f"{math.ceil(new_device_size_total_gb - scaler.increase_gb):.2f}",
                            'expanded_size_gb': f"{math.ceil(new_device_size_total_gb - (new_device_size_total_gb - scaler.increase_gb)):.2f}",
                            'new_device_size_total_gb': f"{math.ceil(scaler.get_device_size(volume.partition_path)/(1024 ** 3)):.2f}",
                            'new_volume_size_gb': f"{math.ceil(scaler.get_device_size(f'/dev/{volume.device_name}')/(1024 ** 3)):.2f}"
                        })
                    else:
                        logger.error(f"Failed to scale volume {volume.volume_id}. Retrying in next interval...")
                        continue
                except Exception as e:
                    logger.error(f"Error performing scaling operation: {e}")
                    continue
            else:
                logger.info(f"Volume {volume.volume_id} is at {usage_percent}% usage, below the set threshold. No scaling required.")
                continue
        
        if volumes_scaled:
            logger.info(f"Sending notification for {len(volumes_scaled)} volumes scaled")
            try:
                scaler.send_notification(volumes_scaled)
            except Exception as e:
                logger.error(f"Error sending notification: {e}")
            
            volumes_scaled = []

        if not daemon:
            break
            
        minutes = scaler.interval // 60
        seconds = scaler.interval % 60
        logger.info(f"Checking again in {minutes} minutes and {seconds} seconds")
        time.sleep(scaler.interval)

if __name__ == "__main__":
    app()
