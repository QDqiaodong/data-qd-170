"""Versioned SQLite chapter archive and literal-token FTS5 search."""
import json
from pathlib import Path
import re
import sqlite3
from .annotations import relocate_for_version
from .db import (
    DDL, SCHEMA_VERSION, check_schema, ensure_catalogue_schema, get_book,
    open_connection, resolve_version, utc_now,
)
from .extract import extract

def ingest(source, database):
    """Import an EPUB as a new version, or idempotently accept a repeated SHA.

    First import requires a database that does not exist. A new SHA for the same
    package identifier is fully extracted and saved as a new version inside one
    transaction; the current version only advances after successful commit.
    """
    book = extract(source)
    conn = open_connection(database)
    try:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if not existing:
            conn.execute('BEGIN')
            for statement in DDL:
                conn.execute(statement)
            conn.execute('INSERT INTO meta(key,value) VALUES (?,?)',
                         ('schema_version', SCHEMA_VERSION))
            cursor = conn.execute(
                'INSERT INTO book(title,identifier,current_version_id) VALUES (?,?,?)',
                (book['title'], book['identifier'], None))
            book_id = cursor.lastrowid
            version_id = _save_version(conn, book_id, book, make_current=True)
            # The first version owns each annotation's initial, located anchor.
            relocate_for_version(conn, book_id, version_id)
            conn.commit()
            action = 'imported'
        else:
            check_schema(conn)
            row = conn.execute('SELECT id, identifier FROM book').fetchall()
            if len(row) != 1:
                raise ValueError('archive must hold exactly one book')
            book_id, identifier = row[0]
            if identifier != book['identifier']:
                raise ValueError(
                    'package identifier conflict: archive already holds another book')
            match = conn.execute(
                'SELECT id FROM versions WHERE book_id=? AND source_sha256=?',
                (book_id, book['source_sha256'])).fetchone()
            if match:
                # Repeated import of the identical source is idempotent.
                conn.rollback()
                action = 'unchanged'
                version_id = match[0]
            else:
                conn.execute('BEGIN')
                ensure_catalogue_schema(conn, create=True)
                version_id = _save_version(conn, book_id, book, make_current=True)
                relocate_for_version(conn, book_id, version_id)
                conn.commit()
                action = 'imported'
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        'action': action,
        'title': book['title'],
        'identifier': book['identifier'],
        'chapter_count': len(book['chapters']),
        'source_sha256': book['source_sha256'],
        'version_id': version_id,
    }


def _save_version(conn, book_id, book, make_current):
    cursor = conn.execute(
        'INSERT INTO versions(book_id,source_sha256,imported_at) VALUES (?,?,?)',
        (book_id, book['source_sha256'], utc_now()))
    version_id = cursor.lastrowid
    for chapter in book['chapters']:
        conn.execute(
            'INSERT INTO chapters(version_id,position,href,title,text) VALUES (?,?,?,?,?)',
            (version_id, chapter['position'], chapter['href'],
             chapter['title'], chapter['text']))
        conn.execute(
            'INSERT INTO search(title,text,version_id,chapter_position) VALUES (?,?,?,?)',
            (chapter['title'], chapter['text'], version_id, chapter['position']))
    for declaration in book.get('contributors', ()):
        conn.execute(
            '''INSERT INTO contributor_declarations(
                   version_id, category, ordinal, name, role, attributes)
               VALUES (?,?,?,?,?,?)''',
            (version_id, declaration['category'], declaration['ordinal'],
             declaration['name'], declaration['role'],
             json.dumps(declaration['attributes'], ensure_ascii=False,
                        sort_keys=True)))
    if make_current:
        conn.execute('UPDATE book SET current_version_id=? WHERE id=?',
                     (version_id, book_id))
    return version_id


def list_versions(database):
    conn = open_connection(database)
    try:
        check_schema(conn)
        book_id, current_id = get_book(conn)
        rows = conn.execute(
            '''SELECT v.id, v.source_sha256, v.imported_at,
                      (SELECT COUNT(*) FROM chapters c WHERE c.version_id=v.id)
               FROM versions v WHERE v.book_id=? ORDER BY v.id''',
            (book_id,)).fetchall()
        return [{
            'version_id': vid,
            'source_sha256': sha,
            'imported_at': imported,
            'chapter_count': count,
            'current': vid == current_id,
        } for vid, sha, imported, count in rows]
    finally:
        conn.close()


def select_version(database, version):
    conn = open_connection(database)
    try:
        check_schema(conn)
        book_id, _ = get_book(conn)
        version_id = resolve_version(conn, book_id, version)
        conn.execute('BEGIN')
        conn.execute('UPDATE book SET current_version_id=? WHERE id=?',
                     (version_id, book_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {'version_id': version_id, 'current': True}


def search(database, query, version=None):
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise ValueError('bounded nonempty query required')
    tokens = re.findall(r'[^\W_]+', query, flags=re.UNICODE)
    if not tokens:
        raise ValueError('query contains no searchable tokens')
    expression = ' AND '.join('"' + token + '"' for token in tokens)
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as conn:
        row = conn.execute('SELECT id, current_version_id FROM book').fetchone()
        if row is None:
            raise ValueError('archive contains no book')
        book_id = row[0]
        version_id = resolve_version(conn, book_id, version)
        hits = [{'position': r[0], 'href': r[1], 'title': r[2], 'snippet': r[3]}
                for r in conn.execute(
                    '''SELECT c.position, c.href, c.title,
                              snippet(search,1,'[',']','…',16)
                       FROM search JOIN chapters c
                         ON c.version_id=search.version_id
                        AND c.position=search.chapter_position
                       WHERE search MATCH ? AND search.version_id=?
                       ORDER BY c.position LIMIT 100''',
                    (expression, version_id))]
        for hit in hits:
            hit['annotations'] = [{
                'annotation_id': r[0],
                'excerpt': r[1],
                'note': r[2],
                'excerpt_offset': r[3],
            } for r in conn.execute(
                '''SELECT a.id, a.excerpt, a.note, al.excerpt_offset
                   FROM annotation_locations al
                   JOIN annotations a ON a.id=al.annotation_id
                   WHERE al.version_id=? AND al.status='located'
                     AND al.chapter_position=?
                   ORDER BY al.excerpt_offset, a.id''',
                (version_id, hit['position']))]
        return hits
