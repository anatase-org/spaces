# Portal and desktop bridge test methodology

This document describes the manual integration tests for the public portal
router and the non-portal desktop bridges.  The probes are diagnostics rather
than a fully unattended test suite: several portal calls deliberately display
host UI and may be accepted or cancelled by the tester.

The important pass condition is that a call reaches the host service, returned
object paths and Unix file descriptors remain usable in the Space, and the
guest caller can close every request or session it owns.  A host portal response
such as `cancelled`, `not allowed`, or `service unavailable` is not a routing
failure when the same call has the same result directly on the host.

## Broker identity and login races

`test_broker_registration.py` compiles the production broker and runs it against
`broker_portal_fixture.py` on a private D-Bus daemon. The fixture keys secrets
by the caller's registered application ID, records registration and secret
requests, and can delay or reject registration. It never opens the real desktop
portal or accesses a user's wallet. Requirements: a C compiler, pkg-config,
GLib development files, dbus-daemon, and Python GI/Gio.

Run inside the test checkout in the Fedora Space:

```sh
PYTHONPATH=src python3 -m unittest tests.test_broker_registration -v
```

To reproduce issue #4 with the original broker, export its source from the
pre-fix checkout and put it in the test environment, then run:

```sh
git show 8a07f9d:native/spaces_broker.c > /tmp/spaces-broker-before.c
SPACES_BROKER_SOURCE=/tmp/spaces-broker-before.c python3 -m unittest \
  tests.test_broker_registration.BrokerRegistrationTests.test_broker_before_portal_never_uses_unregistered_identity -v
```

On `win5`'s Fedora 44 Space, the original broker served a 64-byte derived secret
after starting before the portal; the fixture recorded an empty application ID
instead of `org.anatase.Spaces.fedora`. The regression fails on that assertion.
The corrected broker rejects requests until registration succeeds. Coverage also
includes transient errors, a slow registration, owner replacement, a late reply
from an old owner, portal disappearance/reappearance, and an ambiguous timeout.
Because [Registry.Register is one-shot](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.host.portal.Registry.html),
a timeout requires supervisor replacement
with a fresh D-Bus connection; known failures retry with exponential backoff
capped at 30 seconds. The ready FD describes the integration transport, rather
than claiming that the optional host portal is ready.

Live validation on 2026-10-31, `win5`, SELinux enforcing: installed the test broker
in a temporary `/usr` overlay; stopped the host portal, started brokers while it
was absent, and verified prompt registration errors and no portal activation.
Starting and restarting the host portal restored secret retrieval without
restarting brokers, with the same derived secret. A freshly started unmodified
broker returned the same secret as the correction. The existing Fedora managed
wallet rejected that key; with the user's authorization, retained its encrypted
files and metadata in
`/home/dev/.local/share/kwalletd/spaces-issue4-backup-3rbrqfmd` and recreated the
managed wallet. Repeated KWallet activation/unlock then passed, including after
a host portal restart. The installed test broker is discarded on host reboot;
these checks do not establish reliability across subsequent desktop logins.
The full Fedora test suite passed with 519 tests run and 20 skips; native build
and guest ABI checks passed too.

## Host accessibility (AT-SPI)

Desktop permissions have three levels: `disabled`, `basic`, and
`accessibility`. The wizard calls the last level **Plus Accessibility and Agent
computer use**. The Develop preset enables this tier by default, including
existing Develop spaces on their next start. The Basic/application preset uses
basic integration; its users can select the accessibility tier through Customise.
Custom legacy boolean grants keep their meaning (`false` is disabled,
`true` is basic). Configuration saves write the canonical strings.

The accessibility tier forwards the selected host user's dedicated AT-SPI
bus. It provides the accessible tree, supported semantic actions, text editing,
and events; it does not provide screenshots or general pointer/keyboard input.
It does not change host accessibility settings. `org.a11y.Status.IsEnabled`
being false does not imply that the registry or applications are unavailable.
Toolkit support and application accessibility exposure remain prerequisites.

Run the automated coverage with:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p test_accessibility.py -v
```

The private-bus tests need `dbus-daemon`, `busctl`, `xdg-dbus-proxy`, and Python
GI/Gio in the test environment. They skip explicitly when the proxy/bus or GI
dependencies are missing. These are test requirements; production forwarding
adds no Python GI dependency and reuses Spaces' existing host proxy dependency.
The tests exercise real discovery/proxy traffic with a fake registry and a
separate application bus connection, including unique-name traversal, Unicode
editing, actions, events, reverse callbacks, application replacement, and
disconnecting held connections. Controller tests cover retries, source
replacement, logout, downgrade, and failed-update rollback.

For a real desktop test, run each fixture from a host graphical terminal:

```sh
python3 tests/manual_atspi_fixture.py gtk spaces-test-gtk /tmp/spaces-atspi-gtk.json
```

```sh
python3 tests/manual_atspi_fixture.py qt spaces-test-qt /tmp/spaces-atspi-qt.json
```

GTK requires Python GI with GTK 3; Qt requires PySide6. Fixtures do not enable
host accessibility settings. Use fresh tokens and state files for each run.
Configure the test Space with accessibility integration and restart it through
the usual Spaces configuration flow. With the checkout shared into its home,
run (adjust the checkout path and Space name):

```sh
spaces enter fedora -- python3 /home/dev/Projects/spaces/tests/manual_atspi_probe.py spaces-test-gtk
spaces enter fedora -- python3 /home/dev/Projects/spaces/tests/manual_atspi_probe.py spaces-test-qt
```

The guest probe uses its stock `libatspi.so.0` through Python ctypes, so it
needs no GI bindings or agent SDK. It must find the uniquely named host window,
invoke the button and checkbox, enter `Héllo 世界`, and receive state events.
Independently inspect the **host** JSON files: after queued toolkit callbacks
settle, both must contain `clicks: 1`, the Unicode text, and `checked: true`.
A successful method return alone is insufficient.

Downgrade to basic integration and launch a fresh guest process:

```sh
spaces enter fedora -- python3 /home/dev/Projects/spaces/tests/manual_atspi_probe.py spaces-test-gtk --expect-absent
```

Confirm that the guest manager/activation environment no longer contains the
forwarded address (an empty value means native discovery), the private proxy
has exited, and its socket is no longer mounted. Test logout and replacement
of the selected graphical login too. Basic and disabled tiers must not create
the host accessibility bridge. Guest-native accessibility can still work.
For user isolation, try the host proxy socket as a second ordinary UID: the
private directory/socket must reject it. Root and guest administrators are
outside this Unix permission isolation check.

Keep SELinux enforcing during live tests and inspect audit denials. Do not
disable enforcement or deploy policy using `sync.sh --selinux`; any required
policy installation is user-operated. Close the fixture windows and remove
their temporary state files afterwards.

Validation on 2026-10-31: private-bus traffic passed on the KDE/Wayland host;
GTK and Qt actions, Unicode editing, and events passed through the production
bridge/controller into the running Fedora Space using a temporary test grant.
Host state confirmed both button clicks and checkbox changes. A new guest
process could not discover the host fixture after revocation; a second ordinary
UID was denied access to the proxy socket. SELinux stayed enforcing with no
AVC denials. This exercised the running host/guest bridge without installing
the updated service or changing the Space's persistent permission settings.
Discovery, proxy startup, guest registry traversal, and cleanup were also
verified under the service's actual `spaces_t` SELinux domain while enforcing.

## Probe files

- `manual_portal_probe.sh` performs synchronous property, status, enumeration,
  and negative-API checks.
- `manual_portal_async_probe.py` subscribes to request responses and exercises
  translated request/session paths, cancellation, Unix FDs, file staging,
  pidfds, and cleanup.
- `manual_bridge_fixture.py` publishes a guest StatusNotifierItem, DBusMenu, and
  MPRIS player so their host mirrors can be inspected and invoked.
- `manual_kwin_windows.js` reports KWin's application identity and supported
  window operations.  It is also useful for detecting regressions where an app
  receives only a close button.
- `manual_kwin_screenshot.py` captures the host KWin workspace directly, without
  using the Screenshot portal being tested, so portal UI and window-decoration
  results can be recorded independently.

Automated fake-bus and packaging coverage lives primarily in `test_portal.py`
and `test_packaging.py`.  Run the complete suite before the live probes:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
make -C native clean all
make -C native check-guest-abi
```

