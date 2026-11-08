#!/usr/bin/env bash
# Builds a self-contained Hermes Lite bundle that installs with no internet access:
#   vendor/python/   a standalone CPython (python-build-standalone, via uv)
#   vendor/wheels/   every dependency as a wheel, pinned in vendor/requirements.lock
#   vendor/llama/    optional: your llama-server and the libraries it was built against
# Result: dist/hermes-lite-bundle-<build>-linux-x86_64.tar.gz (extract anywhere, run ./install-offline.sh).
#
#   ./make-bundle.sh                                  # Python + dependencies
#   ./make-bundle.sh --llama ~/llama.cpp/build/bin/llama-server   # also freeze llama-server
#
# Needs uv and internet access once, while building. Linux x86_64 only.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
pyver="3.12"
llama=""
while [ $# -gt 0 ]; do
  case "$1" in
    --llama) llama="$(readlink -f "$2")"; shift 2 ;;
    --python) pyver="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
command -v uv >/dev/null || { echo "make-bundle.sh needs uv (sudo pacman -S uv)" >&2; exit 1; }
[ "$(uname -m)" = x86_64 ] || { echo "Linux x86_64 only" >&2; exit 1; }

vendor="$here/vendor"
rm -rf "$vendor"
mkdir -p "$vendor/wheels" "$here/dist"

# 1. standalone Python, copied whole (it is relocatable)
uv python install --quiet "$pyver"
pyhome="$(dirname "$(dirname "$(readlink -f "$(uv python find "$pyver")")")")"
cp -a "$pyhome" "$vendor/python"
rm -f "$vendor"/python/lib/python3*/EXTERNALLY-MANAGED
py="$vendor/python/bin/python3"

# 2. resolve the dependencies once, pin them, download them as wheels
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
"$py" -m venv "$tmp/venv"
"$tmp/venv/bin/python" -m pip install --quiet --upgrade pip
"$tmp/venv/bin/python" -m pip install --quiet "$here" lightpanda playwright setuptools wheel
rm -rf "$here/build" "$here"/*.egg-info          # setuptools leftovers from that install
"$tmp/venv/bin/python" -m pip freeze --all --exclude hermes-lite > "$vendor/requirements.lock"
"$tmp/venv/bin/python" -m pip download --quiet --only-binary=:all: --no-deps \
  -r "$vendor/requirements.lock" -d "$vendor/wheels"

# 3. optional: llama-server and its libraries (not the NVIDIA driver, not glibc)
if [ -n "$llama" ]; then
  [ -x "$llama" ] || { echo "not an executable: $llama" >&2; exit 1; }
  mkdir -p "$vendor/llama/bin"
  cp -a "$llama" "$vendor/llama/bin/llama-server"
  # ggml loads its backends (CUDA, CPU variants) from the binary's folder: take every library there
  find "$(dirname "$llama")" -maxdepth 1 -name 'lib*.so*' -exec cp -a {} "$vendor/llama/bin/" \;
  ldd "$llama" | awk '/=> \//{print $3}' | while read -r lib; do
    case "$(basename "$lib")" in
      libc.so*|libm.so*|libdl.so*|libpthread.so*|librt.so*|ld-linux*|libgcc_s.so*|libstdc++.so*) ;;
      libcuda.so*|libnvidia*) ;;          # the driver must match the kernel module: never bundled
      *) cp -L "$lib" "$vendor/llama/bin/$(basename "$lib")" ;;
    esac
  done
  cat > "$vendor/llama/llama-server" <<'EOF'
#!/bin/sh
# Runs the bundled llama-server with the libraries it was built against.
d="$(dirname "$(readlink -f "$0")")/bin"
LD_LIBRARY_PATH="$d${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" exec "$d/llama-server" "$@"
EOF
  chmod +x "$vendor/llama/llama-server"
  "$vendor/llama/llama-server" --version >/dev/null 2>&1 || echo "warning: the bundled llama-server did not start (--version failed)" >&2
  echo "llama-server bundled: $(du -sh "$vendor/llama" | cut -f1)"
fi

build="$("$py" -c "import re,sys; print(re.search(r'BUILD = \"([^\"]+)\"', open(sys.argv[1]).read()).group(1).replace(' ', '-'))" "$here/hermes_lite/__init__.py")"
out="$here/dist/hermes-lite-bundle-$build-linux-x86_64.tar.gz"
tar -czf "$out" --exclude=.git --exclude=dist --exclude=__pycache__ --exclude=.pytest_cache --exclude=build --exclude='*.egg-info' \
  -C "$(dirname "$here")" --transform "s|^$(basename "$here")|hermes-lite|" "$(basename "$here")"
echo "Bundle: $out ($(du -h "$out" | cut -f1)), Python $("$py" -c 'import platform; print(platform.python_version())'), $(wc -l < "$vendor/requirements.lock") pinned packages"
