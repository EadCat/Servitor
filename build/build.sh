#!/usr/bin/env bash
# Build a self-contained Servitor bundle into dist/, then wrap it in an
# AppImage.
#
#     build/build.sh                  build, creating .venv if it is missing
#     build/build.sh --venv           recreate .venv and reinstall into it
#     build/build.sh --conda servitor build with that conda environment instead
#
# By default everything runs out of .venv in the repository, made with the
# standard venv module. conda is not used: Anaconda and Miniconda carry terms
# that make them a paid product for many organisations, and a conda
# interpreter's libpython would be copied into the bundle. A venv cannot
# insulate the build from that, since it shares the base interpreter's standard
# library rather than shipping its own.
#
# --conda takes an environment name or path and builds with its interpreter as
# it is. Whatever that environment holds is what ships, so it is for one built
# from conda-forge alone (Miniforge), never from the defaults channel. Nothing
# is installed into it; what is missing is named instead. Its Tk must be the
# xft build, or the bundled interface font cannot be drawn.
#
# You do not need to deactivate a conda environment first. Interpreters are
# looked up by absolute path in /usr/bin and /usr/local/bin, so whatever is on
# PATH is ignored. PYTHON=/path/to/python names one explicitly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RECREATE=no
CONDA_ENV=
while [ $# -gt 0 ]; do
    case "$1" in
        --venv) RECREATE=yes; shift ;;
        --conda) [ $# -ge 2 ] || { echo "--conda needs an environment name or path" >&2; exit 1; }
                 CONDA_ENV="$2"; shift 2 ;;
        --conda=*) CONDA_ENV="${1#*=}"; shift ;;
        -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done
if [ -n "$CONDA_ENV" ] && [ "$RECREATE" = yes ]; then
    echo "--venv and --conda do not go together: --conda does not use .venv" >&2
    exit 1
fi

# An interpreter qualifies only if it is not from conda and can import tkinter.
# tkinter is a compiled module belonging to the interpreter, so pip cannot add
# it later; checking now avoids producing a bundle that will not open.
usable_python() {
    [ -x "$1" ] || return 1
    "$1" - <<'PYEOF' 2>/dev/null
import os, sys, sysconfig
base = sysconfig.get_config_var("prefix") or sys.base_prefix
if os.path.isdir(os.path.join(base, "conda-meta")) or "conda" in sys.version.lower():
    sys.exit(1)
import tkinter
PYEOF
}

# Newest first, so a machine with several pythons builds against the best one.
choose_python() {
    if [ -n "${PYTHON:-}" ]; then
        usable_python "$PYTHON" && echo "$PYTHON"
        return
    fi
    for dir in /usr/bin /usr/local/bin; do
        for candidate in "$dir"/python3.1[5-9] "$dir"/python3.1[0-4] \
                         "$dir"/python3.9 "$dir"/python3; do
            usable_python "$candidate" && { echo "$candidate"; return; }
        done
    done
}

no_python() {
    cat >&2 <<'EOF'

No usable interpreter found for building.

It has to be a distribution python that can import tkinter:
    sudo apt install python3-venv python3-tk

conda interpreters are skipped deliberately, because their libpython would be
copied into the bundle. There is no need to deactivate anything: the search
looks in /usr/bin and /usr/local/bin directly and ignores PATH.

To name one yourself:
    PYTHON=/usr/bin/python3.12 build/build.sh --venv
EOF
    exit 1
}

