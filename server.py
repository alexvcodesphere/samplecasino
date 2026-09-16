#!/usr/bin/env python3
"""
Server for Sample Casino.

Serves the static files and the single-page app on the root route, plus one
extra endpoint the app's Download button calls:

    GET /download?url=<youtube watch url>&title=<artist - title>   (opt-in)

which shells out to yt-dlp to grab the audio as an MP3 and streams it back to
the browser as a file download. That endpoint is off by default; set
ENABLE_DOWNLOAD=1 to turn it on, which also makes the button appear in the UI.

Runs locally (double-click "Start Sample Digger.command") and on Codesphere.
The port comes from $PORT (Codesphere sets 3000); it defaults to 8765 locally.
Bound to 127.0.0.1 — Codesphere routes external traffic to localhost, and the
download endpoint executes a subprocess based on request input.
"""
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import os
import urllib.error
import urllib.parse
import urllib.request

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

import logging
import time

import db as db_layer

log = logging.getLogger('casino')

PORT = int(os.environ.get('PORT', '8765'))
# Locally bind loopback (the /download endpoint runs a subprocess, so don't expose
# it to the LAN); on Codesphere set HOST=0.0.0.0 so the workspace router can reach it.
HOST = os.environ.get('HOST', '127.0.0.1')
APP_FILE = 'sample-digger.html'   # served on the main route "/"
YOUTUBE_RE = re.compile(r'^https://(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]{11}([&?].*)?$')

# A Discogs token raises the rate limit from 25 to 60 requests a minute and is
# the only way to get cover art out of search. Set DISCOGS_TOKEN and the app
# stops asking each viewer for one.
#
# The token is deliberately NOT handed to the page. The browser talks to Discogs
# directly, so a token embedded in the HTML would be readable by anyone who
# loads it — and a Discogs personal token reaches that account's collection,
# wantlist and marketplace. Instead the browser calls /discogs and the server
# attaches the credential, so it never leaves this process.
# The MP3 download shells out to yt-dlp, which is fine on your own machine and
# not something a public deployment should offer to everyone who opens the page.
# It is therefore off unless ENABLE_DOWNLOAD is explicitly turned on, and the
# gate is here rather than only in the UI — hiding the button would still leave
# /download reachable by anyone who guessed the URL.
ENABLE_DOWNLOAD = os.environ.get('ENABLE_DOWNLOAD', '').strip().lower() in ('1', 'true', 'yes', 'on')

# The sample index, built by tools/build_db.py. Absent, the app falls back to
# searching Discogs the old way — four requests a pull instead of one — so this
# is optional rather than fatal.
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///data/samples.db')
INDEX = None
try:
    INDEX = db_layer.open_db(DATABASE_URL)
except Exception as e:                        # noqa: BLE001 — any failure is the same answer
    print(f'!! NO SAMPLE INDEX: {e}', file=sys.stderr)
    print(f'!! url={DATABASE_URL} cwd={os.getcwd()}', file=sys.stderr)
    print('!! the app will search Discogs directly — four requests a pull',
          file=sys.stderr)

DISCOGS_API = 'https://api.discogs.com'
DISCOGS_TOKEN = os.environ.get('DISCOGS_TOKEN', '').strip()
# Only the two endpoints the app actually uses, so this can't be turned into an
# open proxy for the rest of the Discogs API.
DISCOGS_PATH_RE = re.compile(r'^/(database/search|releases/\d+)$')
# Discogs rejects requests without a descriptive User-Agent.
DISCOGS_UA = 'SampleCasino/1.0 (+https://github.com/alexvcodesphere/samplecasino)'

# ffmpeg and deno come from Nix on Codesphere (see ci.yml), which lands them in
# ~/.nix-profile/bin. The subprocess PATH is augmented with that dir so yt-dlp
# can find ffmpeg for the mp3 conversion.
NIX_BIN = os.path.expanduser('~/.nix-profile/bin')

# YouTube encrypts its media URLs behind a JS challenge (the "sig"/"n" params).
# yt-dlp needs BOTH a JavaScript runtime and the yt-dlp-ejs solver script to
# answer it; without them it silently falls back to the android_vr client, whose
# media URLs YouTube rejects with HTTP 403. Only deno is enabled by default, so
# name the other runtimes too and let yt-dlp pick whichever is installed.
JS_RUNTIMES = ['deno', 'node', 'bun']
# The default client order also lands on android_vr (403). web_safari and mweb
# hand back working links. Their https audio-only formats need a GVS PO token we
# don't have, so only the muxed HLS streams are usable: cap the height to keep
# the download small, but stay above 240p — 144p/240p carry HE-AAC (mp4a.40.5),
# while 360p and up carry full AAC-LC (mp4a.40.2), which is what we extract from.
PLAYER_CLIENTS = 'web_safari,mweb'
AUDIO_FORMAT = 'bestaudio/best[height<=480][height>=360]/best[height<=480]/best'


