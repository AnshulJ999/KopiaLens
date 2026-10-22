# Remote access and HTTPS

These settings decide who can open KopiaLens and whether the connection is encrypted. Find them in **Settings → Remote access**. KopiaLens reads them when it starts, so restart it after a change: **Restart** in the tray menu, or restart the container.

## Who can open KopiaLens

| Choice | Who gets in | Default for |
|---|---|---|
| **This window only** | Only the KopiaLens desktop app's own window. Browsers, even on the same computer, are turned away. | - |
| **This computer** | Any browser on the computer KopiaLens runs on. | The desktop app |
| **These addresses** | This computer, plus the addresses and networks you list. | - |
| **Anyone on the network** | Anyone who can reach the port. | Docker and server installs |

For **These addresses**, enter one address or network per line, for example `192.168.1.20` or `192.168.1.0/24`. The computer running KopiaLens is always allowed.

Whenever other devices can reach KopiaLens, turn on sign-in as well. See [Sign-in and your account](sign-in.md).

**This window only** is for the desktop app. Its tray menu then has no **Open in browser** item.

If you change the choice from a browser on another computer to **This computer** or **This window only**, that browser can't reach KopiaLens after the restart. The Settings page warns you.

## HTTPS

HTTPS encrypts the connection between your browser and KopiaLens. It matters when you reach KopiaLens over a network, because the sign-in password and your file names travel over that connection.

There are three ways to get it:

1. **Certificate made by KopiaLens**: the default for Docker and server installs. KopiaLens makes its own certificate on the first start and renews it before it expires. Browsers warn about it once, because no public authority signed it. Check the fingerprint shown in **Settings → Remote access**, then accept it.
2. **Your own certificate files**: choose **My own certificate files** and enter the paths to the certificate (a full-chain PEM file) and its private key. Use this when you have a certificate for your own domain.
3. **A reverse proxy**: Caddy, nginx, Traefik or similar handle HTTPS, and pass requests on to KopiaLens over plain HTTP. See below.

The desktop app serves plain HTTP, because its tray talks to it on the same computer. To reach a desktop install from other devices over HTTPS, use a reverse proxy.

The certificate KopiaLens makes is stored in the data folder under `tls/`.

## Behind a reverse proxy

A reverse proxy receives the browser's request and forwards it to KopiaLens. Two settings make that work well:

- **Trusted proxy addresses**: the proxy's own address. KopiaLens then believes the visitor's real address and protocol that the proxy passes on (`X-Forwarded-For`, `X-Forwarded-Proto`). Sign-in limits then apply per visitor rather than to everyone behind the proxy, and sign-in cookies are marked secure.
- **Hostnames**: the name you reach KopiaLens by, for example `kopia.example.com`. KopiaLens refuses requests for names it doesn't know, which protects against a class of attack called DNS rebinding. Addresses and `localhost` always work.

Example for Caddy, with KopiaLens on `192.168.1.10`:

```
kopia.example.com {
    reverse_proxy 192.168.1.10:7123
}
```

Example for nginx:

```
server {
    server_name kopia.example.com;
    listen 443 ssl;
    # ssl_certificate and ssl_certificate_key as for any nginx site
    location / {
        proxy_pass http://192.168.1.10:7123;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

In both cases, add the proxy's address under **Trusted proxy addresses** and `kopia.example.com` under **Hostnames**. When a proxy handles HTTPS, set KopiaLens's own HTTPS to **Off**.

### Under a sub-path

KopiaLens can also run under a path on a shared address, for example `https://home.example.com/kopialens/`. Two things are needed:

1. The proxy removes the path before it forwards the request. KopiaLens itself always receives `/`.
2. KopiaLens knows the path, so the links it hands the browser carry it. Set the environment variable `KOPIALENS_BASE_PATH=/kopialens`, or add `"base_path": "/kopialens"` to the `kopialens` section of `settings.json` in the data folder. There is no control for it in Settings. The environment variable wins when both are set, and it applies after a restart.

The path may hold only letters, digits and `.`, `_`, `~`, `-`, with `/` between parts. KopiaLens ignores any other value and writes a warning to its log.

Example for Caddy (`handle_path` removes the path):

```
home.example.com {
    handle_path /kopialens/* {
        reverse_proxy 192.168.1.10:7123
    }
}
```

Example for nginx (the `/` at the end of `proxy_pass` removes the path):

```
location /kopialens/ {
    proxy_pass http://192.168.1.10:7123/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

The Home Assistant add-on sets its path on its own ([Home Assistant](home-assistant.md)).

## Settings from the environment

Docker and server installs can set these without the Settings page. An environment variable wins over the setting.

| Variable | Example |
|---|---|
| `KOPIALENS_ACCESS` | `window`, `computer`, `addresses` or `network` |
| `KOPIALENS_ACCESS_ADDRESSES` | `192.168.1.0/24,10.0.0.7` |
| `KOPIALENS_HTTPS` | `auto`, `files` or `off` |
| `KOPIALENS_HTTPS_CERT`, `KOPIALENS_HTTPS_KEY` | `/certs/fullchain.pem`, `/certs/privkey.pem` |
| `KOPIALENS_TRUSTED_PROXIES` | `192.168.1.5` |
| `KOPIALENS_ALLOWED_HOSTS` | `kopia.example.com` |
| `KOPIALENS_BASE_PATH` | `/kopialens` (a sub-path behind a reverse proxy) |
| `KOPIALENS_BIND_HOST` | `0.0.0.0` (the network address KopiaLens listens on. It overrides what the access choice picks) |
