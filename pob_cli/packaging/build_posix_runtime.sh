#!/usr/bin/env bash
# Build minimal native POSIX headless runtime for pob-agent-tools:
#   - LuaJIT 2.1 (exact commit used by PathOfBuilding-SimpleGraphic, supporting += / ..=)
#   - lua-utf8.so (starwing/luautf8 single-file C module)
#
# Output:
#   <OUT_DIR>/bin/luajit
#   <OUT_DIR>/bin/lua-utf8.so
#
# Usage:
#   ./build_posix_runtime.sh <OUT_DIR>

set -euo pipefail

OUT_DIR="${1:-./posix-runtime}"
mkdir -p "$OUT_DIR/bin"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

LUAJIT_COMMIT="2460b3ff93a1c955de3d62cfc825de7d68dc272e"
LUAUTF8_TAG="0.1.6"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/pob-posix-rt.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

OS_NAME="$(uname -s)"
ARCH_NAME="$(uname -m)"
echo "[pob-runtime] Building for ${OS_NAME} (${ARCH_NAME}) into ${OUT_DIR}/bin"

# 1. Fetch & build LuaJIT 2.1 (matching PoB 2.68+ SimpleGraphic commit)
LUAJIT_TAR="$WORKDIR/luajit.tar.gz"
LUAJIT_SRC="$WORKDIR/luajit-src"
mkdir -p "$LUAJIT_SRC"
curl -fsSL "https://github.com/LuaJIT/LuaJIT/archive/${LUAJIT_COMMIT}.tar.gz" -o "$LUAJIT_TAR"
tar -xzf "$LUAJIT_TAR" --strip-components=1 -C "$LUAJIT_SRC"

MAKE_ARGS=(
  BUILDMODE=static
)

if [ "$OS_NAME" = "Darwin" ]; then
  export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"
  JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
  # Export symbols so dynamically loaded C modules (lua-utf8.so) resolve lua_* symbols from luajit
  make -C "$LUAJIT_SRC/src" -j"$JOBS" "${MAKE_ARGS[@]}" amalg
else
  JOBS="$(nproc 2>/dev/null || echo 4)"
  make -C "$LUAJIT_SRC/src" -j"$JOBS" "${MAKE_ARGS[@]}" amalg
fi

cp "$LUAJIT_SRC/src/luajit" "$OUT_DIR/bin/luajit"
chmod +x "$OUT_DIR/bin/luajit"

# 2. Fetch & build starwing/luautf8 (single-file lutf8lib.c -> lua-utf8.so)
LUAUTF8_TAR="$WORKDIR/luautf8.tar.gz"
LUAUTF8_SRC="$WORKDIR/luautf8-src"
mkdir -p "$LUAUTF8_SRC"
curl -fsSL "https://github.com/starwing/luautf8/archive/refs/tags/${LUAUTF8_TAG}.tar.gz" -o "$LUAUTF8_TAR"
tar -xzf "$LUAUTF8_TAR" --strip-components=1 -C "$LUAUTF8_SRC"

CC_BIN="${CC:-cc}"
if [ "$OS_NAME" = "Darwin" ]; then
  "$CC_BIN" -O2 -fPIC -bundle -undefined dynamic_lookup \
    -I"$LUAJIT_SRC/src" \
    "$LUAUTF8_SRC/lutf8lib.c" \
    -o "$OUT_DIR/bin/lua-utf8.so"
else
  "$CC_BIN" -O2 -fPIC -shared \
    -I"$LUAJIT_SRC/src" \
    "$LUAUTF8_SRC/lutf8lib.c" \
    -o "$OUT_DIR/bin/lua-utf8.so"
fi

# 3. Smoke-test compiled runtime: compound assignment (+= / ..=), lua-utf8, and FFI libz
echo "[pob-runtime] Running runtime self-test..."
LUA_CPATH="$OUT_DIR/bin/?.so;;" "$OUT_DIR/bin/luajit" -e '
  local count = 1
  count += 41
  assert(count == 42, "compound += failed")
  local s = "po"
  s ..= "b"
  assert(s == "pob", "compound ..= failed")
  local utf8 = require("lua-utf8")
  assert(utf8.len("流放之路") == 4, "lua-utf8 failed")
  local ffi = require("ffi")
  local z = ffi.load("z")
  ffi.cdef("const char *zlibVersion(void);")
  print("OK: LuaJIT=" .. jit.version .. " utf8=ok zlib=" .. ffi.string(z.zlibVersion()))
'

ls -lh "$OUT_DIR/bin/luajit" "$OUT_DIR/bin/lua-utf8.so"
