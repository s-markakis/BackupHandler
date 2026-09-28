import os
import subprocess
import paramiko
import configparser
import logging
from datetime import datetime
from paramiko.ssh_exception import SSHException, AuthenticationException
import concurrent.futures
import time


def setup_logger(log_file='mySQL_backup.log', log_level=logging.INFO):
    """
    Sets up a configurable logger.
    
    Args:
        log_file (str): Path to the log file.
        log_level (int): Logging level (e.g., logging.INFO, logging.DEBUG).

    Returns:
        logging.Logger: Configured logger instance.
    """
    logger = logging.getLogger()
    logger.setLevel(log_level)

    # File handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)

    return logger

def validate_config(config_path, logger=None):
    """
    Validates the configuration settings from a given config file.
    
    Args:
        config_path (str): The path to the configuration file (db_config.ini).
        logger (logging.Logger, optional): A logger instance for logging messages.

    Raises:
        ValueError: If any required configuration setting is missing or if the config file doesn't exist.
    """
    # Check if the config file exists
    if not os.path.exists(config_path):
        raise ValueError(f"Configuration file does not exist: {config_path}")

    # Read configuration
    config = configparser.ConfigParser()
    config.read(config_path)

    required_sections = ['mysql', 'backup', 'ssh']
    for section in required_sections:
        if section not in config:
            raise ValueError(f"Missing section in config: {section}")

    required_mysql_settings = ['user', 'password', 'database']
    for setting in required_mysql_settings:
        if setting not in config['mysql']:
            raise ValueError(f"Missing MySQL setting: {setting}")

    required_ssh_settings = ['host', 'port', 'user', 'password', 'remote_backup_dir']
    for setting in required_ssh_settings:
        if setting not in config['ssh']:
            raise ValueError(f"Missing SSH setting: {setting}")

    required_backup_settings = ['local_backup_dir']
    for setting in required_backup_settings:
        if setting not in config['backup']:
            raise ValueError(f"Missing backup setting: {setting}")

    logger.info("Configuration validated.")

    return config  # Return the config object for further use

def backup_to_hard_drive(config, logger=None):
    """
    Backs up the MySQL database to a local hard drive.
    
    Args:
        config (configparser.ConfigParser): The parsed configuration object.
        logger (logging.Logger, optional): A logger instance for logging messages.

    Returns:
        str or None: The path to the backup file if successful, or None if an error occurred.
    """
    # MySQL Credentials
    DB_USER = config['mysql'].get('user')
    DB_PASSWORD = config['mysql'].get('password')
    DB_NAME = config['mysql'].get('database')

    # Local Backup Directory
    LOCAL_BACKUP_DIR = config['backup'].get('local_backup_dir')

    # Generate the backup file name with timestamp
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_file = os.path.join(LOCAL_BACKUP_DIR, f"{DB_NAME}_backup_{timestamp}.sql")

    # Command to dump the MySQL database (password passed via env var)
    dump_cmd = [
        "mysqldump",
        "-u", DB_USER,
        "--result-file", backup_file,
        DB_NAME
    ]
    env = os.environ.copy()
    env["MYSQL_PWD"] = DB_PASSWORD

    try:
        logger.info("Starting MySQL backup to hard drive...")
        subprocess.run(dump_cmd, check=True, env=env)
        logger.info(f"Database backup successful: {backup_file}")
        return backup_file

    except subprocess.CalledProcessError as e:
        logger.error(f"mysqldump command failed with exit code {e.returncode}. Output: {e.output}")
        return None

    except FileNotFoundError as e:
        logger.error(f"mysqldump not found: {e}")
        return None

    except Exception as e:
        logger.error(f"Unexpected error during MySQL backup: {e}")
        return None

def transfer_backup_to_server(config, backup_file, logger=None):
    """
    Transfers the backup file to a remote server using SCP over SSH.
    
    Args:
        config (configparser.ConfigParser): The parsed configuration object.
        backup_file (str): The path to the backup file that will be transferred.
        logger (logging.Logger, optional): A logger instance for logging messages.

    Returns:
        bool: True if the transfer was successful, False otherwise.
    """
    # SSH Remote Server Configuration
    SSH_HOST = config['ssh'].get('host')
    SSH_PORT = config['ssh'].getint('port')
    SSH_USER = config['ssh'].get('user')
    SSH_PASSWORD = config['ssh'].get('password')
    REMOTE_BACKUP_DIR = config['ssh'].get('remote_backup_dir')

    try:
        logger.info(f"Connecting to remote server {SSH_HOST}...")

        # Initialize SSH client
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.WarningPolicy())

        # Try connecting to the remote server
        try:
            ssh.connect(SSH_HOST, port=SSH_PORT, username=SSH_USER, password=SSH_PASSWORD)
        except AuthenticationException:
            logger.error("Authentication failed when connecting to the server.")
            return False
        except SSHException as e:
            logger.error(f"SSH connection error: {e}")
            return False

        # Use SFTP to transfer the file
        try:
            sftp = ssh.open_sftp()
            remote_file_path = os.path.join(REMOTE_BACKUP_DIR, os.path.basename(backup_file))
            sftp.put(backup_file, remote_file_path)
            logger.info(f"Backup transferred to remote server: {remote_file_path}")
            sftp.close()

        except FileNotFoundError as e:
            logger.error(f"Backup file not found: {backup_file}. Error: {e}")
            return False
        except SSHException as e:
            logger.error(f"SFTP transfer failed: {e}")
            return False

        ssh.close()
        return True

    except Exception as e:
        logger.error(f"Unexpected error during transfer to remote server: {e}")
        return False

def retry_transfer(config, backup_file, logger=None, retries=3, delay=5):
    """
    Retries transferring the backup to the server on failure.
    
    Args:
        config (configparser.ConfigParser): The parsed configuration object.
        backup_file (str): The path to the backup file that will be transferred.
        logger (logging.Logger, optional): A logger instance for logging messages.
        retries (int): The number of retry attempts.
        delay (int): The delay (in seconds) between retries.

    Returns:
        bool: True if the transfer was successful, False otherwise.
    """
    # Check if the backup file exists
    if not os.path.exists(backup_file):
        logger.error(f"Backup file does not exist: {backup_file}")
        return False

    for attempt in range(retries):
        if transfer_backup_to_server(config, backup_file, logger):
            return True
        logger.warning(f"Retrying transfer... Attempt {attempt + 1}")
        time.sleep(delay)
    
    logger.error("Transfer failed after multiple attempts.")
    return False

def backup_mysql(config_path, logger=None):
    """
    Main function to backup MySQL database locally and transfer it to a remote server.

    Args:
        config_path (str): The path to the configuration file (db_config.ini).
        logger (logging.Logger, optional): A logger instance for logging messages.

    Returns:
        None
    """
    logger = logger or setup_logger()

    try:
        # Validate configuration before proceeding
        config = validate_config(config_path, logger)

        # Step 1: Backup to hard drive
        logger.info("Starting MySQL backup process...")
        backup_file = backup_to_hard_drive(config, logger)

        if backup_file:
            # Step 2: Transfer the backup to the external server using retries
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future_transfer = executor.submit(retry_transfer, config, backup_file, logger)
                if future_transfer.result():
                    logger.info("Backup and transfer process completed successfully.")
                else:
                    logger.error("Backup transfer process failed.")
        else:
            logger.error("Backup process failed. No file to transfer.")

    except Exception as e:
        logger.error(f"Unexpected error in the backup process: {e}")

if __name__ == "__main__":
    # Replace 'db_config.ini' with the actual path to your config file
    backup_mysql('db_config.ini')
