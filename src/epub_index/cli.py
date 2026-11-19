import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from xml.etree.ElementTree import ParseError
import zipfile
from .archive import ingest, search


def main():
    parser = argparse.ArgumentParser(description='Local EPUB text archive')
    commands = parser.add_subparsers(dest='command',required=True)
    load = commands.add_parser('import')
    load.add_argument('--source',type=Path,required=True)
    load.add_argument('--db',type=Path,required=True)
    query = commands.add_parser('search')
    query.add_argument('--db',type=Path,required=True)
    query.add_argument('--query',required=True)
    args = parser.parse_args()
    temporary = None
    try:
        if args.command == 'import':
            args.db.parent.mkdir(parents=True,exist_ok=True)
            fd,temporary = tempfile.mkstemp(dir=args.db.parent,suffix='.sqlite')
            os.close(fd)
            result = ingest(args.source,temporary)
            os.link(temporary,args.db)
        else:
            result = search(args.db,args.query)
    except (OSError,ValueError,TypeError,sqlite3.Error,ParseError,zipfile.BadZipFile,RuntimeError) as error:
        print(str(error),file=sys.stderr)
        return 2
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return 0
