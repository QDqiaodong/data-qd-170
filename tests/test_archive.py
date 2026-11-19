import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from epub_index.extract import extract
from epub_index.archive import ingest,search
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('make_sample',ROOT/'fixtures/make_sample.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.book=fixture.make(self.root/'book.epub');self.db=self.root/'book.sqlite'
    def rewrite(self,transform):
        with zipfile.ZipFile(self.book) as archive:rows={r.filename:archive.read(r) for r in archive.infolist()}
        transform(rows)
        with zipfile.ZipFile(self.book,'w') as archive:
            for name,body in rows.items():archive.writestr(name,body)
    def test_spine_order_and_script_exclusion(self):
        result=extract(self.book)
        self.assertEqual([c['title'] for c in result['chapters']],['Evening','Morning'])
        self.assertNotIn('secret_hidden_marker',' '.join(c['text'] for c in result['chapters']))
    def test_archive_search_is_independent_of_source(self):
        self.assertEqual(ingest(self.book,self.db)['chapter_count'],2);self.book.unlink()
        self.assertEqual([r['title'] for r in search(self.db,'harbor')],['Evening','Morning'])
        self.assertEqual([r['title'] for r in search(self.db,'blue')],['Morning'])
        self.assertEqual(search(self.db,'secret_hidden_marker'),[])
    def test_and_query(self):
        ingest(self.book,self.db)
        self.assertEqual([r['title'] for r in search(self.db,'blue harbor')],['Morning'])
    def test_empty_query_rejected(self):
        ingest(self.book,self.db)
        for raw in ('','***'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):search(self.db,raw)
    def test_path_traversal_rejected(self):
        self.rewrite(lambda rows:rows.update({'../escape.xhtml':b'x'}))
        with self.assertRaisesRegex(ValueError,'unsafe'):extract(self.book)
    def test_missing_spine_member_rejected(self):
        self.rewrite(lambda rows:rows.pop('OPS/two.xhtml'))
        with self.assertRaisesRegex(ValueError,'missing'):extract(self.book)
    def test_external_manifest_rejected(self):
        self.rewrite(lambda rows:rows.update({'OPS/book.opf':rows['OPS/book.opf'].replace(b'one.xhtml',b'https://invalid.test/one.xhtml')}))
        with self.assertRaises(ValueError):extract(self.book)
    def test_dtd_rejected(self):
        self.rewrite(lambda rows:rows.update({'OPS/one.xhtml':b'<!DOCTYPE html>'+rows['OPS/one.xhtml']}))
        with self.assertRaisesRegex(ValueError,'DTD'):extract(self.book)
    def test_non_utf8_xml_rejected(self):
        self.rewrite(lambda rows:rows.update({'OPS/one.xhtml':rows['OPS/one.xhtml'].decode().encode('utf-16')}))
        with self.assertRaises(UnicodeError):extract(self.book)
    def test_existing_database_refused(self):
        ingest(self.book,self.db)
        with self.assertRaises(ValueError):ingest(self.book,self.db)
        self.assertEqual(len(search(self.db,'harbor')),2)
    def test_fixture_is_reproducible(self):
        other=fixture.make(self.root/'other.epub');self.assertEqual(self.book.read_bytes(),other.read_bytes())
    def test_source_digest(self):
        import hashlib
        self.assertEqual(extract(self.book)['source_sha256'],hashlib.sha256(self.book.read_bytes()).hexdigest())
