# Troubleshooting: Ping Fails with "General Failure" and No Internet on Windows

**Date:** October 13, 2026
**Environment:** Windows 11 PC, wired LAN, static IPv4 network, managed gateway that also acts as DNS server
**Final root cause:** Windows Defender Firewall outbound rule blocking ICMP

---

## 1. Symptoms

1. Ethernet link lights were blinking, so the physical link was up.
2. No internet access in the browser.
3. `ping 127.0.0.1` and ping to the PC's own IP worked.
4. `ping <default gateway>` returned `General failure`.
5. `ping 8.8.8.8` returned `General failure`.

## 2. What the symptoms mean

| Test | Result | Meaning |
|------|--------|---------|
| Ping 127.0.0.1 | Works | TCP/IP stack is alive |
| Ping own IP | Works | Adapter has an address bound |
| Ping gateway | General failure | Windows could not send the packet out |
| Ping 8.8.8.8 | General failure | No usable route, or traffic blocked locally |

`General failure` is a local error. The packet never left the machine. It points to a bad IP configuration, a broken network stack, a driver or filter problem, or a local firewall rule. A bad cable or dead port normally produces `Request timed out` or `Destination host unreachable`, not `General failure`.

## 3. Steps tried first (no effect)

1. Set IPv4 to obtain automatically.
2. Disabled and re enabled the adapter.
3. Replugged the LAN cable and tried a second cable.
4. Disabled IPv6.
5. Ran a full stack reset:

```
netsh winsock reset
netsh int ip reset
ipconfig /release
ipconfig /renew
ipconfig /flushdns
```

6. Ran a Windows network reset.

## 4. Diagnostic findings

### 4.1 Adapter configuration

`ipconfig /all` showed `DHCP Enabled: No`. After switching to automatic, the adapter fell back to a `169.254.x.x` address (APIPA).

**Lesson:** a 169.254 address means no DHCP server answered. On this network the addresses are assigned statically, so this was expected and not a fault. The original static configuration was valid.

Working configuration:

```
IPv4 Address . . . : 172.16.x.x
Subnet Mask . . . .: 255.255.0.0
Default Gateway . .: 172.16.0.1
DNS Server . . . . : 172.16.0.1
```

### 4.2 Different cable and port

Using a different cable and a USB Gigabit adapter (ASIX AX88179), Windows reported IPv4 connectivity and a 100 Mbps link. `ping 8.8.8.8` replied, but `ping google.com` failed.

### 4.3 DNS behavior

```
nslookup google.com 8.8.8.8      -> timed out
nslookup google.com 172.16.0.1   -> resolved
```

Public DNS servers (8.8.8.8, 1.1.1.1) are blocked on this network. Only the local gateway answers DNS queries. The manual DNS entries had to be removed and replaced with the gateway address.

### 4.4 Firewall outbound rules

Pings kept failing even when routing and DNS were correct. Reviewing Windows Defender Firewall outbound rules showed a rule that **blocked outgoing ICMP**. This was the cause of the failed ping tests and the misleading `General failure` results.

## 5. Fix

### 5.1 Restore correct network settings

1. Keep the static IP, subnet mask, and gateway assigned for the network.
2. Set DNS to the local gateway only (`172.16.0.1`). Remove 8.8.8.8 and 1.1.1.1.
3. Flush the resolver cache:

```
ipconfig /flushdns
```

### 5.2 Find the blocking firewall rule

Open `wf.msc`, go to **Outbound Rules**, and look for enabled rules with action **Block** that mention ICMP. Or list outbound rules from an admin Command Prompt:

```
netsh advfirewall firewall show rule name=all dir=out
```

### 5.3 Remove the block

Disable the blocking rule:

```
netsh advfirewall firewall set rule name="RULE NAME" new enable=no
```

If you want ping to work explicitly, add an allow rule for outbound ICMPv4 echo requests:

```
netsh advfirewall firewall add rule name="Allow ICMPv4 Echo Out" protocol=icmpv4:8,any dir=out action=allow
```

### 5.4 Verify

```
ping 172.16.0.1
ping 8.8.8.8
ping google.com
```

All three should now reply, and the browser should load pages.

## 6. Key takeaways

1. `General failure` is a local problem. Check IP settings, drivers, and the local firewall before blaming cables.
2. Link lights only prove a physical connection. They say nothing about DHCP, DNS, or firewall rules.
3. A `169.254.x.x` address means no DHCP server replied. On a static network, this is normal.
4. A blocked ICMP rule makes ping a misleading test. If ping fails, confirm with a different method, such as `nslookup`, `Test-NetConnection <host> -Port 443` in PowerShell, or opening a site by IP.
5. On managed networks, use the DNS server the network provides. Public DNS is often blocked.
6. Change one thing at a time and record the result of each test. It shortens the search.

## 7. Quick checklist for next time

1. Run `ipconfig /all` and read the IP, gateway, DNS, and DHCP lines.
2. Ping 127.0.0.1, then your own IP, then the gateway, then 8.8.8.8, then a domain name.
3. Test DNS directly with `nslookup <domain> <dns server>`.
4. Review outbound firewall rules for blocks on ICMP or DNS.
5. Swap cable, port, or adapter only after the software checks pass.
