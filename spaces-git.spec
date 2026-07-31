%global commit %(git rev-parse --verify HEAD)
%global shortcommit %(git rev-parse --short=12 %{commit})
%global gitversion %(tag=$(git describe --tags --abbrev=0 --match 'v[0-9]*' %{commit} 2>/dev/null || :); if test -n "$tag"; then version=${tag#v}; distance=$(git rev-list --count "$tag"..%{commit}); if test "$distance" -eq 0; then printf '%s' "$version"; else printf '%s^%s.g%s' "$version" "$distance" "%{shortcommit}"; fi; else printf '0.0.0^git%s.g%s' "$(git rev-list --count %{commit})" "%{shortcommit}"; fi)

Name:           spaces
Version:        %{gitversion}
Release:        1%{?dist}
Summary:        Spaces. Develop on the distribution of your choice, securely.

License:        AGPL-3.0-or-later
URL:            https://github.com/anatase-org/spaces
Source:         %{url}/archive/%{commit}/%{name}-%{commit}.tar.gz

ExclusiveArch:  x86_64 aarch64
BuildRequires:  gcc
BuildRequires:  binutils
BuildRequires:  container-selinux
BuildRequires:  glib2-devel
BuildRequires:  pkgconfig(gio-unix-2.0)
BuildRequires:  pam-devel
BuildRequires:  selinux-policy-devel
BuildRequires:  systemd-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  python3-build
BuildRequires:  python3-installer
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel

Requires:       python3
Requires:       python3-pillow
Requires:       python3-rich
Requires:       python3-textual
Requires:       librsvg2-tools
Requires:       polkit
Requires:       pam
Requires:       debootstrap
Requires:       dnf5
Requires:       arch-install-scripts
Requires:       systemd
Requires:       systemd-container
Requires:       glib2
Requires:       xdg-dbus-proxy
Requires:       %{name}-selinux

%description
Spaces provide a chroot-like sandboxing environment for you to access your favorite distributions: Arch, Fedora, Kali, and Ubuntu. A simple permission system ensures your local files and credentials remain secure, even if your space is compromised. Spaces are constructed directly using packages from your chosen distribution repositories with signature enforcement. No container middleman or surprises.

%package selinux
Summary:        SELinux policy for Spaces
BuildArch:      noarch
Requires:       container-selinux
Requires:       selinux-policy-targeted
Requires(post): policycoreutils
Requires(postun): policycoreutils

%description selinux
SELinux policy for Spaces. This package can remain installed on images that
do not include the Spaces application.

%prep
%autosetup -n %{name}-%{commit}

%build
%{python3} -m build --wheel --no-isolation
%make_build -C native
%{__make} -C native check-guest-abi
%{__make} -f %{_datadir}/selinux/devel/Makefile -C selinux spaces.pp

%install
%{python3} -m installer --destdir="%{buildroot}" dist/*.whl
%make_install -C native LIBEXECDIR=/usr/lib/spaces
install -Dm644 data/pam/spaces.system-auth \
  %{buildroot}%{_sysconfdir}/pam.d/spaces
install -Dm644 selinux/spaces.pp \
  %{buildroot}%{_datadir}/selinux/packages/spaces.pp
install -d -m0755 \
  %{buildroot}%{_sysconfdir}/spaces \
  %{buildroot}%{_prefix}/local/share/applications/spaces
for distro in arch fedora ubuntu; do
  install -Dm644 "data/applications/spaces-${distro}.desktop" \
    "%{buildroot}%{_datadir}/applications/spaces-${distro}.desktop"
  install -Dm644 \
    "data/icons/hicolor/256x256/apps/spaces-${distro}.png" \
    "%{buildroot}%{_datadir}/icons/hicolor/256x256/apps/spaces-${distro}.png"
done

%post
%systemd_post spaces@.service
%systemd_user_post spaces@.service

%preun
%systemd_preun spaces@.service
%systemd_user_preun spaces@.service

%postun
%systemd_postun_with_restart spaces@.service
%systemd_user_postun_with_restart spaces@.service

%post selinux
%selinux_modules_install %{_datadir}/selinux/packages/spaces.pp
restorecon -RF %{_bindir}/spaces.priv %{_localstatedir}/lib/spaces \
  /usr/lib/spaces/guest %{_datadir}/spaces/portal %{_rundir}/spaces \
  %{_prefix}/local/share/applications/spaces \
  2>/dev/null || :
restorecon -F /home/*/.ssh/config /root/.ssh/config 2>/dev/null || :

%postun selinux
%selinux_modules_uninstall spaces
if [ $1 -eq 0 ]; then
  restorecon -F /home/*/.ssh/config /root/.ssh/config 2>/dev/null || :
fi

%files
%doc readme.md
%license LICENSE
%{_bindir}/%{name}*
%{python3_sitelib}/%{name}*
%{_datadir}/polkit-1/actions/org.anatase.spaces.policy
%dir %{_datadir}/spaces
%dir %{_datadir}/spaces/pam
%{_datadir}/spaces/pam/spaces.common-auth
%{_datadir}/spaces/pam/spaces.system-auth
%{_datadir}/spaces/pam/spaces.ubuntu
%{_datadir}/spaces/pam/spaces.kali
%dir %{_datadir}/spaces/keys
%{_datadir}/spaces/keys/*
%dir %{_datadir}/spaces/repos
%{_datadir}/spaces/repos/*
%config(noreplace) %{_sysconfdir}/pam.d/spaces
%dir %{_sysconfdir}/spaces
%dir /usr/lib/spaces
/usr/lib/spaces/spaces-pam-worker
/usr/lib/spaces/spaces-integration-broker
%dir /usr/lib/spaces/guest
/usr/lib/spaces/guest/pam_spaces.so
/usr/lib/spaces/guest/spaces-portal
/usr/lib/spaces/guest/spaces-open
/usr/lib/spaces/guest/spaces-secret-helper
/usr/lib/spaces/guest/spaces-session-launcher
%dir %{_datadir}/spaces/portal
%{_datadir}/spaces/portal/*
%{_datadir}/applications/spaces-*.desktop
%{_datadir}/icons/hicolor/256x256/apps/spaces-*.png
%dir %{_prefix}/local/share/applications/spaces
%{_unitdir}/spaces@.service
%{_userunitdir}/spaces@.service

%files selinux
%license LICENSE
%{_datadir}/selinux/packages/spaces.pp