Clean native outputs again after testing with `make -C native clean`.

## Live-test setup

The examples use the host `win5` and an Arch Space named `arch`.  Substitute a
different host or Space as needed.

```sh
./sync.sh win5
scp tests/manual_portal_probe.sh \
    tests/manual_portal_async_probe.py \
    tests/manual_bridge_fixture.py \
    tests/manual_kwin_windows.js \
    tests/manual_kwin_screenshot.py win5:/tmp/
ssh win5
```

On the host, start a graphical process so the Space and its portal router stay
alive, then copy the guest probes.  `machinectl` must always be run with `sudo`.

```sh
space=arch
nohup spaces enter --graphical "$space" -- sleep infinity \
    >/tmp/spaces-probe-session.log 2>&1 </dev/null &
sleep 3
sudo machinectl --quiet copy-to "$space" \
    /tmp/manual_portal_probe.sh /tmp/manual_portal_probe.sh
sudo machinectl --quiet copy-to "$space" \
    /tmp/manual_portal_async_probe.py /tmp/manual_portal_async_probe.py
sudo machinectl --quiet copy-to "$space" \
    /tmp/manual_bridge_fixture.py /tmp/manual_bridge_fixture.py
```

Run the general probe groups from the host:

```sh
spaces enter "$space" -- bash /tmp/manual_portal_probe.sh
spaces enter "$space" -- python3 /tmp/manual_portal_async_probe.py core
spaces enter "$space" -- python3 /tmp/manual_portal_async_probe.py dialogs
spaces enter "$space" -- python3 /tmp/manual_portal_async_probe.py sessions
spaces enter "$space" -- python3 /tmp/manual_portal_async_probe.py requests
spaces enter "$space" -- python3 /tmp/manual_portal_async_probe.py \
    usb-acquire camera-fd screencast-fd
```

The `dialogs`, `requests`, and ScreenCast groups can display host dialogs.
Accept a request when its returned data or FD is under test; cancellation is
enough when only request translation and cleanup are under test.

Test with SELinux enforcing first.  If an FD-bearing call fails, capture the
proxy log and AVCs, repeat once in permissive mode, and restore the original
mode afterward.  A pass only in permissive mode means the implementation needs
a repository policy change; do not edit the installed policy ad hoc.

## Public portal interfaces

The following table lists every public interface in the router allowlist.
`Static` refers to `manual_portal_probe.sh`; the other names are selectors for
`manual_portal_async_probe.py`.

