#!/bin/sh
# Rebuilds assistant/site icons and og.png from the app icon (design/icon). Needs Google Chrome, sips, ffmpeg.
set -e
cd "$(dirname "$0")"
SITE=../../site
ICON=../icon/AppIcon-1024.png
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
sed 's|<rect width="1024" height="1024" fill="url(#jade)"/>|<rect width="1024" height="1024" rx="230" fill="url(#jade)"/>|' ../icon/azel-ios.svg > $SITE/favicon.svg
cp ../icon/azel-ios-dark.svg $SITE/assets/jar.svg
for s in 16 32 192 512; do sips -s format png -z $s $s $ICON --out $SITE/icon-$s.png >/dev/null; done
sips -z 180 180 $ICON --out $SITE/apple-touch-icon.png >/dev/null
ffmpeg -loglevel error -y -i $SITE/icon-32.png $SITE/favicon.ico
"$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 --window-size=1200,630 \
  --screenshot="$PWD/$SITE/og.png" "file://$PWD/og.html" 2>/dev/null
echo done
