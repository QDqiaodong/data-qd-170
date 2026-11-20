"""Batch intake of an ordered EPUB manifest into one atomically published directory.

An administrator lists local EPUB paths in an ordered JSON manifest. Every
source is validated with the same bounded EPUB checks as a single import and
archived into its own independent SQLite database. Book identity comes solely
from the EPUB package identifier and the source SHA-256, never from file
names, and package metadata is never trusted as an output path: archive file
names are derived from the identifier but sanitized to a bounded safe alphabet.

The whole batch is validated and built inside a temporary sibling directory of
the target and published with one atomic directory rename only when every item
succeeded. Any item or publish failure leaves no target and no visible partial
archives behind; source EPUBs are only read, never moved, rewritten or
deleted. If temporary-directory cleanup itself fails, the error reports the
actual residue location and never claims the target was published.
"""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from xml.etree.ElementTree import ParseError
import zipfile

from .archive import ingest
from .common import array, fields, publish, read_json
from .db import utc_now
from .extract import extract

SOURCE_LIMIT = 4096
SLUG_LIMIT = 40
RECEIPT_NAME = 'receipt.json'

# Mirrors the CLI's rejection set: any of these marks one manifest item bad.
_ITEM_ERRORS = (OSError, ValueError, TypeError, sqlite3.Error, ParseError,
                zipfile.BadZipFile, RuntimeError)


def _source_value(value):
    if (not isinstance(value, str) or not value or len(value) > SOURCE_LIMIT
            or '\x00' in value):
        raise ValueError('source must be a nonempty bounded path string')
    return value


def _archive_name(index, identifier, sha256):
    """Safe archive file name derived from, never dictated by, the identifier."""
    slug = ''.join(
        char if (char.isalnum() or char in '-._') else '-'
        for char in identifier)
    slug = slug.strip('.-')[:SLUG_LIMIT].strip('.-')
    if not slug:
        slug = 'book'
    return f'{index:03d}-{slug}-{sha256[:12]}.sqlite'


def _load_manifest(manifest):
    """Parse the ordered manifest and fingerprint its exact bytes."""
    path = Path(manifest).resolve()
    data = read_json(path)
    if not isinstance(data, list):
        raise ValueError(
            'manifest must be a JSON array of {"source": path} entries')
    entries = array(data)
    if not entries:
        raise ValueError('manifest lists no books')
    with path.open('rb') as stream:
        raw = stream.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError('input exceeds 8MB')
    digest = hashlib.sha256(raw).hexdigest()
    items = []
    errors = []
    for position, entry in enumerate(entries, 1):
        try:
            fields(entry, 'source')
            items.append((position, _source_value(entry['source'])))
        except ValueError as error:
            errors.append(f'item {position}: {error}')
    if errors:
        raise ValueError('invalid manifest: ' + '; '.join(errors))
    return path, digest, items


def _preflight(manifest_path, items):
    """Validate every listed source in manifest order; collect all item errors.

    Each source is resolved against the manifest's directory, checked with the
    full bounded EPUB validation and fingerprinted. Duplicates of resolved
    path, source SHA-256 or package identifier are reported against the first
    manifest item that claimed them.
    """
    base = manifest_path.parent
    seen_paths, seen_shas, seen_identifiers = {}, {}, {}
    checked = []
    errors = []
    for position, source in items:
        candidate = Path(source)
        resolved = (candidate if candidate.is_absolute()
                    else base / candidate).resolve()
        try:
            if not resolved.is_file():
                raise ValueError('source is not a regular file')
            book = extract(resolved)
        except _ITEM_ERRORS as error:
            errors.append(f'item {position} ({source}): {error}')
            continue
        problems = []
        if resolved in seen_paths:
            problems.append(
                f'duplicate source file also listed at item {seen_paths[resolved]}')
        sha256 = book['source_sha256']
        if sha256 in seen_shas:
            problems.append(
                f'duplicate source SHA-256 also listed at item {seen_shas[sha256]}')
        identifier = book['identifier']
        if identifier in seen_identifiers:
            problems.append(
                f'duplicate package identifier {identifier!r} '
                f'also listed at item {seen_identifiers[identifier]}')
        if problems:
            errors.append(f'item {position} ({source}): ' + '; '.join(problems))
            continue
        seen_paths[resolved] = position
        seen_shas[sha256] = position
        seen_identifiers[identifier] = position
        checked.append({
            'position': position,
            'source': source,
            'resolved': resolved,
            'identifier': identifier,
            'source_sha256': sha256,
            'title': book['title'],
        })
    if errors:
        raise ValueError('batch rejected: ' + '; '.join(errors))
    return checked


def intake(manifest, target):
    """Publish one independent archive per manifest item into a new directory.

    Returns the receipt (also stored as receipt.json inside the published
    directory). Raises before publishing when any item fails; the target then
    does not exist and no partial archives remain visible.
    """
    manifest_path, manifest_sha256, items = _load_manifest(manifest)
    target = Path(target)
    if os.path.lexists(target):
        raise ValueError('target already exists: ' + str(target))
    checked = _preflight(manifest_path, items)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix='.batch-', dir=target.parent))
    try:
        books = []
        used_names = set()
        for item in checked:
            name = _archive_name(
                item['position'], item['identifier'], item['source_sha256'])
            if name in used_names:
                raise ValueError(
                    f"item {item['position']} ({item['source']}): "
                    'archive file name collision')
            used_names.add(name)
            try:
                result = ingest(item['resolved'], temp_dir / name)
            except _ITEM_ERRORS as error:
                raise ValueError(
                    f"item {item['position']} ({item['source']}): {error}"
                ) from error
            if (result['identifier'] != item['identifier']
                    or result['source_sha256'] != item['source_sha256']):
                raise ValueError(
                    f"item {item['position']} ({item['source']}): "
                    'source changed during intake')
            books.append({
                'index': item['position'],
                'source': item['source'],
                'identifier': item['identifier'],
                'source_sha256': item['source_sha256'],
                'title': item['title'],
                'archive': name,
            })
        receipt = {
            'action': 'published',
            'book_count': len(books),
            'books': books,
            'created_at': utc_now(),
            'manifest': str(manifest_path),
            'manifest_sha256': manifest_sha256,
            'target': str(target.resolve()),
        }
        publish(temp_dir / RECEIPT_NAME, receipt)
        if os.path.lexists(target):
            raise ValueError('target appeared during intake: ' + str(target))
        os.rename(temp_dir, target)
    except BaseException as error:
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
        except OSError as cleanup_error:
            raise RuntimeError(
                f'batch intake failed ({error}); target was not published; '
                f'temporary files remain at {temp_dir}: {cleanup_error}'
            ) from error
        raise
    return receipt
