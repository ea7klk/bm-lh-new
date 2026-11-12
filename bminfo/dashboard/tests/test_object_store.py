"""The Silo/S3 client, exercised against a stubbed boto3 client.

These cover the mapping between Silo's responses and the interface the archiver
uses: metadata parsing, 404 handling and the checksum header on upload. No
network is involved.
"""

import os
import tempfile

from botocore.exceptions import ClientError
from django.test import SimpleTestCase, override_settings

from dashboard.object_store import ObjectStoreError, S3ObjectStore, StoredObject


def _client_error(code, operation="HeadObject"):
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, operation)


class FakeBoto:
    def __init__(self):
        self.uploaded = None
        self.deleted = []
        self.heads = {}

    def head_bucket(self, Bucket):
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def upload_fileobj(self, handle, bucket, key, ExtraArgs=None):
        self.uploaded = {
            "bucket": bucket,
            "key": key,
            "data": handle.read(),
            "extra": ExtraArgs or {},
        }

    def head_object(self, Bucket, Key):
        if Key not in self.heads:
            raise _client_error("404")
        return self.heads[Key]

    def download_fileobj(self, bucket, key, handle):
        handle.write(b"payload")

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)

    def get_paginator(self, name):
        class Paginator:
            def paginate(_self, Bucket, Prefix):
                yield {"Contents": [{"Key": f"{Prefix}/a.jsonl.gz", "Size": 10},
                                    {"Key": f"{Prefix}/b.jsonl.gz", "Size": 20}]}
        return Paginator()


@override_settings(S3_ENDPOINT_URL="https://silo.example", S3_BUCKET="bminfo-raw",
                   S3_ACCESS_KEY_ID="key", S3_SECRET_ACCESS_KEY="secret")
class S3ClientTests(SimpleTestCase):
    def setUp(self):
        self.store = S3ObjectStore()
        self.fake = FakeBoto()
        self.store._client = self.fake

    def test_put_file_sends_the_row_count_and_digest_as_metadata(self):
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"compressed-bytes")
            path = tmp.name
        try:
            self.store.put_file("raw-events/x.gz", path, rows=42, sha256="abc")
        finally:
            os.unlink(path)

        self.assertEqual(self.fake.uploaded["bucket"], "bminfo-raw")
        self.assertEqual(self.fake.uploaded["data"], b"compressed-bytes")
        # Metadata is what the archiver reads back to decide whether an object
        # already covers a day.
        self.assertEqual(self.fake.uploaded["extra"]["Metadata"], {"rows": "42", "sha256": "abc"})
        self.assertEqual(self.fake.uploaded["extra"]["ContentType"], "application/gzip")

    def test_head_returns_none_for_a_missing_object(self):
        self.assertIsNone(self.store.head("absent"))

    def test_head_parses_the_recorded_metadata(self):
        self.fake.heads["raw-events/x.gz"] = {
            "ContentLength": 123,
            "Metadata": {"rows": "7", "sha256": "deadbeef"},
        }

        head = self.store.head("raw-events/x.gz")

        self.assertEqual(head, StoredObject(key="raw-events/x.gz", size=123, rows=7, sha256="deadbeef"))

    def test_head_tolerates_missing_metadata(self):
        self.fake.heads["k"] = {"ContentLength": 5}
        head = self.store.head("k")
        self.assertIsNone(head.rows)
        self.assertIsNone(head.sha256)

    def test_other_client_errors_are_raised_as_store_errors(self):
        self.fake.heads = {}
        with self.assertRaises(ObjectStoreError):
            # Simulate a permission problem rather than a missing object.
            self.fake.head_object = lambda **_: (_ for _ in ()).throw(_client_error("403"))
            self.store.head("k")

    def test_list_prefix_paginates(self):
        keys = [obj.key for obj in self.store.list_prefix("raw-events/2026")]
        self.assertEqual(keys, ["raw-events/2026/a.jsonl.gz", "raw-events/2026/b.jsonl.gz"])

    def test_delete_forwards_the_key(self):
        self.store.delete("raw-events/x.gz")
        self.assertEqual(self.fake.deleted, ["raw-events/x.gz"])

    def test_ping_maps_bucket_errors(self):
        def boom(Bucket):
            raise _client_error("NoSuchBucket", "HeadBucket")
        self.fake.head_bucket = boom
        with self.assertRaises(ObjectStoreError):
            self.store.ping()
