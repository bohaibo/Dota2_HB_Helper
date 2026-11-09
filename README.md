# Dota2 改键配置同步工具

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![Release](https://img.shields.io/github/v/release/bohaibo/Dota2_HB_Helper?include_prereleases)](https://github.com/bohaibo/Dota2_HB_Helper/releases)
[![Build](https://img.shields.io/github/actions/workflow/status/bohaibo/Dota2_HB_Helper/build.yml?branch=master&label=build)](https://github.com/bohaibo/Dota2_HB_Helper/actions/workflows/build.yml)
[![Downloads](https://img.shields.io/github/downloads/bohaibo/Dota2_HB_Helper/total.svg)](https://github.com/bohaibo/Dota2_HB_Helper/releases)

> 一键把某个 Steam 账号的 Dota2 改键配置复制到其他账号，附带账号头像识别。
> 支持 Steam Web API 拉取昵称/头像、HTTP 代理、自动备份。

[English](./README_EN.md) | 简体中文

---

## ✨ 功能特性

- **账号自动识别**：扫描 `Steam\userdata` 下所有数字目录，通过 `loginusers.vdf`（SteamID64）和 `localconfig.vdf` 反查昵称，告别"哪个文件夹是哪个号"。
- **改键一键同步**：把源账号的 `570\remote\cfg`（`dotakeys_personal.lst`、`config.cfg` 等）复制到目标账号，支持一次同步到多个号。
- **自动备份（最多 2 份）**：覆盖前把目标原文件改名为 `.bak1` / `.bak2`，连续操作也只保留最近 2 份历史，不会堆积。
- **头像加载（可选）**：填入 Steam Web API key 后，自动拉取各账号头像并显示在列表左侧；昵称也会用线上数据补全本地缺失。头像优先从本地缓存读取，命中缓存时无需联网。
- **HTTP 代理**：内置代理设置（默认 `127.0.0.1:7897`），解决国内直连 Steam API 失败（`WinError 10061`）的问题。
- **离线可用**：不联网也能完成核心的改键同步；头像有缓存时亦可离线显示。
- **单文件 / 免依赖核心**：仅依赖 Python 标准库（头像功能需 Pillow）。

---

## 📦 下载与运行

### 方式一：直接用打包好的 exe（无需装 Python）

到 [Releases](../../releases) 下载 `Dota2CfgSync.exe`，双击运行。

> 首次运行 Windows 可能提示"未知发布者"，点"仍要运行"即可（未做代码签名）。

### 方式二：从源码运行

```bash
git clone <仓库地址>
cd Dota2_HB_Helper
pip install -r requirements.txt   # 仅头像功能需要 Pillow
python dota2_cfg_sync.py
```

### 从源码自行打包 exe

```bash
pip install pyinstaller pillow
pyinstaller "Dota2改键同步.spec" --noconfirm
# 产物在 dist\Dota2改键同步.exe
```

---

## 🚀 使用流程

1. **启动**：程序自动探测 Steam 安装目录（读注册表），也可手动点「浏览…」选 `userdata` 文件夹。
2. **账号列表**：左侧显示头像，右侧显示「昵称 / AccountID / 是否有 Dota2 配置」。
3. **选源账号**（复制改键的号）和**目标账号**（被覆盖的号）。勾选「同时覆盖多个目标」可多选。
4. 可点击「设为默认源账号」，下次启动自动选中该账号。
5. **预览** → **开始同步** → 二次确认 → 完成。也可直接点击「同步到所有其他账号」，自动将当前源账号的配置复制给列表中的其他所有账号（包括尚无 Dota2 配置的账号），无需逐个选择目标。覆盖前仍会确认并备份。

### 启用头像功能（可选）

1. 点「申请key→」去 Steam 官方免费申请 Web API key：<https://steamcommunity.com/dev/apikey>
2. 把 key 填入「API Key」输入框，点「保存」。
3. 如直连失败，在「网络代理」行勾选「启用代理」（Clash 默认端口 7897），保存即可。

---

## 🔧 配置文件

源码运行时设置保存在项目目录的 `config.json`；打包运行时保存在 exe 同目录的 `config.json`，头像保存在同目录的 `avatar_cache` 文件夹。API Key、头像显示开关、代理、userdata 路径和默认源账号会在重启后保留，打包版升级也不会因临时解压目录变化而丢失。头像有缓存时可离线显示。

首次启动会自动迁移旧用户目录 `%LOCALAPPDATA%\Dota2CfgSync` 中的 `config.json` 和头像缓存，不覆盖已有新数据。配置写入失败会显示错误提示。API Key 为本机明文存储，**含个人隐私，请勿分享**。

配置示例：

```json
{
  "steam_api_key": "你的key",
  "show_avatar": true,
  "userdata_dir": "C:\\Program Files (x86)\\Steam\\userdata",
  "default_source_account": "你的AccountID",
  "proxy_enabled": false,
  "proxy_host": "127.0.0.1",
  "proxy_port": "7897"
}
```

参考模板见 [`config.json.example`](./config.json.example)。该文件已在 `.gitignore` 中排除。

---

## 🛡️ 安全说明

- 工具**只读写本地 Dota2 cfg 文件**，不修改游戏本体、不碰录像缓存/云同步数据。
- 覆盖前**必然备份**，可随时手动把 `.bak1` 改回原名还原。
- API key 存在本机明文 `config.json`，请妥善保管；切勿把含 key 的 `config.json` 分享或提交到 git。

---

## 🗂️ 工作原理

Steam 的 `userdata\<数字>` 目录名其实是 **AccountID（SteamID3）**，与完整 SteamID64 的关系：

```
AccountID  =  SteamID64 - 76561197960265728
```

程序据此把目录映射到账号，再复制 `570\remote\cfg` 完成改键同步。

---

## ❓ 常见问题

- **头像加载失败 / `WinError 10061 积极拒绝`**：国内需走代理，在「网络代理」勾选启用并填对端口。
- **某个账号显示「未知用户」**：该号本地无昵称信息且未联网拉取，填 API key 重新刷新即可。
- **目标账号没有 Dota2 配置目录**：同步时会自动创建；源账号需已有 Dota2 配置。
- **怎么还原**：把目标 cfg 目录里的 `xxx.bak1` 改回 `xxx` 即可。

---

## 📜 开源协议

[MIT License](./LICENSE) —— 可自由使用、修改、分发，包括商业闭源用途，仅需保留版权声明。
