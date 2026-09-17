# [No Windows booting]

**Date:** 2026-09-16
**Category:** System
**Status:** Resolved

---

## Problem Summary

No windows booting after inserting the external hardrive

---

## Symptoms

* No windows booting

**Error Message (if applicable):**

`[Error message]`

---

## Investigation

Document the troubleshooting process and what you discovered.

### Step 1 — Pulling out External Drive

The external drive is what I suspected since it only happened after plugging it in and not pulling it out after shutting it down

---

## Root Cause

the boot priority is at the usb

---

## Resolution

plugging out the external drive

---

## Lessons Learned

boot priority is at the highest so whenever a flash drive or external drive is plugged, when turning the pc on. it searches for the os but since the os cannot be found, it will be stuck at boot.
