# Office Monitoring Server Documentation

Setup record for the monitoring server built on a spare ThinkCentre M710, from the Ubuntu installation through Zabbix and Uptime Kuma, including the workstation agent rollout method.

**Last updated:** 3 November 2026
**Maintained by:** Office IT

> **Do not store passwords in this file.** Login credentials are kept in the approved password manager. A copy of `~/monitoring/.env` and `docker-compose.yml` should also be kept off the server.

---

## 1. Overview

| Item | Value |
|---|---|
| Purpose | Monitor every office workstation, the Bizbox server connection, and key network devices |
| Hardware | Lenovo ThinkCentre M710 Tiny, 240 GB SSD (HS-SSD-C100) |
| Operating system | Ubuntu Server (LTS) |
| Server hostname | `it-monitor` |
| Server IP | `172.16.1.39` (static, `/16`) |
| Gateway / pfSense LAN | `172.16.0.1` |
| LAN subnet | `172.16.0.0/16` (mask `255.255.0.0`), no VLANs |
| Bizbox server | `172.16.1.2`, port `1433` (Bizbox Hospital Information System 8.0, SQL Server port) |
| Time zone | `Asia/Manila` |

### Services

| Service | Purpose | Address |
|---|---|---|
| Zabbix web | Detailed monitoring of every PC | `http://172.16.1.39:8080` |
| Zabbix server | Receives data from agents | port `10051` |
| Uptime Kuma | Up/down status of shared infrastructure | `http://172.16.1.39:3001` |
| PostgreSQL | Zabbix database (internal to Docker) | not exposed |

### Decisions made

- **pfSense is not configured.** The IT head confirmed that no changes are needed on pfSense. pfSense is monitored only through Uptime Kuma (ping and HTTP). There is no SNMP integration in Zabbix.
- **The Bizbox check is a port test only.** It tests whether port 1433 accepts connections and never logs in or reads any hospital data.
- **No agent is installed on the Bizbox server.** This is a production hospital system, so nothing is installed there without approval from the IT head and the Bizbox vendor.

---

## 2. Network notes

- The office network is **flat**. Workstations use manually assigned (static) addresses, set in Windows network settings (`ncpa.cpl`).
- No VLANs are configured. The pfSense console shows plain interfaces (`igb0`, `em0`, `igb1`).
- pfSense interfaces: WAN `igb0` (`192.168.1.149/24` by DHCP, behind another router), LAN `em0` (`172.16.0.1/16`), GUESTNETWORK `igb1` (`192.168.1.1/24`).
- Because traffic between PCs and the M710 stays inside the switch, **pfSense rules cannot restrict who reaches the M710**. Strong passwords are the main protection for the web pages.
- The wall socket first gave the M710 a `192.168.0.x` DHCP address, which means another device on that network hands out those addresses. Setting a manual `172.16.x.x` address fixed this.
- Keep an up-to-date **IP list**. Addresses are assigned by hand, so a duplicate can silently knock a PC offline.

---

## 3. Installation: Ubuntu Server

### 3.1 Preparation

1. Wrote the Ubuntu Server ISO to a USB flash drive.
2. Booted the M710 from the USB (F12 boot menu).
3. The old Windows installation on the internal SSD triggered repeated "unclean file system / Windows is hibernated" messages. These are harmless: the installer only mounts the Windows partition read-only.

### 3.2 Wiping the old drive

The installer was very slow while probing block devices, and later crashed, so the old partitions were cleared from the installer shell:

```bash
lsblk -o NAME,SIZE,MODEL,TRAN      # identify the SSD (sata) and the USB stick (usb)
sudo smartctl -H /dev/sda          # installed smartmontools; result: PASSED
sudo wipefs -a /dev/sda            # removes partition table signatures
```

`wipefs` only erases the small signature bytes, which is why it reported 2 and 4 bytes. The installer was restarted afterwards and ran normally.

### 3.3 Installer choices

| Screen | Choice |
|---|---|
| Network | Manual IPv4: subnet `172.16.0.0/16`, address `172.16.1.39`, gateway `172.16.0.1`, DNS from a working PC |
| Mirror | Default mirror; the test can be skipped if there is no internet yet |
| Storage | Use an entire disk, 240 GB SSD, LVM, **LUKS encryption off** (so the server restarts unattended after a power cut) |
| Profile | Server name `it-monitor`, personal admin username, strong password |
| SSH | **Install OpenSSH server** ticked |
| Featured snaps | None (Docker is installed separately) |

The guided layout created `/boot` (2 GB) and a root volume of only 100 GB, leaving about 121 GB free in the volume group. This was extended after installation (section 4).