if [ -n "$CONDA_ENV" ]; then
    case "$CONDA_ENV" in
        */*) PY_BIN="$CONDA_ENV/bin/python" ;;
        *) PY_BIN="$("${CONDA_EXE:-conda}" run -n "$CONDA_ENV" python -c \
                   'import sys; print(sys.executable)' 2>/dev/null)" || PY_BIN= ;;
    esac
    [ -x "${PY_BIN:-}" ] || { echo "no conda environment found at: $CONDA_ENV" >&2; exit 1; }
    CONDA_LIB="$(dirname "$(dirname "$PY_BIN")")/lib"
    "$PY_BIN" -c "import tkinter" 2>/dev/null || {
        echo "$PY_BIN cannot import tkinter; install tk into the environment" >&2; exit 1; }
    # Checked through the extension module, which is what links Tk in.
    TK_MODULE=$("$PY_BIN" -c "import _tkinter; print(_tkinter.__file__)")
    ldd "$TK_MODULE" | grep -q libXft || {
        echo "The Tk in $CONDA_ENV is not the xft build, so the interface font cannot" >&2
        echo "be drawn. Switch it with:" >&2
        echo "    mamba install -n $CONDA_ENV -c conda-forge \"tk=*=xft*\"" >&2
        exit 1
    }
    "$PY_BIN" -c "import PyInstaller, customtkinter, paramiko, pystray, PIL" 2>/dev/null || {
        echo "Missing build requirements in $CONDA_ENV. Install them with:" >&2
        echo "    mamba install -n $CONDA_ENV -c conda-forge pyinstaller paramiko pystray pillow" >&2
        echo "    $PY_BIN -m pip install customtkinter    # not on conda-forge" >&2
        exit 1
    }
fi

# Check before installing anything. A .venv built on the wrong interpreter used
# to be discovered only after pip had downloaded everything into it.
if [ -z "$CONDA_ENV" ] && [ "$RECREATE" = no ] && [ -e .venv ] \
        && ! usable_python .venv/bin/python; then
    echo "The existing .venv cannot be used for building; recreating it." >&2
    RECREATE=yes
fi

[ -z "$CONDA_ENV" ] && [ "$RECREATE" = yes ] && rm -rf .venv
if [ -z "$CONDA_ENV" ] && [ ! -x .venv/bin/python ]; then
    BASE_PYTHON=$(choose_python) || true
    [ -n "${BASE_PYTHON:-}" ] || no_python
    echo "Creating .venv from $BASE_PYTHON..."
    "$BASE_PYTHON" -m venv .venv || {
        echo "Could not create .venv with $BASE_PYTHON -m venv." >&2
        echo "On Debian/Ubuntu: sudo apt install python3-venv" >&2
        exit 1
    }
    RECREATE=yes
fi
[ -n "$CONDA_ENV" ] || PY_BIN=".venv/bin/python"

if [ -z "$CONDA_ENV" ] && [ "$RECREATE" = yes ]; then
    echo "Installing the requirements into .venv..."
    "$PY_BIN" -m pip install --upgrade pip >/dev/null
    "$PY_BIN" -m pip install -r requirements.txt pyinstaller
fi

# Same check the spec makes, but reported before any work is done.
"$PY_BIN" -c "import PyInstaller, customtkinter, paramiko, pystray, PIL" 2>/dev/null || {
    echo "Missing build requirements in .venv. Recreate it with:" >&2
    echo "    $0 --venv" >&2
    exit 1
}

# paramiko and cryptography want 3.9 or newer. pip falls back to older releases
# on 3.8 without comment, so say which case this is.
"$PY_BIN" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" || {
    echo "Note: $("$PY_BIN" -V) predates the 3.9 that paramiko and cryptography" >&2
    echo "ask for, so pip pinned older releases of them. The build still works." >&2
    echo "A newer interpreter avoids it:  PYTHON=/usr/bin/python3.12 $0 --venv" >&2
}

# The icons in assets/ are committed and packaged as they are.
(
    # conda's extensions link the environment's own OpenSSL and libsodium, which
    # PyInstaller would otherwise resolve to the system's older copies; the
    # cryptography it then ships fails to load. Only this step sees the path.
    if [ -n "$CONDA_ENV" ]; then
        export LD_LIBRARY_PATH="$CONDA_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
    "$PY_BIN" -m PyInstaller --noconfirm --clean build/Servitor.spec \
        --workpath .pyinstaller --distpath dist
)

[ "$(uname -s)" = "Linux" ] || exit 0

APPDIR=".pyinstaller/Servitor.AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp -a dist/Servitor/. "$APPDIR/usr/bin/"
# appimagetool looks for metadata named after the desktop file, while
# appstreamcli wants it named after the component id, so both use the id.
# The Icon= value inside stays "servitor", so the icon file keeps that name.
APP_ID="io.github.eadcat.Servitor"
# One place holds the version; the copies that ship carry it, the sources do not.
VERSION=$(tr -d '[:space:]' < VERSION)
[ -n "$VERSION" ] || { echo "VERSION at the repository root is empty" >&2; exit 1; }
# AppStream 1.0 rejects a release without a date. The day VERSION last changed
# is the day this version was cut, so a rebuild does not move it.
RELEASED=$(git log -1 --format=%cs -- VERSION 2>/dev/null || true)
[ -n "$RELEASED" ] || RELEASED=$(date +%F)
stamp_desktop() { sed "/^Terminal=/i X-AppImage-Version=$VERSION" build/servitor.desktop > "$1"; }
stamp_desktop "$APPDIR/$APP_ID.desktop"
cp assets/icon.png "$APPDIR/servitor.png"
# Keep the notices where a recipient can find them without unpacking.
cp LICENSE THIRD-PARTY-NOTICES.md "$APPDIR/"
# appimagetool asks for AppStream metadata; without it every build warns. The
# metadata names a desktop file, and appstreamcli looks for that in the usual
# share/applications place rather than at the AppDir root, so it goes in both.
mkdir -p "$APPDIR/usr/share/metainfo" "$APPDIR/usr/share/applications"
sed -e "s/@VERSION@/$VERSION/" -e "s/@DATE@/$RELEASED/" build/servitor.appdata.xml \
    > "$APPDIR/usr/share/metainfo/$APP_ID.appdata.xml"
stamp_desktop "$APPDIR/usr/share/applications/$APP_ID.desktop"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/Servitor" "$@"
EOF
chmod +x "$APPDIR/AppRun"

TOOL="${APPIMAGETOOL:-$(command -v appimagetool || true)}"
if [ -z "$TOOL" ]; then
    TOOL=".pyinstaller/appimagetool"
    if [ ! -x "$TOOL" ]; then
        curl -sSL -o "$TOOL" \
            https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
        chmod +x "$TOOL"
    fi
fi

# Clear older builds out of the way first, so dist/ holds only this version's
# AppImage: a leftover 1.1.0 sorts before 1.2.0 for anything taking the first.
rm -f dist/Servitor-*.AppImage
OUT="dist/Servitor-$VERSION-x86_64.AppImage"
ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUT"
echo "built $OUT"
