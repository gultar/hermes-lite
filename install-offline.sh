#!/usr/bin/env bash
# Installs Hermes Lite from a bundle made by make-bundle.sh, without internet access:
# the bundled Python and the pinned wheels in vendor/, nothing from PyPI.
# The web tools still work normally whenever you are online.
#
#   ./install-offline.sh                     # Hermes Lite with the bundled Python
#   ./install-offline.sh --use-bundled-llama # also point server.binary at the bundled llama-server
#
# Undo: run ./install.sh to go back to a venv built from your system Python and PyPI.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
vendor="$here/vendor"
use_llama=0
[ "${1:-}" = "--use-bundled-llama" ] && use_llama=1
[ -x "$vendor/python/bin/python3" ] && [ -f "$vendor/requirements.lock" ] || {
  echo "No bundle here (vendor/python, vendor/requirements.lock). Build one with ./make-bundle.sh" >&2; exit 1; }

data="${XDG_DATA_HOME:-$HOME/.local/share}/hermes-lite"
pyver="$("$vendor/python/bin/python3" -c 'import platform; print(platform.python_version())')"
python="$data/python-$pyver"
venv="$data/venv"
mkdir -p "$data"

# the Python lives outside this folder, so moving or deleting the folder never breaks the venv
[ -x "$python/bin/python3" ] || cp -a "$vendor/python" "$python"
# a venv is tied to the Python that made it: rebuild it on the bundled one
if [ "$(readlink -f "$venv/bin/python3" 2>/dev/null)" != "$(readlink -f "$python/bin/python3")" ]; then
  rm -rf "$venv"
  "$python/bin/python3" -m venv "$venv"
fi
pip=(env PYTHONWARNINGS=ignore "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-index --find-links "$vendor/wheels")
"${pip[@]}" -r "$vendor/requirements.lock"
"${pip[@]}" --no-deps --no-build-isolation -e "$here"

if [ -x "$vendor/llama/llama-server" ]; then
  rm -rf "$data/llama"
  cp -a "$vendor/llama" "$data/llama"
fi

mkdir -p "$HOME/.local/bin"
ln -sf "$venv/bin/hermes-lite" "$HOME/.local/bin/hermes-lite"
"$venv/bin/hermes-lite" --print-server-cmd >/dev/null      # creates ~/.hermes-lite/config.yaml on first install
"$venv/bin/hermes-lite" --snapshot                          # `hermes-lite --rollback` can come back to this version

if [ "$use_llama" = 1 ]; then
  [ -x "$data/llama/llama-server" ] || { echo "This bundle has no llama-server (make-bundle.sh --llama ...)" >&2; exit 1; }
  "$venv/bin/hermes-lite" --set "server.binary=$data/llama/llama-server"
elif [ -x "$data/llama/llama-server" ]; then
  echo "A frozen llama-server is in $data/llama. To use it: hermes-lite --set server.binary=$data/llama/llama-server"
fi

case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Add ~/.local/bin to your PATH to run hermes-lite from anywhere.";; esac
echo "Installed offline: Python $pyver, $(grep -c . "$vendor/requirements.lock") pinned packages. Run: hermes-lite"
