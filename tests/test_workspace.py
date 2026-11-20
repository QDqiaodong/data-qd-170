"""Journal anthology workspace: plans, licenses, reservations and publication.

These tests exercise the offline editorial workflow end to end against real
SQLite archives and the existing author catalogue, including the spec's
worked example (licenses A/B, reservation order forcing B before A, revocation
before publication), whole-issue atomic reservation, cross-archive author id
isolation, explicit version identity, publish-time re-verification, idempotent
retry and archive-independent anthology retrieval.
"""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from epub_index.archive import ingest
from epub_index.authors import add_alias, create_author, remove_alias
from epub_index.db import open_connection
from epub_index import workspace as ws

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    'make_workspace', ROOT / 'fixtures/make_workspace.py')
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)
C = fixture.contributor_element


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # One two-chapter book with a single creator, plus the catalogue.
        self.epub = fixture.build(
            self.root / 'book.epub', 'urn:journal:book',
            C('creator', 'J. Lee', 'aut'), chapter_count=2)
        self.db = self.root / 'lib.sqlite'
        ingest(self.epub, self.db)
        self.lee = create_author(self.db, 'Jordan Lee')['author_id']
        add_alias(self.db, self.lee, 'J. Lee')
        conn = open_connection(self.db)
        try:
            self.sha = conn.execute(
                'SELECT source_sha256 FROM versions ORDER BY id LIMIT 1'
            ).fetchone()[0]
        finally:
            conn.close()
        self.workspace = self.root / 'ws'
        ws.init_workspace(self.workspace, self.db)

    def plan_file(self, name, entries):
        path = self.root / name
        path.write_text(json.dumps(entries), encoding='utf-8')
        return path

    def entry(self, position, *, archive=None, identifier='urn:journal:book',
              sha=None):
        return {
            'archive': str(archive or self.db),
            'identifier': identifier,
            'source_sha256': sha or self.sha,
            'position': position,
        }

    def save(self, issue, positions):
        return ws.save_plan(
            self.workspace, issue,
            self.plan_file('plan-%s.json' % issue,
                           [self.entry(position) for position in positions]))

    def license(self, author, positions, quota=1, *, archive=None,
                identifier='urn:journal:book', sha=None):
        return ws.register_license(
            self.workspace, self.db, author, archive or self.db,
            identifier, sha or self.sha, positions, quota)


class WorkspaceInitTests(WorkspaceCase):
    def test_init_creates_database_and_anthology_directory(self):
        result = ws.init_workspace(self.root / 'second', self.db)
        self.assertEqual(result['action'], 'initialized')
        self.assertTrue((self.root / 'second' / 'workspace.sqlite').is_file())
        self.assertTrue((self.root / 'second' / 'anthologies').is_dir())

    def test_init_refuses_missing_catalogue_and_double_init(self):
        with self.assertRaises(ValueError):
            ws.init_workspace(self.root / 'x', self.root / 'missing.sqlite')
        with self.assertRaises(ValueError):
            ws.init_workspace(self.workspace, self.db)

    def test_init_requires_a_catalogue_with_authors(self):
        plain = fixture.build(
            self.root / 'plain.epub', 'urn:journal:plain',
            C('creator', 'Nobody Home', 'aut'))
        plain_db = self.root / 'plain.sqlite'
        ingest(plain, plain_db)
        with self.assertRaisesRegex(ValueError, 'no canonical authors'):
            ws.init_workspace(self.root / 'empty-cat', plain_db)


