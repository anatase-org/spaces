Name:           spaces
Version:        0.0.1
Release:        1%{?dist}
Summary:        Spaces. Develop on the distribution of your choice, securely.

License:        AGPL-3.0-or-later
URL:            https://github.com/anatase-org/spaces
Source:       	https://github.com/anatase-org/spaces/archive/refs/tags/v%{version}.tar.gz

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
Requires:       container-selinux
Requires(post): policycoreutils
Requires(postun): policycoreutils

%description
Spaces provide a chroot-like sandboxing environment for you to access your favorite distributions: Arch, Fedora, Kali, and Ubuntu. A simple permission system ensures your local files and credentials remain secure, even if your space is compromised. Spaces are constructed directly using packages from your chosen distribution repositories with signature enforcement. No container middleman or surprises.

%prep
%autosetup -n %{name}-%{version}

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
  %{buildroot}%{_prefix}/local/share/applications/spaces
for distro in arch fedora ubuntu; do
  install -Dm644 "data/applications/spaces-${distro}.desktop" \
    "%{buildroot}%{_datadir}/applications/spaces-${distro}.desktop"
  install -Dm644 \
    "data/icons/hicolor/256x256/apps/spaces-${distro}.png" \
    "%{buildroot}%{_datadir}/icons/hicolor/256x256/apps/spaces-${distro}.png"
done

%post
%selinux_modules_install %{_datadir}/selinux/packages/spaces.pp
restorecon -RF %{_bindir}/spaces.priv %{_localstatedir}/lib/spaces \
  /usr/lib/spaces/guest %{_datadir}/spaces/portal %{_rundir}/spaces \
  %{_prefix}/local/share/applications/spaces \
  2>/dev/null || :
%systemd_post spaces@.service
%systemd_user_post spaces@.service

%preun
%systemd_preun spaces@.service
%systemd_user_preun spaces@.service

%postun
%systemd_postun_with_restart spaces@.service
%systemd_user_postun_with_restart spaces@.service
%selinux_modules_uninstall spaces

%files
%doc readme.md
%license LICENSE
%{_bindir}/%{name}*
%{python3_sitelib}/%{name}*
%{_datadir}/polkit-1/actions/org.anatase.spaces.policy
%{_datadir}/selinux/packages/spaces.pp
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
%dir /usr/lib/spaces
/usr/lib/spaces/spaces-pam-worker
/usr/lib/spaces/spaces-open-broker
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
