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
import http.server
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


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/health':
            self.send_json(200, {'status': 'ok'})
        elif parsed.path == '/config':
            # Whether a token exists, never the token itself.
            self.send_json(200, {'serverToken': bool(DISCOGS_TOKEN),
                                 'downloads': ENABLE_DOWNLOAD})
        elif parsed.path == '/discogs' or parsed.path.startswith('/discogs/'):
            self.handle_discogs(parsed)
        elif parsed.path == '/download':
            self.handle_download(parsed)
        else:
            if parsed.path == '/':
                self.path = '/' + APP_FILE   # serve the app on the main route
            super().do_GET()

    def handle_discogs(self, parsed):
        if not DISCOGS_TOKEN:
            self.send_json(404, {'error': 'No server-side Discogs token configured.'})
            return

        # The Discogs path is carried in our own path, and the query is passed
        # through untouched. It used to travel in a "path" parameter, which
        # Codesphere's edge 403s unless it happens to be the first parameter in
        # the query string — not a thing worth depending on, and invisible
        # locally because only the edge does it.
        #
        # parsed.path is NOT unquoted, so an encoded traversal stays encoded and
        # simply fails to match below rather than slipping through as "..".
        path = parsed.path[len('/discogs'):]
        if not DISCOGS_PATH_RE.match(path):
            self.send_json(400, {'error': 'Unsupported Discogs path.'})
            return

        req = urllib.request.Request(
            DISCOGS_API + path + ('?' + parsed.query if parsed.query else ''),
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
            self.send_json(502, {'error': 'Could not reach Discogs.'})
            return

        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        # Discogs reports what is left of the bucket, but only lists Location in
        # its access-control-expose-headers, so a browser calling Discogs
        # directly cannot read these. Through this proxy it can — and with a shared
        # server token that count is the only way one viewer learns that another
        # has been spending it.
        for h in ('X-Discogs-Ratelimit', 'X-Discogs-Ratelimit-Remaining', 'X-Discogs-Ratelimit-Used'):
            if headers.get(h) is not None:
                self.send_header(h, headers.get(h))
        self.end_headers()
        self.wfile.write(body)

    def handle_download(self, parsed):
        if not ENABLE_DOWNLOAD:
            self.send_json(404, {'error': 'Downloads are disabled on this server.'})
            return
        qs = urllib.parse.parse_qs(parsed.query)
        url = (qs.get('url') or [''])[0]
        title = (qs.get('title') or ['track'])[0]

        if not YOUTUBE_RE.match(url):
            self.send_json(400, {'error': 'Not a valid YouTube URL.'})
            return

        safe_title = re.sub(r'[^\w\s.,()\'&-]', '', title).strip() or 'track'

        with tempfile.TemporaryDirectory() as tmp:
            outtmpl = os.path.join(tmp, '%(title)s.%(ext)s')
            try:
                subprocess.run(
                    ytdlp_command() + youtube_args() + ['-x', '--audio-format', 'mp3',
                     '--audio-quality', '0', '--no-playlist', '-o', outtmpl, '--', url],
                    check=True, capture_output=True, text=True, timeout=180,
                    env=subprocess_env()
                )
            except FileNotFoundError:
                self.send_json(500, {'error': 'yt-dlp is not installed on the server.'})
                return
            except subprocess.TimeoutExpired:
                self.send_json(504, {'error': 'Download timed out.'})
                return
            except subprocess.CalledProcessError as e:
                err = (e.stderr or '').strip()
                if 'No module named' in err:
                    self.send_json(500, {'error': 'yt-dlp is not installed on the server.'})
                    return
                # A missing JS runtime or solver script surfaces as a bare "HTTP Error
                # 403" on the media URL, which points nowhere useful. The real cause is
                # in the warnings above it, so check for those first.
                if ('challenge solving failed' in err or 'Signature solving failed' in err
                        or 'No supported JavaScript runtime' in err):
                    self.send_json(500, {'error': 'yt-dlp cannot solve YouTube\'s JS '
                                        'challenge. Install a JS runtime (deno) and the '
                                        'yt-dlp-ejs solver package on the server.'})
                    return
                msg = err.splitlines()[-1] if err else 'yt-dlp failed.'
                self.send_json(502, {'error': msg[:200]})
                return

            files = [f for f in os.listdir(tmp) if f.endswith('.mp3')]
            if not files:
                self.send_json(502, {'error': 'No audio file produced (is ffmpeg installed?).'})
                return

            path = os.path.join(tmp, files[0])
            size = os.path.getsize(path)
            fname = urllib.parse.quote(safe_title + '.mp3')

            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Content-Length', str(size))
            self.send_header('Content-Disposition', f"attachment; filename*=UTF-8''{fname}")
            self.end_headers()
            with open(path, 'rb') as f:
                self.wfile.write(f.read())

    def send_json(self, status, obj):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if '/download' in (self.path or ''):
            super().log_message(fmt, *args)
        # keep static-file request logs quiet


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    httpd = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'Serving Sample Casino on http://{HOST}:{PORT} (Ctrl+C to stop)')
    httpd.serve_forever()
