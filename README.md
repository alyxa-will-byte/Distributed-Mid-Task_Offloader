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
### 1. The relay (once, on the KVM 2)
```bash
scp -r relay root@<kvm2>:/root/axr-relay
ssh root@<kvm2> 'cd /root/axr-relay && bash install.sh relay.alyxrbytes.pro'
```
DNS: an A record `relay` → the KVM 2 IP, the same as `qi` and `live`. Back up `/opt/axr-relay/data`: it holds the device list.

### 2. Your laptop (the controller)
```bash
pipx install git+https://github.com/alyxa-will-byte/Distributed-Mid-Task_Offloader
```
Get a code on the KVM 2:
```bash
cd /opt/axr-relay && docker compose exec relay node relay.mjs pair controller
```
Then on the laptop:
```bash
axr join <code>
```

### 3. Each helper
On the laptop, run `axr pair` to get a code. Then on the helper (Windows, Linux or Mac, with Python 3.10+):
```bash
pipx install git+https://github.com/alyxa-will-byte/Distributed-Mid-Task_Offloader
```
```bash
axr join <code>
```
`join` also installs a background service, so the helper starts on its own and stays connected:
- **Windows:** a task that starts at sign-in, with no window.
- **Linux:** a systemd user service.
- **Mac:** a LaunchAgent.

Add `--sandbox docker` on a helper with Docker to run every job in a locked-down container.

## Use
```bash
axr run -- python train.py --epochs 50        # best free helper; results land in ./out
axr run --to old-laptop -- ffmpeg -i in.mov out/out.mp4
axr run --auto -- make -j8                    # offload only if this laptop is busy (CPU > 75% or RAM > 85%)
axr run --image python:3.12 -- python job.py  # inside a container on a Docker helper
axr run --no-local -- ...                     # never fall back to running here
```
Put big or private files you don't want sent in `.axrignore` (same idea as `.gitignore`). `.git`, `node_modules`, virtualenvs and `out/` are never sent.

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
