#!/usr/bin/env sh
set -eu

usage() {
  echo "Usage: $0 <plugin_dir> [output_zip]" >&2
  echo "Example: $0 tri_guess" >&2
  echo "Example: $0 tri_guess dist/tri_guess.zip" >&2
}

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  usage
  exit 2
fi

plugin_dir=${1%/}

case "$plugin_dir" in
  ""|.*|/*|*..*|*/*)
    echo "Error: plugin_dir must be a direct child directory name, for example: tri_guess" >&2
    exit 2
    ;;
esac

if [ ! -d "$plugin_dir" ]; then
  echo "Error: plugin directory not found: $plugin_dir" >&2
  exit 1
fi

if [ ! -f "$plugin_dir/metadata.yaml" ] && [ ! -f "$plugin_dir/metadata.yml" ]; then
  echo "Error: $plugin_dir does not contain metadata.yaml or metadata.yml" >&2
  exit 1
fi

output_zip=${2:-dist/${plugin_dir}.zip}
output_dir=$(dirname "$output_zip")

if [ "$output_dir" != "." ]; then
  mkdir -p "$output_dir"
fi

rm -f "$output_zip"

case "$output_zip" in
  /*)
    zip_target=$output_zip
    ;;
  *)
    zip_target=../$output_zip
    ;;
esac

(
  cd "$plugin_dir"
  zip -r "$zip_target" . \
    -x '*.DS_Store' \
    -x '__MACOSX/*' \
    -x '*/__MACOSX/*' \
    -x '__pycache__/*' \
    -x '*/__pycache__/*' \
    -x '.pytest_cache/*' \
    -x '*/.pytest_cache/*' \
    -x '*.pyc' \
    -x 'data/*' \
    -x '*/data/*'
)

echo "Created: $output_zip"
