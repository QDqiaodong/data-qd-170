from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
MODULE='epub_index'
class CliTests(unittest.TestCase):
    def invoke(self,*args):
        return subprocess.run([sys.executable,'-m',MODULE,*map(str,args)],capture_output=True,text=True,timeout=10)
    def source(self):
        return ROOT/'fixtures/sample.epub'
    def test_import_and_query(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'db.sqlite';r=self.invoke('import','--source',self.source(),'--db',p)
            self.assertEqual(r.returncode,0,r.stderr)
            q=self.invoke(*['search', '--query', 'harbor'],'--db',p);self.assertEqual(q.returncode,0,q.stderr);self.assertIn('Evening',q.stdout)
    def test_existing_target_preserved(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'db.sqlite';p.write_bytes(b'old')
            self.assertEqual(self.invoke('import','--source',self.source(),'--db',p).returncode,2);self.assertEqual(p.read_bytes(),b'old')
    def test_missing_source_creates_no_archive(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'db.sqlite'
            self.assertEqual(self.invoke('import','--source',Path(t)/'missing','--db',p).returncode,2);self.assertFalse(p.exists())
    def test_symlink_target_preserved(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'target';p.write_bytes(b'old');link=Path(t)/'link';link.symlink_to(p)
            self.assertEqual(self.invoke('import','--source',self.source(),'--db',link).returncode,2);self.assertEqual(p.read_bytes(),b'old')
    def test_query_missing_database_does_not_create(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'missing.sqlite'
            self.assertEqual(self.invoke(*['search', '--query', 'harbor'],'--db',p).returncode,2);self.assertFalse(p.exists())