class PlanValidationTests(WorkspaceCase):
    def test_plan_verifies_identifier_sha_and_position_separately(self):
        # Wrong SHA even though the archive and position exist.
        bad_sha = self.plan_file('bad-sha.json', [self.entry(1, sha='f' * 64)])
        with self.assertRaisesRegex(ValueError, 'selected version'):
            ws.save_plan(self.workspace, 1, bad_sha)
        # Wrong identifier even though the SHA exists in this archive.
        bad_id = self.plan_file('bad-id.json',
                                [self.entry(1, identifier='urn:other')])
        with self.assertRaisesRegex(ValueError, 'identifier mismatch'):
            ws.save_plan(self.workspace, 1, bad_id)
        # Position absent from the explicitly selected version.
        bad_pos = self.plan_file('bad-pos.json', [self.entry(99)])
        with self.assertRaisesRegex(ValueError, 'chapter position 99'):
            ws.save_plan(self.workspace, 1, bad_pos)

    def test_duplicate_chapter_in_one_issue_rejected(self):
        path = self.plan_file('dup.json', [self.entry(1), self.entry(1)])
        with self.assertRaisesRegex(ValueError, 'appears twice'):
            ws.save_plan(self.workspace, 1, path)

    def test_same_chapter_in_two_issues_is_allowed(self):
        self.save(1, [1])
        self.save(2, [1])
        issues = ws.list_plans(self.workspace)
        self.assertEqual([item['issue'] for item in issues], [1, 2])

    def test_malformed_plan_files_rejected(self):
        for payload in ([], {}, [{}], [{'archive': 'x'}],
                        [{'archive': str(self.db),
                          'identifier': 'urn:journal:book',
                          'source_sha256': 'abc', 'position': 1}]):
            path = self.plan_file('malformed.json', payload)
            with self.assertRaises(ValueError):
                ws.save_plan(self.workspace, 1, path)

    def test_reserved_plan_cannot_be_overwritten_without_withdraw(self):
        self.license(self.lee, [1], 1)
        self.save(1, [1])
        ws.reserve_plan(self.workspace, 1, self.db)
        with self.assertRaisesRegex(ValueError, 'withdraw'):
            self.save(1, [2])
        ws.withdraw_plan(self.workspace, 1)
        self.save(1, [2])
        self.assertEqual(
            ws.list_plans(self.workspace, 1)['chapters'][0]['position'], 2)

    def test_relative_archive_path_resolves_against_plan_directory(self):
        path = self.plan_file(
            'relative.json',
            [{'archive': self.db.name, 'identifier': 'urn:journal:book',
              'source_sha256': self.sha, 'position': 1}])
        ws.save_plan(self.workspace, 1, path)
        self.assertEqual(
            ws.list_plans(self.workspace, 1)['chapters'][0]['archive'],
            str(self.db.resolve()))


class AuthorResolutionTests(WorkspaceCase):
    def test_unmapped_creator_blocks_reservation_but_plan_save_succeeds(self):
        # A fresh archive whose creator has no catalogue mapping.
        epub = fixture.build(
            self.root / 'other.epub', 'urn:journal:other',
            C('creator', 'Unknown Writer', 'aut'))
        other_db = self.root / 'other.sqlite'
        ingest(epub, other_db)
        conn = open_connection(other_db)
        try:
            sha = conn.execute(
                'SELECT source_sha256 FROM versions').fetchone()[0]
        finally:
            conn.close()
        path = self.plan_file(
            'other.json',
            [self.entry(1, archive=other_db, identifier='urn:journal:other',
                        sha=sha)])
        ws.save_plan(self.workspace, 1, path)
        with self.assertRaisesRegex(ValueError, 'unmapped'):
            ws.reserve_plan(self.workspace, 1, self.db)
        # The failed attempt is recorded with its reason and stays a draft.
        plan = ws.list_plans(self.workspace, 1)
        self.assertEqual(plan['status'], 'draft')
        self.assertTrue(plan['reason'])
        self.assertEqual(plan['events'][-1]['kind'], 'reserve_failed')

    def test_ambiguous_creator_blocks_reservation(self):
        rival = create_author(self.db, 'Rival Identity')['author_id']
        add_alias(self.db, rival, 'J. Lee')
        self.save(1, [1])
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            ws.reserve_plan(self.workspace, 1, self.db)

    def test_contributor_does_not_create_a_demand(self):
        epub = fixture.build(
            self.root / 'mixed.epub', 'urn:journal:mixed',
            C('creator', 'Jordan Lee', 'aut')
            + C('contributor', 'Casey Ng', 'trl'))
        db = self.root / 'mixed.sqlite'
        ingest(epub, db)
        conn = open_connection(db)
        try:
            sha = conn.execute(
                'SELECT source_sha256 FROM versions').fetchone()[0]
        finally:
            conn.close()
        path = self.plan_file(
            'mixed.json',
            [self.entry(1, archive=db, identifier='urn:journal:mixed', sha=sha)])
        ws.save_plan(self.workspace, 1, path)
        ws.register_license(self.workspace, self.db, self.lee, db,
                            'urn:journal:mixed', sha, [1], 1)
        ws.reserve_plan(self.workspace, 1, self.db)
        result = ws.publish_issue(self.workspace, 1, self.db)
        self.assertEqual(
            [author['author_id'] for author in result['chapters'][0]['authors']],
            [self.lee])

    def test_version_with_no_creator_is_rejected(self):
        # A contributor-only book has no creator declaration.
        epub = fixture.build(
            self.root / 'nocreator.epub', 'urn:journal:nocreator',
            C('contributor', 'Only Helper', 'trl'))
        db = self.root / 'nocreator.sqlite'
        ingest(epub, db)
        conn = open_connection(db)
        try:
            sha = conn.execute(
                'SELECT source_sha256 FROM versions').fetchone()[0]
        finally:
            conn.close()
        path = self.plan_file(
            'nocreator.json',
            [self.entry(1, archive=db, identifier='urn:journal:nocreator',
                        sha=sha)])
        ws.save_plan(self.workspace, 1, path)
        with self.assertRaisesRegex(ValueError, 'no creator'):
            ws.reserve_plan(self.workspace, 1, self.db)

    def test_cross_archive_author_ids_are_not_confused(self):
        # The book archive's own author id 1 is a different person; only the
        # catalogue's id 1 may be used.
        epub = fixture.build(
            self.root / 'separate.epub', 'urn:journal:separate',
            C('creator', 'J. Lee', 'aut'))
        book_db = self.root / 'separate.sqlite'
        ingest(epub, book_db)
        # Two local identities inside the book archive; id 2 has no counterpart
        # in the single-author catalogue, so it must never resolve there.
        create_author(book_db, 'Unrelated Person')
        foreign_id = create_author(book_db, 'Second Local Person')['author_id']
        self.assertEqual(foreign_id, 2)
        conn = open_connection(book_db)
        try:
            sha = conn.execute(
                'SELECT source_sha256 FROM versions').fetchone()[0]
        finally:
            conn.close()
        path = self.plan_file(
            'separate.json',
            [self.entry(1, archive=book_db, identifier='urn:journal:separate',
                        sha=sha)])
        ws.save_plan(self.workspace, 1, path)
        with self.assertRaisesRegex(ValueError, 'unknown author identity'):
            ws.register_license(self.workspace, self.db, foreign_id, book_db,
                                'urn:journal:separate', sha, [1], 1)
        # The catalogue author resolves the declaration through its alias.
        ws.register_license(self.workspace, self.db, self.lee, book_db,
                            'urn:journal:separate', sha, [1], 1)
        ws.reserve_plan(self.workspace, 1, self.db)
        result = ws.publish_issue(self.workspace, 1, self.db)
        self.assertEqual(result['chapters'][0]['authors'][0]['author_id'],
                         self.lee)

    def test_wrong_catalogue_argument_rejected(self):
        other = fixture.build(
            self.root / 'cat2.epub', 'urn:journal:cat2',
            C('creator', 'Jordan Lee', 'aut'))
        other_db = self.root / 'cat2.sqlite'
        ingest(other, other_db)
        create_author(other_db, 'Jordan Lee')
        self.save(1, [1])
        with self.assertRaisesRegex(ValueError, 'does not match'):
            ws.reserve_plan(self.workspace, 1, other_db)


