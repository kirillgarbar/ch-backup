"""
Clickhouse-control classes module
"""

import os
import shutil
from hashlib import md5
from tarfile import BLOCKSIZE  # type: ignore
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence, Union

from pkg_resources import parse_version

from ch_backup import logging
from ch_backup.backup.metadata import TableMetadata
from ch_backup.clickhouse.client import ClickhouseClient
from ch_backup.util import chown_dir_contents, retry, strip_query

GET_TABLES_SQL = strip_query("""
    SELECT
        database,
        name,
        engine,
        engine_full,
        create_table_query,
        data_paths[1] "data_path",
        uuid
    FROM system.tables
    WHERE database = '{db_name}'
      AND (empty({tables}) OR has(cast({tables}, 'Array(String)'), name))
    ORDER BY metadata_modification_time
    FORMAT JSON
""")

GET_TABLES_COMPAT_20_3_SQL = strip_query("""
    SELECT
        database,
        name,
        engine,
        engine_full,
        create_table_query,
        data_paths[1] "data_path",
        NULL "uuid"
    FROM system.tables
    WHERE database = '{db_name}'
      AND (empty({tables}) OR has(cast({tables}, 'Array(String)'), name))
    ORDER BY metadata_modification_time
    FORMAT JSON
""")

GET_TABLES_COMPAT_19_14_SQL = strip_query("""
    SELECT
        database,
        name,
        engine,
        engine_full,
        create_table_query,
        data_path,
        NULL "uuid"
    FROM system.tables
    WHERE database = '{db_name}'
      AND (empty({tables}) OR has(cast({tables}, 'Array(String)'), name))
    ORDER BY metadata_modification_time
    FORMAT JSON
""")

CHECK_TABLE_SQL = strip_query("""
    SELECT countIf(database = '{db_name}' AND name = '{table_name}')
    FROM system.tables
    FORMAT TSVRaw
""")

PART_ATTACH_SQL = strip_query("""
    ALTER TABLE `{db_name}`.`{table_name}`
    ATTACH PART '{part_name}'
""")

TABLE_ATTACH_SQL = strip_query("""
    ATTACH TABLE `{database}`.`{table}`
""")

FREEZE_TABLE_SQL = strip_query("""
    ALTER TABLE `{db_name}`.`{table_name}`
    FREEZE
""")

GET_DATABASES_SQL = strip_query("""
    SELECT
        name
    FROM system.databases
    WHERE name NOT IN ('system', '_temporary_and_external_tables')
    FORMAT JSON
""")

SHOW_CREATE_DATABASE_SQL = strip_query("""
    SHOW CREATE DATABASE `{db_name}`
    FORMAT TSVRaw
""")

GET_DATABASE_ENGINE = strip_query("""
    SELECT engine FROM system.databases WHERE name='{db_name}'
    FORMAT TSVRaw
""")

GET_TABLE_PARTITIONS_SQL = strip_query("""
    SELECT DISTINCT partition
    FROM system.parts
    WHERE active
      AND database = '{db_name}'
      AND table = '{table_name}'
    FORMAT JSON
""")

GET_VERSION_SQL = strip_query("""
    SELECT version()
    FORMAT TSVRaw
""")

GET_MACROS_SQL = strip_query("""
    SELECT macro, substitution FROM system.macros
    FORMAT JSON
""")

GET_ACCESS_CONTROL_OBJECTS_SQL = strip_query("""
    SELECT id FROM system.{type} WHERE storage='disk' or storage='local directory'
    FORMAT JSON
""")


class Table(SimpleNamespace):
    """
    Table.
    """
    def __init__(self, database: str, name: str, engine: str, data_path: str, create_statement: str,
                 uuid: Optional[str]) -> None:
        super().__init__()
        self.database = database
        self.name = name
        self.engine = engine
        self.data_path = data_path
        self.create_statement = create_statement
        self.uuid = uuid


class Partition(SimpleNamespace):
    """
    Table partition.
    """
    def __init__(self, database: str, table: str, name: str) -> None:
        super().__init__()
        self.database = database
        self.table = table
        self.name = name


