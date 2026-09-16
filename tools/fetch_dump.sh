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

n=0
while :; do
  n=$((n + 1))
  have=0; [ -f "$OUT" ] && have=$(wc -c < "$OUT" | tr -d ' ')
  printf '%s  attempt %d — have %s bytes\n' "$(date '+%H:%M:%S')" "$n" "$have"

  set +e
  curl -fL -C - --max-time 7200 --connect-timeout 30 -# "$URL" -o "$OUT"
  rc=$?
  set -e

  now=0; [ -f "$OUT" ] && now=$(wc -c < "$OUT" | tr -d ' ')
  if [ "$rc" -eq 0 ] && gzip -t "$OUT" 2>/dev/null; then
    printf '%s  COMPLETE — %s bytes, gzip intact -> %s\n' "$(date '+%H:%M:%S')" "$now" "$OUT"
    exit 0
  fi
  if [ "$now" -gt "$have" ]; then
    printf '%s  progress, continuing\n' "$(date '+%H:%M:%S')"; sleep 5
  else
    printf '%s  refused (curl %d) — waiting %ds\n' "$(date '+%H:%M:%S')" "$rc" "$WAIT"; sleep "$WAIT"
  fi
done