class LicenseRegistrationTests(WorkspaceCase):
    def test_registration_returns_immutable_scope_and_unique_code(self):
        first = self.license(self.lee, [1, 2], 3)
        second = self.license(self.lee, [1], 1)
        self.assertNotEqual(first['license_code'], second['license_code'])
        self.assertEqual(first['positions'], [1, 2])
        self.assertEqual(first['quota'], 3)
        self.assertEqual(first['status'], 'active')

    def test_invalid_registrations_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'quota'):
            self.license(self.lee, [1], 0)
        with self.assertRaisesRegex(ValueError, 'positive integer'):
            self.license(self.lee, [1], -2)
        with self.assertRaisesRegex(ValueError, 'unknown author'):
            self.license(999, [1], 1)
        with self.assertRaisesRegex(ValueError, 'position'):
            self.license(self.lee, [99], 1)
        with self.assertRaisesRegex(ValueError, 'must not repeat'):
            ws.register_license(self.workspace, self.db, self.lee, self.db,
                                'urn:journal:book', self.sha, [1, 1], 1)
        with self.assertRaisesRegex(ValueError, 'selected version'):
            ws.register_license(self.workspace, self.db, self.lee, self.db,
                                'urn:journal:book', 'a' * 64, [1], 1)

    def test_license_listing_shows_scope_status_and_usage_records(self):
        self.license(self.lee, [1], 1)
        self.save(1, [1])
        ws.reserve_plan(self.workspace, 1, self.db)
        ws.publish_issue(self.workspace, 1, self.db)
        record = ws.list_licenses(self.workspace)[0]
        self.assertEqual(record['used'], 1)
        self.assertEqual(record['remaining'], 0)
        self.assertEqual(record['usages'][0]['issue'], 1)
        self.assertEqual(record['usages'][0]['position'], 1)
        self.assertEqual(record['usages'][0]['license_code'],
                         record['license_code'])


