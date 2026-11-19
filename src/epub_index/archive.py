"""Independent SQLite metadata/chapter archive and literal-token FTS5 search."""
from pathlib import Path
import re
import sqlite3
from .extract import extract


def ingest(source, database):
    book = extract(source)
    conn = sqlite3.connect(database)
    try:
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
            raise ValueError('destination must be empty')
        conn.executescript('''BEGIN;
CREATE TABLE book(title TEXT,identifier TEXT,source_sha256 TEXT);
CREATE TABLE chapters(position INTEGER PRIMARY KEY,href TEXT UNIQUE,title TEXT,text TEXT);
CREATE VIRTUAL TABLE search USING fts5(title,text,tokenize='unicode61');''')
        conn.execute('INSERT INTO book VALUES (?,?,?)',(book['title'],book['identifier'],book['source_sha256']))
        for chapter in book['chapters']:
            conn.execute('INSERT INTO chapters VALUES (?,?,?,?)',tuple(chapter[k] for k in ('position','href','title','text')))
            conn.execute('INSERT INTO search(rowid,title,text) VALUES (?,?,?)',(chapter['position'],chapter['title'],chapter['text']))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {'title':book['title'],'chapter_count':len(book['chapters']),'source_sha256':book['source_sha256']}


def search(database, query):
    if not isinstance(query,str) or not query.strip() or len(query)>200:
        raise ValueError('bounded nonempty query required')
    tokens = re.findall(r'[^\W_]+',query,flags=re.UNICODE)
    if not tokens:
        raise ValueError('query contains no searchable tokens')
    expression = ' AND '.join('"'+token+'"' for token in tokens)
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as conn:
        return [{'position':r[0],'href':r[1],'title':r[2],'snippet':r[3]} for r in conn.execute(
            '''SELECT c.position,c.href,c.title,snippet(search,1,'[',']','…',16)
               FROM search JOIN chapters c ON c.position=search.rowid
               WHERE search MATCH ? ORDER BY c.position LIMIT 100''',(expression,))]
