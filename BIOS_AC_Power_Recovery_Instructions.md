# Enable auto power-on after a power cut — PS-BOSS-OS

**Machine:** PS-BOSS-OS (Tailscale IP 100.86.188.31)
**BIOS:** American Megatrends Inc. (AMI Aptio), version 5.14, dated 07/05/2024
**Why:** right now, if this machine loses power, it stays off when power returns until someone physically presses the power button. This change makes it boot itself automatically. SSH and Tailscale are already set to start automatically once the OS boots, so this one BIOS setting is the only missing piece for full automatic recovery.

## Steps

1. Reboot or power on the machine. As soon as the manufacturer logo appears, press **Del** repeatedly (on most AMI Aptio boards; if that doesn't work, try **F2**) to enter BIOS Setup.
2. Go to the **Advanced** tab.
3. Look for **APM Configuration** (sometimes called **Power Management** or **ACPI Power Management Configuration**).
4. Find the option named one of:
   - **Restore on AC Power Loss**
   - **AC Power Loss**
   - **After Power Failure**
5. Set it to **Power On** (not "Stay Off" and not "Last State" — "Last State" only reboots if the machine was already on when power was cut, which isn't reliable for an unattended server).
6. Press **F10** to save and exit (confirm **Yes** when prompted).

## After this change

- Power returns → BIOS auto-boots the machine → Linux boots → `sshd` and `tailscaled` start automatically (already confirmed enabled) → the machine reappears on the Tailscale network and is reachable by SSH within about a minute, with no manual login or browser step needed.
- No further action is needed on the Linux side — this BIOS setting was the only missing piece.

## If the option isn't under APM Configuration

Some AMI boards place it under **Advanced → Chipset Configuration → Power Management**, or on server boards, under **Server Mgmt**. Search BIOS setup pages for any setting with "AC Power" or "Power Failure" in the name — the exact menu path varies slightly by motherboard model.

## One-time only

This setting is stored in BIOS NVRAM and survives reboots and OS reinstalls (it does not survive clearing CMOS or replacing the motherboard battery) — it only needs to be set once.
