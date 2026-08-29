#!/usr/bin/env bash
# Runs ON the rented box, once, before any data arrives. Piped in over ssh by grid.sh.
#
# TA-LIB IS THE ONLY AWKWARD DEPENDENCY and it is awkward in a specific way: the Python
# package is a thin wrapper over a C library that pip cannot install. Without the C library
# `pip install TA-Lib` fails at compile time with a missing header, which reads like a
# Python problem and is not one. Ubuntu ships no usable package, so it is built from source
# here -- about two minutes, and it must happen BEFORE the pip install.
#
# The repo comes from git rather than the upload, so this box gets exactly what master has.
# Only the bar cache and the reference tier are copied in afterwards, because git does not
# carry them (`/data/*` is gitignored except `data/reference/`).
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
echo "=== apt"
apt-get update -qq
apt-get install -y -qq build-essential wget git python3-venv python3-dev rsync >/dev/null

echo "=== TA-Lib C library"
cd /tmp
wget -q https://github.com/TA-Lib/ta-lib/releases/download/v0.6.4/ta-lib-0.6.4-src.tar.gz
tar -xzf ta-lib-0.6.4-src.tar.gz
cd ta-lib-0.6.4
./configure --prefix=/usr >/dev/null
make -s >/dev/null 2>&1
make install >/dev/null
ldconfig
echo "    installed: $(ls /usr/lib/libta* 2>/dev/null | head -1)"

echo "=== repo"
# Public remote, so no credentials travel to the box. The clone brings the code, the
# strategies and book_rules/; the bar cache and reference tier arrive separately by rsync
# because /data/* is gitignored.
REPO="${REPO:-https://github.com/TruongTrReal/stockhunt.git}"
if [ -d /opt/stockhunt/.git ]; then
  git -C /opt/stockhunt fetch -q origin master && git -C /opt/stockhunt reset -q --hard origin/master
else
  git clone -q --depth 1 "$REPO" /opt/stockhunt
fi
echo "    at $(git -C /opt/stockhunt rev-parse --short HEAD)"
mkdir -p /opt/stockhunt

echo "=== venv"
python3 -m venv /opt/stockhunt/.venv
/opt/stockhunt/.venv/bin/pip -q install --upgrade pip
/opt/stockhunt/.venv/bin/pip -q install numpy pandas pyarrow tqdm requests TA-Lib
echo "    python $(/opt/stockhunt/.venv/bin/python -V 2>&1)"
/opt/stockhunt/.venv/bin/python -c "import talib, pandas, numpy, pyarrow; print('    talib', talib.__version__, '| pandas', pandas.__version__)"

# The grid writes here and grid.sh fetches from here, so the path is fixed rather than
# discovered -- a results directory that moves is a fetch that silently returns nothing.
mkdir -p /opt/stockhunt/wfo-results
echo "=== setup done"