| Interface | Probe and operation | Expected result |
|---|---|---|
| `org.freedesktop.portal.Account` | `requests`: `GetUserInformation`; Static: `Properties.GetAll` | A translated request path and one `Response`. Accepting returns the host user's ID, name, and image URI. Closing or host cancellation must be relayed cleanly. |
| `org.freedesktop.portal.Access` | One-off `Properties.GetAll` exposure check shown below; automated fake-bus request relay test | The interface is exposed only when present on the host. An access dialog call returns a guest request path and relays the host response without altering its options. |
| `org.freedesktop.portal.Background` | Static: `SetStatus`; `dialogs`: `RequestBackground` with autostart | Status reaches the host. If autostart is accepted, the host desktop entry wraps the command as `spaces enter --graphical <space> -- <guest command>`. Remove the test autostart entry after inspection. |
| `org.freedesktop.portal.Camera` | Static: `IsCameraPresent`; `requests`: `AccessCamera`; `camera-fd`: `OpenPipeWireRemote` | Access returns a translated response. The remote call returns exactly one valid PipeWire socket FD that can be `fstat`ed in the Space. |
| `org.freedesktop.portal.Clipboard` | `sessions`, as part of a RemoteDesktop session: `RequestClipboard` and `SetSelection` | Calls are relayed against the translated session. `SetSelection` succeeds only when clipboard access was enabled by the host; otherwise the host's `AccessDenied` is expected. |
| `org.freedesktop.portal.DynamicLauncher` | `dialogs`: `PrepareInstall`; `requests`: `RequestInstallToken` | The icon and request are brokered to the host. Acceptance produces a namespaced launcher whose actions enter the Space. A host app-store allowlist denial is valid when reproduced directly on the host. |
| `org.freedesktop.portal.Email` | `requests`: `ComposeEmail` | The returned handle is the expected guest request path. Success, cancellation, or absence of a configured mail application is relayed as a normal portal response. |
| `org.freedesktop.portal.FileChooser` | `dialogs`: local `OpenFile` and cancellation | The best guest backend is selected using portal configuration order. It returns a guest request path, forwards cancellation, and returns guest/document-portal URIs. With no matching backend, expect `org.freedesktop.portal.Error.NotAvailable`. |
| `org.freedesktop.portal.GameMode` | Static and `core`: `QueryStatus`, `RegisterGame`, `UnregisterGame` using the caller PID | The guest PID is converted to a pidfd and the host version-4 methods are called. A configured host returns its GameMode status. `-2` is expected on a host without the GameMode daemon and proves that the call reached the host API. |
| `org.freedesktop.portal.GlobalShortcuts` | `sessions`: `CreateSession`, `ListShortcuts`, and `Close` | Request and session handles use the guest sender/token, listing succeeds, and close removes the host session. The host must recognize the deterministic Space desktop identity. |
| `org.freedesktop.portal.Inhibit` | `sessions`: `Inhibit` and request `Close` | A translated request path is returned and can be closed by its owning guest sender. This is the portal interface, not the omitted legacy PowerManagement inhibit service. |
| `org.freedesktop.portal.InputCapture` | `sessions`: `CreateSession2`, `GetZones`, and `Close` | Creation returns a translated session and close succeeds. `GetZones` should return host zones on a working host. On `win5` it currently returns `Invalid session` even when called directly on the host, so that host-identical error is not a router regression. |
| `org.freedesktop.portal.Location` | `sessions`: direct `CreateSession`, asynchronous `Start`, and `Close` | The direct session path and Start request are translated, the host response is received, and close succeeds. Location data/signals remain controlled by the host permission and location services. |
| `org.freedesktop.portal.NetworkMonitor` | Static: `GetAvailable`, `GetMetered`, `GetConnectivity`, `GetStatus`, and `CanReach` | Values match the host rather than the guest veth. `CanReach("example.com", 443)` returns a boolean without a routing error. |
| `org.freedesktop.portal.Notification` | Static: `AddNotification` and `RemoveNotification` | The host notification appears with the supplied ID and can be removed. The calls have no Space-side policy filtering. |
| `org.freedesktop.portal.OpenURI` | Static: `SchemeSupported`; `requests`: ordinary `OpenURI`; `core`: `OpenFile` and `OpenDirectory` with proof FDs | Ordinary URIs relay directly. Path-bearing calls validate the guest proof FD, map the file or directory through the broker, and relay the host response. A mismatched FD/path must be rejected. |
| `org.freedesktop.portal.PowerProfileMonitor` | Static: read `power-saver-enabled` | The boolean matches the host power-saver state, and host property changes are relayed. |
| `org.freedesktop.portal.Print` | `requests`: `PreparePrint` followed by response or request close | A translated handle is returned. Accepting relays host print settings; cancelling or closing terminates the host request without leaving an owned request behind. |
| `org.freedesktop.portal.ProxyResolver` | Static: `Lookup("https://example.com/")` | Returns the host proxy list, commonly `direct` on an unproxied host. |
| `org.freedesktop.portal.Realtime` | Static and `core`: `MakeThreadHighPriorityWithPID` for the caller's process/thread | The broker opens process and thread pidfds, verifies membership, maps them to host IDs, bounds `RLIMIT_RTTIME`, and calls the host portal. Success is expected on an authorized RTKit host. `win5` currently returns host `AccessDenied` for both routed and direct calls; no numeric-PID or privileged fallback is acceptable. |
| `org.freedesktop.portal.RemoteDesktop` | `sessions`: `CreateSession`, `SelectDevices`, Clipboard calls, and `Close` | All request/session paths are guest paths, host responses arrive once, follow-up methods use the translated host session, and close succeeds. |
| `org.freedesktop.portal.ScreenCast` | `sessions` for lifecycle; `screencast-fd` for `CreateSession`, `SelectSources`, `Start`, `OpenPipeWireRemote`, and non-consuming read/write checks on the returned socket | Selecting a source produces at least one stream `(node_id, properties)`. `OpenPipeWireRemote` returns one connected socket FD on which a zero-byte write succeeds and a peek either finds protocol data or would block. `PermissionError`, `MSG_CTRUNC`, missing FDs, or a disconnected proxy are failures, commonly indicating SELinux FD-use denial. |
| `org.freedesktop.portal.Screenshot` | `dialogs`: non-interactive `Screenshot` | Accepting returns a guest-visible `file:` URI. The broker stages a bounded host file into the Space and cleans temporary host data afterward. Nested Flatpak callers receive a document-portal URI. |
| `org.freedesktop.portal.Secret` | `core`: call `RetrieveSecret` twice with separate memfds; `secret-apps`: ask the integration broker to derive for two synthetic application IDs | Each call writes 64 bytes. Results are stable for one Space/application pair and differ for different application IDs; the raw host secret is never returned. |
| `org.freedesktop.portal.Settings` | Static: `ReadAll` | Returns host settings and namespaces, including the current button layout. Property-setting signals must be relayed without changing their signatures. |
| `org.freedesktop.portal.Usb` | Static: `EnumerateDevices`; `sessions`: create/close; `usb-acquire`: `AcquireDevices` and `FinishAcquireDevices` | Host devices are enumerated without consulting the Space device configuration. Acquisition returns the host response and usable device FDs unchanged, including for devices without a guest `/dev` node. |
| `org.freedesktop.portal.Wallpaper` | `dialogs`: `SetWallpaperFile` with a guest image FD | The broker stages the bounded file and invokes the host portal. Accepted wallpaper data remains in the host cache because desktops may retain its path; cancelled and failed requests are cleaned. |

`org.freedesktop.portal.Request` and `org.freedesktop.portal.Session` are tested
through every asynchronous group.  Returned paths must contain the guest
caller's unique-name component and requested token.  A different guest sender
must not be able to close or use them, and disconnecting the owner must close
the associated host resources.  The automated fake-bus tests cover this
cross-caller isolation and owner-loss cleanup directly.

Use the following one-off call inside the Space to verify that Access was
advertised by host introspection.  An empty property dictionary is a successful
result because Access currently has no public properties.

```sh
gdbus call --session --dest org.freedesktop.portal.Desktop \
    --object-path /org/freedesktop/portal/desktop \
    --method org.freedesktop.DBus.Properties.GetAll \
    org.freedesktop.portal.Access
```

## Non-portal bridges

| Interface | Probe and operation | Expected result |
|---|---|---|
| `org.freedesktop.PowerManagement` | Static: `CanSuspend`, `GetPowerSaveStatus`, and negative `Suspend` | Read-only capabilities/status match the host and signals relay. `Suspend` and `Hibernate` must return `UnknownMethod`. |
| `org.freedesktop.Notifications` | Static: `GetCapabilities`; automated tests exercise `Notify`, `CloseNotification`, actions, close signals, and local `x-kde-urls` hints | Methods and the allowed unicast/broadcast signals relay to the host with notification ID ownership preserved. Local file hints are inode-validated through the broker and rewritten to their canonical host backing URIs without copying. |
| `org.freedesktop.ScreenSaver` | Static: `GetActive`; automated tests exercise both standard paths, inhibition cookies, signals, and disconnect cleanup | State matches the host. Each caller owns its cookies; disconnecting it releases its outstanding host inhibition. Absolute guest executable names are mapped to generated Space shortcut IDs so the host can display the exported application name and icon. |
| `org.freedesktop.FileManager1` | Call `ShowFolders` or `ShowItems` with an existing guest `file:` URI; automated mapping tests cover rejection cases | The host file manager opens the broker-mapped host path. Unmapped, escaping, or invalid guest paths are rejected. |
| `org.kde.StatusNotifierWatcher` / `org.kde.StatusNotifierItem` / `com.canonical.dbusmenu` | Run `manual_bridge_fixture.py`, then inspect and invoke the dynamically named host mirror | The item registers under a readable, collision-free `org.kde.StatusNotifierItem.spaces.space.<space>-<app>.item.i<n>` name. Properties, menu layout, calls, and signals proxy both ways. Stopping the fixture removes the mirror. |
| `org.mpris.MediaPlayer2.*` | Run `manual_bridge_fixture.py`, then inspect root, Player, TrackList, and Playlists on its host mirror | A readable, collision-free `org.mpris.MediaPlayer2.spaces.space.<space>-<app>.player.i<n>` name appears. Reads, writable properties, methods, and signals proxy both ways. Local artwork, including files on private guest mounts such as `/tmp`, is copied from a validated bounded descriptor to a host cache URI and removed on owner loss. Players that return empty introspection XML use the generic MPRIS schema. |

