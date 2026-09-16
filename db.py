#!/usr/bin/env python3
"""
The sample index, behind one interface.

The app used to find records by asking Discogs: a search, then a release fetch
per candidate until one turned up with a playable video. That is what this
replaces. Discogs is still called once per pull, for the community have/want
the grail mechanic runs on — the monthly dump does not carry those.

SQLite today, Postgres when the index outgrows a file. The split is deliberate:
`Database` holds the query logic, and the two subclasses hold only what the
engines genuinely disagree about — placeholder style, how a random row is
drawn, and whether the planner needs to be told which index to walk. Adding
Postgres means filling in `PostgresDB`, not rewriting queries.

    db = open_db('sqlite:///data/samples.db')
    db.count(Filters(vinyl=True, year_from=1965, year_to=1985))   # 641664
    db.pick(Filters(...))                                         # one release
"""
import os
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass, field

# Below this many matches the random cursor stops being the cheap path: it would
# walk millions of index entries looking for a handful of rows, where sorting
# the small match set outright is instant. Measured either side of ~2k on the
# 3.6M-row index; see the timings in the importer's commit message.
CURSOR_MIN = 2000

COLUMNS = ('id', 'title', 'artist', 'year', 'country', 'label',
           'catno', 'genres', 'styles', 'format', 'video')


@dataclass
class Filters:
    """What the drawer can ask for. None or empty means "don't care"."""
    genre: str = ''
    styles: list = field(default_factory=list)
    country: str = ''
    vinyl: bool = False
    year_from: int = None
    year_to: int = None


def _plan(f):
    """Pick the table the query drives off, and what is left to check.

    Genre and style live in their own tables, each row carrying a copy of the
    columns it is filtered alongside — so whichever one drives answers the
    whole predicate from a single index range. Style drives when present: it is
    far more selective than genre, which keeps the leftover EXISTS probes few.
    With neither, the release table drives directly."""
    styles = [s.strip() for s in (f.styles or []) if s.strip()]
    genre = (f.genre or '').strip()
    if styles:
        return 'rs', styles[0], styles[1:], genre
    if genre:
        return 'rg', genre, [], ''
    return 'release', None, [], ''


def _predicates(f, driver, name):
    """(sql, params) with '?' throughout — PostgresDB rewrites the placeholders.
    The filter columns exist on all three driving tables under the same names,
    so this fragment does not care which one it is aimed at."""
    where, args = [], []
    if name is not None:
        where.append('d.name = ?'); args.append(name)
    if f.vinyl:
        where.append('d.vinyl = 1')
    if f.year_from is not None:
        where.append('d.year >= ?'); args.append(int(f.year_from))
    if f.year_to is not None:
        where.append('d.year <= ?'); args.append(int(f.year_to))
    if f.country and f.country.strip():
        where.append('d.country = ?'); args.append(f.country.strip())
    return (' AND '.join(where) or '1 = 1'), args


def _extra(rest_styles, rest_genre, key):
    """EXISTS probes for the dimensions that are not driving. Discogs ANDs
    multiple styles — "Funk,Soul" is the crossover, not the union — so each one
    is its own probe."""
    sql, args = '', []
    for st in rest_styles:
        sql += f' AND EXISTS (SELECT 1 FROM rs x WHERE x.release_id = {key} AND x.name = ?)'
        args.append(st)
    if rest_genre:
        sql += f' AND EXISTS (SELECT 1 FROM rg x WHERE x.release_id = {key} AND x.name = ?)'
        args.append(rest_genre)
    return sql, args


