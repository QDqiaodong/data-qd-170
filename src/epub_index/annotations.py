"""Reader annotation anchors, per-version relocation and pending list."""
from .db import (
    check_schema, get_book, open_connection, resolve_version, utc_now,
)

CONTEXT_CHARS = 32
EXCERPT_LIMIT = 2000
NOTE_LIMIT = 10_000


def _context(text, start, end):
    """At most 32 Unicode characters immediately before/after the excerpt."""
    return text[max(0, start - CONTEXT_CHARS):start], text[end:end + CONTEXT_CHARS]


def find_candidates(text, excerpt, prefix, suffix):
    """Exact occurrences whose saved non-empty prefix or suffix still borders it."""
    candidates = []
    start = text.find(excerpt)
    while start != -1:
        end = start + len(excerpt)
        if (prefix and start >= len(prefix) and text[start - len(prefix):start] == prefix) or (
                suffix and text[end:end + len(suffix)] == suffix):
            candidates.append(start)
        start = text.find(excerpt, end)
    return candidates


def relocate(chapters, excerpt, prefix, suffix):
    """Classify one annotation against one version's chapters.

    chapters: list of (position, text). Returns (status, position, offset).
    """
    found = []
    for position, text in chapters:
        for offset in find_candidates(text, excerpt, prefix, suffix):
            found.append((position, offset))
    if len(found) == 1:
        position, offset = found[0]
        return 'located', position, offset
    if len(found) > 1:
        return 'ambiguous', None, None
    return 'missing', None, None


def add_annotation(database, position, excerpt, note, version=None):
    """Anchor an annotation to an exact raw-text excerpt of one chapter."""
    if type(position) is not int or position < 1:
        raise ValueError('chapter position must be a positive integer')
    if not isinstance(excerpt, str) or not excerpt or len(excerpt) > EXCERPT_LIMIT:
        raise ValueError('nonempty exact excerpt required')
    if not isinstance(note, str) or not note or len(note) > NOTE_LIMIT:
        raise ValueError('nonempty note required')
    conn = open_connection(database)
    try:
        check_schema(conn)
        book_id, _ = get_book(conn)
        version_id = resolve_version(conn, book_id, version)
        row = conn.execute(
            'SELECT href, text FROM chapters WHERE version_id=? AND position=?',
            (version_id, position)).fetchone()
        if row is None:
            raise ValueError('chapter not found in selected version')
        href, text = row
        occurrences = []
        index = text.find(excerpt)
        while index != -1:
            occurrences.append(index)
            index = text.find(excerpt, index + len(excerpt))
        if not occurrences:
            raise ValueError('excerpt is not part of the chapter text')
        if len(occurrences) > 1:
            raise ValueError('excerpt occurs more than once in the chapter')
        offset = occurrences[0]
        prefix, suffix = _context(text, offset, offset + len(excerpt))
        conn.execute('BEGIN')
        cursor = conn.execute(
            '''INSERT INTO annotations(
                   book_id, source_version_id, chapter_position, chapter_href,
                   excerpt, prefix, suffix, note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (book_id, version_id, position, href, excerpt, prefix, suffix, note, utc_now()))
        annotation_id = cursor.lastrowid
        conn.execute(
            '''INSERT INTO annotation_locations(
                   annotation_id, version_id, status, chapter_position, excerpt_offset)
               VALUES (?,?, 'located', ?, ?)''',
            (annotation_id, version_id, position, offset))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        'annotation_id': annotation_id,
        'chapter_position': position,
        'excerpt': excerpt,
        'note': note,
        'offset': offset,
        'version_id': version_id,
    }


def relocate_for_version(conn, book_id, new_version_id):
    """Drive every existing annotation to located/ambiguous/missing on a new version."""
    chapter_rows = conn.execute(
        'SELECT position, text FROM chapters WHERE version_id=? ORDER BY position',
        (new_version_id,)).fetchall()
    annotations = conn.execute(
        'SELECT id, excerpt, prefix, suffix FROM annotations WHERE book_id=? ORDER BY id',
        (book_id,)).fetchall()
    for annotation_id, excerpt, prefix, suffix in annotations:
        status, position, offset = relocate(chapter_rows, excerpt, prefix, suffix)
        conn.execute(
            '''INSERT INTO annotation_locations(
                   annotation_id, version_id, status, chapter_position, excerpt_offset)
               VALUES (?,?,?,?,?)''',
            (annotation_id, new_version_id, status, position, offset))


def pending(database, version=None):
    """Ambiguous/missing annotations for a version, with original anchors preserved."""
    conn = open_connection(database)
    try:
        check_schema(conn)
        book_id, _ = get_book(conn)
        version_id = resolve_version(conn, book_id, version)
        rows = conn.execute(
            '''SELECT a.id, a.source_version_id, sv.source_sha256, a.chapter_position,
                      a.chapter_href, a.excerpt, a.prefix, a.suffix, a.note, al.status,
                      v.source_sha256
               FROM annotation_locations al
               JOIN annotations a ON a.id=al.annotation_id
               JOIN versions v ON v.id=al.version_id
               JOIN versions sv ON sv.id=a.source_version_id
               WHERE al.version_id=? AND al.status IN ('ambiguous','missing')
               ORDER BY a.id''',
            (version_id,)).fetchall()
        result = []
        for (annotation_id, source_version_id, source_sha, chapter_position,
             chapter_href, excerpt, prefix, suffix, note, status, version_sha) in rows:
            item = {
                'annotation_id': annotation_id,
                'status': status,
                'version_id': version_id,
                'version_sha256': version_sha,
                'source_version_id': source_version_id,
                'source_version_sha256': source_sha,
                'source_chapter_position': chapter_position,
                'source_chapter_href': chapter_href,
                'excerpt': excerpt,
                'prefix': prefix,
                'suffix': suffix,
                'note': note,
            }
            if status == 'ambiguous':
                item['candidates'] = _candidate_list(conn, version_id, excerpt, prefix, suffix)
            result.append(item)
        return result
    finally:
        conn.close()


def _candidate_list(conn, version_id, excerpt, prefix, suffix):
    candidates = []
    for position, text in conn.execute(
            'SELECT position, text FROM chapters WHERE version_id=? ORDER BY position',
            (version_id,)):
        for offset in find_candidates(text, excerpt, prefix, suffix):
            candidates.append({'chapter_position': position, 'excerpt_offset': offset})
    return candidates
