# Sample Casino

Pull a record you've never heard of.

Sample Casino rolls one random release out of Discogs inside your filters, finds its
YouTube preview, plays it, and scores it on how much more wanted than owned it is. It
is built to be streamed: a 16:9 stage, a dark ground, and a reserved corner the layout
never occupies so a webcam can sit there in OBS.

The whole app is one file — [`sample-digger.html`](sample-digger.html) — served by a
small Python stdlib server with no dependencies of its own.

---

## Run it

```bash
python3 server.py
```

Then open <http://localhost:8765>. On macOS, `Start Sample Digger.command` does the
same thing with a double-click.

It has to be served over HTTP. YouTube refuses to embed on `file://` pages — error 153,
no referer — so opening the HTML directly will load everything except the player.

Python 3.9+ is enough for the app itself. The Download button needs more (see below).

---

## Configuration

Everything is optional; the app works with none of it.

| Variable | Default | What it does |
| --- | --- | --- |
| `PORT` | `8765` | Port to listen on |
| `HOST` | `127.0.0.1` | Bind address. Set `0.0.0.0` behind a router |
| `DISCOGS_TOKEN` | *(unset)* | Signs Discogs requests server-side |

### About the Discogs token

A token is worth setting. Without one Discogs allows **25 requests a minute** and
returns **no cover art at all** from search — `cover_image` comes back empty, which is
why the rolling animation falls back to YouTube thumbnails. With one you get 60 a
minute and real sleeves.

There are two places a token can live, and they behave differently:

- **`DISCOGS_TOKEN` on the server.** The page never sees it. The browser calls
  `/discogs/...` and the server attaches the credential, because a token embedded in
  the HTML would be readable by anyone who loads the page — and a Discogs personal
  token reaches that account's collection, wantlist and marketplace. One bucket of 60,
  **shared by everyone watching**.
- **A viewer's own token**, pasted into the drawer and kept in their `localStorage`.
  Sent straight from their browser, never proxied. Discogs meters per token, so this is
  a *private* 60 — which is why pasting your own still buys you more rolls even when the
  server already has one.

A personal token wins when both are present.

On Codesphere, set `DISCOGS_TOKEN` as a workspace secret. Not in `ci.yml` — that file
is committed.

---

## How a roll works

1. **Refill the pool.** A random year inside the range plus the other filters go to
   `database/search`. The page count for that filter+year is cached in `localStorage`,
   so later refills jump straight to a random — often deep, therefore obscure — page.
   One refill buys 25 candidates and is reused for at most six pulls, so the year
   rotates often enough to feel random.
2. **Pick a release** at random out of the pool and fetch it in full.
3. **Vet its videos.** Embeddable YouTube links between 45s and 15min, shuffled, each
   checked against its thumbnail — a dead video serves a 120×90 placeholder, a live one
   480×360. The first survivor plays; the rest are vetted anyway and their thumbnails
   kept for the rolling animation, which costs no Discogs request.
4. **Score it.** `want / (have + 1)`. At or above the grail threshold the pull goes gold.

If a release has YouTube links but all of them are dead, that still counts as a pull and
gets a row — `nopreview`, the one state where Discogs has it and YouTube doesn't.

---

## The run

A run is a batch of pulls. By default it is **endless**: it never ends on its own, the
table shows the last five, and starting over is an explicit act — the drawer's new-run
button, or <kbd>N</kbd>. Set a fixed length of 1–6 in the drawer and the run completes
at that length instead, after which ROLL starts a new one.

## Keyboard

| Key | |
| --- | --- |
| <kbd>space</kbd> | roll the next pull |
| <kbd>K</kbd> | play / pause |
| <kbd>F</kbd> | favorite this pull |
| <kbd>Y</kbd> | download the mp3 |
| <kbd>C</kbd> | copy the YouTube link |
| <kbd>R</kbd> | tap tempo |
| <kbd>N</kbd> | start a new run |
| <kbd>S</kbd> | open / close settings |
| <kbd>esc</kbd> | close settings |

The same list is in the drawer, generated from the same table, so the two cannot drift.

---

## Chips

The chip dots in the top bar are the Discogs rate limit expressed as a stake. The app
tracks its own requests in a rolling 60-second window and divides the remainder by the
*observed* cost of a pull, so one chip is one roll you can actually afford. When the
server's token is in use its real remaining count is read from the forwarded
`X-Discogs-Ratelimit-Remaining` header, since this browser's request log cannot see what
other viewers have spent.

---

## Endpoints

| Route | |
| --- | --- |
| `/` | the app |
| `/health` | `{"status": "ok"}` |
| `/config` | `{"serverToken": bool}` — whether a token exists, never the token |
| `/discogs/database/search?…`<br>`/discogs/releases/<id>` | Discogs proxy, server-signed |
| `/download?url=&title=` | shells out to yt-dlp, streams back an MP3 |

The proxy forwards **only** those two Discogs paths, so it cannot be turned into an open
proxy for the rest of the API. Everything else is refused with a 400 before any outbound
call is made.

---

## Downloads

`/download` needs three things on the server:

- **yt-dlp and `yt-dlp-ejs` in the same Python interpreter.** yt-dlp imports the solver
  package to answer YouTube's JS challenge. A standalone yt-dlp binary brings its own
  Python that cannot see it, and downloads then fail with a bare HTTP 403 that says
  nothing about the real cause.
- **A JavaScript runtime** — deno, node or bun — for that same challenge.
- **ffmpeg**, for the MP3 conversion.

```bash
python3 -m pip install --user -U yt-dlp yt-dlp-ejs
```

Without them the app still runs; only the Download button breaks. [`ci.yml`](ci.yml)
installs all three and fails the deploy loudly if they are wired up wrong.

---

## Stored locally

All in `localStorage`, all per-browser, none of it leaves the machine:

| Key | |
| --- | --- |
| `digger_token` | the viewer's own Discogs token |
| `digger_favs` | favorites |
| `digger_pagecount` | filter+year → page count, so the probe happens once |
| `casino_settings` | run length, grail threshold |
| `casino_counts` | table sizes per filter key |
| `casino_covers` | recent cover URLs, so a returning session starts warm |

Filters themselves are session-only by design.

---

## Things that are true about Discogs and cost time to learn

- Search returns **no images** unless the request is authenticated.
- There is **no year-range parameter**. Only an exact `year`. The app picks a random year
  inside your range per refill, which is why the top bar reports the range separately
  from the count.
- **Several styles go in one comma-joined value and are ANDed.** `style=Funk,Soul` is the
  crossover — 340,602 and 523,688 releases narrowing to 108,445. Repeating the parameter
  does nothing: `style=Funk&style=Soul` silently honours the first and drops the rest.
- Over the limit it answers **429** with `{"message":"You are making requests too
  quickly."}` — never a bare 403. A 403 is something else.
- A **wrong token does not announce itself.** It still reports the authenticated rate
  limit. The tell is missing cover art.

## And one about Codesphere

Its edge returns **403** for a query string containing `path=/…` unless `path` happens to
be the first parameter. That is why the Discogs proxy carries the path in the URL
(`/discogs/database/search`) rather than a parameter. Invisible locally, because nothing
sits in front of the server there.

---

## Layout

```
sample-digger.html   the app — markup, design tokens, state machine, Discogs + YouTube
server.py            static files, the Discogs proxy, the download endpoint
ci.yml               Codesphere prepare / test / run
cratedigger.html     the earlier v2 UI, kept, not part of this app
```