class ReservationTests(WorkspaceCase):
    def test_saving_a_plan_consumes_no_quota(self):
        self.license(self.lee, [1], 1)
        self.save(1, [1])
        self.assertEqual(ws.list_licenses(self.workspace)[0]['used'], 0)
        self.assertEqual(ws.list_plans(self.workspace, 1)['status'], 'draft')

    def test_reservation_records_sequence_and_resolved_authors(self):
        self.license(self.lee, [1, 2], 2)
        self.save(1, [1])
        self.save(2, [2])
        first = ws.reserve_plan(self.workspace, 1, self.db)
        second = ws.reserve_plan(self.workspace, 2, self.db)
        self.assertEqual((first['reservation_seq'], second['reservation_seq']),
                         (1, 2))
        plan = ws.list_plans(self.workspace, 1)
        self.assertEqual(plan['status'], 'reserved')
        self.assertEqual(plan['chapters'][0]['authors'],
                         [{'author_id': self.lee, 'name': 'Jordan Lee'}])

    def test_failed_reservation_never_displaces_earlier_ones(self):
        # One quota unit shared by two single-chapter issues.
        self.license(self.lee, [1], 1)
        self.save(1, [1])
        self.save(2, [1])
        ws.reserve_plan(self.workspace, 1, self.db)
        with self.assertRaisesRegex(ValueError, 'insufficient'):
            ws.reserve_plan(self.workspace, 2, self.db)
        statuses = {row['issue']: row['status']
                    for row in ws.list_plans(self.workspace)}
        self.assertEqual(statuses, {1: 'reserved', 2: 'draft'})
        self.assertTrue(ws.list_plans(self.workspace, 2)['reason'])

    def test_withdraw_releases_quota_and_rejoin_goes_to_queue_tail(self):
        self.license(self.lee, [1], 2)
        self.save(1, [1])
        self.save(2, [1])
        ws.reserve_plan(self.workspace, 1, self.db)
        ws.reserve_plan(self.workspace, 2, self.db)
        ws.withdraw_plan(self.workspace, 1)
        # Re-reserving issue 1 queues it after issue 2.
        new_seq = ws.reserve_plan(self.workspace, 1, self.db)['reservation_seq']
        seqs = {row['issue']: row['reservation_seq']
                for row in ws.list_plans(self.workspace)}
        self.assertGreater(seqs[1], seqs[2])
        self.assertEqual(new_seq, seqs[1])

    def test_concurrent_reservations_serialize_without_double_spending(self):
        self.license(self.lee, [1], 1)
        self.save(3, [1])
        self.save(4, [1])
        outcomes = {}

        def worker(issue):
            try:
                ws.reserve_plan(self.workspace, issue, self.db)
                outcomes[issue] = 'reserved'
            except ValueError:
                outcomes[issue] = 'rejected'

        threads = [threading.Thread(target=worker, args=(issue,))
                   for issue in (3, 4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes.values()), ['rejected', 'reserved'])


class MultiAuthorTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        # Two creators on every chapter; both must be licensed per chapter.
        epub = fixture.build(
            self.root / 'duo.epub', 'urn:journal:duo',
            C('creator', 'Jordan Lee', 'aut')
            + C('creator', 'Robin Fox', 'aut'), chapter_count=2)
        self.duo_db = self.root / 'duo.sqlite'
        ingest(epub, self.duo_db)
        self.fox = create_author(self.db, 'Robin Fox')['author_id']
        conn = open_connection(self.duo_db)
        try:
            self.duo_sha = conn.execute(
                'SELECT source_sha256 FROM versions').fetchone()[0]
        finally:
            conn.close()

    def duo_entry(self, position):
        return {'archive': str(self.duo_db),
                'identifier': 'urn:journal:duo',
                'source_sha256': self.duo_sha, 'position': position}

    def test_whole_issue_returns_when_any_single_demand_is_unmet(self):
        path = self.plan_file('duo.json', [self.duo_entry(1)])
        ws.save_plan(self.workspace, 1, path)
        # Only Lee is licensed; Fox demand cannot be met.
        ws.register_license(self.workspace, self.db, self.lee, self.duo_db,
                            'urn:journal:duo', self.duo_sha, [1], 1)
        with self.assertRaisesRegex(ValueError, 'insufficient'):
            ws.reserve_plan(self.workspace, 1, self.db)
        plan = ws.list_plans(self.workspace, 1)
        self.assertEqual(plan['status'], 'draft')
        # No partial reservation snapshot is left behind as a demand source.
        self.assertEqual(plan['chapters'][0]['authors'], [])

    def test_each_author_needs_a_separate_quota_unit(self):
        path = self.plan_file('duo2.json', [self.duo_entry(1)])
        ws.save_plan(self.workspace, 1, path)
        ws.register_license(self.workspace, self.db, self.lee, self.duo_db,
                            'urn:journal:duo', self.duo_sha, [1], 1)
        ws.register_license(self.workspace, self.db, self.fox, self.duo_db,
                            'urn:journal:duo', self.duo_sha, [1], 1)
        ws.reserve_plan(self.workspace, 1, self.db)
        result = ws.publish_issue(self.workspace, 1, self.db)
        codes = sorted(author['license_code']
                       for author in result['chapters'][0]['authors'])
        self.assertEqual(len(codes), 2)

    def test_duplicate_canonical_author_from_two_declarations_counts_once(self):
        # Both creator declarations resolve to the same canonical identity.
        epub = fixture.build(
            self.root / 'alias.epub', 'urn:journal:alias',
            C('creator', 'Jordan Lee', 'aut')
            + C('creator', 'J. Lee', 'aut'))
        db = self.root / 'alias.sqlite'
        ingest(epub, db)
        conn = open_connection(db)
        try:
            sha = conn.execute(
                'SELECT source_sha256 FROM versions').fetchone()[0]
        finally:
            conn.close()
        path = self.plan_file(
            'alias.json',
            [{'archive': str(db), 'identifier': 'urn:journal:alias',
              'source_sha256': sha, 'position': 1}])
        ws.save_plan(self.workspace, 1, path)
        ws.register_license(self.workspace, self.db, self.lee, db,
                            'urn:journal:alias', sha, [1], 1)
        ws.reserve_plan(self.workspace, 1, self.db)
        result = ws.publish_issue(self.workspace, 1, self.db)
        # One canonical author, one license unit despite two declarations.
        self.assertEqual(len(result['chapters'][0]['authors']), 1)


