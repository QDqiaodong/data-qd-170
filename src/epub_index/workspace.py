"""Journal anthology workspace: chapter plans, reproduction licenses, reservations and publication.

This module owns the *offline* editorial workflow that sits on top of the
versioned EPUB archives. A workspace is one local directory shared by the two
roles:

* editors save ordered chapter-selection plans per journal issue, apply for
  quota reservations and publish reserved issues as JSON anthologies;
* the copyright clerk registers and revokes reproduction licenses and inspects
  their original scope, status and formal usage records.

A workspace designates exactly one existing archive as its common author
catalogue. Canonical author ids always come from that catalogue's own
``authors`` table: an id that merely coincides with an id in another archive is
never accepted. Book archives and the catalogue are only ever opened read-only,
so workspace operations never mutate an archived library.

Identity of a selected chapter is the triple (package identifier, source
SHA-256, spine position); the archive path only tells the workflow which file
to open. The current version, another version's same-position chapter and
search snippets can never substitute for the explicitly selected chapter text.

Reservation feasibility is a bipartite capacity problem: every unit of demand
(one chapter, one canonical author, per issue) must be assignable to an active
license whose fixed scope covers that exact chapter and whose remaining quota
(initial quota minus formal usage) is positive. A max-flow computation decides
feasibility and, at publication time, which license each author actually uses.
"""
from collections import deque
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .authors import _alias_index, resolve_name
from .common import array, fields, publish, read_json
from .db import check_schema, ensure_catalogue_schema, utc_now

WORKSPACE_DB = 'workspace.sqlite'
ANTHOLOGY_DIR = 'anthologies'
SHA256_HEX = '0123456789abcdef'

DDL = (
    """CREATE TABLE ws_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE issues(
        issue INTEGER PRIMARY KEY,
        status TEXT NOT NULL CHECK(status IN ('draft','reserved','published')),
        reservation_seq INTEGER,
        reserved_at TEXT,
        published_at TEXT,
        reason TEXT,
        updated_at TEXT NOT NULL
    )""",
    # One row per planned chapter in plan order. The archive path is recorded
    # for reopening only; the chapter identity is (identifier, sha, position).
    """CREATE TABLE issue_chapters(
        issue INTEGER NOT NULL REFERENCES issues(issue),
        ordinal INTEGER NOT NULL,
        archive_path TEXT NOT NULL,
        identifier TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY(issue, ordinal),
        UNIQUE(issue, identifier, source_sha256, position)
    )""",
    # Canonical authors resolved against the common catalogue at the time the
    # issue was last successfully reserved. Names are snapshotted so published
    # anthologies never recompute against a later catalogue.
    """CREATE TABLE issue_chapter_authors(
        issue INTEGER NOT NULL,
        ordinal INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        author_name TEXT NOT NULL,
        PRIMARY KEY(issue, ordinal, author_id),
        FOREIGN KEY(issue, ordinal) REFERENCES issue_chapters(issue, ordinal)
    )""",
    """CREATE TABLE reservation_events(
        id INTEGER PRIMARY KEY,
        issue INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN (
            'reserved','reserve_failed','withdrawn','returned',
            'publish_rejected','published')),
        seq INTEGER,
        reason TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE licenses(
        license_code TEXT PRIMARY KEY,
        author_id INTEGER NOT NULL,
        author_name TEXT NOT NULL,
        identifier TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        quota INTEGER NOT NULL CHECK(quota > 0),
        status TEXT NOT NULL CHECK(status IN ('active','revoked')),
        created_at TEXT NOT NULL,
        revoked_at TEXT,
        revoke_reason TEXT
    )""",
    """CREATE TABLE license_positions(
        license_code TEXT NOT NULL REFERENCES licenses(license_code),
        position INTEGER NOT NULL,
        PRIMARY KEY(license_code, position)
    )""",
    """CREATE TABLE publications(
        publication_id INTEGER PRIMARY KEY,
        issue INTEGER NOT NULL UNIQUE REFERENCES issues(issue),
        published_at TEXT NOT NULL,
        anthology_path TEXT NOT NULL,
        anthology_json TEXT NOT NULL
    )""",
    """CREATE TABLE license_usages(
        id INTEGER PRIMARY KEY,
        publication_id INTEGER NOT NULL REFERENCES publications(publication_id),
        license_code TEXT NOT NULL REFERENCES licenses(license_code),
        issue INTEGER NOT NULL,
        ordinal INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        identifier TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        position INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(publication_id, license_code, issue, ordinal, author_id)
    )""",
)


