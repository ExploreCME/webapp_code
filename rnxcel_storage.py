"""Owner-isolated RNXcel persistence. Schema installation is migration-only."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from uuid import UUID, uuid4

import rnxcel_engine as engine


logger = logging.getLogger(__name__)


class RNXcelUnavailable(RuntimeError):
    def __init__(self, reason="database_error"):
        self.reason = reason
        super().__init__("RNXcel is temporarily unavailable")


class RNXcelConflict(ValueError):
    """An attempt or selected published version no longer matches the request."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decode(value):
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _timestamp(value):
    return value.isoformat() if isinstance(value, datetime) else value


class RNXcelStore:
    def __init__(self, connection_factory=None, dialect="mysql"):
        if dialect not in ("mysql", "sqlite"):
            raise ValueError("Unsupported RNXcel database dialect")
        self.dialect = dialect
        self.connection_factory = connection_factory
        self._check_adapter()

    def _check_adapter(self):
        if self.dialect == "sqlite":
            from flask import current_app, has_app_context
            if (self.connection_factory is None or not has_app_context()
                    or not current_app.testing):
                raise ValueError("SQLite RNXcel storage is available only in TESTING")

    @contextmanager
    def _connection(self, write=False):
        self._check_adapter()
        connection = None
        try:
            if self.connection_factory is None:
                from db_pool import get_db_connection
                connection = get_db_connection()
            else:
                connection = self.connection_factory()
            if write:
                if self.dialect == "sqlite":
                    connection.execute("BEGIN IMMEDIATE")
                else:
                    connection.begin()
            yield connection
            if write:
                connection.commit()
        except (RNXcelUnavailable, ValueError) as exc:
            if isinstance(exc, RNXcelUnavailable):
                logger.warning("RNXcel unavailable: reason=%s", exc.reason)
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    logger.exception("RNXcel rollback failed")
            raise
        except Exception as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    logger.exception("RNXcel rollback failed")
            error_args = getattr(exc, "args", ())
            missing = (
                (bool(error_args) and error_args[0] == 1146)
                or ("no such table: rnxcel_" in str(exc).lower())
            )
            reason = "missing_migration" if missing else "database_error"
            logger.exception("RNXcel database operation failed: reason=%s", reason)
            raise RNXcelUnavailable(reason) from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    logger.exception("RNXcel connection release failed")

    def _query(self, connection, sql, parameters=(), *, one=False):
        if self.dialect == "sqlite":
            sql = sql.replace("%s", "?")
            parameters = tuple(
                value.isoformat() if isinstance(value, datetime) else value
                for value in parameters
            )
        cursor = connection.cursor()
        try:
            cursor.execute(sql, parameters)
            if cursor.description is None:
                return None
            columns = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
            result = [
                dict(row) if hasattr(row, "keys") else dict(zip(columns, row))
                for row in rows
            ]
            return (result[0] if result else None) if one else result
        finally:
            cursor.close()

    @property
    def _lock(self):
        return " FOR UPDATE" if self.dialect == "mysql" else ""

    def _version(self, row):
        try:
            version = engine.validate_version(_decode(row["authored_json"]))
            if (version["version_id"] != row["version_id"]
                    or version["exam_profile"] != row["exam_profile"]):
                raise ValueError("Published version metadata mismatch")
            return version
        except (ValueError, TypeError, KeyError) as exc:
            raise RNXcelUnavailable("content_unavailable") from exc

    def published(self):
        with self._connection() as connection:
            rows = self._query(
                connection,
                "SELECT version_id, exam_profile, authored_json "
                "FROM rnxcel_item_versions WHERE publication_status = %s "
                "AND exam_profile = %s ORDER BY version_id",
                ("published", engine.EXAM_PROFILE),
            )
            versions = []
            for row in rows:
                try:
                    versions.append(self._version(row))
                except RNXcelUnavailable:
                    logger.error(
                        "Invalid published RNXcel version %s: reason=content_unavailable",
                        row["version_id"],
                    )
            if rows and not versions:
                raise RNXcelUnavailable("content_unavailable")
            return versions

    @staticmethod
    def _owner(owner):
        if (isinstance(owner, bool) or not isinstance(owner, (str, int))
                or not str(owner).strip() or str(owner) != str(owner).strip()
                or len(str(owner)) > 255):
            raise ValueError("Invalid attempt owner")
        return str(owner)

    @staticmethod
    def _id(attempt_id):
        try:
            if not isinstance(attempt_id, str) or str(UUID(attempt_id)) != attempt_id:
                raise ValueError("Invalid attempt ID")
        except (ValueError, AttributeError) as exc:
            raise ValueError("Invalid attempt ID") from exc
        return attempt_id

    def has_consent(self, owner):
        owner = self._owner(owner)
        with self._connection() as connection:
            row = self._query(
                connection, "SELECT owner_id FROM rnxcel_consents WHERE owner_id = %s",
                (owner,), one=True,
            )
            return row is not None

    def accept_consent(self, owner):
        owner = self._owner(owner)
        with self._connection(write=True) as connection:
            suffix = (
                " ON CONFLICT(owner_id) DO NOTHING" if self.dialect == "sqlite"
                else " ON DUPLICATE KEY UPDATE accepted_at = accepted_at"
            )
            self._query(
                connection,
                "INSERT INTO rnxcel_consents (owner_id, accepted_at) VALUES (%s, %s)" + suffix,
                (owner, datetime.now(timezone.utc).replace(tzinfo=None)),
            )

    def _attempt(self, row):
        if row is None:
            return None
        try:
            snapshot = _decode(row["snapshot_json"])
            items = engine.flatten(snapshot)
            responses = _decode(row["responses_json"])
            current = row["current_index"]
            if (type(current) is not int or not isinstance(responses, list)
                    or len(responses) != current or not 0 <= current <= len(items)
                    or row["status"] != (
                        "completed" if current == len(items) else "active")):
                raise ValueError("Invalid attempt state")
            for index, response in enumerate(responses):
                if (type(response.get("index")) is not int
                        or response["index"] != index
                        or engine.score(items[index], response.get("selections"))
                        != (response.get("earned"), response.get("possible"))):
                    raise ValueError("Invalid stored response")
            return {
                "id": row["id"], "owner_id": row["owner_id"],
                "snapshot": snapshot, "responses": responses,
                "current_index": current, "status": row["status"],
                "created_at": _timestamp(row["created_at"]),
                "completed_at": _timestamp(row["completed_at"]),
            }
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise RNXcelUnavailable("content_unavailable") from exc

    def create(self, owner, versions):
        owner = self._owner(owner)
        if not isinstance(versions, list) or not versions:
            raise ValueError("Select at least one published version")
        selected = [engine.validate_version(version) for version in versions]
        ids = [version["version_id"] for version in selected]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate selected version")
        with self._connection(write=True) as connection:
            locked = {}
            for version_id in sorted(ids):
                row = self._query(
                    connection,
                    "SELECT version_id, publication_status, exam_profile, authored_json "
                    "FROM rnxcel_item_versions WHERE version_id = %s" + self._lock,
                    (version_id,), one=True,
                )
                if row is None or row["publication_status"] != "published":
                    raise RNXcelConflict("Selected version is no longer published")
                locked[version_id] = self._version(row)
            for version in selected:
                if _json(version) != _json(locked[version["version_id"]]):
                    raise RNXcelConflict("Selected version has changed")
            snapshot = [locked[version_id] for version_id in ids]
            attempt_id = str(uuid4())
            created_at = datetime.now(timezone.utc).replace(tzinfo=None)
            self._query(
                connection,
                "INSERT INTO rnxcel_attempts "
                "(id, owner_id, snapshot_json, responses_json, current_index, "
                "status, created_at, completed_at) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s)",
                (attempt_id, owner, _json(snapshot), "[]", 0, "active", created_at, None),
            )
            return self._fetch(connection, owner, attempt_id)

    def _fetch(self, connection, owner, attempt_id, *, lock=False):
        row = self._query(
            connection,
            "SELECT id, owner_id, snapshot_json, responses_json, current_index, "
            "status, created_at, completed_at FROM rnxcel_attempts "
            "WHERE owner_id = %s AND id = %s" + (self._lock if lock else ""),
            (owner, attempt_id), one=True,
        )
        return self._attempt(row)

    def get(self, owner, attempt_id):
        owner, attempt_id = self._owner(owner), self._id(attempt_id)
        with self._connection() as connection:
            return self._fetch(connection, owner, attempt_id)

    def history(self, owner):
        owner = self._owner(owner)
        with self._connection() as connection:
            rows = self._query(
                connection,
                "SELECT id, owner_id, snapshot_json, responses_json, current_index, "
                "status, created_at, completed_at FROM rnxcel_attempts "
                "WHERE owner_id = %s ORDER BY created_at DESC, id DESC", (owner,),
            )
            return [self._attempt(row) for row in rows]

    def answer(self, owner, id, index, selections):
        owner, attempt_id = self._owner(owner), self._id(id)
        if type(index) is not int or index < 0:
            raise ValueError("Invalid item index")
        with self._connection(write=True) as connection:
            attempt = self._fetch(connection, owner, attempt_id, lock=True)
            if attempt is None:
                return None
            items = engine.flatten(attempt["snapshot"])
            if index >= len(items):
                raise ValueError("Invalid item index")
            if index > attempt["current_index"]:
                raise RNXcelConflict("Answer the current item first")
            if index < attempt["current_index"]:
                previous = attempt["responses"][index]["selections"]
                if (isinstance(selections, list) and len(selections) == len(previous)
                        and all(isinstance(value, str) for value in selections)
                        and set(selections) == set(previous)):
                    return attempt
                raise RNXcelConflict("This item has already been answered")
            if index == attempt["current_index"] and attempt["status"] != "active":
                raise RNXcelConflict("Answer the current item first")
            earned, possible = engine.score(items[index], selections)
            canonical = [
                option["id"] for option in items[index]["options"]
                if option["id"] in selections
            ]
            attempt["responses"].append({
                "index": index, "selections": canonical,
                "earned": earned, "possible": possible,
            })
            current = index + 1
            completed = current == len(items)
            self._query(
                connection,
                "UPDATE rnxcel_attempts SET responses_json = %s, current_index = %s, "
                "status = %s, completed_at = %s WHERE owner_id = %s AND id = %s",
                (_json(attempt["responses"]), current,
                 "completed" if completed else "active",
                 datetime.now(timezone.utc).replace(tzinfo=None) if completed else None,
                 owner, attempt_id),
            )
            return self._fetch(connection, owner, attempt_id)
