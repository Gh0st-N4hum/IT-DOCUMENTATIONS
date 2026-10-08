
# Bizbox HIS 8 Transaction Slip Preview/Print Fails with HTTP 401 on One PC

**Date:** 2026-10-08
**Category:** Software
**Status:** Monitoring

---

## Problem Summary

On a single workstation, Preview and Print on the transaction slip in Bizbox Health Information System 8 fail with an HTTP error. Other computers in the hospital run the same function without problems.

---

## Symptoms

* Preview and Print on the transaction slip show an error dialog
* The issue occurs on one PC only, other PCs work normally
* Adding the report server credential in Credential Manager also failed

**Error Message (if applicable):**

`Please check the Global Settings > Report Server URL. The request failed with HTTP status 401: Unauthorized.`

`Credential Manager Error: This information cannot be saved. Make sure the information is correct and that all required fields are completed.`

---

## Investigation

### Step 1: Interpreted the error

A 401 means the report server was reached but rejected the credentials. Since other PCs work, the server and the report service are healthy. The difference is on this PC.

**Result:** Narrowed the problem to authentication on the affected workstation, not the network or the server.

### Step 2: Opened Credential Manager to add the report server login

Tried to add a Windows credential using the full URL `http://iscmmh-serverdb/ReportServer`, user name `Administrator`, and the server password.

**Result:** Credential Manager rejected it with "This information cannot be saved."

### Step 3: Identified the cause of the save failure

Credential Manager only accepts a host name or network address in the address field. A full URL with `http://` and a path is invalid input. The user name was also entered without the server name, so Windows could treat it as the local Administrator account of the PC instead of the server's account.

**Result:** Corrected the entry format (see Resolution).

---

## Root Cause

> **Root cause:** Not yet confirmed
> **Most likely cause:** This PC had no valid saved credential for the report server `iscmmh-serverdb`, so SQL Server Reporting Services returned 401 when the HIS requested the report. The first attempt to add the credential failed only because it was entered in the wrong format.

---

## Resolution

1. Open Control Panel > Credential Manager > Windows Credentials > Add a Windows credential.
2. Enter the address as the host name only: `iscmmh-serverdb`
3. Enter the user name with the server prefix: `iscmmh-serverdb\Administrator`
4. Enter the same password used on the working PCs and save.
5. Close the HIS completely and reopen it.
6. Retry Preview and Print on the transaction slip.

---

## Verification

* Credential saved without error: **PENDING**
* Preview on transaction slip works after reopening HIS: **PENDING**
* Print on transaction slip works: **PENDING**
* Browser test of `http://iscmmh-serverdb/ReportServer` on this PC: **PENDING**

**Final Status:** Monitoring (update to Resolved once the tests above pass)

---

## Lessons Learned

A 401 is an authentication problem, not a connectivity problem. The request reached the server, so the cause is almost always credentials or identity. When only one PC fails, compare what is unique to that machine: saved credentials, login account, and settings.

Credential Manager takes a host name only, never a full URL. A user name without a machine prefix can be matched against the local PC account, so use `SERVERNAME\username` when the account lives on the server.

Testing the same URL in a browser separates an application settings problem from an account or server problem in under a minute.
