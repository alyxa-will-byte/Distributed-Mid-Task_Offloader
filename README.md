# axr-offload

Send heavy work from your laptop to your other devices (an old laptop, a Mac, the KVM 2, the GPU box), so the laptop stays cool.

```
laptop:  axr run -- python render.py ──wss──┐
                                             ├── relay.alyxrbytes.pro   (routes + keeps the device list; cannot read jobs)
helper:  axr worker (background service) ───┘
```

- **Helpers need none of your software.** Each job carries its folder with it. It runs in a throwaway workspace on the helper, its output streams back live, `out/` is copied back to your laptop, and the workspace is deleted.
- **Pair once, stay paired.** A device gets a permanent key when it joins. It reconnects by itself after reboots, sleep, Wi-Fi drops and relay restarts. Only the pairing code expires (10 minutes, one use).
- **You can cut any device off at any time:** pause, disconnect or remove it (see below).
- **End-to-end encrypted.** Jobs are sealed between your laptop and the helper (NaCl box). The relay only ever sees ciphertext.
- **No open ports.** Every device connects out to the relay.

## What it can and can't offload
It runs **commands**: Python and Node scripts, ffmpeg, Blender CLI renders, builds, data jobs, and container images. Python jobs with a `requirements.txt` get their packages installed on the helper by `uv` into a cache, not system-wide. A job only goes to a helper that has the program it needs, so an `ffmpeg` job skips helpers without ffmpeg. `--os windows` keeps Windows-only programs on Windows helpers.

It can't move an app that's already running on your laptop to another machine. No operating system can do that. For whole desktop apps the plan is a remote workstation: the app runs on a helper and you see it over remote desktop (a later phase).

## Set up
### Install on any device (one line)
Windows (PowerShell):
```powershell
irm https://alyxrbytes.pro/axr.ps1 | iex
```
Mac / Linux:
```bash
curl -fsSL https://alyxrbytes.pro/axr.sh | sh
```
No admin rights, no Python needed beforehand, about 10–30 seconds. The script:
1. installs `uv` (a fast Python installer that also fetches Python if the machine has none)
2. downloads the `axr` package and checks its SHA-256 against the value baked into the script, refusing anything that doesn't match
3. installs it for this user only
4. opens the setup screen

The setup screen scans the machine (CPU, RAM, GPU, tools) and asks for the six-word pairing code. It catches typos ("did you mean 'river'?"), pairs the machine and starts the background service:
- **Windows:** starts whenever you sign in (a per-user sign-in entry, so no admin rights needed), with no window.
- **Linux:** a systemd user service, falling back to cron when there's no systemd session.
- **Mac:** a LaunchAgent.

With a code ready, skip the question:
```powershell
$env:AXR_CODE='amber-oak-river-sage-tide-wren'; irm https://alyxrbytes.pro/axr.ps1 | iex
```
```bash
curl -fsSL https://alyxrbytes.pro/axr.sh | AXR_CODE=amber-oak-river-sage-tide-wren sh
```
Run `axr` any time to reopen the setup screen: check the connection, reinstall the service, or unlink.

### The relay (once, on the KVM 2)
```bash
scp -r relay root@<kvm2>:/root/axr-relay
ssh root@<kvm2> 'cd /root/axr-relay && bash install.sh relay.alyxrbytes.pro'
```
DNS: an A record `relay` → the KVM 2 IP, the same as `qi` and `live`. Back up `/opt/axr-relay/data`: it holds the device list.

### Your laptop first
Get a controller code on the KVM 2:
```bash
cd /opt/axr-relay && docker compose exec relay node relay.mjs pair controller
```
Run the installer on the laptop and enter that code. After that, make codes for helpers on the laptop with `axr pair`.

### Publishing a new version
```bash
python installer/build.py
```
This builds `dist/site/` (the package and both scripts, with the fingerprint filled in). Copy it to `axr/` in the alyxrbytes.pro repo.

## Use
```bash
axr run -- python train.py --epochs 50        # best free helper; results land in ./out
axr run --to old-laptop -- ffmpeg -i in.mov out/out.mp4
axr run --auto -- make -j8                    # offload only if this laptop is busy (CPU > 75% or RAM > 85%)
axr run --image python:3.12 -- python job.py  # inside a container on a Docker helper
axr run --no-local -- ...                     # never fall back to running here
```
Put big or private files you don't want sent in `.axrignore` (same idea as `.gitignore`). `.git`, `node_modules`, virtualenvs and `out/` are never sent.

## Remote desktop (Windows helpers)
Uses RustDesk with your own server on the KVM (`/opt/rustdesk`, `rd.alyxrbytes.pro`), so sessions never go through anyone else's servers.
```bat
axr desktop setup TronaldsDump --key <contents of /opt/rustdesk/data/id_ed25519.pub>
axr desktop viewer
axr desktop TronaldsDump
axr desktop list
```
- **`setup`** makes the helper download the official RustDesk installer and show **one admin prompt on its own screen**. Someone has to click Yes; Windows doesn't allow any program to approve that remotely. The helper then:
  - installs RustDesk as a service, so it works at the login screen and after restarts
  - points it at your server
  - sets a random permanent password
  - sends the RustDesk ID and password back over the encrypted channel

  The controller stores them in `desktops.json`, readable only by you. The key and server are remembered after the first use.
- **`viewer`** installs and configures RustDesk on the controller (one admin prompt there).
- **`axr desktop <helper>`** opens the session with the password filled in.
- **Unlinking a helper** (`axr devices remove` or `axr leave`) also turns its remote desktop off within 5 minutes. A small system task changes RustDesk's password to an unknown one and disables the service once the pairing is gone.
- **Mac and Linux helpers:** install RustDesk by hand for now, since it needs sudo or a privacy approval.

## Devices
```bash
axr devices                       # online / offline (last seen), paused, load, jobs
axr devices pause old-laptop      # stays paired, gets no new jobs
axr devices resume old-laptop
axr devices disconnect old-laptop # drop its connection now and cancel its jobs; it reconnects later
axr devices remove old-laptop     # unlink: it deletes its keys, cache and service. Pair again to reuse it.
```
If a device is off when you remove it, it's refused and wipes itself the next time it connects. On a helper, `axr leave` unlinks it from that end. The relay operator can do the same on the server with `node relay.mjs devices | pause | resume | disconnect | remove <name>`.

## Security
- Only devices you paired can connect. Only controllers can send jobs or manage devices. A helper accepts work only from a controller.
- A helper runs whatever your laptop sends it, so pair only machines you own. Process mode gives each job its own folder, a cleaned environment, a timeout and a whole-process-tree kill. It isn't a security boundary. Use `--sandbox docker` on a helper for real isolation: capabilities dropped, no-new-privileges, memory/CPU/process limits, and `--no-network` per job.
- Device keys are kept in the OS credential store when `keyring` is installed (`pipx install "axr-offload[keyring] @ git+..."`). Otherwise they're kept in a file that only this user can read.
- The relay keeps only public keys and its own signing key. Devices pin that key when they pair, so a fake relay can't tell them to wipe themselves.

## Develop
```bash
cd relay && npm ci && cd ..
uv venv && uv pip install -e ".[test]"
python -m pytest
```
The tests start a real relay and real helper processes. They cover pairing, running jobs, restarts, pause, disconnect and removal (online and offline).
