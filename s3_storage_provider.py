# -*- coding: utf-8 -*-
# Copyright 2018 New Vector Ltd
# Copyright 2021 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import hashlib
import logging
import os
import threading

from six import string_types

import aws_encryption_sdk
import boto3
import botocore
from aws_encryption_sdk.identifiers import (
    CommitmentPolicy,
    EncryptionKeyType,
    WrappingAlgorithm,
)
from aws_encryption_sdk.internal.crypto.wrapping_keys import WrappingKey
from aws_encryption_sdk.key_providers.raw import RawMasterKeyProvider

from twisted.internet import defer, reactor, threads
from twisted.python.failure import Failure
from twisted.python.threadpool import ThreadPool

from synapse.logging.context import LoggingContext, make_deferred_yieldable
from synapse.rest.media.v1._base import Responder
from synapse.rest.media.v1.storage_provider import StorageProvider

# Synapse 1.13.0 moved current_context to a module-level function.
try:
    from synapse.logging.context import current_context
except ImportError:
    current_context = LoggingContext.current_context

logger = logging.getLogger("synapse.s3")


# The list of valid AWS storage class names
_VALID_STORAGE_CLASSES = (
    "STANDARD",
    "REDUCED_REDUNDANCY",
    "STANDARD_IA",
    "INTELLIGENT_TIERING",
)

# Chunk size to use when reading from s3 connection in bytes
READ_CHUNK_SIZE = 16 * 1024


class S3StorageProviderBackend(StorageProvider):
    """
    Args:
        hs (HomeServer)
        config: The config returned by `parse_config`
    """

    def __init__(self, hs, config):
        self.cache_directory = hs.config.media.media_store_path
        self.bucket = config["bucket"]
        # A dictionary of extra arguments for uploading files.
        # See https://boto3.amazonaws.com/v1/documentation/api/latest/reference/customizations/s3.html#boto3.s3.transfer.S3Transfer.ALLOWED_UPLOAD_ARGS
        # for a list of possible keys.
        self.extra_args = config["extra_args"]
        self.api_kwargs = {}

        if "region_name" in config:
            self.api_kwargs["region_name"] = config["region_name"]

        if "endpoint_url" in config:
            self.api_kwargs["endpoint_url"] = config["endpoint_url"]

        if "access_key_id" in config:
            self.api_kwargs["aws_access_key_id"] = config["access_key_id"]

        if "secret_access_key" in config:
            self.api_kwargs["aws_secret_access_key"] = config["secret_access_key"]

        if "verify" in config:
            self.api_kwargs["verify"]=config["verify"]

        self._s3_client = None
        self._s3_client_lock = threading.Lock()

        threadpool_size = config.get("threadpool_size", 40)
        self._s3_pool = ThreadPool(name="s3-pool", maxthreads=threadpool_size)
        self._s3_pool.start()

        self._cse_client = None
        if "cse_master_key" in config:
            self._cse_client = ClientSideEncryption(config["cse_master_key"])

        # Manually stop the thread pool on shutdown. If we don't do this then
        # stopping Synapse takes an extra ~30s as Python waits for the threads
        # to exit.
        reactor.addSystemEventTrigger(
            "during", "shutdown", self._s3_pool.stop,
        )

    def _get_s3_client(self):
        # this method is designed to be thread-safe, so that we can share a
        # single boto3 client across multiple threads.
        #
        # (XXX: is creating a client actually a blocking operation, or could we do
        # this on the main thread, to simplify all this?)

        # first of all, do a fast lock-free check
        s3 = self._s3_client
        if s3:
            return s3

        # no joy, grab the lock and repeat the check
        with self._s3_client_lock:
            s3 = self._s3_client
            if not s3:
                b3_session = boto3.session.Session()
                self._s3_client = s3 = b3_session.client("s3", **self.api_kwargs)
            return s3

    def store_file(self, path, file_info):
        """See StorageProvider.store_file"""

        parent_logcontext = current_context()

        def _store_file():
            with LoggingContext(parent_context=parent_logcontext):
                if self._cse_client:
                    self.store_with_client_side_encryption(path)
                else:
                    self._get_s3_client().upload_file(
                        Filename=os.path.join(self.cache_directory, path),
                        Bucket=self.bucket,
                        Key=path,
                        ExtraArgs=self.extra_args,
                    )

        return make_deferred_yieldable(
            threads.deferToThreadPool(reactor, self._s3_pool, _store_file)
        )

    def store_with_client_side_encryption(self, path):
        s3_client = self._get_s3_client()
        file_name = os.path.join(self.cache_directory, path)
        with open(file_name, 'rb') as file:
            with self._cse_client.encrypt(file) as encryptor:
                s3_client.upload_fileobj(
                    Fileobj=encryptor,
                    Bucket=self.bucket,
                    Key=path,
                    ExtraArgs=self.extra_args)


    def fetch(self, path, file_info):
        """See StorageProvider.fetch"""
        logcontext = current_context()

        d = defer.Deferred()

        def _get_file():
            s3_download_task(
                self, self.bucket, path, self.extra_args, d, logcontext
            )

        self._s3_pool.callInThread(_get_file)
        return make_deferred_yieldable(d)

    @staticmethod
    def parse_config(config):
        """Called on startup to parse config supplied. This should parse
        the config and raise if there is a problem.

        The returned value is passed into the constructor.

        In this case we return a dict with fields, `bucket` and `storage_class`
        """
        bucket = config["bucket"]
        storage_class = config.get("storage_class", "STANDARD")

        assert isinstance(bucket, string_types)
        assert storage_class in _VALID_STORAGE_CLASSES

        result = {
            "bucket": bucket,
            "extra_args": {"StorageClass": storage_class},
        }

        if "region_name" in config:
            result["region_name"] = config["region_name"]

        if "endpoint_url" in config:
            result["endpoint_url"] = config["endpoint_url"]

        if "access_key_id" in config:
            result["access_key_id"] = config["access_key_id"]

        if "secret_access_key" in config:
            result["secret_access_key"] = config["secret_access_key"]

        if "verify" in config:
            result["verify"]=config["verify"]

        if "sse_customer_key" in config:
            result["extra_args"]["SSECustomerKey"] = config["sse_customer_key"]
            result["extra_args"]["SSECustomerAlgorithm"] = config.get(
                "sse_customer_algo", "AES256"
            )

        if "cse_master_key" in config:
            result["cse_master_key"] = config["cse_master_key"]

        return result


