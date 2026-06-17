# Dota2 Keybind Config Sync Tool

> Copy one Steam account's Dota2 keybind config to other accounts with one click,
> with avatar recognition. Supports Steam Web API for nicknames/avatars, HTTP
> proxy, and automatic backup.

English | [简体中文](./README.md)

---

## ✨ Features

- **Account auto-detection**: Scans all numeric folders under `Steam\userdata` and
  resolves nicknames via `loginusers.vdf` (SteamID64) and `localconfig.vdf`. No more
  guessing which folder belongs to which account.
- **One-click keybind sync**: Copies the source account's `570\remote\cfg`
  (`dotakeys_personal.lst`, `config.cfg`, etc.) to target accounts. Supports syncing
  to multiple targets at once.
- **Automatic backup (max 2 copies)**: Before overwriting, the target's original files
  are renamed to `.bak1` / `.bak2`. Repeated operations keep only the 2 most recent
  copies — no pile-up.
- **Avatar loading (optional)**: After entering a Steam Web API key, each account's
  avatar is fetched and shown on the left of the list; missing local nicknames are
  filled from online data too.
- **HTTP proxy**: Built-in proxy settings (default `127.0.0.1:7897`) to fix direct
  Steam API connection failures (`WinError 10061`) in regions where Steam is blocked.
- **Offline-capable**: Core keybind sync works without internet; avatar is optional.
- **Single-file, dependency-light core**: Uses only the Python standard library
  (Pillow needed only for avatars).

---

## 📦 Download & Run

### Option 1: Prebuilt exe (no Python needed)

Download `Dota2CfgSync.exe` from [Releases](../../releases) and double-click to run.

> Windows may warn about an "unknown publisher" on first launch — click
> "Run anyway" (the binary is unsigned).

### Option 2: Run from source

```bash
git clone <repo-url>
cd Dota2_HB_Helper
pip install -r requirements.txt   # Pillow is only needed for avatars
python dota2_cfg_sync.py
```

### Build the exe yourself

```bash
pip install pyinstaller pillow
pyinstaller "Dota2改键同步.spec" --noconfirm
# Output: dist\Dota2改键同步.exe
```

---

## 🚀 Usage

1. **Launch**: The app auto-detects the Steam install dir (via registry); you can also
   click "浏览…" to pick the `userdata` folder manually.
2. **Account list**: Avatars on the left, "nickname / AccountID / has Dota2 config" on
   the right.
3. Choose a **source account** (the one to copy from) and **target account(s)** (to be
   overwritten). Check "同时覆盖多个目标" to multi-select.
4. **Preview** → **Start Sync** → confirm → done.

### Enable avatars (optional)

1. Click "申请key→" to get a free Steam Web API key: <https://steamcommunity.com/dev/apikey>
2. Paste it into the "API Key" field and click "保存" (Save).
3. If direct connection fails, check "启用代理" in the "网络代理" row (Clash default
   port 7897) and save.

---

## 🔧 Config file

At runtime, `config.json` is generated next to the program (**contains private data —
do not share**):

```json
{
  "steam_api_key": "your_key",
  "show_avatar": true,
  "proxy_enabled": false,
  "proxy_host": "127.0.0.1",
  "proxy_port": "7897"
}
```

See [`config.json.example`](./config.json.example) for a template. The file is excluded
in `.gitignore`.

---

## 🛡️ Safety notes

- The tool **only reads/writes local Dota2 cfg files** — it does not touch the game
  itself, replay cache, or cloud-sync data.
- A backup is **always** made before overwriting; restore anytime by renaming
  `xxx.bak1` back to `xxx`.
- The API key is stored in plaintext `config.json` on your machine — keep it safe and
  never share or commit a `config.json` that contains a key.

---

## 🗂️ How it works

The `userdata\<number>` folder name is actually the **AccountID (SteamID3)**, related
to the full SteamID64 by:

```
AccountID  =  SteamID64 - 76561197960265728
```

The program maps folders to accounts via this relation, then copies `570\remote\cfg`
to sync keybinds.

---

## ❓ FAQ

- **Avatar loading fails / `WinError 10061 connection refused`**: You need a proxy in
  regions where Steam is blocked — enable it in "网络代理" with the correct port.
- **An account shows "未知用户" (unknown)**: No local nickname and not fetched online
  — enter an API key and refresh.
- **Target account has no Dota2 config folder**: Launch Dota2 once on that account
  first.
- **How to restore**: Rename `xxx.bak1` back to `xxx` in the target cfg folder.

---

## 📜 License

[MIT License](./LICENSE) — free to use, modify, and distribute, including commercial
and closed-source use; just keep the copyright notice.
