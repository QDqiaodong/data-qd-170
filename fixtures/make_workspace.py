"""Build original miniature EPUBs for the journal anthology workflow tests.

The helper wraps :mod:`fixtures.make_authors` so tests can declare an exact
creator/contributor metadata block, a fixed spine length and deterministic
chapter text. All prose is synthetic and original; no third-party book text is
bundled.
"""
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    'make_authors', ROOT / 'make_authors.py')
_authors = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_authors)

contributor_element = _authors.contributor_element


def build(path, identifier, metadata, *, title='Journal Source Book',
          chapter_count=2, word='harbor'):
    """Write an EPUB with ``chapter_count`` spine items and the given metadata."""
    chapters = [
        ('Chapter %d' % number,
         'Synthetic %s paragraph number %d for the journal workflow.'
         % (word, number))
        for number in range(1, chapter_count + 1)
    ]
    return _authors.build(path, identifier, metadata, title=title,
                          chapters=chapters)


def chapter_text(number, word='harbor'):
    # Extraction joins the <h1> heading and the paragraph into one body text.
    return ('Chapter %d Synthetic %s paragraph number %d '
            'for the journal workflow.' % (number, word, number))
