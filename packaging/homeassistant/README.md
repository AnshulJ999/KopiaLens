# Home Assistant add-on

`kopialens/` is the KopiaLens add-on as published in [AnshulJ999/homeassistant-addons](https://github.com/AnshulJ999/homeassistant-addons). To install it, add that repository to Home Assistant's add-on store; the user guide is [Home Assistant](../../docs/home-assistant.md).

- Image: `ghcr.io/anshulj999/kopialens-ha-{arch}` (amd64, aarch64), built by the release pipeline from the release image `ghcr.io/anshulj999/kopialens`.
- `config.yaml` `version` is stamped by the release pipeline from the release tag.

## Testing a change on HAOS as a local add-on

1. Copy the `kopialens/` folder into HAOS's `/addons/` share (Samba share `addons`).
2. Either keep `image:` in `config.yaml` to use the published image, or delete the `image:` line from the copy so HAOS builds `Dockerfile` from `build.yaml`'s `build_from`.
3. Settings > Add-ons > Add-on store > ⋮ > Check for updates; KopiaLens shows under "Local add-ons".

## What to check there

- Opens in the sidebar (ingress) with no sign-in page; the URL carries `/api/hassio_ingress/...` and every screen loads.
- `/share`, `/media`, `/backup` writable, `/homeassistant` read-only.
- A new connection gets the identity from the options (`kopia_hostname`, `kopia_username`).
- Snapshot mounting off: the mount action says it is off; on (after a restart): a mount works.
- A Home Assistant backup leaves out `repo*/`, `cache/`, `kopia-cache/`, `logs/` but keeps `configs/`, `settings.json`, `repos.json`, `users.json`.
- The direct port, when set, asks for KopiaLens's own sign-in.
