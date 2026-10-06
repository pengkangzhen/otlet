"""SQLite-backed storage for papers, tags, conversations, and messages."""

import functools
import json
import re
import shutil
import sqlite3
import threading
import uuid
import zlib
from pathlib import Path
from typing import Sequence

from otlet.models.author import Author
from otlet.models.paper import Paper
from otlet.models.tag import Tag

_DOI_URL_PREFIX = re.compile(r"^https?://(dx\.)?doi\.org/", re.IGNORECASE)


def normalize_doi(doi: str | None) -> str | None:
    """Canonical form for dedup/storage: strip the resolver URL, lowercase."""
    if not doi:
        return None
    norm = _DOI_URL_PREFIX.sub("", doi.strip()).strip().lower()
    return norm or None

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS tags (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    color       TEXT,
    is_top      INTEGER DEFAULT 0,   -- 1 = research theme (top-level)
    parent_id   TEXT REFERENCES tags(id) ON DELETE SET NULL,  -- manual nesting
    category    TEXT,                -- 'problem' | 'model' | 'algorithm'
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS authors (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    first_name  TEXT DEFAULT '',
    last_name   TEXT DEFAULT '',
    affiliation TEXT,
    orcid       TEXT,
    email       TEXT
);

CREATE TABLE IF NOT EXISTS papers (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    year        INTEGER,
    venue       TEXT,
    volume      TEXT,
    issue       TEXT,
    pages       TEXT,
    publisher   TEXT,
    language    TEXT,
    doi         TEXT UNIQUE,
    url         TEXT,
    abstract    TEXT,
    keywords    TEXT DEFAULT '[]',
    citation_count INTEGER,
    added_date  TEXT DEFAULT (date('now')),
    pdf_path    TEXT,
    pdf_fingerprint TEXT,
    bibtex_key  TEXT,
    paper_type  TEXT,
    is_deleted  INTEGER DEFAULT 0,
    deleted_date TEXT
);
CREATE TABLE IF NOT EXISTS paper_tags (
    paper_id    TEXT REFERENCES papers(id) ON DELETE CASCADE,
    tag_id      TEXT REFERENCES tags(id) ON DELETE CASCADE,
    source      TEXT DEFAULT 'manual',   -- 'manual' | 'auto'
    PRIMARY KEY (paper_id, tag_id)
);

-- Notes (multiple timestamped notes per paper)
CREATE TABLE IF NOT EXISTS paper_notes (
    id          TEXT PRIMARY KEY,
    paper_id    TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paper_authors (
    paper_id    TEXT REFERENCES papers(id) ON DELETE CASCADE,
    author_id   TEXT REFERENCES authors(id) ON DELETE CASCADE,
    position    INTEGER,
    PRIMARY KEY (paper_id, author_id)
);

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    paper_id    TEXT REFERENCES papers(id) ON DELETE CASCADE,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'tool')),
    content         TEXT NOT NULL,
    tool_call_id    TEXT,               -- pairs a tool message with its call
    tool_name       TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

