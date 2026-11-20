"""Shared SQLite connection, schema and version resolution helpers."""
from datetime import datetime, timezone
import sqlite3

SCHEMA_VERSION = '3'

# Version 2 archives are the same chapter/version archive without the author
# catalogue; they stay readable and gain the catalogue tables when first needed.
SUPPORTED_SCHEMAS = ('2', '3')

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
    # Author catalogue: canonical identities are independent of source
    # declarations. An identity's display name is its canonical name; aliases
    # add further exact full names. The same text may map to several identities
    # (a conflict) so no unique constraint is placed on the alias text alone.
    """CREATE TABLE authors(
        id INTEGER PRIMARY KEY,
        display_name TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE author_aliases(
        id INTEGER PRIMARY KEY,
        author_id INTEGER NOT NULL REFERENCES authors(id),
        alias TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(author_id, alias)
    )""",
    # One row per OPF creator/contributor declaration per book version. The
    # source name is never rewritten and declarations are never merged across
    # versions; ordinal preserves the declaration order within its category.
    """CREATE TABLE contributor_declarations(
        id INTEGER PRIMARY KEY,
        version_id INTEGER NOT NULL REFERENCES versions(id),
        category TEXT NOT NULL CHECK(category IN ('creator','contributor')),
        ordinal INTEGER NOT NULL,
        name TEXT NOT NULL,
        role TEXT,
        attributes TEXT NOT NULL,
        UNIQUE(version_id, category, ordinal)
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
    if row[0] not in SUPPORTED_SCHEMAS:
        raise ValueError('unsupported archive schema version: ' + row[0])
    return row[0]


# Catalogue tables added in schema 3. An archive imported by an earlier build
# has no author catalogue; creating these tables in place lets existing sources
# be queried without a re-import while keeping every archived version.
_CATALOGUE_TABLES = ('authors', 'author_aliases', 'contributor_declarations')


def ensure_catalogue_schema(conn, create=False):
    """Create any missing author-catalogue tables for an older archive.

    Existing tables and archived rows are untouched. When ``create`` is false
    nothing is written, so read-only queries never modify an archive. Returns
    True when the complete catalogue schema is available afterwards.
    """
    existing = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [name for name in _CATALOGUE_TABLES if name not in existing]
    if not missing:
        return True
    if not create:
        return False
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute('BEGIN')
    try:
        for name in missing:
            conn.execute(next(statement for statement in DDL
                              if statement.startswith('CREATE TABLE ' + name + '(')))
        if own_transaction:
            conn.commit()
    except Exception:
        if own_transaction:
            conn.rollback()
        raise
    return True


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