Exercise FileManager path mapping from inside the Space with an existing guest
directory:

```sh
gdbus call --session --dest org.freedesktop.FileManager1 \
    --object-path /org/freedesktop/FileManager1 \
    --method org.freedesktop.FileManager1.ShowFolders \
    "['file:///tmp']" ''
```

To exercise the two dynamic bridges, run the fixture in the Space and leave it
running:

```sh
spaces enter "$space" -- python3 /tmp/manual_bridge_fixture.py
```

In a second host shell, discover the generated names and inspect them:

```sh
status_name=$(busctl --user list --no-legend | \
    awk '/org\.kde\.StatusNotifierItem\.spaces\./ { print $1; exit }')
player_name=$(busctl --user list --no-legend | \
    awk '/org\.mpris\.MediaPlayer2\.spaces\./ { print $1; exit }')

busctl --user get-property "$status_name" /StatusNotifierItem \
    org.kde.StatusNotifierItem Title
busctl --user call "$status_name" /MenuBar \
    com.canonical.dbusmenu GetLayout iias 0 -1 0
busctl --user call "$status_name" /StatusNotifierItem \
    org.kde.StatusNotifierItem Activate ii 10 20

busctl --user get-property "$player_name" /org/mpris/MediaPlayer2 \
    org.mpris.MediaPlayer2 Identity
busctl --user get-property "$player_name" /org/mpris/MediaPlayer2 \
    org.mpris.MediaPlayer2.Player Metadata
busctl --user set-property "$player_name" /org/mpris/MediaPlayer2 \
    org.mpris.MediaPlayer2.Player Volume d 0.75
busctl --user call "$player_name" /org/mpris/MediaPlayer2 \
    org.mpris.MediaPlayer2.Player Play
```

The fixture prints `EVENT` or `SET` for host-to-guest calls.  Send it `SIGUSR1`
to emit guest-to-host property/status signals, then terminate it and verify both
generated bus names disappear.

## Optional credential activation services

The existing `org.freedesktop.secrets`, `org.kde.secretservicecompat`,
`org.kde.kwalletd5`, and `org.kde.kwalletd6` activation names are not public
portal-router interfaces.
They start the guest KWallet integration independently when the corresponding
guest packages are installed.  Test the interface appropriate to the image;
for a Secret Service client this can be done with `secret-tool`:

```sh
printf '%s' 'spaces-value' | spaces enter "$space" -- \
    secret-tool store --label='Spaces methodology probe' \
    spaces-methodology key
spaces enter "$space" -- secret-tool lookup spaces-methodology key
```

Expect the lookup to return `spaces-value`, no host wallet file to be mounted
inside the Space, and a different Space not to unlock or read this Space's
wallet.  On KF6 the helper starts the `kwalletd6` compatibility proxy and
performs a real `org.kde.KWallet.open` call to verify that the managed
collection is usable; the Secret Service `Locked` property is not a sufficient
startup check.  A failed open backs up the managed `.kwl`, `.salt`, and
`_attributes.json` files with the first free shared numeric suffix before
recreating the wallet.  A D-Bus timeout kills the provider and leaves the
wallet files untouched.  Test the timeout branch with a managed
`dbus-test-tool echo --sleep-ms=100000` owner and compare the wallet checksum
before and after the helper call; the helper must fail, remove the owner, and
preserve the checksum.  If the image has no KWallet/Secret Service
implementation, activation
is optional and this test is recorded as unavailable rather than a portal
router failure.  SSH/GPG credential-agent forwarding and desktop shortcut
export are likewise outside the D-Bus interface matrix; retain their automated
coverage in `test_session.py`, `test_launch.py`, and `test_shortcuts.py`.

## Application identity and window controls

Start any graphical application in the Space; the test must not depend on its
executable name.  Chrome is a useful regression case because the broken router
previously produced incorrect identity and only a close button.

```sh
spaces enter --graphical "$space" -- google-chrome-stable --new-window \
    https://example.com/
busctl --user call org.kde.KWin /Scripting org.kde.kwin.Scripting \
    unloadScript s spaces_window_probe || true
busctl --user call org.kde.KWin /Scripting org.kde.kwin.Scripting \
    loadScript ss /tmp/manual_kwin_windows.js spaces_window_probe
busctl --user call org.kde.KWin /Scripting org.kde.kwin.Scripting start
journalctl --user --since '1 minute ago' | \
    grep -E 'desktopFileName|resourceClass|minimizable|maximizable'
```

For a normal resizable application, expect a real `desktopFileName` and
`resourceClass`, with `closeable`, `minimizable`, `maximizable`,
`fullScreenable`, and `resizeable` all true.  During the graphical session, the
host must also contain one hidden deterministic identity named
`org.anatase.Spaces.<name>.desktop`; it must disappear when the portal proxy is
closed, unless its inode was replaced by the user.

### Independent KWin screenshot

Use `manual_kwin_screenshot.py` on the host to preserve visual evidence of the
workspace after opening a portal dialog or Space application:

```sh
python3 /tmp/manual_kwin_screenshot.py /tmp/spaces-workspace.png
file /tmp/spaces-workspace.png
test -s /tmp/spaces-workspace.png
```

The script requires one output path, replaces that file with mode `0600`, and
passes its FD to KWin's `org.kde.KWin.ScreenShot2.CaptureWorkspace` method.  It
must be run as the graphical host user with access to that user's session bus.
Expected output is a non-empty workspace image readable by an image viewer.

This is intentionally independent of `org.freedesktop.portal.Screenshot`: it
does not validate portal request translation, staging, or guest URI import.
Use the `dialogs` selector for those checks.  The KWin capture is instead used
to confirm visible host consent UI and application decorations, such as the
presence of minimize and maximize buttons in the Chrome regression case.

## Explicitly absent APIs

| API | Negative test | Expected result |
|---|---|---|
| `org.freedesktop.PowerManagement.Inhibit` | Ask the guest session bus for its name owner | `NameHasNoOwner`; the separate legacy service is intentionally not implemented. |
| `Suspend` and `Hibernate` on `org.freedesktop.PowerManagement` | Static negative call or `gdbus call` | `UnknownMethod`; the facade is read-only. |
| `org.freedesktop.UPower` host facade | Inspect the guest system bus and compare any owner/device paths with the host | Spaces does not publish or proxy UPower. A guest distribution may independently run its own UPower service, which must not be mistaken for a host facade. |

## Cleanup

Close every prompt, stop the fixture, remove any accepted test launcher or
Background autostart entry, and terminate only the Space started for this test:

```sh
sudo machinectl terminate "$space"
```

If SELinux was temporarily switched to permissive for diagnosis, restore its
original mode after collecting `ausearch -m AVC` output.

## Host system D-Bus bridge

`tests/test_system_bus.py` exercises two private D-Bus daemons and mock host
services. Build with `make -C native check-guest-abi`, then run the complete
bridge suite with:

```sh
sudo env PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_system_bus.py
```

Root is required to test the production EXTERNAL authentication boundary and
ordinary-user calls. The mocks never contact the host resolver or NetworkManager.
The suite covers descriptor transfer in both directions, remote errors, dynamic
introspection and properties, NetworkManager's ObjectManager parent path,
credential callbacks and spoof rejection, broadcasts, restarts, client cleanup,
message limits, and independent space permissions.

