"""Validated PDF uploads to OpenAI; task payloads hold document metadata."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import io
import json
import os
import re
import uuid

from werkzeug.utils import secure_filename


_FILE_ID_PATTERN = re.compile(r"file-[A-Za-z0-9_-]+", re.ASCII)
_CHUNK_BYTES = 64 * 1024


def _max_bytes():
    try:
        limit = int(os.environ.get("MAX_DOCUMENT_BYTES", "20971520"))
    except ValueError as exc:
        raise RuntimeError("MAX_DOCUMENT_BYTES must be a positive integer.") from exc
    if limit <= 0:
        raise RuntimeError("MAX_DOCUMENT_BYTES must be a positive integer.")
    return limit


def _validate_owner(username):
    if not isinstance(username, str) or not username:
        raise ValueError("A document owner is required.")


def validated_file_id(payload, username):
    """Authorize a task's file reference without making any network requests."""
    _validate_owner(username)
    if (not isinstance(payload, dict)
            or any(key in payload for key in ("object_key", "filepath", "file_path"))):
        raise ValueError("This legacy document is unavailable. Please reupload the PDF.")
    if payload.get("owner") != username:
        raise ValueError("The document does not belong to this user.")
    file_id = payload.get("openai_file_id")
    if not isinstance(file_id, str) or _FILE_ID_PATTERN.fullmatch(file_id) is None:
        raise ValueError("Invalid OpenAI document file ID. Please reupload the PDF.")
    return file_id


def _ttl_seconds():
    try:
        ttl = int(os.environ.get("DOCUMENT_TTL_SECONDS", "86400"))
    except ValueError as exc:
        raise RuntimeError("DOCUMENT_TTL_SECONDS must be an integer between 3600 and 2592000.") from exc
    if not 3600 <= ttl <= 2592000:
        raise RuntimeError("DOCUMENT_TTL_SECONDS must be an integer between 3600 and 2592000.")
    return ttl


@contextmanager
def _openai_client():
    api_key = (os.environ.get("OPENAI_API_KEY", "").strip()
               or os.environ.get("MY_API_KEY", "").strip())
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY or MY_API_KEY is required.")
    from openai import OpenAI

    client = OpenAI(api_key=api_key, timeout=30.0, max_retries=2)
    try:
        yield client
    finally:
        client.close()


def _copy_bounded(stream, destination, limit):
    total = 0
    while True:
        chunk = stream.read(min(_CHUNK_BYTES, limit - total + 1))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise ValueError("Document streams must contain bytes.")
        total += len(chunk)
        if total > limit:
            raise ValueError("Document exceeds MAX_DOCUMENT_BYTES.")
        destination.write(chunk)
    if not total:
        raise ValueError("The document is empty.")
    return total


def _validate_pdf(stream):
    stream.seek(0)
    if stream.read(5) != b"%PDF-":
        raise ValueError("The document must be a valid PDF.")
    stream.seek(0)
    from pypdf import PdfReader

    try:
        reader = PdfReader(stream, strict=True)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported.")
        if not len(reader.pages):
            raise ValueError("The PDF must contain at least one page.")
        # Resolve every page, not only the page count.
        for page in reader.pages:
            page.get_object()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The document must be a valid PDF.") from exc
    finally:
        stream.seek(0)


def upload_document(file, username):
    """Validate a FileStorage and return metadata for its task payload."""
    _validate_owner(username)
    ttl = _ttl_seconds()
    original_filename = file.filename or ""
    if not original_filename.lower().endswith(".pdf"):
        raise ValueError("The document filename must end in .pdf.")
    filename = secure_filename(original_filename) or "document.pdf"
    content = io.BytesIO()
    size = _copy_bounded(file.stream, content, _max_bytes())
    _validate_pdf(content)
    with _openai_client() as client:
        uploaded = client.files.create(
            purpose="user_data",
            file=(f"{uuid.uuid4().hex}.pdf", content.getvalue(), "application/pdf"),
            expires_after={"anchor": "created_at", "seconds": ttl},
        )
    file_id = validated_file_id({"owner": username, "openai_file_id": uploaded.id}, username)
    uploaded_at = datetime.fromtimestamp(uploaded.created_at, timezone.utc)
    expires_at = (datetime.fromtimestamp(uploaded.expires_at, timezone.utc)
                  if uploaded.expires_at is not None else uploaded_at + timedelta(seconds=ttl))
    return {
        "openai_file_id": file_id, "owner": username, "original_filename": filename,
        "size_bytes": size, "uploaded_at": uploaded_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }


def delete_document(payload, username):
    """Delete an authorized remote file; the caller controls cleanup timing."""
    file_id = validated_file_id(payload, username)
    with _openai_client() as client:
        return client.files.delete(file_id)


def list_documents(cursor, username):
    """List this owner's stored documents using existing task metadata."""
    _validate_owner(username)
    cursor.execute(
        """SELECT id, quiz_name, task_type, payload, status, created_at
           FROM quiz_generation_tasks
           WHERE username = %s AND task_type IN (%s, %s)
           ORDER BY created_at DESC, id DESC""",
        (username, "document", "remediation_pdf"),
    )
    documents = []
    for row in cursor.fetchall():
        if not isinstance(row, dict):
            try:
                row = dict(zip(
                    ("id", "quiz_name", "task_type", "payload", "status", "created_at"),
                    row,
                ))
            except TypeError:
                continue
        try:
            if row.get("task_type") not in ("document", "remediation_pdf"):
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                payload = json.loads(payload)
            validated_file_id(payload, username)
            filename = payload.get("original_filename")
            if not isinstance(filename, str) or not filename:
                continue
            documents.append({
                "id": row["id"], "quiz_name": row["quiz_name"],
                "status": row["status"],
                "original_filename": secure_filename(filename) or "document.pdf",
            })
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            continue
    return documents
