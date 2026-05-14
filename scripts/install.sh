#!/usr/bin/env bash
set -euo pipefail

TARGET_DIR="${TARGET_DIR:-/opt/vps-work}"

# Supports two modes:
# 1) REPO_URL=https://github.com/user/repo.git BUNDLE_PATH=vps_worker_bundle ./install.sh
# 2) ARCHIVE_URL=https://.../bundle.tar.gz ./install.sh

if [ -n "${ARCHIVE_URL:-}" ]; then
  tmpd="$(mktemp -d)"
  curl -fsSL "$ARCHIVE_URL" -o "$tmpd/bundle.tgz"
  mkdir -p "$TARGET_DIR"
  tar -xzf "$tmpd/bundle.tgz" -C "$TARGET_DIR" --strip-components=1
elif [ -n "${REPO_URL:-}" ]; then
  BUNDLE_PATH="${BUNDLE_PATH:-vps_worker_bundle}"
  tmpd="$(mktemp -d)"
  git clone --depth 1 "$REPO_URL" "$tmpd/repo"
  mkdir -p "$TARGET_DIR"
  rsync -a "$tmpd/repo/$BUNDLE_PATH/" "$TARGET_DIR/"
else
  # local mode: run from inside bundle directory
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
  mkdir -p "$TARGET_DIR"
  rsync -a "$ROOT_DIR/" "$TARGET_DIR/"
fi

bash "$TARGET_DIR/scripts/setup_vps.sh" "$TARGET_DIR"
