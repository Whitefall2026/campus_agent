#!/bin/bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This build requires a native macOS host." >&2
  exit 1
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$project_root"
python_bin="${PYTHON:-python3}"
version="$("$python_bin" -B -c 'from app.version import __version__; print(__version__)')"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
  echo "Invalid application version: $version" >&2
  exit 1
fi
arch="$(uname -m)"
python_arch="$("$python_bin" -B -c 'import platform; print(platform.machine())')"
case "$arch" in
  arm64|x86_64) ;;
  *) echo "Unsupported macOS architecture: $arch" >&2; exit 1 ;;
esac
if [[ "$python_arch" != "$arch" || "${RUC_AGENT_BUILD_ARCH:-$arch}" != "$arch" ]]; then
  echo "Runner, Python and requested build architectures must match." >&2
  exit 1
fi

"$python_bin" -B -c 'import PyInstaller, PIL, AppKit, Foundation, objc'
node --check static/app.js

# Override even an inherited data setting before importing any application
# modules. TemporaryDirectory cleans only the directories it creates.
"$python_bin" -B - <<'PY'
import os
import tempfile
import unittest

with tempfile.TemporaryDirectory(prefix="ruc-agent-macos-tests-") as data_dir:
    os.environ["RUC_AGENT_DATA_DIR"] = data_dir
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
PY

mkdir -p "$project_root/build" "$project_root/dist/macos"
# Unique build/staging directory: reruns never delete another build's files.
work_dir="$(mktemp -d "$project_root/build/macos-${arch}-XXXXXX")"
export RUC_AGENT_BUILD_DIR="$work_dir"
"$python_bin" -m PyInstaller --noconfirm --clean \
  --distpath "$work_dir/dist" --workpath "$work_dir/pyinstaller" \
  packaging/ruc_agent_macos.spec

app_path="$work_dir/dist/RUC Agent.app"
executable="$app_path/Contents/MacOS/RUCAgent"
[[ -x "$executable" ]]
[[ "$(lipo -archs "$executable")" == "$arch" ]]
"$python_bin" -B - "$executable" <<'PY'
import os
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory(prefix="ruc-agent-macos-smoke-") as data_dir:
    env = {**os.environ, "RUC_AGENT_DATA_DIR": data_dir}
    subprocess.run([sys.argv[1], "--smoke-test"], env=env, check=True, timeout=90)
PY

stage="$work_dir/dmg"
mkdir "$stage"
ditto "$app_path" "$stage/RUC Agent.app"
ln -s /Applications "$stage/Applications"
dmg_name="RUC-Agent-${version}-macos-${arch}.dmg"
hdiutil create -volname "RUC Agent $version" -srcfolder "$stage" \
  -format UDZO -ov "$project_root/dist/macos/$dmg_name"
(
  cd "$project_root/dist/macos"
  shasum -a 256 "$dmg_name" > "SHA256SUMS-${version}-macos-${arch}.txt"
)
echo "Built dist/macos/$dmg_name"
echo "App bundle: $app_path"
