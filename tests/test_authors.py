"""Author catalogue: source declarations, identities, aliases and book queries."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from epub_index.archive import ingest, search, select_version
from epub_index.authors import (
    add_alias, author_books, create_author, list_authors, list_declarations,
    query_author_books, query_declarations, query_unresolved, remove_alias,
    unresolved_declarations,
)
from epub_index.db import open_connection

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'make_authors', ROOT / 'fixtures/make_authors.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

C = fixture.contributor_element


class AuthorFixture(unittest.TestCase):
    """Two independent books plus a versioned book, all with OPF declarations."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lee_db = self.root / 'lee.sqlite'
        self.jordan_db = self.root / 'jordan.sqlite'
        # Book A declares the alias form "J. Lee" first, then a translator.
        self.book_a = fixture.build(
            self.root / 'a.epub', 'urn:authors:a', C('creator', 'J. Lee', 'aut')
            + C('creator', 'Robin Fox', 'aut')
            + C('contributor', 'Casey Ng', 'trl'),
            title='Northern Lights')
        # Book B (separate archive) declares the canonical form "Jordan Lee".
        self.book_b = fixture.build(
            self.root / 'b.epub', 'urn:authors:b', C('creator', 'Jordan Lee', 'aut'),
            title='Southern Tides')
        ingest(self.book_a, self.lee_db)
        ingest(self.book_b, self.jordan_db)
        self.jordan = create_author(self.lee_db, 'Jordan Lee')['author_id']
        self.other = create_author(self.lee_db, 'Robin Fox')['author_id']

    def library(self, *dbs):
        return [str(self.lee_db)] + [str(db) for db in dbs]


class DeclarationStorageTests(AuthorFixture):
    def test_every_category_order_name_and_role_is_preserved(self):
        rows = list_declarations(self.lee_db)
        self.assertEqual(
            [(r['category'], r['ordinal'], r['name'], r['role']) for r in rows],
            [('creator', 1, 'J. Lee', 'aut'),
             ('creator', 2, 'Robin Fox', 'aut'),
             ('contributor', 1, 'Casey Ng', 'trl')])
        # Only the identity's own canonical name resolves without an alias.
        resolution = {r['name']: r['resolution']['status'] for r in rows}
        self.assertEqual(resolution, {'J. Lee': 'unmapped',
                                      'Robin Fox': 'resolved',
                                      'Casey Ng': 'unmapped'})
        self.assertTrue(all(r['version_id'] == 1 for r in rows))

    def test_import_does_not_rewrite_source_declarations(self):
        add_alias(self.lee_db, self.jordan, 'J. Lee')
        stored = list_declarations(self.lee_db)[0]
        # The raw declaration keeps the source spelling, only resolution moves.
        self.assertEqual(stored['name'], 'J. Lee')
        self.assertEqual(stored['resolution'],
                         {'status': 'resolved', 'author_id': self.jordan})
        with zipfile.ZipFile(self.book_a) as archive:
            self.assertIn(b'J. Lee', archive.read('OPS/book.opf'))

    def test_versions_keep_separate_declarations(self):
        # A newer version of the same book changes its contributors.
        newer = fixture.build(
            self.root / 'a2.epub', 'urn:authors:a',
            C('creator', 'Jordan Lee', 'aut') + C('contributor', 'Dana Ruiz', 'edt'),
            title='Northern Lights')
        ingest(newer, self.lee_db)
        rows = list_declarations(self.lee_db)
        by_version = {}
        for row in rows:
            by_version.setdefault(row['version_id'], []).append(
                (row['category'], row['ordinal'], row['name'], row['role']))
        self.assertEqual(by_version[1], [
            ('creator', 1, 'J. Lee', 'aut'),
            ('creator', 2, 'Robin Fox', 'aut'),
            ('contributor', 1, 'Casey Ng', 'trl')])
        self.assertEqual(by_version[2], [
            ('creator', 1, 'Jordan Lee', 'aut'),
            ('contributor', 1, 'Dana Ruiz', 'edt')])
        self.assertEqual(len(rows), 5)

    def test_missing_role_is_explicit_and_declaration_kept(self):
        no_role = fixture.build(
            self.root / 'no-role.epub', 'urn:authors:no-role',
            C('contributor', 'Uncredited Helper'),
            title='No Role')
        db = self.root / 'no-role.sqlite'
        ingest(no_role, db)
        row = list_declarations(db)[0]
        self.assertEqual(row['name'], 'Uncredited Helper')
        self.assertIsNone(row['role'])
        self.assertEqual(row['resolution']['status'], 'unmapped')

    def test_opf3_refines_role_is_read_without_rewriting_name(self):
        metadata = ('<dc:creator id="p1">Sam Ito</dc:creator>'
                    '<meta refines="#p1" property="role">ill</meta>'
                    '<meta refines="#p1" property="file-as">Ito, Sam</meta>')
        epub = fixture.build(self.root / 'refines.epub', 'urn:authors:refines',
                             metadata, title='Refines')
        db = self.root / 'refines.sqlite'
        ingest(epub, db)
        row = list_declarations(db)[0]
        self.assertEqual(row['name'], 'Sam Ito')
        self.assertEqual(row['role'], 'ill')
        self.assertEqual(row['attributes']['file-as'], 'Ito, Sam')

    def test_failed_import_leaves_no_partial_declarations(self):
        newer = fixture.build(
            self.root / 'a-broken.epub', 'urn:authors:a',
            C('creator', 'Jordan Lee', 'aut'), title='Northern Lights')
        ingest(newer, self.lee_db)
        broken = self.root / 'broken.epub'
        broken.write_bytes(b'not a zip')
        with self.assertRaises(Exception):
            ingest(broken, self.lee_db)
        self.assertEqual(len(list_declarations(self.lee_db)), 4)

    def test_mid_transaction_failure_rolls_back_declarations(self):
        from unittest import mock
        newer = fixture.build(
            self.root / 'a2.epub', 'urn:authors:a',
            C('creator', 'Dana Ruiz', 'edt'), title='Northern Lights',
            chapters=[('Chapter One', 'Fresh harbor prose for a new edition.')])
        # Fail after chapters and declarations are inserted, before commit.
        with mock.patch('epub_index.archive.relocate_for_version',
                        side_effect=RuntimeError('boom')):
            with self.assertRaisesRegex(RuntimeError, 'boom'):
                ingest(newer, self.lee_db)
        rows = list_declarations(self.lee_db)
        self.assertEqual([(r['version_id'], r['name']) for r in rows], [
            (1, 'J. Lee'), (1, 'Robin Fox'), (1, 'Casey Ng')])
        conn = open_connection(self.lee_db)
        try:
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM versions').fetchone()[0], 1)
        finally:
            conn.close()


