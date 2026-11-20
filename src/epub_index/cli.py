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
from .authors import (
    add_alias, create_author, list_authors, query_author_books,
    query_declarations, query_unresolved, remove_alias,
)
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
    # Author catalogue maintenance.
    author_new = commands.add_parser('author-add')
    author_new.add_argument('--db', type=Path, required=True)
    author_new.add_argument('--name', required=True, help='canonical display name')
    author_list = commands.add_parser('authors')
    author_list.add_argument('--db', type=Path, required=True)
    alias_add = commands.add_parser('alias-add')
    alias_add.add_argument('--db', type=Path, required=True)
    alias_add.add_argument('--author', required=True, type=int)
    alias_add.add_argument('--alias', required=True, help='exact declared full name')
    alias_remove = commands.add_parser('alias-remove')
    alias_remove.add_argument('--db', type=Path, required=True)
    alias_remove.add_argument('--author', required=True, type=int)
    alias_remove.add_argument('--alias', required=True, help='exact declared full name')
    # Querying source declarations and one identity's books. Queries accept one
    # or more archives; the first one (or --catalogue) owns the catalogue.
    declarations = commands.add_parser('contributors')
    declarations.add_argument('--db', type=Path, required=True, action='append')
    declarations.add_argument('--catalogue', type=Path)
    declarations.add_argument('--version', help='version id or source SHA-256 (default: all)')
    unresolved = commands.add_parser('unresolved')
    unresolved.add_argument('--db', type=Path, required=True, action='append')
    unresolved.add_argument('--catalogue', type=Path)
    unresolved.add_argument('--version', help='version id or source SHA-256 (default: all)')
    catalogue = commands.add_parser('author-books')
    catalogue.add_argument('--db', type=Path, required=True, action='append')
    catalogue.add_argument('--catalogue', type=Path)
    catalogue.add_argument('--author', required=True, type=int)
    catalogue.add_argument('--version', help='version id or source SHA-256 (default: all)')
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
        elif args.command == 'pending':
            result = pending(args.db, _version_value(args.version))
        elif args.command == 'author-add':
            result = create_author(args.db, args.name)
        elif args.command == 'authors':
            result = list_authors(args.db)
        elif args.command == 'alias-add':
            result = add_alias(args.db, args.author, args.alias)
        elif args.command == 'alias-remove':
            result = remove_alias(args.db, args.author, args.alias)
        elif args.command == 'contributors':
            result = query_declarations(
                args.db, args.catalogue, _version_value(args.version))
        elif args.command == 'unresolved':
            result = query_unresolved(
                args.db, args.catalogue, _version_value(args.version))
        else:
            result = query_author_books(
                args.db, args.author, args.catalogue, _version_value(args.version))
    except (OSError, ValueError, TypeError, sqlite3.Error, ParseError,
            zipfile.BadZipFile, RuntimeError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
