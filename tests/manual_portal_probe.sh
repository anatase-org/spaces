#!/usr/bin/env bash

set -u

destination=org.freedesktop.portal.Desktop
desktop=/org/freedesktop/portal/desktop

run() {
    local label=$1
    shift
    local output status
    printf 'TEST %-30s ' "$label"
    output=$(timeout 20 gdbus call --session \
        --dest "$destination" --object-path "$desktop" \
        --method "$@" 2>&1)
    status=$?
    printf 'rc=%s %s\n' "$status" "$output"
}

run network-available org.freedesktop.portal.NetworkMonitor.GetAvailable
run network-metered org.freedesktop.portal.NetworkMonitor.GetMetered
run network-connectivity org.freedesktop.portal.NetworkMonitor.GetConnectivity
run network-status org.freedesktop.portal.NetworkMonitor.GetStatus
run network-reach org.freedesktop.portal.NetworkMonitor.CanReach example.com 443
run proxy-resolver org.freedesktop.portal.ProxyResolver.Lookup https://example.com/
run settings org.freedesktop.portal.Settings.ReadAll '[]'
run openuri-scheme org.freedesktop.portal.OpenURI.SchemeSupported https '{}'
run usb-enumerate org.freedesktop.portal.Usb.EnumerateDevices '{}'
run background-status org.freedesktop.portal.Background.SetStatus \
    "{'message': <'Spaces integration test'>}"
run notification-add org.freedesktop.portal.Notification.AddNotification \
    spaces-router-test \
    "{'title': <'Spaces router test'>, 'body': <'Portal notification relay works'>}"
run notification-remove org.freedesktop.portal.Notification.RemoveNotification \
    spaces-router-test
run camera-present org.freedesktop.DBus.Properties.Get \
    org.freedesktop.portal.Camera IsCameraPresent
run power-profile org.freedesktop.DBus.Properties.Get \
    org.freedesktop.portal.PowerProfileMonitor power-saver-enabled
run gamemode-query org.freedesktop.portal.GameMode.QueryStatus $$
run realtime-nice org.freedesktop.portal.Realtime.MakeThreadHighPriorityWithPID \
    $$ $$ 0

for interface in Account Camera Clipboard Email FileChooser GlobalShortcuts \
    Inhibit InputCapture Location Print RemoteDesktop ScreenCast Screenshot \
    Secret Usb Wallpaper DynamicLauncher; do
    run "property-$interface" org.freedesktop.DBus.Properties.GetAll \
        "org.freedesktop.portal.$interface"
done

run_service() {
    local label=$1 service=$2 path=$3
    shift 3
    local output status
    printf 'TEST %-30s ' "$label"
    output=$(timeout 20 gdbus call --session --dest "$service" \
        --object-path "$path" --method "$@" 2>&1)
    status=$?
    printf 'rc=%s %s\n' "$status" "$output"
}

run_service notifications-legacy org.freedesktop.Notifications \
    /org/freedesktop/Notifications org.freedesktop.Notifications.GetCapabilities
run_service screensaver org.freedesktop.ScreenSaver \
    /org/freedesktop/ScreenSaver org.freedesktop.ScreenSaver.GetActive
run_service power-can-suspend org.freedesktop.PowerManagement \
    /org/freedesktop/PowerManagement org.freedesktop.PowerManagement.CanSuspend
run_service power-save org.freedesktop.PowerManagement \
    /org/freedesktop/PowerManagement org.freedesktop.PowerManagement.GetPowerSaveStatus

printf 'TEST %-30s ' power-suspend-absent
output=$(gdbus call --session --dest org.freedesktop.PowerManagement \
    --object-path /org/freedesktop/PowerManagement \
    --method org.freedesktop.PowerManagement.Suspend 2>&1)
status=$?
printf 'rc=%s %s\n' "$status" "$output"
