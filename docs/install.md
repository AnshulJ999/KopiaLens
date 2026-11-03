# Installing KopiaLens

KopiaLens runs on Windows, macOS and Linux, in Docker and in Home Assistant. Each download comes with kopia, so you don't need to install kopia separately. Downloads: the [releases page](https://github.com/AnshulJ999/KopiaLens/releases).

## Windows

- **Installer** (`-setup.exe`): installs for you only, or for everyone on the computer. It can add a desktop shortcut, start KopiaLens when you sign in, and add `klens` to your PATH. KopiaLens updates itself: the app offers the update when one is ready.
- **Portable** (`-portable.zip`): unzip it anywhere and run `KopiaLens.exe`. It keeps all its data in its own `data` folder. Update it by downloading the new zip.

The first run may show a "Windows protected your PC" notice, which Windows shows for new apps. Choose **More info**, then **Run anyway**.

## macOS

Download the `.dmg`, open it, and drag **KopiaLens** to **Applications**.

KopiaLens isn't notarized by Apple, so macOS blocks it the first time:

1. Open KopiaLens. macOS says it can't verify the app. Choose **Done**.
2. Open **System Settings → Privacy & Security** and scroll down to **Security**.
3. Next to "KopiaLens was blocked", choose **Open Anyway**, then confirm with your password.

You do this once. KopiaLens updates itself after that.

Mounting snapshots works without extra software. If you have macFUSE installed, KopiaLens uses it.

## Linux

Pick one:

- **AppImage**: make it executable (`chmod +x KopiaLens*.AppImage`) and run it. It updates itself. On Ubuntu 22.04 and newer, AppImages need `libfuse2` (`libfuse2t64` on 24.04).
- **Debian / Ubuntu** (`.deb`): `sudo apt install ./KopiaLens*.deb`
- **Fedora / openSUSE** (`.rpm`): `sudo dnf install ./KopiaLens*.rpm`

The `.deb` and `.rpm` installs don't update themselves: KopiaLens tells you when a new version is out, and you install the new package the same way.

KopiaLens lives in the system tray. On GNOME, the tray needs the AppIndicator extension (Ubuntu has it on by default). Mounting snapshots needs FUSE (`fuse3`).

## Docker

See [Running in Docker](docker.md).

## Home Assistant

See [Home Assistant](home-assistant.md).

## Where your data goes

KopiaLens keeps its settings and index in a data folder, separate from your backups. To find the folder for your install, see [Where does KopiaLens keep its data?](faq.md#where-does-kopialens-keep-its-data)

## Uninstall KopiaLens

Uninstalling removes the app. It doesn't touch your backups or kopia repositories, and KopiaUI and the kopia command line can still open them.

If you activated Pro with a key, first select **Settings → Licence → Remove licence…**. This frees the key's place, so you can activate it again on another computer.

- **Windows installer:** open **Settings → Apps → Installed apps**, then uninstall KopiaLens. To also remove its data, select **Also delete KopiaLens settings and index**.
- **Windows portable:** delete the KopiaLens folder. Its data is inside that folder.
- **macOS:** move **KopiaLens** from **Applications** to the Trash.
- **Linux:** delete the AppImage, or remove the package with `apt` or `dnf`.
- **Docker:** run `docker compose down`, then delete your config folder if you don't need it.
- **Home Assistant:** uninstall the add-on from its page.

The data folder stays on macOS and Linux. To remove it, delete the folder yourself: [Where does KopiaLens keep its data?](faq.md#where-does-kopialens-keep-its-data)
