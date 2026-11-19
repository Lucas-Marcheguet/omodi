#!/usr/bin/env bash
# Install omodi on Linux.
#
# Creates a virtualenv, installs omodi in it and symlinks the `omodi`
# executable into a directory of your PATH (default: ~/.local/bin).
#
# Works from a local checkout (./install.sh) or straight from GitHub:
#   curl -fsSL https://raw.githubusercontent.com/Lucas-Marcheguet/omodi/main/install.sh | bash
#   wget -qO- https://raw.githubusercontent.com/Lucas-Marcheguet/omodi/main/install.sh | bash
# Pass options with `| bash -s -- --js`.
#
# Usage: ./install.sh [--js] [--ref REF] [--prefix DIR] [--bin-dir DIR] [--uninstall]
#   --js           also install the optional JS parsing deps (tree-sitter)
#   --ref REF      branch, tag or commit to install when not run from a checkout
#                  (default: main)
#   --prefix DIR   virtualenv location (default: ~/.local/share/omodi)
#   --bin-dir DIR  where the symlink is created (default: ~/.local/bin)
#   --uninstall    remove the symlink and the virtualenv
set -euo pipefail

REPO="Lucas-Marcheguet/omodi"
REF="main"
SRC_DIR=""
# Local checkout? (not the case when piped from curl/wget)
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    _dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -f "${_dir}/pyproject.toml" ] && SRC_DIR="$_dir"
fi
PREFIX="${HOME}/.local/share/omodi"
BIN_DIR="${HOME}/.local/bin"
WITH_JS=0
UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --js) WITH_JS=1 ;;
        --ref) REF="${2:?--ref needs a value}"; shift ;;
        --prefix) PREFIX="${2:?--prefix needs a value}"; shift ;;
        --bin-dir) BIN_DIR="${2:?--bin-dir needs a value}"; shift ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

LINK="${BIN_DIR}/omodi"

if [ "$UNINSTALL" -eq 1 ]; then
    [ -L "$LINK" ] && rm -f "$LINK" && echo "removed $LINK"
    [ -d "$PREFIX" ] && rm -rf "$PREFIX" && echo "removed $PREFIX"
    exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found (>= 3.9 required)" >&2
    exit 1
fi
python3 - <<'EOF' || { echo "error: Python >= 3.9 required" >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
EOF

echo "Creating virtualenv in ${PREFIX}"
python3 -m venv "$PREFIX"

EXTRA=""
[ "$WITH_JS" -eq 1 ] && EXTRA="[js]"
if [ -n "$SRC_DIR" ]; then
    TARGET="${SRC_DIR}${EXTRA}"
    echo "Installing omodi from ${SRC_DIR}"
else
    # No git needed: pip installs straight from the GitHub tarball.
    TARGET="omodi${EXTRA} @ https://github.com/${REPO}/archive/${REF}.tar.gz"
    echo "Installing omodi from https://github.com/${REPO} (${REF})"
fi
"${PREFIX}/bin/python" -m pip install --quiet --upgrade pip
"${PREFIX}/bin/python" -m pip install --quiet "$TARGET"

mkdir -p "$BIN_DIR"
ln -sf "${PREFIX}/bin/omodi" "$LINK"
echo "Linked ${LINK} -> ${PREFIX}/bin/omodi"

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) echo "warning: ${BIN_DIR} is not in your PATH; add it to your shell profile" ;;
esac

echo "Done. Try: omodi --version"
