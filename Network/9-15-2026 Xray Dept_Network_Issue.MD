# # [Incident Title]

**Date:** 2026-09-15
**Category:** Network
**Status:** Resolved

---

## Problem Summary

[Briefly describe the reported problem or issue encountered.]
NETWORK CANT ACCESS THE BIZBOX'S SYSTEM UPON CONNECTING TO 172.16.2.1 (IP ADDRESS OF SERVER)
---

## Symptoms

* [Observed symptom or error]
* [Observed symptom or error]
* [Observed symptom or error]

**Error Message (if applicable):**

`[Error message]`

---

## Investigation

IT CANT BE LOGGED IN THROUGH ACCESSING IT IN SMB BY THE IP OF \\172.16.2.1

### Step 1 — [Action Taken]

PINGED THE SERVER'S IP
IT IS REPLYING BUT CAN'T ACCESS THE SMB

### Step 2 — [Action Taken]

ENABLED GUEST LOGON

IT DOES NOT RESOLVE IT

### Step 3 — [Action Taken]

USING NET USE //172.16.2.1 /USER:Administrator
After putting the password, it can now access the server's IP along with the shared folder the server


---

## Root Cause


> **Root cause:** Undetermined
> **Most likely cause:** Unknown

---




**Final Status:** Resolved

---

## Lessons Learned

[What did you learn from this incident?]

[From what I currently know, it looks like the accessing function for the network have a timer or sudden caused of disconnection, so as to resolve this, the administrator must relogged in through the use of net use]