# --------------------------------------------------------------------------- #
# Small Edmonds-Karp max flow for license feasibility.
# --------------------------------------------------------------------------- #
class _MaxFlow:
    """Bipartite b-matching: demand nodes (capacity 1) to licensed capacity."""

    def __init__(self, size):
        self.graph = [[] for _ in range(size)]

    def add_edge(self, source, target, capacity):
        # Edge as [target, reverse-index, residual capacity].
        self.graph[source].append([target, len(self.graph[target]), capacity])
        self.graph[target].append([source, len(self.graph[source]) - 1, 0])

    def max_flow(self, source, sink):
        total = 0
        while True:
            parent = {source: None}
            queue = deque([source])
            while queue and sink not in parent:
                node = queue.popleft()
                for index, edge in enumerate(self.graph[node]):
                    target, _reverse, residual = edge
                    if residual > 0 and target not in parent:
                        parent[target] = (node, index)
                        queue.append(target)
            if sink not in parent:
                return total
            bottleneck = float('inf')
            node = sink
            while node != source:
                upper, index = parent[node]
                bottleneck = min(bottleneck, self.graph[upper][index][2])
                node = upper
            node = sink
            while node != source:
                upper, index = parent[node]
                edge = self.graph[upper][index]
                edge[2] -= bottleneck
                reverse = self.graph[node][edge[1]]
                reverse[2] += bottleneck
                node = upper
            total += bottleneck

    def edge_flow(self, source, index):
        """Flow carried by an edge added earlier as (capacity minus residual)."""
        target, reverse_index, residual = self.graph[source][index]
        return self.graph[target][reverse_index][2]


def _feasible(demands, licenses):
    """Return (ok, assignment) for demand units against active license capacity.

    demands: list of demand tuples (issue, ordinal, author_id, identifier,
    source_sha256, position). licenses: list of dicts carrying code, author_id,
    identifier, source_sha256, a positions set and remaining quota.

    A saturated flow covering every demand certifies that all involved whole
    issues can be honored at once; the assignment maps each demand index to the
    license code that would cover it.
    """
    demand_count = len(demands)
    license_count = len(licenses)
    source = 0
    sink = demand_count + license_count + 1
    flow = _MaxFlow(sink + 1)
    demand_edges = []
    outgoing = []
    for index, demand in enumerate(demands, start=1):
        edge_index = len(flow.graph[source])
        flow.add_edge(source, index, 1)
        demand_edges.append(edge_index)
        issue, ordinal, author_id, identifier, sha, position = demand
        eligible = []
        for license_index, lic in enumerate(licenses, start=demand_count + 1):
            if (lic['author_id'] == author_id
                    and lic['identifier'] == identifier
                    and lic['source_sha256'] == sha
                    and position in lic['positions']
                    and lic['remaining'] > 0):
                edge = len(flow.graph[index])
                flow.add_edge(index, license_index, 1)
                eligible.append((edge, lic['license_code']))
        outgoing.append(eligible)
    for license_index, lic in enumerate(licenses, start=demand_count + 1):
        flow.add_edge(license_index, sink, lic['remaining'])
    total = flow.max_flow(source, sink)
    if total != demand_count:
        return False, {}
    assignment = {}
    for index in range(demand_count):
        if flow.edge_flow(source, demand_edges[index]) != 1:
            return False, {}
        chosen = [code for edge, code in outgoing[index]
                  if flow.edge_flow(index + 1, edge) == 1]
        if len(chosen) != 1:
            return False, {}
        assignment[index] = chosen[0]
    return True, assignment


# --------------------------------------------------------------------------- #
# Workspace connections and validation.
# --------------------------------------------------------------------------- #
def _workspace_path(workspace):
    path = Path(workspace)
    if path.is_symlink() or not path.is_dir():
        raise ValueError('workspace does not exist: ' + str(path))
    database = path / WORKSPACE_DB
    if not database.is_file():
        raise ValueError('workspace is not initialized: ' + str(path))
    return path, database


def _connect(database):
    conn = sqlite3.connect(database, timeout=15)
    conn.isolation_level = None
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=15000')
    return conn


def _open_archive(path):
    """Open an archived library strictly read-only; never create a file."""
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError('archive does not exist: ' + str(path))
    resolved = candidate.resolve()
    conn = sqlite3.connect(resolved.as_uri() + '?mode=ro', uri=True)
    try:
        check_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn, str(resolved)


def _open_catalogue(path):
    conn, resolved = _open_archive(path)
    try:
        if not ensure_catalogue_schema(conn, create=False):
            raise ValueError('archive has no author catalogue: ' + resolved)
        row = conn.execute('SELECT COUNT(*) FROM authors').fetchone()
        if row[0] == 0:
            raise ValueError('catalogue holds no canonical authors: ' + resolved)
    except Exception:
        conn.close()
        raise
    return conn, resolved


def _sha256(value):
    if not isinstance(value, str) or len(value) != 64 \
            or any(char not in SHA256_HEX for char in value):
        raise ValueError('source_sha256 must be a 64-character lowercase hex digest')
    return value


def _issue_number(value):
    if type(value) is not int or not 1 <= value <= 10**9:
        raise ValueError('issue number must be a positive integer')
    return value


def _catalogue_path(conn, catalogue):
    """Return the catalogue path to use, checking an explicit argument."""
    stored = conn.execute(
        "SELECT value FROM ws_meta WHERE key='catalogue_path'").fetchone()[0]
    if catalogue is not None:
        given = str(Path(catalogue).resolve())
        if given != stored:
            raise ValueError(
                'catalogue does not match the workspace catalogue: ' + stored)
    return stored