The root-only mount regression uses a private mount namespace with Ubuntu-style
systemd aliases. With the bridge mounts applied, it verifies that package-owned
units and D-Bus activation files still support hard-link backups and replacement.
Guest daemons are suppressed with condition drop-ins; D-Bus activation overrides
live in `/etc/dbus-1/system-services`, ahead of the vendor directories.

For upgrade validation, install the updated Spaces package and fully stop/start
the Ubuntu space so the old bind mounts disappear. Inside the guest, run
`sudo apt --fix-broken install`, then retry the upgrade. Confirm that
`systemctl show systemd-resolved.service -p ConditionResult -p ActiveState`
reports the guest resolver inactive after a start attempt, and that
`resolvectl status` still reaches the host bridge.

| Service operation | Ordinary guest users | Guest root |
| --- | --- | --- |
| UPower properties, enumeration, history and refresh | Allowed | Allowed |
| UPower configuration | Denied | Denied |
| Resolver and NetworkManager status | Allowed | Allowed |
| Host DNS or NetworkManager changes | Only Network Admin | Only Network Admin |
| NetworkManager secrets and credential-agent registration | Only Network Admin | Only Network Admin |

Network Admin authorizes all guest users, without requiring guest root. Forwarded
calls use the root broker’s host identity, so this grants authority directly rather
than reproducing host per-user polkit authorization.

The Develop preset already grants Network Admin. Guest sudo uses the existing
administrator permission; desktop and host-authentication permissions do not gate
the system bridge. NetworkManager profiles, VPN plugins, and file-path arguments
belong to the host. Guest logind is unchanged. Hosts without a requested service
receive service-unavailable errors instead of falling back to guest daemons.

For live validation, use a temporary private guest bus with the relay and broker
against the real host services. Verify `nmcli general status`, `resolvectl status`,
and `upower -e`; ordinary-user `nmcli general permissions` must deny changes.
Create a disposable dummy interface, set its DNS through the bridge, confirm the
host sees that setting, check an ordinary user cannot revert it, then revert as
root and remove the interface. Do not assign a default route or a `~.` routing
domain during this test. Finally validate packaged Ubuntu startup and NordVPN
connectivity under the installed Spaces SELinux domain. Updating that policy is
a user action; never run `sync.sh --selinux` as the agent.

## scu computer use

### Current KWin-only backend

`scu mcp` exposes exactly `status` and `computer` over stdio. All computer use
requires the **Plus Accessibility and Agent computer use** desktop tier. Basic
and disabled spaces receive a configuration error with `spaces configure
<space>`. MCP initialization and schema discovery remain available without the
permission or optional libraries. Preset defaults are unchanged.

The desktop controller prepares a private listener without starting the SCU
engine. A short `scu-host --listen` setup command calls `listen()` as the host
user, establishes the peer credentials guests authenticate, and exits without
initializing the engine. The controller's existing wait loop watches dormant
listeners. A pending guest connection makes it register the listener through
the control socket, activating `spaces-scu-host.service` in the host user's
systemd manager. All active Spaces for that UID share one
`scu-host --user-service`, outside individual Spaces' cgroups. Guests receive only their own
`/run/spaces/desktop/<uid>/computer/bus`; the host control socket, session bus,
logind connection and KWin authorization remain private.

Each controller retains its listening descriptor and holds a registration lease
while its clients use the host. Revocation closes only that Space's
listener and clients, discards its queued work, cancels its active request and
acknowledges neutralization before unmounting. The host releases idle leases and
exits two seconds after its final client disconnects; this brief grace period
avoids repeated startup when clients reconnect quickly. A later connection activates it again
through the same mounted socket, without remounting or another watcher process.
Logind session loss also retires it; reconciliation prepares a listener again
when the selected desktop returns. Executable replacement restarts the common
service when needed, disconnecting clients without replaying actions.

One event loop executes complete MCP requests sequentially while continuing to
receive cancellation and disconnect notifications. EIS, geometry, the current
full-overview capture and accessibility handles are shared. JSON-RPC request IDs
are scoped to their originating connection. A later overview supersedes the
current capture for every client. The existing restartable AT-SPI helper and its
private pipe protocol are unchanged.

Before screenshots or EIS input, the host checks the selected session through
logind, requests `Unlock()` when needed and waits for both logind and the screen
locker. The unlock wait is bounded to five seconds and the request deadline.
It leaves the desktop unlocked afterward. Accessibility-only steps with
`observe:false` and pure delays do not unlock the desktop; window observations
include screenshots and therefore require unlocking.

While MCP clients are connected, `kscreen-doctor` supplies the output inventory.
If none is usable, the host supervises one `krfb-virtualmonitor` at 1280x720,
scale 1. It passes no password, which makes KRfb create the output without
starting a VNC listener. ScreenShot2 still supplies SCU images. The fallback is
removed when the final client disconnects or a physical output returns. The KDE
helpers are RPM recommendations, so Spaces remains installable without KDE.
KScreen omits `enabled` for outputs that cannot be disabled; those connected
outputs count as usable. The host does not change their enablement, position,
scale or priority through KScreen.

`status({})` captures the full desktop and returns one inline PNG together with:

```json
{
  "canvas": {
    "width": 1600,
    "height": 900,
    "id": 1
  },
  "apps": [{
    "pid": 123,
    "app_name": "firefox",
    "windows": [{
      "window_id": 1,
      "title": "Example Domain",
      "bounds": {"x": 100, "y": 100, "width": 900, "height": 600},
      "shown": true,
      "active": true
    }]
  }]
}
```

Window enumeration and placement come from KWin, including applications without
AT-SPI support. Window IDs are stable within the MCP process. Status must return
an error if any required library, the accessibility registry, KWin input or
capture is unavailable. It returns no health text, partial metadata or image on
failure. `computer` checks the same prerequisites before starting a batch.
MCP initialization and schema discovery remain usable without those prerequisites.
Status does not report success with a null canvas. Its
entire operation is bounded to 30 seconds. Explicit cancellation and clean EOF
release input and close the host connection.

`computer` accepts only `steps`, `observe`, and optional `id`.
The capture `id` is a positive integer, starting at 1 for each MCP process.
The same numeric ID appears in canvas, desktop/zoom and tree observations.
An explicit pixel-step `id` overrides the batch ID; omission uses the latest
valid overview. Strings and the old `capture_id` spelling are rejected.
Steps are ordered `{tool,args}` objects, at most 64. The supported actions are
`click`, `double_click`,
`right_click`, `move_cursor`, `drag`, `scroll`, `type_text`, `set_value`,
`press_key`, `hotkey`, and `delay_ms`. Whole-batch validation precedes effects; execution
stops at the first runtime failure without replay. Responses report
`status:"ok"` or `status:"failed"`, the `completed` step count, one failure and
any material warnings.
Observation failure is separate from completed actions and also sets status to
`failed`; actions already completed remain counted.

```json
{"status":"ok","completed":2,"observation":{"id":2,"width":1600,"height":900}}
```

