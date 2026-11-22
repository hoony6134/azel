#!/bin/sh
# Zips this channel and sideloads it onto a Roku in developer mode (digest auth, password from a 0600 file).
# Usage: ./deploy.sh [roku-ip]   (default 192.168.0.106; password: ~/.config/biseo/roku_dev_password, never printed)
# build_version is bumped in the copy that gets zipped, so the Roku never answers "identical, not replacing".
set -eu
ROKU="${1:-192.168.0.106}"
PW="${BISEO_HOME:-$HOME/.config/biseo}/roku_dev_password"
[ -r "$PW" ] || { echo "no $PW" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
TMP=$(mktemp -d)
cp -R "$SRC/manifest" "$SRC/source" "$SRC/components" "$SRC/fonts" "$TMP/"
sed -i.bak "s/^build_version=.*/build_version=$(($(date +%s) / 60 % 100000))/" "$TMP/manifest" && rm "$TMP/manifest.bak"
(cd "$TMP" && zip -qr "$TMP/azel-tv.zip" manifest source components fonts)
unzip -l "$TMP/azel-tv.zip" | grep -q ' manifest$' || { echo "manifest is not at the zip root" >&2; exit 1; }
/usr/bin/curl --noproxy '*' -sS -m 120 --digest -u "rokudev:$(tr -d '\n' < "$PW")" \
  -F mysubmit=Install -F "archive=@$TMP/azel-tv.zip" "http://$ROKU/plugin_install" | grep -o 'Install Success\|Identical to previous[^<"]*\|Install Failure[^<]*\|Install timeout[^<"]*' | head -2
rm -rf "$TMP"
