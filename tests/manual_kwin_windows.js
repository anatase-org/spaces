// Manual diagnostic helper: load through org.kde.KWin /Scripting, then read
// the KWin journal to inspect application identity and supported operations.
for (const window of workspace.windowList()) {
    const report = JSON.stringify({
        caption: window.caption,
        desktopFileName: window.desktopFileName,
        resourceClass: window.resourceClass,
        resourceName: window.resourceName,
        pid: window.pid,
        closeable: window.closeable,
        minimizable: window.minimizable,
        maximizable: window.maximizable,
        fullScreenable: window.fullScreenable,
        resizeable: window.resizeable,
    });
    print(report);
    callDBus(
        "org.anatase.Spaces.WindowProbe",
        "/org/anatase/Spaces/WindowProbe",
        "org.anatase.Spaces.WindowProbe1",
        "Report",
        report,
    );
}