def init_workspace(workspace, catalogue):
    """Create a workspace directory bound to one common author catalogue."""
    root = Path(workspace)
    if root.is_symlink():
        raise ValueError('workspace path symlink rejected')
    database = root / WORKSPACE_DB
    if root.exists() and not root.is_dir():
        raise ValueError('workspace path is not a directory')
    if database.exists() or database.is_symlink():
        raise ValueError('workspace already initialized: ' + str(root))
    catalogue_conn, catalogue_resolved = _open_catalogue(catalogue)
    catalogue_conn.close()
    root.mkdir(parents=True, exist_ok=True)
    (root / ANTHOLOGY_DIR).mkdir(exist_ok=True)
    conn = _connect(database)
    try:
        conn.execute('BEGIN IMMEDIATE')
        for statement in DDL:
            conn.execute(statement)
        conn.execute('INSERT INTO ws_meta(key,value) VALUES (?,?)',
                     ('catalogue_path', catalogue_resolved))
        conn.execute('INSERT INTO ws_meta(key,value) VALUES (?,?)',
                     ('reservation_counter', '0'))
        conn.execute('INSERT INTO ws_meta(key,value) VALUES (?,?)',
                     ('license_counter', '0'))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        # We exclusively created this file above; remove it so a retry is not
        # mistaken for an already-initialized workspace.
        database.unlink(missing_ok=True)
        raise
    conn.close()
    return {'action': 'initialized', 'workspace': str(root.resolve()),
            'catalogue': catalogue_resolved}


# --------------------------------------------------------------------------- #
# Archive identity verification and catalogue-based author resolution.
# --------------------------------------------------------------------------- #
def _verify_chapter(archive_conn, item):
    """Verify the exact (identifier, SHA, position) chapter in one archive."""
    row = archive_conn.execute('SELECT identifier FROM book').fetchone()
    if row is None:
        raise ValueError('archive contains no book')
    if row[0] != item['identifier']:
        raise ValueError(
            'package identifier mismatch: plan names %r but archive holds %r'
            % (item['identifier'], row[0]))
    version = archive_conn.execute(
        'SELECT id FROM versions WHERE source_sha256=?',
        (item['source_sha256'],)).fetchone()
    if version is None:
        raise ValueError(
            'selected version %s is not stored in archive for %r'
            % (item['source_sha256'], item['identifier']))
    chapter = archive_conn.execute(
        'SELECT href, title FROM chapters WHERE version_id=? AND position=?',
        (version[0], item['position'])).fetchone()
    if chapter is None:
        raise ValueError(
            'chapter position %s not found in selected version %s'
            % (item['position'], item['source_sha256']))
    return version[0], chapter[0], chapter[1]


def _resolve_authors(catalogue_conn, archive_conn, version_id, where):
    """Resolve every creator declaration of one version to a unique author.

    Contributors never count. At least one creator declaration is required and
    every creator name must uniquely resolve against the common catalogue. Two
    declarations resolving to the same canonical author count once.
    """
    alias_index = _alias_index(catalogue_conn)
    declarations = archive_conn.execute(
        "SELECT name FROM contributor_declarations "
        "WHERE version_id=? AND category='creator' ORDER BY ordinal",
        (version_id,)).fetchall() if ensure_catalogue_schema(
        archive_conn, create=False) else []
    if not declarations:
        raise ValueError(where + ': selected version declares no creator')
    authors = {}
    for (name,) in declarations:
        author_id, status = resolve_name(alias_index, name)
        if status != 'resolved':
            if status == 'ambiguous':
                candidates = sorted(alias_index[name])
                raise ValueError(
                    '%s: creator declaration %r is ambiguous between authors %s'
                    % (where, name, candidates))
            raise ValueError(
                '%s: creator declaration %r is unmapped in the common catalogue'
                % (where, name))
        if author_id not in authors:
            display = catalogue_conn.execute(
                'SELECT display_name FROM authors WHERE id=?',
                (author_id,)).fetchone()
            authors[author_id] = display[0]
    return sorted(authors.items())


def _load_plan_file(plan_path):
    path = Path(plan_path)
    data = read_json(path)
    if not isinstance(data, list):
        raise ValueError('plan must be a JSON array of chapter selections')
    items = array(data)
    if not items:
        raise ValueError('plan lists no chapters')
    base = path.resolve().parent
    normalized = []
    seen = set()
    for index, entry in enumerate(items, start=1):
        fields(entry, 'archive identifier source_sha256 position')
        archive_text = entry['archive']
        if not isinstance(archive_text, str) or not archive_text:
            raise ValueError('item %s: archive must be a nonempty path' % index)
        archive = Path(archive_text)
        if not archive.is_absolute():
            archive = base / archive
        item = {
            'archive': str(archive.resolve()),
            'identifier': entry['identifier'],
            'source_sha256': _sha256(entry['source_sha256']),
            'position': entry['position'],
        }
        if type(item['position']) is not int or item['position'] < 1:
            raise ValueError('item %s: spine position must be a positive integer'
                             % index)
        if not isinstance(item['identifier'], str) or not item['identifier']:
            raise ValueError('item %s: identifier must be a nonempty string'
                             % index)
        identity = (item['identifier'], item['source_sha256'], item['position'])
        if identity in seen:
            raise ValueError(
                'item %s: the same chapter appears twice in this issue' % index)
        seen.add(identity)
        normalized.append(item)
    return normalized


