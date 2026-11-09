# Zabbix: Duplicate Hostnames Result in Only One Device Being Monitored

## Summary

When two or more computers report to Zabbix with the **same hostname**, Zabbix treats them as a **single host**. Only the first registered device appears in the host list, and data from the others is either mixed into that host or lost.

This is common when rolling out the Zabbix Agent across many office PCs, especially when machines were cloned from the same disk image.

## Symptoms

- Only one device appears in **Data collection → Hosts**, even though multiple PCs have the agent installed.
- Latest data for a host flips between different machines, or only one machine seems to report.
- A second PC does **not** auto-register as a new host.
- Creating a new host with an existing host name fails with a "host already exists" error.

## Cause

In Zabbix, the **Host name** is the unique technical identifier used to match incoming data to a host.

| Agent mode | How the host is matched | Effect of duplicate names |
|------------|------------------------|---------------------------|
| Active agent | By the `Hostname=` value in the agent config | Both PCs send data to the same host entry. Auto-registration only creates the host for the first PC. |
| Passive agent | By the IP/DNS in the host interface | Data collection is less affected, but two hosts still cannot share the same host name in the frontend. |

### Common root causes

- PCs cloned from the same disk image, keeping the same Windows computer name.
- Agent config copied between machines without changing `Hostname=`.
- `HostnameItem=system.hostname` is used while the PCs genuinely share a Windows computer name.

## Resolution

### 1. Identify the duplicates

On each affected PC, check the computer name:

```powershell
hostname
```

In the Zabbix frontend, compare the host name against the machines that should be reporting.

### 2. Rename the Windows computer (Windows 10 Pro)

**Via Settings**

1. Open **Settings → System → About**.
2. Click **Rename this PC**.
3. Enter the new name, click **Next**, then restart.

**Via PowerShell (run as Administrator)**

```powershell
Rename-Computer -NewName "PC-ACCT-01" -Restart
```

**Remotely**

```powershell
Rename-Computer -ComputerName "OLD-NAME" -NewName "NEW-NAME" -Restart -Credential (Get-Credential)
```

**Naming rules**

- Maximum 15 characters (NetBIOS limit).
- Letters, numbers, and hyphens only. No spaces or underscores.
- Cannot be all numbers.
- Use a consistent scheme, for example `DEPT-NN` (`ACCT-01`, `HR-02`).

> **Domain-joined PCs:** use an account with permission to rename computer objects, and make sure the change is reflected in Active Directory to avoid breaking the trust relationship.

### 3. Set a unique `Hostname=` in the agent config

Edit `C:\Program Files\Zabbix Agent\zabbix_agentd.conf` (path may vary by version):

```ini
Hostname=PC-ACCT-01
```

If the agent uses `HostnameItem=system.hostname`, it will follow the new Windows name automatically (remove or comment out any static `Hostname=` line in that case).

### 4. Restart the Zabbix Agent service

```powershell
Restart-Service "Zabbix Agent"
```

### 5. Verify in the Zabbix frontend

- The original host remains tied to the first machine.
- The renamed machine should **auto-register** as a new host. If auto-registration is not configured, create the host manually using its unique host name.
- Confirm **Latest data** is updating for each host independently.
- Optionally clear the mixed history on the original host if the data is confusing.

## Prevention

- Rename each PC **before** installing the Zabbix Agent.
- Never copy `zabbix_agentd.conf` between machines without editing `Hostname=`.
- If cloning images, run Sysprep or rename the computer as part of post-imaging setup.
- Use the **Visible name** field in Zabbix for friendly labels. It does not need to match the agent hostname, though keeping names unique is easier to manage.

## Quick Reference

| Task | Command / Location |
|------|--------------------|
| Check Windows hostname | `hostname` |
| Rename PC | `Rename-Computer -NewName "NEW-NAME" -Restart` |
| Agent config file | `zabbix_agentd.conf` → `Hostname=` |
| Restart agent | `Restart-Service "Zabbix Agent"` |
| Zabbix host list | **Data collection → Hosts** |

## References

- [Zabbix documentation: Zabbix agent configuration](https://www.zabbix.com/documentation/current/en/manual/appendix/config/zabbix_agentd)
- [Zabbix documentation: Auto-registration](https://www.zabbix.com/documentation/current/en/manual/discovery/auto_registration)
