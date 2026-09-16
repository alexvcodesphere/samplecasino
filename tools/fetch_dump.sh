#!/bin/sh
# Fetch a Discogs monthly releases dump into data/.
#
#   tools/fetch_dump.sh            # newest dump published
#   tools/fetch_dump.sh 20260901   # a specific one
#
# data.discogs.com throttles per IP and the cooldown runs to the hour, so this
# waits minutes between attempts rather than hammering — hammering is what keeps
# it alive. -f matters: on a 429 curl must write nothing, or the 50-byte throttle
# reply lands in the middle of a resumed .gz and quietly corrupts it.
set -e
cd "$(dirname "$0")/.."
mkdir -p data

BASE='https://data.discogs.com'
WAIT=${WAIT:-900}

STAMP="$1"
if [ -z "$STAMP" ]; then
  YEAR=$(date +%Y)
  STAMP=$(curl -sL --max-time 60 "$BASE/?prefix=data%2F$YEAR%2F" \
          | grep -oE 'discogs_[0-9]{8}_releases\.xml\.gz' | sort -u | tail -1 \
          | grep -oE '[0-9]{8}') || true
  [ -z "$STAMP" ] && { echo "could not work out the newest dump; pass one, e.g. 20260901"; exit 1; }
  echo "newest dump: $STAMP"
fi

OUT="data/discogs_${STAMP}_releases.xml.gz"
URL="$BASE/?download=data%2F$(echo "$STAMP" | cut -c1-4)%2Fdiscogs_${STAMP}_releases.xml.gz"

size_of() { [ -f "$1" ] && wc -c < "$1" | tr -d ' ' || echo 0; }

# What the server says the finished file weighs. Completeness is decided by
# comparing against this rather than by `gzip -t`: decompressing 11 GB off
# network storage takes minutes, long enough that the retry loop starts a second
# check on top of the first, and it answers a question curl already answered.
# grep -i, not awk's IGNORECASE: that is a gawk extension and this runs under
# mawk on the server, where it silently matches nothing.
EXPECT=$(curl -sIL --max-time 120 "$URL" | tr -d '\r' | grep -i '^content-length:' | tail -1 | awk '{print $2}')
[ -n "$EXPECT" ] && echo "expecting $EXPECT bytes"

if [ "$(size_of "$OUT")" = "$EXPECT" ] && [ -n "$EXPECT" ]; then
  echo "already complete -> $OUT"
  exit 0
fi

n=0
while :; do
  n=$((n + 1))
  have=$(size_of "$OUT")
  printf '%s  attempt %d — have %s bytes\n' "$(date '+%H:%M:%S')" "$n" "$have"

  set +e
  # -f so a 429 body is never written: with -C - those 50 bytes of JSON would
  # land in the middle of a resumed .gz and quietly corrupt it.
  curl -fL -C - --max-time 7200 --connect-timeout 30 -# "$URL" -o "$OUT"
  rc=$?
  set -e

  now=$(size_of "$OUT")

  # curl exiting 0 means the transfer finished — that alone ends this, with the
  # expected size as a second opinion when the HEAD gave one. Hanging completion
  # on EXPECT alone is what made this loop immortal when the HEAD came back
  # empty: a finished download, waiting 900s to try again, forever.
  if [ "$rc" -eq 0 ] && [ "$now" -gt 0 ]; then
    if [ -z "$EXPECT" ] || [ "$now" = "$EXPECT" ]; then
      printf '%s  COMPLETE — %s bytes -> %s\n' "$(date '+%H:%M:%S')" "$now" "$OUT"
      exit 0
    fi
    printf '%s  curl finished at %s bytes, expected %s — retrying\n' \
      "$(date '+%H:%M:%S')" "$now" "$EXPECT"
  fi

  if [ "$now" -gt "$have" ]; then
    printf '%s  progress, continuing\n' "$(date '+%H:%M:%S')"; sleep 5
  else
    printf '%s  no progress (curl %d) — waiting %ds\n' "$(date '+%H:%M:%S')" "$rc" "$WAIT"; sleep "$WAIT"
  fi
done
