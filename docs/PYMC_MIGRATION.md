# Migrating the BBS to a pyMC repeater companion (TCP)

Use this guide when moving from a USB **companion** radio on the BBS host to a **[pyMC_Repeater](https://github.com/pyMC-dev/pyMC_Repeater)** node that exposes a **companion identity over TCP**. The BBS keeps the same SQLite database and config; only the `device` section and Docker wiring change.

---

## Overview

| Piece | Role |
|-------|------|
| **pyMC repeater** | LoRa repeater + one or more TCP **companion** identities |
| **meshcore-bbs** | Connects with `device.connection: tcp` via `meshcore_py` (`MeshCore.create_tcp`) |
| **USB companion (old)** | Can stay on the bench for key export; not used by the BBS after migration |

pyMC documents companion TCP under `mesh.identities.companions` in `config.yaml` — each companion listens on `tcp_port` (commonly **5000**). Only **one** TCP client may use a given companion at a time, so stop any other tools (meshcore-cli, tests) before starting the BBS.

---

## 1. Export identity from the existing USB companion

You need the **private key** (and ideally the **advert name**) from the companion the BBS used before, so the pyMC companion keeps the same mesh identity and contacts still recognise the BBS.

### 1.1 Public key and name (easy)

While the BBS still runs on serial, note the pubkey from startup logs:

```text
BBS ready. self_pubkey=abcd1234ef56
```

Or set `device.expected_pubkey` in `config.yaml` to the full 64-character hex pubkey after you read it once.

With the BBS **stopped** (nothing else on the serial port), use [meshcore-cli](https://github.com/meshcore-dev/meshcore-cli) on the host:

```bash
meshcore-cli -s /dev/serial/by-id/YOUR_COMPANION_DEVICE -b 115200
# at the prompt:
infos
card
```

- `infos` — node name, public key, radio summary  
- `card` — export URI for this node (useful to verify identity)

### 1.2 Private key (required for pyMC)

MeshCore documents `get prv.key` / `set prv.key <hex>` on **repeater / room-server** firmware over USB serial ([FAQ §3.5](https://github.com/meshcore-dev/MeshCore/blob/main/docs/faq.md)). Many **USB serial companion** builds also expose a console when nothing else holds the port:

1. Stop the BBS container.  
2. Attach a serial terminal (115200 8N1), e.g. `picocom -b 115200 /dev/serial/by-id/YOUR_DEVICE`.  
3. Run `get prv.key` and copy the **full private key** (typically **128 hex characters** for firmware export format).  
4. Reboot the device if you later use `set prv.key` on repeater-class firmware.

If the companion console does not accept `get prv.key`, export from the **MeshCore mobile app** (identity / config export) or follow [MeshCore key application instructions](https://gessaman.com/mc-keygen/instructions/) for your device type.

**Security:** treat the private key like a password. Do not commit it to git or paste it into issue trackers.

### 1.3 BBS display name vs mesh name

- **Mesh advert name** — set on the companion / pyMC companion `node_name`; peers see this on the mesh.  
- **BBS `bbs.name`** — welcome text and STATUS only; independent of the radio identity.

---

## 2. Configure pyMC with that identity

On the pyMC host, edit `/etc/pymc_repeater/config.yaml` (paths may differ on your install). Under `mesh.identities.companions`, add (or edit) an entry:

```yaml
mesh:
  identities:
    companions:
      - name: "MyBBSCompanion"
        identity_key: "<128-hex-private-key-from-step-1>"
        settings:
          node_name: "YourMeshName"   # same as old companion advert name
          tcp_port: 5000
          bind_address: "0.0.0.0"
```

Alternatively, store the key in a file and reference pyMC’s `identity_file` / `convert_firmware_key.sh` workflow ([discussion #97](https://github.com/pyMC-dev/pyMC_Repeater/discussions/97)):

```bash
sudo ./convert_firmware_key.sh <128-hex-firmware-key> --output-format=identity
sudo systemctl restart pymc-repeater
```

Confirm the loaded public key in logs:

```bash
sudo journalctl -u pymc-repeater -f | grep -i 'hash\|identity\|public'
```

Ensure radio parameters (frequency, SF, BW, CR) match your region — the BBS does not set radio params when using TCP.

---

## 3. Point meshcore-bbs at TCP

Edit `/data/config.yaml` (or your appdata copy):

```yaml
device:
  connection: tcp
  tcp_host: "192.168.1.50"   # pyMC repeater LAN IP
  tcp_port: 5000
  expected_pubkey: "<full-64-char-pubkey-from-old-companion>"
```

Leave `serial_path` / `baud` in place if you might switch back; they are ignored when `connection: tcp`.

### Docker networking

- **Same host as pyMC:** use the repeater’s LAN IP, or on Docker Desktop `host.docker.internal`.  
- **Linux bridge network:** add to compose:

  ```yaml
  extra_hosts:
    - "host.docker.internal:host-gateway"
  ```

  then `tcp_host: host.docker.internal` if pyMC listens on the host loopback/LAN.

Remove the `--device=/dev/serial/...` mapping from the BBS container when you no longer use USB serial.

Restart:

```bash
docker compose pull && docker compose up -d
docker compose logs -f meshcore-bbs
# expect: BBS ready. self_pubkey=<same prefix as before>
```

---

## 4. Verify on the mesh

1. `curl http://localhost:8080/health` — `ok`  
2. DM the BBS from a MeshCore client — same pubkey as before should work without re-onboarding.  
3. `STATUS` — queue depth and uptime  
4. Optional: `ADVERT` (admin) — BBS visible on mesh with pyMC’s better RF path

---

## 5. Rollback

1. Set `device.connection: serial` and restore the USB `devices:` mapping.  
2. Stop pyMC’s companion TCP or use a different `tcp_port` so only one stack owns the identity.  
3. `docker compose up -d` on the BBS host.

The SQLite DB is unchanged; users and mail remain.

---

## Troubleshooting

| Symptom | Check |
|---------|--------|
| `Failed to connect to companion at host:port` | pyMC running, companion enabled, firewall allows `tcp_port`, correct IP from inside container (`docker exec … ping`) |
| `pubkey != expected` | Wrong `identity_key` on pyMC or typo in `expected_pubkey` |
| Connection drops | pyMC `tcp_timeout`; only one TCP client — stop meshcore-cli / other bridges |
| No mesh traffic | pyMC radio config, antenna, same channel preset as the rest of the mesh |

See also [DEPLOYMENT.md](DEPLOYMENT.md) §9 and [OPERATIONS.md](OPERATIONS.md).
