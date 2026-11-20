"""Author identity catalogue and source-declaration resolution.

Source declarations are stored verbatim per book version at import time. The
catalogue never rewrites them: identities and aliases live in their own tables
and every query reads the *current* alias mapping, so adding or revoking an
alias changes resolution immediately without touching imported versions.

Names map exactly: an identity's canonical display name and each of its
explicit aliases are exact full-name mappings, with no case folding or fuzzy
matching. A declaration is resolved only when exactly *one* identity owns its
name; zero owners and multiple owners stay unresolved and queryable. Two
identities may share a display name; that shared name then owns no single
identity, so declarations using it are ambiguous rather than auto-merged.

A catalogue lives inside one archive. Queries may span several archives; the
archive that owns the identities and aliases is the catalogue, and declarations
from every named archive are resolved against it.
"""
import json
from pathlib import Path

from .db import (
    check_schema, ensure_catalogue_schema, get_book, open_connection,
    resolve_version, utc_now,
)

NAME_LIMIT = 200


def _name(value, label):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > NAME_LIMIT or any(ord(c) < 32 for c in value)):
        raise ValueError('invalid ' + label)
    return value


def _attributes(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _connect(database):
    """Open an existing archive without ever creating an empty database file."""
    path = Path(database)
    if path.is_symlink() or not path.is_file():
        raise ValueError('archive does not exist')
    return open_connection(database)


def _catalogue_ready(conn):
    """Validate one archive and report whether its catalogue tables exist.

    Reads never create tables: an archive imported before the catalogue existed
    reports no identities or declarations until a catalogue write or a new
    version import upgrades it inside a transaction.
    """
    check_schema(conn)
    available = ensure_catalogue_schema(conn, create=False)
    get_book(conn)
    return available


def _write_begin(conn):
    """Validate, open a transaction and create catalogue tables if missing."""
    check_schema(conn)
    conn.execute('BEGIN')
    ensure_catalogue_schema(conn, create=True)


def _author(conn, author_id):
    row = conn.execute(
        'SELECT id, display_name, created_at FROM authors WHERE id=?',
        (author_id,)).fetchone()
    if row is None:
        raise ValueError('unknown author identity')
    return row


def _alias_index(conn):
    """Exact declared name -> set of identity ids currently mapping it.

    An identity's canonical display name always counts as one of its names, so
    a declaration using the canonical name resolves without an extra alias.
    Explicit aliases add further names. The same text may map to several
    identities, which is how a conflict is represented.
    """
    mapping = {}
    for display_name, author_id in conn.execute(
            'SELECT display_name, id FROM authors ORDER BY id'):
        mapping.setdefault(display_name, set()).add(author_id)
    for alias, author_id in conn.execute(
            'SELECT alias, author_id FROM author_aliases ORDER BY id'):
        mapping.setdefault(alias, set()).add(author_id)
    return mapping


def resolve_name(alias_index, name):
    """Return (author_id, status) for one exact declared name.

    status is 'resolved', 'unmapped' or 'ambiguous'. Only a single identity
    makes a name resolvable; a name shared by several identities belongs to
    nobody, and no fuzzy matching or automatic merge is attempted.
    """
    identities = alias_index.get(name, set())
    if len(identities) == 1:
        return next(iter(identities)), 'resolved'
    if not identities:
        return None, 'unmapped'
    return None, 'ambiguous'


def _author_record(conn, row):
    author_id, display_name, created_at = row
    aliases = [{
        'alias_id': alias_id,
        'alias': alias,
    } for alias_id, alias in conn.execute(
        'SELECT id, alias FROM author_aliases WHERE author_id=? ORDER BY id',
        (author_id,))]
    return {
        'author_id': author_id,
        'display_name': display_name,
        'created_at': created_at,
        'aliases': aliases,
    }


def create_author(database, display_name):
    """Create an independent canonical identity. Display names may repeat."""
    name = _name(display_name, 'author display name')
    conn = _connect(database)
    try:
        _write_begin(conn)
        cursor = conn.execute(
            'INSERT INTO authors(display_name, created_at) VALUES (?,?)',
            (name, utc_now()))
        author_id = cursor.lastrowid
        conn.commit()
        return _author_record(conn, _author(conn, author_id))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_alias(database, author_id, alias):
    """Map one exact full name to one identity; creates no implicit merges."""
    alias = _name(alias, 'alias')
    if type(author_id) is not int:
        raise ValueError('invalid author identity')
    conn = _connect(database)
    try:
        _write_begin(conn)
        display_name = _author(conn, author_id)[1]
        if alias == display_name:
            raise ValueError(
                'alias duplicates the identity display name; it already matches')
        existing = conn.execute(
            'SELECT id FROM author_aliases WHERE author_id=? AND alias=?',
            (author_id, alias)).fetchone()
        if existing:
            action, alias_id = 'unchanged', existing[0]
            conn.commit()
        else:
            cursor = conn.execute(
                'INSERT INTO author_aliases(author_id, alias, created_at) VALUES (?,?,?)',
                (author_id, alias, utc_now()))
            alias_id = cursor.lastrowid
            conn.commit()
            action = 'added'
        return {
            'action': action,
            'alias_id': alias_id,
            'alias': alias,
            'author_id': author_id,
            # Two or more candidates mean this alias is currently conflicted.
            'candidates': sorted(_alias_index(conn).get(alias, set())),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def remove_alias(database, author_id, alias):
    """Revoke one identity's mapping for an exact full name.

    Source declarations are untouched; after revocation matching declarations
    are unresolved again. Returns the number of mappings removed (0 when the
    identity never owned that alias).
    """
    alias = _name(alias, 'alias')
    if type(author_id) is not int:
        raise ValueError('invalid author identity')
    conn = _connect(database)
    try:
        _write_begin(conn)
        _author(conn, author_id)
        cursor = conn.execute(
            'DELETE FROM author_aliases WHERE author_id=? AND alias=?',
            (author_id, alias))
        conn.commit()
        return {
            'action': 'removed' if cursor.rowcount else 'absent',
            'alias': alias,
            'author_id': author_id,
            'removed': cursor.rowcount,
            'candidates': sorted(_alias_index(conn).get(alias, set())),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_authors(database):
    """Every canonical identity in one catalogue archive with its aliases."""
    conn = _connect(database)
    try:
        if not _catalogue_ready(conn):
            return []
        return [_author_record(conn, row) for row in conn.execute(
            'SELECT id, display_name, created_at FROM authors ORDER BY id')]
    finally:
        conn.close()


def _declaration_rows(conn, version_id=None):
    """All source declarations in OPF document order (id follows insertion)."""
    sql = '''SELECT d.id, d.version_id, d.category, d.ordinal, d.name, d.role,
                    d.attributes, v.source_sha256, v.imported_at,
                    b.id, b.title, b.identifier, b.current_version_id
             FROM contributor_declarations d
             JOIN versions v ON v.id=d.version_id
             JOIN book b ON b.id=v.book_id'''
    if version_id is None:
        return conn.execute(sql + ' ORDER BY v.id, d.id')
    return conn.execute(
        sql + ' WHERE d.version_id=? ORDER BY d.id', (version_id,))


def _declaration(row, alias_index, archive):
    (declaration_id, version_id, category, ordinal, name, role, attributes,
     source_sha256, imported_at, book_id, title, identifier,
     current_version_id) = row
    author_id, status = resolve_name(alias_index, name)
    resolution = {'status': status, 'author_id': author_id}
    if status == 'ambiguous':
        resolution['candidate_author_ids'] = sorted(alias_index[name])
    return {
        'declaration_id': declaration_id,
        'archive': archive,
        'book_id': book_id,
        'title': title,
        'identifier': identifier,
        'version_id': version_id,
        'version_sha256': source_sha256,
        'version_current': version_id == current_version_id,
        'imported_at': imported_at,
        'category': category,
        'ordinal': ordinal,
        'name': name,
        'role': role,
        'attributes': _attributes(attributes),
        'resolution': resolution,
    }


def _paths(databases):
    """Normalize one path or an iterable of paths into a list of Paths."""
    if isinstance(databases, (str, Path)):
        databases = [databases]
    paths = list(dict.fromkeys(Path(path) for path in databases))
    if not paths:
        raise ValueError('at least one archive is required')
    return paths


def _open_set(databases, catalogue):
    """Open the catalogue archive and every archive whose declarations count.

    The catalogue archive is always included so its own book participates.
    Returns (opened, books, alias_index); ``opened`` is a list of (path, conn)
    that the caller must close, and ``books`` lists every archive's book.
    """
    paths = _paths(databases)
    catalogue_path = Path(catalogue) if catalogue is not None else paths[0]
    if catalogue_path not in paths:
        paths.insert(0, catalogue_path)
    opened = []
    try:
        for path in paths:
            opened.append((path, _connect(path)))
        connections = dict(opened)
        catalogue_available = ensure_catalogue_schema(
            connections[catalogue_path], create=False)
        books = []
        for path in paths:
            conn = connections[path]
            check_schema(conn)
            books.append((path, conn, get_book(conn)[0]))
        alias_index = (_alias_index(connections[catalogue_path])
                       if catalogue_available else {})
        return opened, books, alias_index
    except Exception:
        for path, conn in opened:
            conn.close()
        raise


def _close_set(opened):
    for path, conn in opened:
        conn.close()


def _has_declarations(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='contributor_declarations'").fetchone() is not None


def _collect(databases, catalogue, version, include):
    """Read and resolve declarations across archives, keeping source order.

    With a version selector, archives that do not hold that version are skipped;
    at least one archive must match so a typo still fails loudly.
    """
    opened, books, alias_index = _open_set(databases, catalogue)
    try:
        result = []
        matched_version = version is None
        for path, conn, book_id in books:
            if not _has_declarations(conn):
                # An older archive without the catalogue contributes nothing;
                # it stays readable and untouched.
                continue
            if version is None:
                version_id = None
            else:
                try:
                    version_id = resolve_version(conn, book_id, version)
                except ValueError:
                    continue
                matched_version = True
            for row in _declaration_rows(conn, version_id):
                item = _declaration(row, alias_index, str(path))
                if include(item):
                    result.append(item)
        if not matched_version:
            raise ValueError('unknown version')
        return result
    finally:
        _close_set(opened)


def _single_query(database, version, include):
    return _collect([database], database, version, include)


def list_declarations(database, version=None):
    """Every raw source declaration with its current catalogue resolution."""
    return _single_query(database, version, lambda item: True)


def unresolved_declarations(database, version=None):
    """Declarations no single identity currently owns.

    Includes names with no alias at all ('unmapped') and names whose alias maps
    to several identities ('ambiguous'), so both stay discoverable together with
    their original name, category, order, role and source version.
    """
    return _single_query(
        database, version,
        lambda item: item['resolution']['status'] != 'resolved')


def query_declarations(databases, catalogue=None, version=None):
    """Every source declaration across one or more archives."""
    return _collect(databases, catalogue, version, lambda item: True)


def query_unresolved(databases, catalogue=None, version=None):
    """Unresolved declarations across one or more archives."""
    return _collect(
        databases, catalogue, version,
        lambda item: item['resolution']['status'] != 'resolved')


def _group_books(declarations, author_id):
    books = {}
    for item in declarations:
        if item['resolution']['author_id'] != author_id:
            continue
        book = books.setdefault((item['archive'], item['book_id']), {
            'archive': item['archive'],
            'book_id': item['book_id'],
            'title': item['title'],
            'identifier': item['identifier'],
            'versions': {},
        })
        entry = book['versions'].setdefault(item['version_id'], {
            'version_id': item['version_id'],
            'source_sha256': item['version_sha256'],
            'imported_at': item['imported_at'],
            'current': item['version_current'],
            'declarations': [],
        })
        entry['declarations'].append({
            'declaration_id': item['declaration_id'],
            'category': item['category'],
            'ordinal': item['ordinal'],
            'name': item['name'],
            'role': item['role'],
            'attributes': item['attributes'],
        })
    result = list(books.values())
    for book in result:
        book['versions'] = list(book['versions'].values())
    return result


def author_books(database, author_id, version=None):
    """One archive's books and versions resolving uniquely to one identity."""
    return _author_books([database], database, author_id, version)


def query_author_books(databases, author_id, catalogue=None, version=None):
    """Books and versions across archives resolving uniquely to one identity.

    Declarations stay grouped per archive/version and are never deduplicated
    across versions or categories, so every source statement that caused the
    match stays visible.
    """
    return _author_books(databases, catalogue, author_id, version)


def _author_books(databases, catalogue, author_id, version):
    if type(author_id) is not int:
        raise ValueError('invalid author identity')
    paths = _paths(databases)
    catalogue_path = Path(catalogue) if catalogue is not None else paths[0]
    conn = _connect(catalogue_path)
    try:
        if not _catalogue_ready(conn):
            raise ValueError('archive has no author catalogue')
        row = _author(conn, author_id)
    finally:
        conn.close()
    declarations = _collect(
        databases, catalogue, version, lambda item: True)
    return {
        'author_id': row[0],
        'display_name': row[1],
        'books': _group_books(declarations, author_id),
    }
