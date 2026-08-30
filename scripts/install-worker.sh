#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

set -euo pipefail

RELEASE_TAG="__AIPG_WORKER_RELEASE_TAG__"
RELEASE_ROOT="https://github.com/AIPowerGrid/grid-text-worker/releases/download/${RELEASE_TAG}"
INSTALL_DIR="${HOME}/.local/bin"

usage() {
  cat <<'EOF'
Usage: install-worker.sh [--install-dir DIRECTORY]

Downloads the Linux worker from its fixed GitHub release, verifies it against
that release's SHA256SUMS, and installs it without running it.
EOF
}

while (($#)); do
  case "$1" in
    --install-dir)
      [[ $# -ge 2 && -n "$2" ]] || {
        echo "error: --install-dir requires a directory" >&2
        exit 2
      }
      INSTALL_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

for command in awk curl install mkdir mktemp mv rm sha256sum tr uname; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "error: required command not found: $command" >&2
    exit 1
  }
done

[[ "$(uname -s)" == "Linux" ]] || {
  echo "error: this installer supports Linux only" >&2
  exit 1
}

case "$(uname -m)" in
  x86_64|amd64)
    ASSET="grid-inference-worker-linux-x64"
    ;;
  aarch64|arm64)
    ASSET="grid-inference-worker-linux-arm64"
    ;;
  *)
    echo "error: unsupported Linux architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

umask 077
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/aipg-worker-install.XXXXXX")"
STAGED_PATH=""
cleanup() {
  [[ -z "$STAGED_PATH" ]] || rm -f -- "$STAGED_PATH"
  rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT INT TERM

download() {
  local name="$1"
  curl \
    --proto '=https' \
    --proto-redir '=https' \
    --tlsv1.2 \
    --fail \
    --show-error \
    --silent \
    --location \
    --output "$TMP_DIR/$name" \
    "$RELEASE_ROOT/$name"
}

checksum_for() {
  local name="$1"
  awk -v wanted="$name" '
    {
      digest = $1
      file = $2
      sub(/^\*/, "", file)
      if (file == wanted) {
        if (NF != 2) malformed = 1
        print digest
        matches += 1
      }
    }
    END { if (matches != 1 || malformed) exit 1 }
  ' "$TMP_DIR/SHA256SUMS"
}

verify_file() {
  local name="$1"
  local expected actual
  expected="$(checksum_for "$name")" || {
    echo "error: release checksum does not uniquely cover $name" >&2
    exit 1
  }
  [[ "$expected" =~ ^[0-9a-fA-F]{64}$ ]] || {
    echo "error: release checksum is malformed for $name" >&2
    exit 1
  }
  actual="$(sha256sum "$TMP_DIR/$name" | awk '{print $1}')"
  actual="$(printf '%s' "$actual" | tr '[:upper:]' '[:lower:]')"
  expected="$(printf '%s' "$expected" | tr '[:upper:]' '[:lower:]')"
  [[ "$actual" == "$expected" ]] || {
    echo "error: checksum mismatch for $name" >&2
    exit 1
  }
}

echo "Downloading AI Power Grid text worker ${RELEASE_TAG} for $(uname -m)..."
download SHA256SUMS
download worker-release.json
download "$ASSET"
verify_file worker-release.json
verify_file "$ASSET"

mkdir -p -- "$INSTALL_DIR"
DESTINATION="$INSTALL_DIR/grid-inference-worker"
STAGED_PATH="$INSTALL_DIR/.grid-inference-worker.$$.tmp"
install -m 0755 "$TMP_DIR/$ASSET" "$STAGED_PATH"
mv -f -- "$STAGED_PATH" "$DESTINATION"
STAGED_PATH=""

echo "Installed verified worker at $DESTINATION"
echo "Next: run '$DESTINATION --verify-runtime', then '$DESTINATION' to open setup."
echo "The installer did not start the worker and never requested a Grid key or wallet secret."