---

## 4. Post-install configuration

### 4.1 Network check

```bash
ip a
ping -c 3 172.16.0.1     # pfSense
ping -c 3 172.16.1.2     # Bizbox
ping -c 3 8.8.8.8        # internet
ping -c 3 google.com     # DNS
```

If the address needs correcting later, edit `/etc/netplan/50-cloud-init.yaml` (spaces only, no tabs) and run `sudo netplan apply`:

```yaml
network:
  version: 2
  ethernets:
    enp0s31f6:
      dhcp4: false
      addresses: [172.16.1.39/16]
      routes:
        - to: default
          via: 172.16.0.1
      nameservers:
        addresses: [172.16.0.1, 8.8.8.8]
```

### 4.2 Use the whole disk

```bash
sudo lvextend -r -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
df -h /
```

### 4.3 Update and time zone

```bash
sudo apt update && sudo apt full-upgrade -y
sudo timedatectl set-timezone Asia/Manila
```

### 4.4 Docker

```bash
sudo apt install -y ca-certificates curl
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Log out and back in afterwards. Until then, Docker commands fail with **"permission denied while trying to connect to the docker API"**. Test with `docker run --rm hello-world`.

---

## 5. Zabbix and Uptime Kuma (Docker)

### 5.1 Project folder

```bash
mkdir -p ~/monitoring && cd ~/monitoring
echo "DB_PASSWORD=$(openssl rand -hex 24)" > .env
chmod 600 .env
```

### 5.2 `docker-compose.yml`

```yaml
services:
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: zabbix
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: zabbix
    volumes:
      - ./pgdata:/var/lib/postgresql/data

  zabbix-server:
    image: zabbix/zabbix-server-pgsql:alpine-7.0-latest
    restart: unless-stopped
    depends_on:
      - postgres
    environment:
      DB_SERVER_HOST: postgres
      POSTGRES_USER: zabbix
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: zabbix
    ports:
      - "10051:10051"

  zabbix-web:
    image: zabbix/zabbix-web-nginx-pgsql:alpine-7.0-latest
    restart: unless-stopped
    depends_on:
      - postgres
      - zabbix-server
    environment:
      ZBX_SERVER_HOST: zabbix-server
      DB_SERVER_HOST: postgres
      POSTGRES_USER: zabbix
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: zabbix
      PHP_TZ: Asia/Manila
    ports:
      - "8080:8080"

  uptime-kuma:
    image: louislam/uptime-kuma:1
    restart: unless-stopped
    volumes:
      - ./kuma:/app/data
    ports:
      - "3001:3001"
```

### 5.3 Start and verify

```bash
cd ~/monitoring
docker compose up -d
docker compose ps
```

The first start takes one to two minutes while PostgreSQL builds the Zabbix database.

### 5.4 First login and hardening

- Zabbix: `http://172.16.1.39:8080`, default login `Admin` / `zabbix` (**case-sensitive**). The password was changed on first login, and the `guest` user should be disabled.
- Zabbix has its own accounts, separate from Ubuntu. Five failed logins lock an account for about 30 seconds.
- Uptime Kuma: `http://172.16.1.39:3001`, admin account created on first visit with a different password.
- All credentials are stored in the password manager.

---

## 6. Uptime Kuma monitors

Settings used: heartbeat interval `60`, retries `3`, **Upside Down Mode off** on every monitor (leaving it on makes a working service show as down).

### Group: Bizbox

| Friendly name | Type | Target |
|---|---|---|
| Bizbox Server | Ping | `172.16.1.2` |
| Bizbox Database Port | TCP Port | `172.16.1.2`, port `1433` |

If Ping is green but the TCP Port is red, the server is up but the database service is down.

### Group: Network

| Friendly name | Type | Target |
|---|---|---|
| pfSense Firewall | Ping | `172.16.0.1` |
| pfSense Web Page | HTTP(s) | `http://172.16.0.1` (the pfSense web page uses plain HTTP) |
| Upstream Router | Ping | WAN gateway shown in pfSense (Status > Gateways) |
| Internet | Ping | `8.8.8.8` |
| DNS Check | DNS | `google.com`, resolver `172.16.0.1`, port `53`, record type `A` |

"Friendly Name" is only a label. "Hostname" is the address that is actually tested.

---

## 7. Zabbix configuration

All steps are in the Zabbix web interface.

### 7.1 Host group and template

1. **Data collection > Host groups:** created `Workstations`.
2. **Data collection > Templates > Create template:**
   - Name `Office PC - Bizbox`, template group `Workstations`.
   - Linked template: **Windows by Zabbix agent active** (this provides the standard CPU, memory, disk, uptime and service monitoring).