class IdentityAndAliasTests(AuthorFixture):
    def test_identities_may_share_a_display_name_without_merging(self):
        second = create_author(self.lee_db, 'Jordan Lee')['author_id']
        self.assertNotEqual(second, self.jordan)
        names = [a['display_name'] for a in list_authors(self.lee_db)]
        self.assertEqual(names.count('Jordan Lee'), 2)
        # Sharing a display name resolves nothing by itself.
        self.assertEqual(unresolved_declarations(self.lee_db)[0]['name'], 'J. Lee')

    def test_alias_added_then_revoked_moves_declarations(self):
        add_alias(self.lee_db, self.jordan, 'J. Lee')
        self.assertEqual(
            [b['title'] for b in query_author_books(
                self.library(self.jordan_db), self.jordan)['books']],
            ['Northern Lights', 'Southern Tides'])
        removed = remove_alias(self.lee_db, self.jordan, 'J. Lee')
        self.assertEqual(removed['action'], 'removed')
        names = [b['title'] for b in query_author_books(
            self.library(self.jordan_db), self.jordan)['books']]
        self.assertEqual(names, ['Southern Tides'])
        unresolved = {r['name']: r for r in query_unresolved(
            self.library(self.jordan_db), self.lee_db)}
        self.assertIn('J. Lee', unresolved)
        self.assertEqual(unresolved['J. Lee']['category'], 'creator')
        self.assertEqual(unresolved['J. Lee']['ordinal'], 1)
        self.assertEqual(unresolved['J. Lee']['role'], 'aut')
        self.assertEqual(unresolved['J. Lee']['title'], 'Northern Lights')

    def test_conflicting_alias_belongs_to_no_identity(self):
        add_alias(self.lee_db, self.jordan, 'J. Lee')
        added = add_alias(self.lee_db, self.other, 'J. Lee')
        self.assertEqual(added['candidates'], sorted([self.jordan, self.other]))
        for author_id in (self.jordan, self.other):
            books = query_author_books(
                self.library(self.jordan_db), author_id)['books']
            matched = [d['name'] for book in books
                       for version in book['versions']
                       for d in version['declarations']]
            self.assertNotIn('J. Lee', matched)
        conflicted = [r for r in query_unresolved(
            self.library(self.jordan_db), self.lee_db)
            if r['name'] == 'J. Lee']
        self.assertEqual(len(conflicted), 1)
        self.assertEqual(conflicted[0]['resolution']['status'], 'ambiguous')
        self.assertEqual(conflicted[0]['resolution']['candidate_author_ids'],
                         sorted([self.jordan, self.other]))
        # Resolving the conflict restores the match; the declaration survived.
        remove_alias(self.lee_db, self.other, 'J. Lee')
        titles = [b['title'] for b in query_author_books(
            self.library(self.jordan_db), self.jordan)['books']]
        self.assertIn('Northern Lights', titles)

    def test_exact_full_name_only_no_fuzzy_or_display_name_matching(self):
        # "Jordan Lee" is the display name but is not an alias by itself.
        self.assertEqual(author_books(self.lee_db, self.jordan)['books'], [])
        # Partial and case-varied names never match an explicit alias.
        add_alias(self.lee_db, self.jordan, 'J. Lee')
        for alias in ('J Lee', 'j. lee', 'Lee'):
            self.assertEqual(
                add_alias(self.lee_db, self.jordan, alias)['action'], 'added')
        titles = [b['title'] for b in author_books(self.lee_db, self.jordan)['books']]
        self.assertEqual(titles, ['Northern Lights'])

    def test_identical_alias_re_added_is_idempotent(self):
        first = add_alias(self.lee_db, self.jordan, 'J. Lee')
        again = add_alias(self.lee_db, self.jordan, 'J. Lee')
        self.assertEqual(first['alias_id'], again['alias_id'])
        self.assertEqual(again['action'], 'unchanged')
        self.assertEqual(len(list_authors(self.lee_db)[0]['aliases']), 1)

    def test_display_name_is_implicitly_canonical(self):
        # "Jordan Lee" resolves via the identity's own display name even though
        # no alias was added, and an alias duplicating it is rejected.
        book = query_author_books(
            self.library(self.jordan_db), self.jordan,
            catalogue=self.lee_db)['books'][0]
        self.assertEqual(book['title'], 'Southern Tides')
        with self.assertRaisesRegex(ValueError, 'display name'):
            add_alias(self.lee_db, self.jordan, 'Jordan Lee')

    def test_unknown_identity_and_missing_archive_rejected(self):
        with self.assertRaises(ValueError):
            add_alias(self.lee_db, 999, 'Ghost Writer')
        missing = self.root / 'missing.sqlite'
        with self.assertRaises(ValueError):
            list_authors(missing)
        self.assertFalse(missing.exists())


