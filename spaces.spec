Name:           spaces
Version:        0.0.1
Release:        1%{?dist}
Summary:        Spaces. Develop on the distribution of your choice, securely.

License:        AGPL-3.0-or-later
URL:            https://github.com/anatase-org/spaces
Source:       	https://github.com/anatase-org/spaces/archive/refs/tags/v%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  systemd-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  python3-build
BuildRequires:  python3-installer
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel

Requires:       python3
Requires:       python3-rich
Requires:       python3-textual
Requires:       polkit
Requires:       debootstrap
Requires:       ubuntu-keyring
Requires:       systemd
Requires:       systemd-container

%description
Spaces provide a chroot-like sandboxing environment for you to access your favorite distributions: Arch, Fedora, and Ubuntu. A simple permission system ensures your local files and credentials remain secure, even if your space is compromised. Spaces are constructed directly using packages from your chosen distribution repositories with signature enforcement. No container middleman or surprises.

%prep
%autosetup -n %{name}-%{version}

%build
%{python3} -m build --wheel --no-isolation

%install
%{python3} -m installer --destdir="%{buildroot}" dist/*.whl

%post
%systemd_post spaces@.service

%preun
%systemd_preun spaces@.service

%postun
%systemd_postun_with_restart spaces@.service

%files
%doc readme.md
%license LICENSE
%{_bindir}/%{name}*
%{python3_sitelib}/%{name}*
%{_datadir}/polkit-1/actions/org.anatase.spaces.policy
%{_unitdir}/spaces@.service
