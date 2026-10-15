# Fix: Repeated Network Credential Prompt on Windows

## Problem

Every time the PC is turned off and started again, opening the Bizbox health information system asks for network credentials again. The login is not remembered between restarts.

## Cause

Windows was not holding a saved credential that matched the server address Bizbox uses. Credentials are stored in Credential Manager per user profile and per target name. If the saved entry is missing, saved under a different name, or saved under a different user, Windows asks again.

## Solution: Save the credential in Credential Manager

1. Sign in to Windows as the user who uses Bizbox. The credential is saved per user profile, so the correct account matters.
2. Open Control Panel.
3. Go to Credential Manager.
4. Select Windows Credentials.
5. Click Add a Windows credential.
6. Internet or network address: enter the server IP address or hostname, exactly as Bizbox reaches it.
7. User name: enter `SERVER_IP_OR_NAME\username`.
8. Password: enter the account password.
9. Click OK.
10. Restart the PC and open Bizbox to confirm it no longer asks.

## Command line equivalent

Run in a normal terminal (not as administrator) while signed in as the Bizbox user:

```
cmdkey /add:SERVER_IP_OR_NAME /user:SERVER_IP_OR_NAME\username /pass:YOUR_PASSWORD
cmdkey /list
```

`cmdkey /list` shows all saved credentials so you can confirm the entry exists.

## If Bizbox uses a mapped drive

Map the share with a persistent connection:

```
net use Z: \\SERVER_IP_OR_NAME\sharename * /user:SERVER_IP_OR_NAME\username /persistent:yes
```

Using `*` instead of the password makes Windows ask for it, so it is not stored in the command history.

### `net use` syntax

```
NET USE [devicename | *] [\\computername\sharename [password | *]]
        [/USER:[domainname\]username]
        [/SAVECRED]
        [/DELETE | /PERSISTENT:{YES | NO}]
```

Every part is separated by a space. A common mistake is writing the password and a switch together, for example `password/persistent:yes`. Windows then reads the whole text as the password and authentication fails.

Useful forms:

```
net use                  # list current connections
net use Z: /delete       # remove one mapping
net use * /delete        # remove all mappings
net view \\SERVER_IP_OR_NAME /all    # list shares on the server
```

## Common errors

**Error 1219, multiple connections with different credentials.**
Run `net use * /delete` to clear existing connections, then map again.

**Credential disappears after every shutdown.**
A policy is probably deleting it. Open `gpedit.msc`, go to Computer Configuration, Windows Settings, Security Settings, Local Policies, Security Options, and set "Network access: Do not allow storage of passwords and credentials for network authentication" to Disabled. Then run `gpupdate /force`. On a domain PC, a domain Group Policy can override this on every boot.

**Prompt returns after a password change.**
The saved password is outdated. Update it in Credential Manager.

**Drive shows a red X after boot.**
Windows tried to reconnect before the network was ready. Saving the credential with `cmdkey` as well as using `/persistent:yes` fixes this.

## Notes

1. Do not run `cmdkey` as a different administrator account. The credential is saved to that account's vault, and the Bizbox user will never see it.
2. A dedicated account with access to only the needed share is safer than using the server Administrator account.
3. Never commit real IP addresses, usernames, or passwords to a public repository.
