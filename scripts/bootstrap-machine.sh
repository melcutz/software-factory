#!/usr/bin/env bash
set -euo pipefail

# Bootstrap a Linux/macOS/WSL2 machine for software-factory-agy (v0.2.0).
# Usage:
#   bash bootstrap-machine.sh [--bundle /path/to/factory-bootstrap-bundle.zip] [--work-dir ~/work] [--skip-herdr]

WORK_DIR="${WORK_DIR:-$HOME/work}"
BUNDLE_PATH=""
SKIP_HERDR=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle)
      BUNDLE_PATH="$2"
      shift 2
      ;;
    --work-dir)
      WORK_DIR="$2"
      shift 2
      ;;
    --skip-herdr)
      SKIP_HERDR=1
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--bundle PATH] [--work-dir DIR] [--skip-herdr]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$BUNDLE_PATH" && -f "$HOME/factory-bootstrap-bundle.zip" ]]; then
  BUNDLE_PATH="$HOME/factory-bootstrap-bundle.zip"
fi

echo "==> 1. Checking prerequisites (git, python3, gh, agy)"
for cmd in git python3 gh agy; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: Required command '$cmd' not found on PATH." >&2
    exit 1
  fi
done

if ! gh auth status >/dev/null 2>&1; then
  echo "ERROR: gh is not authenticated. Run 'gh auth login' first." >&2
  exit 1
fi

echo "==> 2. Extracting env bundle (if provided)"
if [[ -n "$BUNDLE_PATH" ]]; then
  if [[ ! -f "$BUNDLE_PATH" ]]; then
    echo "ERROR: Bundle not found: $BUNDLE_PATH" >&2
    exit 1
  fi
  python3 - "$BUNDLE_PATH" "$HOME" <<'PY'
import os, sys, zipfile

bundle_path, home_dir = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(bundle_path, "r") as zf:
    for info in zf.infolist():
        if info.is_dir():
            continue
        rel = info.filename.lstrip("/")
        if ".." in rel.split("/"):
            raise SystemExit(f"Refusing unsafe path in zip: {info.filename}")
        dest = os.path.join(home_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with zf.open(info) as src, open(dest, "wb") as dst:
            dst.write(src.read())
        os.chmod(dest, 0o600)
        print(f"    restored {dest} (0600)")
PY
else
  echo "    (no bundle passed; skipping env file restore)"
fi

echo "==> 3. Cloning / updating repositories in $WORK_DIR"
mkdir -p "$WORK_DIR"

clone_or_pull() {
  local name="$1"
  local url="$2"
  local target="$WORK_DIR/$name"
  if [[ -d "$target/.git" ]]; then
    echo "    updating $name"
    git -C "$target" pull --ff-only || echo "    warning: could not fast-forward $name"
  else
    echo "    cloning $name"
    git clone "$url" "$target"
  fi
}

clone_or_pull "software-factory-agy" "https://github.com/melcutz/software-factory-agy.git"
clone_or_pull "keepfor-cloud" "https://github.com/melcutz/keepfor-cloud.git"
clone_or_pull "keepfor.me" "https://github.com/melcutz/keepfor.me.git"

echo "==> 4. Installing software-factory-agy CLI"
if python3 -m pip install --user -e "$WORK_DIR/software-factory-agy" >/dev/null 2>&1; then
  true
else
  python3 -m pip install --user --break-system-packages -e "$WORK_DIR/software-factory-agy"
fi

export PATH="$HOME/.local/bin:$PATH"
if ! command -v factory >/dev/null 2>&1; then
  echo "WARNING: 'factory' not on PATH. Add 'export PATH=\"\$HOME/.local/bin:\$PATH\"' to your shell profile." >&2
fi

echo "==> 5. Writing ~/.config/factory/repos.json and host.json"
mkdir -p "$HOME/.config/factory"
cat > "$HOME/.config/factory/repos.json" <<EOF
[
  "$WORK_DIR/keepfor-cloud",
  "$WORK_DIR/keepfor.me",
  "$WORK_DIR/software-factory-agy"
]
EOF

if [[ ! -f "$HOME/.config/factory/host.json" ]]; then
  HOSTNAME_SHORT="$(hostname -s 2>/dev/null || hostname)"
  cat > "$HOME/.config/factory/host.json" <<EOF
{
  "machine_id": "$HOSTNAME_SHORT",
  "max_workers": 2,
  "min_free_ram_gb": 2.0,
  "max_cpu_load_pct": 85.0,
  "ramp_up_delay_sec": 20.0,
  "poll_interval_sec": 45.0
}
EOF
  echo "    created $HOME/.config/factory/host.json (machine_id=$HOSTNAME_SHORT)"
fi

if [[ "$SKIP_HERDR" -eq 0 ]] && ! command -v hc >/dev/null 2>&1; then
  echo "==> 6. Installing herdr-app / herdr-core (hc)"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL https://releases.herdr.dev/app/install.sh | sh || echo "    warning: herdr install failed; you can still run factory in a plain terminal"
  else
    echo "    curl not found; skipping herdr install"
  fi
else
  echo "==> 6. herdr-core (hc) already installed or skipped"
fi

echo "==> 7. Running factory doctor --all"
factory doctor --all

echo ""
echo "Bootstrap complete! Start the durable worker with:"
if command -v hc >/dev/null 2>&1; then
  echo "  hc run --bg --name factory-worker -- factory worker --all"
else
  echo "  factory worker --all"
fi
