# Installing KopiaLens

KopiaLens runs on Windows, macOS and Linux, and in Docker. Each download comes with kopia, so you don't need to install kopia separately. Downloads: the [releases page](https://github.com/AnshulJ999/KopiaLens/releases).

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
