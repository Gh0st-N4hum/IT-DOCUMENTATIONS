# Samba (SMB) File Server on Docker

A step-by-step record of setting up a Samba file share in Docker on an Ubuntu server, including the problems hit along the way and how they were fixed.

## Table of Contents

1. [Overview](#overview)
2. [Environment](#environment)
3. [Why Samba instead of Nextcloud](#why-samba-instead-of-nextcloud)
4. [Final setup](#final-setup)
5. [Connecting from Windows](#connecting-from-windows)
6. [What did not work: external NTFS USB drive](#what-did-not-work-external-ntfs-usb-drive)
7. [Troubleshooting](#troubleshooting)
8. [Security notes](#security-notes)
9. [Backups and next steps](#backups-and-next-steps)

---

## Overview

Goal: a lightweight internal file share for IT use (drivers, installers, forms) that Windows PCs can open with a mapped network drive (`\\server-ip\Share`).

Result: a Samba container with four shares, two user accounts (one admin, one read-only), running with a memory limit on the server's internal disk.

| Share | Purpose | Access |
|---|---|---|
| `Drivers` | Device and printer drivers | `itadmin` read/write, `staff` read-only |
| `Installers` | Software installers | `itadmin` read/write, `staff` read-only |
| `Forms` | Documents for staff to download | `itadmin` read/write, `staff` read-only |
| `IT-Private` | IT team files (hidden in the share list) | `itadmin` only |

## Environment

- Ubuntu Server (64-bit), 4-core CPU, about 3.2 GB RAM
- Docker and Docker Compose already running (monitoring stack: Zabbix, PostgreSQL, Uptime Kuma)
- Monitoring done with `glances` and `btop`
- About 1.9 GB of free RAM at the start, so new services had to be lightweight

### Reading the `glances` process table (quick reference)

| Column | Meaning |
|---|---|
| PID | Process ID, a unique number for each running process |
| THR | Number of threads the process is running |
| USER | Account that owns the process |
| NI | Nice value (scheduling priority, 0 is normal) |
| S | State: `S` sleeping, `R` running, `Z` zombie |
| CPU% / MEM% | Share of CPU / RAM used |

Healthy signs on a 4-core machine: load average well under 4.0, free memory not near zero, swap barely used.

## Why Samba instead of Nextcloud

| | Samba (SMB) | Nextcloud |
|---|---|---|
| RAM | about 50-100 MB | about 600 MB-1 GB (app, database, cache) |
| Access | Windows mapped drive | Browser, mobile app, sync client |
| Best for | Shared folders, drivers, installers | File sync, sharing links, calendar |
| Setup | Quick | Longer, needs tuning |

With about 1.9 GB of free RAM, Nextcloud would have used half of it. Samba was chosen first. Nextcloud can be added later if more RAM is available.

## Final setup

### 1. Create the folders

```bash
sudo mkdir -p /srv/shares/{Drivers,Installers,Forms,IT-Private}
sudo chown -R 1000:1000 /srv/shares
sudo chmod -R 770 /srv/shares
ls -ld /srv/shares/*
```

The `chown` step matters. The container runs as user ID 1000, so folders created with `sudo mkdir` are owned by `root` and the container cannot write to them until ownership is fixed.

### 2. Create the project folder and secrets file

```bash
mkdir -p ~/samba
cd ~/samba
nano .env
```

Put the passwords in `.env` (do **not** commit this file to Git):

```
SAMBA_ADMIN_PASSWORD=use-a-long-unique-password
SAMBA_STAFF_PASSWORD=use-a-different-long-password
```

Avoid `$`, `#`, quotes, and backslashes in passwords. They can be mangled in environment variables.

### 3. Create `docker-compose.yml`

```yaml
services:
  samba:
    image: ghcr.io/servercontainers/samba:latest
    container_name: samba
    restart: unless-stopped
    network_mode: host
    mem_limit: 256m
    environment:
      # Accounts
      ACCOUNT_itadmin: "${SAMBA_ADMIN_PASSWORD}"
      UID_itadmin: 1000
      ACCOUNT_staff: "${SAMBA_STAFF_PASSWORD}"
      UID_staff: 1001

      # Shares: itadmin can write, staff is read-only
      SAMBA_VOLUME_CONFIG_drivers: "[Drivers]; path=/shares/Drivers; valid users = itadmin staff; read only = no; read list = staff; write list = itadmin; browseable = yes"
      SAMBA_VOLUME_CONFIG_installers: "[Installers]; path=/shares/Installers; valid users = itadmin staff; read only = no; read list = staff; write list = itadmin; browseable = yes"
      SAMBA_VOLUME_CONFIG_forms: "[Forms]; path=/shares/Forms; valid users = itadmin staff; read only = no; read list = staff; write list = itadmin; browseable = yes"
      SAMBA_VOLUME_CONFIG_itprivate: "[IT-Private]; path=/shares/IT-Private; valid users = itadmin; read only = no; browseable = no"
    volumes:
      - /srv/shares:/shares
```

Key points:

- `network_mode: host` lets Samba use ports 445/139 directly.
- `mem_limit: 256m` stops the container from starving the other services.
- `read list = staff` forces the `staff` account to read-only even on shares it can open.
- `browseable = no` hides `IT-Private` from the share list. It is still reachable by typing the path.

### 4. Firewall (only if UFW is active)

```bash
sudo ufw status
sudo ufw allow from 192.168.1.0/24 to any port 445 proto tcp
```

Replace the subnet with your own internal network. Never expose port 445 to the internet.

### 5. Start it and test

```bash
cd ~/samba
docker compose up -d
docker compose logs samba | tail -20
docker ps | grep samba
```

Test from the server itself:

```bash
sudo apt install smbclient -y
smbclient -L 127.0.0.1 -U itadmin -m SMB3
```

Expected output lists `Drivers`, `Installers`, and `Forms` (plus the built-in `IPC$`). `IT-Private` does not appear because it is hidden.

Test a write:

```bash
smbclient //127.0.0.1/Forms -U itadmin -m SMB3 -c "mkdir testdir; ls; rmdir testdir"
```

## Connecting from Windows

1. Open File Explorer and type `\\<server-ip>\Drivers` in the address bar.
2. Log in as `itadmin` (full access) or `staff` (read-only).
3. To make it permanent: right-click **This PC**, choose **Map network drive**, enter the path, and tick **Reconnect at sign-in**.
4. For the hidden share, type `\\<server-ip>\IT-Private` directly.

If Windows keeps using the wrong login (a very common cause of "Access Denied"), clear saved connections:

```
net use * /delete
```

Then reconnect with the right user. If that does not help, remove the server's entry from **Credential Manager** (Windows Credentials).

## What did not work: external NTFS USB drive

The first plan was to store the shares on an existing external USB drive that already held files (NTFS). It was abandoned for these reasons:

1. **NTFS permissions on Linux are limited.** `chown` and `chmod` have no effect; ownership is fixed by mount options (`uid`, `gid`, `umask`) in `/etc/fstab`.
2. **The volume was flagged dirty.** `dmesg` showed `ntfs3(sdb1): volume is dirty and "force" flag is not set!`, so the mount failed. Plain `ntfsfix` did not clear it; `ntfsfix -d` or running `chkdsk /f` on Windows is needed.
3. **The drive disconnected by itself.** `dmesg` showed `USB disconnect` followed by a new device. Unexpected disconnects keep re-flagging NTFS as dirty and can corrupt data. Common causes are weak USB power, a bad cable, or a hub.
4. **A silent failure mode.** When the USB was not mounted, `/srv/shares` was just an empty folder on the internal disk. Samba (started earlier) kept sharing that folder, and write tests appeared to "work" even though nothing was on the USB. Always run `findmnt /srv/shares` before starting the container, and restart the container after any mount change, because Docker binds the path when the container starts.

Decision: use the server's internal disk (ext4, about 213 GB free). Files on the USB were never modified.

If you want to try an external drive again later:

- Use ext4 (not NTFS) if the drive is only for Samba.
- Add `nofail` in `/etc/fstab` so the server still boots when the drive is missing.
- Use `UUID=...` in fstab, not `/dev/sdb1`.
- Use a powered port or hub, and a short cable.
- Check health with `sudo smartctl -a /dev/sdb`.

Example fstab line (ext4):

```
UUID=your-uuid-here  /srv/shares  ext4  defaults,nofail  0  2
```

Always back up `/etc/fstab` before editing, and run `sudo mount -a` to check for errors before rebooting:

```bash
sudo cp /etc/fstab /etc/fstab.bak
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `NT_STATUS_CONNECTION_DISCONNECTED` right after install | Container not ready yet, or started before the storage was in place | Wait 15-20 seconds, or `docker compose up -d --force-recreate` |
| `NT_STATUS_LOGON_FAILURE` | Wrong password. The password is whatever is in `.env` / the compose file; there is no default | Check the password in the compose file or `.env`, recreate the container |
| Windows: "You need permission to perform this action" | Folder owned by `root`, wrong saved Windows login, or `staff` account used | `sudo chown -R 1000:1000 /srv/shares`, then `net use * /delete` on Windows and log in as `itadmin` |
| Cannot see the server by name | Name discovery needs extra setup | Use the IP address |
| Container exits immediately | Typo in a share config line | `docker compose logs samba` |
| New share folders not visible | Container started before the folders or mount existed | `docker compose up -d --force-recreate` |
| Mount fails with "wrong fs type, bad option, bad superblock" | NTFS volume flagged dirty | `sudo dmesg \| tail -20`, then `sudo ntfsfix -d /dev/sdb1` or `chkdsk /f` on Windows |

Useful diagnostic commands:

```bash
docker compose logs samba | tail -40
docker exec samba testparm -s | grep -A10 '^\[Forms\]'
docker exec samba ls -ld /shares/Forms
sudo ss -tlnp | grep -E ':(445|139)'
findmnt /srv/shares
```

## Security notes

- Use strong, unique passwords, and a separate account per person or team when possible.
- Keep secrets in `.env` and add it to `.gitignore`. Never push passwords to a public repository.
- Keep port 445 reachable only from the internal network. Do not port-forward it on the router.
- Do not store sensitive or regulated data (for example patient records) on the share unless your organization's IT policy allows it.
- Avoid auto-updating containers blindly. A tool such as Diun can notify you about new images without changing anything.
- Set `mem_limit` on every new container so one runaway service cannot push the server into swap.

Suggested `.gitignore` for this folder:

```
.env
```

## Backups and next steps

- **Back up `/srv/shares`.** The share is only as safe as its last backup. A simple start:
  ```bash
  rsync -a /srv/shares/ /path/to/backup/shares/
  ```
  Kopia or Duplicati can run this on a schedule.
- **Reboot test.** Run `sudo reboot` once while you are at the server, then check `docker ps | grep samba` to confirm everything comes back by itself.
- **Per-team accounts.** Add more `ACCOUNT_name` / `UID_name` lines and adjust `valid users` per share.

### Other lightweight containers considered for this server

Suited to a 3 GB RAM machine:

- Remote support and tools: RustDesk server, MeshCentral, netboot.xyz, File Browser
- Monitoring extras: ntfy or Gotify (phone alerts), Healthchecks, Speedtest Tracker, Diun, Dozzle, Homepage
- Inventory and tickets: Snipe-IT or GLPI (pick one)
- Access and networking: Nginx Proxy Manager or Caddy
- Backups: Kopia or Duplicati

Avoid on this box without more RAM: NetBox, Graylog, Elasticsearch/Kibana, Nextcloud with many users, Paperless-ngx.
