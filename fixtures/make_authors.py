"""Build original miniature EPUBs with explicit creator/contributor metadata.

Each variant is an independent book (its own package identifier) unless a
``variant`` of the same title is requested; the helper accepts a raw metadata
block so tests can exercise OPF role attributes and declaration order.
"""
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr
import zipfile

CONTAINER = (
    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OPS/book.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>')


def _chapter(title, paragraph):
    return ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>' + title
            + '</title></head><body><h1>' + title + '</h1><p>' + paragraph
            + '</p></body></html>')


def contributor_element(category, name, role=None, extra=()):
    """One dc:creator / dc:contributor element with optional OPF role."""
    attributes = ''.join(' %s=%s' % (key, quoteattr(value)) for key, value in extra)
    if role is not None:
        attributes += ' role=%s' % quoteattr(role)
    return '<dc:%s%s>%s</dc:%s>' % (category, attributes, escape(name), category)


def build(path, identifier, metadata, *, title='Harbor Notes', chapters=None):
    """Write an EPUB whose OPF metadata is the given pre-rendered declarations.

    ``metadata`` is a string of dc:creator/dc:contributor elements placed in the
    OPF metadata in the exact order given.
    """
    chapters = chapters or [('Chapter One', 'Lanterns shine over the harbor.')]
    hrefs = ['%s.xhtml' % i for i in range(1, len(chapters) + 1)]
    items = ''.join(
        '<item id="c%d" href="%s" media-type="application/xhtml+xml"/>' % (i, href)
        for i, href in enumerate(hrefs, 1))
    refs = ''.join('<itemref idref="c%d"/>' % i for i in range(1, len(chapters) + 1))
    opf = ('<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:title>' + escape(title) + '</dc:title>'
           '<dc:identifier>' + escape(identifier) + '</dc:identifier>'
           + metadata + '</metadata><manifest>' + items
           + '</manifest><spine>' + refs + '</spine></package>')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        entries = [('mimetype', 'application/epub+zip'),
                   ('META-INF/container.xml', CONTAINER),
                   ('OPS/book.opf', opf)]
        entries += [('OPS/' + href, _chapter(title, paragraph))
                    for href, (title, paragraph) in zip(hrefs, chapters)]
        for name, content in entries:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, content.encode('utf-8'))
    return path