class Database:
    """Engine-independent query logic. Subclasses supply the dialect."""

    # --- dialect hooks -----------------------------------------------------
    def _sql(self, sql):
        """Render '?' placeholders for this engine."""
        return sql

    def _cursor_hint(self, driver):
        """Index hint for the random-cursor path, empty where unneeded."""
        return ''

    def _random(self):
        return 'RANDOM()'

    def _query(self, sql, args):
        raise NotImplementedError

    # --- the interface the server uses -------------------------------------
    def count(self, f):
        driver, name, rest_s, rest_g = _plan(f)
        where, args = _predicates(f, driver, name)
        key = 'd.id' if driver == 'release' else 'd.release_id'
        ex, ex_args = _extra(rest_s, rest_g, key)
        rows = self._query(
            f'SELECT COUNT(*) FROM {driver} d WHERE {where}{ex}', args + ex_args)
        return rows[0][0] if rows else 0

    def pick(self, f, known_count=None):
        """One random release matching `f`, or None when nothing matches.

        Two paths, chosen by how selective the filter is. A broad filter walks
        the indexed random column from a random point and stops at the first
        match. A narrow one cannot afford that walk — millions of index entries
        to find five rows — and is small enough to sort outright instead."""
        n = self.count(f) if known_count is None else known_count
        if not n:
            return None

        driver, name, rest_s, rest_g = _plan(f)
        where, args = _predicates(f, driver, name)
        key = 'd.id' if driver == 'release' else 'd.release_id'
        ex, ex_args = _extra(rest_s, rest_g, key)
        cols = ', '.join('r.' + c for c in COLUMNS)
        join = '' if driver == 'release' else ' JOIN release r ON r.id = d.release_id'
        src = f'{driver} d{join}' if driver != 'release' else 'release d'
        sel = cols if driver != 'release' else ', '.join('d.' + c for c in COLUMNS)

        if n >= CURSOR_MIN:
            import random
            hint = self._cursor_hint(driver)
            sql = (f'SELECT {sel} FROM {driver} d{hint}{join} WHERE {where}{ex} '
                   f'AND d.rnd > ? ORDER BY d.rnd LIMIT 1')
            if driver == 'release':
                sql = sql.replace('SELECT ' + sel, 'SELECT ' + sel)
            rows = self._query(sql, args + ex_args + [random.random()])
            if not rows:
                # The draw landed past the last match; wrap to the start rather
                # than reporting an empty table we know is not empty.
                rows = self._query(sql, args + ex_args + [-1.0])
        else:
            sql = (f'SELECT {sel} FROM {driver} d{join} WHERE {where}{ex} '
                   f'ORDER BY {self._random()} LIMIT 1')
            rows = self._query(sql, args + ex_args)

        return _as_track(rows[0]) if rows else None

    def close(self):
        pass


def _as_track(row):
    """The row, in the shape the page already renders."""
    r = dict(zip(COLUMNS, row))
    return {
        'id': r['id'],
        'title': r['title'],
        'release': r['title'],
        'artist': r['artist'] or '—',
        'year': r['year'] or '—',
        'country': r['country'] or '—',
        'label': r['label'] or '—',
        'catno': r['catno'] or '—',
        'genres': (r['genres'] or '').split('\n') if r['genres'] else [],
        'styles': (r['styles'] or '').split('\n') if r['styles'] else [],
        'videoId': r['video'],
        'watchUrl': 'https://youtu.be/' + r['video'],
        'discogsUrl': f"https://www.discogs.com/release/{r['id']}",
        # Cover art and have/want are not in the dump. The page already falls
        # back to the youtube thumbnail, and the server fills the counts in
        # from one Discogs call per pull.
        'cover': None,
    }


class SQLiteDB(Database):
    def __init__(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(
                f'{path} not found — build it with tools/build_db.py')
        self.path = path
        # check_same_thread=False: the server answers on several threads and
        # every query here is a read.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute('PRAGMA query_only = ON')

    def _cursor_hint(self, driver):
        # SQLite's planner reaches for the year index and then sorts every
        # match to take one row — 769ms where the cursor needs 0.1ms. It has to
        # be told. Postgres works this out from its own statistics.
        return ' INDEXED BY rel_rnd' if driver == 'release' else ''

    def _query(self, sql, args):
        return self.conn.execute(sql, args).fetchall()

    def close(self):
        self.conn.close()


class PostgresDB(Database):
    """Not wired up yet — the shape the move needs, so it stays a small step.

    Only the dialect differs: %s placeholders, no index hint (the planner has
    real statistics), and a pooled connection instead of a file handle."""

    def __init__(self, dsn):
        raise NotImplementedError(
            'Postgres support is not built yet. The importer writes SQLite; '
            'point DATABASE_URL at a sqlite:/// path.')

    def _sql(self, sql):
        return re.sub(r'\?', '%s', sql)

    def _random(self):
        return 'random()'


def open_db(url):
    """sqlite:///relative/path, sqlite:////absolute/path, or postgresql://..."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme in ('', 'sqlite'):
        path = (parts.path or url).lstrip('/') if parts.scheme else url
        if url.startswith('sqlite:////'):
            path = '/' + path
        return SQLiteDB(path)
    if parts.scheme in ('postgres', 'postgresql'):
        return PostgresDB(url)
    raise ValueError(f'unsupported database url: {url}')
