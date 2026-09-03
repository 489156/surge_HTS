# surge mobile — Capacitor wrapper (iOS / Android)

Wraps the **read-only PWA** (built by `surge pages-export`) into native iOS and
Android app shells. The web surface is the whole product; Capacitor gives it a
native container, an app icon, offline behavior, and the store presence.

> This folder is **scaffolding**. Generating and building the native projects
> requires tools that don't run in CI (Node + Capacitor CLI, Xcode, Android
> Studio). Everything here is ready for you to run locally.

## What's already done (in this repo)

- The exported site is an **installable, offline-capable PWA** — `manifest.webmanifest`,
  a service worker (`sw.js`), maskable icons, and a first-run **disclaimer gate**.
  See `src/surge/dashboard/pwa.py` and `export.py`.
- `capacitor.config.json` + `package.json` here.

## Build & wrap (run locally)

```bash
# 0) one-time: install JS deps for the wrapper
cd mobile && npm install

# 1) build the web bundle into mobile/www (run from the REPO ROOT)
cd .. && surge pages-export --out mobile/www

# 2) create the native projects (needs Xcode / Android Studio installed)
cd mobile
npx cap add ios
npx cap add android

# 3) sync web + native, then open the IDEs to build/run/submit
npx cap sync
npx cap open ios        # → Xcode
npx cap open android    # → Android Studio
```

## Two freshness strategies (choose one)

The exported page embeds the latest calls inline, so **freshness depends on how
you serve it**:

1. **Bundled (offline-first, default `webDir: "www"`)** — the app ships a
   snapshot. Simple and fully offline, but showing *new* nightly calls needs a
   rebuild + app update. Fine for a slow-changing reference.
2. **Live (hosted PWA via `server.url`)** — point Capacitor at the hosted Pages
   URL so nightly updates appear with **no resubmission**. Add to
   `capacitor.config.json`:
   ```json
   "server": { "url": "https://<your-pages-url>", "cleartext": false }
   ```
   The service worker still serves the last view offline. ⚠️ Loading a remote
   URL as the whole app raises Apple's **4.2** scrutiny — pair it with the
   native features below.

## Apple 4.2 — native value (so it isn't "just a website")

A pure web wrapper is often rejected. This scaffold pre-declares plugins to add
real native behavior on top of the read-only view:

| Plugin | Native value |
|---|---|
| `@capacitor/network` | offline banner / retry when connectivity changes |
| `@capacitor/share` | native share sheet for a call/ledger |
| `@capacitor/haptics` | tactile feedback on interactions |
| `@capacitor/preferences` | native persistence (disclaimer ack, theme) |
| `@capacitor/app` | deep links, app lifecycle |

Add **biometric app-lock** (a community plugin) as an extra native gate if
desired. The service worker already provides offline caching.

## External prerequisites (not code — see the launch plan)

- Apple Developer **organization** account + D-U-N-S number
- Google Play Console account + **20 testers × 14 days** closed test
- **`appId`** in `capacitor.config.json` is a placeholder (`com.surgehts.app`) —
  replace with your owned reverse-domain, and confirm the **"surge" name/trademark**.
- Store assets, privacy-policy URL, and listing copy: see `docs/mobile/`.
- Regulatory positioning (유사투자자문): see the launch roadmap and
  `docs/mobile/DISCLAIMER.md`.
