<div align="center">

# Glimpse

**Watch and drive the Claude Code / Codex sessions running on your laptop and dev boxes — from your phone.**

Live output · reply and approve from your phone · push notifications on done / error / question · multi-machine session tabs

**English** · [简体中文](README.zh-CN.md)

[Quick start](#quick-start) · [How it works](#how-it-works) · [Configuration](#configuration) · [Security](#security) · [FAQ](#faq)

![python](https://img.shields.io/badge/hub-python%203.9%2B-3776AB?logo=python&logoColor=white)
![platform](https://img.shields.io/badge/hub-macOS%20%7C%20Linux-lightgrey)
![client](https://img.shields.io/badge/client-iPhone%20%7C%20Android%20%7C%20desktop%20PWA-black)
![deps](https://img.shields.io/badge/remote%20deps-none-brightgreen)

<img src="docs/img/home.png" width="22%" alt="Session list"> <img src="docs/img/approval.png" width="22%" alt="Approve from the phone"> <img src="docs/img/chat.png" width="22%" alt="Chat view"> <img src="docs/img/terminal.png" width="22%" alt="Terminal view">

</div>

## Why

You leave Codex running on your Mac and Claude Code on a dev box, then go out for lunch. How far along are they? Is one stuck on a permission prompt? Did one crash?

Glimpse turns your phone into a remote control for those sessions. What you see on the phone is their real terminal, not a separate sandbox.

## Features

- **Session tabs across machines**: every session from every machine in one list. Each shows the machine id (M1, M2…), task name, working directory, live status and the latest reply.
- **Two views**:
  - **Chat** renders the Claude Code / Codex transcript as chat bubbles, with expandable tool calls and output.
  - **Terminal** is the full terminal screen, rendered with xterm.js.
- **Type from the phone**: messages are submitted to the session's process exactly as if typed. Quick keys cover Esc, ^C, Tab, the arrow keys, 1/2/3 and more.
- **TUI menus in chat view**: when `/model`, `/effort` or another interactive picker opens in the terminal, chat view shows it as a menu card with large tappable rows (current choice and cursor marked), plus Esc and, for Claude, "s · this session only". A "/" button in the composer types common slash commands (`/model`, `/effort`, `/compact`, `/clear`, `/status`).
- **One-tap approvals**: when Claude Code asks for permission, an approval card offers Allow, Always allow, Deny or Handle in terminal. This uses the official `PermissionRequest` hook, not simulated key presses.
- **Answer questions**: `AskUserQuestion` options show up as buttons.
- **Push notifications** through standard Web Push when a turn finishes, the agent stops on an error, it needs approval or an answer, or the process exits.
- **Swap key and restart**: when an API key runs out of credit, pick another one on the phone. Glimpse stops the old process, starts a new one and resumes the conversation (`claude --resume` / `codex resume`).
- **Model & mode controls**:
  - The pickers show the real lists from each machine. Codex models and their efforts come from `codex debug models`; Claude models are default / opus / sonnet / haiku / fable / opusplan, with the custom names from your `--settings` file. Lists are cached for 10 minutes.
  - Claude Code: permission mode (manual / acceptEdits / auto / plan / dontAsk / bypassPermissions), `/model`, effort (low / medium / high / xhigh / max / ultracode) and `/goal`. `ultracode` starts with `--effort xhigh` and then runs `/effort ultracode`.
  - Codex: model, reasoning effort (low / medium / high / xhigh / max / ultra, as supported by the model), approval policy and sandbox. This restarts Codex and resumes the conversation.
  - All of these can also be chosen when creating a session.
- **Continue a conversation started on the computer**: the "adopt" sheet lists recent Claude / Codex conversations on a machine. Glimpse resumes the one you pick under its control.
- **Delete sessions**: swipe a card left, or use 会话操作 → 删除. A running process is stopped first. There's also one-tap cleanup of finished sessions.
- **Manage machines from the phone**: add, edit, remove and test SSH machines. The hub's existing SSH keys are used, and host names are validated.
- **Widgets**: network latency and throughput, token usage (including **today's total** per machine, counted from all Claude / Codex transcripts on it), context usage, model & mode, machine CPU and memory, and session counts. Each can be toggled and reordered.
- **Sessions outlive every connection**: an agent daemon owns the PTYs, the same idea as tmux. Restarting the hub, dropping SSH or locking the phone never stops the AI process. Output resumes from the exact byte offset on reconnect.
- **Laptop and phone together**: `./glimpse run claude` attaches your laptop terminal to the same session. `Ctrl-]` detaches.
- **Light / dark**: Settings → 外观 switches between follow system, light and dark. The terminal view stays dark.

<p align="center"><img src="docs/img/controls.png" width="22%" alt="Model & mode"> <img src="docs/img/adopt.png" width="22%" alt="Continue a conversation"> <img src="docs/img/restart.png" width="22%" alt="Key"> <img src="docs/img/home-light.png" width="22%" alt="Light"></p>

## How it works

```
 phone / any browser (installed as a PWA)
    │  HTTPS + WebSocket over Tailscale (WireGuard, peer-to-peer whenever NAT traversal succeeds)
    ▼
 hub (an always-on computer, hub/server.py)
    │  one stdio JSON channel per machine: a local subprocess, or ssh for dev boxes (uses ~/.ssh/config)
    ▼
 agent (agent/glimpse_agent.py, Python standard library only)
    └─ runs claude / codex inside PTYs and reads their hooks and transcripts
```

- **Why Tailscale instead of port forwarding or a relay server**: your phone and computer are on the same tailnet and connect directly. Nothing sits in between, latency is roughly the network round trip, and no public port is opened. Tailscale also issues free `*.ts.net` HTTPS certificates, and iOS Web Push requires HTTPS.
- **Nothing to install on dev boxes**: the hub sends the agent source over ssh stdin and `exec`s it in memory, so the agent code is never installed there. The only files it writes are per-session temporary Claude settings files (`0600`, in a private temp dir, deleted when the process exits). The remote only needs `python3` (3.8+) and passwordless ssh. It doesn't need Tailscale either.
- **State detection**:
  - **Claude Code**: hooks injected at launch (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `Notification`, `Stop`, `StopFailure`, …). Content comes from the transcript JSONL.
  - **Codex**: the rollout JSONL (`task_started`, `task_complete`, `token_count`, …) plus the terminal's OSC 9 notifications.
- **Only a browser on the phone**: no app store, profile or certificate to install, no jailbreak, no sideloading.

## Quick start

### Requirements

| Device | Needs |
| --- | --- |
| hub (an always-on macOS or Linux computer) | Python 3.9+ and [Tailscale](https://tailscale.com/download), with MagicDNS and HTTPS Certificates enabled in the admin console |
| each dev box | `python3`, and passwordless `ssh <host>` from the hub |
| phone / other devices | the Tailscale app, signed in to the same tailnet. iPhone needs iOS 16.4+ for push; Android and desktop Chrome / Edge / Safari work too |

### 1. Install

```sh
git clone https://github.com/lyfeng001/Glimpse.git
cd Glimpse
./glimpse setup            # creates .venv, installs dependencies, creates config.json from config.example.json
```

### 2. Configure

Edit `config.json` (every field is documented under [Configuration](#configuration)):

- `hub.listen` and `hub.tailscale_hostname` default to `"auto"`, which uses this computer's Tailscale IP and MagicDNS name.
- `keys` and `machines`: your API keys and the machines to monitor. Machines can also be added later from the phone.

### 3. Run the hub

```sh
./glimpse hub                    # foreground; the first start fetches an HTTPS certificate via `tailscale cert`
scripts/install_launchd.sh       # optional (macOS): start at login and restart if it dies
```

### 4. Open it on the phone

1. Open the Tailscale app and stay connected.
2. On the computer, run `./glimpse url` and open the printed link in the phone's browser. The link contains your access token, so don't share it.
3. Install it as an app (see [Use it as an app](#use-it-as-an-app)), and from then on open Glimpse from its icon.
4. In ⚙︎ Settings, tap **Enable notifications** and allow them, then tap **Send test notification** to check.

### 5. Start a session

- **From the phone**: tap ＋ and pick a machine, a type (Claude Code / Codex / Shell), a key and a working directory.
- **From the computer**: run `./glimpse run claude` or `./glimpse run codex`. Your terminal attaches to the same session:
  - `Ctrl-]` detaches.
  - `./glimpse attach <id>` re-attaches, and plain `./glimpse attach` picks the latest live session. On the hub computer this also works for sessions on other machines (`./glimpse attach M2:<id>`), through the hub. The phone's 会话操作 sheet shows the exact command and a deep link for any other browser.
  - `./glimpse ls` lists all sessions.
  - Arguments after `--` go to the tool unchanged, e.g. `./glimpse run claude -- --model sonnet`.
  - It uses the machine's preset from `config.json` (argv and default key, the same as the phone). `-k/--key NAME` picks another key, `--key none` uses none. It prints the key name, never its value.
  - `./glimpse run claude --resume <id>` continues a conversation. If a Glimpse session already holds it, you attach to that session (restarted in place if it had exited) instead of getting a second copy.

- **Shell integration (keep typing your usual commands)**: add one line to `~/.zshrc` (or `~/.bashrc`) on the hub computer:

  ```sh
  eval "$(/path/to/Glimpse/glimpse shell-init)"
  ```

  From then on the plain commands run as Glimpse sessions, with your terminal attached and the session visible on the phone:

  ```sh
  codex resume 01a0edaf-75de-7bd2-b706-c991c4118ecb --no-daemon
  claude --resume <id>
  claude
  ```

  - The machine preset is applied: Codex gets the preset key exported, Claude launches with the preset settings file. `GLIMPSE_KEY=key2 codex ...` picks another key for one run.
  - A conversation that already has a Glimpse session is attached to instead of being started twice.
  - Non-interactive uses (`claude -p`, `codex exec`, `--version`, sub-commands like `mcp`, pipes and scripts) run the real program untouched. `GLIMPSE=0 codex ...` or `command codex ...` always bypasses Glimpse.

> A `claude` / `codex` you started directly in another terminal can't be attached as-is, because Glimpse doesn't own that terminal. To continue it, quit it there and pick its conversation in **接管电脑上的会话** on the phone. Glimpse then resumes it with `--resume` / `codex resume`.

## Use it as an app

Glimpse is a Progressive Web App: once installed, it opens full-screen from its own icon with no browser bars, and it can receive push notifications.

| Device | How to install |
| --- | --- |
| iPhone / iPad | open the link in Safari → **Share → Add to Home Screen**. Push needs iOS 16.4+ and must be enabled from the installed app |
| Android | Chrome → **⋮ → Install app** (Glimpse also shows an **Install** button) |
| Mac / Windows / Linux | Chrome or Edge: the install icon in the address bar. Safari on macOS: **File → Add to Dock** |

There is no App Store build. The installed web app *is* the app, and it works the same on every platform, including any computer on your tailnet.

## Configuration

Every setting, including secrets such as API keys, lives in one file: `config.json`. It is git-ignored and mode `0600`. [`config.example.json`](config.example.json) in the repo is the template.

```jsonc
{
  "hub": {
    "listen": [{"host": "auto", "port": 8443}, {"host": "127.0.0.1", "port": 8443}],   // auto = this machine's Tailscale IP
    "tailscale_hostname": "auto",    // or your-mac.your-tailnet.ts.net
    "caffeinate": true,              // macOS: keep the machine from idle-sleeping while the hub runs
    // "listen": [{"host": "auto", "port": 8443}], "tailscale_hostname": "auto"   <- the defaults
    "push_subject": "mailto:you@example.com",
    "approval_wait_seconds": 600     // unanswered approvals go back to the terminal prompt after this long
  },
  "keys": {
    // one key value; the hub maps it to the right variable per session type
    "env": {"claude": "ANTHROPIC_AUTH_TOKEN", "codex": "OPENAI_API_KEY"},
    "profiles": [{"name": "key1", "value": "sk-..."}]
  },
  "machines": [
    {"id": "M1", "name": "mac", "type": "local", "default_cwd": "~/code",
     "presets": {
       "claude": {"argv": ["claude", "--settings", "~/.claude/my-settings.json"], "key": null},
       "codex":  {"argv": ["codex"], "key": "key1"}
     }},
    {"id": "M2", "name": "devbox", "type": "ssh", "host": "devbox", "default_cwd": "~"}
  ]
}
```

**How keys are injected**

- **Claude Code** always starts with a `--settings` file. Glimpse reads your own settings file from the preset and writes the selected key into its `env`. It then adds Glimpse's hooks and saves the result as a temporary file (`0600`, deleted when the process exits). The key never appears in the process environment or on the command line.
- **Codex** gets the key as an environment variable (the equivalent of `export OPENAI_API_KEY=...`), named in `keys.env.codex`. The variable is set after your shell rc files run, so a stale key exported in `~/.bashrc` can't override it. If your Codex setup needs extra flags (e.g. `--no-daemon`), add them to `argv`.

## Security

- **Network**: the hub listens only on the addresses in `hub.listen` (by default the Tailscale IP and `127.0.0.1`), so it is unreachable from the LAN and the internet. Every request also needs a 192-bit random access token (`data/token`):
  - it is stored in an `HttpOnly; Secure; SameSite=Strict` cookie and compared in constant time;
  - WebSocket connections from another origin are rejected;
  - the token is never written to the log.
- **Keys**: the browser only ever sees key names. Key values live in the hub's `config.json` (written atomically, `0600`) and in the temporary launch files. Launch command lines are not sent to the phone either.
- **Agent socket**: the Unix socket is mode `0600`. On Linux, abstract sockets check `SO_PEERCRED` in both directions, so only the same uid can connect and a socket squatted by another user is refused.
- **Adding machines from the phone** only accepts plain SSH host names or aliases. The hub connects with `BatchMode=yes` and its own keys, never with passwords.
- **Files**: everything under `data/` (token, VAPID private key, push subscriptions, TLS key) is `0600`.
- **Revoking access**: delete `data/token`, restart the hub, and log in again with the new `./glimpse url`.

## FAQ

<details><summary><b>Do I install something on the phone or on the computer?</b></summary>

The project runs on the computer (the hub). The phone only needs the Tailscale app and a browser, and dev boxes need nothing at all.
</details>

<details><summary><b>Only iPhone? What about Android or another computer?</b></summary>

Any device with a modern browser that is on your tailnet works: Android, iPad, or another Mac / Windows / Linux computer. Open the same link and log in with the token. Push notifications work on Android Chrome and on desktop Chrome, Edge and Safari as well.
</details>

<details><summary><b>What does the logo mean?</b></summary>

A winking face drawn with terminal glyphs: `>` and a dot as the eyes, the cursor line bent into a smile. A friendly *glimpse* at your terminal from wherever you are.
</details>

<details><summary><b>Is it safe for my iPhone?</b></summary>

Yes. It's a web page added to the Home Screen. There's no profile, certificate, jailbreak or sideloading involved, and deleting the icon removes it completely.
</details>

<details><summary><b>Notifications don't arrive</b></summary>

- You can only enable notifications in Settings when the page was opened from the Home Screen icon.
- Notifications must be allowed in iPhone Settings → Notifications → Glimpse.
- The hub must be on HTTPS: `tailscale_hostname` is set and the certificate was issued.
- While Glimpse is open in the foreground you get an in-app banner instead of a system notification.
</details>

<details><summary><b>Does it keep working when I close my laptop lid?</b></summary>

While the hub runs it holds `caffeinate -i` to prevent idle sleep. Closing the lid still sleeps most Macs unless they're on power with an external display. If you have an always-on Linux box, run the hub there instead.
</details>

<details><summary><b>The approval card vs. the dialog in the terminal — which one wins?</b></summary>

Whichever you answer first. If you tap Allow or Deny on the phone, Claude gets that decision directly. The request goes back to Claude's own dialog if you tap **Handle in terminal**, press any key in the terminal, or leave it unanswered for `approval_wait_seconds`.
</details>

## Development

```sh
./glimpse test                   # unit tests + hub integration tests (incl. the RFC 8291 Web Push vectors)
./glimpse hub -v                 # verbose logging
GLIMPSE_DEBUG=1 ./glimpse hub    # the agent logs hook events and launch commands to data/agent/agent.log
```

```
agent/glimpse_agent.py   PTY daemon, hook relay, transcript parsing, CLI (stdlib only, runs in-memory on remotes)
hub/server.py            HTTPS + WebSocket server, auth, push, machine management
hub/machines.py          stdio channel to each machine (local subprocess / ssh)
hub/webpush.py           Web Push (RFC 8291 encryption + RFC 8292 VAPID), no third-party push library
hub/config.py            configuration loading
hub/attach_client.py     `./glimpse attach` through the hub (sessions on other machines)
web/                     the PWA (vanilla JS + xterm.js, no build step); web/icon.svg is the master logo (PNGs are generated from it)
```

## Acknowledgements

- [xterm.js](https://xtermjs.org/) (MIT) renders the terminal on the phone
- [Tailscale](https://tailscale.com/) provides the network and HTTPS certificates
- The interaction design was inspired by [Moshi](https://getmoshi.app/) and [Happier](https://github.com/happier-dev/happier)