def youtube_args():
    args = []
    for runtime in JS_RUNTIMES:
        args += ['--js-runtimes', runtime]
    args += ['--extractor-args', f'youtube:player_client={PLAYER_CLIENTS}']
    args += ['-f', AUDIO_FORMAT]
    return args


def ytdlp_command():
    # Run yt-dlp as a module of THIS interpreter first. yt-dlp has to import the
    # yt_dlp_ejs solver package to answer YouTube's JS challenge, so the two must
    # live in the same interpreter — a standalone yt-dlp binary (Nix, pipx, a
    # distro package) brings its own Python that cannot see our site-packages,
    # and downloads then fail with a bare HTTP 403. Only fall back to a binary if
    # the module isn't importable at all.
    if importlib.util.find_spec('yt_dlp') is not None:
        return [sys.executable, '-m', 'yt_dlp']
    nix = os.path.join(NIX_BIN, 'yt-dlp')
    if os.path.exists(nix):
        return [nix]
    found = shutil.which('yt-dlp')
    if found:
        return [found]
    return [sys.executable, '-m', 'yt_dlp']


def subprocess_env():
    env = dict(os.environ)
    if os.path.isdir(NIX_BIN):
        env['PATH'] = NIX_BIN + os.pathsep + env.get('PATH', '')
    return env


app = FastAPI(title='Sample Casino', docs_url=None, redoc_url=None)

# Counted per source so the split is visible at a glance: pulls answered from
# the index versus pulls the page had to go to Discogs for.
SERVED = {'pull': 0, 'counts': 0, 'discogs': 0}


@app.exception_handler(HTTPException)
def as_error(request, exc):
    """The page reads `error` off a failed response; FastAPI's own shape is
    `detail`. Keeping the old key means the server change is invisible to it."""
    return JSONResponse({'error': exc.detail}, status_code=exc.status_code)


def _describe(f):
    """A filter in one short line, for the log."""
    bits = []
    if f.vinyl:
        bits.append('vinyl')
    if f.year_from or f.year_to:
        bits.append(f'{f.year_from or ""}-{f.year_to or ""}')
    if f.genre:
        bits.append(f.genre)
    if f.styles:
        bits.append('+'.join(f.styles))
    if f.country:
        bits.append(f.country)
    return '[' + ' '.join(bits) + ']' if bits else '[any]'


def _filters(genre, style, country, fmt, year_from, year_to):
    """The drawer's query string, as the index understands it. Style arrives as
    one comma-joined value and is ANDed, the way Discogs treats it."""
    return db_layer.Filters(
        genre=(genre or '').strip(),
        styles=[s for s in (style or '').split(',') if s.strip()],
        country=(country or '').strip(),
        vinyl=(fmt or '').lower() == 'vinyl',
        year_from=year_from,
        year_to=year_to)


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.get('/stats')
def stats():
    """What the index is, so 'is it actually serving from the database' has an
    answer you can read rather than infer. Empty when there is no index."""
    if INDEX is None:
        return {'index': False,
                'reason': f'no database at {DATABASE_URL}', 'cwd': os.getcwd(),
                'served': SERVED}
    return {'index': True, 'url': DATABASE_URL,
            'path': getattr(INDEX, 'path', None), 'cwd': os.getcwd(),
            'meta': INDEX.meta(), 'served': SERVED}


@app.get('/config')
def config():
    # Whether a token exists, never the token itself.
    return {'serverToken': bool(DISCOGS_TOKEN),
            'downloads': ENABLE_DOWNLOAD,
            'index': INDEX is not None}


# Handlers are sync on purpose: sqlite, urllib and subprocess all block, and
# FastAPI runs a `def` endpoint in its threadpool. Declaring them `async` would
# park that work on the event loop and stall every other request.
@app.get('/counts')
def counts(genre: str = '', style: str = '', country: str = '', format: str = '',
           yearFrom: int = None, yearTo: int = None):
    if INDEX is None:
        raise HTTPException(503, 'No sample index on this server.')
    f = _filters(genre, style, country, format, yearFrom, yearTo)
    t0 = time.perf_counter()
    n = INDEX.count(f)
    SERVED['counts'] += 1
    log.info('counts %s -> %d in %.1fms', _describe(f), n, (time.perf_counter() - t0) * 1000)
    return {'count': n}


@app.get('/pull')
def pull(genre: str = '', style: str = '', country: str = '', format: str = '',
         yearFrom: int = None, yearTo: int = None):
    """One random release matching the filters — what used to cost a Discogs
    search plus a release fetch per candidate. Have/want are not in the dump, so
    the page still asks Discogs for those, once."""
    if INDEX is None:
        raise HTTPException(503, 'No sample index on this server.')
    f = _filters(genre, style, country, format, yearFrom, yearTo)
    t0 = time.perf_counter()
    track = INDEX.pick(f)
    ms = (time.perf_counter() - t0) * 1000
    if track is None:
        log.info('pull  %s -> nothing in %.1fms', _describe(f), ms)
        raise HTTPException(404, 'Nothing matches these filters.')
    SERVED['pull'] += 1
    log.info('pull  %s -> %s (%s) via %s in %.1fms',
             _describe(f), track['release'][:40], track['videoId'],
             INDEX.last_plan, ms)
    return track


