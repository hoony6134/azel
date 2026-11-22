#!/bin/sh
# Pushes the gateway URL and the TV token to a Roku over ECP so nothing has to be typed with the remote.
# Usage: ./provision.sh <roku-ip> [gateway-url | demo]   (token from ~/.config/biseo/tv_token on THIS machine; never printed)
set -eu
ROKU="${1:?roku ip}"
URL="${2:-https://azel.scian.xyz}"
FILE="${BISEO_HOME:-$HOME/.config/biseo}/tv_token"
[ -r "$FILE" ] || { echo "no $FILE" >&2; exit 1; }
TOKEN=$(tr -d '\n' < "$FILE")
# the Roku is on your LAN; the token only travels there, never to the internet
/usr/bin/curl --noproxy '*' -sS -o /dev/null -w "%{http_code}\n" -X POST \
  --data-urlencode "azelUrl=$URL" --data-urlencode "azelToken=$TOKEN" -G "http://$ROKU:8060/launch/dev"
