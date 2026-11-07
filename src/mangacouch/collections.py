from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
import uuid

from .database import Library
from .errors import InvalidInput, NotFound, RevisionConflict
from .models import canonical_json
from .searching import SearchOptions, rule_conditions

PROGRESS = (
    "json_extract(p.state_json,'$.completed') AS completed,"
    "json_extract(p.state_json,'$.resume_page') IS NOT NULL AS started"
)


def name_value(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 256:
        raise InvalidInput("Collection name must contain 1 to 256 characters")
    return value


def encode_cursor(name: str, identifier: str) -> str:
    return base64.urlsafe_b64encode(canonical_json([name, identifier]).encode()).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[str, str]:
    try:
        name, identifier = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if isinstance(name, str) and isinstance(identifier, str):
            return name, identifier
    except (ValueError, TypeError, binascii.Error):
        pass
    raise InvalidInput("Invalid collection cursor")


class Collections:
    def __init__(self, library: Library):
        self.library = library

    @staticmethod
    def _get(db: sqlite3.Connection, identifier: str, revision: int | None = None) -> sqlite3.Row:
        row = db.execute("SELECT * FROM collection WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise NotFound("Collection not found", code="collection_not_found")
        if revision is not None and row["revision"] != revision:
            raise RevisionConflict("Collection changed; reload the current order")
        return row

    @staticmethod
    def _rule(rule: dict) -> dict:
        query = rule.get("query", "")
        options = SearchOptions.model_validate(rule.get("options", {}))
        if not isinstance(query, str) or len(query) > 1024:
            raise InvalidInput("Invalid collection rule")
        options.order = "default"
        options.seed = None
        return {"query": query, "options": options.model_dump()}

    @staticmethod
    def _stored_rule(db: sqlite3.Connection, identifier: str) -> dict | None:
        row = db.execute(
            "SELECT rule_json FROM collection_rule WHERE collection_id=?", (identifier,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _members(db: sqlite3.Connection, identifier: str, rule: dict | None, columns: str = "") -> list:
        """Member rows in collection order: static by position, dynamic by book id."""
        extra = "," + columns if columns else ""
        if rule is not None:
            conditions, values = rule_conditions(rule["query"], SearchOptions.model_validate(rule["options"]))
            where = " AND ".join(conditions) or "1"
            return db.execute(
                f"SELECT b.id AS book_id{extra} FROM book b JOIN personal p ON p.book_id=b.id "
                f"WHERE {where} ORDER BY b.id",
                values,
            ).fetchall()
        return db.execute(
            f"SELECT i.book_id,i.position{extra} FROM collection_item i "
            "JOIN personal p ON p.book_id=i.book_id WHERE i.collection_id=? ORDER BY i.position",
            (identifier,),
        ).fetchall()

    def freeze(self, identifier: str, revision: int) -> dict:
        with self.library.transaction() as db:
            self._get(db, identifier, revision)
            rule = self._stored_rule(db, identifier)
            if rule is None:
                return self.get(identifier)
            ids = [row["book_id"] for row in self._members(db, identifier, rule)]
            db.execute("DELETE FROM collection_item WHERE collection_id=?", (identifier,))
            db.executemany(
                "INSERT INTO collection_item VALUES(?,?,?)",
                ((identifier, book_id, pos) for pos, book_id in enumerate(ids)),
            )
            db.execute("DELETE FROM collection_rule WHERE collection_id=?", (identifier,))
            db.execute("UPDATE collection SET revision=revision+1 WHERE id=?", (identifier,))
        return self.get(identifier)

    def create(self, name: str, rule: dict | None = None) -> dict:
        identifier = str(uuid.uuid4())
        with self.library.transaction() as db:
            db.execute("INSERT INTO collection(id,name) VALUES(?,?)", (identifier, name_value(name)))
            if rule is not None:
                db.execute(
                    "INSERT INTO collection_rule VALUES(?,?)", (identifier, canonical_json(self._rule(rule)))
                )
        return self.get(identifier)

    def list(self, *, limit: int = 50, after: str = "") -> dict:
        """Collections by name (case-insensitive) then id, with an opaque keyset cursor."""
        if not 1 <= limit <= 200 or len(after) > 2048:
            raise InvalidInput("Invalid collection page size")
        name, identifier = decode_cursor(after) if after else ("", "")
        with self.library.connection() as db:
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT c.*,r.rule_json,(SELECT count(*) FROM collection_item i "
                "WHERE i.collection_id=c.id) AS count FROM collection c "
                "LEFT JOIN collection_rule r ON r.collection_id=c.id "
                "WHERE (c.name COLLATE NOCASE,c.id)>(?,?) ORDER BY c.name COLLATE NOCASE,c.id LIMIT ?",
                (name, identifier, limit + 1),
            ).fetchall()
            result = []
            for row in rows[:limit]:
                item = dict(row)
                rule = json.loads(item.pop("rule_json")) if row["rule_json"] else None
                result.append({**item, "kind": "dynamic" if rule else "static", "rule": rule})
            dynamic = [item for item in result if item["rule"] is not None]
            if dynamic:
                expressions, values = [], []
                for item in dynamic:
                    rule = item["rule"]
                    conditions, params = rule_conditions(
                        rule["query"], SearchOptions.model_validate(rule["options"])
                    )
                    expressions.append(
                        "coalesce(sum(CASE WHEN "
                        + (" AND ".join(conditions) or "1")
                        + " THEN 1 ELSE 0 END),0)"
                    )
                    values.extend(params)
                counts = db.execute(
                    "SELECT " + ",".join(expressions) + " FROM book b JOIN personal p ON p.book_id=b.id",
                    values,
                ).fetchone()
                for item, count in zip(dynamic, counts, strict=True):
                    item["count"] = count
        last = rows[limit - 1] if len(rows) > limit else None
        return {"collections": result, "after": encode_cursor(last["name"], last["id"]) if last else None}

    def get(
        self,
        identifier: str,
        *,
        limit: int = 50,
        after_position: int = -1,
        expected_result: str | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        if not 1 <= limit <= 200 or after_position < -1:
            raise InvalidInput("Invalid collection page bounds")
        with self.library.connection() as db:
            db.execute("BEGIN")
            collection = dict(self._get(db, identifier, expected_revision))
            rule = self._stored_rule(db, identifier)
            if rule is None:
                count = db.execute(
                    "SELECT count(*) FROM collection_item WHERE collection_id=?", (identifier,)
                ).fetchone()[0]
                items = [
                    dict(row)
                    for row in db.execute(
                        "SELECT book_id,position FROM collection_item WHERE collection_id=? AND position>? "
                        "ORDER BY position LIMIT ?",
                        (identifier, after_position, limit),
                    )
                ]
                token = None
            else:
                # Dynamic results are hashed so later pages detect a changed result set.
                ids = [row["book_id"] for row in self._members(db, identifier, rule)]
                count = len(ids)
                token = hashlib.sha256("\n".join(ids).encode()).hexdigest()
                items = [
                    {"book_id": book_id, "position": position}
                    for position, book_id in enumerate(ids)
                    if after_position < position <= after_position + limit
                ]
            if expected_result is not None and expected_result != token:
                raise RevisionConflict("Collection results changed; reload the first page")
            collection.update(
                count=count,
                kind="dynamic" if rule else "static",
                rule=rule,
                result_token=token,
                items=items,
                after_position=items[-1]["position"] if len(items) == limit else None,
            )
            return collection

    def context(self, identifier: str, book_id: str | None = None) -> dict:
        with self.library.connection() as db:
            db.execute("BEGIN")
            result = dict(self._get(db, identifier))
            members = self._members(db, identifier, self._stored_rule(db, identifier), PROGRESS)
        ids = [row["book_id"] for row in members]
        unread = next(
            (row["book_id"] for row in members if not row["completed"] and not row["started"]), None
        )
        reading = next((row["book_id"] for row in members if not row["completed"] and row["started"]), None)
        result["count"] = len(ids)
        result["all_completed"] = bool(ids) and unread is None and reading is None
        result["continue_id"] = unread or reading or (ids[0] if ids else None)
        result["continue_state"] = "unread" if unread else "reading" if reading else "completed"
        if book_id is not None:
            index = next((index for index, member in enumerate(ids) if member == book_id), None)
            if index is None:
                raise NotFound("Book is not in this collection")
            result.update(
                index=index,
                previous_id=ids[index - 1] if index else None,
                next_id=ids[index + 1] if index + 1 < len(ids) else None,
            )
        return result

    def rename(self, identifier: str, name: str, revision: int) -> dict:
        with self.library.transaction() as db:
            row = self._get(db, identifier, revision)
            name = name_value(name)
            if row["name"] != name:
                db.execute("UPDATE collection SET name=?,revision=revision+1 WHERE id=?", (name, identifier))
        return self.get(identifier)

    def edit_item(
        self,
        identifier: str,
        book_id: str,
        revision: int,
        *,
        action: str = "add",
        before_id: str | None = None,
    ) -> dict:
        if action not in {"add", "move", "remove"}:
            raise InvalidInput("Invalid collection action")
        with self.library.transaction() as db:
            self._get(db, identifier, revision)
            if db.execute("SELECT 1 FROM collection_rule WHERE collection_id=?", (identifier,)).fetchone():
                raise InvalidInput(
                    "Convert a dynamic collection before editing its members", code="dynamic_collection"
                )
            member = db.execute(
                "SELECT position FROM collection_item WHERE collection_id=? AND book_id=?",
                (identifier, book_id),
            ).fetchone()
            changed = False
            if action == "add":
                if db.execute("SELECT 1 FROM book WHERE id=?", (book_id,)).fetchone() is None:
                    raise NotFound("Book not found", code="book_not_found")
                if member is None:
                    db.execute(
                        "INSERT INTO collection_item VALUES(?,?,(SELECT coalesce(max(position)+1,0) "
                        "FROM collection_item WHERE collection_id=?))",
                        (identifier, book_id, identifier),
                    )
                    changed = True
            elif member is None:
                raise NotFound("Book is not in this collection")
            elif action == "remove":
                # Positions may have gaps; only their order matters.
                db.execute(
                    "DELETE FROM collection_item WHERE collection_id=? AND book_id=?", (identifier, book_id)
                )
                changed = True
            elif before_id != book_id:
                changed = self._move(db, identifier, book_id, before_id)
            if changed:
                db.execute("UPDATE collection SET revision=revision+1 WHERE id=?", (identifier,))
        return self.get(identifier)

    @staticmethod
    def _move(db: sqlite3.Connection, identifier: str, book_id: str, before_id: str | None) -> bool:
        current = [
            (row[0], row[1])
            for row in db.execute(
                "SELECT book_id,position FROM collection_item WHERE collection_id=? ORDER BY position",
                (identifier,),
            )
        ]
        order = [item for item, _ in current]
        if before_id is not None and before_id not in order:
            raise NotFound("Target position is not in this collection")
        desired = [item for item in order if item != book_id]
        desired.insert(desired.index(before_id) if before_id else len(desired), book_id)
        if desired == order:
            return False
        positions = dict(current)
        moved = [(item, index) for index, item in enumerate(desired) if positions[item] != index]
        # Rewrite only rows whose position changes; untouched rows already hold their index.
        db.executemany(
            "DELETE FROM collection_item WHERE collection_id=? AND book_id=?",
            ((identifier, item) for item, _ in moved),
        )
        db.executemany(
            "INSERT INTO collection_item VALUES(?,?,?)", ((identifier, item, index) for item, index in moved)
        )
        return True

    def delete(self, identifier: str, revision: int) -> None:
        with self.library.transaction() as db:
            self._get(db, identifier, revision)
            db.execute("DELETE FROM collection WHERE id=?", (identifier,))
