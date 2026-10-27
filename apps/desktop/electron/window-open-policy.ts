interface WindowOpenDetails {
  url: string
}

interface WindowOpenPolicyTarget {
  setWindowOpenHandler: (handler: (details: WindowOpenDetails) => { action: 'deny' }) => void
}

/**
 * Make a webContents popup-safe even when its document contains untrusted
 * iframes. Electron's iframe sandbox is not a sufficient popup boundary on
 * affected releases (GHSA-9f4c-93c8-jc8g), so every path ends in an explicit
 * main-process deny. Trusted app windows may still forward the URL to the OS
 * browser through `onDenied`; no child BrowserWindow is ever created.
 */
export function installWindowOpenDeny(target: WindowOpenPolicyTarget, onDenied?: (url: string) => void): void {
  target.setWindowOpenHandler(details => {
    try {
      onDenied?.(details.url)
    } catch {
      // Forwarding is best-effort. A consumer failure must never interrupt the
      // main-process policy before Electron receives the explicit denial.
    }

    return { action: 'deny' }
  })
}

/** Default-deny newly created BrowserWindow and <webview> webContents. */
export function denyPopupsForCreatedWebContents(_event: unknown, contents: WindowOpenPolicyTarget): void {
  installWindowOpenDeny(contents)
}