Step failures identify the zero-based index and stop the remaining sequence:

```json
{"status":"failed","completed":1,"failed":{"index":1,"tool":"click","code":"invalid_coordinates","hint":"Coordinates lie outside the full desktop overview."}}
```

Without any `delay_ms` step, a batch pauses 150 ms between steps and before its
final screenshot. Empty observations have no added pause. Any explicit delay,
including zero, disables all automatic pauses for that batch. Automatic pauses
remain cancellable and stop on permission loss; they do not count as steps.

`delay_ms` is an explicit step with a required integer `ms` from 0 to 30000;
the old top-level `delay_ms` argument is rejected. Place delays between actions
or at the end. The final observation occurs after all steps, including trailing
delays, complete. Delays count toward the 64-step limit and the existing
60-second request deadline, remain cancellable, and stop on permission loss.
They are fixed timers and do not check whether an application has finished.

```json
{
  "steps": [
    {"tool":"press_key","args":{"key":"Enter"}},
    {"tool":"delay_ms","args":{"ms":1500}}
  ]
}
```

This submits Enter, waits 1.5 seconds, then returns one screenshot. A delay-only
sequence waits before observing; `observe:false` suppresses that observation.
Adding delays to semantic-only sequences does not start a visual session.

An empty call captures a full overview. Foreground batches return one final
full overview automatically; `observe:false` suppresses it and an explicit
observation replaces it. Semantic-only batches do not capture automatically.
Desktop observation metadata contains `id`, `width`, `height`, and
`zoom` when requested. Width and height describe the returned image, including
zoom crops; the global input coordinate frame remains the full overview.
An explicit window observation requests an accessibility tree:

```json
{"observe":{"kind":"window","pid":123,"window_id":1,"tree_format":"elements"}}
```

Tree limits remain 250 elements / 12 levels by default, at most 2000 / 32.
Formats are `markdown`, `elements`, or `both`. Password values are omitted.
Element tokens identify semantic action targets; replacing that window's tree
invalidates its old tokens. A window without a unique PID/title/client-geometry
association to an AT-SPI window returns an explicit accessibility-window error.
Unsupported individual element rectangles are null; they never expose another
coordinate system. Semantic token actions do not need pixel coordinates.

### One coordinate system

The current full overview establishes the only public pixel coordinate frame.
**KWin window bounds, accessibility element frames, pixel input and zoom all
use that frame.** Tree observations include its capture ID. AT-SPI rectangles
are requested relative to their window and anchored to verified KWin client
geometry before conversion; raw screen/window coordinates never reach MCP.

Outputs retain KWin's logical arrangement, with negative origins normalized to
the overview top-left and gaps painted black. Public monitor selectors and
`target` defaults are removed. Input in gaps is rejected. Cross-monitor drags
preflight every route and capability, retain the button across devices on one
EIS seat, and release it on interruption. Motion/buttons/scrolling use the same
pointer device where possible; another device is used only for a split capability.

The default image cap is 1600 pixels on the long edge; explicit
`max_image_dimension:0` requests native detail within image/memory limits.
`canvas` contains only overview `width`, `height`, and `id`.
Zoom reads fresh native-detail ScreenShot2 pixels, not enlarged overview pixels:

```json
{"id":1,"observe":{"kind":"desktop","zoom":[100,200,500,450]}}
```

`zoom` is `[left,top,right,bottom]` in full-overview coordinates. It requires a
valid overview, preserves that overview's capture ID and coordinate frame, and
is not sticky. New full observations rotate the ID. KWin/output/input-mapping
replacement invalidates it. Removed selectors and invalid/stale zoom options
are rejected before effects.

After foreground input, observation attempts a bounded EIS round trip and
allows the existing 100 ms render interval before requesting fresh compositor
pixels. Capture responses carry no `frame_status` field: each image is captured
on demand. Responses also omit `effect`; application completion must be checked
independently through images or readback.

Unicode text prefers libei text, then the compositor keymap. Clipboard-paste
fallback uses Klipper and reports that both clipboard and PRIMARY text are
replaced. For a focused accessible text control, scu attempts to restore an
existing selection after Klipper takes PRIMARY ownership, checking that focus,
text and the restored selection remain stable before pasting. Failed restoration
returns `clipboard_selection_changed` without sending paste. On the tested GTK /
Klipper combination, the restored selection does not remain stable: selected
Unicode replacement fails safely, and callers should use semantic `set_value`.
Unicode insertion without a selection works. Controls without usable
accessibility selection metadata cannot have their selection preserved through
this fallback. If the requested text has no supported delivery route, the tool
fails explicitly.

### Reproducible checks and live testing

Guest runtime requirements are GLib/GIO, libei, libxkbcommon, GdkPixbuf and
libatspi. All are required for computer use; screenshot-only fallback is not
supported. scu no longer depends on PipeWire. Build/test
requirements additionally include the corresponding development headers,
libeis, a C compiler, private D-Bus support, PyGObject and Pillow.

```sh
make -C native scu check-guest-abi
PYTHONPATH=src venv/bin/python -m unittest tests.test_scu tests.test_computer_integration tests.test_native_abi -v
PYTHONPATH=src:/usr/lib64/python3.14/site-packages venv/bin/python -m unittest tests.test_session tests.test_accessibility tests.test_packaging tests.test_broker_registration tests.test_launch tests.test_tui -v
git diff --check
```

Private tests execute the actual guest and host-service binaries against fake
KWin/ScreenShot2/Klipper services and a real libeis oracle. They validate raw
image FD transfer, malformed/truncated formats, immutable capture descriptors,
geometry, native zoom, independently delivered input, Unicode clipboard
fallback, AT-SPI action/readback, cancellation, revocation and recovery. The
30-second status deadline is exercised against a deliberately held response.
Lifecycle tests cover the permission boundary and closing the service before
unmount, including failure paths. Synthetic triple-monitor tests cover negative
origins, mixed scaling, gaps and cross-device dragging.

Use only win5 and its Fedora Space for live GUI testing:

```sh
./sync.sh win5
ssh win5 sudo systemctl restart spaces@fedora.service
ssh win5f sudo dnf install at-spi2-core libei libxkbcommon gdk-pixbuf2
scp tests/manual_atspi_fixture.py win5:/tmp/scu-kwin-atspi-fixture.py
scp tests/manual_scu_probe.py win5f:/tmp/manual_scu_probe.py
```

Launch one host fixture at a time with the host's actual graphical environment:

```sh
ssh win5 'env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 nohup python3 /tmp/scu-kwin-atspi-fixture.py qt scu-kwin-qt /tmp/scu-kwin-qt-state.json --computer-use >/tmp/scu-kwin-qt.log 2>&1 </dev/null &'
ssh win5f python3 /tmp/manual_scu_probe.py scu-kwin-qt --artifacts /tmp/scu-kwin-qt --capture
ssh win5 cat /tmp/scu-kwin-qt-state.json
```

Repeat with `gtk` and a unique token/state path. Verify button/checkbox actions,
exact Unicode text, scrolling and dragging independently through fixture JSON.
Measure input directly from returned element frames or the current full image,
then inspect a native-detail crop and verify those same global points still
work. Automatic images replace capture IDs; never replay an interrupted batch.
Stop only the owned fixture process afterward. Test revocation with a held MCP
connection and a fresh process, restore the original tier, and verify recovery.