class WorkedExampleTests(WorkspaceCase):
    """The spec's A/B license example and its pre-publication revocation."""

    def test_publishing_order_forces_b_then_a(self):
        # License A covers chapters 1 and 2; license B covers chapter 1 only.
        self.license(self.lee, [1, 2], 1)   # LIC-0001 = A
        self.license(self.lee, [1], 1)      # LIC-0002 = B
        self.save(1, [1])                   # issue 1 = chapter 甲 (1)
        self.save(2, [2])                   # issue 2 = chapter 乙 (2)
        ws.reserve_plan(self.workspace, 1, self.db)
        ws.reserve_plan(self.workspace, 2, self.db)
        issue_one = ws.publish_issue(self.workspace, 1, self.db)
        self.assertEqual(
            issue_one['chapters'][0]['authors'][0]['license_code'], 'LIC-0002')
        issue_two = ws.publish_issue(self.workspace, 2, self.db)
        self.assertEqual(
            issue_two['chapters'][0]['authors'][0]['license_code'], 'LIC-0001')

    def test_revoking_b_before_publication_keeps_issue_one_and_returns_issue_two(
            self):
        self.license(self.lee, [1, 2], 1)   # A
        self.license(self.lee, [1], 1)      # B
        self.save(1, [1])
        self.save(2, [2])
        ws.reserve_plan(self.workspace, 1, self.db)
        ws.reserve_plan(self.workspace, 2, self.db)
        result = ws.revoke_license(
            self.workspace, 'LIC-0002', 'clerk revocation')
        self.assertEqual(result['retained_issues'], [1])
        self.assertEqual(result['returned_issues'], [2])
        statuses = {row['issue']: row['status']
                    for row in ws.list_plans(self.workspace)}
        self.assertEqual(statuses, {1: 'reserved', 2: 'draft'})
        plan_two = ws.list_plans(self.workspace, 2)
        self.assertIn('LIC-0002', plan_two['reason'])
        self.assertEqual(plan_two['events'][-1]['kind'], 'returned')
        # Issue 1 still publishes using the surviving license A.
        issue_one = ws.publish_issue(self.workspace, 1, self.db)
        self.assertEqual(
            issue_one['chapters'][0]['authors'][0]['license_code'], 'LIC-0001')

    def test_revocation_does_not_return_an_issue_covered_elsewhere(self):
        # Two licenses both cover chapter 1; revoking one leaves enough.
        self.license(self.lee, [1], 1)
        self.license(self.lee, [1], 1)
        self.save(1, [1])
        ws.reserve_plan(self.workspace, 1, self.db)
        result = ws.revoke_license(self.workspace, 'LIC-0001')
        self.assertEqual(result['returned_issues'], [])
        self.assertEqual(
            ws.list_plans(self.workspace, 1)['status'], 'reserved')

    def test_revocation_cascade_respects_priority_and_freed_capacity(self):
        # A covers 1,2 (q1); B covers 1 (q1); C covers 2 (q1).
        self.license(self.lee, [1, 2], 1)   # A
        self.license(self.lee, [1], 1)      # B
        self.license(self.lee, [2], 1)      # C
        self.save(1, [1])
        self.save(2, [2])
        self.save(3, [1, 2])
        ws.reserve_plan(self.workspace, 1, self.db)
        ws.reserve_plan(self.workspace, 2, self.db)
        # Issue 3 needs two units; total capacity after issues 1 and 2 leaves
        # none, so it cannot reserve yet.
        with self.assertRaises(ValueError):
            ws.reserve_plan(self.workspace, 3, self.db)
        # Revoking B: issue 1 rehomes onto A; issue 2 keeps C; nothing returns.
        result = ws.revoke_license(self.workspace, 'LIC-0002')
        self.assertEqual(result['returned_issues'], [])
        # Now issue 3 still cannot be fully paid (only C's chapter-2 slot and
        # no chapter-1 slot remain); confirming returned capacity is not
        # double-counted is covered by the reservation rejection.
        with self.assertRaises(ValueError):
            ws.reserve_plan(self.workspace, 3, self.db)

    def test_revoked_license_cannot_serve_new_use_but_usage_history_remains(self):
        self.license(self.lee, [1], 1)
        self.save(1, [1])
        ws.reserve_plan(self.workspace, 1, self.db)
        ws.publish_issue(self.workspace, 1, self.db)
        ws.revoke_license(self.workspace, 'LIC-0001')
        record = ws.list_licenses(self.workspace, 'LIC-0001')
        self.assertEqual(record['status'], 'revoked')
        self.assertEqual(len(record['usages']), 1)
        # A second issue cannot spend the revoked license.
        self.save(2, [1])
        with self.assertRaisesRegex(ValueError, 'insufficient'):
            ws.reserve_plan(self.workspace, 2, self.db)


