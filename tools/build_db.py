#!/usr/bin/env python3
"""
Turn a Discogs monthly releases dump into the small SQLite the app rolls on.

    python3 tools/build_db.py data/discogs_20260901_releases.xml.gz -o data/samples.db

The dump is ~10.5 GB gzipped and several times that unpacked, so it is never
unpacked: gzip is decoded as a stream and the XML consumed with iterparse,
clearing each <release> as it goes. Peak memory stays flat regardless of size.

What survives the filter is the point. A release with no YouTube video is dead
weight for this app — worse, picking one is what produces the "discogs has it,
youtube doesn't" dead end — so the video is a hard requirement. Everything the
page never renders (notes, tracklist, identifiers, companies) is dropped.

Counts are collected for everything, not just what is kept, so a run reports
what the filter actually cost.
"""
import argparse
import gzip
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET

YT = re.compile(r'(?:v=|youtu\.be/|embed/)([\w-]{11})')

SCHEMA = """
CREATE TABLE release (
  id       INTEGER PRIMARY KEY,
  title    TEXT NOT NULL,
  artist   TEXT NOT NULL,
  year     INTEGER,
  country  TEXT,
  label    TEXT,
  catno    TEXT,
  genres   TEXT,          -- newline-joined, matched with LIKE
  styles   TEXT,
  format   TEXT,
  vinyl    INTEGER NOT NULL,  -- the app offers vinyl-or-any, and LIKE '%Vinyl%'
                              -- costs a row scan: 1270ms for a COUNT, 17ms with this
  video    TEXT NOT NULL, -- 11-char youtube id
  rnd      REAL NOT NULL  -- see pick_sql() in the server: random draw by index
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

INDEXES = """
/* Two access paths, picked by how selective the filter is (see the server):
   a broad filter walks rel_rnd as a cursor and stops at the first match; a
   narrow one is small enough to sort outright, and needs the filter columns
   indexed to find its rows at all. */
CREATE INDEX rel_rnd       ON release (rnd);
CREATE INDEX rel_v_year    ON release (vinyl, year, rnd);
CREATE INDEX rel_v_country ON release (vinyl, country, year);
"""


def text_of(el, path):
    node = el.find(path)
    return (node.text or '').strip() if node is not None and node.text else ''


def parse_release(el):
    """The dump's shape, reduced to what the page shows. Returns None if unusable."""
    vid = ''
    for v in el.iterfind('videos/video'):
        m = YT.search(v.get('src') or '')
        if m:
            vid = m.group(1)
            break
    if not vid:
        return None

    title = text_of(el, 'title')
    if not title:
        return None

    names = [text_of(a, 'name') for a in el.iterfind('artists/artist')]
    artist = ', '.join(n for n in names if n) or '—'

    released = text_of(el, 'released')
    year = int(released[:4]) if released[:4].isdigit() else None

    label = catno = ''
    first = el.find('labels/label')
    if first is not None:
        label = (first.get('name') or '').strip()
        catno = (first.get('catno') or '').strip()

    # A release carries several <format> entries (Vinyl + its descriptions);
    # the name is what the app's format filter matches on.
    formats = {(f.get('name') or '').strip() for f in el.iterfind('formats/format')}
    formats.discard('')

    return {
        'id': int(el.get('id') or 0),
        'title': title,
        'artist': artist,
        'year': year,
        'country': text_of(el, 'country'),
        'label': label,
        'catno': catno,
        'genres': '\n'.join(g.text.strip() for g in el.iterfind('genres/genre') if g.text),
        'styles': '\n'.join(s.text.strip() for s in el.iterfind('styles/style') if s.text),
        'format': '\n'.join(sorted(formats)),
        'vinyl': 1 if any(f.lower() == 'vinyl' for f in formats) else 0,
        'video': vid,
    }