### 7.2 Macros (template, Macros tab)

| Macro | Value |
|---|---|
| `{$BIZBOX_HOST}` | `172.16.1.2` |
| `{$BIZBOX_PORT}` | `1433` |

### 7.3 Item

| Field | Value |
|---|---|
| Name | `Bizbox port reachable` |
| Type | Zabbix agent (active) |
| Key | `net.tcp.port[{$BIZBOX_HOST},{$BIZBOX_PORT}]` |
| Type of information | Numeric (unsigned) |
| Update interval | `30s` |

The check runs on each PC, so it tests that PC's own path to Bizbox. Value `1` means the port answered, `0` means it did not. To create it, click the **Items** link in the template's row, then **Create item**. Make sure the type is **Zabbix agent (active)** and not the plain "Zabbix agent".

### 7.4 Trigger

| Field | Value |
|---|---|
| Name | `Bizbox unreachable from {HOST.NAME}` |
| Severity | High |
| Expression | `max(/Office PC - Bizbox/net.tcp.port[{$BIZBOX_HOST},{$BIZBOX_PORT}],3m)=0` |

It only fires after three continuous minutes of failures, which avoids false alarms from a single missed check.

### 7.5 Auto-registration action

**Alerts > Actions > Autoregistration actions > Register Windows PCs** (status **Enabled**).

- **Condition:** Host metadata contains `windows-office` (lowercase, case-sensitive).
- **Operations (all three are required):**
  1. Add host
  2. Add to host groups: `Workstations`
  3. Link to templates: `Office PC - Bizbox`

> An earlier version of the action had only the host-group operation, so the laptop registered but never received the template. The template had to be linked to that host by hand. The action does not re-process hosts that already exist.

---

## 8. Installing the agent on a workstation

Download **Zabbix Agent 2** for Windows (MSI, amd64, OpenSSL, version 7.0.x) from the Zabbix website. Run in an **administrator** Command Prompt:

```bat
msiexec /i "C:\Temp\zabbix_agent2.msi" /qn SERVER=172.16.1.39 SERVERACTIVE=172.16.1.39 HOSTNAMEITEM=system.hostname HOSTMETADATA=windows-office ENABLEPATH=1
```

The `HostMetadata` value must match the action's condition exactly. The install wizard has no field for it, so the silent command is the easier method. If the wizard was used, add this line to `C:\Program Files\Zabbix Agent 2\zabbix_agent2.conf` (no `#` at the start) and run `Restart-Service "Zabbix Agent 2"`:

```
HostMetadata=windows-office
```

### Verification on the PC

```powershell
Get-Service "Zabbix Agent 2"
Test-NetConnection 172.16.1.39 -Port 10051
Test-NetConnection 172.16.1.2 -Port 1433
Select-String -Path "C:\Program Files\Zabbix Agent 2\zabbix_agent2.conf" -Pattern "^(ServerActive|Hostname|HostnameItem|HostMetadata)="
Get-Content "C:\Program Files\Zabbix Agent 2\zabbix_agent2.log" -Tail 30
```

### Verification in Zabbix

1. **Data collection > Hosts:** the PC appears in `Workstations` with the template linked. The **ZBX** badge turns green after a minute or two.
2. **Monitoring > Latest data**, filter by the host, search `Bizbox`: `Bizbox port reachable` should show `1`.

### Alert test (completed on the test laptop)

A host-level macro `{$BIZBOX_PORT}` = `1444` was set on one host. The value dropped to `0` and the problem `Bizbox unreachable from <host>` appeared, proving the trigger works. The override must then be **removed** (Macros tab, enter the macro name `{$BIZBOX_PORT}` in the Macro box and the number in the Value box while testing, and click Remove afterwards), or the PC keeps reporting a false problem.

### Rollout plan for the remaining PCs

1. Get approval from the IT head before installing on all workstations.
2. Install on two or three PCs, confirm they register with the template.
3. Continue department by department, using the command above, a shared folder for the MSI, or a Group Policy startup script.
4. Keep a list matching each Windows computer name to a user or desk, and set a friendly **Visible name** on each host.

---

## 9. Problems encountered and fixes

