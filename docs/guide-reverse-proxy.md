# Run KopiaLens behind a reverse proxy

This guide puts KopiaLens on your own address, such as `https://kopia.example.com`, behind a reverse proxy like Caddy, nginx or Traefik. The proxy handles HTTPS, and KopiaLens keeps its own sign-in. It's for server and Docker installs that you open from other devices.

## Before you start

- KopiaLens runs on a server or in Docker, and the proxy can reach it, for example at `192.168.1.10:7123`.
- You have a domain name that points at the proxy, and the proxy can get a certificate for it.
- KopiaLens has sign-in set up. Anyone who reaches the address can otherwise use it ([Sign-in and your account](sign-in.md)).

## 1. Let the proxy reach KopiaLens

In **Settings → Remote access**, set **Who can open KopiaLens** so the proxy's address is allowed: **These addresses** with the proxy's address, or **Anyone on the network** ([Remote access and HTTPS](remote-access.md#who-can-open-kopialens)). If the proxy runs on the same computer as KopiaLens, **This computer** is enough.

## 2. Tell KopiaLens about the proxy

In **Settings → Remote access**:

1. Add the proxy's own address under **Trusted proxy addresses**. KopiaLens then trusts the visitor's address and protocol that the proxy passes on.
2. Add your domain, for example `kopia.example.com`, under **Hostnames**. KopiaLens refuses requests for names it doesn't know.
3. Set KopiaLens's own **HTTPS** to **Off**. The proxy handles HTTPS.
4. Restart KopiaLens. It reads these settings when it starts.

In Docker, the same settings can come from environment variables: `KOPIALENS_TRUSTED_PROXIES`, `KOPIALENS_ALLOWED_HOSTS` and `KOPIALENS_HTTPS=off` ([Settings from the environment](remote-access.md#settings-from-the-environment)).

## 3. Set up the proxy

For Caddy, which gets the certificate on its own:

```
kopia.example.com {
    reverse_proxy 192.168.1.10:7123
}
```

For nginx, see the example in [Behind a reverse proxy](remote-access.md#behind-a-reverse-proxy). It passes the `Host`, `X-Forwarded-For` and `X-Forwarded-Proto` headers, which KopiaLens needs.

To serve KopiaLens under a path instead, such as `https://home.example.com/kopialens/`, see [Under a sub-path](remote-access.md#under-a-sub-path).

## 4. Check it

1. Open `https://kopia.example.com`. KopiaLens's sign-in page appears.
2. Sign in.
3. If KopiaLens says the address isn't allowed, check **Hostnames** in step 2.

## Keep it safe

- Keep sign-in on. The proxy makes KopiaLens reachable from wherever your domain is.
- Prefer a private network or a VPN over the open internet.
- Set [Permissions](settings.md#permissions) no higher than you need from remote devices.

## Related

- [Remote access and HTTPS](remote-access.md)
- [Running in Docker](docker.md)
- [Sign-in and your account](sign-in.md)
