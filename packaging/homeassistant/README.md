# Home Assistant add-on

`kopialens/` is the add-on folder exactly as it goes into the public repository `AnshulJ999/homeassistant-addons` at the public release. Until then it lives only here; never push it there early.

- Image: `ghcr.io/anshulj999/kopialens-ha-{arch}` (amd64, aarch64), built by the release pipeline (`.github/workflows/kopialens-release.yml`, HA part) from the release image `ghcr.io/anshulj999/kopialens`. Private on GHCR until release.
- `config.yaml` `version` is stamped by the pipeline from the release tag.

## Testing on HAOS as a local add-on

1. Copy the `kopialens/` folder into HAOS's `/addons/` share (Samba share `addons`).
2. The image is private until release, so HAOS needs a GHCR login (a token with `read:packages`) through the Supervisor's Docker registry settings (exact steps not checked yet). Then either:
   - **Prebuilt image:** keep `image:` in `config.yaml`; or
   - **Build on HAOS:** delete the `image:` line from the copy; HAOS builds `Dockerfile` from `build.yaml`'s `build_from`.
3. Settings > Add-ons > Add-on store > ⋮ > Check for updates; KopiaLens shows under "Local add-ons".

## What to check there

- Opens in the sidebar (ingress) with no sign-in page; the URL carries `/api/hassio_ingress/...` and every screen loads.
- `/share`, `/media`, `/backup` writable, `/homeassistant` read-only.
- A new connection gets the identity from the options (`kopia_hostname`, `kopia_username`).
- Snapshot mounting off: the mount action says it is off; on (after a restart): a mount works.
- A Home Assistant backup leaves out `repo*/`, `cache/`, `kopia-cache/`, `logs/` but keeps `configs/`, `settings.json`, `repos.json`, `users.json`.
- The direct port, when set, asks for KopiaLens's own sign-in.
