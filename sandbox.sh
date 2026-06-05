#!/usr/bin/env bash
# sandbox.sh — build and run recall in an isolated Docker container
# Usage:
#   ./sandbox.sh          → interactive shell
#   ./sandbox.sh test     → run pytest suite
#   ./sandbox.sh init     → run `recall init` and observe output
#   ./sandbox.sh <cmd>    → run any recall command, e.g. ./sandbox.sh "recall today"

set -euo pipefail

IMAGE="dev-recall-sandbox:local"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Building sandbox image ==="
docker build -f "$SCRIPT_DIR/Dockerfile.sandbox" -t "$IMAGE" "$SCRIPT_DIR"

case "${1:-shell}" in
  shell)
    echo "=== Dropping into sandbox shell (type 'exit' to leave) ==="
    docker run --rm -it \
      --network none \
      --cap-drop ALL \
      --security-opt no-new-privileges \
      "$IMAGE"
    ;;
  test)
    echo "=== Running pytest inside sandbox ==="
    docker run --rm \
      --network none \
      --cap-drop ALL \
      --security-opt no-new-privileges \
      "$IMAGE" \
      bash -c "cd /home/devuser/dev-recall && python -m pytest -v"
    ;;
  init)
    echo "=== Running 'recall init' inside sandbox ==="
    # systemd is not available in Docker, so the daemon will fall back to subprocess mode
    docker run --rm -it \
      --network none \
      --cap-drop ALL \
      --security-opt no-new-privileges \
      "$IMAGE" \
      bash -c "recall init; echo '--- Files written ---'; find ~/.local ~/.config -type f 2>/dev/null | sort"
    ;;
  *)
    # Pass arbitrary command
    echo "=== Running: $* ==="
    docker run --rm -it \
      --network none \
      --cap-drop ALL \
      --security-opt no-new-privileges \
      "$IMAGE" \
      bash -c "$*"
    ;;
esac
