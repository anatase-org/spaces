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
computer use**. Both Basic/application and Develop presets use basic
integration, including existing Develop spaces on their next start.
Accessibility and Agent computer use require an explicit Customise selection.
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

`scu mcp` runs inside the guest, through the existing portal
and accessibility forwarding. Spaces changes for this executable are limited
to its native bundle and command exposure. There is no added status mount or
host computer-control service. The accessibility socket gates every desktop
operation, including each batch step. An absent socket returns
`accessibility_permission_required` with `spaces configure <space>`; an
unusable forwarded registry reports `accessibility_unavailable`. Because there
is no separate permission record, absence cannot distinguish a disabled tier
from integration that has not started. `health_report` records that distinction
as `forwarded` versus actual registry `ready` and reports missing libraries.

### Action and observation contract

Read `get_desktop_state` (or a semantic window tree) before acting. Use
`run_steps` for a known sequence. A foreground batch returns counts, one final
PNG image block and observation metadata; it does not return successful step
dumps or capture between steps. For example, with a browser already focused:

```json
{"steps":[{"hotkey":{"keys":["ctrl","n"]}},{"hotkey":{"keys":["ctrl","l"]}},{"type_text":{"text":"https://example.com"}},{"press_key":{"key":"Enter"}}]}
```

`observe:false` disables the final image. `observe:{"kind":"desktop", ...}`
selects a final monitor and optional `max_image_dimension`;
`observe:{"kind":"window","pid":123,"window_id":1}` requests a tree instead.
Legacy tree objects without `kind` remain accepted, and can inherit the last
explicitly targeted window. Pure semantic batches have no implicit screenshot
or visual consent request. All structure, defaults and observation options are
validated before effects. Runtime errors stop the remaining steps without
retries; `failed` identifies the zero-based index, tool, code and hint. A separate
`observation_error` preserves the completed/skipped counts when readback fails.
Clipboard fallback reports `clipboard_paste` and warns that clipboard text was
replaced. Batch warnings retain the relevant step index.

The first granted monitor is selected initially (`primary` is its alias).
Explicit monitor observations or desktop targets change the selected monitor
for subsequent implicit operations, including keyboard-only final observation.
This does not move keyboard focus. Each image uses its own local coordinates,
scaling transform and capture ID; monitors are never stitched. Desktop metadata
lists available IDs and dimensions. Batch `target` and `capture_id` can supply
pixel defaults, while explicit step targets override them and semantic tokens
keep their own targeting. A capture default is not inherited across displays.
Every new image supersedes the previous capture token for that monitor.

Automatic images have a 1600-pixel long-edge cap; explicit observations retain
their requested size (omitted/zero means full resolution). After input, capture
attempts an EIS round trip, drains queued frames, allows a 100 ms render interval
and waits at most one second for a subsequent frame. `frame_status` is `new`,
`cached`, or `unknown` when input synchronization is unavailable. Frame receipt
is tracked separately from display geometry, so ordinary new frames do not
invalidate coordinate mappings. New pixels are not proof of application
completion: action batches remain `effect:"unverifiable"`. Inspect the inline
image; if navigation is still loading, observe again without replaying actions.

Build and run the isolated tests from a Fedora development Space:

```sh
sudo dnf install gcc pkgconf-pkg-config glib2-devel at-spi2-core-devel \
  libei-devel pipewire-devel libxkbcommon-devel gdk-pixbuf2-devel \
  dbus-daemon python3-gobject
make -C native check-guest-abi
PYTHONPATH=src venv/bin/python -m unittest tests.test_scu -v
```

`test_scu.py` compiles the actual executable and uses private D-Bus endpoints.
`scu_atspi_fixture.py` exposes a registry and an application on separate
connections. State is written independently by the fixture, so successful tool
submission alone cannot pass the action checks. Coverage includes semantic
insertion versus replacement, Unicode, password suppression, token expiry,
whole-batch validation before effects, first-failure stopping, recovery after
bus replacement, and revocation with a running MCP process.
`scu_portal_fixture.py` checks permission rejection before any portal call,
consent refusal, and closing an outstanding request on cancellation. Portal
fixtures run with graphical/session variables absent, and with deliberately
stale bus/runtime variables, to verify direct guest runtime discovery. Invalid,
oversized and Unicode JSON, both MCP eras, missing optional libraries and clean
EOF shutdown are also covered.
The same short workflow instructions are advertised by legacy initialization
and modern discovery. Nested batch schemas cover named and shorthand actions,
explicit observation and opt-out. Tests also verify compact success/failure
responses, semantic-only batches, preflight rejection of invalid final
observations/background input, and observation failures after successful effects.

`scu_backend_fixture.c` includes the production translation unit and drives a
real libei client against a private libeis peer. It checks delivered pointer,
scroll, drag and keyboard events and release of held input during cancellation.
Separate cases feed CPU video buffers through the real frame conversion and PNG
encoding code, validate truncation rejection, monitor pairing, negative origins,
mixed scaling, resized-image coordinates, stale captures, ambiguous mappings,
and private rotating restore-token files. These buffer/geometry cases are
isolated fixtures, not an end-to-end multi-monitor PipeWire compositor test.
The EIS case additionally verifies final PNG MCP content, persistent monitor
selection, shared pixel defaults, partial batch completion, delayed/new versus
queued/cached frames, missing round-trip support, resize invalidation,
cancellation and accessibility revocation during observation. Refused visual
grants must not trigger another prompt for automatic batch observation.

For live testing on `win5`, deploy without modifying SELinux policy:

```sh
./sync.sh win5
ssh win5f sudo dnf install at-spi2-core libei pipewire-libs libxkbcommon gdk-pixbuf2
```