@app.get('/discogs/{rest:path}')
def discogs(rest: str, request: Request):
    if not DISCOGS_TOKEN:
        raise HTTPException(404, 'No server-side Discogs token configured.')

    # The Discogs path is carried in our own path, and the query is passed
    # through untouched. It used to travel in a "path" parameter, which
    # Codesphere's edge 403s unless it happens to be the first parameter in the
    # query string — not a thing worth depending on, and invisible locally
    # because only the edge does it.
    path = '/' + rest
    if not DISCOGS_PATH_RE.match(path):
        raise HTTPException(400, 'Unsupported Discogs path.')

    query = request.url.query
    req = urllib.request.Request(
        DISCOGS_API + path + ('?' + query if query else ''),
        headers={
            'Authorization': 'Discogs token=' + DISCOGS_TOKEN,
            'User-Agent': DISCOGS_UA,
            'Accept': 'application/json',
        })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body, status, headers = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        # Pass the status through — the app reads 429 as a spent balance.
        body, status, headers = (e.read() or b'{}'), e.code, e.headers
    except Exception:
        raise HTTPException(502, 'Could not reach Discogs.')

    SERVED['discogs'] += 1
    log.info('discogs %s -> %d  (index served %d pulls, %d counts)',
             path, status, SERVED['pull'], SERVED['counts'])

    # Discogs reports what is left of the bucket, but only lists Location in its
    # access-control-expose-headers, so a browser calling Discogs directly
    # cannot read these. Through this proxy it can — and with a shared server
    # token that count is the only way one viewer learns another has been
    # spending it.
    passthrough = {h: headers.get(h) for h in
                   ('X-Discogs-Ratelimit', 'X-Discogs-Ratelimit-Remaining',
                    'X-Discogs-Ratelimit-Used')
                   if headers.get(h) is not None}
    return Response(content=body, status_code=status,
                    media_type='application/json', headers=passthrough)


@app.get('/download')
def download(url: str = '', title: str = 'track'):
    if not ENABLE_DOWNLOAD:
        raise HTTPException(404, 'Downloads are disabled on this server.')
    if not YOUTUBE_RE.match(url):
        raise HTTPException(400, 'Not a valid YouTube URL.')

    safe_title = re.sub(r'[^\w\s.,()\'&-]', '', title).strip() or 'track'
    tmp = tempfile.mkdtemp()
    outtmpl = os.path.join(tmp, '%(title)s.%(ext)s')
    try:
        subprocess.run(
            ytdlp_command() + youtube_args() + ['-x', '--audio-format', 'mp3',
             '--audio-quality', '0', '--no-playlist', '-o', outtmpl, '--', url],
            check=True, capture_output=True, text=True, timeout=180,
            env=subprocess_env())
    except FileNotFoundError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(500, 'yt-dlp is not installed on the server.')
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(504, 'Download timed out.')
    except subprocess.CalledProcessError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        err = (e.stderr or '').strip()
        if 'No module named' in err:
            raise HTTPException(500, 'yt-dlp is not installed on the server.')
        # A missing JS runtime or solver script surfaces as a bare "HTTP Error
        # 403" on the media URL, which points nowhere useful. The real cause is
        # in the warnings above it, so check for those first.
        if ('challenge solving failed' in err or 'Signature solving failed' in err
                or 'No supported JavaScript runtime' in err):
            raise HTTPException(500, "yt-dlp cannot solve YouTube's JS challenge. "
                                'Install a JS runtime (deno) and the yt-dlp-ejs '
                                'solver package on the server.')
        raise HTTPException(502, (err.splitlines()[-1] if err else 'yt-dlp failed.')[:200])

    files = [f for f in os.listdir(tmp) if f.endswith('.mp3')]
    if not files:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(502, 'No audio file produced (is ffmpeg installed?).')

    fname = urllib.parse.quote(safe_title + '.mp3')
    return FileResponse(
        os.path.join(tmp, files[0]), media_type='audio/mpeg',
        headers={'Content-Disposition': f"attachment; filename*=UTF-8\'\'{fname}"},
        # the temp dir outlives the handler now, so it is cleaned after the send
        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))


@app.get('/')
def index():
    return FileResponse(APP_FILE, media_type='text/html')


# Deliberately no static mount. The old server handed out the whole working
# directory — server.py, db.py and a listing of data/ were all fetchable — and
# the page needs nothing local beyond itself.


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f'Serving Sample Casino on http://{HOST}:{PORT} (Ctrl+C to stop)')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(message)s',
                        datefmt='%H:%M:%S')
    if INDEX is not None:
        m = INDEX.meta()
        print(f"index: {m.get('count_kept', '?')} samples from "
              f"{m.get('source', '?')}, built {m.get('built', '?')}")
    uvicorn.run(app, host=HOST, port=PORT, log_level='warning', access_log=False)
