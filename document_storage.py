"""Private S3 document storage; task payloads hold the document metadata.

The operator must configure S3_BUCKET as a private bucket. No object ACLs are
sent, so bucket-owner-enforced buckets are supported.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
import uuid

from werkzeug.utils import secure_filename


_KEY_PATTERN = re.compile(r"uploads/([0-9a-f]{64})/([0-9a-f]{32})/source\.pdf")
_ARTIFACT_NAMES = frozenset(("extracted_topics.json", "output.json"))
_CHUNK_BYTES = 64 * 1024


def _max_bytes():
    try:
        limit = int(os.environ.get("MAX_DOCUMENT_BYTES", "20971520"))
    except ValueError as exc:
        raise RuntimeError("MAX_DOCUMENT_BYTES must be a positive integer.") from exc
    if limit <= 0:
        raise RuntimeError("MAX_DOCUMENT_BYTES must be a positive integer.")
    return limit


def _owner_hash(username):
    if not isinstance(username, str) or not username:
        raise ValueError("A document owner is required.")
    return hashlib.sha256(username.encode("utf-8")).hexdigest()


def _document_key(payload, username):
    owner_hash = _owner_hash(username)
    if not isinstance(payload, dict) or not payload.get("object_key"):
        raise ValueError("This legacy document is unavailable. Please reupload the PDF.")
    if payload.get("owner") != username:
        raise ValueError("The document does not belong to this user.")
    key = payload["object_key"]
    match = _KEY_PATTERN.fullmatch(key) if isinstance(key, str) else None
    if match is None or match.group(1) != owner_hash:
        raise ValueError("Invalid document object key.")
    return key


def _s3():
    bucket = os.environ.get("S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("S3_BUCKET must name a private document bucket.")
    endpoint = os.environ.get("S3_ENDPOINT_URL") or None
    if endpoint:
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise RuntimeError("S3_ENDPOINT_URL must be an HTTPS endpoint without credentials.")
    # Import and construct only at use time; boto3's normal credential chain
    # handles AWS environment credentials without putting them in task payloads.
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=os.environ.get("AWS_DEFAULT_REGION") or "us-east-1",
        config=Config(
            signature_version="s3v4", connect_timeout=5, read_timeout=30,
            retries={"mode": "standard", "total_max_attempts": 3},
        ),
    )
    return client, bucket


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
    owner_hash = _owner_hash(username)
    original_filename = file.filename or ""
    if not original_filename.lower().endswith(".pdf"):
        raise ValueError("The document filename must end in .pdf.")
    filename = secure_filename(original_filename) or "document.pdf"
    content = io.BytesIO()
    size = _copy_bounded(file.stream, content, _max_bytes())
    _validate_pdf(content)
    key = f"uploads/{owner_hash}/{uuid.uuid4().hex}/source.pdf"
    client, bucket = _s3()
    client.put_object(
        Bucket=bucket, Key=key, Body=content.getvalue(),
        ContentType="application/pdf",
    )
    return {
        "object_key": key, "owner": username, "original_filename": filename,
        "size_bytes": size, "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }


def _check_size(value, limit):
    if type(value) is not int or value <= 0:
        raise ValueError("Invalid document size metadata.")
    if value > limit:
        raise ValueError("Document exceeds MAX_DOCUMENT_BYTES.")


@contextmanager
def download_document(payload, username):
    """Yield a validated, temporary absolute PDF path; never delete S3 data."""
    key = _document_key(payload, username)
    limit = _max_bytes()
    expected_size = payload.get("size_bytes")
    if expected_size is not None:
        _check_size(expected_size, limit)
    client, bucket = _s3()
    with TemporaryDirectory(prefix=".document-") as directory:
        path = Path(directory).resolve() / "source.pdf"
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            content_length = response.get("ContentLength")
            if content_length is not None:
                _check_size(content_length, limit)
            with path.open("w+b") as destination:
                size = _copy_bounded(body, destination, limit)
                if ((expected_size is not None and size != expected_size)
                        or (content_length is not None and size != content_length)):
                    raise ValueError("Downloaded document size does not match metadata.")
                _validate_pdf(destination)
        finally:
            body.close()
        yield str(path)


def upload_json_artifact(payload, username, local_path, artifact_name):
    """Upload an allowed generated JSON artifact next to its source PDF."""
    key = _document_key(payload, username)
    if artifact_name not in _ARTIFACT_NAMES:
        raise ValueError("Unsupported document artifact name.")
    content = io.BytesIO()
    with open(local_path, "rb") as source:
        _copy_bounded(source, content, _max_bytes())
    artifact_key = key.rsplit("/", 1)[0] + "/" + artifact_name
    client, bucket = _s3()
    client.put_object(
        Bucket=bucket, Key=artifact_key, Body=content.getvalue(),
        ContentType="application/json",
    )
    return artifact_key


def list_documents(cursor, username):
    """List this owner's stored documents using existing task metadata."""
    _owner_hash(username)
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
            _document_key(payload, username)
            filename = payload.get("original_filename")
            if not isinstance(filename, str) or not filename:
                continue
            documents.append({
                "id": row["id"], "quiz_name": row["quiz_name"],
                "status": row["status"], "original_filename": filename,
            })
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            continue
    return documents
