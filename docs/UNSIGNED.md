# Running System Graft — Gatekeeper, SmartScreen and firewalls

macOS builds are signed and notarised, so they just open. The Windows
builds are unsigned and SmartScreen will object once. This page covers
that, the firewall prompts, and how to verify a download.

## Why the Windows builds are unsigned

macOS signing is covered: this project carries an Apple Developer Program
membership and a *Developer ID Application* certificate, and every macOS
artefact is notarised by Apple.

Windows is not. An Authenticode certificate (OV, or EV to skip building
SmartScreen reputation) runs ~$200-500/year, and the certificate authorities
will only issue one to a registered legal entity — which this project is not.
Nothing is wrong with the Windows downloads; Windows simply has no publisher
identity to check them against, so it assumes the worst once and then
remembers your answer.

> If you'd rather not trust a stranger's binary at all, every release is reproducible
> from source — see the build instructions in the README.

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

## Signing it yourself

