#!/usr/bin/env bash
# t-chat1.3 one-click launcher (Linux / macOS)
# Python 3.6 - 3.13
set -e
cd "$(dirname "$0")"

echo "=========================================="
echo "  t-chat1.3 launcher"
echo "  Author: Tang at Shuai"
echo "  Bilibili: https://space.bilibili.com/3546392266017069"
echo "=========================================="

PY=python3
if ! command -v "$PY" >/dev/null 2>&1; then
  PY=python
fi
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[ERROR] python3/python not found. Install Python 3.6+ first."
  echo "  Debian/Ubuntu: sudo apt install -y python3 python3-pip python3-venv"
  echo "  RHEL/CentOS:   sudo yum install -y python3 python3-pip"
  exit 1
fi

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)'; then
  echo "[ERROR] Python 3.6+ required. Current: $("$PY" --version 2>&1)"
  exit 1
fi

PIPFLAGS=""
if [ -x ".venv/bin/python" ] && ".venv/bin/python" -c 'import sys' >/dev/null 2>&1; then
  . .venv/bin/activate
  PYRUN=python
else
  rm -rf .venv
  echo "[1/3] Creating virtual environment .venv ..."
  if "$PY" -m venv .venv 2>/dev/null && [ -x ".venv/bin/python" ]; then
    . .venv/bin/activate
    PYRUN=python
  else
    echo "[WARN] venv creation failed, falling back to system Python."
    PYRUN="$PY"
    PIPFLAGS="--user"
  fi
fi

REQ=requirements-py36.txt
if "$PYRUN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)'; then
  REQ=requirements-legacy.txt
fi
if "$PYRUN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  REQ=requirements.txt
fi

echo "[2/3] Checking dependencies ($REQ) ..."
if ! "$PYRUN" check_deps.py; then
  echo "      Installing dependencies ..."
  "$PYRUN" -m pip install --upgrade pip || true
  if ! "$PYRUN" -m pip install $PIPFLAGS -r "$REQ"; then
    echo "[WARN] Retrying with legacy requirements ..."
    if ! "$PYRUN" -m pip install $PIPFLAGS -r requirements-legacy.txt; then
      "$PYRUN" -m pip install $PIPFLAGS -r requirements-py36.txt
    fi
  fi
fi

if ! "$PYRUN" check_deps.py; then
  echo "[ERROR] Dependency installation failed."
  echo "  Try: $PYRUN -m pip install $PIPFLAGS -r $REQ"
  echo "  Mirror: ... -i https://pypi.tuna.tsinghua.edu.cn/simple"
  exit 1
fi

echo "[3/3] Starting server ..."
echo
echo "Local:  http://127.0.0.1:5000"
echo "Admin:  http://127.0.0.1:5000/admin"
echo "Press Ctrl+C to stop."
echo

export PYTHONIOENCODING=utf-8
exec "$PYRUN" server.py
