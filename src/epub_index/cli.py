import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from xml.etree.ElementTree import ParseError
import zipfile
from .annotations import add_annotation, pending
from .archive import ingest, list_versions, search, select_version
from .common import read_json


def _version_value(raw):
    if raw is None:
        return None
    text = str(raw)
    if text.isdigit():
        return int(text)
    return text


def _payload(args, inline, file_name):
    file_path = getattr(args, file_name)
    if inline is not None and file_path is not None:
        raise ValueError('inline value and file input are mutually exclusive')
    if inline is not None:
        return inline
    if file_path is None:
        raise ValueError('an inline value or a file input is required')
    data = read_json(file_path)
    if not isinstance(data, str) or not data:
        raise ValueError('file input must contain a nonempty JSON string')
    return data


def main():
    parser = argparse.ArgumentParser(description='Local EPUB text archive')
    commands = parser.add_subparsers(dest='command', required=True)
    load = commands.add_parser('import')
    load.add_argument('--source', type=Path, required=True)
    load.add_argument('--db', type=Path, required=True)
    versions = commands.add_parser('versions')
    versions.add_argument('--db', type=Path, required=True)
    choose = commands.add_parser('select')
    choose.add_argument('--db', type=Path, required=True)
    choose.add_argument('--version', required=True, help='version id or source SHA-256')
    note_cmd = commands.add_parser('annotate')
    note_cmd.add_argument('--db', type=Path, required=True)
    note_cmd.add_argument('--chapter', required=True, type=int)
    note_cmd.add_argument('--excerpt')
    note_cmd.add_argument('--excerpt-file', type=Path)
    note_cmd.add_argument('--note')
    note_cmd.add_argument('--note-file', type=Path)
    note_cmd.add_argument('--version', help='version id or source SHA-256 (default: current)')
    query = commands.add_parser('search')
    query.add_argument('--db', type=Path, required=True)
    query.add_argument('--query', required=True)
    query.add_argument('--version', help='version id or source SHA-256 (default: current)')
    todo = commands.add_parser('pending')
    todo.add_argument('--db', type=Path, required=True)
    todo.add_argument('--version', help='version id or source SHA-256 (default: current)')
    args = parser.parse_args()
    temporary = None
    try:
        if args.command == 'import':
            args.db.parent.mkdir(parents=True, exist_ok=True)
            if args.db.is_symlink():
                raise ValueError('symlink target rejected')
            if args.db.exists():
                # Existing target must be a versioned SQLite archive; anything
                # else is refused and left byte-for-byte untouched.
                with args.db.open('rb') as stream:
                    header = stream.read(16)
                if header != b'SQLite format 3\x00':
                    raise ValueError('existing target is not a SQLite archive')
                result = ingest(args.source, args.db)
            else:
                # First import keeps the atomic temp-file + hard-link publication.
                fd, temporary = tempfile.mkstemp(dir=args.db.parent, suffix='.sqlite')
                os.close(fd)
                result = ingest(args.source, temporary)
                os.link(temporary, args.db)
        elif args.command == 'versions':
            result = list_versions(args.db)
        elif args.command == 'select':
            result = select_version(args.db, _version_value(args.version))
        elif args.command == 'annotate':
            excerpt = _payload(args, args.excerpt, 'excerpt_file')
            note = _payload(args, args.note, 'note_file')
            result = add_annotation(
                args.db, args.chapter, excerpt, note, _version_value(args.version))
        elif args.command == 'search':
            result = search(args.db, args.query, _version_value(args.version))
        else:
            result = pending(args.db, _version_value(args.version))
    except (OSError, ValueError, TypeError, sqlite3.Error, ParseError,
            zipfile.BadZipFile, RuntimeError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