class PublicationTests(WorkspaceCase):
    def test_publish_requires_reservation(self):
        self.license(self.lee, [1], 1)
        self.save(1, [1])
        with self.assertRaisesRegex(ValueError, 'only reserved'):
            ws.publish_issue(self.workspace, 1, self.db)

    def test_published_anthology_freezes_exact_identity_title_and_text(self):
        self.license(self.lee, [1, 2], 1)
        self.save(1, [2])
        ws.reserve_plan(self.workspace, 1, self.db)
        result = ws.publish_issue(self.workspace, 1, self.db)
        chapter = result['chapters'][0]
        self.assertEqual(chapter['identifier'], 'urn:journal:book')
        self.assertEqual(chapter['source_sha256'], self.sha)
        self.assertEqual(chapter['position'], 2)
        self.assertEqual(chapter['title'], 'Chapter 2')
        self.assertEqual(chapter['text'], fixture.chapter_text(2))
        self.assertEqual(chapter['authors'][0]['name'], 'Jordan Lee')
        # The anthology file is committed next to the workspace database.
        on_disk = json.loads(
            Path(result['anthology_path']).read_text(encoding='utf-8'))
        self.assertEqual(on_disk['chapters'], result['chapters'])

    def test_explicitly_selected_old_version_ignores_current_version(self):
        v2 = fixture.build(
            self.root / 'v2.epub', 'urn:journal:book',
            C('creator', 'J. Lee', 'aut'), chapter_count=3, word='quay')
        ingest(v2, self.db)  # becomes current; v1 keeps only two chapters
        self.license(self.lee, [1], 1)
        # The plan pins the original v1 SHA and its chapter-1 text.
        self.save(1, [1])
        ws.reserve_plan(self.workspace, 1, self.db)
        result = ws.publish_issue(self.workspace, 1, self.db)
        chapter = result['chapters'][0]
        self.assertEqual(chapter['source_sha256'], self.sha)
        self.assertEqual(chapter['text'], fixture.chapter_text(1))
        # A position existing only in the current v2 cannot be selected on v1.
        path = self.plan_file('future.json', [self.entry(3)])
        with self.assertRaisesRegex(ValueError, 'chapter position 3'):
            ws.save_plan(self.workspace, 2, path)

    def test_publish_rechecks_author_set_and_keeps_reservation_on_failure(self):
        self.license(self.lee, [1], 1)
        self.save(1, [1])
        ws.reserve_plan(self.workspace, 1, self.db)
        rival = create_author(self.db, 'Rival Identity')['author_id']
        add_alias(self.db, rival, 'J. Lee')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            ws.publish_issue(self.workspace, 1, self.db)
        plan = ws.list_plans(self.workspace, 1)
        self.assertEqual(plan['status'], 'reserved')
        self.assertEqual(plan['events'][-1]['kind'], 'publish_rejected')
        self.assertEqual(ws.list_licenses(self.workspace)[0]['used'], 0)
        # Resolving the conflict allows the unchanged reservation to publish.
        remove_alias(self.db, rival, 'J. Lee')
        ws.publish_issue(self.workspace, 1, self.db)

    def test_publish_rejects_a_genuine_author_set_change(self):
        # The creator declaration "R. Fox" first uniquely resolves to one
        # canonical identity; after the alias is reassigned to a different
        # identity it still resolves uniquely, but the author set no longer
        # matches the reserved set.
        epub = fixture.build(
            self.root / 'alias.epub', 'urn:journal:alias',
            C('creator', 'R. Fox', 'aut'))
        db = self.root / 'alias.sqlite'
        ingest(epub, db)
        first = create_author(self.db, 'Robin Fox')['author_id']
        second = create_author(self.db, 'Casey Ng')['author_id']
        add_alias(self.db, first, 'R. Fox')
        conn = open_connection(db)
        try:
            sha = conn.execute(
                'SELECT source_sha256 FROM versions').fetchone()[0]
        finally:
            conn.close()
        path = self.plan_file(
            'alias.json',
            [{'archive': str(db), 'identifier': 'urn:journal:alias',
              'source_sha256': sha, 'position': 1}])
        ws.save_plan(self.workspace, 1, path)
        ws.register_license(self.workspace, self.db, first, db,
                            'urn:journal:alias', sha, [1], 1)
        ws.reserve_plan(self.workspace, 1, self.db)
        # Reassign the exact name to a different single identity.
        remove_alias(self.db, first, 'R. Fox')
        add_alias(self.db, second, 'R. Fox')
        with self.assertRaisesRegex(ValueError, 'author set differs'):
            ws.publish_issue(self.workspace, 1, self.db)
        # The reservation survives and no quota was spent.
        self.assertEqual(
            ws.list_plans(self.workspace, 1)['status'], 'reserved')
        self.assertEqual(ws.list_licenses(self.workspace)[0]['used'], 0)
        # Restoring the mapping lets the unchanged reservation publish.
        remove_alias(self.db, second, 'R. Fox')
        add_alias(self.db, first, 'R. Fox')
        ws.publish_issue(self.workspace, 1, self.db)

    def test_missing_archive_at_publish_rejects_without_deduction(self):
        self.license(self.lee, [1], 1)
        self.save(1, [1])
        ws.reserve_plan(self.workspace, 1, self.db)
        moved = self.root / 'moved.sqlite'
        shutil.move(str(self.db), moved)
        with self.assertRaises(ValueError):
            ws.publish_issue(self.workspace, 1, self.db)
        self.assertEqual(
            ws.list_plans(self.workspace, 1)['status'], 'reserved')
        shutil.move(str(moved), str(self.db))
        ws.publish_issue(self.workspace, 1, self.db)

    def test_publishing_protects_other_reserved_issues_quota(self):
        # Issue 1 needs chapter 1; issue 2 needs chapter 2. Only one license
        # unit covers chapter 2, so issue 1 must not consume the shared A quota.
        self.license(self.lee, [1, 2], 1)   # A: shared
        self.license(self.lee, [1], 1)      # B: chapter 1 only
        self.save(1, [1])
        self.save(2, [2])
        ws.reserve_plan(self.workspace, 1, self.db)
        ws.reserve_plan(self.workspace, 2, self.db)
        ws.publish_issue(self.workspace, 1, self.db)
        # Issue 2 must still publish; feasibility guaranteed B for issue 1.
        issue_two = ws.publish_issue(self.workspace, 2, self.db)
        self.assertEqual(
            issue_two['chapters'][0]['authors'][0]['license_code'], 'LIC-0001')

    def test_repeated_publish_is_idempotent_after_revocation_and_move(self):
        self.license(self.lee, [1], 1)
        self.save(1, [1])
        ws.reserve_plan(self.workspace, 1, self.db)
        first = ws.publish_issue(self.workspace, 1, self.db)
        used_after_first = ws.list_licenses(self.workspace)[0]['used']
        # Revoke the license and move the archive away; replay still returns
        # the frozen anthology and never deducts a second quota unit.
        ws.revoke_license(self.workspace, 'LIC-0001')
        moved = self.root / 'away.sqlite'
        shutil.move(str(self.db), moved)
        replay = ws.publish_issue(self.workspace, 1, self.db)
        self.assertTrue(replay['idempotent_replay'])
        self.assertEqual(replay['chapters'], first['chapters'])
        self.assertEqual(
            ws.list_licenses(self.workspace, 'LIC-0001')['used'],
            used_after_first)
        # The dedicated retrieval command works without the archive as well.
        fetched = ws.get_anthology(self.workspace, 1)
        self.assertEqual(fetched['chapters'], first['chapters'])
        output = self.root / 'copy.json'
        ws.get_anthology(self.workspace, 1, output)
        self.assertTrue(output.is_file())

    def test_anthology_unknown_issue_rejected(self):
        with self.assertRaisesRegex(ValueError, 'no published anthology'):
            ws.get_anthology(self.workspace, 99)

    def test_published_issue_is_immutable_and_not_withdrawable(self):
        self.license(self.lee, [1], 1)
        self.save(1, [1])
        ws.reserve_plan(self.workspace, 1, self.db)
        ws.publish_issue(self.workspace, 1, self.db)
        with self.assertRaisesRegex(ValueError, 'published'):
            ws.withdraw_plan(self.workspace, 1)
        with self.assertRaisesRegex(ValueError, 'published'):
            self.save(1, [2])

    def test_mid_publish_failure_rolls_back_quota_and_publication(self):
        from unittest import mock
        self.license(self.lee, [1], 1)
        self.save(1, [1])
        ws.reserve_plan(self.workspace, 1, self.db)
        # Fail after usage rows would be written but before the commit.
        with mock.patch('epub_index.workspace.os.replace',
                        side_effect=OSError('simulated rename failure')):
            with self.assertRaises(OSError):
                ws.publish_issue(self.workspace, 1, self.db)
        # No formal usage, no publication, reservation retained for a retry.
        self.assertEqual(ws.list_licenses(self.workspace)[0]['used'], 0)
        self.assertEqual(
            ws.list_plans(self.workspace, 1)['status'], 'reserved')
        self.assertIsNone(ws._existing_publication(
            ws._connect(self.workspace / ws.WORKSPACE_DB), 1))
        # No anthology file is left behind.
        self.assertEqual(
            list((self.workspace / ws.ANTHOLOGY_DIR).iterdir()), [])
        # A clean retry now succeeds exactly once.
        result = ws.publish_issue(self.workspace, 1, self.db)
        self.assertEqual(result['issue'], 1)
        self.assertEqual(ws.list_licenses(self.workspace)[0]['used'], 1)


