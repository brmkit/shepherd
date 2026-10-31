# shepherd

Deploy Proxmox VMs directly from Herdr. Choose a template, **shepherd** clones and configures it to connects the machine to your workspace.

![shepherd](preview.png)

## Requirements

- macOS or Linux with Herdr running, `hc`, Python 3.9+, and OpenSSH.
- Proxmox API token and a local SSH key pair.
- A Debian/Ubuntu-compatible QEMU template with QEMU Guest Agent and reachable networking.

## Install

```sh
python3 install.py
```

To update: `python3 install.py --update`.

Open **shepherd** beside Files/Changes, add your hypervisor, and select **Deploy VM**. Linked clones are enabled by default.

**shepherd** runs local Python/SSH/`hc` commands and uses the Proxmox API for privileged guest setup. Credentials and job state stay in `~/.config/herdr-app/data/shepherd/` (or your `HERDR_APP_HOME`). SSH uses your configured key files and no guest password or sudo access is created.

Implementation and build details: [RECIPE.md](RECIPE.md).
