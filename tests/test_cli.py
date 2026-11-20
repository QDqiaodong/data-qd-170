from pathlib import Path
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
MODULE='epub_index'
spec=importlib.util.spec_from_file_location('make_versions',ROOT/'fixtures/make_versions.py')
versions_fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(versions_fixture)
class CliTests(unittest.TestCase):
    def invoke(self,*args):
        return subprocess.run([sys.executable,'-m',MODULE,*map(str,args)],capture_output=True,text=True,timeout=10)
    def source(self):
        return ROOT/'fixtures/sample.epub'
    def payload(self,tmp,name,value):
        path=Path(tmp)/name
        path.write_text(json.dumps(value),encoding='utf-8')
        return path
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
    def test_version_workflow_annotate_pending_and_select(self):
        with tempfile.TemporaryDirectory() as t:
            db=Path(t)/'books.sqlite'
            v1=versions_fixture.build(Path(t)/'v1.epub','v1')
            v2=versions_fixture.build(Path(t)/'v2.epub','v2')
            v3=versions_fixture.build(Path(t)/'v3.epub','v3')
            self.assertEqual(self.invoke('import','--source',v1,'--db',db).returncode,0)
            again=self.invoke('import','--source',v1,'--db',db)
            self.assertEqual(again.returncode,0,again.stderr)
            self.assertIn('unchanged',again.stdout)
            note_file=self.payload(t,'note.json','reader note kept verbatim')
            add=self.invoke('annotate','--db',db,'--chapter','2','--version','1',
                            '--excerpt','old quay','--note-file',note_file)
            self.assertEqual(add.returncode,0,add.stderr)
            annotation_id=json.loads(add.stdout)['annotation_id']
            # Importing new versions drives relocation automatically.
            self.assertEqual(self.invoke('import','--source',v2,'--db',db).returncode,0)
            listed=self.invoke('versions','--db',db)
            self.assertEqual(listed.returncode,0,listed.stderr)
            versions=json.loads(listed.stdout)
            self.assertEqual([v['version_id'] for v in versions],[1,2])
            self.assertTrue(versions[1]['current'])
            # Annotation relocates uniquely onto v2 chapter three.
            hits=json.loads(self.invoke('search','--db',db,'--query','quay','--version','2').stdout)
            shown={h['position']:[a['annotation_id'] for a in h['annotations']] for h in hits}
            self.assertEqual(shown.get(3),[annotation_id])
            self.assertEqual(shown.get(4),[])
            # Old version keeps its original chapter-two placement.
            old=json.loads(self.invoke('search','--db',db,'--query','quay','--version','1').stdout)
            self.assertEqual([a['annotation_id'] for a in old[0]['annotations']],[annotation_id])
            # Switching current version back to v1 changes default search.
            self.assertEqual(self.invoke('select','--db',db,'--version','1').returncode,0)
            default=json.loads(self.invoke('search','--db',db,'--query','quay').stdout)
            self.assertEqual(default[0]['position'],2)
            # Ambiguous v3 hides the annotation from search and lists it pending.
            self.assertEqual(self.invoke('import','--source',v3,'--db',db).returncode,0)
            todo=json.loads(self.invoke('pending','--db',db,'--version','3').stdout)
            self.assertEqual(len(todo),1)
            self.assertEqual(todo[0]['status'],'ambiguous')
            self.assertEqual(len(todo[0]['candidates']),2)
            self.assertEqual(todo[0]['note'],'reader note kept verbatim')
            hidden=json.loads(self.invoke('search','--db',db,'--query','quay','--version','3').stdout)
            self.assertTrue(all(not h['annotations'] for h in hidden))
    def test_annotate_rejects_offtext_excerpt(self):
        with tempfile.TemporaryDirectory() as t:
            db=Path(t)/'db.sqlite'
            v1=versions_fixture.build(Path(t)/'v1.epub','v1')
            self.invoke('import','--source',v1,'--db',db)
            bad=self.invoke('annotate','--db',db,'--chapter','2','--excerpt','nonexistent phrase','--note','x')
            self.assertEqual(bad.returncode,2)
            self.assertIn('not part of the chapter text',bad.stderr)
    def test_failed_import_preserves_old_version_results(self):
        with tempfile.TemporaryDirectory() as t:
            db=Path(t)/'db.sqlite'
            v1=versions_fixture.build(Path(t)/'v1.epub','v1')
            broken=Path(t)/'broken.epub';broken.write_bytes(b'not a zip')
            self.invoke('import','--source',v1,'--db',db)
            fail=self.invoke('import','--source',broken,'--db',db)
            self.assertEqual(fail.returncode,2)
            versions=json.loads(self.invoke('versions','--db',db).stdout)
            self.assertEqual(len(versions),1)
            self.assertTrue(versions[0]['current'])
            hits=json.loads(self.invoke('search','--db',db,'--query','harbor').stdout)
            self.assertEqual(len(hits),1)
    def test_existing_non_sqlite_target_refused_before_opening(self):
        with tempfile.TemporaryDirectory() as t:
            db=Path(t)/'db.sqlite';db.write_bytes(b'')
            v1=versions_fixture.build(Path(t)/'v1.epub','v1')
            r=self.invoke('import','--source',v1,'--db',db)
            self.assertEqual(r.returncode,2)
            self.assertEqual(db.read_bytes(),b'')
    def test_version_import_through_symlink_refused(self):
        with tempfile.TemporaryDirectory() as t:
            db=Path(t)/'db.sqlite';v1=versions_fixture.build(Path(t)/'v1.epub','v1')
            v2=versions_fixture.build(Path(t)/'v2.epub','v2')
            self.invoke('import','--source',v1,'--db',db)
            link=Path(t)/'link.sqlite';link.symlink_to(db)
            r=self.invoke('import','--source',v2,'--db',link)
            self.assertEqual(r.returncode,2)
            versions=json.loads(self.invoke('versions','--db',db).stdout)
            self.assertEqual(len(versions),1)
