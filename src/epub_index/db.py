"""Shared SQLite connection, schema and version resolution helpers."""
from datetime import datetime, timezone
import sqlite3

SCHEMA_VERSION = '2'

# Separate statements so each runs inside the caller's explicit transaction;
# executescript() would issue an implicit COMMIT and break rollback atomicity.
DDL = (
    """CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE book(
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        identifier TEXT NOT NULL UNIQUE,
        current_version_id INTEGER
    )""",
    """CREATE TABLE versions(
        id INTEGER PRIMARY KEY,
        book_id INTEGER NOT NULL REFERENCES book(id),
        source_sha256 TEXT NOT NULL,
        imported_at TEXT NOT NULL,
        UNIQUE(book_id, source_sha256)
    )""",
    """CREATE TABLE chapters(
        version_id INTEGER NOT NULL REFERENCES versions(id),
        position INTEGER NOT NULL,
        href TEXT NOT NULL,
        title TEXT NOT NULL,
        text TEXT NOT NULL,
        PRIMARY KEY(version_id, position),
        UNIQUE(version_id, href)
    )""",
    """CREATE VIRTUAL TABLE search USING fts5(
        title,
        text,
        version_id UNINDEXED,
        chapter_position UNINDEXED,
        tokenize='unicode61'
    )""",
    """CREATE TABLE annotations(
        id INTEGER PRIMARY KEY,
        book_id INTEGER NOT NULL REFERENCES book(id),
        source_version_id INTEGER NOT NULL REFERENCES versions(id),
        chapter_position INTEGER NOT NULL,
        chapter_href TEXT NOT NULL,
        excerpt TEXT NOT NULL,
        prefix TEXT NOT NULL,
        suffix TEXT NOT NULL,
        note TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE annotation_locations(
        annotation_id INTEGER NOT NULL REFERENCES annotations(id),
        version_id INTEGER NOT NULL REFERENCES versions(id),
        status TEXT NOT NULL CHECK(status IN ('located','ambiguous','missing')),
        chapter_position INTEGER,
        excerpt_offset INTEGER,
        PRIMARY KEY(annotation_id, version_id),
        CHECK(
            (status='located' AND chapter_position IS NOT NULL AND excerpt_offset IS NOT NULL)
            OR
            (status<>'located' AND chapter_position IS NULL AND excerpt_offset IS NULL)
        )
    )""",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def open_connection(database):
    """Manual-transaction connection so DDL/DML commit or rollback together."""
    conn = sqlite3.connect(database)
    conn.isolation_level = None
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def check_schema(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        raise ValueError('unsupported archive schema')
    if row[0] != SCHEMA_VERSION:
        raise ValueError('unsupported archive schema version: ' + row[0])
    return row[0]


def get_book(conn):
    row = conn.execute('SELECT id, current_version_id FROM book').fetchone()
    if row is None:
        raise ValueError('archive contains no book')
    return row[0], row[1]


def resolve_version(conn, book_id, version):
    """Resolve None (current), integer version id or SHA-256 string to a version id."""
    if version is None:
        row = conn.execute(
            'SELECT current_version_id FROM book WHERE id=?', (book_id,)).fetchone()
        if row is None or row[0] is None:
            raise ValueError('no imported version')
        return row[0]
    if type(version) is int:
        row = conn.execute(
            'SELECT id FROM versions WHERE book_id=? AND id=?', (book_id, version)).fetchone()
    elif isinstance(version, str) and version.strip():
        row = conn.execute(
            'SELECT id FROM versions WHERE book_id=? AND source_sha256=?',
            (book_id, version)).fetchone()
    else:
        raise ValueError('invalid version selector')
    if row is None:
        raise ValueError('unknown version')
    return row[0]
