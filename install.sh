#!/usr/bin/env bash
# Installs Hermes Lite into its own virtualenv and links `hermes-lite` into ~/.local/bin.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
venv="${XDG_DATA_HOME:-$HOME/.local/share}/hermes-lite/venv"

if command -v uv >/dev/null 2>&1; then
  [ -x "$venv/bin/python" ] || uv venv --quiet "$venv"
  VIRTUAL_ENV="$venv" uv pip install --quiet -e "$here"
else
  [ -x "$venv/bin/python" ] || python3 -m venv "$venv"
  "$venv/bin/pip" install --quiet --upgrade pip
  "$venv/bin/pip" install --quiet -e "$here"
fi

# Lightpanda: a light headless browser for pages built by JavaScript (optional; Chromium is the fallback).
# Playwright: drives Lightpanda or Chromium for the browser tool (clicking, typing); it uses the
# browsers already installed, it downloads none.
if command -v uv >/dev/null 2>&1; then
  VIRTUAL_ENV="$venv" uv pip install --quiet lightpanda 2>/dev/null || echo "Lightpanda not installed (optional): JavaScript pages will use Chromium."
  VIRTUAL_ENV="$venv" uv pip install --quiet playwright 2>/dev/null || echo "Playwright not installed (optional): no browser tool (web_extract still reads pages)."
else
  "$venv/bin/pip" install --quiet lightpanda 2>/dev/null || echo "Lightpanda not installed (optional): JavaScript pages will use Chromium."
  "$venv/bin/pip" install --quiet playwright 2>/dev/null || echo "Playwright not installed (optional): no browser tool (web_extract still reads pages)."
fi

mkdir -p "$HOME/.local/bin"
ln -sf "$venv/bin/hermes-lite" "$HOME/.local/bin/hermes-lite"

# create ~/.hermes-lite/config.yaml on first install
"$venv/bin/hermes-lite" --print-server-cmd >/dev/null
# keep a copy of this version: `hermes-lite --rollback` goes back to the previous one
"$venv/bin/hermes-lite" --snapshot

case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Add ~/.local/bin to your PATH to run hermes-lite from anywhere.";; esac
echo "Installed. Edit ~/.hermes-lite/config.yaml (server.model_path, server.binary), then run: hermes-lite"
command -v rg >/dev/null || echo "Tip: install ripgrep for faster search_files (sudo pacman -S ripgrep)."
command -v pdftotext >/dev/null || echo "Tip: install poppler for PDF reading (sudo pacman -S poppler)."
