# Running System Graft — Gatekeeper and Linux packages

macOS builds are signed and notarised, so they just open. There's no Windows
build to run: Windows isn't supported for writing, and the patch path runs
there from source under MSYS2 (see [Platform support](../README.md#platform-support)).
This page covers checking the macOS signature and installing the Linux
packages, which are unsigned.

## macOS — nothing to do

Every macOS artefact is Developer ID-signed, notarised by Apple and stapled,
so it opens on a double-click with no warning and no quarantine step. That
covers the nested helper binaries inside the bundle too, which is what the
old right-click-Open workaround never did.

To confirm it for yourself:

```sh
spctl -a -vv -t install "/Applications/<app>.app"
# accepted / source=Notarized Developer ID
```

## Linux

No signing gate. Make the binary executable if you took the tarball:

```sh
chmod +x ./system-graft
```

The `.deb` and `.rpm` packages are unsigned too, so your package manager may object:
`sudo dpkg -i <file>.deb` or `sudo rpm -i --nosignature <file>.rpm`.
