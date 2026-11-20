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
from .batch import intake
from .common import read_json
from . import workspace as ws


def _version_value(raw):
    if raw is None:
        return None
    text = str(raw)
    if text.isdigit():
        return int(text)
    return text


def _positions(raw):
    parts = [part.strip() for part in str(raw).split(',')]
    if not parts or any(not part.isdigit() for part in parts):
        raise ValueError('positions must be comma-separated positive integers')
    positions = [int(part) for part in parts]
    if not positions or any(position < 1 for position in positions):
        raise ValueError('positions must be comma-separated positive integers')
    return positions


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
    batch = commands.add_parser('import-batch')
    batch.add_argument('--manifest', type=Path, required=True,
                       help='ordered JSON manifest of {"source": path} entries')
    batch.add_argument('--target', type=Path, required=True,
                       help='new directory to publish; must not exist')
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
    # Journal anthology workflow: one local workspace bound to one catalogue.
    ws_init = commands.add_parser('workspace-init')
    ws_init.add_argument('--workspace', type=Path, required=True)
    ws_init.add_argument('--catalogue', type=Path, required=True,
                         help='existing archive whose authors table is the common catalogue')
    plan_save = commands.add_parser('plan-save')
    plan_save.add_argument('--workspace', type=Path, required=True)
    plan_save.add_argument('--issue', required=True, type=int)
    plan_save.add_argument('--plan', type=Path, required=True,
                           help='JSON array of {archive, identifier, source_sha256, position}')
    plan_reserve = commands.add_parser('plan-reserve')
    plan_reserve.add_argument('--workspace', type=Path, required=True)
    plan_reserve.add_argument('--issue', required=True, type=int)
    plan_reserve.add_argument('--catalogue', type=Path,
                              help='must equal the catalogue bound at workspace-init')
    plan_withdraw = commands.add_parser('plan-withdraw')
    plan_withdraw.add_argument('--workspace', type=Path, required=True)
    plan_withdraw.add_argument('--issue', required=True, type=int)
    plan_list = commands.add_parser('plan-list')
    plan_list.add_argument('--workspace', type=Path, required=True)
    plan_list.add_argument('--issue', type=int)
    license_reg = commands.add_parser('license-register')
    license_reg.add_argument('--workspace', type=Path, required=True)
    license_reg.add_argument('--catalogue', type=Path, required=True)
    license_reg.add_argument('--author', required=True, type=int)
    license_reg.add_argument('--archive', type=Path, required=True)
    license_reg.add_argument('--identifier', required=True)
    license_reg.add_argument('--version-sha', required=True, help='full source SHA-256 of the exact version')
    license_reg.add_argument('--positions', required=True,
                             help='comma-separated allowed 1-based spine positions, e.g. 1,3')
    license_reg.add_argument('--quota', required=True, type=int)
    license_revoke = commands.add_parser('license-revoke')
    license_revoke.add_argument('--workspace', type=Path, required=True)
    license_revoke.add_argument('--license', required=True)
    license_revoke.add_argument('--reason')
    license_list = commands.add_parser('license-list')
    license_list.add_argument('--workspace', type=Path, required=True)
    license_list.add_argument('--license')
    publish = commands.add_parser('publish')
    publish.add_argument('--workspace', type=Path, required=True)
    publish.add_argument('--issue', required=True, type=int)
    publish.add_argument('--catalogue', type=Path,
                         help='must equal the catalogue bound at workspace-init')
    anthology = commands.add_parser('anthology')
    anthology.add_argument('--workspace', type=Path, required=True)
    anthology.add_argument('--issue', required=True, type=int)
    anthology.add_argument('--output', type=Path, help='also copy the frozen anthology JSON to this path')
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
        elif args.command == 'import-batch':
            result = intake(args.manifest, args.target)
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
        elif args.command == 'author-books':
            result = query_author_books(
                args.db, args.author, args.catalogue, _version_value(args.version))
        elif args.command == 'workspace-init':
            result = ws.init_workspace(args.workspace, args.catalogue)
        elif args.command == 'plan-save':
            result = ws.save_plan(args.workspace, args.issue, args.plan)
        elif args.command == 'plan-reserve':
            result = ws.reserve_plan(
                args.workspace, args.issue, args.catalogue)
        elif args.command == 'plan-withdraw':
            result = ws.withdraw_plan(args.workspace, args.issue)
        elif args.command == 'plan-list':
            result = ws.list_plans(args.workspace, args.issue)
        elif args.command == 'license-register':
            positions = _positions(args.positions)
            result = ws.register_license(
                args.workspace, args.catalogue, args.author, args.archive,
                args.identifier, args.version_sha, positions, args.quota)
        elif args.command == 'license-revoke':
            result = ws.revoke_license(
                args.workspace, args.license, args.reason)
        elif args.command == 'license-list':
            result = ws.list_licenses(args.workspace, args.license)
        elif args.command == 'publish':
            result = ws.publish_issue(args.workspace, args.issue, args.catalogue)
        else:
            result = ws.get_anthology(
                args.workspace, args.issue, args.output)
    except (OSError, ValueError, TypeError, sqlite3.Error, ParseError,
            zipfile.BadZipFile, RuntimeError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
