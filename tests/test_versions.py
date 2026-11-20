import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from unittest import mock
from epub_index import annotations as ann
from epub_index.archive import ingest, list_versions, search, select_version
from epub_index.annotations import add_annotation, pending, relocate
from epub_index.db import check_schema, open_connection

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'make_versions', ROOT / 'fixtures/make_versions.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

EXCERPT = 'old quay'
NOTE = 'reader note kept verbatim'


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'books.sqlite'
        self.v1 = fixture.build(self.root / 'v1.epub', 'v1')
        self.v2 = fixture.build(self.root / 'v2.epub', 'v2')
        self.v3 = fixture.build(self.root / 'v3.epub', 'v3')
        self.v4 = fixture.build(self.root / 'v4.epub', 'v4')

    def import_versions(self, *variants):
        paths = {'v1': self.v1, 'v2': self.v2, 'v3': self.v3, 'v4': self.v4}
        results = [ingest(paths[v], self.db) for v in variants]
        return results

    def annotate_v1(self):
        # Explicitly anchor against version 1 regardless of the current version.
        return add_annotation(self.db, 2, EXCERPT, NOTE, version=1)

    # --- version import -------------------------------------------------
    def test_first_import_requires_missing_target(self):
        self.db.write_bytes(b'old')
        with self.assertRaises(sqlite3.Error):
            ingest(self.v1, self.db)
        self.assertEqual(self.db.read_bytes(), b'old')

    def test_new_sha_saved_as_version_without_overwrite(self):
        first, second = self.import_versions('v1', 'v2')
        self.assertEqual(first['action'], 'imported')
        self.assertEqual(first['version_id'], 1)
        self.assertEqual(second['action'], 'imported')
        self.assertEqual(second['version_id'], 2)
        versions = list_versions(self.db)
        self.assertEqual([v['version_id'] for v in versions], [1, 2])
        self.assertEqual([v['current'] for v in versions], [False, True])
        conn = open_connection(self.db)
        try:
            counts = conn.execute(
                'SELECT version_id, COUNT(*) FROM chapters GROUP BY version_id'
            ).fetchall()
            self.assertEqual(counts, [(1, 3), (2, 4)])
            fts = conn.execute(
                'SELECT version_id, COUNT(*) FROM search GROUP BY version_id'
            ).fetchall()
            self.assertEqual(fts, [(1, 3), (2, 4)])
        finally:
            conn.close()

    def test_repeated_sha_is_idempotent(self):
        first = self.import_versions('v1')[0]
        again = ingest(self.v1, self.db)
        self.assertEqual(again['action'], 'unchanged')
        self.assertEqual(again['version_id'], first['version_id'])
        self.assertEqual(len(list_versions(self.db)), 1)
        conn = open_connection(self.db)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM versions').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM chapters').fetchone()[0], 3)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM search').fetchone()[0], 3)
        finally:
            conn.close()

    def test_same_sha_identifier_conflict_rejected(self):
        # v1 content under a different package identifier must never be accepted.
        other = self.root / 'other.epub'
        with zipfile.ZipFile(self.v1) as archive:
            rows = {r.filename: archive.read(r) for r in archive.infolist()}
        rows['OPS/book.opf'] = rows['OPS/book.opf'].replace(
            b'urn:sample:harbor', b'urn:sample:different')
        with zipfile.ZipFile(other, 'w') as archive:
            for name, body in rows.items():
                archive.writestr(name, body)
        self.import_versions('v1')
        with self.assertRaisesRegex(ValueError, 'identifier conflict'):
            ingest(other, self.db)
        self.assertEqual(
            conn_count(self.db, 'SELECT COUNT(*) FROM book'), 1)

    def test_failed_update_leaves_no_partial_version(self):
        self.import_versions('v1')
        bad = self.root / 'bad.epub'
        bad.write_bytes(b'not an epub')
        with self.assertRaises(Exception):
            ingest(bad, self.db)
        versions = list_versions(self.db)
        self.assertEqual(len(versions), 1)
        self.assertTrue(versions[0]['current'])
        # Old search results remain unchanged and searchable.
        self.assertEqual(len(search(self.db, 'harbor')), 1)

    def test_mid_transaction_failure_rolls_back_everything(self):
        self.import_versions('v1')
        # Force a failure after chapters/FTS rows are inserted but before commit.
        with mock.patch('epub_index.archive.relocate_for_version',
                        side_effect=RuntimeError('boom')):
            with self.assertRaisesRegex(RuntimeError, 'boom'):
                ingest(self.v2, self.db)
        conn = open_connection(self.db)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM versions').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM chapters').fetchone()[0], 3)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM search').fetchone()[0], 3)
            current = conn.execute('SELECT current_version_id FROM book').fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(current, 1)
        # The archive stays fully usable and points at the unchanged version.
        self.assertEqual(len(search(self.db, 'quay', 1)), 1)

    def test_first_import_failure_leaves_empty_database(self):
        fresh = self.root / 'fresh.sqlite'
        with mock.patch('epub_index.archive.relocate_for_version',
                        side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                ingest(self.v1, fresh)
        conn = open_connection(fresh)
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        finally:
            conn.close()
        self.assertEqual(tables, [])

    def test_select_version_and_scoped_search(self):
        self.import_versions('v1', 'v2')
        sha1 = list_versions(self.db)[0]['source_sha256']
        # Default = newest imported version (v2, 4 chapters).
        self.assertEqual(len(search(self.db, 'harbor')), 1)
        select_version(self.db, 1)
        self.assertEqual([v['current'] for v in list_versions(self.db)], [True, False])
        hits = search(self.db, 'harbor')
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['position'], 1)
        # SHA selector works too and does not change current.
        hits_sha = search(self.db, 'harbor', sha1)
        self.assertEqual(len(hits_sha), 1)
        with self.assertRaises(ValueError):
            search(self.db, 'harbor', 'deadbeef')

    # --- annotation creation -------------------------------------------
    def test_annotation_stores_anchor_context_and_verbatim_note(self):
        self.import_versions('v1')
        result = self.annotate_v1()
        self.assertEqual(result['chapter_position'], 2)
        conn = open_connection(self.db)
        try:
            row = conn.execute(
                'SELECT chapter_position,chapter_href,excerpt,prefix,suffix,note '
                'FROM annotations').fetchone()
        finally:
            conn.close()
        position, href, excerpt, prefix, suffix, note = row
        self.assertEqual(position, 2)
        self.assertEqual(href, 'OPS/two.xhtml')
        self.assertEqual(excerpt, EXCERPT)
        self.assertEqual(note, NOTE)
        self.assertTrue(prefix.endswith('Fishermen remember the '))
        self.assertEqual(suffix, ' where the herring boats landed.')
        self.assertLessEqual(len(prefix), 32)
        self.assertLessEqual(len(suffix), 32)

    def test_empty_or_offtext_excerpt_rejected(self):
        self.import_versions('v1')
        for bad in ('', '   '):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                add_annotation(self.db, 2, bad, NOTE)
        # Text that FTS tokenization could find but which is not a raw substring.
        with self.assertRaisesRegex(ValueError, 'not part of the chapter text'):
            add_annotation(self.db, 2, 'harbor boats', NOTE)
        with self.assertRaisesRegex(ValueError, 'not part of the chapter text'):
            add_annotation(self.db, 2, 'OLD QUAY', NOTE)
        with self.assertRaises(ValueError):
            add_annotation(self.db, 99, EXCERPT, NOTE)

    def test_fts_tokens_are_not_used_as_anchors(self):
        self.import_versions('v1')
        # FTS-style wildcard syntax and whitespace-normalized phrases are not raw
        # substrings, so they cannot serve as annotation anchors even though the
        # same tokens are valid FTS query words.
        with self.assertRaises(ValueError):
            add_annotation(self.db, 2, 'quay*', 'wildcard note')
        with self.assertRaises(ValueError):
            add_annotation(self.db, 2, 'old   quay', 'spaced note')
        # A genuine contiguous raw substring is accepted and anchored verbatim.
        created = add_annotation(self.db, 2, 'quay', 'single word note')
        self.assertEqual(created['offset'],
                         self._chapter_text(1, 2).find('quay'))

    def _chapter_text(self, version_id, position):
        conn = open_connection(self.db)
        try:
            return conn.execute(
                'SELECT text FROM chapters WHERE version_id=? AND position=?',
                (version_id, position)).fetchone()[0]
        finally:
            conn.close()

    # --- relocation ------------------------------------------------------
    def test_unique_relocation_shows_only_on_matching_chapter(self):
        self.import_versions('v1', 'v2')
        self.annotate_v1()
        # Re-import order already ran relocation; annotation made on v1 gets a
        # v2 location automatically on future imports. Simulate fresh order:
        self._relocate_like_import(2)
        hits_v2 = search(self.db, 'quay', 2)
        chapter_three = next(h for h in hits_v2 if h['position'] == 3)
        self.assertEqual(len(chapter_three['annotations']), 1)
        self.assertEqual(chapter_three['annotations'][0]['excerpt'], EXCERPT)
        chapter_four = next(h for h in hits_v2 if h['position'] == 4)
        self.assertEqual(chapter_four['annotations'], [])
        # Old version still shows the annotation on its original chapter.
        hits_v1 = search(self.db, 'quay', 1)
        chapter_two = next(h for h in hits_v1 if h['position'] == 2)
        self.assertEqual(len(chapter_two['annotations']), 1)
        self.assertEqual(chapter_two['annotations'][0]['note'], NOTE)
        self.assertEqual(pending(self.db, 2), [])

    def _relocate_like_import(self, version_id):
        conn = open_connection(self.db)
        try:
            check_schema(conn)
            conn.execute('DELETE FROM annotation_locations WHERE version_id=?',
                         (version_id,))
            conn.execute('BEGIN')
            ann.relocate_for_version(conn, 1, version_id)
            conn.commit()
        finally:
            conn.close()

    def test_ambiguous_relocation_hidden_from_search_and_listed_pending(self):
        self.import_versions('v1', 'v3')
        self.annotate_v1()
        self._relocate_like_import(2)
        for hit in search(self.db, 'quay', 2):
            self.assertEqual(hit['annotations'], [])
        todo = pending(self.db, 2)
        self.assertEqual(len(todo), 1)
        item = todo[0]
        self.assertEqual(item['status'], 'ambiguous')
        self.assertEqual(len(item['candidates']), 2)
        self.assertEqual({c['chapter_position'] for c in item['candidates']}, {2, 3})
        # Original anchor and reader text are preserved.
        self.assertEqual(item['excerpt'], EXCERPT)
        self.assertEqual(item['note'], NOTE)
        self.assertEqual(item['source_chapter_position'], 2)
        self.assertEqual(item['source_chapter_href'], 'OPS/two.xhtml')
        # The old version still displays the annotation.
        old = search(self.db, 'quay', 1)
        old_chapter = next(h for h in old if h['position'] == 2)
        self.assertEqual(len(old_chapter['annotations']), 1)

    def test_missing_relocation_kept_in_pending_with_original_anchor(self):
        self.import_versions('v1', 'v4')
        self.annotate_v1()
        self._relocate_like_import(2)
        for hit in search(self.db, 'quay', 2):
            self.assertEqual(hit['annotations'], [])
        todo = pending(self.db, 2)
        self.assertEqual(len(todo), 1)
        item = todo[0]
        self.assertEqual(item['status'], 'missing')
        self.assertNotIn('candidates', item)
        self.assertEqual(item['excerpt'], EXCERPT)
        self.assertEqual(item['note'], NOTE)
        self.assertEqual(item['source_chapter_href'], 'OPS/two.xhtml')
        # Old version display is untouched.
        old = search(self.db, 'quay', 1)
        old_chapter = next(h for h in old if h['position'] == 2)
        self.assertEqual(len(old_chapter['annotations']), 1)

    def test_relocation_rules_unit(self):
        text_a = 'xx remember the old quay where the herring boats landed. yy'
        text_b = 'A painted sign reads old quay beside the new marina entrance.'
        prefix = ' remember the '
        suffix = ' where the herring boats landed.'
        status, position, offset = relocate(
            [(1, text_a), (2, text_b)], EXCERPT, prefix, suffix)
        self.assertEqual((status, position), ('located', 1))
        self.assertEqual(text_a[offset:offset + len(EXCERPT)], EXCERPT)
        status, _, _ = relocate([(1, text_a), (2, text_a)], EXCERPT, prefix, suffix)
        self.assertEqual(status, 'ambiguous')
        status, _, _ = relocate([(1, text_b)], EXCERPT, prefix, suffix)
        self.assertEqual(status, 'missing')

    def test_annotation_created_after_import_gets_future_relocation(self):
        self.import_versions('v1')
        created = self.annotate_v1()
        # Importing v2 after the annotation exists must relocate it automatically.
        ingest(self.v2, self.db)
        todo = pending(self.db, 2)
        self.assertEqual(todo, [])
        hits = search(self.db, 'quay', 2)
        shown = [a for h in hits for a in h['annotations']]
        self.assertEqual([a['annotation_id'] for a in shown], [created['annotation_id']])

    def test_path_change_preserves_annotation_text(self):
        self.import_versions('v1', 'v2')
        self.annotate_v1()
        conn = open_connection(self.db)
        try:
            hrefs = {r[0]: r[1] for r in conn.execute(
                'SELECT version_id, href FROM chapters WHERE position=2')}
        finally:
            conn.close()
        self.assertEqual(hrefs, {1: 'OPS/two.xhtml', 2: 'OPS/two-renamed.xhtml'})
        note = conn_note(self.db)
        self.assertEqual(note, NOTE)


def conn_count(db, sql):
    conn = open_connection(db)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def conn_note(db):
    conn = open_connection(db)
    try:
        return conn.execute('SELECT note FROM annotations').fetchone()[0]
    finally:
        conn.close()


if __name__ == '__main__':
    unittest.main()