| Problem | Cause and fix |
|---|---|
| Installer slow at "probing for block devices", then crashed | Old Windows partitions confused the installer. Cleared with `wipefs -a /dev/sda` and restarted the installer |
| "smartctl not found" in the installer shell | Not included in the installer. Installed `smartmontools` with apt |
| Static IP rejected or no connection | Wall socket first served `192.168.0.x` by DHCP. Used a manual `172.16.x.x/16` address with gateway `172.16.0.1`. The netplan YAML must use spaces, never tabs |
| `permission denied` on Docker | The `docker` group was not active yet. Log out and back in, or `newgrp docker` |
| Zabbix login failed with the Ubuntu password | Zabbix accounts are separate. Use `Admin` / `zabbix` first, then change it |
| pfSense web page monitor failed | The pfSense web page runs on plain HTTP, so the monitor URL is `http://172.16.0.1` |
| Uptime Kuma monitor showed Bizbox as down while it worked | **Upside Down Mode** was ticked. Untick it |
| PC registered but showed nothing | The auto-registration action was missing the "Link to templates" operation. Added it, and linked the template by hand to the existing host |
| Host macro test did nothing | The number was entered in the Macro name box. Macro name goes in "Macro", the value goes in "Value" |
| Manually created host stays grey | The agent's hostname must match the Zabbix host name exactly (case-sensitive), `ServerActive` must be `172.16.1.39`, and the template must be linked. The easier route is to delete the manual host and let auto-registration recreate it |
| `Zabbix server` host is red ("connection refused to 127.0.0.1:10050") | The built-in host expects an agent inside the container, and there isn't one. Harmless. Disable the host |

---

## 10. Maintenance

### Monthly updates

```bash
cd ~/monitoring && docker compose pull && docker compose up -d
sudo apt update && sudo apt upgrade -y
```

### Nightly database backup

```bash
mkdir -p ~/backups
cd ~/monitoring
docker compose exec -T postgres pg_dump -U zabbix zabbix | gzip > ~/backups/zabbix-$(date +%F).sql.gz
```

Schedule it with `crontab -e` and **copy the files to another machine regularly**. A backup that only lives on the same SSD does not protect against a failed disk. Also keep `docker-compose.yml` and `.env` somewhere safe off the server.

### Routine

- **Daily:** check **Monitoring > Problems** in Zabbix and the Uptime Kuma dashboard.
- **New PC:** install the agent with the command in section 8. It registers itself.
- **Retired PC:** delete its host in Zabbix so it does not show as down.
- **Power:** use a UPS for the M710, because sudden power loss can corrupt the database.

### Useful commands

```bash
cd ~/monitoring
docker compose ps                         # service status
docker compose logs --tail 50 zabbix-server
docker compose restart                    # restart all services
```

---

## 11. Open items and recommendations

- [ ] Set up **Telegram alerts** for Zabbix (Alerts > Media types > Telegram) and Uptime Kuma, sending only **High** severity from Zabbix to avoid noise.
- [ ] Roll the agent out to the remaining workstations after IT head approval.
- [ ] Add a **"no data for 5 minutes"** trigger to detect PCs that went silent: `nodata(/Office PC - Bizbox/net.tcp.port[{$BIZBOX_HOST},{$BIZBOX_PORT}],5m)=1` (Warning severity, keep out of the Telegram action because PCs switched off at night will trigger it).
- [ ] Build a Zabbix dashboard with a Problems widget filtered to `Workstations`.
- [ ] Disable the built-in `Zabbix server` host (red icon, harmless).
- [ ] Schedule the nightly backup and copy it off the server.
- [ ] Add printers, switches and access points to Uptime Kuma.
- [ ] Restrict access to ports `8080`, `3001` and SSH to IT PCs. This needs firewall rules on the M710 itself (Docker publishes ports around UFW), since pfSense does not see this traffic.
- [ ] Optional pfSense housekeeping, only with IT head approval and a configuration backup first:
  - The WAN (`192.168.1.149/24`) and GUESTNETWORK (`192.168.1.1/24`) are in the **same subnet**. Moving the guest network to an unused range (for example `192.168.50.1/24`) would remove the overlap.
  - pfSense is on version **2.6.0**, which is old. Plan an update (System > Update).
  - Switch the pfSense web interface from HTTP to HTTPS (System > Advanced > Admin Access).
- [ ] Find the device handing out `192.168.0.x` addresses on the wall-socket network.

---

## 12. Quick reference

| What | Value |
|---|---|
| Zabbix web | `http://172.16.1.39:8080` |
| Uptime Kuma | `http://172.16.1.39:3001` |
| Agents connect to | `172.16.1.39`, TCP `10051` |
| Auto-registration metadata | `windows-office` |
| Template | `Office PC - Bizbox` (includes Windows by Zabbix agent active) |
| Host group | `Workstations` |
| Bizbox | `172.16.1.2`, port `1433` |
| pfSense | `172.16.0.1` (web page on plain HTTP) |
| Project folder on the server | `~/monitoring` |
