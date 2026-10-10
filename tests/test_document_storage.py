import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from pypdf import PdfWriter
from werkzeug.datastructures import FileStorage


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("document_storage", ROOT / "document_storage.py")
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


def pdf_bytes(pages=1, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("test-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class DocumentStorageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = patch("tempfile.tempdir", str(ROOT))
        self.tempdir.start()
        self.addCleanup(self.tempdir.stop)
        self.env = patch.dict(os.environ, {"S3_BUCKET": "private-documents"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.boto3 = MagicMock()
        self.modules = patch.dict(sys.modules, {"boto3": self.boto3})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.client = self.boto3.client.return_value
        self.pdf = pdf_bytes()
        self.key = "uploads/" + hashlib.sha256(b"alice").hexdigest() + "/" + "a" * 32 + "/source.pdf"
        self.payload = {
            "object_key": self.key, "owner": "alice",
            "original_filename": "source.pdf", "size_bytes": len(self.pdf),
        }

    def upload(self, content=None, filename="../../unsafe name.pdf"):
        return storage.upload_document(
            FileStorage(stream=io.BytesIO(self.pdf if content is None else content),
                        filename=filename),
            "alice",
        )

    def response(self, content=None, **kwargs):
        body = io.BytesIO(self.pdf if content is None else content)
        self.client.get_object.return_value = {"Body": body, **kwargs}
        return body

    def test_upload_metadata_private_unique_keys(self):
        first, second = self.upload(), self.upload()
        self.assertNotEqual(first["object_key"], second["object_key"])
        self.assertRegex(first["object_key"], storage._KEY_PATTERN)
        self.assertEqual(first["owner"], "alice")
        self.assertEqual(first["original_filename"], "unsafe_name.pdf")
        self.assertEqual(first["size_bytes"], len(self.pdf))
        self.assertTrue(first["uploaded_at"].endswith("+00:00"))
        for call in self.client.put_object.call_args_list:
            self.assertEqual(call.kwargs["Bucket"], "private-documents")
            self.assertEqual(call.kwargs["ContentType"], "application/pdf")
            self.assertEqual(call.kwargs["Body"], self.pdf)
            self.assertNotIn("ACL", call.kwargs)
        self.assertEqual(self.upload(filename="SOURCE.PDF")["original_filename"], "SOURCE.PDF")

    def test_non_pdf_filenames_rejected_before_parsing(self):
        for filename in ("", "source", "source.txt", "source.pdf.exe"):
            with self.subTest(filename=filename), patch.object(storage, "_validate_pdf") as validate:
                with self.assertRaisesRegex(ValueError, "filename"):
                    self.upload(filename=filename)
                validate.assert_not_called()
        self.boto3.client.assert_not_called()

    def test_invalid_pdfs_never_use_s3(self):
        for content in (b"", b"not pdf", b"%PDF-1.7\nbroken", pdf_bytes(0), pdf_bytes(encrypted=True)):
            with self.subTest(content=content[:20]), self.assertRaises(ValueError):
                self.upload(content)
        self.boto3.client.assert_not_called()

    def test_upload_size_bound_and_exact_boundary(self):
        with patch.dict(os.environ, {"MAX_DOCUMENT_BYTES": str(len(self.pdf))}):
            self.upload()
            with self.assertRaisesRegex(ValueError, "MAX_DOCUMENT_BYTES"):
                self.upload(self.pdf + b"x")
        with patch.dict(os.environ, {"MAX_DOCUMENT_BYTES": "5"}):
            stream = MagicMock()
            stream.read.return_value = b"123456"
            with self.assertRaises(ValueError):
                storage.upload_document(FileStorage(stream=stream, filename="x.pdf"), "alice")
            stream.read.assert_called_once_with(6)

    def test_invalid_limit_and_owner(self):
        for value in ("0", "-1", "invalid"):
            with patch.dict(os.environ, {"MAX_DOCUMENT_BYTES": value}):
                with self.assertRaisesRegex(RuntimeError, "MAX_DOCUMENT_BYTES"):
                    self.upload()
        for username in (None, ""):
            with self.assertRaises(ValueError):
                storage.upload_document(FileStorage(stream=io.BytesIO(self.pdf)), username)
        self.boto3.client.assert_not_called()

    def test_download_success_closes_body_and_cleans_path(self):
        body = self.response(ContentLength=len(self.pdf))
        with patch.object(storage, "TemporaryDirectory", wraps=TemporaryDirectory) as temporary:
            with storage.download_document(self.payload, "alice") as path:
                self.assertTrue(Path(path).is_absolute())
                self.assertEqual(Path(path).read_bytes(), self.pdf)
                self.assertTrue(body.closed)
                directory = Path(path).parent
            temporary.assert_called_once_with(prefix=".document-")
        self.assertFalse(directory.exists())
        self.client.get_object.assert_called_once_with(Bucket="private-documents", Key=self.key)
        self.client.delete_object.assert_not_called()

    def test_cleanup_on_consumer_failure(self):
        self.response()
        with self.assertRaisesRegex(RuntimeError, "consumer"):
            with storage.download_document(self.payload, "alice") as path:
                directory = Path(path).parent
                raise RuntimeError("consumer failed")
        self.assertFalse(directory.exists())
        self.client.delete_object.assert_not_called()

    def test_owner_keys_and_legacy_rejected_before_network(self):
        cases = [
            ({}, "reupload"),
            ({"file_path": "/etc/passwd"}, "reupload"),
            ({**self.payload, "owner": "bob"}, "belong"),
            ({**self.payload, "object_key": self.key.replace("a" * 32, "../escape")}, "key"),
            ({**self.payload, "object_key": self.key.replace("/source.pdf", "/output.json")}, "key"),
            ({**self.payload, "object_key": self.key.replace(hashlib.sha256(b"alice").hexdigest(), "0" * 64)}, "key"),
        ]
        for payload, error in cases:
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, error):
                with storage.download_document(payload, "alice"):
                    self.fail("Must not yield")
        self.boto3.client.assert_not_called()

    def test_download_rejects_invalid_content_and_sizes_and_cleans(self):
        before = set(ROOT.glob(".document-*"))
        cases = [
            (b"invalid", {}, self.payload),
            (self.pdf, {"ContentLength": len(self.pdf) + 1}, self.payload),
            (self.pdf, {}, {**self.payload, "size_bytes": len(self.pdf) + 1}),
            (self.pdf, {"ContentLength": 0}, self.payload),
            (pdf_bytes(encrypted=True), {}, {"object_key": self.key, "owner": "alice"}),
        ]
        for content, response, payload in cases:
            body = self.response(content, **response)
            with self.subTest(response=response), self.assertRaises(ValueError):
                with storage.download_document(payload, "alice"):
                    self.fail("Must not yield")
            self.assertTrue(body.closed)
            self.assertEqual(set(ROOT.glob(".document-*")), before)
        self.client.delete_object.assert_not_called()

    def test_download_bounded_without_metadata(self):
        body = self.response(self.pdf + b"x")
        with patch.dict(os.environ, {"MAX_DOCUMENT_BYTES": str(len(self.pdf))}):
            with self.assertRaisesRegex(ValueError, "MAX_DOCUMENT_BYTES"):
                with storage.download_document({"owner": "alice", "object_key": self.key}, "alice"):
                    self.fail("Must not yield")
        self.assertTrue(body.closed)
        self.response(ContentLength=len(self.pdf) + 1)
        with patch.dict(os.environ, {"MAX_DOCUMENT_BYTES": str(len(self.pdf))}):
            with self.assertRaises(ValueError):
                with storage.download_document(self.payload, "alice"):
                    self.fail("Must not yield")

    def test_download_network_and_stream_failure_cleanup(self):
        before = set(ROOT.glob(".document-*"))
        self.client.get_object.side_effect = RuntimeError("network")
        with self.assertRaises(RuntimeError):
            with storage.download_document(self.payload, "alice"):
                self.fail("Must not yield")
        self.client.get_object.side_effect = None
        body = MagicMock()
        body.read.side_effect = RuntimeError("stream")
        self.client.get_object.return_value = {"Body": body}
        with self.assertRaises(RuntimeError):
            with storage.download_document(self.payload, "alice"):
                self.fail("Must not yield")
        body.close.assert_called_once()
        self.assertEqual(set(ROOT.glob(".document-*")), before)

    def test_config_defaults_and_custom_endpoint(self):
        self.upload()
        kwargs = self.boto3.client.call_args.kwargs
        self.assertEqual(kwargs["region_name"], "us-east-1")
        self.assertIsNone(kwargs["endpoint_url"])
        self.assertEqual(kwargs["config"].signature_version, "s3v4")
        self.assertEqual(kwargs["config"].connect_timeout, 5)
        self.assertEqual(kwargs["config"].read_timeout, 30)
        self.assertEqual(kwargs["config"].retries["total_max_attempts"], 3)
        with patch.dict(os.environ, {
            "S3_ENDPOINT_URL": "https://objects.example.test", "AWS_DEFAULT_REGION": "eu-west-1",
            "AWS_ACCESS_KEY_ID": "example", "AWS_SECRET_ACCESS_KEY": "example",
        }):
            self.upload()
        kwargs = self.boto3.client.call_args.kwargs
        self.assertEqual(kwargs["endpoint_url"], "https://objects.example.test")
        self.assertEqual(kwargs["region_name"], "eu-west-1")
        self.assertNotIn("aws_access_key_id", kwargs)
        self.assertNotIn("aws_secret_access_key", kwargs)

    def test_missing_bucket_and_unsafe_endpoints(self):
        with patch.dict(os.environ, {"S3_BUCKET": ""}):
            with self.assertRaisesRegex(RuntimeError, "S3_BUCKET"):
                self.upload()
        for endpoint in ("http://host", "https://", "******host", "https://host?secret=x"):
            with patch.dict(os.environ, {"S3_ENDPOINT_URL": endpoint}):
                with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                    self.upload()
        self.boto3.client.assert_not_called()

    def test_artifacts_alongside_source_and_local_file_retained(self):
        with TemporaryDirectory(dir=ROOT, prefix=".artifact-test-") as directory:
            path = Path(directory) / "result.json"
            path.write_text('{"topics": []}')
            for name in ("extracted_topics.json", "output.json"):
                key = storage.upload_json_artifact(self.payload, "alice", path, name)
                self.assertEqual(key, self.key.rsplit("/", 1)[0] + "/" + name)
                kwargs = self.client.put_object.call_args.kwargs
                self.assertEqual(kwargs["ContentType"], "application/json")
                self.assertNotIn("ACL", kwargs)
                self.assertEqual(kwargs["Body"], path.read_bytes())
            self.client.put_object.side_effect = RuntimeError("upload failed")
            with self.assertRaises(RuntimeError):
                storage.upload_json_artifact(self.payload, "alice", path, "output.json")
            self.assertTrue(path.exists())

    def test_artifacts_reject_bad_names_and_ownership_before_network(self):
        for name in ("source.pdf", "../output.json", "other.json"):
            with self.assertRaises(ValueError):
                storage.upload_json_artifact(self.payload, "alice", "nonexistent", name)
        with self.assertRaises(ValueError):
            storage.upload_json_artifact(self.payload, "bob", "nonexistent", "output.json")
        self.boto3.client.assert_not_called()

    def test_listing_defensive_and_scoped(self):
        cursor = MagicMock()
        row = {"id": 1, "quiz_name": "quiz", "task_type": "document",
               "payload": json.dumps(self.payload), "status": "completed", "created_at": None}
        cursor.fetchall.return_value = [
            row, (2, "remediation", "remediation_pdf", self.payload, "pending", None),
            {**row, "payload": "broken"}, {**row, "payload": "[]"},
            {**row, "payload": None}, {**row, "payload": json.dumps({"file_path": "old.pdf"})},
            {**row, "payload": {**self.payload, "owner": "bob"}},
            {**row, "payload": {**self.payload, "original_filename": None}},
            {**row, "task_type": "blueprint"}, None,
        ]
        result = storage.list_documents(cursor, "alice")
        self.assertEqual(result, [
            {"id": 1, "quiz_name": "quiz", "status": "completed", "original_filename": "source.pdf"},
            {"id": 2, "quiz_name": "remediation", "status": "pending", "original_filename": "source.pdf"},
        ])
        sql, params = cursor.execute.call_args.args
        self.assertIn("WHERE username = %s", sql)
        self.assertEqual(params, ("alice", "document", "remediation_pdf"))
        self.boto3.client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
