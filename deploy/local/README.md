# Local rehearsal

Prove the whole viaduct stack works on your machine before touching a droplet.
Two options, low to high fidelity:

- **`rehearse.sh`** — quick, no container. Runs the binaries directly with a real
  Caddy in front. Proves the wiring (Caddy → viaductd → tunnel → app). Details
  below.
- **`container-rehearse.sh`** — high fidelity. Runs the *same* `deploy/provision.sh`
  the droplet uses, inside a **systemd container**, so it reproduces the real
  services, users, and file permissions — including the `caddy`/`viaduct`
  permission split behind the earlier 502. Needs a Docker daemon (e.g. Colima:
  `brew install colima docker && colima start`), then:

  ```sh
  deploy/local/container-rehearse.sh
  ```

  It builds a systemd Ubuntu image, provisions it in `internal` TLS mode over a
  fake `viaduct.test` domain (no public DNS or DO token), waits for `caddy` and
  `viaductd` to come up under systemd, and asserts a real HTTPS request to a
  server-assigned name returns 200 through the full stack. This is the closest
  thing to the droplet you can run locally — if it's green, provisioning works.

---

## rehearse.sh (quick, no container)

Stands up the exact production request path and verifies a real HTTPS request
round-trips to a local app:

```
curl --HTTPS--> Caddy (:8443, internal CA) --plaintext--> viaductd (:8080)
                                                              |
                                                tunnel (:4443, TLS)
                                                              |
                                                   viaduct client
                                                              |
                                                   demo app (:3000)
```

## Run it

```sh
python3 -m venv .venv && .venv/bin/pip install .   # once, from the repo root
deploy/local/rehearse.sh
```

It downloads a standard Caddy binary on first run (cached under
`deploy/local/.cache/`), starts everything, and prints `REHEARSAL PASSED` after
a real HTTPS GET to a server-assigned `*.viaduct.test` name returns 200 through
the full chain. Everything is cleaned up on exit; runtime files live in
`deploy/local/.run/` (gitignored).

To keep it running and open it in a browser yourself:

```sh
deploy/local/rehearse.sh keep
```

It will print the exact `curl` command and the `/etc/hosts` line to use (the
name is random each run, and `.test` doesn't resolve publicly, so you point it
at `127.0.0.1` yourself).

## What it does and doesn't cover

Faithful to production:

- Caddy terminating HTTPS and reverse-proxying plaintext to viaductd, exactly as
  on the droplet — this is the wiring that 502'd there.
- viaductd assigning a random `*.viaduct.test` name, host-header routing, and a
  404 for unknown names.
- TLS on the tunnel port with the client verifying the server certificate.
- A browser-valid cert chain (Caddy's internal CA instead of Let's Encrypt).

Deliberately different (and why):

- **Caddy's internal CA instead of Let's Encrypt + DNS-01.** A real
  `*.viaduct.sh` cert needs public DNS pointing at the box; the internal CA
  exercises identical TLS wiring without it.
- **High ports (8443) instead of 443.** Avoids needing `sudo`; production binds
  443 via `CAP_NET_BIND_SERVICE` in the systemd unit.
- **No systemd / separate users.** Everything runs as you, so this cannot
  reproduce Linux file-permission issues between the `viaduct` and `caddy`
  service users — that class of bug only shows up on the droplet (or in a
  multipass VM, if you want the higher-fidelity rehearsal).
