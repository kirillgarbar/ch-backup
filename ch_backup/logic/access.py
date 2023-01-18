"""
Clickhouse backup logic for access entities.
"""
import os
from contextlib import contextmanager
from typing import Any, Dict, Generator, Iterator, Sequence, Union

from ch_backup import logging
from ch_backup.backup_context import BackupContext
from ch_backup.logic.backup_manager import BackupManager
from ch_backup.zookeeper.zookeeper import ZookeeperCTL

CH_MARK_FILE = 'need_rebuild_lists.mark'


class AccessBackup(BackupManager):
    """
    Access backup class
    """
    def backup(self, context: BackupContext, **kwargs: Any) -> None:
        """
        Backup access rights
        """
        if kwargs['backup_access_control'] or context.config.get('backup_access_control'):
            self._backup(context)

    def restore(self, context: BackupContext) -> None:
        """
        Restore access rights
        """
        acl_list, acl_meta = context.backup_meta.get_access_control()
        if not acl_list:
            logging.debug('No access control entities to restore.')
            return

        has_replicated_access = self._has_replicated_access(context)
        mark_to_rebuild = not has_replicated_access

        self._restore_local(acl_list, context, mark_to_rebuild=mark_to_rebuild)
        if has_replicated_access:
            self._restore_replicated(acl_list, acl_meta, context)

    def _backup(self, context: BackupContext) -> None:
        """
        Backup method
        """
        objects = context.ch_ctl.get_access_control_objects()
        context.backup_meta.set_access_control(objects)
        acl_list, _ = context.backup_meta.get_access_control()

        if self._has_replicated_access(context):
            self._backup_replicated(acl_list, context)
        self._backup_local(acl_list, context)

    def _backup_local(self, acl_list: Sequence[str], context: BackupContext) -> None:
        """
        Backup access entities from local storage.
        """
        logging.debug(f'Backupping {len(acl_list)} access entities from local storage')
        for name in _get_access_control_files(acl_list):
            context.backup_layout.upload_access_control_file(context.backup_meta.name, name)

    def _backup_replicated(self, acl_list: Sequence[str], context: BackupContext) -> None:
        """
        Backup access entities from replicated storage (ZK/CK).
        """
        logging.debug(f'Backupping {len(acl_list)} access entities from replicated storage')
        with _zk_ctl_client(context) as zk_ctl:
            for uuid in acl_list:
                uuid_zk_path = _get_access_zk_path(context, f'/uuid/{uuid}')
                data, _ = zk_ctl.zk_client.get(uuid_zk_path)
                _file_create(context, f'{uuid}.sql', data.decode())

    def _restore_local(self, acl_list: Sequence[str], context: BackupContext, mark_to_rebuild: bool = True) -> None:
        """
        Restore access entities to local storage.
        """
        logging.debug(f'Restoring {len(acl_list)} access entities to local storage')
        for name in _get_access_control_files(acl_list):
            context.backup_layout.download_access_control_file(context.backup_meta.name, name)
        if mark_to_rebuild:
            self._mark_to_rebuild(context)

    def _restore_replicated(self, acl_list: Sequence[str], acl_meta: Dict[str, Dict[str, Any]],
                            context: BackupContext) -> None:
        """
        Restore access entities to replicated storage (ZK/CK).
        """
        logging.debug(f'Restoring {len(acl_list)} access entities to replicated storage')
        if not acl_meta:
            logging.warning('Can not restore access entities to replicated storage without meta information!')
            return

        with _zk_ctl_client(context) as zk_ctl:
            for i, uuid in enumerate(acl_list):
                meta_data = acl_meta[str(i)]
                name, obj_char = meta_data['name'], meta_data['char']

                # restore object data
                file_path = _get_access_file_path(context, f'{uuid}.sql')
                with open(file_path, 'r', encoding='utf-8') as file:
                    data = file.read()
                    uuid_zk_path = _get_access_zk_path(context, f'/uuid/{uuid}')
                    _zk_upsert_data(zk_ctl, uuid_zk_path, data)

                # restore object link
                uuid_zk_path = _get_access_zk_path(context, f'/{obj_char}/{name}')
                _zk_upsert_data(zk_ctl, uuid_zk_path, uuid)

    def _mark_to_rebuild(self, context: BackupContext) -> None:
        """
        Creates special mark file to rebuild the lists.
        """
        mark_file = _get_access_file_path(context, CH_MARK_FILE)
        with open(mark_file, 'a', encoding='utf-8'):
            pass

    def _has_replicated_access(self, context: BackupContext) -> bool:
        return context.ch_config.config.get('user_directories', {}).get('replicated') is not None


def _get_access_control_files(objects: Sequence[str]) -> Iterator[str]:
    """
    Return list of file to be backuped/restored.
    """
    return map(lambda obj: f'{obj}.sql', objects)


def _get_access_file_path(context: BackupContext, file_name: str) -> str:
    return os.path.join(context.ch_ctl_conf['access_control_path'], file_name)


def _get_access_zk_path(context: BackupContext, zk_path: str) -> str:
    zk_path = zk_path.lstrip('/')
    return f"{context.zk_ctl.zk_root_path}/{context.ch_ctl_conf['zk_access_control_path']}/{zk_path}"


@contextmanager
def _zk_ctl_client(context: BackupContext) -> Generator[ZookeeperCTL, None, None]:
    zk_ctl = context.zk_ctl
    try:
        zk_ctl.zk_client.start()
        zk_ctl.zk_add_auth()
        yield zk_ctl
    finally:
        zk_ctl.zk_client.stop()


def _zk_upsert_data(zk_ctl: ZookeeperCTL, path: str, value: Union[str, bytes]) -> None:
    if isinstance(value, str):
        value = value.encode()

    zk = zk_ctl.zk_client
    logging.debug(f'Upserting zk access entity "{path}"')
    if zk.exists(path):
        zk.set(path, value)
    else:
        zk.create(path, value, makepath=True)


def _file_create(context: BackupContext, file_name: str, file_content: str = '') -> str:
    file_path = _get_access_file_path(context, file_name)
    logging.debug(f'Creating "{file_path}" access entity file')
    with open(file_path, 'w', encoding='utf-8') as file:
        file.write(file_content)

    return file_path