"""Generate an original miniature EPUB fixture with reproducible ZIP metadata."""
from pathlib import Path
import zipfile


def make(path):
    entries = {
        'mimetype':'application/epub+zip',
        'META-INF/container.xml':'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        'OPS/book.opf':'<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Small Harbor Notes</dc:title><dc:identifier>urn:sample:harbor</dc:identifier></metadata><manifest><item id="one" href="one.xhtml" media-type="application/xhtml+xml"/><item id="two" href="two.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="two"/><itemref idref="one"/></spine></package>',
        'OPS/one.xhtml':'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Morning</title></head><body><h1>Morning at the harbor</h1><p>Blue boats cross quiet water.</p><script>secret_hidden_marker</script></body></html>',
        'OPS/two.xhtml':'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Evening</title></head><body><h1>Evening notes</h1><p>Lanterns shine over the harbor.</p></body></html>'
    }
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(path,'w') as archive:
        for name,content in entries.items():
            info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0))
            archive.writestr(info,content.encode('utf-8'))
    return path


if __name__=='__main__':
    make(Path(__file__).with_name('sample.epub'))
