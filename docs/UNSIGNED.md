# Running unsigned System Graft builds

Everything this project releases is built and published unsigned. This page explains
what each operating system will say, exactly how to get past it, and how to sign the
artifacts yourself if you'd rather not click through warnings.

## Why they're unsigned

Clearing these warnings for redistribution costs money, not effort:

| Platform | What's needed | Cost |
|---|---|---|
| macOS | Apple Developer Program + a *Developer ID Application* certificate, plus notarization of every build | $99/year |
| Windows | An Authenticode code-signing certificate (OV, or EV to skip SmartScreen reputation-building) | ~$200–500/year |

This project carries neither. Nothing is wrong with the downloads — the OS simply has
no publisher identity to check them against, so it assumes the worst once and then
remembers your answer.

> If you'd rather not trust a stranger's binary at all, every release is reproducible
> from source — see the build instructions in the README.

## macOS — Gatekeeper

macOS quarantines anything downloaded from a browser, then refuses to launch it because
**"the developer cannot be verified"**. Any one of these clears it:

**1. Right-click → Open** (simplest, per-app, once)

Right-click (or Control-click) the app → **Open** → **Open** in the dialog. The plain
double-click won't offer this — it has to be the context menu.

**2. Clear the quarantine flag** (scriptable, good for deployment)

```sh
xattr -dr com.apple.quarantine "/Applications/System Graft.app"
```

**3. Approve it after the fact**

Launch it once, let macOS block it, then go to **System Settings → Privacy & Security**
and click **Open Anyway** next to the message about the blocked app.

### The CLI build

The command-line archive isn't an app bundle, so there's no right-click → Open. Extract
it, then:

```sh
xattr -dr com.apple.quarantine ./system-graft
chmod +x ./system-graft
./system-graft --help
```

### `.pkg` installers

A `.pkg` from an unidentified developer is blocked the same way. Right-click → **Open**,
or clear the flag on the installer itself before running it:

```sh
xattr -dr com.apple.quarantine ~/Downloads/<file>.pkg
```

### Apple Silicon and the `.zip` trap

If you copy an app out of a `.zip` with Finder the quarantine flag comes with it. Prefer the
`.dmg` or `.pkg`, or run the `xattr` command above after copying.

## Linux

No signing gate. Make the binary executable if you took the tarball:

```sh
chmod +x ./system-graft
```

The `.deb` and `.rpm` packages are unsigned too, so your package manager may object:
`sudo dpkg -i <file>.deb` or `sudo rpm -i --nosignature <file>.rpm`.

## Signing it yourself

### macOS — ad-hoc (local machine only)

An ad-hoc signature stops the OS re-prompting on **your own machine**. It is **not**
notarization and will do nothing for anyone else:

```sh
codesign --force --deep --sign - "/Applications/System Graft.app"
```

Verify it took:

```sh
codesign -dv --verbose=4 "/Applications/System Graft.app"
spctl -a -vv "/Applications/System Graft.app"   # still reports "rejected" — ad-hoc is not notarization
```

### macOS — real signing and notarization

With an Apple Developer Program membership and a *Developer ID Application* certificate:

```sh
codesign --force --deep --options runtime --timestamp \
  --sign "Developer ID Application: Your Name (TEAMID)" "<artifact>"
xcrun notarytool submit "<artifact>.zip" --apple-id you@example.com \
  --team-id TEAMID --password "app-specific-password" --wait
xcrun stapler staple "<artifact>"
```

Note the **hardened runtime** (`--options runtime`) — notarization rejects builds without
it, and a hardened build with an ad-hoc signature won't launch at all.

## Verifying a download

Signing proves *who* built it; a checksum proves you got *what they built* — worth doing
even unsigned. Compare against the release notes:

```sh
shasum -a 256 <file>        # macOS / Linux
certutil -hashfile <file> SHA256   # Windows
```

You can also confirm the artifact came from this repo's CI by checking the release page it
was downloaded from — GitHub shows the workflow run that produced each asset.
