"""Build original multi-version miniature EPUBs for version/annotation tests."""
from pathlib import Path
import zipfile

IDENTIFIER = 'urn:sample:harbor'

CONTAINER = (
    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OPS/book.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>')


def _chapter(title, paragraphs):
    body = ''.join('<p>' + p + '</p>' for p in paragraphs)
    return ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>' + title
            + '</title></head><body><h1>' + title + '</h1>' + body + '</body></html>')


def _opf(hrefs):
    items = ''.join(
        '<item id="c%d" href="%s" media-type="application/xhtml+xml"/>' % (i, href)
        for i, href in enumerate(hrefs, 1))
    refs = ''.join('<itemref idref="c%d"/>' % i for i in range(1, len(hrefs) + 1))
    return ('<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>Small Harbor Notes</dc:title><dc:identifier>' + IDENTIFIER
            + '</dc:identifier></metadata><manifest>' + items
            + '</manifest><spine>' + refs + '</spine></package>')


def build(path, variant):
    """variants: v1 initial, v2 unique relocation, v3 ambiguous, v4 missing."""
    if variant == 'v1':
        files = {
            'OPS/one.xhtml': _chapter('Chapter One', [
                'Lanterns shine over the harbor at first light.',
            ]),
            'OPS/two.xhtml': _chapter('Chapter Two', [
                'Waves keep their own counsel here.',
                'Fishermen remember the old quay where the herring boats landed.',
            ]),
            'OPS/three.xhtml': _chapter('Chapter Three', [
                'Blue boats cross quiet water beyond the pier.',
            ]),
        }
    elif variant == 'v2':
        # The annotated paragraph moves from chapter two (renamed path) into
        # chapter three; its suffix is preserved. A decoy identical excerpt
        # appears elsewhere with different surrounding text.
        files = {
            'OPS/one.xhtml': _chapter('Chapter One', [
                'Lanterns shine over the harbor at first light.',
            ]),
            'OPS/two-renamed.xhtml': _chapter('Chapter Two', [
                'Waves keep their own counsel here.',
            ]),
            'OPS/three.xhtml': _chapter('Chapter Three', [
                'Fishermen remember the old quay where the herring boats landed.',
                'Blue boats cross quiet water beyond the pier.',
            ]),
            'OPS/four.xhtml': _chapter('Chapter Four', [
                'A painted sign reads old quay beside the new marina entrance.',
            ]),
        }
    elif variant == 'v3':
        # Two occurrences keep the same saved context: relocation is ambiguous.
        files = {
            'OPS/one.xhtml': _chapter('Chapter One', [
                'Lanterns shine over the harbor at first light.',
            ]),
            'OPS/two.xhtml': _chapter('Chapter Two', [
                'Waves keep their own counsel here.',
                'Fishermen remember the old quay where the herring boats landed.',
            ]),
            'OPS/three.xhtml': _chapter('Chapter Three', [
                'Blue boats cross quiet water beyond the pier.',
                'Fishermen remember the old quay where the herring boats landed.',
            ]),
        }
    elif variant == 'v4':
        # The original chapter disappears; the only remaining excerpt lost both
        # context clues, so the annotation is missing.
        files = {
            'OPS/one.xhtml': _chapter('Chapter One', [
                'Lanterns shine over the harbor at first light.',
            ]),
            'OPS/three.xhtml': _chapter('Chapter Three', [
                'Blue boats cross quiet water beyond the pier.',
                'A painted sign reads old quay beside the new marina entrance.',
            ]),
        }
    else:
        raise ValueError('unknown variant: ' + variant)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in [
                ('mimetype', 'application/epub+zip'),
                ('META-INF/container.xml', CONTAINER),
                ('OPS/book.opf', _opf([name[4:] for name in files])),
                *sorted(files.items())]:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, content.encode('utf-8'))
    return path
