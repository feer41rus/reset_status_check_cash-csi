#!/usr/bin/env bash
# Сборка ResetStatusApp-x86_64.AppImage — запускается без установленного Python.
#
#   ./packaging/build_appimage.sh
#
# Требуется на машине сборки: python3 (>=3.8) с tkinter (apt install python3-tk python3-venv),
# интернет (pip + appimagetool) и libfuse2 для запуска appimagetool.
# Важно: AppImage работает на системах с glibc не старее, чем на машине сборки.
# Для широкой совместимости собирайте на самом старом поддерживаемом дистрибутиве.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$ROOT/packaging"
BUILD="$PKG/build"
DIST="$ROOT/dist"
APPDIR="$BUILD/AppDir"
VENV="$BUILD/venv"
PYTHON="${PYTHON:-python3}"
ARCH="$(uname -m)"
VERSION="$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$ROOT/main.py")"
APPIMAGETOOL="$BUILD/appimagetool-$ARCH.AppImage"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"

echo "==> Проверка tkinter"
"$PYTHON" -c "import tkinter; tkinter.Tcl()" \
  || { echo "Нет tkinter: sudo apt install python3-tk"; exit 1; }

echo "==> Виртуальное окружение для сборки"
mkdir -p "$BUILD" "$DIST"
[ -x "$VENV/bin/python" ] || "$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$ROOT/requirements.txt" "pyinstaller>=6.0"

echo "==> PyInstaller"
rm -rf "$BUILD/pyi" "$APPDIR"
"$VENV/bin/pyinstaller" --noconfirm --clean --onedir --windowed \
  --name ResetStatusApp \
  --distpath "$BUILD/pyi/dist" --workpath "$BUILD/pyi/work" --specpath "$BUILD/pyi" \
  --hidden-import babel.numbers \
  --collect-data tkcalendar \
  --collect-data babel \
  --add-data "$ROOT/assets:assets" \
  "$ROOT/main.py"

echo "==> AppDir"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp -a "$BUILD/pyi/dist/ResetStatusApp" "$APPDIR/usr/lib/"
install -m 755 "$PKG/AppRun" "$APPDIR/AppRun"
install -m 644 "$PKG/reset-status-app.desktop" "$APPDIR/reset-status-app.desktop"
install -m 644 "$PKG/reset-status-app.desktop" "$APPDIR/usr/share/applications/"
install -m 644 "$ROOT/assets/logo.svg" "$APPDIR/reset-status-app.svg"
install -m 644 "$ROOT/assets/logo.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/reset-status-app.svg"
ln -sf reset-status-app.svg "$APPDIR/.DirIcon"

echo "==> appimagetool"
if [ ! -x "$APPIMAGETOOL" ]; then
  curl -fL --retry 3 -o "$APPIMAGETOOL" "$APPIMAGETOOL_URL"
  chmod +x "$APPIMAGETOOL"
fi
OUT="$DIST/ResetStatusApp-$VERSION-$ARCH.AppImage"
ARCH="$ARCH" VERSION="$VERSION" "$APPIMAGETOOL" --no-appstream "$APPDIR" "$OUT"

echo
echo "Готово: $OUT"
echo "Запуск: chmod +x \"$OUT\" && \"$OUT\""
