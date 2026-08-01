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
| `org.freedesktop.portal.Wallpaper` | `dialogs`: `SetWallpaperFile` with a guest image FD | The broker stages the bounded file and invokes the host portal. Acceptance changes the host wallpaper; cancellation is relayed and staged data is cleaned. |

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
| `org.freedesktop.ScreenSaver` | Static: `GetActive`; automated tests exercise both standard paths, inhibition cookies, signals, and disconnect cleanup | State matches the host. Each caller owns its cookies; disconnecting it releases its outstanding host inhibition. |
| `org.freedesktop.FileManager1` | Call `ShowFolders` or `ShowItems` with an existing guest `file:` URI; automated mapping tests cover rejection cases | The host file manager opens the broker-mapped host path. Unmapped, escaping, or invalid guest paths are rejected. |
| `org.kde.StatusNotifierWatcher` / `org.kde.StatusNotifierItem` / `com.canonical.dbusmenu` | Run `manual_bridge_fixture.py`, then inspect and invoke the dynamically named host mirror | The item registers under a readable, collision-free `org.kde.StatusNotifierItem.spaces.space.<space>-<app>.item.i<n>` name. Properties, menu layout, calls, and signals proxy both ways. Stopping the fixture removes the mirror. |
| `org.mpris.MediaPlayer2.*` | Run `manual_bridge_fixture.py`, then inspect root, Player, TrackList, and Playlists on its host mirror | A readable, collision-free `org.mpris.MediaPlayer2.spaces.space.<space>-<app>.player.i<n>` name appears. Reads, writable properties, methods, and signals proxy both ways. Local artwork becomes a bounded host cache URI and is removed on owner loss. Players that return empty introspection XML use the generic MPRIS schema. |

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

The existing `org.freedesktop.secrets`, `org.kde.secretservicecompat`, and
`org.kde.kwalletd5` activation names are not public portal-router interfaces.
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
wallet.  An explicit KWallet `isOpen=false` backs up the managed `.kwl`,
`.salt`, and `_attributes.json` files with the first free shared numeric suffix
before recreating the wallet.  A D-Bus timeout kills the provider and leaves
the wallet files untouched.  Test the timeout branch with a managed
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