Keep SELinux enforcing and inspect AVCs. Never run `sync.sh --selinux`; policy
deployment is user-operated. No GUI testing may be substituted onto the user's
active development host.

### KWin live results, 2026-11-01

Ordinary `./sync.sh win5` RPM deployment passed with SELinux **Enforcing**; no
policy was installed and no matching AVC denials were observed. The host ran
KWin 6.7.5-1.fc45.ludos with one 1920×1080 output at scale 1.65, logical size
1164×655. Prompt-free `status` captured the 1920×1080 source into a 1600×900
overview. This exercised the installed fixed-path host executable and its
authorization metadata, rather than an unrestricted test executable. Later RPM
upgrades automatically replaced the old helper through normal reconciliation.

Both Qt and GTK host fixtures were driven from `ssh win5f`. Independent fixture
state verified semantic button/checkbox actions, exact Unicode text, pixel
clicks derived from converted element frames, scrolling and dragging. A Qt crop
`[580,290,1020,410]` returned 527×143 native-detail pixels while preserving the
overview capture ID; clicking the same global button point afterward incremented
the host counter. The final GTK probe used:

```sh
ssh win5f python3 /tmp/manual_scu_probe.py scu-kwin-gtk --artifacts /tmp/scu-kwin-gtk-final --capture --visual-actions --entry-point 639 291 --surface-point 639 419 --zoom 460 220 810 490
ssh win5 cat /tmp/scu-kwin-gtk-state.json
```

Its final independently read state was:

```json
{"clicks":3,"text":"Visual Unicode 世界 scu-kwin-gtk!","checked":false,"scrolls":2,"drags":2}
```

That probe combines semantic replacement, ASCII shortcut replacement and Unicode
insertion, avoiding the selected-text Klipper limitation described above. A
separate live selected-Unicode test returned `clipboard_selection_changed` and
left the entry text unchanged instead of appending or sending paste.

Terminating the host service disconnected an already open MCP client's input;
its next operation reported disconnection, and a later status reconnected and
captured without replaying actions. A second ordinary host UID was denied access
to the socket. A temporary downgrade to basic followed by the normal Space
restart removed access: new clients rejected both status and keyboard input
with the configuration hint, and the held client was terminated. The original
accessibility setting was restored and capture retested. Automated lifecycle
tests separately cover direct reconciliation revocation without a Space restart.

The scu/host-service/ABI suite passed **33 tests**. Affected session,
accessibility, packaging, broker, launch and TUI checks passed **210 tests, with
2 unrelated skips**. Native builds and GLIBC ≤ 2.17 / architecture checks passed.
Live cross-monitor dragging remains unverified because win5 has only one output;
synthetic triple-output capture/zoom and real libeis cross-device routing tests
cover the implementation. KWin process replacement was exercised with private
fixtures, rather than restarting the live compositor. No GUI interaction was
performed on the user's active development host.

Delay-step contract update, 2026-11-01: the **38-test** scu/lifecycle/ABI suite
passed after removing batch-level `delay_ms` and adding explicit delay steps.
Private compositor capture timestamps confirm that both inter-action and
trailing delays finish before the single final screenshot. Coverage also checks
delay-only observation, `observe:false`, semantic-only batches, whole-batch
validation, cancellation and permission revocation during a 30-second delay.
Native GLIBC ≤ 2.17 and architecture checks passed.
After ordinary `./sync.sh win5`, a fresh `ssh win5f` MCP client advertised only
`steps`, `observe`, and `id` at the top level. A single
`{"tool":"delay_ms","args":{"ms":500}}` step returned one 1600×900 PNG after
0.694 seconds. This live check observed the remote desktop without sending
input or opening applications.

The subsequent response simplification removes `frame_status` from status
canvas metadata and both overview/zoom observations. Exact-field assertions
cover these responses; all 38 scu/lifecycle/ABI tests pass. The EIS oracle also
neutralizes a disconnected client's held-input counters, matching compositor
teardown when disconnect overtakes queued release events.
After ordinary sync and restarting the Fedora Space stopped by that sync
workflow, a fresh `ssh win5f` client verified that both `status({})` and
`computer({})` return one inline PNG and coordinate metadata without
`frame_status`.

Strict readiness update: `health_check` is removed. Status returns canvas,
applications and a PNG only when both adapters work; prerequisite failures return
an error without images or partial metadata. Computer batches check all required
guest libraries and the live accessibility registry before executing steps.
All **40 scu/lifecycle/ABI tests** pass, including missing libraries, absent and
broken accessibility bridges, no-input/no-capture failures and recovery. Native
build and GLIBC ≤ 2.17 / architecture checks passed. This update was built and
tested locally before deployment.

Current deployed contract verification, 2026-11-01: ordinary `./sync.sh win5`
installed the current executable and restarted Fedora. A fresh `ssh win5f` MCP
client verified `status` returns only canvas/apps plus its PNG, with numeric
canvas ID. An empty `computer` call returned:

```json
{"completed":0,"observation":{"id":2,"width":1600,"height":900},"status":"ok"}
```

A click outside the canvas was rejected before input delivery, returning
`completed:0`, `status:"failed"`, an indexed `invalid_coordinates` error, and MCP
`isError:true`. Both responses omit the removed fields. Old contract examples
were removed from this document. The latest 40-test suite and native ABI checks
passed before deployment; live verification sent no keyboard or pointer input.

Settings reconciliation regression, 2026-11-01: atomically replacing host
`kdeglobals` now refreshes the changed settings mounts without terminating SCU,
the AT-SPI proxy, or the guest graphical session. The portal bridge is rebuilt
because its broker pins file-mapping descriptors. Changes to permission,
selected login, environment, or non-settings mounts retain full teardown.
Failed settings updates retain the actual mounted subset for the next retry;
the original source-identity and read-only mount checks still apply.

Reproduce the controller regression checks without accessing a desktop:

```sh
PYTHONPATH=src venv/bin/python -m unittest tests.test_computer_integration -v
```

All 16 lifecycle tests pass, covering settings replacement, untouched mounts,
portal mapping refresh, mount/unmount failure and recovery, a second atomic
replacement between planning and mounting, permission downgrade, session
replacement and logout. The SCU/private-backend and native ABI suites also
passed; affected session, accessibility, launch, broker and packaging checks ran
190 tests with 2 skips. `make -C native check-guest-abi` passed. This fix was
tested locally with private fixtures and mocked machine operations; it has not
been deployed or live-tested on win5. No host GUI was controlled.

Shared SCU host verification, 2026-11-01: the SCU, controller, packaging,
session, accessibility and native ABI suites ran **169 tests with 7 skips**.
Private logind, screen-locker and display helpers cover visual-only unlocking,
unlock denial and timeout, accessibility while locked, session retirement,
independent registration revocation, connection-scoped cancellation IDs and
fallback ownership. After removing the unnecessary virtual-output configuration
branch, three targeted display tests and `make -C native check-guest-abi` passed
again. The missing-`enabled` regression remains covered.

