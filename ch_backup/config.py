"""
config module defines Config class and default values
"""

import copy
import socket
from typing import Any

import yaml

from ch_backup import logging

DEFAULT_CONFIG = {
    'clickhouse': {
        'data_path': '/var/lib/clickhouse',
        'host': socket.gethostname(),
        'protocol': 'http',
        'port': None,
        'ca_path': None,
        'timeout': 90,
        'user': 'clickhouse',
        'group': 'clickhouse',
    },
    'backup': {
        'exclude_dbs': ['system', 'default'],
        'path_root': None,
        'deduplicate_parts': True,
        'deduplication_age_limit': {
            'days': 7,
        },
        'min_interval': {
            'minutes': 0,
        },
        'time_format': '%Y-%m-%d %H:%M:%S %z',
        # Retain policy for the purge command that sets the maximum age of
        # backups to keep.
        'retain_time': {},
        # Retain policy for the purge command that sets the maximum number of
        # valid backups to keep.
        'retain_count': None,
        # User data including into backup metadata.
        'labels': {},
        'keep_freezed_data_on_failure': True,
    },
    'storage': {
        'type': 's3',
        'credentials': {
            'endpoint_url': None,
            'access_key_id': None,
            'secret_access_key': None,
            'bucket': None,
        },
        'boto_config': {
            'addressing_style': 'auto',
            'region_name': 'us-east-1',
        },
        'disable_ssl_warnings': True,
        # Chunk size used when uploading / downloading data, in bytes.
        'chunk_size': 8 * 1024 * 1024,
        # Buffer size, in bytes.
        'buffer_size': 128 * 1024 * 1024,
        # The maximum number of chunks on which uploading or downloading data
        # can be split. If data_size > chunk_size * max_chunk_count,
        # chunk_size will be multiplied on a required number of times
        # to satisfy the limit.
        'max_chunk_count': 10000,
        # How many files we can delete by bulk delete operation in one call
        'bulk_delete_chunk_size': 1000,
    },
    'encryption': {
        'type': 'nacl',
        # Chunk size used when encrypting / decrypting data, in bytes.
        'chunk_size': 8 * 1024 * 1024,
        # Buffer size, in bytes.
        'buffer_size': 128 * 1024 * 1024,
        # Encryption key.
        'key': None,
    },
    'filesystem': {
        'type': 'unlimited',
        # Chunk size used when reading from / writing to filesystem, in bytes.
        'chunk_size': 1 * 1024 * 1024,
        # Buffer size, in bytes.
        'buffer_size': 128 * 1024 * 1024,
    },
    'multiprocessing': {
        'workers': 4,
    },
    'main': {
        'user': 'clickhouse',
        'group': 'clickhouse',
        'drop_privileges': True,
        'ca_bundle': [],
        'disable_ssl_warnings': False,
    },
    'logging': {
        'version': 1,
        'formatters': {
            'ch-backup': {
                'format': '%(asctime)s [%(levelname)s]: %(message)s',
            },
            'boto': {
                'format': '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            },
        },
        'handlers': {
            'ch-backup': {
                'class': 'logging.FileHandler',
                'filename': '/var/log/ch-backup/ch-backup.log',
                'formatter': 'ch-backup',
            },
            'boto': {
                'class': 'logging.FileHandler',
                'filename': '/var/log/ch-backup/boto.log',
                'formatter': 'boto',
            },
        },
        'loggers': {
            'ch-backup': {
                'handlers': ['ch-backup'],
                'level': 'DEBUG',
            },
            'botocore': {
                'handlers': ['boto'],
                'level': 'DEBUG',
            },
            'boto3': {
                'handlers': ['boto'],
                'level': 'DEBUG',
            },
        },
    },
}


class Config:
    """
    Config for all components
    """

    def __init__(self, config_file: str) -> None:
        self._conf = copy.deepcopy(DEFAULT_CONFIG)
        self._read_config(file_name=config_file)

    def _recursively_update(self, base_dict, update_dict):
        for key, value in update_dict.items():
            if isinstance(value, dict):
                if key not in base_dict:
                    base_dict[key] = {}
                self._recursively_update(base_dict[key], update_dict[key])
            else:
                base_dict[key] = value

    def _read_config(self, file_name):
        with open(file_name, 'r') as fileobj:
            try:
                custom_config = yaml.safe_load(fileobj)
                if custom_config:
                    self._recursively_update(self._conf, custom_config)
            except yaml.YAMLError as e:
                raise RuntimeError(f'Failed to load config file: {e}')

    def __getitem__(self, item):
        try:
            return self._conf[item]
        except KeyError:
            logging.critical('Config item "%s" was not defined', item)
            raise

    def __setitem__(self, item, value):
        try:
            self._conf[item] = value
        except KeyError:
            logging.critical('Config item "%s" was not defined', item)
            raise

    def get(self, key: str, default: Any = None) -> Any:
        """
        Returns value by key or default
        """

        return self._conf.get(key, default)