def s3_download_task(s3, bucket, key, extra_args, deferred, parent_logcontext):
    """Attempts to download a file from S3.

    Args:
        s3_client: boto3 s3 client
        bucket (str): The S3 bucket which may have the file
        key (str): The key of the file
        deferred (Deferred[_S3Responder|None]): If file exists
            resolved with an _S3Responder instance, if it doesn't
            exist then resolves with None.
        parent_logcontext (LoggingContext): the logcontext to report logs and metrics
            against.
    """
    with LoggingContext(parent_context=parent_logcontext):
        logger.info("Fetching %s from S3", key)

        s3_client = s3._get_s3_client()
        try:
            if "SSECustomerKey" in extra_args and "SSECustomerAlgorithm" in extra_args:
                resp = s3_client.get_object(
                    Bucket=bucket,
                    Key=key,
                    SSECustomerKey=extra_args["SSECustomerKey"],
                    SSECustomerAlgorithm=extra_args["SSECustomerAlgorithm"],
                )
            else:
                resp = s3_client.get_object(Bucket=bucket, Key=key)

        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey",):
                logger.info("Media %s not found in S3", key)
                reactor.callFromThread(deferred.callback, None)
                return

            reactor.callFromThread(deferred.errback, Failure())
            return

        producer = _S3Responder()
        reactor.callFromThread(deferred.callback, producer)
        _stream_to_producer(reactor, producer, resp["Body"], s3, timeout=90.0)


def _stream_to_producer(reactor, producer, body, s3, status=None, timeout=None):
    """Streams a file like object to the producer.

    Correctly handles producer being paused/resumed/stopped.

    Args:
        reactor
        producer (_S3Responder): Producer object to stream results to
        body (file like): The object to read from
        s3 (S3StorageProviderBackend): The object to use for client-side encryption
        status (_ProducerStatus|None): Used to track whether we're currently
            paused or not. Used for testing
        timeout (float|None): Timeout in seconds to wait for consume to resume
            after being paused
    """

    if not status:
        status = _ProducerStatus()

    try:
        if s3._cse_client:
            stream_body_with_cse(body, producer, reactor, s3, timeout, status)
        else:
            stream_body(body, producer, reactor, status, timeout)
    except Exception:
        reactor.callFromThread(producer._error, Failure())
    finally:
        reactor.callFromThread(producer._finish)
        if body:
            body.close()

def stream_body(body, producer, reactor, status, timeout):
    """Stream body to client

    Args:
        body (file like): The object to read from
        producer (_S3Responder): Producer object to stream results to
        reactor
        status (_ProducerStatus|None): Used to track whether we're currently
            paused or not. Used for testing
        timeout (float|None): Timeout in seconds to wait for consume to resume
            after being paused
    """
    # Set when we should be producing, cleared when we are paused
    wakeup_event = producer.wakeup_event
    # Set if we should stop producing forever
    stop_event = producer.stop_event

    while not stop_event.is_set():
        # We wait for the producer to signal that the consumer wants
        # more data (or we should abort)
        if not wakeup_event.is_set():
            status.set_paused(True)
            ret = wakeup_event.wait(timeout)
            if not ret:
                raise Exception("Timed out waiting to resume")
            status.set_paused(False)

        # Check if we were woken up so that we abort the download
        if stop_event.is_set():
            return

        chunk = body.read(READ_CHUNK_SIZE)
        if not chunk:
            return

        reactor.callFromThread(producer._write, chunk)

