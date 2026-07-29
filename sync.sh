#!/usr/bin/env bash

set -euo pipefail

usage() {
    echo "Usage: $0 <ssh-host>" >&2
}

die() {
    echo "sync.sh: $*" >&2
    exit 1
}

if (( $# != 1 )); then
    usage
    exit 2
fi

remote_host=$1
if [[ -z $remote_host || $remote_host == -* || $remote_host == *[[:space:]]* ]]; then
    die "invalid SSH host: $remote_host"
fi

for required_command in awk git podman scp ssh tar; do
    command -v "$required_command" >/dev/null ||
        die "required command not found: $required_command"
done

project_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd "$project_dir"

rpm_version=$(
    awk '$1 == "Version:" { print $2; exit }' spaces.spec
)
[[ $rpm_version =~ ^[0-9A-Za-z._+~-]+$ ]] ||
    die "could not read a safe Version from spaces.spec"

sync_id=$(date -u +%Y%m%d%H%M%S)
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/spaces-sync.XXXXXXXX")
source_dir=$build_dir/source
container_context=$build_dir/container
builder_image=localhost/spaces-rpm-builder:fedora44

cleanup() {
    if [[ -n ${build_dir:-} && -d $build_dir ]]; then
        rm -rf -- "$build_dir"
    fi
}
trap cleanup EXIT

mkdir -p "$source_dir" "$container_context"

cp Containerfile.sync "$container_context/Containerfile"

echo "Preparing the cached RPM builder..."
podman build --tag "$builder_image" "$container_context"

echo "Snapshotting the current checkout..."
git ls-files --cached --others --exclude-standard -z |
    tar --null --files-from=- --ignore-failed-read -cf - |
    tar -xf - -C "$source_dir"

tar -C "$source_dir" \
    --transform "s,^\\.,spaces-${rpm_version}," \
    -czf "$build_dir/v${rpm_version}.tar.gz" .

echo "Building Spaces ${rpm_version} RPM..."
podman run --rm \
    -e "RPM_VERSION=$rpm_version" \
    -e "SYNC_ID=$sync_id" \
    -v "$build_dir:/work:Z" \
    "$builder_image" \
    bash -euxo pipefail -c '
        mkdir -p \
            /work/rpmbuild/BUILD \
            /work/rpmbuild/BUILDROOT \
            /work/rpmbuild/RPMS \
            /work/rpmbuild/SOURCES \
            /work/rpmbuild/SPECS \
            /work/rpmbuild/SRPMS

        cp "/work/v${RPM_VERSION}.tar.gz" /work/rpmbuild/SOURCES/
        cp /work/source/spaces.spec /work/rpmbuild/SPECS/
        rpmbuild \
            --define "_topdir /work/rpmbuild" \
            --define "dist .fc44.sync${SYNC_ID}" \
            -ba /work/rpmbuild/SPECS/spaces.spec
    '

mapfile -t rpm_candidates < <(
    find "$build_dir/rpmbuild/RPMS" -type f \
        -name 'spaces-*.rpm' \
        ! -name 'spaces-debuginfo-*' \
        ! -name 'spaces-debugsource-*'
)

(( ${#rpm_candidates[@]} == 1 )) ||
    die "expected exactly one installable Spaces RPM"

rpm_path=${rpm_candidates[0]}
rpm_name=${rpm_path##*/}

echo "Copying $rpm_name to $remote_host:~/..."
scp "$rpm_path" "$remote_host:"

echo "Installing $rpm_name on $remote_host..."
ssh -t "$remote_host" "sudo rpm-ostree usroverlay || true
sudo dnf5 install -y ~/$rpm_name &&
sudo systemctl try-reload-or-restart polkit.service &&
sudo systemctl stop 'spaces@*'"

echo "Spaces is ready on $remote_host."
