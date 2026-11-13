from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

from pydantic import ValidationError

from .cursors import decode_cursor, encode_cursor
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
        try:
            options = SearchOptions.model_validate(rule.get("options", {}))
        except ValidationError as error:
            raise InvalidInput("Invalid collection rule") from error
        if not isinstance(query, str) or len(query) > 1024:
            raise InvalidInput("Invalid collection rule")
        options.order = "default"
        options.seed = None
        rule_conditions(query, options)
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

    def list(self, *, limit: int = 50, after: str = "", kind: str | None = None, counts: bool = True) -> dict:
        """Collections by name (case-insensitive) then id, with an opaque keyset cursor."""
        if not 1 <= limit <= 200:
            raise InvalidInput("Invalid collection page size")
        name, identifier = decode_cursor(after, str, str) if after else ("", "")
        with self.library.connection() as db:
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT c.*,r.rule_json,"
                + (
                    "(SELECT count(*) FROM collection_item i WHERE i.collection_id=c.id)"
                    if counts
                    else "NULL"
                )
                + " AS count FROM collection c "
                "LEFT JOIN collection_rule r ON r.collection_id=c.id "
                "WHERE (c.name COLLATE NOCASE,c.id)>(?,?) AND "
                "(? IS NULL OR (?='static' AND r.collection_id IS NULL) OR "
                "(?='dynamic' AND r.collection_id IS NOT NULL)) ORDER BY c.name COLLATE NOCASE,c.id LIMIT ?",
                (name, identifier, kind, kind, kind, limit + 1),
            ).fetchall()
            result = []
            for row in rows[:limit]:
                item = dict(row)
                rule = json.loads(item.pop("rule_json")) if row["rule_json"] else None
                result.append({**item, "kind": "dynamic" if rule else "static", "rule": rule})
            dynamic = [item for item in result if item["rule"] is not None] if counts else []
            if dynamic:
                expressions, values = [], []
                for item in dynamic:
                    rule = item["rule"]
                    try:
                        conditions, params = rule_conditions(
                            rule["query"], SearchOptions.model_validate(rule["options"])
                        )
                    except (ValueError, KeyError, TypeError):
                        item.update(error="invalid_input", count=None)
                        continue
                    expressions.append(
                        "coalesce(sum(CASE WHEN "
                        + (" AND ".join(conditions) or "1")
                        + " THEN 1 ELSE 0 END),0)"
                    )
                    values.extend(params)
                totals = (
                    db.execute(
                        "SELECT " + ",".join(expressions) + " FROM book b JOIN personal p ON p.book_id=b.id",
                        values,
                    ).fetchone()
                    if expressions
                    else []
                )
                for item, count in zip(
                    [item for item in dynamic if "error" not in item], totals, strict=True
                ):
                    item["count"] = count
        last = rows[limit - 1] if len(rows) > limit else None
        return {"items": result, "after": encode_cursor(last["name"], last["id"]) if last else None}

    def get(
        self,
        identifier: str,
        *,
        limit: int = 50,
        after: str = "",
        expected_result: str | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        if not 1 <= limit <= 200:
            raise InvalidInput("Invalid collection page bounds")
        with self.library.connection() as db:
            db.execute("BEGIN")
            collection = dict(self._get(db, identifier, expected_revision))
            rule = self._stored_rule(db, identifier)
            if rule is None:
                (after_position,) = decode_cursor(after, int) if after else (-1,)
                if expected_result is not None:
                    raise RevisionConflict("Collection results changed; reload the first page")
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
                following = encode_cursor(items[-1]["position"]) if len(items) == limit else None
            else:
                count, token = self._result_token(db, identifier, rule, expected_result)
                items = self._dynamic_items(db, rule, limit, after)
                following = (
                    encode_cursor(items[-1]["position"], items[-1]["book_id"])
                    if len(items) == limit
                    else None
                )
            collection.update(
                count=count,
                kind="dynamic" if rule else "static",
                rule=rule,
                result_token=token,
                items=items,
                after=following,
            )
            return collection

    def _result_token(
        self, db: sqlite3.Connection, identifier: str, rule: dict, expected: str | None
    ) -> tuple[int, str]:
        """The member count and a token that later pages send back to detect changed results.

        The token carries the search revision, so while nothing searchable changed a later page
        neither lists nor counts the members again; after a change they are hashed and compared."""
        revision = db.execute("SELECT token FROM search_revision WHERE singleton=1").fetchone()[0]
        known = None
        if expected is not None:
            try:
                known = decode_cursor(expected, str, int, str)
            except InvalidInput:
                raise RevisionConflict("Collection results changed; reload the first page") from None
        if known is not None and known[0] == revision:
            count, digest = known[1], known[2]
        else:
            ids = [row["book_id"] for row in self._members(db, identifier, rule)]
            count, digest = len(ids), hashlib.sha256("\n".join(ids).encode()).hexdigest()
            if known is not None and known[1:] != (count, digest):
                raise RevisionConflict("Collection results changed; reload the first page")
        return count, encode_cursor(revision, count, digest)

    @staticmethod
    def _dynamic_items(db: sqlite3.Connection, rule: dict, limit: int, after: str) -> list[dict]:
        """One page of a dynamic collection, by book id after the cursor's book."""
        position, after_id, offset = -1, "", 0
        if after:
            try:
                position, after_id = decode_cursor(after, int, str)
            except InvalidInput:
                # A bare position, as OPDS page links carry it.
                (position,) = decode_cursor(after, int)
                offset = position + 1
        conditions, values = rule_conditions(rule["query"], SearchOptions.model_validate(rule["options"]))
        conditions.append("b.id>?")
        rows = db.execute(
            "SELECT b.id FROM book b JOIN personal p ON p.book_id=b.id WHERE "
            + " AND ".join(conditions)
            + " ORDER BY b.id LIMIT ? OFFSET ?",
            (*values, after_id, limit, offset),
        ).fetchall()
        return [{"book_id": row[0], "position": position + 1 + index} for index, row in enumerate(rows)]

    def context(self, identifier: str, book_id: str | None = None) -> dict:
        with self.library.connection() as db:
            db.execute("BEGIN")
            result = dict(self._get(db, identifier))
            rule = self._stored_rule(db, identifier)
            if rule is None:
                base = (
                    "SELECT i.book_id,i.position AS ordering,"
                    + PROGRESS
                    + (
                        " FROM collection_item i JOIN personal p ON p.book_id=i.book_id "
                        "WHERE i.collection_id=?"
                    )
                )
                params = [identifier]
            else:
                conditions, params = rule_conditions(
                    rule["query"], SearchOptions.model_validate(rule["options"])
                )
                base = (
                    "SELECT b.id AS book_id,b.id AS ordering,"
                    + PROGRESS
                    + (
                        " FROM book b JOIN personal p ON p.book_id=b.id WHERE "
                        + (" AND ".join(conditions) or "1")
                    )
                )
            prefix = "WITH members AS (" + base + ") "
            result["count"] = db.execute(prefix + "SELECT count(*) FROM members", params).fetchone()[0]
            unfinished = db.execute(
                prefix + "SELECT * FROM members WHERE NOT completed ORDER BY ordering LIMIT 1", params
            ).fetchone()
            first = (
                unfinished
                or db.execute(prefix + "SELECT * FROM members ORDER BY ordering LIMIT 1", params).fetchone()
            )
            result["all_completed"] = bool(first) and unfinished is None
            result["continue_id"] = first["book_id"] if first else None
            result["continue_state"] = (
                ("reading" if unfinished["started"] else "unread") if unfinished else "completed"
            )
            if book_id is not None:
                current = db.execute(
                    prefix + "SELECT ordering FROM members WHERE book_id=?", (*params, book_id)
                ).fetchone()
                if current is None:
                    raise NotFound("Book is not in this collection")
                position = current[0]
                result["index"] = db.execute(
                    prefix + "SELECT count(*) FROM members WHERE ordering<?", (*params, position)
                ).fetchone()[0]
                for name, compare, order in (("previous_id", "<", "DESC"), ("next_id", ">", "ASC")):
                    row = db.execute(
                        prefix
                        + f"SELECT book_id FROM members WHERE ordering{compare}? "
                        + f"ORDER BY ordering {order} LIMIT 1",
                        (*params, position),
                    ).fetchone()
                    result[name] = row[0] if row else None
            return result

    def update_rule(self, identifier: str, rule: dict, revision: int) -> dict:
        value = canonical_json(self._rule(rule))
        with self.library.transaction() as db:
            self._get(db, identifier, revision)
            if self._stored_rule(db, identifier) is None:
                raise InvalidInput("Only dynamic collections have rules")
            db.execute("UPDATE collection_rule SET rule_json=? WHERE collection_id=?", (value, identifier))
            db.execute("UPDATE collection SET revision=revision+1 WHERE id=?", (identifier,))
        return self.get(identifier)

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
