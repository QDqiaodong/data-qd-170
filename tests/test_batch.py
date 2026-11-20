import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from epub_index.batch import intake

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'epub_index'
spec = importlib.util.spec_from_file_location(
    'make_batch', ROOT / 'fixtures/make_batch.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sources = self.root / 'src'
        self.sources.mkdir()
        self.river = fixture.build(
            self.sources / 'river.epub', 'urn:batch:river', 'River Ledger',
            creator='M. Quill',
            paragraphs=['The ferryman counts lanterns along the north bank.',
                        'Every crossing is copied into the river ledger.'])
        self.orchard = fixture.build(
            self.sources / 'orchard.epub', 'urn:batch:orchard',
            'Orchard Minutes', creator='P. Rowan',
            paragraphs=['The quince harvest arrives before the first frost.',
                        'Minutes of the orchard meeting fill two baskets.'])
        self.lantern = fixture.build(
            self.sources / 'lantern.epub', 'urn:batch:lantern', 'Lantern Atlas',
            paragraphs=['A paper lantern drifts above the sleeping canal.',
                        'The atlas marks every jetty in blue ink.'])
        self.target = self.root / 'out'

    def invoke(self, *args):
        return subprocess.run([sys.executable, '-m', MODULE, *map(str, args)],
                              capture_output=True, text=True, timeout=30)

    def run_batch(self, entries, name='manifest.json'):
        manifest = self.root / name
        manifest.write_text(json.dumps(entries), encoding='utf-8')
        return manifest, self.invoke(
            'import-batch', '--manifest', manifest, '--target', self.target)

    def leftovers(self):
        return [p for p in self.root.iterdir() if p.name.startswith('.batch-')]

    def assert_sources_untouched(self, before):
        self.assertEqual({p: sha256(p) for p in before}, before)

    # --- success path ----------------------------------------------------
    def test_success_publishes_each_book_in_manifest_order(self):
        books = [self.river, self.orchard, self.lantern]
        before = {p: sha256(p) for p in books}
        entries = [
            {'source': 'src/orchard.epub'},   # relative to the manifest file
            {'source': str(self.river)},      # absolute
            {'source': 'src/lantern.epub'},
        ]
        manifest, result = self.run_batch(entries)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['action'], 'published')
        self.assertEqual(receipt['book_count'], 3)
        self.assertEqual(receipt['manifest'], str(manifest.resolve()))
        self.assertEqual(receipt['manifest_sha256'], sha256(manifest))
        self.assertEqual(receipt['target'], str(self.target.resolve()))
        self.assertIsInstance(receipt['created_at'], str)
        entries_out = receipt['books']
        # Manifest order and per-item identity are stable.
        self.assertEqual([b['index'] for b in entries_out], [1, 2, 3])
        self.assertEqual([b['identifier'] for b in entries_out],
                         ['urn:batch:orchard', 'urn:batch:river',
                          'urn:batch:lantern'])
        self.assertEqual([b['source'] for b in entries_out],
                         ['src/orchard.epub', str(self.river),
                          'src/lantern.epub'])
        expected = {
            'urn:batch:orchard': (self.orchard, 'Orchard Minutes'),
            'urn:batch:river': (self.river, 'River Ledger'),
            'urn:batch:lantern': (self.lantern, 'Lantern Atlas'),
        }
        for entry in entries_out:
            source_path, title = expected[entry['identifier']]
            self.assertEqual(entry['source_sha256'], sha256(source_path))
            self.assertEqual(entry['title'], title)
            self.assertRegex(entry['archive'],
                             r'^\d{3}-[^/\\]+-[0-9a-f]{12}\.sqlite$')
            self.assertTrue(entry['archive'].startswith(
                f"{entry['index']:03d}-"))
            self.assertIn(entry['source_sha256'][:12], entry['archive'])
        # The receipt inside the published directory matches stdout.
        on_disk = json.loads(
            (self.target / 'receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(on_disk, receipt)
        # Exactly the three archives plus the receipt were published.
        published = sorted(p.name for p in self.target.iterdir())
        self.assertEqual(
            published,
            sorted([b['archive'] for b in entries_out] + ['receipt.json']))
        self.assert_sources_untouched(before)
        # Every archive holds exactly its own book and answers all commands.
        terms = {'urn:batch:orchard': 'quince',
                 'urn:batch:river': 'ferryman',
                 'urn:batch:lantern': 'jetty'}
        for entry in entries_out:
            db = self.target / entry['archive']
            versions = json.loads(self.invoke('versions', '--db', db).stdout)
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0]['source_sha256'],
                             entry['source_sha256'])
            self.assertTrue(versions[0]['current'])
            conn = sqlite3.connect(db)
            try:
                rows = conn.execute('SELECT identifier, title FROM book').fetchall()
            finally:
                conn.close()
            self.assertEqual(rows, [(entry['identifier'], entry['title'])])
            own = terms[entry['identifier']]
            hits = json.loads(
                self.invoke('search', '--db', db, '--query', own).stdout)
            self.assertTrue(hits)
            for other in terms.values():
                if other == own:
                    continue
                misses = json.loads(
                    self.invoke('search', '--db', db, '--query', other).stdout)
                self.assertEqual(misses, [])
        # Annotations, version selection and the author catalogue all work.
        river_db = self.target / entries_out[1]['archive']
        note = self.invoke('annotate', '--db', river_db, '--chapter', '1',
                           '--excerpt', 'ferryman counts lanterns',
                           '--note', 'ink smudged here')
        self.assertEqual(note.returncode, 0, note.stderr)
        hits = json.loads(
            self.invoke('search', '--db', river_db, '--query', 'ferryman').stdout)
        self.assertEqual(hits[0]['annotations'][0]['note'], 'ink smudged here')
        select = self.invoke('select', '--db', river_db, '--version', '1')
        self.assertEqual(select.returncode, 0, select.stderr)
        pending = self.invoke('pending', '--db', river_db)
        self.assertEqual(pending.returncode, 0, pending.stderr)
        added = self.invoke('author-add', '--db', river_db, '--name', 'M. Quill')
        self.assertEqual(added.returncode, 0, added.stderr)
        author_id = json.loads(added.stdout)['author_id']
        declarations = json.loads(
            self.invoke('contributors', '--db', river_db).stdout)
        self.assertEqual([d['name'] for d in declarations], ['M. Quill'])
        self.assertEqual(declarations[0]['resolution']['status'], 'resolved')
        owned = json.loads(self.invoke(
            'author-books', '--db', river_db, '--author', str(author_id)).stdout)
        self.assertEqual(owned['books'][0]['identifier'], 'urn:batch:river')

    def test_batch_archive_accepts_later_versions(self):
        _, result = self.run_batch([{'source': 'src/river.epub'}])
        self.assertEqual(result.returncode, 0, result.stderr)
        db = self.target / json.loads(result.stdout)['books'][0]['archive']
        v2 = fixture.build(
            self.sources / 'river-v2.epub', 'urn:batch:river', 'River Ledger',
            creator='M. Quill',
            paragraphs=['The ferryman counts lanterns along the south bank.',
                        'Every crossing is copied into the river ledger.'])
        again = self.invoke('import', '--source', v2, '--db', db)
        self.assertEqual(again.returncode, 0, again.stderr)
        versions = json.loads(self.invoke('versions', '--db', db).stdout)
        self.assertEqual(len(versions), 2)
        self.assertTrue(versions[1]['current'])

    def test_missing_parent_directories_created(self):
        self.target = self.root / 'deep' / 'nest' / 'out'
        _, result = self.run_batch([{'source': 'src/river.epub'}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.target / 'receipt.json').is_file())

    def test_identifier_path_characters_never_steer_output(self):
        tricky = fixture.build(
            self.sources / 'tricky.epub', '../../etc/eps\\olon', 'Tricky Book',
            paragraphs=['A cautious archivist checks every name.',
                        'Nothing escapes the intake directory.'])
        _, result = self.run_batch(
            [{'source': 'src/tricky.epub'}, {'source': 'src/river.epub'}])
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        names = [b['archive'] for b in receipt['books']]
        for name in names:
            self.assertNotIn('/', name)
            self.assertNotIn('\\', name)
            self.assertNotIn('..', name)
            self.assertTrue((self.target / name).is_file())
        self.assertFalse((self.root / 'etc').exists())
        self.assertFalse((self.root / 'eps').exists())
        self.assertEqual(len(list(self.target.iterdir())), 3)

    # --- rejection: whole batch refused, nothing published ---------------
    def test_mixed_valid_and_invalid_rejected(self):
        broken = self.sources / 'broken.epub'
        broken.write_bytes(b'this is not a zip')
        before = {p: sha256(p) for p in (self.river, self.orchard, broken)}
        _, result = self.run_batch([{'source': 'src/river.epub'},
                                    {'source': 'src/broken.epub'},
                                    {'source': 'src/orchard.epub'}])
        self.assertEqual(result.returncode, 2)
        self.assertIn('item 2', result.stderr)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.leftovers(), [])
        self.assert_sources_untouched(before)

    def test_last_item_failure_leaves_nothing(self):
        broken = self.sources / 'broken.epub'
        broken.write_bytes(b'this is not a zip')
        before = {p: sha256(p) for p in (self.river, self.orchard, broken)}
        _, result = self.run_batch([{'source': 'src/river.epub'},
                                    {'source': 'src/orchard.epub'},
                                    {'source': 'src/broken.epub'}])
        self.assertEqual(result.returncode, 2)
        self.assertIn('item 3', result.stderr)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.leftovers(), [])
        self.assert_sources_untouched(before)

    def test_duplicate_identifier_rejected(self):
        fixture.build(self.sources / 'twin.epub', 'urn:batch:river',
                      'River Ledger Revised',
                      paragraphs=['A revised crossing list begins the season.',
                                  'New ink settles over old routes.'])
        _, result = self.run_batch([{'source': 'src/river.epub'},
                                    {'source': 'src/twin.epub'}])
        self.assertEqual(result.returncode, 2)
        self.assertIn('item 2', result.stderr)
        self.assertIn('item 1', result.stderr)
        self.assertIn('identifier', result.stderr)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.leftovers(), [])

    def test_same_source_listed_twice_rejected(self):
        _, result = self.run_batch([{'source': 'src/river.epub'},
                                    {'source': 'src/../src/river.epub'}])
        self.assertEqual(result.returncode, 2)
        self.assertIn('item 2', result.stderr)
        self.assertIn('duplicate source file', result.stderr)
        self.assertFalse(self.target.exists())

    def test_same_content_different_path_rejected(self):
        shutil.copyfile(self.river, self.sources / 'river-copy.epub')
        _, result = self.run_batch([{'source': 'src/river.epub'},
                                    {'source': 'src/river-copy.epub'}])
        self.assertEqual(result.returncode, 2)
        self.assertIn('item 2', result.stderr)
        self.assertIn('SHA-256', result.stderr)
        self.assertFalse(self.target.exists())

    def test_malicious_epub_rejected(self):
        evil = self.sources / 'evil.epub'
        fixture.build(evil, 'urn:batch:evil', 'Evil',
                      paragraphs=['x', 'y'])
        with zipfile.ZipFile(evil) as archive:
            rows = {r.filename: archive.read(r) for r in archive.infolist()}
        rows['../escape.xhtml'] = b'x'
        with zipfile.ZipFile(evil, 'w') as archive:
            for name, body in rows.items():
                archive.writestr(name, body)
        _, result = self.run_batch([{'source': 'src/river.epub'},
                                    {'source': 'src/evil.epub'}])
        self.assertEqual(result.returncode, 2)
        self.assertIn('item 2', result.stderr)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.leftovers(), [])

    def test_missing_source_rejected(self):
        _, result = self.run_batch([{'source': 'src/ghost.epub'}])
        self.assertEqual(result.returncode, 2)
        self.assertIn('item 1', result.stderr)
        self.assertFalse(self.target.exists())

    def test_malformed_manifests_rejected(self):
        cases = [
            [],
            {},
            [{'source': 7}],
            [{'source': ''}],
            [{}],
            [{'source': 'src/river.epub', 'label': 'x'}],
            ['src/river.epub'],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                _, result = self.run_batch(entries)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.target.exists())
                self.assertEqual(self.leftovers(), [])

    # --- target conflicts --------------------------------------------------
    def test_existing_target_directory_refused(self):
        self.target.mkdir()
        marker = self.target / 'keep.txt'
        marker.write_text('keep', encoding='utf-8')
        _, result = self.run_batch([{'source': 'src/river.epub'}])
        self.assertEqual(result.returncode, 2)
        self.assertIn('already exists', result.stderr)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')

    def test_existing_target_file_refused(self):
        self.target.write_bytes(b'old')
        _, result = self.run_batch([{'source': 'src/river.epub'}])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.target.read_bytes(), b'old')

    def test_target_symlink_refused(self):
        real = self.root / 'real'
        real.mkdir()
        self.target.symlink_to(real)
        _, result = self.run_batch([{'source': 'src/river.epub'}])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(real.iterdir()), [])

    # --- publish interruption and cleanup ----------------------------------
    def test_publish_failure_removes_temporary_directory(self):
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps(
            [{'source': 'src/river.epub'}, {'source': 'src/orchard.epub'}]),
            encoding='utf-8')
        before = {p: sha256(p) for p in (self.river, self.orchard)}
        with mock.patch('epub_index.batch.os.rename',
                        side_effect=OSError('simulated publish failure')):
            with self.assertRaises(OSError):
                intake(manifest, self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.leftovers(), [])
        self.assert_sources_untouched(before)

    def test_cleanup_failure_reports_residue_without_claiming_success(self):
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps([{'source': 'src/river.epub'}]),
                            encoding='utf-8')
        with mock.patch('epub_index.batch.os.rename',
                        side_effect=OSError('simulated publish failure')):
            with mock.patch('epub_index.batch.shutil.rmtree',
                            side_effect=OSError('simulated cleanup failure')):
                with self.assertRaises(RuntimeError) as caught:
                    intake(manifest, self.target)
        message = str(caught.exception)
        self.assertIn('not published', message)
        residue = self.leftovers()
        self.assertEqual(len(residue), 1)
        self.assertIn(str(residue[0]), message)
        self.assertFalse(self.target.exists())
        shutil.rmtree(residue[0])


if __name__ == '__main__':
    unittest.main()