class FreezedPart(SimpleNamespace):
    """
    Freezed data part.
    """
    def __init__(self, database: str, table: str, name: str, path: str, checksum: str, size: int):
        super().__init__()
        self.database = database
        self.table = table
        self.name = name
        self.path = path
        self.checksum = checksum
        self.size = size


class ClickhouseCTL:
    """
    ClickHouse control tool.
    """
    def __init__(self, config: dict) -> None:
        self._config = config
        self._ch_client = ClickhouseClient(config)
        self._root_data_path = config['data_path']
        self._shadow_data_path = os.path.join(self._root_data_path, 'shadow')
        self._ch_version = self._ch_client.query(GET_VERSION_SQL)

    def chown_detached_table_parts(self, table: Table) -> None:
        """
        Change permissions (owner and group) of detached data parts for the
        specified table. New values for permissions are taken from the config.
        """
        detached_path = self._get_table_detached_path(table)
        self.chown_dir(detached_path)

    def attach_part(self, table: Table, part_name: str) -> None:
        """
        Attach data part to the specified table.
        """
        query_sql = PART_ATTACH_SQL.format(db_name=table.database, table_name=table.name, part_name=part_name)

        self._ch_client.query(query_sql)

    def attach_table(self, table: Union[TableMetadata, Table]) -> None:
        """
        Attach data part to the specified table.
        """
        query_sql = TABLE_ATTACH_SQL.format(database=table.database, table=table.name)

        self._ch_client.query(query_sql)

    def freeze_table(self, table: Table) -> Sequence[FreezedPart]:
        """
        Make snapshot of the specified table.
        """
        query_sql = FREEZE_TABLE_SQL.format(db_name=table.database, table_name=table.name)

        self._ch_client.query(query_sql)

        return self._get_freezed_parts(table)

    def remove_freezed_data(self) -> None:
        """
        Remove all freezed partitions.
        """
        logging.debug('Removing shadow data: %s', self._shadow_data_path)
        self._remove_shadow_data(self._shadow_data_path)

    def remove_freezed_part(self, part: FreezedPart) -> None:
        """
        Remove the freezed part.
        """
        logging.debug('Removing freezed part: %s', part.path)
        self._remove_shadow_data(part.path)

    def get_databases(self, exclude_dbs: Optional[Sequence[str]] = None) -> Sequence[str]:
        """
        Get list of all databases
        """
        if not exclude_dbs:
            exclude_dbs = []

        result: List[str] = []
        ch_resp = self._ch_client.query(GET_DATABASES_SQL)
        if 'data' in ch_resp:
            result = [row['name'] for row in ch_resp['data'] if row['name'] not in exclude_dbs]

        return result

    def get_database_schema(self, db_name: str) -> str:
        """
        Return database schema (CREATE DATABASE query).
        """
        query_sql = SHOW_CREATE_DATABASE_SQL.format(db_name=db_name)
        return self._ch_client.query(query_sql)

    def get_tables_ordered(self, db_name: str, tables: Optional[Sequence[str]] = None) -> Sequence[Table]:
        """
        Get ordered by mtime list of all database tables
        """
        query_sql: str
        if self._match_ch_version(min_version='20.4'):
            query_sql = GET_TABLES_SQL
        elif self._match_ch_version(min_version='19.15'):
            query_sql = GET_TABLES_COMPAT_20_3_SQL
        else:
            query_sql = GET_TABLES_COMPAT_19_14_SQL

        query_sql = query_sql.format(db_name=db_name, tables=tables or [])
        result: List[Table] = []
        for row in self._ch_client.query(query_sql)['data']:
            result.append(_make_table(row))

        return result

    def get_table(self, db_name: str, table_name: str) -> Table:
        """
        Get ordered by mtime list of all database tables
        """
        query_sql: str
        if self._match_ch_version(min_version='20.4'):
            query_sql = GET_TABLES_SQL
        elif self._match_ch_version(min_version='19.15'):
            query_sql = GET_TABLES_COMPAT_20_3_SQL
        else:
            query_sql = GET_TABLES_COMPAT_19_14_SQL

        query_sql = query_sql.format(db_name=db_name, tables=[table_name])
        return _make_table(self._ch_client.query(query_sql)['data'][0])

    def does_table_exist(self, db_name: str, table_name: str) -> bool:
        """
        Return True if the specified table exists.
        """
        query_sql = CHECK_TABLE_SQL.format(db_name=db_name, table_name=table_name)
        return bool(int(self._ch_client.query(query_sql)))

    def get_partitions(self, table: Table) -> Sequence[Partition]:
        """
        Get dict with all table parts
        """
        query_sql = GET_TABLE_PARTITIONS_SQL.format(db_name=table.database, table_name=table.name)

        data = self._ch_client.query(query_sql)['data']
        return [Partition(table.database, table.name, item['partition']) for item in data]

    def restore_meta(self, query_sql: str) -> None:
        """
        Restore database or table meta sql
        """
        self._ch_client.query(query_sql)

    def get_database_metadata_path(self, database: str) -> str:
        """
        Get filesystem absolute path to databse metadata.
        """
        return os.path.join(self._root_data_path, 'metadata', database)

    def get_detached_part_path(self, table: Table, part_name: str) -> str:
        """
        Get filesystem absolute path to detached data part.
        """
        return os.path.join(self._get_table_detached_path(table), part_name)

    def get_version(self) -> str:
        """
        Get ClickHouse version.
        """
        return self._ch_version

    def get_access_control_objects(self) -> Sequence[str]:
        """
        Returns all access control objects.
        """
        result: List[str] = []

        for obj_type in ['users', 'roles', 'quotas', 'row_policies', 'settings_profiles']:
            ch_resp = self._ch_client.query(GET_ACCESS_CONTROL_OBJECTS_SQL.format(type=obj_type))
            result.extend(map(lambda row: row['id'], ch_resp.get('data', [])))

        return result

    def _get_freezed_parts(self, table: Table) -> Sequence[FreezedPart]:

        path = os.path.join(self._shadow_data_path, self._get_shadow_increment(), self._get_table_data_relpath(table))

        if not os.path.exists(path):
            logging.debug('Shadow path %s is empty', path)
            return []

        freezed_parts: List[FreezedPart] = []
        for part in os.listdir(path):
            part_path = os.path.join(path, part)
            checksum = _get_part_checksum(part_path)
            size = _get_part_size(part_path)
            freezed_parts.append(FreezedPart(table.database, table.name, part, part_path, checksum, size))

        return freezed_parts

    def _get_table_data_relpath(self, table: Table) -> str:
        return os.path.relpath(table.data_path, self._root_data_path)

    def _get_table_detached_path(self, table: Table) -> str:
        return os.path.join(table.data_path, 'detached')

    def chown_dir(self, dir_path: str) -> None:
        """
        Change owner and group for all files in folder.
        """
        assert dir_path.startswith(self._root_data_path)

        chown_dir_contents(self._config['user'], self._config['group'], dir_path)

    @retry(OSError)
    def _remove_shadow_data(self, path: str) -> None:
        assert path.startswith(self._shadow_data_path)

        if os.path.exists(path):
            shutil.rmtree(path)

    def _match_ch_version(self, min_version: str) -> bool:
        return parse_version(self._ch_version) >= parse_version(min_version)

    def _get_shadow_increment(self) -> str:
        file_path = os.path.join(self._shadow_data_path, 'increment.txt')
        with open(file_path, 'r') as file:
            return file.read().strip()

    def get_macros(self) -> Dict:
        """
        Get ClickHouse macros.
        """
        ch_resp = self._ch_client.query(GET_MACROS_SQL)
        return {row['macro']: row['substitution'] for row in ch_resp.get('data', [])}


def _make_table(record: dict) -> Table:
    return Table(database=record['database'],
                 name=record['name'],
                 engine=record['engine'],
                 create_statement=record['create_table_query'],
                 data_path=record['data_path'],
                 uuid=record.get('uuid', None))


def _get_part_checksum(part_path: str) -> str:
    with open(os.path.join(part_path, 'checksums.txt'), 'rb') as f:
        return md5(f.read()).hexdigest()  # nosec


def _get_part_size(part_path: str) -> int:
    size = 0
    for file in os.listdir(part_path):
        filesize = os.path.getsize(os.path.join(part_path, file))
        remainder = filesize % BLOCKSIZE
        if remainder > 0:
            filesize += BLOCKSIZE - remainder
        size += filesize
    return size
