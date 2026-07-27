# Maintainer: Antheas Kapenekakis <git@antheas.dev>

pkgname=spaces
pkgver=0.0.1
pkgrel=1
pkgdesc="Develop on the distribution of your choice, securely"
arch=('any')
url="https://github.com/anatase-org/spaces"
license=('AGPL-3.0-or-later')
depends=(
  'python'
  'python-rich'
  'python-textual'
  'polkit'
  'debootstrap'
  'ubuntu-keyring'
)
makedepends=(
  'python-build'
  'python-installer'
  'python-setuptools'
  'python-wheel'
)
source=("$pkgname-$pkgver.tar.gz::$url/archive/refs/tags/v$pkgver.tar.gz")
sha256sums=('SKIP')

build() {
  cd "$pkgname-$pkgver"
  python -m build --wheel --no-isolation
}

check() {
  cd "$pkgname-$pkgver"
  PYTHONPATH=src python -m unittest discover -s tests -v
}

package() {
  cd "$pkgname-$pkgver"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 readme.md "$pkgdir/usr/share/doc/$pkgname/readme.md"
}