-- Per-page PDF text: authoritative copy (compressed) for page reads
CREATE TABLE IF NOT EXISTS pdf_text (
    paper_id    TEXT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    page_count  INTEGER NOT NULL,
    pages       BLOB NOT NULL,          -- zlib JSON array of page texts
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- Searchable mirror of pdf_text, one row per page.
-- trigram tokenizer = substring semantics, works for CJK and Latin alike.
CREATE VIRTUAL TABLE IF NOT EXISTS pdf_fts USING fts5(
    paper_id UNINDEXED, page UNINDEXED, text, tokenize='trigram'
);

CREATE INDEX IF NOT EXISTS idx_paper_tags_tag ON paper_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_paper_tags_paper ON paper_tags(paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_notes_paper ON paper_notes(paper_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversations_paper ON conversations(paper_id);
"""

# Migrations for existing databases
_MIGRATIONS = [
    "ALTER TABLE papers ADD COLUMN paper_type TEXT",
    "ALTER TABLE paper_tags ADD COLUMN source TEXT DEFAULT 'manual'",
    "ALTER TABLE papers ADD COLUMN is_deleted INTEGER DEFAULT 0",
    "ALTER TABLE papers ADD COLUMN deleted_date TEXT",
    "ALTER TABLE authors ADD COLUMN first_name TEXT DEFAULT ''",
    "ALTER TABLE authors ADD COLUMN last_name TEXT DEFAULT ''",
    "ALTER TABLE papers ADD COLUMN volume TEXT",
    "ALTER TABLE papers ADD COLUMN issue TEXT",
    "ALTER TABLE papers ADD COLUMN pages TEXT",
    "ALTER TABLE papers ADD COLUMN publisher TEXT",
    "ALTER TABLE papers ADD COLUMN language TEXT",
    "ALTER TABLE tags ADD COLUMN category TEXT",
    "ALTER TABLE tags ADD COLUMN is_top INTEGER DEFAULT 0",
    "ALTER TABLE papers ADD COLUMN pdf_fingerprint TEXT",
]

# Research-facet classification for the project overview tag tree
_TAG_CATEGORIES = {"problem", "model", "algorithm"}


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _normalize_tag_name(name: str) -> str:
    """Normalize a tag name: lowercase, collapse whitespace, strip."""
    return re.sub(r"\s+", "-", name.strip()).lower()


def _locked(func):
    """Serialize access to the shared SQLite connection.

    pywebview executes bridge calls on worker threads, so every public
    Database method must hold the lock for its full read/write sequence.
    RLock allows nested calls between decorated methods.
    """
    @functools.wraps(func)
    def wrapper(self: "Database", *args, **kwargs):
        with self._lock:
            return func(self, *args, **kwargs)
    return wrapper


class Database:
    """Manages the SQLite database for otlet."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(
                str(self._path), check_same_thread=False
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            # Tolerate a stale lock left behind by a crashed previous run
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.row_factory = sqlite3.Row
            had_tags = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tags'"
            ).fetchone() is not None
            self._conn.executescript(_SCHEMA)
            if not had_tags:
                # Born at the current schema — stamp the tag-model generation
                # so the legacy fold never misreads the manual nesting column
                # as the legacy parent_id hierarchy
                self._conn.execute("PRAGMA user_version = 3")
            # Run migrations (ignore errors if column already exists)
            for sql in _MIGRATIONS:
                try:
                    self._conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            # One-time: fold the legacy single notes field into paper_notes
            try:
                cols = [r[1] for r in self._conn.execute("PRAGMA table_info(papers)")]
                if "notes" in cols:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO paper_notes "
                        "(id, paper_id, content) "
                        "SELECT lower(hex(randomblob(6))), id, notes "
                        "FROM papers WHERE notes IS NOT NULL AND TRIM(notes) <> ''"
                    )
                    self._conn.execute("ALTER TABLE papers DROP COLUMN notes")
                    self._conn.commit()
            except sqlite3.OperationalError:
                pass
            # One-time: fold perspectives/groups + the legacy parent_id
            # hierarchy into flat tags with an is_top flag
            self._migrate_tag_model()
            # The nesting column returns *after* the fold: on a legacy
            # database the old hierarchy is folded away first, and the new
            # column starts empty with drag-and-drop nesting semantics
            try:
                self._conn.execute(
                    "ALTER TABLE tags ADD COLUMN parent_id TEXT "
                    "REFERENCES tags(id) ON DELETE SET NULL"
                )
            except sqlite3.OperationalError:
                pass  # column already exists (fresh schema / v3 database)
            # The index lives here, not in _SCHEMA: on a v2 database the
            # column does not exist until the ALTER above has run
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tags_parent ON tags(parent_id)"
            )
            # Same reason as idx_tags_parent: legacy papers tables gain
            # pdf_fingerprint via ALTER, after _SCHEMA has run
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_papers_fingerprint "
                "ON papers(pdf_fingerprint)"
            )
            # One-time: widen messages to carry tool messages (agent loop),
            # and normalize stored DOIs to the canonical resolver-free form
            self._migrate_messages_table()
            self._normalize_stored_dois()
            self._conn.execute("PRAGMA user_version = 3")
            # Merge duplicate tags on startup (only if explicitly requested)
            # self.merge_duplicate_tags()
        except sqlite3.Error as e:
            raise RuntimeError(
                f"SQLite error at {self._path}: {e}"
            ) from e

    @_locked
    def close(self) -> None:
        """Close the database, flushing WAL first. Safe to call twice."""
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self._conn.close()

    # ── Tags ──────────────────────────────────────────────────

    @_locked
    def create_tag(self, name: str, *, color: str | None = None) -> Tag:
        norm = _normalize_tag_name(name)
        # Return existing tag if a normalized match already exists
        existing = self.get_tag_by_name(norm)
        if existing:
            return existing
        tag = Tag(id=_new_id(), name=norm, color=color)
        self._conn.execute(
            "INSERT INTO tags (id, name, color) VALUES (?, ?, ?)",
            (tag.id, tag.name, tag.color),
        )
        self._conn.commit()
        return tag

    @_locked
    def get_tag(self, tag_id: str) -> Tag | None:
        row = self._conn.execute(
            "SELECT * FROM tags WHERE id = ?", (tag_id,)
        ).fetchone()
        return Tag(**dict(row)) if row else None

    @_locked
    def get_tag_by_name(self, name: str) -> Tag | None:
        row = self._conn.execute(
            "SELECT * FROM tags WHERE name = ?", (name,)
        ).fetchone()
        return Tag(**dict(row)) if row else None

    @_locked
    def list_tags(self, include_auto: bool = False) -> list[Tag]:
        """Tags visible in the sidebar.

        Without include_auto: only tags with at least one manual link to a
        live paper. Auto-only tags are hidden to keep the sidebar clean.
        include_auto=True returns the full tag table.
        """
        if include_auto:
            rows = self._conn.execute("SELECT * FROM tags ORDER BY name").fetchall()
            return [Tag(**dict(r)) for r in rows]
        rows = self._conn.execute(
            """SELECT t.* FROM tags t
                WHERE EXISTS (
                    SELECT 1 FROM paper_tags pt
                    JOIN papers p ON p.id = pt.paper_id AND p.is_deleted = 0
                    WHERE pt.tag_id = t.id AND pt.source = 'manual'
                )
                ORDER BY name"""
        ).fetchall()
        return [Tag(**dict(r)) for r in rows]

    @_locked
    def list_tags_with_counts(self, include_auto: bool = False) -> list[dict]:
        """Sidebar tag list: [{id, name, is_top, parent_id, category, color,
        paper_count}].

        Themes (is_top=1) sort first, then by live-paper count. Tags with
        zero counted papers are omitted.
        """
        source_clause = "" if include_auto else "AND pt.source = 'manual'"
        rows = self._conn.execute(
            f"""SELECT t.id, t.name, t.is_top, t.parent_id, t.category, t.color,
                       COUNT(p.id) AS paper_count
                FROM tags t
                LEFT JOIN paper_tags pt ON pt.tag_id = t.id {source_clause}
                LEFT JOIN papers p ON p.id = pt.paper_id AND p.is_deleted = 0
                GROUP BY t.id
                HAVING paper_count > 0
                ORDER BY t.is_top DESC, paper_count DESC, t.name"""
        ).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def get_cooccurring_tags(
        self, tag_id: str, *, include_auto: bool = False
    ) -> list[dict]:
        """Tags sharing at least one live paper with the given tag.

        [{id, name, is_top, category, paper_count}] ordered by co-occurrence
        count. This is the derived second level under a theme tag — the
        sidebar tree is a query, not stored structure. Tags nested under a
        manual parent (parent_id set via drag-and-drop) live under that
        parent only and are excluded here.
        """
        source_clause = (
            "" if include_auto
            else "AND pt1.source = 'manual' AND pt2.source = 'manual'"
        )
        rows = self._conn.execute(
            f"""SELECT t.id, t.name, t.is_top, t.parent_id, t.category,
                       COUNT(*) AS paper_count
                FROM paper_tags pt1
                JOIN paper_tags pt2 ON pt2.paper_id = pt1.paper_id
                JOIN tags t ON t.id = pt2.tag_id
                JOIN papers p ON p.id = pt1.paper_id AND p.is_deleted = 0
                WHERE pt1.tag_id = ? AND t.id != ? AND t.parent_id IS NULL
                      {source_clause}
                GROUP BY t.id
                ORDER BY paper_count DESC, t.name""",
            (tag_id, tag_id),
        ).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def delete_tag(self, tag_id: str) -> None:
        # Nested children climb to the deleted tag's level instead of
        # being dropped to the root (and the tag's own parent is optional)
        self._conn.execute(
            """UPDATE tags SET parent_id = (SELECT parent_id FROM tags WHERE id = ?)
               WHERE parent_id = ?""",
            (tag_id, tag_id),
        )
        self._conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        self._conn.commit()

    @_locked
    def set_tag_category(self, tag_id: str, category: str | None) -> None:
        """Classify a tag as 'problem' / 'model' / 'algorithm'.

        An empty value clears the classification (back to uncategorized).
        """
        if category:
            category = category.strip().lower()
            if category not in _TAG_CATEGORIES:
                valid = ", ".join(sorted(_TAG_CATEGORIES))
                raise ValueError(
                    f"category must be one of {valid}, got {category!r}"
                )
        else:
            category = None
        self._conn.execute(
            "UPDATE tags SET category = ? WHERE id = ?", (category, tag_id)
        )
        self._conn.commit()

    # ── Papers ────────────────────────────────────────────────

    @_locked
    def add_paper(self, paper: Paper) -> Paper:
        if not paper.id:
            paper = paper.model_copy(update={"id": _new_id()})
        paper.doi = normalize_doi(paper.doi)

        self._conn.execute(
            """INSERT OR IGNORE INTO papers
               (id, title, year, venue, volume, issue, pages, publisher,
                language, doi, url, abstract,
                keywords, citation_count, added_date,
                pdf_path, pdf_fingerprint, bibtex_key, paper_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                paper.id,
                paper.title,
                paper.year,
                paper.venue,
                paper.volume,
                paper.issue,
                paper.pages,
                paper.publisher,
                paper.language,
                paper.doi,
                paper.url,
                paper.abstract,
                json.dumps(paper.keywords),
                paper.citation_count,
                paper.added_date,
                paper.pdf_path,
                paper.pdf_fingerprint,
                paper.bibtex_key,
                paper.paper_type,
            ),
        )

        # Authors
        for pos, author in enumerate(paper.authors):
            aid = self._upsert_author(author)
            self._conn.execute(
                "INSERT OR IGNORE INTO paper_authors (paper_id, author_id, position) "
                "VALUES (?, ?, ?)",
                (paper.id, aid, pos),
            )

        # Tags (by name → resolve to id); paper.tags are manual, auto_tags are auto
        for tag_name in paper.tags:
            self._link_tag_by_name(paper.id, tag_name, source="manual")
        for tag_name in paper.auto_tags:
            self._link_tag_by_name(paper.id, tag_name, source="auto")

        self._conn.commit()
        return paper

    @_locked
    def get_paper(self, paper_id: str) -> Paper | None:
        row = self._conn.execute(
            "SELECT * FROM papers WHERE id = ? AND is_deleted = 0", (paper_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_paper(row)

    @_locked
    def get_paper_by_doi(self, doi: str) -> Paper | None:
        # DOIs are stored normalized (resolver-free, lowercase) at write
        # time, so a plain equality hits the UNIQUE index
        norm = normalize_doi(doi)
        if not norm:
            return None
        row = self._conn.execute(
            "SELECT * FROM papers WHERE doi = ? AND is_deleted = 0",
            (norm,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_paper(row)

    @_locked
    def get_paper_by_fingerprint(self, fingerprint: str) -> Paper | None:
        """Exact-file lookup: same PDF bytes → same SHA-256."""
        row = self._conn.execute(
            "SELECT * FROM papers WHERE pdf_fingerprint = ? AND is_deleted = 0",
            (fingerprint,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_paper(row)

    @_locked
    def get_paper_by_title(self, title: str) -> Paper | None:
        """Fuzzy title match: lowercase, strip punctuation."""
        import unicodedata

        def _norm(s: str) -> str:
            s = s.lower().strip()
            s = "".join(
                c
                for c in unicodedata.normalize("NFKD", s)
                if c.isalnum() or c.isspace()
            )
            # collapse whitespace runs so "a  b" matches "a b"
            return " ".join(s.split())[:120]

        norm = _norm(title)
        rows = self._conn.execute(
            "SELECT * FROM papers WHERE is_deleted = 0"
        ).fetchall()
        for row in rows:
            if _norm(row["title"]) == norm:
                return self._row_to_paper(row)
        return None

    @_locked
    def update_paper(self, paper_id: str, **fields) -> None:
        """Update one or more paper fields."""
        allowed = {"title", "year", "venue", "volume", "issue", "pages",
                   "publisher", "language", "doi", "url", "abstract",
                   "paper_type", "pdf_path", "pdf_fingerprint"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if "doi" in updates:
            updates["doi"] = normalize_doi(updates["doi"])
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [paper_id]
        self._conn.execute(
            f"UPDATE papers SET {set_clause} WHERE id = ?", values
        )
        self._conn.commit()

    @_locked
    def rename_tag(self, old_name: str, new_name: str) -> None:
        """Rename a tag (normalized)."""
        norm_old = _normalize_tag_name(old_name)
        norm_new = _normalize_tag_name(new_name)
        if not norm_new:
            return
        # Check if target name already exists
        existing = self.get_tag_by_name(norm_new)
        if existing:
            # Merge: re-link papers from old to existing tag, delete old
            old_tag = self.get_tag_by_name(norm_old)
            if old_tag:
                self._conn.execute(
                    "INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) "
                    "SELECT paper_id, ? FROM paper_tags WHERE tag_id = ?",
                    (existing.id, old_tag.id),
                )
                self._conn.execute(
                    "DELETE FROM paper_tags WHERE tag_id = ?", (old_tag.id,)
                )
                self._conn.execute("DELETE FROM tags WHERE id = ?", (old_tag.id,))
        else:
            self._conn.execute(
                "UPDATE tags SET name = ? WHERE name = ?",
                (norm_new, norm_old),
            )
        self._conn.commit()

    @_locked
    def set_tag_top(self, tag_id: str, is_top: bool) -> None:
        """Mark or unmark a tag as a research theme (top-level).

        Promoting to top-level detaches the tag from any manual parent —
        the two hierarchy notions are mutually exclusive.
        """
        if is_top:
            self._conn.execute(
                "UPDATE tags SET is_top = 1, parent_id = NULL WHERE id = ?",
                (tag_id,),
            )
        else:
            self._conn.execute(
                "UPDATE tags SET is_top = 0 WHERE id = ?", (tag_id,)
            )
        self._conn.commit()

    @_locked
    def set_tag_parent(self, tag_id: str, parent_id: str | None) -> None:
        """Nest a tag under another tag, or detach it with None.

        A nested tag leaves the top level (is_top cleared) and no longer
        shows up in co-occurrence lists — it is visible under its parent.
        """
        if not self._conn.execute(
            "SELECT 1 FROM tags WHERE id = ?", (tag_id,)
        ).fetchone():
            raise ValueError("Tag not found")
        if parent_id is None:
            self._conn.execute(
                "UPDATE tags SET parent_id = NULL WHERE id = ?", (tag_id,)
            )
            self._conn.commit()
            return
        if parent_id == tag_id:
            raise ValueError("A tag cannot be nested under itself")
        if not self._conn.execute(
            "SELECT 1 FROM tags WHERE id = ?", (parent_id,)
        ).fetchone():
            raise ValueError("Parent tag not found")
        # Reject moves that would create a cycle: walk the ancestor chain
        cur = parent_id
        while cur:
            if cur == tag_id:
                raise ValueError("Cannot nest a tag under its own descendant")
            r = self._conn.execute(
                "SELECT parent_id FROM tags WHERE id = ?", (cur,)
            ).fetchone()
            cur = r["parent_id"] if r else None
        self._conn.execute(
            "UPDATE tags SET parent_id = ?, is_top = 0 WHERE id = ?",
            (parent_id, tag_id),
        )
        self._conn.commit()

    # ── One-time tag model migration ────────────────────────

    def _migrate_tag_model(self) -> None:
        """Fold legacy structures into the flat tag model (2026-09).

        Guarded by PRAGMA user_version: a database stamped v2 (or born at
        that schema) has already been folded — any parent_id column it
        carries is the manual drag-and-drop nesting, not legacy hierarchy.

        - Each tag group becomes a theme tag (is_top=1); its directly-filed
          papers get a manual link to that tag.
        - Legacy parent_id themes (tags that have children) become is_top=1,
          and child-tagged papers are back-filled with the parent tag so
          theme filters keep their old semantics.
        - Container tables and the parent_id column are then dropped.
        The tags/paper_tags pair is rebuilt via an in-memory round-trip —
        ALTER TABLE RENAME would rewrite paper_tags' foreign key to the
        stale table name. A .pre-v2.bak copy of the database file is
        written first whenever anything needs migrating.
        """
        if self._conn.execute("PRAGMA user_version").fetchone()[0] >= 2:
            return
        tables = {
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        tag_cols = [r[1] for r in self._conn.execute("PRAGMA table_info(tags)")]
        needs_data_move = "tag_groups" in tables
        needs_rebuild = "parent_id" in tag_cols
        if not (needs_data_move or needs_rebuild):
            return

        # Backup first (checkpoint WAL so the copy is complete)
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copy2(self._path, f"{self._path}.pre-v2.bak")

        # FK constraints must be off while dropping/recreating referenced tables
        self._conn.commit()
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            if needs_data_move:
                for g in self._conn.execute(
                    "SELECT id, name FROM tag_groups"
                ).fetchall():
                    tag = self.get_tag_by_name(_normalize_tag_name(g["name"]))
                    if not tag:
                        tag = self.create_tag(g["name"])
                    self._conn.execute(
                        "UPDATE tags SET is_top = 1 WHERE id = ?", (tag.id,)
                    )
                    self._conn.execute(
                        """INSERT OR IGNORE INTO paper_tags
                           (paper_id, tag_id, source)
                           SELECT paper_id, ?, 'manual' FROM group_papers
                           WHERE group_id = ?""",
                        (tag.id, g["id"]),
                    )
                # Legacy parent_id themes → theme tags; back-fill parent links
                self._conn.execute(
                    """UPDATE tags SET is_top = 1 WHERE id IN (
                           SELECT DISTINCT parent_id FROM tags
                           WHERE parent_id IS NOT NULL)"""
                )
                self._conn.execute(
                    """INSERT OR IGNORE INTO paper_tags (paper_id, tag_id, source)
                       SELECT DISTINCT pt.paper_id, c.parent_id, 'manual'
                       FROM paper_tags pt
                       JOIN tags c ON c.id = pt.tag_id
                       WHERE c.parent_id IS NOT NULL"""
                )
                for tbl in (
                    "tag_group_members",
                    "group_papers",
                    "tag_groups",
                    "perspectives",
                ):
                    self._conn.execute(f"DROP TABLE IF EXISTS {tbl}")
                self._conn.commit()

            if needs_rebuild:
                tag_rows = self._conn.execute(
                    """SELECT id, name, color, COALESCE(is_top, 0) AS is_top,
                              category, created_at FROM tags"""
                ).fetchall()
                link_rows = self._conn.execute(
                    "SELECT paper_id, tag_id, source FROM paper_tags"
                ).fetchall()
                self._conn.execute("DROP TABLE paper_tags")
                self._conn.execute("DROP TABLE tags")
                self._conn.executescript(_SCHEMA)
                self._conn.executemany(
                    "INSERT INTO tags (id, name, color, is_top, category, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    [tuple(r) for r in tag_rows],
                )
                self._conn.executemany(
                    "INSERT INTO paper_tags (paper_id, tag_id, source) "
                    "VALUES (?, ?, ?)",
                    [tuple(r) for r in link_rows],
                )
                self._conn.commit()

            violations = self._conn.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if violations:
                raise RuntimeError(
                    f"tag model migration failed foreign_key_check: {violations}"
                )
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_messages_table(self) -> None:
        """Widen messages from the (user, assistant) era to include tool
        messages (role='tool' + tool_call_id/tool_name), kept for the
        agent tool loop. SQLite cannot ALTER a CHECK constraint, so a
        legacy messages table is rebuilt through a copy. Guarded by the
        presence of tool_call_id — fresh databases are born at the new
        schema and skip this. A .pre-v3.bak copy is written first.
        """
        cols = [
            r[1] for r in self._conn.execute("PRAGMA table_info(messages)")
        ]
        if not cols or "tool_call_id" in cols:
            return

        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copy2(self._path, f"{self._path}.pre-v3.bak")
        self._conn.commit()
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.execute(
                """CREATE TABLE messages_v3 (
                    id              TEXT PRIMARY KEY,
                    conversation_id TEXT REFERENCES conversations(id)
                                    ON DELETE CASCADE,
                    role            TEXT NOT NULL
                                    CHECK(role IN ('user', 'assistant', 'tool')),
                    content         TEXT NOT NULL,
                    tool_call_id    TEXT,
                    tool_name       TEXT,
                    created_at      TEXT DEFAULT (datetime('now'))
                )"""
            )
            self._conn.execute(
                """INSERT INTO messages_v3
                   (id, conversation_id, role, content, created_at)
                   SELECT id, conversation_id, role, content, created_at
                   FROM messages"""
            )
            self._conn.execute("DROP TABLE messages")
            self._conn.execute("ALTER TABLE messages_v3 RENAME TO messages")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_conv "
                "ON messages(conversation_id)"
            )
            violations = self._conn.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if violations:
                raise RuntimeError(
                    f"messages migration failed foreign_key_check: {violations}"
                )
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.commit()

    def _normalize_stored_dois(self) -> None:
        """Rewrite legacy DOIs (resolver-URL form, mixed case) into the
        canonical lowercase form so the UNIQUE index serves lookups.
        Rows whose normalized twin already exists keep their spelling —
        dedup still finds them via the twin.
        """
        cols = [
            r[1] for r in self._conn.execute("PRAGMA table_info(papers)")
        ]
        if "doi" not in cols:  # ultra-legacy papers table
            return
        rows = self._conn.execute(
            "SELECT id, doi FROM papers "
            "WHERE doi IS NOT NULL AND doi != ''"
        ).fetchall()
        changed = False
        for r in rows:
            norm = normalize_doi(r["doi"])
            if norm and norm != r["doi"]:
                try:
                    self._conn.execute(
                        "UPDATE papers SET doi = ? WHERE id = ?",
                        (norm, r["id"]),
                    )
                    changed = True
                except sqlite3.IntegrityError:
                    pass
        if changed:
            self._conn.commit()

    # ── Knowledge Graph ────────────────────────────────────

    @_locked
    def get_knowledge_graph(
        self,
        *,
        tag_filter: list[str] | None = None,
        min_papers: int = 1,
    ) -> dict:
        """Return nodes and edges for knowledge graph visualization.

        Returns: {"nodes": [...], "edges": [...]}
        Nodes: {id, label, type, size, extra}
        Edges: {source, target, type, weight}
        """
        nodes = []
        edges = []
        node_ids = set()

        # 1. Paper nodes
        paper_rows = self._conn.execute(
            "SELECT id, title, year, venue FROM papers WHERE is_deleted = 0"
        ).fetchall()

        # Filter papers by tag if specified
        if tag_filter:
            placeholders = ",".join("?" for _ in tag_filter)
            paper_rows = self._conn.execute(
                f"""SELECT DISTINCT p.id, p.title, p.year, p.venue
                    FROM papers p
                    JOIN paper_tags pt ON pt.paper_id = p.id
                    JOIN tags t ON pt.tag_id = t.id
                    WHERE t.name IN ({placeholders}) AND p.is_deleted = 0""",
                tag_filter,
            ).fetchall()

        for r in paper_rows:
            label = (r["title"] or "Untitled")[:40]
            if len(r["title"] or "") > 40:
                label += "…"
            nodes.append({
                "id": f"paper:{r['id']}",
                "label": label,
                "type": "paper",
                "size": 10,
                "extra": {
                    "title": r["title"],
                    "year": r["year"],
                    "venue": r["venue"],
                    "paper_id": r["id"],
                },
            })
            node_ids.add(f"paper:{r['id']}")

        paper_id_list = [r["id"] for r in paper_rows]

        # 2. Tag nodes (connected to filtered papers)
        if paper_id_list:
            placeholders = ",".join("?" for _ in paper_id_list)
            tag_rows = self._conn.execute(
                f"""SELECT t.id, t.name, t.is_top,
                           COUNT(pt.paper_id) as paper_count
                    FROM tags t
                    JOIN paper_tags pt ON pt.tag_id = t.id
                    WHERE pt.paper_id IN ({placeholders})
                    GROUP BY t.id
                    HAVING paper_count >= ?
                    ORDER BY paper_count DESC""",
                paper_id_list + [min_papers],
            ).fetchall()

            for t in tag_rows:
                nodes.append({
                    "id": f"tag:{t['id']}",
                    "label": t["name"],
                    "type": "tag",
                    "size": min(5 + t["paper_count"] * 2, 30),
                    "extra": {
                        "name": t["name"],
                        "paper_count": t["paper_count"],
                        "is_top": bool(t["is_top"]),
                    },
                })
                node_ids.add(f"tag:{t['id']}")

            # 3. Paper-Tag edges
            pt_rows = self._conn.execute(
                f"""SELECT pt.paper_id, pt.tag_id
                    FROM paper_tags pt
                    WHERE pt.paper_id IN ({placeholders})""",
                paper_id_list,
            ).fetchall()
            for r in pt_rows:
                pid, tid = f"paper:{r['paper_id']}", f"tag:{r['tag_id']}"
                if pid in node_ids and tid in node_ids:
                    edges.append({
                        "source": pid, "target": tid,
                        "type": "tagged", "weight": 1,
                    })

        # 4. Author nodes
        if paper_id_list:
            placeholders = ",".join("?" for _ in paper_id_list)
            author_rows = self._conn.execute(
                f"""SELECT a.id, a.name, COUNT(pa.paper_id) as paper_count
                    FROM authors a
                    JOIN paper_authors pa ON pa.author_id = a.id
                    WHERE pa.paper_id IN ({placeholders})
                    GROUP BY a.id
                    HAVING paper_count >= ?
                    ORDER BY paper_count DESC
                    LIMIT 50""",
                paper_id_list + [min_papers],
            ).fetchall()

            for a in author_rows:
                nodes.append({
                    "id": f"author:{a['id']}",
                    "label": a["name"],
                    "type": "author",
                    "size": min(5 + a["paper_count"] * 3, 25),
                    "extra": {"name": a["name"], "paper_count": a["paper_count"]},
                })
                node_ids.add(f"author:{a['id']}")

            # 5. Paper-Author edges
            pa_rows = self._conn.execute(
                f"""SELECT pa.paper_id, pa.author_id
                    FROM paper_authors pa
                    WHERE pa.paper_id IN ({placeholders})""",
                paper_id_list,
            ).fetchall()
            for r in pa_rows:
                pid = f"paper:{r['paper_id']}"
                aid = f"author:{r['author_id']}"
                if pid in node_ids and aid in node_ids:
                    edges.append({
                        "source": pid, "target": aid,
                        "type": "authored", "weight": 1,
                    })

        # 6. Tag-Tag co-occurrence edges
        if len(tag_rows) > 1:
            tag_ids = [t["id"] for t in tag_rows]
            tag_placeholders = ",".join("?" for _ in tag_ids)
            cooc_rows = self._conn.execute(
                f"""SELECT pt1.tag_id as t1, pt2.tag_id as t2,
                       COUNT(*) as weight
                    FROM paper_tags pt1
                    JOIN paper_tags pt2 ON pt1.paper_id = pt2.paper_id
                    WHERE pt1.tag_id IN ({tag_placeholders})
                      AND pt2.tag_id IN ({tag_placeholders})
                      AND pt1.tag_id < pt2.tag_id
                    GROUP BY pt1.tag_id, pt2.tag_id
                    HAVING weight >= 2
                    ORDER BY weight DESC
                    LIMIT 100""",
                tag_ids + tag_ids,
            ).fetchall()
            for r in cooc_rows:
                n1, n2 = f"tag:{r['t1']}", f"tag:{r['t2']}"
                if n1 in node_ids and n2 in node_ids:
                    edges.append({
                        "source": n1, "target": n2,
                        "type": "cooccurrence", "weight": r["weight"],
                    })

        return {"nodes": nodes, "edges": edges}

    @_locked
    def list_papers(self) -> list[Paper]:
        rows = self._conn.execute(
            "SELECT * FROM papers WHERE is_deleted = 0 ORDER BY added_date DESC"
        ).fetchall()
        return [self._row_to_paper(r) for r in rows]

    @_locked
    def list_trash(self) -> list[Paper]:
        """Papers in the trash (soft-deleted), newest deletion first."""
        rows = self._conn.execute(
            "SELECT * FROM papers WHERE is_deleted = 1 "
            "ORDER BY deleted_date DESC, added_date DESC"
        ).fetchall()
        return [self._row_to_paper(r) for r in rows]

    @_locked
    def find_papers_by_tags(
        self, tag_names: Sequence[str], *, match_all: bool = True
    ) -> list[Paper]:
        """Filter papers by tag names (AND by default)."""
        if not tag_names:
            return self.list_papers()

        placeholders = ",".join("?" for _ in tag_names)
        having = (
            f"HAVING COUNT(DISTINCT t.name) = {len(tag_names)}"
            if match_all
            else ""
        )

        rows = self._conn.execute(
            f"""SELECT p.* FROM papers p
                JOIN paper_tags pt ON p.id = pt.paper_id
                JOIN tags t ON pt.tag_id = t.id
                WHERE t.name IN ({placeholders}) AND p.is_deleted = 0
                GROUP BY p.id
                {having}
                ORDER BY p.added_date DESC""",
            list(tag_names),
        ).fetchall()
        return [self._row_to_paper(r) for r in rows]

    @_locked
    def search_papers(self, query: str) -> list[Paper]:
        """Full-text search across title, abstract, and venue."""
        pattern = f"%{query}%"
        rows = self._conn.execute(
            """SELECT * FROM papers
               WHERE is_deleted = 0
                 AND (title LIKE ? OR abstract LIKE ? OR venue LIKE ?)
               ORDER BY added_date DESC""",
            (pattern, pattern, pattern),
        ).fetchall()
        return [self._row_to_paper(r) for r in rows]

    @_locked
    def delete_paper(self, paper_id: str) -> None:
        """Move a paper to the trash (soft delete)."""
        self._conn.execute(
            "UPDATE papers SET is_deleted = 1, deleted_date = date('now') "
            "WHERE id = ? AND is_deleted = 0",
            (paper_id,),
        )
        self._conn.commit()

    @_locked
    def restore_paper(self, paper_id: str) -> None:
        """Restore a paper from the trash."""
        self._conn.execute(
            "UPDATE papers SET is_deleted = 0, deleted_date = NULL WHERE id = ?",
            (paper_id,),
        )
        self._conn.commit()

    @_locked
    def purge_paper(self, paper_id: str) -> None:
        """Permanently delete a paper row (links cascade).

        pdf_text rows cascade via FK; the FTS mirror has no FK support
        and is cleared explicitly. Does not touch the stored PDF file.
        """
        self._conn.execute(
            "DELETE FROM pdf_fts WHERE paper_id = ?", (paper_id,)
        )
        self._conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
        self._conn.commit()

    @_locked
    def empty_trash(self) -> int:
        """Permanently delete all trashed papers. Returns count purged."""
        self._conn.execute(
            "DELETE FROM pdf_fts WHERE paper_id IN "
            "(SELECT id FROM papers WHERE is_deleted = 1)"
        )
        cur = self._conn.execute("DELETE FROM papers WHERE is_deleted = 1")
        self._conn.commit()
        return cur.rowcount

    # ── Notes ──────────────────────────────────────────────────

    @_locked
    def list_notes(self, paper_id: str) -> list[dict]:
        """All notes of a paper in insertion order (rowid beats the
        second-granularity created_at when notes land in the same second)."""
        rows = self._conn.execute(
            "SELECT id, content, created_at, updated_at FROM paper_notes "
            "WHERE paper_id = ? ORDER BY rowid",
            (paper_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def add_note(self, paper_id: str, content: str) -> dict:
        note_id = _new_id()
        self._conn.execute(
            "INSERT INTO paper_notes (id, paper_id, content) VALUES (?, ?, ?)",
            (note_id, paper_id, content),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id, content, created_at, updated_at FROM paper_notes "
            "WHERE id = ?",
            (note_id,),
        ).fetchone()
        return dict(row)

    @_locked
    def update_note(self, note_id: str, content: str) -> None:
        self._conn.execute(
            "UPDATE paper_notes SET content = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (content, note_id),
        )
        self._conn.commit()

    @_locked
    def delete_note(self, note_id: str) -> None:
        self._conn.execute("DELETE FROM paper_notes WHERE id = ?", (note_id,))
        self._conn.commit()

    @_locked
    def set_paper_authors(self, paper_id: str, authors: Sequence[Author]) -> None:
        """Replace a paper's author list (used by the detail editor)."""
        self._conn.execute(
            "DELETE FROM paper_authors WHERE paper_id = ?", (paper_id,)
        )
        for pos, author in enumerate(authors):
            aid = self._upsert_author(author)
            self._conn.execute(
                "INSERT OR IGNORE INTO paper_authors "
                "(paper_id, author_id, position) VALUES (?, ?, ?)",
                (paper_id, aid, pos),
            )
        self._conn.commit()

    @_locked
    def update_paper_pdf(self, paper_id: str, pdf_path: str) -> None:
        # Clearing the path also clears the fingerprint — the invariant
        # "fingerprint set ⟺ a stored file is linked" keeps exact-file
        # dedup from refusing to re-attach a dropped duplicate
        self._conn.execute(
            """UPDATE papers
               SET pdf_path = ?,
                   pdf_fingerprint = CASE WHEN ? = '' THEN NULL
                                          ELSE pdf_fingerprint END
               WHERE id = ?""",
            (pdf_path, pdf_path, paper_id),
        )
        self._conn.commit()

    @_locked
    def add_tag_to_paper(
        self, paper_id: str, tag_name: str, source: str = "manual"
    ) -> None:
        self._link_tag_by_name(paper_id, tag_name, source=source)
        self._conn.commit()

    @_locked
    def mark_tag_source(
        self, paper_id: str, tag_names: Sequence[str], source: str
    ) -> int:
        """Set source on a paper's existing links. Returns rows changed."""
        if not tag_names:
            return 0
        placeholders = ",".join("?" for _ in tag_names)
        cur = self._conn.execute(
            f"""UPDATE paper_tags SET source = ?
                WHERE paper_id = ? AND tag_id IN (
                    SELECT id FROM tags WHERE name IN ({placeholders}))""",
            (source, paper_id, *tag_names),
        )
        self._conn.commit()
        return cur.rowcount

    @_locked
    def remove_tag_from_paper(self, paper_id: str, tag_name: str) -> None:
        tag = self.get_tag_by_name(tag_name)
        if tag:
            self._conn.execute(
                "DELETE FROM paper_tags WHERE paper_id = ? AND tag_id = ?",
                (paper_id, tag.id),
            )
            self._conn.commit()

    # ── PDF full-text index (per-page) ─────────────────────

    @_locked
    def upsert_pdf_text(
        self, paper_id: str, page_count: int, blob: bytes
    ) -> None:
        """Store the authoritative compressed per-page text of a paper."""
        self._conn.execute(
            "INSERT OR REPLACE INTO pdf_text (paper_id, page_count, pages) "
            "VALUES (?, ?, ?)",
            (paper_id, page_count, blob),
        )
        self._conn.commit()

    @_locked
    def get_pdf_text(self, paper_id: str) -> tuple[int, bytes] | None:
        """(page_count, compressed pages blob) or None if not indexed."""
        row = self._conn.execute(
            "SELECT page_count, pages FROM pdf_text WHERE paper_id = ?",
            (paper_id,),
        ).fetchone()
        return (row["page_count"], row["pages"]) if row else None

    @_locked
    def list_pdf_texts(self) -> list[tuple[str, bytes]]:
        """All indexed live papers as (paper_id, compressed blob)."""
        rows = self._conn.execute(
            """SELECT t.paper_id, t.pages FROM pdf_text t
               JOIN papers p ON p.id = t.paper_id AND p.is_deleted = 0"""
        ).fetchall()
        return [(r["paper_id"], r["pages"]) for r in rows]

    @_locked
    def papers_missing_index(self) -> list[str]:
        """Live papers with a stored PDF but no full-text index yet."""
        rows = self._conn.execute(
            """SELECT p.id FROM papers p
               WHERE p.is_deleted = 0 AND p.pdf_path IS NOT NULL
                 AND p.pdf_path != ''
                 AND NOT EXISTS (SELECT 1 FROM pdf_text t WHERE t.paper_id = p.id)
               ORDER BY p.added_date DESC"""
        ).fetchall()
        return [r["id"] for r in rows]

    @_locked
    def replace_fts_rows(self, paper_id: str, page_texts: Sequence[str]) -> None:
        """Mirror the per-page texts into the FTS table (1-based pages)."""
        self._conn.execute(
            "DELETE FROM pdf_fts WHERE paper_id = ?", (paper_id,)
        )
        self._conn.executemany(
            "INSERT INTO pdf_fts (paper_id, page, text) VALUES (?, ?, ?)",
            [
                (paper_id, i, text)
                for i, text in enumerate(page_texts, start=1)
            ],
        )
        self._conn.commit()

    @_locked
    def fts_search(self, phrase: str, *, limit: int = 50) -> list[dict]:
        """Match an FTS phrase (>=3 chars) against indexed pages.

        Returns [{paper_id, page, snippet}] for live papers only.
        """
        rows = self._conn.execute(
            """SELECT f.paper_id, f.page,
                      snippet(pdf_fts, 2, '[', ']', '…', 16) AS snippet
               FROM pdf_fts f
               JOIN papers p ON p.id = f.paper_id AND p.is_deleted = 0
               WHERE pdf_fts MATCH ?
               LIMIT ?""",
            (phrase, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Conversations & Messages ──────────────────────────────

    @_locked
    def create_conversation(self, paper_id: str) -> str:
        conv_id = _new_id()
        self._conn.execute(
            "INSERT INTO conversations (id, paper_id) VALUES (?, ?)",
            (conv_id, paper_id),
        )
        self._conn.commit()
        return conv_id

    @_locked
    def get_conversation(self, paper_id: str) -> str | None:
        """Return the latest conversation ID for a paper, or None."""
        row = self._conn.execute(
            "SELECT id FROM conversations WHERE paper_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (paper_id,),
        ).fetchone()
        return row["id"] if row else None

    @_locked
    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
    ) -> None:
        if role not in ("user", "assistant", "tool"):
            raise ValueError(f"invalid message role: {role!r}")
        self._conn.execute(
            "INSERT INTO messages "
            "(id, conversation_id, role, content, tool_call_id, tool_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                _new_id(),
                conversation_id,
                role,
                content,
                tool_call_id,
                tool_name,
            ),
        )
        self._conn.commit()

    @_locked
    def get_messages(self, conversation_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT role, content, tool_call_id, tool_name, created_at "
            "FROM messages WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Internal helpers ──────────────────────────────────────

    def _upsert_author(self, author: Author) -> str:
        """Return the author's id, inserting only if truly new.

        Matches by ORCID first, then by full name so the same person
        resolves to one row (keeps knowledge-graph author nodes unique).
        """
        if author.orcid:
            row = self._conn.execute(
                "SELECT id FROM authors WHERE orcid = ?", (author.orcid,)
            ).fetchone()
            if row:
                return row["id"]

        row = self._conn.execute(
            "SELECT id FROM authors WHERE name = ?", (author.name,)
        ).fetchone()
        if row:
            return row["id"]

        aid = _new_id()
        self._conn.execute(
            "INSERT OR IGNORE INTO authors "
            "(id, name, first_name, last_name, affiliation, orcid, email) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                aid,
                author.name,
                author.first_name,
                author.last_name,
                author.affiliation,
                author.orcid,
                author.email,
            ),
        )
        return aid

    @_locked
    def get_papers_with_dois(self) -> list[tuple[str, str]]:
        """(paper_id, doi) for every live paper that has a DOI."""
        rows = self._conn.execute(
            "SELECT id, doi FROM papers "
            "WHERE is_deleted = 0 AND doi IS NOT NULL AND doi != ''"
        ).fetchall()
        return [(r["id"], r["doi"]) for r in rows]

    @_locked
    def set_paper_author_affiliations(
        self, paper_id: str, affiliations: dict[str, str]
    ) -> int:
        """Fill author affiliations for one paper, matched by author name.

        Only fills authors whose affiliation is still empty — user edits
        are never overwritten. Returns the number of authors updated.
        """
        updated = 0
        for name, aff in affiliations.items():
            aff = (aff or "").strip()
            if not aff:
                continue
            cur = self._conn.execute(
                """UPDATE authors SET affiliation = ?
                   WHERE (affiliation IS NULL OR affiliation = '')
                     AND id IN (
                       SELECT author_id FROM paper_authors WHERE paper_id = ?
                     )
                     AND name = ?""",
                (aff, paper_id, name),
            )
            updated += cur.rowcount
        self._conn.commit()
        return updated

    def _link_tag_by_name(
        self, paper_id: str, tag_name: str, source: str = "manual"
    ) -> None:
        """Ensure tag exists (normalized) and link it to a paper."""
        norm = _normalize_tag_name(tag_name)
        if not norm:
            return
        tag = self.get_tag_by_name(norm)
        if not tag:
            tag = self.create_tag(norm)
        self._conn.execute(
            "INSERT OR IGNORE INTO paper_tags (paper_id, tag_id, source) "
            "VALUES (?, ?, ?)",
            (paper_id, tag.id, source),
        )

    @_locked
    def merge_duplicate_tags(self) -> int:
        """Merge tags that are identical after normalization. Returns merge count."""
        rows = self._conn.execute("SELECT id, name FROM tags").fetchall()
        # Group by normalized name
        groups: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            norm = _normalize_tag_name(r["name"])
            groups.setdefault(norm, []).append(r)

        merged = 0
        for _norm, members in groups.items():
            if len(members) <= 1:
                continue
            # Keep the first (or the one that's already normalized), remove others
            keeper = members[0]
            for dup in members[1:]:
                # Re-link all papers from dup → keeper
                self._conn.execute(
                    "INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) "
                    "SELECT paper_id, ? FROM paper_tags WHERE tag_id = ?",
                    (keeper["id"], dup["id"]),
                )
                # Remove dup links and the tag itself
                self._conn.execute(
                    "DELETE FROM paper_tags WHERE tag_id = ?", (dup["id"],)
                )
                self._conn.execute(
                    "DELETE FROM tags WHERE id = ?", (dup["id"],)
                )
                merged += 1
        self._conn.commit()
        return merged

    def _row_to_paper(self, row: sqlite3.Row) -> Paper:
        d = dict(row)
        d["keywords"] = json.loads(d.get("keywords", "[]"))

        # Load tags, split by source
        tag_rows = self._conn.execute(
            "SELECT t.name, pt.source FROM tags t "
            "JOIN paper_tags pt ON t.id = pt.tag_id "
            "WHERE pt.paper_id = ?",
            (d["id"],),
        ).fetchall()
        d["tags"] = [r["name"] for r in tag_rows if r["source"] == "manual"]
        d["auto_tags"] = [r["name"] for r in tag_rows if r["source"] == "auto"]

        # Load authors
        author_rows = self._conn.execute(
            "SELECT a.* FROM authors a "
            "JOIN paper_authors pa ON a.id = pa.author_id "
            "WHERE pa.paper_id = ? ORDER BY pa.position",
            (d["id"],),
        ).fetchall()
        d["authors"] = [
            Author(
                name=r["name"],
                first_name=r["first_name"] or "",
                last_name=r["last_name"] or "",
                affiliation=r["affiliation"],
                orcid=r["orcid"],
                email=r["email"],
            )
            for r in author_rows
        ]

        return Paper(**d)
