"""
S3 storage engine.
"""

import os
import time
from tempfile import TemporaryFile
from typing import Sequence

import boto3
import botocore.vendored.requests.packages.urllib3 as boto_urllib3
from botocore.client import Config
from botocore.errorfactory import ClientError

from .base import PipeLineCompatibleStorageEngine

DEFAULT_DOWNLOAD_PART_LEN = 8 * 1024 * 1024


class S3StorageEngine(PipeLineCompatibleStorageEngine):
    """
    Engine for S3-compatible storage services.
    """

    def __init__(self, config: dict) -> None:
        credentials_config = config['credentials']
        boto_config = config['boto_config']
        self._s3_session = boto3.session.Session(
            aws_access_key_id=credentials_config['access_key_id'],
            aws_secret_access_key=credentials_config['secret_access_key'],
        )

        self._s3_client = self._s3_session.client(
            service_name='s3',
            endpoint_url=credentials_config['endpoint_url'],
            config=Config(s3={
                'addressing_style': boto_config['addressing_style'],
                'region_name': boto_config['region_name'],
            }),
        )

        self._s3_bucket_name = credentials_config['bucket']

        self._multipart_uploads: dict = {}
        self._multipart_downloads: dict = {}

        if config.get('disable_ssl_warnings'):
            self.disable_boto_requests_warnings()

    def upload_file(self, local_path: str, remote_path: str) -> str:
        remote_path = remote_path.lstrip('/')
        with open(local_path, 'rb') as data:
            self._s3_client.upload_fileobj(data, self._s3_bucket_name, remote_path)
        return remote_path

    def upload_data(self, data: bytes, remote_path: str) -> str:
        remote_path = remote_path.lstrip('/')
        self._s3_client.put_object(Body=data, Bucket=self._s3_bucket_name, Key=remote_path)
        return remote_path

    def download_file(self, remote_path: str, local_path: str) -> None:
        remote_path = remote_path.lstrip('/')
        self._s3_client.download_file(self._s3_bucket_name, remote_path, local_path)

    def download_data(self, remote_path):
        remote_path = remote_path.lstrip('/')
        with TemporaryFile() as fileobj:
            self._s3_client.download_fileobj(self._s3_bucket_name, remote_path, fileobj)
            fileobj.seek(0)
            data = fileobj.read()
        return data

    def delete_file(self, remote_path: str) -> None:
        remote_path = remote_path.lstrip('/')
        self._s3_client.delete_object(Bucket=self._s3_bucket_name, Key=remote_path)

    def delete_files(self, remote_paths: Sequence[str]) -> dict:
        """
        Delete multiple files from S3
        """
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#S3.Client.delete_objects
        delete_objects = list(map(lambda x: {'Key': x.lstrip('/')}, remote_paths))

        return self._s3_client.delete_objects(Bucket=self._s3_bucket_name, Delete={'Objects': delete_objects})

    def list_dir(self, remote_path: str, recursive: bool = False, absolute: bool = False) -> Sequence[str]:
        remote_path = remote_path.lstrip('/')
        contents = []
        paginator = self._s3_client.get_paginator('list_objects')
        list_object_kwargs = dict(Bucket=self._s3_bucket_name, Prefix=f'{remote_path}/')
        if not recursive:
            list_object_kwargs['Delimiter'] = '/'

        for result in paginator.paginate(**list_object_kwargs):
            if result.get('CommonPrefixes') is not None:
                for dir_prefix in result.get('CommonPrefixes'):
                    if absolute:
                        dir_path = dir_prefix.get('Prefix')
                    else:
                        dir_path = os.path.relpath(dir_prefix.get('Prefix'), remote_path)

                    contents.append(dir_path)

            if result.get('Contents') is not None:
                for file_key in result.get('Contents'):
                    if absolute:
                        file_path = file_key.get('Key')
                    else:
                        file_path = os.path.relpath(file_key.get('Key'), remote_path)

                    contents.append(file_path)

        return contents

    def path_exists(self, remote_path: str) -> bool:
        """
        Check if remote path exists.
        """
        try:
            self._s3_client.head_object(Bucket=self._s3_bucket_name, Key=remote_path)
            return True
        except ClientError:
            return False

    def create_multipart_upload(self, remote_path: str) -> str:
        s3_resp = self._s3_client.create_multipart_upload(Bucket=self._s3_bucket_name, Key=remote_path)

        upload_id = s3_resp['UploadId']

        # TODO: limit multipart uploads + clean up expired
        self._multipart_uploads[upload_id] = {
            'ctime': int(time.time()),
            'Parts': [],
        }
        return upload_id

    def upload_part(self, data, remote_path, upload_id):
        upload_parts = self._multipart_uploads[upload_id]['Parts']
        try:
            part_num = upload_parts[-1]['PartNumber'] + 1
        except IndexError:
            part_num = 1

        s3_resp = self._s3_client.upload_part(Body=data,
                                              Bucket=self._s3_bucket_name,
                                              Key=remote_path,
                                              UploadId=upload_id,
                                              PartNumber=part_num)

        # save part metadata for complete upload
        upload_parts.append({'ETag': s3_resp['ETag'], 'PartNumber': part_num})

    def complete_multipart_upload(self, remote_path: str, upload_id: str) -> None:
        parts = self._multipart_uploads[upload_id]['Parts']
        self._s3_client.complete_multipart_upload(Bucket=self._s3_bucket_name,
                                                  Key=remote_path,
                                                  UploadId=upload_id,
                                                  MultipartUpload={
                                                      'Parts': parts,
                                                  })

        del self._multipart_uploads[upload_id]

    def create_multipart_download(self, remote_path: str) -> str:
        remote_path = remote_path.lstrip('/')

        resp = self._s3_client.get_object(Bucket=self._s3_bucket_name, Key=remote_path)

        download_id = f'{remote_path}_{time.time()}'
        self._multipart_downloads[download_id] = resp
        return download_id

    def download_part(self, download_id: str, part_len: int = None) -> bytes:
        if part_len:
            part_len = DEFAULT_DOWNLOAD_PART_LEN
        return self._multipart_downloads[download_id]['Body'].read(part_len)

    def complete_multipart_download(self, download_id):
        self._multipart_downloads[download_id]['Body'].close()
        del self._multipart_downloads[download_id]

    @staticmethod
    def disable_boto_requests_warnings() -> None:
        """
        Disable urllib warnings (annoys with self-signed ca)
        """
        boto_urllib3.disable_warnings(boto_urllib3.exceptions.InsecureRequestWarning)
