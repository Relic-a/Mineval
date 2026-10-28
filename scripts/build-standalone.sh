#!/bin/sh
set -eu
# Run in a pinned build environment. No publishing occurs here.
export PYTHONHASHSEED=0
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}"
python -m PyInstaller --clean --noconfirm --onefile --name mineval \
  --collect-all mineval packaging/entrypoint.py
./dist/mineval --help