def stream_body_with_cse(body, producer, reactor, s3, timeout, status):
    """Stream body with client-side encryption logic

    Args:
        body (file like): The object to read from
        producer (_S3Responder): Producer object to stream results to
        reactor
        s3 (S3StorageProviderBackend): The object to use for client-side encryption
        status (_ProducerStatus|None): Used to track whether we're currently
            paused or not. Used for testing
        timeout (float|None): Timeout in seconds to wait for consume to resume
            after being paused
    """
    # Set when we should be producing, cleared when we are paused
    wakeup_event = producer.wakeup_event
    # Set if we should stop producing forever
    stop_event = producer.stop_event
    with s3._cse_client.decrypt(body) as decryptor:
        for chunk in decryptor:
            # We wait for the producer to signal that the consumer wants
            # more data (or we should abort)

            if not wakeup_event.is_set():
                status.set_paused(True)
                ret = wakeup_event.wait(timeout)
                if not ret:
                    raise Exception("Timed out waiting to resume")
                status.set_paused(False)

            # Check if we were woken up so that we abort the download
            if stop_event.is_set():
                return

            reactor.callFromThread(producer._write, chunk)


class _S3Responder(Responder):
    """A Responder for S3. Created by _S3DownloadThread
    """

    def __init__(self):
        # Triggered by responder when more data has been requested (or
        # stop_event has been triggered)
        self.wakeup_event = threading.Event()
        # Trigered by responder when we should abort the download.
        self.stop_event = threading.Event()

        # The consumer we're registered to
        self.consumer = None

        # The deferred returned by write_to_consumer, which should resolve when
        # all the data has been written (or there has been a fatal error).
        self.deferred = defer.Deferred()

    def write_to_consumer(self, consumer):
        """See Responder.write_to_consumer
        """
        self.consumer = consumer
        # We are a IPushProducer, so we start producing immediately until we
        # get a pauseProducing or stopProducing
        consumer.registerProducer(self, True)
        self.wakeup_event.set()
        return make_deferred_yieldable(self.deferred)

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop_event.set()
        self.wakeup_event.set()

    def resumeProducing(self):
        """See IPushProducer.resumeProducing
        """
        # The consumer is asking for more data, signal _S3DownloadThread
        self.wakeup_event.set()

    def pauseProducing(self):
        """See IPushProducer.stopProducing
        """
        self.wakeup_event.clear()

    def stopProducing(self):
        """See IPushProducer.stopProducing
        """
        # The consumer wants no more data ever, signal _S3DownloadThread
        self.stop_event.set()
        self.wakeup_event.set()
        if not self.deferred.called:
            with LoggingContext():
                self.deferred.errback(Exception("Consumer ask to stop producing"))

    def _write(self, chunk):
        """Writes the chunk of data to consumer. Called by _S3DownloadThread.
        """
        if self.consumer and not self.stop_event.is_set():
            self.consumer.write(chunk)

    def _error(self, failure):
        """Called when a fatal error occured while getting data. Called by
        _S3DownloadThread.
        """
        if self.consumer:
            self.consumer.unregisterProducer()
            self.consumer = None

        if not self.deferred.called:
            self.deferred.errback(failure)

    def _finish(self):
        """Called when there is no more data to write. Called by _S3DownloadThread.
        """
        if self.consumer:
            self.consumer.unregisterProducer()
            self.consumer = None

        if not self.deferred.called:
            self.deferred.callback(None)


class _ProducerStatus(object):
    """Used to track whether the s3 download thread is currently paused
    waiting for consumer to resume. Used for testing.
    """

    def __init__(self):
        self.is_paused = threading.Event()
        self.is_paused.clear()

    def wait_until_paused(self, timeout=None):
        is_paused = self.is_paused.wait(timeout)
        if not is_paused:
            raise Exception("Timed out waiting")

    def set_paused(self, paused):
        if paused:
            self.is_paused.set()
        else:
            self.is_paused.clear()

class ClientSideEncryption():
    def __init__(self, master_key):
        self._key_provider = FixedKeyProvider()
        self._key_provider.set_master_key(master_key)
        self._encryption_client = aws_encryption_sdk.EncryptionSDKClient(commitment_policy=CommitmentPolicy.FORBID_ENCRYPT_ALLOW_DECRYPT)

    def decrypt(self, source):
        return self._encryption_client.stream(
            mode='d',
            source=source,
            key_provider=self._key_provider
        )

    def encrypt(self, source):
        return self._encryption_client.stream(
              mode='e',
              source=source,
              key_provider=self._key_provider
        )

class FixedKeyProvider(RawMasterKeyProvider):
    provider_id = "fixed"

    def set_master_key(self, master_key):
        self.master_key = hashlib.sha256(master_key.encode("utf-8")).digest()
        self.add_master_key('')

    def _get_raw_key(self, key_id):
        return WrappingKey(
            wrapping_algorithm=WrappingAlgorithm.AES_256_GCM_IV12_TAG16_NO_PADDING,
            wrapping_key=self.master_key,
            wrapping_key_type=EncryptionKeyType.SYMMETRIC,
        )