class BookQueryTests(AuthorFixture):
    def test_alias_makes_all_versions_visible_with_source_declarations(self):
        newer = fixture.build(
            self.root / 'a2.epub', 'urn:authors:a',
            C('creator', 'J. Lee', 'aut') + C('creator', 'Robin Fox', 'aut')
            + C('contributor', 'Casey Ng', 'trl'),
            title='Northern Lights',
            chapters=[('Chapter One', 'Lanterns shine over the revised harbor.')])
        ingest(newer, self.lee_db)
        add_alias(self.lee_db, self.jordan, 'J. Lee')
        result = query_author_books(self.library(self.jordan_db), self.jordan)
        book = next(b for b in result['books'] if b['title'] == 'Northern Lights')
        self.assertEqual([v['version_id'] for v in book['versions']], [1, 2])
        for version in book['versions']:
            self.assertEqual(
                [(d['category'], d['ordinal'], d['name'], d['role'])
                 for d in version['declarations']],
                [('creator', 1, 'J. Lee', 'aut')])
            self.assertEqual(len(version['source_sha256']), 64)

    def test_new_version_with_different_contributors_keeps_old_queryable(self):
        newer = fixture.build(
            self.root / 'a2.epub', 'urn:authors:a',
            C('creator', 'Dana Ruiz', 'edt'), title='Northern Lights')
        ingest(newer, self.lee_db)
        # v2 no longer names "J. Lee"; v1 still does, and remains queryable.
        add_alias(self.lee_db, self.jordan, 'J. Lee')
        book = author_books(self.lee_db, self.jordan)['books'][0]
        self.assertEqual([v['version_id'] for v in book['versions']], [1])
        all_versions = query_declarations(self.library())
        self.assertIn(2, {d['version_id'] for d in all_versions})
        self.assertEqual(
            [d['name'] for d in all_versions if d['version_id'] == 2],
            ['Dana Ruiz'])

    def test_version_filter_scopes_declarations(self):
        newer = fixture.build(
            self.root / 'a2.epub', 'urn:authors:a',
            C('creator', 'Dana Ruiz', 'edt'), title='Northern Lights')
        ingest(newer, self.lee_db)
        self.assertEqual(
            [d['name'] for d in list_declarations(self.lee_db, 1)],
            ['J. Lee', 'Robin Fox', 'Casey Ng'])
        self.assertEqual(
            [d['name'] for d in list_declarations(self.lee_db, 2)], ['Dana Ruiz'])

    def test_query_never_returns_stale_attribution(self):
        add_alias(self.lee_db, self.jordan, 'J. Lee')
        before = query_declarations(self.library(), self.lee_db)
        self.assertEqual(before[0]['resolution']['author_id'], self.jordan)
        remove_alias(self.lee_db, self.jordan, 'J. Lee')
        after = query_declarations(self.library(), self.lee_db)
        self.assertEqual(after[0]['resolution']['status'], 'unmapped')
        self.assertEqual(after[0]['name'], 'J. Lee')
        # The same declaration text and version identity survive both states.
        self.assertEqual([d['declaration_id'] for d in before],
                         [d['declaration_id'] for d in after])

    def test_switching_current_version_does_not_drop_old_metadata(self):
        newer = fixture.build(
            self.root / 'a2.epub', 'urn:authors:a',
            C('creator', 'Dana Ruiz', 'edt'), title='Northern Lights',
            chapters=[('Chapter One', 'Revised harbor prose entirely.')])
        ingest(newer, self.lee_db)
        add_alias(self.lee_db, self.jordan, 'J. Lee')
        self.assertEqual(
            [d['name'] for d in list_declarations(self.lee_db, 1)],
            ['J. Lee', 'Robin Fox', 'Casey Ng'])
        select_version(self.lee_db, 1)
        # The old version now being current changes nothing about declarations.
        self.assertEqual(
            [d['name'] for d in list_declarations(self.lee_db)],
            ['J. Lee', 'Robin Fox', 'Casey Ng', 'Dana Ruiz'])
        book = author_books(self.lee_db, self.jordan)['books'][0]
        self.assertEqual([v['version_id'] for v in book['versions']], [1])
        self.assertTrue(book['versions'][0]['current'])

    def test_contributor_category_is_queryable_separately(self):
        add_alias(self.lee_db, self.other, 'Casey Ng')
        rows = unresolved_declarations(self.lee_db)
        self.assertNotIn('Casey Ng', [r['name'] for r in rows])
        book = author_books(self.lee_db, self.other)['books'][0]
        declaration = next(d for d in book['versions'][0]['declarations']
                           if d['category'] == 'contributor')
        self.assertEqual(
            (declaration['category'], declaration['ordinal'], declaration['role']),
            ('contributor', 1, 'trl'))