# --------------------------------------------------------------------------- #
# Plans.
# --------------------------------------------------------------------------- #
def save_plan(workspace, issue, plan_path):
    """Save (or replace a non-reserved) ordered chapter plan for one issue."""
    issue = _issue_number(issue)
    items = _load_plan_file(plan_path)
    root, database = _workspace_path(workspace)
    # Structural validation against the named archives before any write.
    opened = {}
    try:
        for item in items:
            if item['archive'] not in opened:
                conn, resolved = _open_archive(item['archive'])
                opened[item['archive']] = (conn, resolved)
            conn = opened[item['archive']][0]
            item['archive'] = opened[item['archive']][1]
            _verify_chapter(conn, item)
    finally:
        for conn, _resolved in opened.values():
            conn.close()
    conn = _connect(database)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT status FROM issues WHERE issue=?', (issue,)).fetchone()
        if row is not None and row[0] != 'draft':
            raise ValueError(
                'issue %s is %s; withdraw it before changing the plan'
                % (issue, row[0]))
        now = utc_now()
        if row is None:
            conn.execute(
                'INSERT INTO issues(issue,status,reason,updated_at) '
                "VALUES (?, 'draft', ?, ?)", (issue, None, now))
        else:
            conn.execute(
                "UPDATE issues SET status='draft', reservation_seq=NULL, "
                'reserved_at=NULL, published_at=NULL, reason=NULL, updated_at=? '
                'WHERE issue=?', (now, issue))
            # Author snapshots reference the chapter rows, so clear them first.
            conn.execute('DELETE FROM issue_chapter_authors WHERE issue=?',
                         (issue,))
            conn.execute('DELETE FROM issue_chapters WHERE issue=?', (issue,))
        for ordinal, item in enumerate(items, start=1):
            conn.execute(
                '''INSERT INTO issue_chapters(
                       issue, ordinal, archive_path, identifier,
                       source_sha256, position)
                   VALUES (?,?,?,?,?,?)''',
                (issue, ordinal, item['archive'], item['identifier'],
                 item['source_sha256'], item['position']))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {'action': 'saved', 'issue': issue, 'chapter_count': len(items)}


def _issue_demands(conn, issue):
    return [(issue, ordinal, author_id, identifier, source_sha256, position)
            for ordinal, author_id, identifier, source_sha256, position in
            conn.execute(
                '''SELECT c.ordinal, a.author_id, c.identifier,
                          c.source_sha256, c.position
                   FROM issue_chapter_authors a
                   JOIN issue_chapters c
                     ON c.issue=a.issue AND c.ordinal=a.ordinal
                   WHERE a.issue=?
                   ORDER BY c.ordinal, a.author_id''', (issue,)).fetchall()]


def _load_license_pool(conn):
    """Active licenses with remaining quota after formal usage is deducted."""
    pool = []
    rows = conn.execute(
        '''SELECT l.license_code, l.author_id, l.identifier, l.source_sha256,
                  l.quota, l.status,
                  (SELECT COUNT(*) FROM license_usages u
                    WHERE u.license_code=l.license_code) AS used
           FROM licenses l ORDER BY l.license_code''').fetchall()
    for code, author_id, identifier, sha, quota, status, used in rows:
        if status != 'active':
            continue
        positions = {position for position, in conn.execute(
            'SELECT position FROM license_positions WHERE license_code=?',
            (code,)).fetchall()}
        pool.append({
            'license_code': code, 'author_id': author_id,
            'identifier': identifier, 'source_sha256': sha,
            'positions': positions, 'remaining': quota - used,
        })
    return pool


def _reserved_issues(conn, exclude=None):
    sql = ("SELECT issue FROM issues WHERE status='reserved'")
    params = []
    if exclude is not None:
        sql += ' AND issue<>?'
        params.append(exclude)
    sql += ' ORDER BY reservation_seq'
    return [row[0] for row in conn.execute(sql, params).fetchall()]


def _reverify_and_resolve(conn, catalogue_path, issue):
    """Re-open every planned archive and resolve current catalogue authors."""
    items = conn.execute(
        '''SELECT ordinal, archive_path, identifier, source_sha256, position
           FROM issue_chapters WHERE issue=? ORDER BY ordinal''',
        (issue,)).fetchall()
    catalogue_conn, _resolved = _open_catalogue(catalogue_path)
    resolved = {}
    opened = {}
    try:
        for ordinal, archive_path, identifier, sha, position in items:
            if archive_path not in opened:
                archive_conn, archive_resolved = _open_archive(archive_path)
                opened[archive_path] = (archive_conn, archive_resolved)
            archive_conn = opened[archive_path][0]
            item = {'identifier': identifier, 'source_sha256': sha,
                    'position': position}
            version_id, _href, _title = _verify_chapter(archive_conn, item)
            where = 'issue %s chapter %s' % (issue, ordinal)
            resolved[ordinal] = _resolve_authors(
                catalogue_conn, archive_conn, version_id, where)
    finally:
        catalogue_conn.close()
        for archive_conn, _path in opened.values():
            archive_conn.close()
    return resolved


