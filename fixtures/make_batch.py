"""Build original miniature EPUBs with distinct identities for batch intake tests."""
from pathlib import Path
import zipfile

CONTAINER = (
    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OPS/book.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>')


def _chapter(title, paragraphs):
    body = ''.join('<p>' + p + '</p>' for p in paragraphs)
    return ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>' + title
            + '</title></head><body><h1>' + title + '</h1>' + body
            + '</body></html>')


def _opf(identifier, title, creator, hrefs):
    items = ''.join(
        '<item id="c%d" href="%s" media-type="application/xhtml+xml"/>' % (i, href)
        for i, href in enumerate(hrefs, 1))
    refs = ''.join('<itemref idref="c%d"/>' % i for i in range(1, len(hrefs) + 1))
    byline = '<dc:creator>' + creator + '</dc:creator>' if creator else ''
    return ('<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>' + title + '</dc:title><dc:identifier>' + identifier
            + '</dc:identifier>' + byline + '</metadata><manifest>' + items
            + '</manifest><spine>' + refs + '</spine></package>')


def build(path, identifier, title, creator=None, paragraphs=None):
    """One valid two-chapter EPUB with its own package identifier."""
    paragraphs = paragraphs or ['A quiet catalogue opens the record.',
                                'The final page rests beside the window.']
    files = {
        'OPS/opening.xhtml': _chapter('Opening', paragraphs[:1]),
        'OPS/closing.xhtml': _chapter('Closing', paragraphs[1:]),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in [
                ('mimetype', 'application/epub+zip'),
                ('META-INF/container.xml', CONTAINER),
                ('OPS/book.opf',
                 _opf(identifier, title, creator, [name[4:] for name in files])),
                *sorted(files.items())]:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, content.encode('utf-8'))
    return path