def build(src, out, year_from, year_to, formats, limit, count_only):
    stats = dict(seen=0, with_video=0, in_years=0, in_format=0, kept=0)
    decades = {}          # decade -> [with video, of those on vinyl]
    rows = []
    db = None
    if not count_only:
        if os.path.exists(out):
            os.remove(out)
        db = sqlite3.connect(out)
        db.execute('PRAGMA journal_mode = OFF')
        db.execute('PRAGMA synchronous = OFF')
        db.execute('PRAGMA cache_size = -200000')     # ~200 MB page cache
        db.executescript(SCHEMA)

    want_formats = {f.lower() for f in formats} if formats else None
    started = time.time()
    truncated = False

    # random() seeded per row rather than at query time: ORDER BY RANDOM() would
    # scan every matching row, an indexed rnd cursor touches one.
    import random
    rng = random.Random(20260901)

    try:
        with gzip.open(src, 'rb') as fh:
            for event, el in ET.iterparse(fh, events=('end',)):
                if el.tag != 'release':
                    continue
                stats['seen'] += 1

                rec = parse_release(el)
                if rec:
                    stats['with_video'] += 1
                    is_vinyl = 'vinyl' in rec['format'].lower()
                    dec = (rec['year'] // 10 * 10) if rec['year'] else 0
                    slot = decades.setdefault(dec, [0, 0])
                    slot[0] += 1
                    if is_vinyl:
                        slot[1] += 1
                    ok_year = True
                    if year_from is not None and (rec['year'] is None or rec['year'] < year_from):
                        ok_year = False
                    if year_to is not None and (rec['year'] is None or rec['year'] > year_to):
                        ok_year = False
                    if ok_year:
                        stats['in_years'] += 1
                    ok_fmt = True
                    if want_formats:
                        have = {f.lower() for f in rec['format'].split('\n') if f}
                        ok_fmt = bool(have & want_formats)
                    if ok_fmt:
                        stats['in_format'] += 1
                    if ok_year and ok_fmt:
                        stats['kept'] += 1
                        if db:
                            rec['rnd'] = rng.random()
                            rows.append(tuple(rec[k] for k in (
                                'id', 'title', 'artist', 'year', 'country',
                                'label', 'catno', 'genres', 'styles', 'format',
                                'vinyl', 'video', 'rnd')))
                            if len(rows) >= 20000:
                                flush(db, rows)

                el.clear()

                if stats['seen'] % 200000 == 0:
                    report(stats, started)
                if limit and stats['seen'] >= limit:
                    break
    except (EOFError, gzip.BadGzipFile, ET.ParseError) as e:
        # Expected while the download is still running — report on what we got.
        truncated = True
        print(f"\n  stream ended early ({type(e).__name__}) — partial run", file=sys.stderr)

    if db:
        flush(db, rows)
        db.executescript(INDEXES)
        db.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)', [
            ('source', os.path.basename(src)),
            ('built', time.strftime('%Y-%m-%d %H:%M:%S')),
            ('year_from', str(year_from or 'any')), ('year_to', str(year_to or 'any')),
            ('formats', ','.join(formats) if formats else 'any'),
            ('partial', '1' if truncated else '0'),
        ] + [(f'count_{k}', str(v)) for k, v in stats.items()])
        db.commit()
        db.close()

    report(stats, started, final=True)
    print_decades(decades)
    if not count_only and os.path.exists(out):
        print(f"  db                {os.path.getsize(out) / 1e6:,.0f} MB  ->  {out}")
    return stats


def print_decades(d):
    if not d:
        return
    print("\n  decade      with video        on vinyl", file=sys.stderr)
    for dec in sorted(d):
        vid, vinyl = d[dec]
        label = 'no year' if dec == 0 else f'{dec}s'
        print(f"  {label:<10}{vid:12,}{vinyl:16,}", file=sys.stderr)
    sys.stderr.flush()


def flush(db, rows):
    db.executemany('INSERT OR REPLACE INTO release VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    rows.clear()


def pct(n, of):
    return f"{100.0 * n / of:5.1f}%" if of else "    —"


def report(s, started, final=False):
    el = time.time() - started
    rate = s['seen'] / el if el else 0
    head = "\n=== final ===" if final else f"  {el:6.0f}s  {rate:8,.0f}/s"
    print(head, file=sys.stderr)
    print(f"  releases seen     {s['seen']:12,}", file=sys.stderr)
    print(f"  with youtube      {s['with_video']:12,}  {pct(s['with_video'], s['seen'])}", file=sys.stderr)
    print(f"  ... in year range {s['in_years']:12,}  {pct(s['in_years'], s['with_video'])} of those", file=sys.stderr)
    print(f"  ... in format     {s['in_format']:12,}  {pct(s['in_format'], s['with_video'])} of those", file=sys.stderr)
    print(f"  kept              {s['kept']:12,}  {pct(s['kept'], s['seen'])} of all", file=sys.stderr)
    sys.stderr.flush()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('dump', help='discogs_YYYYMMDD_releases.xml.gz')
    p.add_argument('-o', '--out', default='data/samples.db')
    # No year cut by default. The app's slider spans YR_MIN..YR_MAX (1900 to
    # next year), so anything trimmed here becomes an invisible wall: the handle
    # still moves, the table just goes empty. The flags stay for experiments.
    p.add_argument('--year-from', type=int, default=None)
    p.add_argument('--year-to', type=int, default=None)
    p.add_argument('--format', action='append', default=[],
                   help='keep only these format names (repeatable); default: any')
    p.add_argument('--limit', type=int, default=0, help='stop after N releases (trial run)')
    p.add_argument('--count-only', action='store_true', help='measure, write nothing')
    a = p.parse_args()
    build(a.dump, a.out, a.year_from, a.year_to, a.format, a.limit, a.count_only)


if __name__ == '__main__':
    main()
