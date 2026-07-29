#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_root="${script_dir}/../.."
output_dir="${repository_root}/src/spaces/overlay"
launcher_icon_dir="${repository_root}/data/icons/hicolor/256x256/apps"
spaces_logo="${repository_root}/art/spaces.svg"

if ! command -v magick >/dev/null 2>&1; then
    echo "error: ImageMagick's 'magick' command is required" >&2
    exit 1
fi

mkdir -p -- "${output_dir}"
mkdir -p -- "${launcher_icon_dir}"

temporary_images=()
cleanup() {
    rm -f -- "${temporary_images[@]}"
}
trap cleanup EXIT

publish_image() {
    local candidate="$1"
    local output="$2"

    if [[ -f "${output}" ]] \
        && magick compare -metric AE \
            "${output}" "${candidate}" null: 2>/dev/null; then
        rm -f -- "${candidate}"
        echo "unchanged ${output}"
        return
    fi

    mv -f -- "${candidate}" "${output}"
    echo "generated ${output}"
}

shopt -s nullglob
images=("${script_dir}"/*.png)

if ((${#images[@]} == 0)); then
    echo "error: no PNG files found in ${script_dir}" >&2
    exit 1
fi

for image in "${images[@]}"; do
    output="${output_dir}/$(basename -- "${image}")"
    candidate="$(mktemp --suffix=.png "${output_dir}/.overlay.XXXXXX")"
    temporary_images+=("${candidate}")

    magick "${image}" \
        -resize 110x110 \
        -background none \
        -gravity southeast \
        -extent 256x256 \
        "PNG32:${candidate}"

    publish_image "${candidate}" "${output}"
done

for distribution in arch fedora ubuntu; do
    distribution_icon="${script_dir}/${distribution}.png"
    output="${launcher_icon_dir}/spaces-${distribution}.png"
    candidate="$(
        mktemp --suffix=.png "${launcher_icon_dir}/.launcher.XXXXXX"
    )"
    temporary_images+=("${candidate}")

    magick "${distribution_icon}" \
        -background none \
        -gravity center \
        -extent 256x256 \
        \( "${spaces_logo}" \
            -background none \
            -resize 110x110 \
            -gravity southeast \
            -extent 256x256 \
        \) \
        -compose over \
        -composite \
        "PNG32:${candidate}"

    publish_image "${candidate}" "${output}"
done
