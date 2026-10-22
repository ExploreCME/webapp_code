#!/usr/bin/env bash
# Generate a standardized quiz PDF, upload it to S3, and print a presigned URL.
# Prerequisites:
#   - python is available
#   - aws CLI is installed and configured
#   - AWS credentials have s3:PutObject and s3:GetObject permissions
# Usage: bash scripts/export_and_share_pdf.sh "Family Medicine" 1 your-bucket-name [s3-prefix]
set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "Usage: $0 <quiz-name> <version> <s3-bucket> [s3-prefix]" >&2
  exit 1
fi

QUIZ_NAME="$1"
VERSION="$2"
BUCKET="$3"
PREFIX="${4:-exports}"

EXPORT_DIR="/opt/render/project/src/exports"
mkdir -p "$EXPORT_DIR"

SLUG="$(printf '%s' "$QUIZ_NAME" | sed -E 's/[^A-Za-z0-9]+/_/g; s/^_+//; s/_+$//')"
FILENAME="${SLUG}_version_${VERSION}.pdf"
LOCAL_PATH="$EXPORT_DIR/$FILENAME"
PREFIX="${PREFIX#/}"
PREFIX="${PREFIX%/}"
if [ -n "$PREFIX" ]; then
  S3_KEY="$PREFIX/$FILENAME"
else
  S3_KEY="$FILENAME"
fi

echo "Generating PDF: $LOCAL_PATH"
python export_standardized_quiz_pdf.py --quiz-name "$QUIZ_NAME" --version "$VERSION" --output "$LOCAL_PATH"

echo "Uploading to s3://$BUCKET/$S3_KEY"
aws s3 cp "$LOCAL_PATH" "s3://$BUCKET/$S3_KEY"

echo "Presigned URL (valid 1 hour):"
aws s3 presign "s3://$BUCKET/$S3_KEY" --expires-in 3600