The separate live probes are reproducible with:

```sh
./sync.sh win5
venv/bin/python tests/manual_scu_session_probe.py headless --artifacts .build/scu-win5-headless
venv/bin/python tests/manual_scu_session_probe.py lock --artifacts .build/scu-win5-lock
```

Both probes passed on win5 with SELinux enforcing, using the Fedora Space.
Each probe checked that concurrent clients shared one host process.
The headless probe saves KScreen configuration,
temporarily forces the built-in DRM connector off and triggers a card change.
KWin refuses disabling its last output through KScreen configuration, so the
connector override exercises actual output loss. One scale-1 1280x720 fallback
appeared; ScreenShot2 captured it and EIS text matched the fixture's independently
read JSON. The fallback survived one of two clients disconnecting, disappeared
after the final client, returned for a new client and disappeared when the
physical output returned. Its helper had no listening VNC socket.

The lock probe ran after restoring the panel. It kept eDP-1 enabled and powered
on, left the lock screen visible for five seconds, and confirmed an accessibility
button action changed the fixture while logind remained locked. A screenshot
then unlocked the session. A separate lock and five-second visible wait preceded
an EIS text action, which unlocked and delivered the exact expected fixture text.
Each artifact directory contains PNG captures, response metadata, independent
fixture state, output configurations and timestamped lock/output transitions.
The probes restore the original display configuration, unlock the session and
remove their owned fixtures in cleanup. Recent AVC checks reported no matches;
no SELinux policy was deployed or changed.

Socket-demand activation update, 2026-11-01: granting computer-use access now
prepares the private socket without starting the engine. The existing login
monitor wakes on socket readiness, registers a pending listener and lets the
systemd control socket activate the host. The host returns idle registrations
after two seconds, retaining no SCU process when clients stop using it. The
controller keeps the same listening descriptor and mounted inode for subsequent
activation. No extra watcher process or per-client engine is involved.

The affected SCU, controller, packaging, session, accessibility, ABI and launch
suites ran **263 tests with 7 skips**. Two focused tests additionally verified
socket wakeups and retry after a failed activation. After adding the idle grace
period, the **64-test SCU/native ABI suite** and
`make -C native check-guest-abi` passed again. The lifecycle fixture verifies two
Spaces share one host, one Space's idle lease leaves the other's client usable,
the last client stops the host, a new connection reuses the mounted socket and
explicit revocation still disconnects its client.

Ordinary `./sync.sh win5` deployed the update. Before use, the Fedora Space was
active while SCU reported `ActiveState=inactive` and `MainPID=0`. The separate
headless and visible-lock probes passed again, saving artifacts under
`.build/scu-win5-demand-headless` and `.build/scu-win5-demand-lock`. During the
headless probe, host PID 79414 served both clients; closing the final client
stopped it and removed the fallback. The next connection activated PID 80604
through the existing socket and recreated the fallback. Physical-output return
removed it again. The lock probe independently verified the accessibility
button while locked, screenshot unlocking and EIS unlocking after separate
five-second visible lock periods, with eDP-1 enabled and powered on.

Both saved before/after display configurations match: eDP-1 enabled, mode 1,
scale 1.65, position 0,0. Final checks show the session unlocked, SCU inactive
with `MainPID=0`, SELinux enforcing and no recent AVC matches. Owned fixtures
were removed. The PNGs, fixture JSON, output snapshots and timestamped events
remain in the artifact directories; the AVC query is recorded in
`.build/scu-win5-demand-avc.log`.

### Browser accessibility activation

SCU actively registers one `object:children-changed` consumer in its shared
AT-SPI helper on the first accessibility operation. Preparing a dormant socket,
MCP initialization and schema discovery do not register a consumer. Active
clients share the registration; revoking one Space retains it while another
client remains. The helper is discarded when the last client disconnects, and
the common host retains its existing two-second idle grace period. This differs
from passive accessibility-tier bus forwarding described above: on AT-SPI 2.62,
consumer registration lets the host bus launcher activate `IsEnabled`. SCU does
not directly write that property or GSettings.

Before a window snapshot, SCU requests the selected frame's attributes through
the standard AT-SPI API. Chromium uses this query as a signal to enable its full
tree. SCU dispatches events and refreshes the frame cache for up to two seconds
if no readable children exist, including nonzero counts with null child objects.
Unsupported attribute queries are nonfatal. Snapshot tokens are allocated after
preparation, and normal cancellation, window matching and geometry checks apply.

The private-bus tests cover consumer reuse, idle event dispatch, explicit
deregistration on helper EOF, owner-disconnect cleanup, independent Space
revocation, helper reactivation, initialization recovery, delayed children,
unsupported attributes, empty frames and cancellation during preparation:

```sh
PYTHONPATH=src venv/bin/python -m unittest \
  tests.test_scu tests.test_computer_integration tests.test_launch tests.test_native_abi -v
make -C native check-guest-abi
```

Run the live browser diagnostic with an active graphical desktop and the
Fedora Space configured for computer use. It starts two isolated browser
profiles, one before and one after consumer activation, and records full SCU
observations plus independent host activation/registry/service state. It never
uses `ACCESSIBILITY_ENABLED` or force-renderer flags, and removes only its own
browser instances and profiles. The initial state must show no other active
SCU client to establish cold activation and last-client cleanup.

```sh
./sync.sh win5
venv/bin/python tests/manual_scu_browser_probe.py \
  --artifacts .build/scu-win5-browser-baseline
```

If automatic activation fails, compare the same launch with temporary access
to the session-bus activation service. This argument affects only the test
launches; it does not install a Flatpak override:

```sh
venv/bin/python tests/manual_scu_browser_probe.py --talk-a11y \
  --artifacts .build/scu-win5-browser-permission
```

Live validation on 2026-11-01, win5/win5f, SELinux enforcing: ordinary sync
deployed the SCU changes without deploying SELinux policy. Consumer registration
changed `IsEnabled` from false to true. With the existing browser declaration,
the dedicated AT-SPI bus and `GetRegisteredEvents` worked, but the sandbox's
`toolkit-accessibility` remained false and its session-bus `IsEnabled` query
returned `ServiceUnknown`. Neither browser launch exposed a tree. Those results
are recorded in `.build/scu-win5-browser-activation`.

Adding only the temporary `--talk-name=org.a11y.Bus` permission restored the
activation query. The browser launched after SCU activation exposed 202 elements,
including the synthetic page's heading, button and input. The browser launched
while activation was false remained unavailable: restart that browser while a
SCU consumer is active. A second client retained the same registration and page
tree after the first disconnected. Closing the last client removed the
registration and stopped the host; a fresh client started another host and read
the same page controls. Final checks showed `IsEnabled=false`, no registered
events and `MainPID=0`. The reproducible probe passed again with artifacts in
`.build/scu-win5-browser-final`; audit checks found no recent AVC denials.

This comparison justifies adding `--talk-name=org.a11y.Bus` to Anatase Browser's
Flatpak declaration. The declaration must be rebuilt/deployed to ship that
permission; live validation used its equivalent temporary launch argument.
Browser semantic text replacement is not covered by this change.
