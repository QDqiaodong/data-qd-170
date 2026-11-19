"""Bounded in-memory EPUB ZIP and namespace-aware package/spine extraction."""
import hashlib
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
import zipfile

CONTAINER = '{urn:oasis:names:tc:opendocument:xmlns:container}'
OPF = '{http://www.idpf.org/2007/opf}'
DC = '{http://purl.org/dc/elements/1.1/}'
XHTML = '{http://www.w3.org/1999/xhtml}'


def safe_path(value):
    path = PurePosixPath(value)
    if not value or value.startswith('/') or '\\' in value or any(p in ('..','.') for p in value.split('/')) or path.as_posix() != value:
        raise ValueError('unsafe archive member path')
    return value


def xml(raw):
    raw.decode('utf-8')
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('DTD and entities are not supported')
    if len(raw) > 2_000_000:
        raise ValueError('XML member exceeds limit')
    return ET.fromstring(raw)


def extract(source):
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > 500 or sum(e.file_size for e in entries) > 20_000_000:
            raise ValueError('EPUB exceeds member or expanded-size limit')
        names = set()
        for entry in entries:
            safe_path(entry.filename.rstrip('/'))
            if entry.filename in names or entry.flag_bits & 1:
                raise ValueError('duplicate or encrypted archive entry')
            names.add(entry.filename)
        def read(name):
            if name not in names:
                raise ValueError('missing archive member: ' + name)
            if archive.getinfo(name).file_size > 2_000_000:
                raise ValueError('member exceeds limit')
            return archive.read(name)
        if read('mimetype') != b'application/epub+zip':
            raise ValueError('wrong EPUB mimetype')
        container = xml(read('META-INF/container.xml'))
        roots = container.findall(f'{CONTAINER}rootfiles/{CONTAINER}rootfile')
        if len(roots) != 1:
            raise ValueError('exactly one rootfile required')
        package_path = safe_path(roots[0].get('full-path',''))
        package = xml(read(package_path))
        if package.tag != OPF+'package':
            raise ValueError('OPF package required')
        title = ' '.join(''.join(package.findtext(f'{OPF}metadata/{DC}title',default='')).split())
        identifier = package.findtext(f'{OPF}metadata/{DC}identifier',default='').strip()
        if not title or not identifier:
            raise ValueError('title and identifier required')
        manifest = {}
        for item in package.findall(f'{OPF}manifest/{OPF}item'):
            iid, href = item.get('id',''), item.get('href','')
            if not iid or iid in manifest:
                raise ValueError('duplicate or missing manifest id')
            if not href or href.startswith('/') or '\\' in href:
                raise ValueError('unsafe manifest href')
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                raise ValueError('manifest href must be local member path')
            member = safe_path((PurePosixPath(package_path).parent / unquote(parsed.path)).as_posix())
            manifest[iid] = (member,item.get('media-type'))
        chapters, seen = [], set()
        for ref in package.findall(f'{OPF}spine/{OPF}itemref'):
            iid = ref.get('idref')
            if iid not in manifest or iid in seen:
                raise ValueError('unknown or duplicate spine reference')
            seen.add(iid)
            member, media = manifest[iid]
            if media != 'application/xhtml+xml':
                raise ValueError('spine item must be XHTML')
            tree = xml(read(member))
            body = tree.find(XHTML+'body')
            if tree.tag != XHTML+'html' or body is None:
                raise ValueError('XHTML html/body required')
            for parent in body.iter():
                for child in list(parent):
                    if child.tag in (XHTML+'script',XHTML+'style'):
                        tail = child.tail or ''
                        index = list(parent).index(child)
                        if index:
                            previous = list(parent)[index-1]
                            previous.tail = (previous.tail or '') + tail
                        else:
                            parent.text = (parent.text or '') + tail
                        parent.remove(child)
            content = ' '.join(' '.join(body.itertext()).split())
            chapter_title = tree.findtext(f'{XHTML}head/{XHTML}title',default=member)
            chapters.append({'position':len(chapters)+1,'href':member,'title':chapter_title,'text':content})
        if not chapters:
            raise ValueError('empty spine')
    with open(source,'rb') as stream:
        digest = hashlib.file_digest(stream,'sha256').hexdigest()
    return {'title':title,'identifier':identifier,'source_sha256':digest,'chapters':chapters}
