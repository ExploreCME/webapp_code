import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import get_type_hints
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
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.openai = MagicMock()
        self.modules = patch.dict(sys.modules, {"openai": self.openai})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.client = self.openai.OpenAI.return_value
        self.client.files.create.return_value = SimpleNamespace(
            id="file-test_123-ABC", created_at=1700000000, expires_at=1700086400,
        )
        self.pdf = pdf_bytes()
        self.payload = {
            "openai_file_id": "file-test_123-ABC", "owner": "alice",
            "original_filename": "source.pdf", "size_bytes": len(self.pdf),
        }

    def upload(self, content=None, filename="../../unsafe name.pdf"):
        return storage.upload_document(
            FileStorage(stream=io.BytesIO(self.pdf if content is None else content),
                        filename=filename),
            "alice",
        )

    def test_upload_metadata_unique_names_and_expiration(self):
        first, second = self.upload(), self.upload()
        calls = self.client.files.create.call_args_list
        self.assertNotEqual(calls[0].kwargs["file"][0], calls[1].kwargs["file"][0])
        self.assertEqual(set(first), {
            "openai_file_id", "owner", "original_filename", "size_bytes",
            "uploaded_at", "expires_at",
        })
        self.assertEqual(first["openai_file_id"], self.payload["openai_file_id"])
        self.assertEqual(first["owner"], "alice")
        self.assertEqual(first["original_filename"], "unsafe_name.pdf")
        self.assertEqual(first["size_bytes"], len(self.pdf))
        self.assertEqual(first["uploaded_at"], "2023-11-14T22:13:20+00:00")
        self.assertEqual(first["expires_at"], "2023-11-15T22:13:20+00:00")
        for call in calls:
            filename, content, mime = call.kwargs["file"]
            self.assertRegex(filename, r"^[0-9a-f]{32}\.pdf$")
            self.assertEqual(content, self.pdf)
            self.assertEqual(mime, "application/pdf")
            self.assertEqual(call.kwargs["purpose"], "user_data")
            self.assertEqual(call.kwargs["expires_after"],
                             {"anchor": "created_at", "seconds": 86400})
        self.assertEqual(second["original_filename"], "unsafe_name.pdf")
        self.assertEqual(self.client.close.call_count, 2)
        self.assertEqual(self.upload(filename="SOURCE.PDF")["original_filename"], "SOURCE.PDF")

    def test_non_pdf_filenames_rejected_before_parsing(self):
        for filename in ("", "source", "source.txt", "source.pdf.exe"):
            with self.subTest(filename=filename), patch.object(storage, "_validate_pdf") as validate:
                with self.assertRaisesRegex(ValueError, "filename"):
                    self.upload(filename=filename)
                validate.assert_not_called()
        self.openai.OpenAI.assert_not_called()

    def test_actual_invalid_pdfs_never_use_network(self):
        for content in (b"", b"not pdf", b"%PDF-1.7\nbroken",
                        pdf_bytes(0), pdf_bytes(encrypted=True)):
            with self.subTest(content=content[:20]), self.assertRaises(ValueError):
                self.upload(content)
        self.openai.OpenAI.assert_not_called()

    def test_pdf_reader_strict_and_resolves_every_page(self):
        reader = MagicMock()
        reader.is_encrypted = False
        pages = [MagicMock(), MagicMock()]
        reader.pages = pages
        with patch("pypdf.PdfReader", return_value=reader) as constructor:
            self.upload()
        self.assertTrue(constructor.call_args.kwargs["strict"])
        for page in pages:
            page.get_object.assert_called_once_with()
        self.assertEqual(constructor.call_args.args[0].tell(), 0)

    def test_pdf_page_resolution_failure_is_invalid(self):
        reader = MagicMock()
        reader.is_encrypted = False
        page = MagicMock()
        page.get_object.side_effect = RuntimeError("broken page")
        reader.pages = [page]
        with patch("pypdf.PdfReader", return_value=reader):
            with self.assertRaisesRegex(ValueError, "valid PDF"):
                self.upload()
        self.openai.OpenAI.assert_not_called()

    def test_upload_size_bound_and_exact_boundary(self):
        with patch.dict(os.environ, {"MAX_DOCUMENT_BYTES": str(len(self.pdf))}):
            self.upload()
            with self.assertRaisesRegex(ValueError, "MAX_DOCUMENT_BYTES"):
                self.upload(self.pdf + b"x")
        self.openai.OpenAI.reset_mock()
        with patch.dict(os.environ, {"MAX_DOCUMENT_BYTES": "5"}):
            stream = MagicMock()
            stream.read.return_value = b"123456"
            with self.assertRaises(ValueError):
                storage.upload_document(FileStorage(stream=stream, filename="x.pdf"), "alice")
            stream.read.assert_called_once_with(6)
        self.openai.OpenAI.assert_not_called()

    def test_bounded_chunk_reads_and_non_byte_stream(self):
        content = io.BytesIO()
        stream = MagicMock()
        stream.read.side_effect = [b"x" * storage._CHUNK_BYTES, b"x", b""]
        self.assertEqual(storage._copy_bounded(stream, content, storage._CHUNK_BYTES + 1),
                         storage._CHUNK_BYTES + 1)
        self.assertEqual([call.args[0] for call in stream.read.call_args_list],
                         [storage._CHUNK_BYTES, 2, 1])
        stream.read.side_effect = None
        stream.read.return_value = "not bytes"
        with self.assertRaisesRegex(ValueError, "bytes"):
            storage._copy_bounded(stream, io.BytesIO(), 100)

    def test_invalid_limit_and_owner(self):
        for value in ("0", "-1", "invalid"):
            with patch.dict(os.environ, {"MAX_DOCUMENT_BYTES": value}):
                with self.assertRaisesRegex(RuntimeError, "MAX_DOCUMENT_BYTES"):
                    self.upload()
        for username in (None, "", 123):
            with self.assertRaises(ValueError):
                storage.upload_document(FileStorage(stream=io.BytesIO(self.pdf)), username)
        self.openai.OpenAI.assert_not_called()

    def test_ttl_config_and_missing_expiration_fallback(self):
        self.client.files.create.return_value.expires_at = None
        for ttl in (3600, 2592000):
            with patch.dict(os.environ, {"DOCUMENT_TTL_SECONDS": str(ttl)}):
                metadata = self.upload()
            self.assertEqual(self.client.files.create.call_args.kwargs["expires_after"]["seconds"], ttl)
            from datetime import datetime
            delta = (datetime.fromisoformat(metadata["expires_at"])
                     - datetime.fromisoformat(metadata["uploaded_at"]))
            self.assertEqual(delta.total_seconds(), ttl)
        self.openai.OpenAI.reset_mock()
        for ttl in ("", "invalid", "3599", "2592001", "86400.5"):
            with patch.dict(os.environ, {"DOCUMENT_TTL_SECONDS": ttl}):
                with self.assertRaisesRegex(RuntimeError, "DOCUMENT_TTL_SECONDS"):
                    self.upload()
        self.openai.OpenAI.assert_not_called()

    def test_api_key_precedence_fallback_and_bounded_client(self):
        with patch.dict(os.environ, {"MY_API_KEY": "fallback-key"}):
            self.upload()
        self.openai.OpenAI.assert_called_with(api_key="test-key", timeout=30.0, max_retries=2)
        with patch.dict(os.environ, {"OPENAI_API_KEY": " ", "MY_API_KEY": " fallback-key "}):
            self.upload()
        self.openai.OpenAI.assert_called_with(api_key="fallback-key", timeout=30.0, max_retries=2)
        self.openai.OpenAI.reset_mock()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "API_KEY"):
                self.upload()
        self.openai.OpenAI.assert_not_called()

    def test_upload_error_closes_client_without_deleting(self):
        self.client.files.create.side_effect = RuntimeError("upload failed")
        with self.assertRaisesRegex(RuntimeError, "upload failed"):
            self.upload()
        self.client.close.assert_called_once_with()
        self.client.files.delete.assert_not_called()

    def test_constructor_failure_does_not_delete_or_close(self):
        self.openai.OpenAI.side_effect = RuntimeError("construction failed")
        for operation in (self.upload, lambda: storage.delete_document(self.payload, "alice")):
            with self.assertRaisesRegex(RuntimeError, "construction failed"):
                operation()
        self.client.files.delete.assert_not_called()
        self.client.close.assert_not_called()

    def test_owner_legacy_and_unsafe_ids_rejected_before_network(self):
        cases = [
            ({}, "belong"),
            (None, "reupload"),
            ({"filepath": "old.pdf"}, "reupload"),
            ({"file_path": "old.pdf"}, "reupload"),
            ({**self.payload, "object_key": "old"}, "reupload"),
            ({**self.payload, "owner": "bob"}, "belong"),
        ]
        cases.extend(({**self.payload, "openai_file_id": value}, "file ID")
                     for value in (None, "", 123, "file-", "files-abc", "file-../abc",
                                   "file-abc\n", "file-é", " file-abc", "file-abc?x=1"))
        for payload, error in cases:
            with self.subTest(payload=payload):
                for operation in (storage.validated_file_id, storage.delete_document):
                    with self.assertRaisesRegex(ValueError, error):
                        operation(payload, "alice")
        for username in (None, "", 123):
            with self.assertRaises(ValueError):
                storage.delete_document(self.payload, username)
        self.openai.OpenAI.assert_not_called()

    def test_validation_does_not_require_network_or_live_file(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(storage.validated_file_id(self.payload, "alice"), "file-test_123-ABC")
        self.openai.OpenAI.assert_not_called()

    def test_delete_remote_file_and_close_on_success_and_failure(self):
        result = storage.delete_document(self.payload, "alice")
        self.assertIs(result, self.client.files.delete.return_value)
        self.client.files.delete.assert_called_once_with("file-test_123-ABC")
        self.client.close.assert_called_once_with()
        self.client.files.delete.side_effect = RuntimeError("delete failed")
        with self.assertRaisesRegex(RuntimeError, "delete failed"):
            storage.delete_document(self.payload, "alice")
        self.assertEqual(self.client.close.call_count, 2)

    def test_listing_defensive_legacy_and_owner_scoped(self):
        cursor = MagicMock()
        row = {"id": 1, "quiz_name": "quiz", "task_type": "document",
               "payload": json.dumps(self.payload), "status": "completed", "created_at": None}
        cursor.fetchall.return_value = [
            row, (2, "remediation", "remediation_pdf", self.payload, "failed", None),
            {**row, "id": 3, "payload": {**self.payload, "original_filename": "../../unsafe name.pdf"}},
            {**row, "payload": "broken"}, {**row, "payload": "[]"},
            {**row, "payload": None}, {**row, "payload": {"filepath": "old.pdf"}},
            {**row, "payload": {"object_key": "old", "owner": "alice"}},
            {**row, "payload": {**self.payload, "owner": "bob"}},
            {**row, "payload": {**self.payload, "original_filename": None}},
            {**row, "payload": {**self.payload, "openai_file_id": "file-../unsafe"}},
            {**row, "task_type": "blueprint"}, None,
        ]
        self.assertEqual(storage.list_documents(cursor, "alice"), [
            {"id": 1, "quiz_name": "quiz", "status": "completed", "original_filename": "source.pdf"},
            {"id": 2, "quiz_name": "remediation", "status": "failed", "original_filename": "source.pdf"},
            {"id": 3, "quiz_name": "quiz", "status": "completed", "original_filename": "unsafe_name.pdf"},
        ])
        sql, params = cursor.execute.call_args.args
        self.assertIn("WHERE username = %s", sql)
        self.assertEqual(params, ("alice", "document", "remediation_pdf"))
        self.openai.OpenAI.assert_not_called()


class InstalledSDKTests(unittest.TestCase):
    def test_files_create_supports_user_data_expiration(self):
        from openai.resources.files import Files
        from openai.types import file_create_params
        self.assertIn("expires_after", inspect.signature(Files.create).parameters)
        self.assertEqual(set(file_create_params.ExpiresAfter.__annotations__), {"anchor", "seconds"})
        self.assertIn("user_data", str(get_type_hints(file_create_params.FileCreateParams)["purpose"]))


if __name__ == "__main__":
    unittest.main()
