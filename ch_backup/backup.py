"""
Clickhouse backup logic
"""

import logging
import os
from datetime import datetime, timedelta

from ch_backup.clickhouse.layout import (ClickhouseBackupStructure,
                                         ClickhousePartInfo)
from ch_backup.exceptions import ClickHouseBackupError, InvalidBackupStruct


class ClickhouseBackup:
    """
    Clickhouse backup logic
    """

    # pylint: disable=too-many-arguments,too-many-instance-attributes
    def __init__(self, config, ch_ctl, backup_layout):
        self._config = config
        self._ch_ctl = ch_ctl
        self._existing_backups = []
        self._dedup_time = None

        self._backup_layout = backup_layout

    def show(self, backup_name):
        """
        Show backup meta struct
        """
        backup_meta_path = self._backup_layout.get_backup_meta_path(
            backup_name)
        backup_meta = self._load_backup_meta(backup_meta_path)
        return backup_meta

    def list(self):
        """
        Get list of existing backup names.
        """
        return self._backup_layout.get_existing_backups_names()

    def backup(self, databases=None):
        """
        Start backup
        """
        if databases is None:
            databases = self._ch_ctl.get_all_databases(
                self._config['exclude_dbs'])

        # load existing backups if deduplication is enabled
        if self._config.get('deduplicate_parts'):
            backup_age_limit = datetime.utcnow() - timedelta(
                **self._config['deduplication_age_limit'])

            self._load_existing_backups(backup_age_limit)

        backup_meta = ClickhouseBackupStructure()
        backup_meta.name = self._backup_layout.backup_name
        backup_meta.path = self._backup_layout.backup_path

        backup_meta.mark_start()

        logging.debug('Starting backup "%s" for databases: %s',
                      backup_meta.name, ', '.join(databases))

        for db_name in databases:
            # run backup per db
            self.backup_database(db_name, backup_meta)
        self._ch_ctl.remove_shadow_data()

        backup_meta.mark_end()
        backup_meta_json = backup_meta.dump_json()
        logging.debug('Resultant backup meta:\n%s', backup_meta_json)
        self._backup_layout.save_backup_meta(backup_meta_json)

        return backup_meta.name

    def restore(self, backup_name, databases=None):
        """
        Restore specified backup
        """
        self._backup_layout.backup_name = backup_name
        backup_meta = ClickhouseBackupStructure()
        backup_meta.load_json(self._backup_layout.get_backup_meta())

        if databases is None:
            databases = backup_meta.get_databases()
        else:
            # check all required databases exists in backup meta
            missed_databases = (db_name for db_name in databases
                                if db_name not in backup_meta.get_databases())
            if missed_databases:
                logging.critical(
                    'Required databases %s were not found in backup meta: %s',
                    ', '.join(missed_databases), backup_meta.path)
                raise ClickHouseBackupError(
                    'Required databases were not found in backup struct')

        for db_name in databases:
            self.restore_database(db_name, backup_meta)

    def _get_backup_path(self, backup_name):
        """
        Get storage backup path
        """
        return os.path.join(self._config['path_root'], backup_name)

    def backup_database(self, db_name, backup_meta):
        """
        Backup database
        """
        backup_meta.add_database(db_name)

        logging.debug('Running database backup: %s', db_name)

        # get db objects ordered by mtime
        tables = self._ch_ctl.get_all_db_tables_ordered(db_name)
        for table_name in tables:
            logging.debug('Running table "%s.%s" backup', db_name, table_name)

            # save table sql
            backup_meta.add_table_sql_path(db_name, table_name,
                                           self._backup_table_meta(
                                               db_name, table_name))

            parts_rows = self._ch_ctl.get_all_table_parts_info(
                db_name, table_name)

            # remove previous data from shadow path
            self._ch_ctl.remove_shadow_data()

            # freeze table parts
            try:
                self._ch_ctl.freeze_table(db_name, table_name)
            except Exception as exc:
                logging.critical('Unable to freeze: %s', exc)
                raise ClickHouseBackupError

            for part_row in parts_rows:
                part_info = ClickhousePartInfo(meta=part_row)
                logging.debug('Working on part %s: %s', part_info.name,
                              part_info)

                # calculate backup total rows and bytes count
                backup_meta.rows += int(part_info.rows)
                backup_meta.bytes += int(part_info.bytes)
                # TODO: save backup total and real (exclude deduplicated)

                # trying to find part in storage
                link, part_remote_paths = self._deduplicate_part(part_info)

                if not link:
                    # preform backup if deduplication is not available
                    logging.debug('Starting backup for "%s.%s" part: %s',
                                  db_name, table_name, part_info.name)

                    part_remote_paths = self._backup_layout.save_part_data(
                        db_name, table_name, part_info.name)

                # save part files and meta in backup struct
                backup_meta.add_part_contents(
                    db_name,
                    table_name,
                    part_info.name,
                    part_remote_paths,
                    part_info.get_contents(),
                    link=link)

            logging.debug('Waiting for uploads')
            self._backup_layout.wait()

        # save database sql
        backup_meta.set_db_sql_path(db_name,
                                    self._backup_database_meta(db_name))

    def _backup_database_meta(self, db_name):
        """
        Backup database sql
        """

        db_sql_abs_path = self._ch_ctl.get_db_sql_abs_path(db_name)
        logging.debug('Making database "%s" sql backup: %s', db_name,
                      db_sql_abs_path)

        with open(db_sql_abs_path) as file_fd:
            file_contents = file_fd.read()
        metadata = file_contents.replace('ATTACH ', 'CREATE ', 1)
        return self._backup_layout.save_database_meta(db_name, metadata)

    def _backup_table_meta(self, db_name, table_name):
        """
        Backup table sql
        """

        table_sql_abs_path = self._ch_ctl.get_table_sql_abs_path(
            db_name, table_name)
        logging.debug('Making table "%s.%s" sql backup: %s', db_name,
                      table_name, table_sql_abs_path)

        with open(table_sql_abs_path) as file_fd:
            file_contents = file_fd.read()

        metadata = file_contents.replace(
            'ATTACH TABLE ',
            'CREATE TABLE {db_name}.'.format(db_name=db_name),
            1)

        return self._backup_layout.save_table_meta(db_name, table_name,
                                                   metadata)

    def restore_database(self, db_name, backup_meta):
        """
        Restore database
        """

        logging.debug('Running database restore: %s', db_name)

        # restore db sql
        db_sql = self._backup_layout.download_str(
            backup_meta.get_db_sql_path(db_name))
        self._ch_ctl.restore_meta(db_sql)

        logging.debug('Restoring "%s" tables', db_name)

        # restore table sql
        for table_sql_path in backup_meta.get_tables_sql_paths(db_name):
            table_sql = self._backup_layout.download_str(table_sql_path)
            self._ch_ctl.restore_meta(table_sql)

        # restore table data (download and attach parts)
        for table_name in backup_meta.get_tables(db_name):
            logging.debug('Running table "%s.%s" data restore', db_name,
                          table_name)

            attach_parts = []
            for part_name in backup_meta.get_parts(db_name, table_name):
                logging.debug('Fetching "%s.%s" part: %s', db_name, table_name,
                              part_name)

                part_paths = backup_meta.get_part_paths(
                    db_name, table_name, part_name)

                self._backup_layout.download_part_data(db_name, table_name,
                                                       part_name, part_paths)
                attach_parts.append(part_name)

            logging.debug('Waiting for downloads')
            self._backup_layout.wait()
            self._ch_ctl.chown_dettached_table_parts(db_name, table_name)
            for part_name in attach_parts:
                logging.debug('Attaching "%s.%s" part: %s', db_name,
                              table_name, part_name)

                self._ch_ctl.attach_part(db_name, table_name, part_name)

    def _deduplicate_part(self, part_info):
        """
        Deduplicate part if it's possible
        """

        logging.debug('Looking for deduplication of part "%s"', part_info.name)

        for backup_meta in self._existing_backups:
            # load every existing backup entry
            backup_part_contents = backup_meta.get_part_contents(
                part_info.database, part_info.table, part_info.name)

            if not backup_part_contents:
                logging.debug('Part "%s" was not found in backup "%s", skip',
                              part_info.name, backup_meta.name)
                continue

            backup_part_info = ClickhousePartInfo(**backup_part_contents)

            if backup_part_info.link:
                logging.debug('Part "%s" in backup "%s" is link, skip',
                              part_info.name, backup_meta.name)
                continue

            if backup_part_info != part_info:
                logging.debug('Part "%s" in backup "%s" is differ form local',
                              part_info.name, backup_meta.name)
                continue

            #  check if part files exist in storage
            if self._check_part_availability(backup_part_info):
                logging.info('Deduplicating part "%s" based on %s',
                             part_info.name, backup_meta.name)
                return backup_meta.path, backup_part_info.paths

        return False, None

    def _check_part_availability(self, part_info):
        """
        Check if part files exist in storage
        """

        failed_part_files = [
            path for path in part_info.paths
            if not self._backup_layout.path_exists(path)
        ]

        if failed_part_files:
            logging.error('Some part files were not found in storage: %s',
                          ', '.join(failed_part_files))
            return False

        return True

    def _load_backup_meta(self, backup_meta_path):
        """
        Download from storage and load backup meta file
        """

        backup_meta_contents = self._backup_layout.download_backup_meta(
            backup_meta_path)
        backup_meta = ClickhouseBackupStructure()
        try:
            backup_meta.load_json(backup_meta_contents)
        except InvalidBackupStruct:
            logging.critical('Can not load backup meta file: %s',
                             backup_meta_path)
            raise
        return backup_meta

    def _load_existing_backups(self, backup_age_limit=None):
        """
        Load all current backup entries
        """

        if backup_age_limit is None:
            backup_age_limit = datetime.fromtimestamp(0)

        logging.debug('Collecting existing backups for deduplication')
        backup_paths = self._backup_layout.get_existing_backups_names()

        existing_backups = []
        for backup_name in backup_paths:
            backup_meta_path = self._backup_layout.get_backup_meta_path(
                backup_name)
            if not self._backup_layout.path_exists(backup_meta_path):
                logging.warning('Backup path without meta file was found: %s',
                                backup_meta_path)
                continue

            backup_meta = self._load_backup_meta(backup_meta_path)

            # filter old entries (see deduplication_age_limit)
            if backup_meta.end_time > backup_age_limit:
                existing_backups.append(backup_meta)
            else:
                logging.debug(
                    'Backup "%s" is too old for deduplication (%s > %s), skip',
                    backup_meta_path, backup_meta.end_time, backup_age_limit)

        # Sort by time (new is first)
        # we want to duplicate part based on freshest backup
        existing_backups.sort(key=lambda b: b.end_time, reverse=True)
        self._existing_backups = existing_backups