def reserve_plan(workspace, issue, catalogue=None):
    """Apply for a whole-issue reservation; failure leaves prior reservations intact."""
    issue = _issue_number(issue)
    root, database = _workspace_path(workspace)
    conn = _connect(database)
    try:
        catalogue_path = _catalogue_path(conn, catalogue)
        row = conn.execute(
            'SELECT status FROM issues WHERE issue=?', (issue,)).fetchone()
        if row is None:
            raise ValueError('no saved plan for issue %s' % issue)
        if row[0] == 'published':
            raise ValueError('issue %s is already published' % issue)
        if row[0] == 'reserved':
            raise ValueError('issue %s is already reserved' % issue)
        # Re-verify the archives and resolve authors against the catalogue as
        # it reads now. This is read-only and happens before any write lock.
        verify_error = None
        try:
            resolved = _reverify_and_resolve(conn, catalogue_path, issue)
        except ValueError as error:
            verify_error = str(error)
        conn.execute('BEGIN IMMEDIATE')
        now = utc_now()
        if verify_error is not None:
            _record_event(conn, issue, 'reserve_failed', None, verify_error)
            conn.execute('DELETE FROM issue_chapter_authors WHERE issue=?',
                         (issue,))
            conn.execute('UPDATE issues SET reason=?, updated_at=? WHERE issue=?',
                         (verify_error, now, issue))
            conn.commit()
            raise ValueError(verify_error)
        # Snapshot this issue's resolved authors for the feasibility demand set.
        conn.execute('DELETE FROM issue_chapter_authors WHERE issue=?', (issue,))
        for ordinal, authors in resolved.items():
            for author_id, name in authors:
                conn.execute(
                    '''INSERT INTO issue_chapter_authors(
                           issue, ordinal, author_id, author_name)
                       VALUES (?,?,?,?)''', (issue, ordinal, author_id, name))
        demands = []
        for other in _reserved_issues(conn):
            demands.extend(_issue_demands(conn, other))
        demands.extend(_issue_demands(conn, issue))
        ok, _assignment = _feasible(demands, _load_license_pool(conn))
        if not ok:
            reason = ('insufficient active license quota: the new plan together '
                      'with all earlier reserved issues cannot be fully licensed')
            _record_event(conn, issue, 'reserve_failed', None, reason)
            # A rejected reservation leaves no partial author snapshot behind.
            conn.execute('DELETE FROM issue_chapter_authors WHERE issue=?',
                         (issue,))
            conn.execute('UPDATE issues SET reason=?, updated_at=? WHERE issue=?',
                         (reason, now, issue))
            conn.commit()
            raise ValueError(reason)
        counter = int(conn.execute(
            "SELECT value FROM ws_meta WHERE key='reservation_counter'"
        ).fetchone()[0]) + 1
        conn.execute("UPDATE ws_meta SET value=? WHERE key='reservation_counter'",
                     (str(counter),))
        conn.execute(
            '''UPDATE issues SET status='reserved', reservation_seq=?,
                  reserved_at=?, reason=NULL, updated_at=? WHERE issue=?''',
            (counter, now, now, issue))
        _record_event(conn, issue, 'reserved', counter, None)
        conn.commit()
        return {'action': 'reserved', 'issue': issue,
                'reservation_seq': counter,
                'demands': len(_issue_demands(conn, issue))}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _record_event(conn, issue, kind, seq, reason):
    conn.execute(
        'INSERT INTO reservation_events(issue,kind,seq,reason,created_at) '
        'VALUES (?,?,?,?,?)', (issue, kind, seq, reason, utc_now()))


def withdraw_plan(workspace, issue):
    """Release a reservation; the saved plan stays available for resubmission."""
    issue = _issue_number(issue)
    _root, database = _workspace_path(workspace)
    conn = _connect(database)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT status FROM issues WHERE issue=?', (issue,)).fetchone()
        if row is None:
            raise ValueError('no saved plan for issue %s' % issue)
        if row[0] == 'published':
            raise ValueError('issue %s is published and cannot be withdrawn'
                             % issue)
        if row[0] != 'reserved':
            raise ValueError('issue %s is not reserved' % issue)
        conn.execute(
            '''UPDATE issues SET status='draft', reservation_seq=NULL,
                  reserved_at=NULL, reason=NULL, updated_at=? WHERE issue=?''',
            (utc_now(), issue))
        _record_event(conn, issue, 'withdrawn', None, None)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {'action': 'withdrawn', 'issue': issue}


def _plan_payload(conn, issue_row):
    issue, status, seq, reserved_at, published_at, reason, updated_at = issue_row
    chapters = []
    for ordinal, archive_path, identifier, sha, position in conn.execute(
            '''SELECT ordinal, archive_path, identifier, source_sha256, position
               FROM issue_chapters WHERE issue=? ORDER BY ordinal''',
            (issue,)).fetchall():
        authors = [{'author_id': author_id, 'name': name}
                   for author_id, name in conn.execute(
                       '''SELECT author_id, author_name FROM issue_chapter_authors
                          WHERE issue=? AND ordinal=? ORDER BY author_id''',
                       (issue, ordinal)).fetchall()]
        chapters.append({
            'ordinal': ordinal, 'archive': archive_path,
            'identifier': identifier, 'source_sha256': sha,
            'position': position, 'authors': authors,
        })
    events = [{'kind': kind, 'seq': event_seq, 'reason': event_reason,
               'created_at': created_at}
              for kind, event_seq, event_reason, created_at in conn.execute(
                  '''SELECT kind, seq, reason, created_at
                     FROM reservation_events WHERE issue=? ORDER BY id''',
                  (issue,)).fetchall()]
    return {
        'issue': issue, 'status': status, 'reservation_seq': seq,
        'reserved_at': reserved_at, 'published_at': published_at,
        'reason': reason, 'updated_at': updated_at,
        'chapters': chapters, 'events': events,
    }