Enable accessibility on the host with `sudo spaces configure fedora`, then
restart the Space. A configuration error must leave the setting user-controlled.
Direct machinectl/systemd-run commands must always use sudo. Keep SELinux
enforcing; inspect AVCs with `ssh win5 sudo ausearch -m AVC,USER_AVC -ts recent -i`.
Never use `sync.sh --selinux`; deployment of policy is user-operated.

From the host graphical session, start one fixture at a time:

```sh
python3 tests/manual_atspi_fixture.py gtk scu-live-gtk /tmp/scu-gtk-state.json --computer-use
python3 tests/manual_atspi_fixture.py qt scu-live-qt /tmp/scu-qt-state.json --computer-use
```

GTK needs PyGObject/GTK 3 and Qt needs PySide6. For a Qt fixture that otherwise
omits its accessibility tree, launch that fixture with
`QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`. The test does not change host accessibility
settings. Copy `tests/manual_scu_probe.py` into the guest if the checkout is not
shared, then run it there:

```sh
python3 tests/manual_scu_probe.py scu-live-gtk --artifacts /tmp/scu-gtk --capture
```

Inspect the returned PNG, then repeat with the entry and scroll/drag surface
centers measured in that image:

```sh
python3 tests/manual_scu_probe.py scu-live-gtk --artifacts /tmp/scu-gtk-actions \
  --capture --visual-actions --entry-point 639 290 --surface-point 639 420
```

Coordinates above are the recorded GTK fixture positions on win5, not portable
constants. The Qt fixture used entry `639 288`, surface `639 406`. Update these
from a fresh image if the window moves. Wayland AT-SPI frames can be local to a
window; never use them as global screenshot coordinates. If capture is black,
check whether the desktop is locked. A reported `unverifiable` action is not
proof of delivery.

Read the **host** state file independently after each run. Expect a button count
increment, checkbox toggle, text exactly `Visual Unicode 世界 scu-live-gtk!`
(or `scu-live-qt!`), and positive `scrolls`/`drags`. Semantic-only runs write
`Héllo 世界 <token>`. Foreground batches also return a final image; use its new
capture token for subsequent pixel actions. Clipboard fallback replaces regular clipboard text and
uses Ctrl+V; Shift+Insert can address PRIMARY on Linux.

For recovery, hold one MCP process open, capture, restart the host's
`plasma-xdg-desktop-portal-kde.service`, and make another observation. It should
report `visual_session_closed`. A subsequent request may restore a fresh
session and capture; no interrupted action may replay. For revocation, downgrade
the Space, apply the existing restart flow, and launch a fresh client. It must
return the configuration instruction without a consent prompt. Restore the
original tier after the test. The isolated socket-replacement tests additionally
exercise revocation while the MCP transport remains open.

Live results on 2026-10-31: Fedora 44 guest on win5's KDE/Wayland host, SELinux
enforcing. Both GTK and Qt fixtures confirmed semantic button/checkbox actions,
Unicode insertion/replacement, Ctrl+A, Unicode clipboard paste plus mapped-key
insertion, PNG monitor observation, scrolling and dragging through independent
JSON state. The monitor was 1920x1080 at 1.65 scale, captured at 1280x720; EIS
coordinate conversion delivered to the correct widgets. Restarting the portal
invalidated the session and a fresh request restored capture. AVC inspection
reported no matches. Only one monitor was granted on this host, so live
multi-monitor/hotplug acceptance remains unverified. Automated geometry tests
cover mixed scaling and negative origins; they do not replace that live test.

The affected configuration/TUI, authentication, launch, session, accessibility,
native-ABI and packaging regression run completed with 256 tests, 7 explicit
skips. The scu suite completed with 12 tests, including a real EIS peer. Native
build/GLIBC <= 2.17 checks and the ordinary RPM sync/install passed. A freshly
started client after a live downgrade to `basic` returned the correct
`spaces configure fedora` instruction; accessibility was restored afterward.
Configuration hints use `spaces configure <space>`; scu does not discover or
publish the Space name. Consent tokens are scoped by guest machine ID and UID.

Response/observation update, 2026-11-01: the 12-test scu suite passes with the
expanded batch, monitor-selection, real EIS, PNG and freshness checks above.
The affected launch/session/packaging/native-ABI regression run passed 165 tests
with two skips; `make -C native check-guest-abi` passed the GLIBC <= 2.17 and
architecture checks. `tests/manual_scu_probe.py` now consumes each foreground
batch's final observation token before sending subsequent pixel actions.

Current-host acceptance, 2026-11-01: the initial attempts initialized and reported
`forwarded:true`, `ready:true` with all optional libraries present, but timed out
waiting for the host portal response. After the user requested another attempt,
a fresh `native/scu mcp` process captured successfully. Two monitors were granted:
2560x1600 with logical bounds 1707x1067 at (3840,1093), and 3840x2160 with logical
bounds 3840x2160 at (0,0). The first image was returned inline at 1600x1000.

From that image, one `run_steps` batch used desktop target `display-1`, its
capture ID, and `delay_ms:150`: click the visible Chrome address bar at (420,58),
Ctrl+N, Ctrl+L, type `https://example.com`, Enter. The batch took 1120 ms and
returned `completed:5`, `skipped:0`, `ok:true`, `effect:"unverifiable"`, and one
automatic 1600x1000 PNG with `frame_status:"new"`. Visual inspection of that
same response confirmed a new Chrome window with one Example Domain tab,
`example.com` in its address bar, and the loaded page. No additional observation
or action replay was needed. Live pointer actions were exercised on display-1;
this run does not establish cross-monitor input/hotplug behavior. The temporary
MCP process was closed cleanly afterward. No SELinux policy was deployed.
