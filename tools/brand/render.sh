#!/bin/sh
set -e
cd "${1:-/tmp/amore_logo}"
echo 'svg{color:#6f8f73}' > _sage.css
rsvg-convert --stylesheet _sage.css -w 512 -h 512 mark.svg -o _mark-512.png
rsvg-convert --stylesheet _sage.css -w 64 -h 64 mark.svg -o _mark-64.png
rsvg-convert --stylesheet _sage.css -w 512 -h 512 mark-tile.svg -o _tile-512.png
rsvg-convert -w 32 -h 32 favicon.svg -o favicon-32.png
rsvg-convert --stylesheet _sage.css -w 180 -h 180 _apple-square.svg -o favicon-180.png
rsvg-convert -z 2 _preview.svg -o preview.png
rsvg-convert -z 2 _lockup.svg -o lockup.png
# zoomed small sizes for inspection (nearest-neighbour upscale via a quick python/PIL if available)
rsvg-convert --stylesheet _sage.css -w 16 -h 16 mark.svg -o _mark-16.png
rsvg-convert --stylesheet _sage.css -w 24 -h 24 mark.svg -o _mark-24.png
ls -la *.png *.svg