class WorkspaceCliTests(WorkspaceCase):
    def invoke(self, *args):
        return subprocess.run(
            [sys.executable, '-m', 'epub_index', *map(str, args)],
            capture_output=True, text=True, timeout=30,
            env={**__import__('os').environ,
                 'PYTHONPATH': str(ROOT / 'src')})

    def test_full_cli_workflow(self):
        # self.workspace was already initialized in setUp.
        plan = self.plan_file(
            'cli-plan.json', [self.entry(1)])
        self.assertEqual(self.invoke(
            'plan-save', '--workspace', self.workspace, '--issue', '7',
            '--plan', plan).returncode, 0)
        reg = self.invoke(
            'license-register', '--workspace', self.workspace,
            '--catalogue', self.db, '--author', str(self.lee),
            '--archive', self.db, '--identifier', 'urn:journal:book',
            '--version-sha', self.sha, '--positions', '1,2', '--quota', '2')
        self.assertEqual(reg.returncode, 0, reg.stderr)
        code = json.loads(reg.stdout)['license_code']
        self.assertEqual(self.invoke(
            'plan-reserve', '--workspace', self.workspace, '--issue', '7',
            '--catalogue', self.db).returncode, 0)
        published = self.invoke(
            'publish', '--workspace', self.workspace, '--issue', '7',
            '--catalogue', self.db)
        self.assertEqual(published.returncode, 0, published.stderr)
        payload = json.loads(published.stdout)
        self.assertEqual(payload['issue'], 7)
        # Replay returns the same record through the CLI.
        replay = self.invoke(
            'publish', '--workspace', self.workspace, '--issue', '7')
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertTrue(json.loads(replay.stdout)['idempotent_replay'])
        listing = self.invoke(
            'license-list', '--workspace', self.workspace,
            '--license', code)
        self.assertEqual(json.loads(listing.stdout)['used'], 1)
        plans = self.invoke('plan-list', '--workspace', self.workspace)
        self.assertEqual(json.loads(plans.stdout)[0]['status'], 'published')
        fetched = self.invoke(
            'anthology', '--workspace', self.workspace, '--issue', '7')
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertEqual(json.loads(fetched.stdout)['chapters'],
                         payload['chapters'])

    def test_cli_rejections_exit_two_and_follow_json_convention(self):
        missing = self.root / 'no-workspace'
        result = self.invoke('plan-list', '--workspace', missing)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(result.stdout, '')
        # Bad positions argument is rejected like other invalid input.
        ws.init_workspace(self.root / 'w2', self.db)
        bad = self.invoke(
            'license-register', '--workspace', self.root / 'w2',
            '--catalogue', self.db, '--author', str(self.lee),
            '--archive', self.db, '--identifier', 'urn:journal:book',
            '--version-sha', self.sha, '--positions', '0,-1', '--quota', '1')
        self.assertEqual(bad.returncode, 2)


if __name__ == '__main__':
    unittest.main()
