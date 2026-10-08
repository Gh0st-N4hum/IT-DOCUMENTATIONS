# [Lab Computer network error connection]

**Date:** 2026-09-23
**Category:**Netwokk
**Status:** Resolved

---

## Problem Summary

LABORATORY DEPARTMENT REPORTED THAT THEIR COMPUTER CANT ACCESS THE SERVER. USED A BOTTOM TO TOP TROUBLESHOOTING. TCP/IP DIAGNOSTIC. It shows that has an internet but it shows different IP. It was connected wirelessly tho it should be connected through lan cable so that is the culprit that I think.

---

## Symptoms

**Can't connect to the server please check your network**

`[Error message]`
**Can't connect to the server please check your network**
-----------------------------------------------------

## Investigation

Upon navigating to the terminal the computer has different private IP instead of 172 it shows 192.

### Step 1 — [Action Taken]

[IPCONFIG/ALL was entered through the cmd and saw the details for all the network]

**Result:** [realization that 192 became the ip which is different from the servers 172]

**Result:** [checked the ethernet connection]

---

## Root Cause

ethernet connection was not properly connected maybe because of faulty cable.

If the exact cause could not be confirmed:

> **Root cause:** faulty cable
> **Most likely cause:** THE ETHERNET PORT DOES NOT LIGHT UP MEANING IT IS NOT CONNECTED

---

## Resolution

[Describe what was done to resolve the issue.]

1. Re inserted the cable multiple times until the right spot to light up the indicator light.
2. [Resolution step]
3. [Resolution step]

---

## Verification

Describe how you confirmed that the issue was successfully resolved.

* [Re insertion of ethernet] — **PASS**

**Final Status:** Resolved

---

## Lessons Learned

ALWAYS DIAGNOSE FROM BOTTOM TO TOP TCP/IP NETWORK LAYER. MOST LIKELY THAT LAYER 1 IS THE PROBLEM.