class SearchRegressionTests(AuthorFixture):
    def test_search_and_version_behaviour_unchanged_by_catalogue(self):
        add_alias(self.lee_db, self.jordan, 'J. Lee')
        hits = search(self.lee_db, 'harbor')
        self.assertEqual(len(hits), 1)
        select_version(self.lee_db, 1)
        self.assertEqual(search(self.lee_db, 'harbor')[0]['position'], 1)


class LegacyArchiveTests(unittest.TestCase):
    def test_schema_two_archive_gains_catalogue_on_first_write(self):
        # Simulate an archive imported before the catalogue existed.
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            db = root / 'old.sqlite'
            epub = fixture.build(root / 'book.epub', 'urn:authors:legacy',
                                 C('creator', 'Legacy Name', 'aut'), title='Legacy')
            ingest(epub, db)
            conn = open_connection(db)
            try:
                for table in ('authors', 'author_aliases',
                              'contributor_declarations'):
                    conn.execute('DROP TABLE ' + table)
                conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
            finally:
                conn.close()
            # Reads stay read-only and report an empty catalogue.
            self.assertEqual(list_declarations(db), [])
            self.assertEqual(list_authors(db), [])
            # A new version upgrades the archive in its import transaction and
            # records its declarations; the old version remains searchable.
            ingest(fixture.build(root / 'book2.epub', 'urn:authors:legacy',
                                 C('creator', 'Legacy Name', 'aut'),
                                 title='Legacy',
                                 chapters=[('Chapter One',
                                            'Different harbor prose entirely.')]), db)
            rows = list_declarations(db)
            self.assertEqual([(r['version_id'], r['name']) for r in rows],
                             [(2, 'Legacy Name')])
            self.assertEqual(len(search(db, 'harbor')), 1)


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run(
            [sys.executable, '-m', 'epub_index', *map(str, args)],
            capture_output=True, text=True, timeout=10,
            env={**__import__('os').environ, 'PYTHONPATH': str(ROOT / 'src')})

    def test_full_catalogue_workflow(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            a = fixture.build(root / 'a.epub', 'urn:authors:a',
                              C('creator', 'J. Lee', 'aut'), title='Northern Lights')
            b = fixture.build(root / 'b.epub', 'urn:authors:b',
                              C('creator', 'Jordan Lee', 'aut'), title='Southern Tides')
            db_a, db_b = root / 'a.sqlite', root / 'b.sqlite'
            self.assertEqual(self.invoke('import', '--source', a, '--db', db_a).returncode, 0)
            self.assertEqual(self.invoke('import', '--source', b, '--db', db_b).returncode, 0)

            created = json.loads(self.invoke('author-add', '--db', db_a,
                                             '--name', 'Jordan Lee').stdout)
            author_id = created['author_id']
            listed = json.loads(self.invoke('authors', '--db', db_a).stdout)
            self.assertEqual(listed[0]['display_name'], 'Jordan Lee')

            before = json.loads(self.invoke(
                'author-books', '--db', db_a, '--db', db_b,
                '--author', author_id).stdout)
            self.assertEqual([x['title'] for x in before['books']], ['Southern Tides'])

            self.assertEqual(self.invoke('alias-add', '--db', db_a, '--author',
                                         author_id, '--alias', 'J. Lee').returncode, 0)
            after = json.loads(self.invoke(
                'author-books', '--db', db_a, '--db', db_b,
                '--author', author_id).stdout)
            self.assertEqual(sorted(x['title'] for x in after['books']),
                             ['Northern Lights', 'Southern Tides'])

            unresolved = json.loads(self.invoke(
                'unresolved', '--db', db_a, '--db', db_b).stdout)
            self.assertNotIn('J. Lee', [x['name'] for x in unresolved])

            self.assertEqual(self.invoke('alias-remove', '--db', db_a, '--author',
                                         author_id, '--alias', 'J. Lee').returncode, 0)
            unresolved = json.loads(self.invoke(
                'unresolved', '--db', db_a, '--db', db_b).stdout)
            entry = next(x for x in unresolved if x['name'] == 'J. Lee')
            self.assertEqual(entry['category'], 'creator')
            self.assertEqual(entry['ordinal'], 1)
            self.assertEqual(entry['role'], 'aut')
            self.assertEqual(entry['version_id'], 1)

    def test_queries_do_not_create_missing_archive(self):
        with tempfile.TemporaryDirectory() as t:
            missing = Path(t) / 'missing.sqlite'
            result = self.invoke('unresolved', '--db', missing)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(missing.exists())

    def test_invalid_alias_input_exits_two(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            epub = fixture.build(root / 'a.epub', 'urn:authors:a',
                                 C('creator', 'J. Lee', 'aut'))
            db = root / 'a.sqlite'
            self.invoke('import', '--source', epub, '--db', db)
            author_id = json.loads(self.invoke('author-add', '--db', db,
                                               '--name', 'X').stdout)['author_id']
            for alias in (' J. Lee', '   ', ''):
                result = self.invoke('alias-add', '--db', db, '--author',
                                     author_id, '--alias', alias)
                self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