def list_plans(workspace, issue=None):
    """List saved, reserved and published plans with rejection/return reasons."""
    _root, database = _workspace_path(workspace)
    conn = _connect(database)
    try:
        if issue is not None:
            issue = _issue_number(issue)
            row = conn.execute(
                '''SELECT issue,status,reservation_seq,reserved_at,published_at,
                          reason,updated_at
                   FROM issues WHERE issue=?''', (issue,)).fetchone()
            if row is None:
                raise ValueError('no saved plan for issue %s' % issue)
            return _plan_payload(conn, row)
        rows = conn.execute(
            '''SELECT issue,status,reservation_seq,reserved_at,published_at,
                      reason,updated_at
               FROM issues ORDER BY issue''').fetchall()
        return [_plan_payload(conn, row) for row in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Licenses.
# --------------------------------------------------------------------------- #
def register_license(workspace, catalogue, author_id, archive, identifier,
                     source_sha256, positions, quota):
    """Register one immutable-scope license for an author/version/position set."""
    if type(author_id) is not int or author_id < 1:
        raise ValueError('author id must be a positive integer')
    if type(quota) is not int or quota < 1:
        raise ValueError('quota must be a positive integer')
    sha = _sha256(source_sha256)
    if not isinstance(identifier, str) or not identifier:
        raise ValueError('identifier must be a nonempty string')
    positions = list(positions)
    if not positions or any(type(p) is not int or p < 1 for p in positions):
        raise ValueError('at least one positive chapter position is required')
    if len(set(positions)) != len(positions):
        raise ValueError('license positions must not repeat')
    _root, database = _workspace_path(workspace)
    # The author must exist in the common catalogue; ids from other archives
    # are never consulted.
    catalogue_conn, catalogue_resolved = _open_catalogue(catalogue)
    try:
        author = catalogue_conn.execute(
            'SELECT id, display_name FROM authors WHERE id=?',
            (author_id,)).fetchone()
        if author is None:
            raise ValueError('unknown author identity in the common catalogue')
        author_name = author[1]
    finally:
        catalogue_conn.close()
    # The exact book version and every allowed position must exist.
    archive_conn, archive_resolved = _open_archive(archive)
    try:
        version_id, _href, _title = _verify_chapter(
            archive_conn,
            {'identifier': identifier, 'source_sha256': sha,
             'position': positions[0]})
        for position in positions[1:]:
            found = archive_conn.execute(
                'SELECT 1 FROM chapters WHERE version_id=? AND position=?',
                (version_id, position)).fetchone()
            if found is None:
                raise ValueError(
                    'chapter position %s not found in selected version %s'
                    % (position, sha))
    finally:
        archive_conn.close()
    conn = _connect(database)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _catalogue_path(conn, catalogue_resolved)
        counter = int(conn.execute(
            "SELECT value FROM ws_meta WHERE key='license_counter'"
        ).fetchone()[0]) + 1
        code = 'LIC-%04d' % counter
        now = utc_now()
        conn.execute(
            '''INSERT INTO licenses(license_code,author_id,author_name,
                   identifier,source_sha256,quota,status,created_at)
               VALUES (?,?,?,?,?,?, 'active', ?)''',
            (code, author_id, author_name, identifier, sha, quota, now))
        for position in positions:
            conn.execute(
                'INSERT INTO license_positions(license_code,position) '
                'VALUES (?,?)', (code, position))
        conn.execute("UPDATE ws_meta SET value=? WHERE key='license_counter'",
                     (str(counter),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {'action': 'registered', 'license_code': code,
            'author_id': author_id, 'author_name': author_name,
            'identifier': identifier, 'source_sha256': sha,
            'positions': positions, 'quota': quota, 'status': 'active'}


def _license_payload(conn, row):
    code, author_id, author_name, identifier, sha, quota, status, created_at, \
        revoked_at, revoke_reason = row
    positions = [position for position, in conn.execute(
        'SELECT position FROM license_positions WHERE license_code=? '
        'ORDER BY position', (code,)).fetchall()]
    usages = [{
        'usage_id': usage_id, 'publication_id': publication_id,
        'license_code': code, 'issue': issue, 'ordinal': ordinal,
        'author_id': usage_author, 'identifier': usage_identifier,
        'source_sha256': usage_sha, 'position': usage_position,
        'created_at': usage_created,
    } for usage_id, publication_id, issue, ordinal, usage_author,
        usage_identifier, usage_sha, usage_position, usage_created in
        conn.execute(
            '''SELECT id, publication_id, issue, ordinal, author_id,
                      identifier, source_sha256, position, created_at
               FROM license_usages WHERE license_code=? ORDER BY id''',
            (code,)).fetchall()]
    used = len(usages)
    return {
        'license_code': code, 'author_id': author_id, 'author_name': author_name,
        'identifier': identifier, 'source_sha256': sha,
        'positions': positions, 'quota': quota, 'status': status,
        'created_at': created_at, 'revoked_at': revoked_at,
        'revoke_reason': revoke_reason, 'used': used,
        'remaining': max(quota - used, 0) if status == 'active' else 0,
        'usages': usages,
    }


def list_licenses(workspace, license_code=None):
    """Show license scope, status and formal usage records."""
    _root, database = _workspace_path(workspace)
    conn = _connect(database)
    try:
        columns = ('''license_code,author_id,author_name,identifier,
                      source_sha256,quota,status,created_at,revoked_at,
                      revoke_reason''')
        if license_code is not None:
            row = conn.execute(
                'SELECT ' + columns + ' FROM licenses WHERE license_code=?',
                (license_code,)).fetchone()
            if row is None:
                raise ValueError('unknown license: ' + str(license_code))
            return _license_payload(conn, row)
        rows = conn.execute(
            'SELECT ' + columns + ' FROM licenses ORDER BY license_code'
        ).fetchall()
        return [_license_payload(conn, row) for row in rows]
    finally:
        conn.close()


def revoke_license(workspace, license_code, reason=None):
    """Revoke a license and cascade the effect over reserved issues atomically.

    Reserved issues keep their reservation in reservation-priority order while
    they remain fully satisfiable together with the earlier issues already
    kept. An issue that can no longer be paid for returns to draft, releases
    its whole reservation and carries the revocation reason; later issues are
    judged without that returned issue occupying any capacity. Published usage
    records always remain.
    """
    _root, database = _workspace_path(workspace)
    conn = _connect(database)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT status FROM licenses WHERE license_code=?',
            (license_code,)).fetchone()
        if row is None:
            raise ValueError('unknown license: ' + str(license_code))
        if row[0] == 'revoked':
            raise ValueError('license %s is already revoked' % license_code)
        now = utc_now()
        text = ('license %s revoked' % license_code
                + (': ' + reason if reason else ''))
        conn.execute(
            '''UPDATE licenses SET status='revoked', revoked_at=?,
                  revoke_reason=? WHERE license_code=?''',
            (now, text, license_code))
        pool = _load_license_pool(conn)
        retained, returned = [], []
        kept_demands = []
        for issue in _reserved_issues(conn):
            trial = kept_demands + _issue_demands(conn, issue)
            ok, _assignment = _feasible(trial, pool)
            if ok:
                retained.append(issue)
                kept_demands = trial
                continue
            conn.execute(
                '''UPDATE issues SET status='draft', reservation_seq=NULL,
                      reserved_at=NULL, reason=?, updated_at=? WHERE issue=?''',
                (text, now, issue))
            _record_event(conn, issue, 'returned', None, text)
            returned.append(issue)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {'action': 'revoked', 'license_code': license_code,
            'retained_issues': retained, 'returned_issues': returned}


# --------------------------------------------------------------------------- #
# Publication and anthologies.
# --------------------------------------------------------------------------- #
def _existing_publication(conn, issue, replay=False):
    row = conn.execute(
        'SELECT publication_id, published_at, anthology_path, anthology_json '
        'FROM publications WHERE issue=?', (issue,)).fetchone()
    if row is None:
        return None
    publication_id, published_at, anthology_path, anthology_json = row
    payload = json.loads(anthology_json)
    payload['publication_id'] = publication_id
    payload['anthology_path'] = anthology_path
    if replay:
        # Mark a repeated publish call so callers can tell the record was
        # replayed; the first publication omits this marker.
        payload['idempotent_replay'] = True
    return payload


def publish_issue(workspace, issue, catalogue=None):
    """Publish one reserved issue, deduct quota once and freeze its anthology.

    Re-verification against the live archives and the current catalogue happens
    before any quota is touched; a failed verification rejects the whole issue,
    deducts nothing and keeps the reservation. License usage, the publication
    record and the final JSON anthology commit together. Repeating the call for
    an already published issue returns the original record without re-opening
    any archive or deducting quota again.
    """
    issue = _issue_number(issue)
    root, database = _workspace_path(workspace)
    conn = _connect(database)
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = _existing_publication(conn, issue, replay=True)
        if existing is not None:
            conn.commit()
            return existing
        row = conn.execute(
            'SELECT status FROM issues WHERE issue=?', (issue,)).fetchone()
        if row is None:
            raise ValueError('no saved plan for issue %s' % issue)
        if row[0] != 'reserved':
            raise ValueError(
                'issue %s is %s; only reserved issues can be published'
                % (issue, row[0]))
        catalogue_path = _catalogue_path(conn, catalogue)

        def reject(message):
            _record_event(conn, issue, 'publish_rejected', None, message)
            conn.execute('UPDATE issues SET reason=?, updated_at=? WHERE issue=?',
                         (message, utc_now(), issue))
            conn.commit()
            raise ValueError(message)

        try:
            resolved = _reverify_and_resolve(conn, catalogue_path, issue)
        except ValueError as error:
            reject(str(error))
        # The author identity set must be exactly the one reserved.
        for ordinal, authors in resolved.items():
            current_ids = {author_id for author_id, _name in authors}
            reserved_ids = {
                author_id for author_id, in conn.execute(
                    '''SELECT author_id FROM issue_chapter_authors
                       WHERE issue=? AND ordinal=?''',
                    (issue, ordinal)).fetchall()}
            if current_ids != reserved_ids:
                reject('issue %s chapter %s: author set differs from the '
                       'reserved set' % (issue, ordinal))
        # Build the demand set: this issue plus every other reserved issue.
        demands = []
        for other in _reserved_issues(conn, exclude=issue):
            demands.extend(_issue_demands(conn, other))
        own_demands = _issue_demands(conn, issue)
        offset = len(demands)
        demands.extend(own_demands)
        ok, assignment = _feasible(demands, _load_license_pool(conn))
        if not ok:
            reject('publishing issue %s would leave another reserved issue '
                   'without sufficient license quota' % issue)
        usage_by_chapter = {}
        for index, demand in enumerate(own_demands):
            _issue, ordinal, author_id, identifier, sha, position = demand
            usage_by_chapter.setdefault(ordinal, []).append(
                (author_id, assignment[offset + index]))

        # Read the exact selected chapters' archived content (never the current
        # version and never search snippets) and assemble the anthology.
        opened = {}
        chapters_out = []
        try:
            plan_rows = conn.execute(
                '''SELECT ordinal, archive_path, identifier, source_sha256,
                          position
                   FROM issue_chapters WHERE issue=? ORDER BY ordinal''',
                (issue,)).fetchall()
            for ordinal, archive_path, identifier, sha, position in plan_rows:
                if archive_path not in opened:
                    archive_conn, _resolved_path = _open_archive(archive_path)
                    opened[archive_path] = archive_conn
                archive_conn = opened[archive_path]
                version_row = archive_conn.execute(
                    'SELECT id FROM versions WHERE source_sha256=?',
                    (sha,)).fetchone()
                chapter_row = archive_conn.execute(
                    '''SELECT href, title, text FROM chapters
                       WHERE version_id=? AND position=?''',
                    (version_row[0], position)).fetchone()
                href, title, text = chapter_row
                authors_out = []
                for author_id, name in resolved[ordinal]:
                    license_code = dict(usage_by_chapter[ordinal])[author_id]
                    authors_out.append({
                        'author_id': author_id, 'name': name,
                        'license_code': license_code,
                    })
                chapters_out.append({
                    'ordinal': ordinal, 'archive': archive_path,
                    'identifier': identifier, 'source_sha256': sha,
                    'position': position, 'href': href, 'title': title,
                    'text': text, 'authors': authors_out,
                })
        finally:
            for archive_conn in opened.values():
                archive_conn.close()

        now = utc_now()
        anthology = {'issue': issue, 'published_at': now, 'chapters': chapters_out}
        anthology_text = json.dumps(anthology, ensure_ascii=False,
                                    sort_keys=True, allow_nan=False, indent=2)
        anthology_dir = root / ANTHOLOGY_DIR
        anthology_dir.mkdir(exist_ok=True)
        final_path = anthology_dir / ('issue-%s.json' % issue)
        if final_path.is_symlink():
            raise ValueError('anthology output symlink rejected')
        temporary_path = None
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=anthology_dir, delete=False) as stream:
            temporary_path = stream.name
            stream.write(anthology_text)
            stream.write('\n')
        committed = False
        try:
            cursor = conn.execute(
                '''INSERT INTO publications(
                       issue, published_at, anthology_path, anthology_json)
                   VALUES (?,?,?,?)''',
                (issue, now, str(final_path.resolve()), anthology_text))
            publication_id = cursor.lastrowid
            for index, demand in enumerate(own_demands):
                _issue, ordinal, author_id, identifier, sha, position = demand
                conn.execute(
                    '''INSERT INTO license_usages(publication_id, license_code,
                           issue, ordinal, author_id, identifier, source_sha256,
                           position, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)''',
                    (publication_id, assignment[offset + index], issue, ordinal,
                     author_id, identifier, sha, position, now))
            conn.execute(
                '''UPDATE issues SET status='published', published_at=?,
                      reason=NULL, updated_at=? WHERE issue=?''',
                (now, now, issue))
            _record_event(conn, issue, 'published', None, None)
            os.replace(temporary_path, final_path)
            temporary_path = None
            conn.commit()
            committed = True
        finally:
            if not committed and temporary_path is not None:
                Path(temporary_path).unlink(missing_ok=True)
        result = json.loads(anthology_text)
        result['publication_id'] = publication_id
        result['anthology_path'] = str(final_path.resolve())
        return result
    except Exception:
        # Any unexpected failure before commit rolls back usage and publication
        # rows together, so quota can never be deducted without an anthology.
        conn.rollback()
        raise
    finally:
        conn.close()


def get_anthology(workspace, issue, output=None):
    """Return the frozen anthology for a published issue; works offline forever.

    The stored record is authoritative, so a replay succeeds even if a license
    was later revoked or the originating archive has been moved away.
    """
    issue = _issue_number(issue)
    _root, database = _workspace_path(workspace)
    conn = _connect(database)
    try:
        payload = _existing_publication(conn, issue)
        if payload is None:
            raise ValueError('issue %s has no published anthology' % issue)
    finally:
        conn.close()
    payload.pop('idempotent_replay', None)
    if output is not None:
        publish(output, payload)
        payload['output'] = str(Path(output).resolve())
    return payload
