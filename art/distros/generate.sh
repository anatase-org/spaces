#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
output_dir="${script_dir}/../../src/spaces/overlay"

if ! command -v magick >/dev/null 2>&1; then
    echo "error: ImageMagick's 'magick' command is required" >&2
    exit 1
fi

mkdir -p -- "${output_dir}"

shopt -s nullglob
images=("${script_dir}"/*.png)

if ((${#images[@]} == 0)); then
    echo "error: no PNG files found in ${script_dir}" >&2
    exit 1
fi

for image in "${images[@]}"; do
    output="${output_dir}/$(basename -- "${image}")"

    magick "${image}" \
        -resize 45x45 \
        -background none \
        -gravity southeast \
        -extent 128x128 \
        "${output}"

    echo "generated ${output}"
done
