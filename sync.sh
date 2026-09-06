#!/usr/bin/env bash

set -euo pipefail

usage() {
    echo "Usage: $0 [--selinux] <ssh-host>" >&2
}

die() {
    echo "sync.sh: $*" >&2
    exit 1
}

install_selinux=false
remote_host=
while (( $# > 0 )); do
    case $1 in
        --selinux)
            install_selinux=true
            ;;
        -*)
            usage
            die "unknown option: $1"
            ;;
        *)
            [[ -z $remote_host ]] || {
                usage
                die "multiple SSH hosts specified"
            }
            remote_host=$1
            ;;
    esac
    shift
done

if [[ -z $remote_host ]]; then
    usage
    exit 2
fi

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

mapfile -t spaces_rpm_candidates < <(
    find "$build_dir/rpmbuild/RPMS" -type f \
        -name 'spaces-*.rpm' \
        ! -name 'spaces-selinux-*' \
        ! -name 'spaces-debuginfo-*' \
        ! -name 'spaces-debugsource-*'
)
mapfile -t selinux_rpm_candidates < <(
    find "$build_dir/rpmbuild/RPMS" -type f \
        -name 'spaces-selinux-*.rpm'
)

(( ${#spaces_rpm_candidates[@]} == 1 )) ||
    die "expected exactly one Spaces application RPM"
(( ${#selinux_rpm_candidates[@]} == 1 )) ||
    die "expected exactly one Spaces SELinux policy RPM"

spaces_rpm_path=${spaces_rpm_candidates[0]}
spaces_rpm_name=${spaces_rpm_path##*/}
selinux_rpm_path=${selinux_rpm_candidates[0]}
selinux_rpm_name=${selinux_rpm_path##*/}

rpm_paths=("$spaces_rpm_path")
rpm_names=("$spaces_rpm_name")
remote_rpms="~/$spaces_rpm_name"
if [[ $install_selinux == true ]]; then
    rpm_paths+=("$selinux_rpm_path")
    rpm_names+=("$selinux_rpm_name")
    remote_rpms+=" ~/$selinux_rpm_name"
fi

echo "Copying ${rpm_names[*]} to $remote_host:~/..."
scp "${rpm_paths[@]}" "$remote_host:"

echo "Installing ${rpm_names[*]} on $remote_host..."
ssh -t "$remote_host" "sudo rpm-ostree usroverlay || true
sudo systemctl stop 'spaces@*'
sudo dnf5 install -y $remote_rpms &&
sudo systemctl try-reload-or-restart polkit.service"

echo "Spaces is ready on $remote_host."
