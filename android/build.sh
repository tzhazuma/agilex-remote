#!/usr/bin/env bash
#
# Hand-rolled Android build for the AgileX Remote WebView shell.
#   aapt2 (compile+link) -> javac -> d8 -> zipalign -> apksigner
# Deliberately avoids Gradle: there is no Gradle on this Mac and downloading one
# would be a large, pointless fetch for a single-Activity app.
#
# Usage:  ./build.sh
# Output: ./agilex-remote.apk   (debug-signed)
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK="${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}"

# --- pick toolchain (prefer newest present, no `sort -V` for BSD portability) ---
BT="${BUILD_TOOLS:-$SDK/build-tools/36.0.0}"
if [ ! -x "$BT/aapt2" ]; then
  BT="$(ls -d "$SDK"/build-tools/*/ 2>/dev/null | tail -1)"
  BT="${BT%/}"
fi
PLATFORM="$SDK/platforms/android-35/android.jar"
if [ ! -f "$PLATFORM" ]; then
  PLATFORM="$(ls -d "$SDK"/platforms/*/ 2>/dev/null | tail -1)"
  PLATFORM="${PLATFORM%/}/android.jar"
fi

MIN_SDK=24
TARGET_SDK="$(basename "$(dirname "$PLATFORM")" | sed 's/^android-//')"
PKG_DIR="com/azuma/agilexremote"
OUT="$HERE/agilex-remote.apk"
KS="${DEBUG_KEYSTORE:-$HOME/.android/debug.keystore}"
BUILD="$HERE/build"

for f in "$BT/aapt2" "$BT/d8" "$BT/zipalign" "$BT/apksigner"; do
  [ -x "$f" ] || { echo "FATAL: missing build-tool $f" >&2; exit 1; }
done
[ -f "$PLATFORM" ] || { echo "FATAL: missing android.jar at $PLATFORM" >&2; exit 1; }
[ -f "$KS" ] || { echo "FATAL: missing debug keystore $KS" >&2; exit 1; }

echo "=== toolchain ==="
echo "build-tools : $BT"
echo "android.jar : $PLATFORM"
echo "minSdk      : $MIN_SDK   targetSdk: $TARGET_SDK"
echo "keystore    : $KS"
echo "javac       : $(javac -version 2>&1)"

step() { echo; echo "### $*"; }

rm -rf "$BUILD"
mkdir -p "$BUILD/classes" "$BUILD/dex"

step "aapt2 compile --dir res -o build/res.zip"
"$BT/aapt2" compile --dir "$HERE/res" -o "$BUILD/res.zip"

step "aapt2 link -> build/base.apk"
"$BT/aapt2" link \
  -o "$BUILD/base.apk" \
  -I "$PLATFORM" \
  --manifest "$HERE/AndroidManifest.xml" \
  --min-sdk-version "$MIN_SDK" \
  --target-sdk-version "$TARGET_SDK" \
  --version-code 1 \
  --version-name 1.0 \
  --java "$BUILD/gen" \
  "$BUILD/res.zip"
# NOTE: compiled resources are positional `files...`; -R means *overlay* semantics.

step "javac --release 11 -> build/classes"
javac --release 11 -Xlint:-options \
  -cp "$PLATFORM" \
  -d "$BUILD/classes" \
  "$BUILD/gen/$PKG_DIR/R.java" \
  "$HERE/java/$PKG_DIR/"*.java

step "d8 -> build/dex/classes.dex"
"$BT/d8" --lib "$PLATFORM" --min-api "$MIN_SDK" \
  --output "$BUILD/dex" \
  "$BUILD/classes/$PKG_DIR/"*.class

step "zip classes.dex into build/unsigned.apk"
cp "$BUILD/base.apk" "$BUILD/unsigned.apk"
( cd "$BUILD/dex" && zip -q -X "$BUILD/unsigned.apk" classes.dex )

step "zipalign -f -p 4 -> build/aligned.apk"
"$BT/zipalign" -f -p 4 "$BUILD/unsigned.apk" "$BUILD/aligned.apk"

step "apksigner sign -> $OUT"
rm -f "$OUT"
"$BT/apksigner" sign \
  --ks "$KS" \
  --ks-pass pass:android \
  --key-pass pass:android \
  --ks-key-alias androiddebugkey \
  --out "$OUT" \
  "$BUILD/aligned.apk"

step "apksigner verify --print-certs"
"$BT/apksigner" verify --print-certs "$OUT"

step "aapt2 dump badging"
"$BT/aapt2" dump badging "$OUT" | head -20

step "done"
ls -l "$OUT"
echo
echo "APK: $OUT"
