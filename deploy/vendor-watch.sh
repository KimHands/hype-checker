#!/bin/sh
# watch 스킬(MIT, 0.3.2) 스크립트를 이미지 빌드용으로 deploy/vendor/ 에 복사한다(커밋하지 않음).
# 다시 구현하지 않고 맥에서 쓰는 것과 같은 판을 그대로 호출한다. rsync 전에 실행.
set -eu
SRC="$HOME/.claude/plugins/cache/claude-video/watch/0.3.2"
DEST="$(dirname "$0")/vendor/watch-0.3.2"
rm -rf "$DEST"
mkdir -p "$DEST/scripts"
cp "$SRC/LICENSE" "$DEST/"
cp "$SRC"/skills/watch/scripts/*.py "$DEST/scripts/"
echo "watch 0.3.2 → $DEST"
