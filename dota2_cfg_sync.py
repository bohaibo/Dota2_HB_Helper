# -*- coding: utf-8 -*-
"""
Dota2 改键配置同步工具
======================
把一个 Steam 账号的 Dota2 改键配置 (570\\remote\\cfg) 复制到其他账号。

特性:
  - 自动识别 userdata 下各数字目录对应的 Steam 账号 (昵称)
  - 优先读 loginusers.vdf (SteamID64 -> AccountID), 兜底读 localconfig.vdf
  - 可选: 用 Steam Web API 拉取头像并显示在账号列表中 (需 API key + Pillow)
  - 同步前自动备份目标文件 (改名 .bak1/.bak2), 最多保留 2 份
  - 支持一次同步到多个目标账号

运行: `python dota2_cfg_sync.py`
头像功能: `pip install -r requirements.txt` (装 Pillow)
"""

import json
import os
import re
import shutil
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from tkinter import ttk, filedialog, messagebox

# --- 软依赖: Pillow (用于头像解码/缩放) -----------------------------------

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# --- 常量 ----------------------------------------------------------------

# SteamID64 -> AccountID 的偏移量 (SteamID32 = SteamID64 - 76561197960265728)
STEAMID64_BASE = 76561197960265728

# Dota2 的 Steam App ID 对应的 userdata 子目录
DOTA2_APP_ID = "570"

# 备份后缀
BAK_SUFFIXES = [".bak1", ".bak2"]

# Dota2 cfg 目录相对 userdata\<id> 的路径
CFG_REL = os.path.join(DOTA2_APP_ID, "remote", "cfg")

# 脚本所在目录 (用于存 config.json / avatar_cache)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 配置文件 + 头像缓存目录
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
AVATAR_CACHE_DIR = os.path.join(SCRIPT_DIR, "avatar_cache")

# Steam Web API
STEAM_API_SUMMARY_URL = (
    "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
)
STEAM_API_KEY_URL = "https://steamcommunity.com/dev/apikey"

# 列表里头像缩略图尺寸 (像素)
AVATAR_SIZE = 32


# --- Steam 路径探测 ------------------------------------------------------

def detect_steam_userdata_dir():
    """尝试自动定位 Steam 的 userdata 目录。

    优先读注册表 HKCU\\Software\\Valve\\Steam\\SteamPath, 再回退默认安装路径。
    返回 userdata 完整路径 (字符串), 找不到返回 None。
    """
    steam_path = None
    try:
        import winreg
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"
            ) as key:
                steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
        except FileNotFoundError:
            pass
    except ImportError:
        pass  # 非 Windows 环境 (开发/调试)

    if not steam_path or not os.path.isdir(steam_path):
        for cand in (
            r"C:\Program Files (x86)\Steam",
            r"C:\Program Files\Steam",
        ):
            if os.path.isdir(cand):
                steam_path = cand
                break

    if not steam_path:
        return None

    # 注册表里的 SteamPath 用正斜杠 (如 c:/program files (x86)/steam),
    # 规范化为系统原生路径, 避免和后续 os.path.join 混搭出正反斜杠混杂。
    steam_path = os.path.normpath(steam_path)
    # 规范盘符大小写: c:\... -> C:\...
    if len(steam_path) >= 2 and steam_path[1] == ":":
        steam_path = steam_path[0].upper() + steam_path[1:]

    userdata = os.path.join(steam_path, "userdata")
    return userdata if os.path.isdir(userdata) else None


# --- VDF 轻量解析 --------------------------------------------------------

def _read_text(path):
    """容错读取 VDF 文本 (Steam 文件多为 UTF-8 / UTF-8-BOM)。"""
    for enc in ("utf-8-sig", "utf-8", "mbcs", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, OSError):
            continue
    return ""


def parse_loginusers(steam_dir):
    """解析 Steam\\config\\loginusers.vdf。

    返回 dict: { AccountID(str) : {"PersonaName": ..., "AccountName": ...} }
    AccountID 由 SteamID64 - 基址 得到。
    """
    result = {}
    path = os.path.join(steam_dir, "config", "loginusers.vdf")
    text = _read_text(path)
    if not text:
        return result

    # 每个 block: "76561198xxxxxxxx" { "AccountName" "x" "PersonaName" "y" ... }
    block_re = re.compile(
        r'"(\d{17})"\s*\{([^}]*)\}', re.DOTALL
    )
    kv_re = re.compile(r'"(\w+)"\s+"((?:[^"\\]|\\.)*)"')

    for sid64_str, body in block_re.findall(text):
        try:
            sid64 = int(sid64_str)
            account_id = str(sid64 - STEAMID64_BASE)
        except ValueError:
            continue
        info = {"PersonaName": "", "AccountName": ""}
        for k, v in kv_re.findall(body):
            if k in info:
                # 反转义 VDF 字符串
                info[k] = v.encode("latin-1", "ignore").decode(
                    "unicode_escape", "ignore"
                ) if "\\" in v else v
        result[account_id] = info
    return result


def parse_persona_from_localconfig(localconfig_path):
    """从 userdata\\<id>\\config\\localconfig.vdf 里取 PersonaName。

    结构里有 "friends" { "PersonaName" "xxx" }。找不到返回 ""。
    """
    text = _read_text(localconfig_path)
    if not text:
        return ""
    m = re.search(r'"PersonaName"\s+"((?:[^"\\]|\\.)*)"', text)
    if not m:
        return ""
    v = m.group(1)
    return v.encode("latin-1", "ignore").decode(
        "unicode_escape", "ignore"
    ) if "\\" in v else v


# --- 账号模型 ------------------------------------------------------------

class Account:
    """一个 Steam 账号。"""

    def __init__(self, account_id, persona_name="", account_name="",
                 has_dota_cfg=False):
        self.account_id = account_id          # userdata 目录名
        self.persona_name = persona_name      # 昵称 (显示优先)
        self.account_name = account_name      # 登录名
        self.has_dota_cfg = has_dota_cfg      # 是否存在 570\remote\cfg
        self.steamid64 = ""                   # 完整 SteamID64 (字符串)
        try:
            self.steamid64 = str(int(account_id) + STEAMID64_BASE)
        except ValueError:
            pass

    @property
    def display_name(self):
        name = self.persona_name or self.account_name or ""
        if name:
            return name
        return "未知用户"

    def __str__(self):
        flag = " [有Dota2配置]" if self.has_dota_cfg else " [无Dota2配置]"
        return f"{self.display_name}    ({self.account_id}){flag}"


def load_accounts(userdata_dir):
    """加载 userdata 下所有 Steam 账号。

    返回 list[Account], 按 display_name 排序。
    """
    accounts = []
    if not userdata_dir or not os.path.isdir(userdata_dir):
        return accounts

    # 拿到 Steam 安装目录 (userdata 的父目录) 用于读 loginusers.vdf
    steam_dir = os.path.dirname(userdata_dir)
    login_map = parse_loginusers(steam_dir)

    for name in os.listdir(userdata_dir):
        if not name.isdigit():
            continue
        acct_dir = os.path.join(userdata_dir, name)
        if not os.path.isdir(acct_dir):
            continue

        persona = ""
        account_name = ""
        if name in login_map:
            persona = login_map[name].get("PersonaName", "")
            account_name = login_map[name].get("AccountName", "")

        # 兜底: 从 localconfig.vdf 取昵称
        if not persona:
            lc = os.path.join(acct_dir, "config", "localconfig.vdf")
            if os.path.isfile(lc):
                persona = parse_persona_from_localconfig(lc)

        cfg_dir = os.path.join(acct_dir, CFG_REL)
        has_cfg = os.path.isdir(cfg_dir) and any(
            os.path.isfile(os.path.join(cfg_dir, x))
            for x in os.listdir(cfg_dir)
        ) if os.path.isdir(cfg_dir) else False

        accounts.append(Account(name, persona, account_name, has_cfg))

    accounts.sort(key=lambda a: (not a.has_dota_cfg, a.display_name.lower()))
    return accounts


# --- Steam Web API / 头像 -------------------------------------------------

def load_config():
    """读取脚本同目录的 config.json。返回 dict。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            if isinstance(cfg, dict):
                return cfg
    except (OSError, ValueError):
        pass
    return {}


def save_config(cfg):
    """写 config.json (UTF-8, 缩进)。"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def get_api_key():
    return load_config().get("steam_api_key", "").strip()


def set_api_key(key):
    cfg = load_config()
    cfg["steam_api_key"] = key.strip()
    save_config(cfg)


def get_show_avatar():
    """是否启用头像功能。默认 True (装了 Pillow 时)。"""
    cfg = load_config()
    if "show_avatar" not in cfg:
        return True
    return bool(cfg["show_avatar"])


def set_show_avatar(val):
    cfg = load_config()
    cfg["show_avatar"] = bool(val)
    save_config(cfg)


def get_proxy_config():
    """读取代理配置。返回 dict(proxy_enabled, proxy_host, proxy_port)。"""
    cfg = load_config()
    return {
        "enabled": cfg.get("proxy_enabled", False),
        "host": cfg.get("proxy_host", "127.0.0.1"),
        "port": cfg.get("proxy_port", "7897"),
    }


def set_proxy_config(enabled, host, port):
    """保存代理配置。"""
    cfg = load_config()
    cfg["proxy_enabled"] = bool(enabled)
    cfg["proxy_host"] = str(host).strip() or "127.0.0.1"
    cfg["proxy_port"] = str(port).strip() or "7897"
    save_config(cfg)
    return cfg["proxy_host"], cfg["proxy_port"]


def _build_opener():
    """构建 urllib opener，根据配置决定是否走 HTTP 代理。"""
    proxy = get_proxy_config()
    if proxy["enabled"]:
        proxy_url = f"http://{proxy['host']}:{proxy['port']}"
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def fetch_summaries(api_key, steamid64_list):
    """调用 GetPlayerSummaries/v2 批量拉取账号摘要。

    返回 { steamid64(str): {"personaname":..., "avatarfull":url, ...} }。
    失败抛 RuntimeError(友好信息)。
    """
    if not api_key:
        raise RuntimeError("未配置 Steam API key")
    if not steamid64_list:
        return {}

    # 单次最多 100 个
    ids_param = ",".join(str(s) for s in steamid64_list[:100])
    qs = urllib.parse.urlencode(
        {"key": api_key, "steamids": ids_param}
    )
    url = f"{STEAM_API_SUMMARY_URL}?{qs}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dota2-cfg-sync"})
        with _build_opener().open(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Steam API HTTP {e.code} (key 可能无效或被限流)")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"网络请求失败: {e}")
    except ValueError:
        raise RuntimeError("Steam API 返回非 JSON 数据")

    players = data.get("response", {}).get("players", []) or []
    return {p.get("steamid", ""): p for p in players}


def avatar_cache_path(steamid64):
    """头像在缓存目录中的本地路径。"""
    return os.path.join(AVATAR_CACHE_DIR, f"{steamid64}.jpg")


def download_avatar(url, steamid64):
    """下载头像到 avatar_cache, 返回本地路径。已存在则直接返回。"""
    path = avatar_cache_path(steamid64)
    if os.path.isfile(path):
        return path
    os.makedirs(AVATAR_CACHE_DIR, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dota2-cfg-sync"})
        with _build_opener().open(req, timeout=12) as resp:
            data = resp.read()
        with open(path, "wb") as f:
            f.write(data)
        return path
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def clear_avatar_cache():
    """删除整个头像缓存目录。"""
    if os.path.isdir(AVATAR_CACHE_DIR):
        shutil.rmtree(AVATAR_CACHE_DIR, ignore_errors=True)


# --- 备份 + 同步逻辑 ------------------------------------------------------

def backup_cfg_dir(cfg_dir, log=None):
    """备份 cfg_dir 内每个原始文件, 改名为 .bak1/.bak2, 最多保留 2 份。

    规则:
      - 原始文件 (无 .bakN 后缀) 视为需要备份的对象
      - 已存在 .bak2 -> 删除 (丢掉最旧一份)
      - 已存在 .bak1 -> 重命名为 .bak2
      - 当前文件复制 (不剪切, 保留原文件以便随后被覆盖) 为 .bak1
    log: 可选回调 fn(str)
    """
    if not os.path.isdir(cfg_dir):
        return 0

    # 先收集原始文件名 (排除已有的 .bak1/.bak2)
    originals = []
    for fn in os.listdir(cfg_dir):
        full = os.path.join(cfg_dir, fn)
        if not os.path.isfile(full):
            continue
        if any(fn.endswith(suf) for suf in BAK_SUFFIXES):
            continue
        originals.append(fn)

    count = 0
    for fn in originals:
        src = os.path.join(cfg_dir, fn)
        bak1 = src + ".bak1"
        bak2 = src + ".bak2"

        # 最旧的备份直接丢弃
        if os.path.exists(bak2):
            try:
                os.remove(bak2)
            except OSError as e:
                if log:
                    log(f"  ! 删除旧备份失败 {bak2}: {e}")

        # bak1 升级为 bak2
        if os.path.exists(bak1):
            try:
                os.replace(bak1, bak2)
            except OSError as e:
                if log:
                    log(f"  ! 重命名备份失败 {bak1} -> {bak2}: {e}")

        # 当前文件复制为 bak1 (用复制而非移动, 让后续覆盖步骤简单些)
        try:
            shutil.copy2(src, bak1)
            count += 1
            if log:
                log(f"  备份: {fn} -> {fn}.bak1")
        except OSError as e:
            if log:
                log(f"  ! 备份失败 {fn}: {e}")

    return count


def sync_cfg(src_cfg_dir, dst_cfg_dir, log=None):
    """把 src_cfg_dir 内所有文件复制到 dst_cfg_dir, 覆盖。

    同步前自动对 dst 做备份。返回 (备份文件数, 同步文件数)。
    """
    if not os.path.isdir(src_cfg_dir):
        if log:
            log(f"  ! 源 cfg 目录不存在: {src_cfg_dir}")
        return (0, 0)

    # 确保目标目录存在 (若目标从未跑过 Dota2, 目录可能不存在)
    os.makedirs(dst_cfg_dir, exist_ok=True)

    bak_n = backup_cfg_dir(dst_cfg_dir, log=log)

    # 复制源内所有普通文件 (含子目录? Dota2 cfg 一般无子目录, 这里递归以保稳妥)
    sync_n = 0
    for root, _dirs, files in os.walk(src_cfg_dir):
        rel = os.path.relpath(root, src_cfg_dir)
        dst_root = dst_cfg_dir if rel == "." else os.path.join(
            dst_cfg_dir, rel
        )
        os.makedirs(dst_root, exist_ok=True)
        for fn in files:
            # 源里如果混进 .bakN 不复制
            if any(fn.endswith(suf) for suf in BAK_SUFFIXES):
                continue
            s = os.path.join(root, fn)
            d = os.path.join(dst_root, fn)
            try:
                shutil.copy2(s, d)
                sync_n += 1
                if log:
                    log(f"  同步: {fn}")
            except OSError as e:
                if log:
                    log(f"  ! 同步失败 {fn}: {e}")
    return (bak_n, sync_n)


# --- GUI -----------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Dota2 改键配置同步")
        self.root.minsize(720, 600)

        self.userdata_dir = tk.StringVar()
        self.accounts = []                 # list[Account]
        self.src_var = tk.StringVar()      # 选中源账号的 account_id
        self.multi_target = tk.BooleanVar(value=False)

        # 头像相关状态
        self.api_key = get_api_key()
        self.show_avatar = tk.BooleanVar(
            value=get_show_avatar() and PIL_AVAILABLE
        )
        self._avatar_imgs = {}             # account_id -> ImageTk.PhotoImage (防 GC)
        self._item_by_acct = {}            # account_id -> tree item id
        self._avatar_thread = None         # 后台头像加载线程

        # 代理相关状态
        proxy = get_proxy_config()
        self.proxy_enabled = tk.BooleanVar(value=proxy["enabled"])
        self.proxy_host_var = tk.StringVar(value=proxy["host"])
        self.proxy_port_var = tk.StringVar(value=proxy["port"])

        # 自动探测默认目录
        det = detect_steam_userdata_dir()
        if det:
            self.userdata_dir.set(det)

        self._build_ui()
        # 启动时若已探测到路径, 自动加载
        if det:
            self.refresh_accounts()

        # 让窗口自适应内容大小, 避免初始需要拖拽才能看到完整界面
        self.root.update_idletasks()
        w = max(self.root.winfo_reqwidth(), 720)
        h = max(self.root.winfo_reqheight(), 600)
        self.root.geometry(f"{w}x{h}")

    # ---------- UI 构建 ----------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # 设置 Treeview 行高, 避免 32px 头像在默认行高中互相覆盖
        style = ttk.Style()
        style.configure("Treeview", rowheight=AVATAR_SIZE + 6)

        # 顶部: userdata 路径选择
        top = ttk.LabelFrame(self.root, text="Steam userdata 目录")
        top.pack(fill="x", **pad)

        ttk.Entry(top, textvariable=self.userdata_dir).pack(
            side="left", fill="x", expand=True, padx=(8, 4), pady=8
        )
        ttk.Button(top, text="浏览…", command=self.browse_dir).pack(
            side="left", pady=8
        )
        ttk.Button(top, text="刷新账号", command=self.refresh_accounts).pack(
            side="left", padx=(4, 8), pady=8
        )

        # API Key 配置行 (用于头像加载)
        api = ttk.LabelFrame(self.root, text="Steam Web API (头像)")
        api.pack(fill="x", **pad)
        self.api_key_var = tk.StringVar(value=self.api_key)
        ttk.Label(api, text="API Key:").pack(side="left", padx=(8, 4), pady=8)
        self.api_entry = ttk.Entry(
            api, textvariable=self.api_key_var, width=38, show="*"
        )
        self.api_entry.pack(side="left", pady=8)
        ttk.Button(api, text="保存", command=self.save_api_key).pack(
            side="left", padx=4
        )
        ttk.Button(
            api, text="申请key→", command=lambda: webbrowser.open(STEAM_API_KEY_URL)
        ).pack(side="left", padx=4)
        ttk.Checkbutton(
            api, text="显示头像(需联网)", variable=self.show_avatar,
            command=self.on_toggle_avatar
        ).pack(side="left", padx=(16, 4))
        ttk.Button(api, text="清除头像缓存", command=self.clear_cache).pack(
            side="left", padx=4
        )
        if not PIL_AVAILABLE:
            ttk.Label(
                api, text="(未装 Pillow, 头像不可用: pip install Pillow)",
                foreground="#b00020"
            ).pack(side="left", padx=8)

        # 代理设置行 (用于通过代理访问 Steam API)
        prox = ttk.LabelFrame(self.root, text="网络代理 (HTTP)")
        prox.pack(fill="x", **pad)
        ttk.Checkbutton(
            prox, text="启用代理", variable=self.proxy_enabled
        ).pack(side="left", padx=(8, 4), pady=8)
        ttk.Label(prox, text="地址:").pack(side="left")
        ttk.Entry(prox, textvariable=self.proxy_host_var, width=14).pack(
            side="left", padx=2, pady=8
        )
        ttk.Label(prox, text="端口:").pack(side="left")
        ttk.Entry(prox, textvariable=self.proxy_port_var, width=6).pack(
            side="left", padx=2, pady=8
        )
        ttk.Button(
            prox, text="保存代理设置", command=self.save_proxy
        ).pack(side="left", padx=4)

        # 中部: 账号列表 (左侧头像 + 右侧信息)
        mid = ttk.LabelFrame(self.root, text="检测到的账号")
        mid.pack(fill="both", expand=True, **pad)

        cols = ("info",)
        # show="tree headings" 启用 #0 树形列用于显示头像
        self.tree = ttk.Treeview(
            mid, columns=cols, show="tree headings", height=10
        )
        self.tree.heading("#0", text="头像")
        self.tree.heading("info", text="账号 (昵称 / AccountID / Dota2配置)")
        self.tree.column("#0", width=AVATAR_SIZE + 24, stretch=False, anchor="center")
        self.tree.column("info", width=620, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)

        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", pady=8, padx=(0, 8))

        # 底部: 源/目标选择 + 操作按钮
        bot = ttk.LabelFrame(self.root, text="同步设置")
        bot.pack(fill="x", **pad)

        ttk.Label(bot, text="源账号 (复制改键):").grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 2)
        )
        self.src_combo = ttk.Combobox(
            bot, textvariable=self.src_var, state="readonly", width=40
        )
        self.src_combo.grid(row=0, column=1, sticky="w", pady=(8, 2))

        ttk.Label(bot, text="目标账号 (被覆盖):").grid(
            row=1, column=0, sticky="nw", padx=8, pady=2
        )
        self.dst_frame = ttk.Frame(bot)
        self.dst_frame.grid(row=1, column=1, sticky="w", pady=2)

        # 单选目标下拉 (默认)
        self.dst_var = tk.StringVar()
        self.dst_combo = ttk.Combobox(
            self.dst_frame, textvariable=self.dst_var, state="readonly", width=40
        )
        self.dst_combo.pack(side="left")

        # 多选目标列表 (勾选 multi 时切换显示)
        self.multi_frame = ttk.Frame(self.dst_frame)
        # 内嵌一个紧凑的多选 Treeview
        self.multi_tree = ttk.Treeview(
            self.multi_frame, columns=("sel",), show="tree headings",
            height=5
        )
        self.multi_tree.heading("#0", text="勾选目标账号")
        self.multi_tree.heading("sel", text="")
        self.multi_tree.column("#0", width=320, anchor="w")
        self.multi_tree.column("sel", width=0, stretch=False)
        self.multi_tree.pack(side="left")
        self.multi_tree.bind("<Button-1>", self._on_multi_click)
        # 默认隐藏
        self.multi_frame.pack_forget()

        self.multi_target.trace_add("write", self._toggle_multi)

        ttk.Checkbutton(
            bot, text="同时覆盖多个目标 (勾选后在下方列表中点选)",
            variable=self.multi_target
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 8))

        # 操作按钮
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", **pad)
        self.preview_btn = ttk.Button(
            actions, text="预览", command=self.preview
        )
        self.preview_btn.pack(side="left", padx=8)
        self.sync_btn = ttk.Button(
            actions, text="开始同步", command=self.do_sync
        )
        self.sync_btn.pack(side="left")
        ttk.Button(actions, text="退出", command=self.root.quit).pack(
            side="right", padx=8
        )

        # 日志区
        logf = ttk.LabelFrame(self.root, text="日志")
        logf.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(
            logf, height=8, wrap="word", state="disabled",
            background="#1e1e1e", foreground="#d4d4d4", font=("Consolas", 9)
        )
        self.log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y", pady=8, padx=(0, 8))

    # ---------- 辅助 ----------

    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.root.update_idletasks()

    def browse_dir(self):
        d = filedialog.askdirectory(
            title="选择 Steam userdata 目录",
            initialdir=self.userdata_dir.get() or "C:\\",
        )
        if d:
            self.userdata_dir.set(d)
            self.refresh_accounts()

    def refresh_accounts(self):
        ud = self.userdata_dir.get().strip()
        # 清空旧列表
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.src_combo.set("")
        self.dst_combo.set("")
        for i in self.multi_tree.get_children():
            self.multi_tree.delete(i)

        if not ud:
            self.log("请先选择 userdata 目录")
            return
        if not os.path.isdir(ud):
            self.log(f"目录不存在: {ud}")
            return

        self.accounts = load_accounts(ud)
        if not self.accounts:
            self.log(f"未在 {ud} 下找到任何账号目录")
            return

        # 填账号列表 (先填文字, 头像后台异步回填)
        self._item_by_acct = {}
        self._avatar_imgs = {}
        labels = []
        for a in self.accounts:
            item = self.tree.insert("", "end", values=(str(a),))
            self._item_by_acct[a.account_id] = item
            labels.append(f"{a.display_name} ({a.account_id})")

        self.src_combo["values"] = labels
        self.dst_combo["values"] = labels

        # 多选列表: 用复选框风格 (文字前加 ☐/☑)
        for a in self.accounts:
            self.multi_tree.insert(
                "", "end", text=f"☐ {a}", values=("0",),
                tags=(a.account_id,)
            )

        n_cfg = sum(1 for a in self.accounts if a.has_dota_cfg)
        self.log(f"已加载 {len(self.accounts)} 个账号 "
                 f"(其中 {n_cfg} 个含 Dota2 配置)")

        # 异步加载头像
        self._maybe_load_avatars()

    # ---------- 头像加载 ----------

    def _maybe_load_avatars(self):
        """缓存优先: 先渲染已有的缓存头像, 仅对缺失的走 API。"""
        if not self.show_avatar.get():
            return
        if not PIL_AVAILABLE:
            return
        if not self.accounts:
            return
        # 避免重复并发
        if self._avatar_thread and self._avatar_thread.is_alive():
            return

        # 1) 先从缓存渲染已有的头像 (无需 API)
        cached = {}
        uncached = []
        for a in self.accounts:
            if not a.steamid64:
                continue
            path = avatar_cache_path(a.steamid64)
            if os.path.isfile(path):
                cached[a.account_id] = path
            else:
                uncached.append((a.account_id, a.steamid64))

        if cached:
            self.root.after(0, lambda: self._render_avatars(cached))

        # 2) 没有未缓存的 → 全部来自缓存, 不联网
        if not uncached:
            if cached:
                self.log(f"头像已从缓存加载: {len(cached)} 个 (无需联网)")
            return

        # 3) 有未缓存的, 需要 API key
        if not self.api_key:
            if cached:
                self.log(
                    f"已从缓存加载 {len(cached)} 个头像, "
                    f"{len(uncached)} 个未缓存 (需 API key)"
                )
            else:
                self.log("未配置 API key, 跳过头像加载 (可选)")
            return

        # 4) 联网拉取未缓存头像
        if cached:
            self.log(
                f"已从缓存加载 {len(cached)} 个头像, "
                f"正在联网加载 {len(uncached)} 个…"
            )
        else:
            self.log(f"正在联网加载 {len(uncached)} 个头像…")

        cached_ids = set(cached.keys())
        self._avatar_thread = threading.Thread(
            target=self._load_avatars_worker,
            args=(uncached, self.api_key, cached_ids),
            daemon=True,
        )
        self._avatar_thread.start()

    def _load_avatars_worker(self, snapshot, api_key, cached_ids):
        """后台线程: 拉摘要 + 仅下载未缓存的头像, 通过 after() 回主线程。"""
        # 1) 拉取摘要 (只请求未缓存账号)
        sid64_list = [s[1] for s in snapshot]
        summaries = {}
        err = None
        try:
            summaries = fetch_summaries(api_key, sid64_list)
        except RuntimeError as e:
            err = str(e)

        # 主线程: 处理摘要 (补全昵称 + 报错)
        self.root.after(0, lambda: self._on_summaries(summaries, err))

        if not summaries:
            return

        # 2) 只下载尚未缓存的头像
        ok = {}
        for acct_id, sid64 in snapshot:
            if acct_id in cached_ids:
                continue  # 已在缓存中存在, 跳过
            p = summaries.get(sid64)
            if not p:
                continue
            url = p.get("avatarfull") or p.get("avatarmedium") or p.get("avatar")
            if not url:
                continue
            local = download_avatar(url, sid64)
            if local:
                ok[acct_id] = local

        # 主线程: 渲染新下载的头像
        if ok:
            self.root.after(0, lambda: self._render_avatars(ok))

    def _on_summaries(self, summaries, err):
        """主线程: 用 API 昵称补全本地缺失的账号, 并处理错误。"""
        if err:
            self.log(f"! 头像摘要加载失败: {err}")
            return
        if not summaries:
            self.log("! 未取到账号摘要 (key 无效或网络问题)")
            return
        by_sid = {a.steamid64: a for a in self.accounts}
        updated = 0
        for sid64, info in summaries.items():
            a = by_sid.get(sid64)
            if not a:
                continue
            name = info.get("personaname", "")
            if name and (not a.persona_name):
                a.persona_name = name
                item = self._item_by_acct.get(a.account_id)
                if item:
                    self.tree.item(item, values=(str(a),))
                updated += 1
        if updated:
            # 同步刷新下拉框标签
            labels = [f"{a.display_name} ({a.account_id})" for a in self.accounts]
            self.src_combo["values"] = labels
            self.dst_combo["values"] = labels
            self.log(f"已用 API 昵称补全 {updated} 个账号")

    def _render_avatars(self, path_map):
        """主线程: 把缓存的头像图片加载为缩略图并贴到对应行。"""
        if not PIL_AVAILABLE:
            return
        for acct_id, path in path_map.items():
            item = self._item_by_acct.get(acct_id)
            if not item:
                continue
            try:
                im = Image.open(path)
                im = im.resize(
                    (AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS
                ).convert("RGBA")
                photo = ImageTk.PhotoImage(im)
                self._avatar_imgs[acct_id] = photo  # 防 GC
                self.tree.item(item, image=photo)
            except Exception as e:
                self.log(f"! 头像渲染失败 {acct_id}: {e}")
        if path_map:
            self.log(f"头像加载完成: {len(path_map)} 个")

    def save_api_key(self):
        key = self.api_key_var.get().strip()
        self.api_key = key
        set_api_key(key)
        self.log("API key 已保存")
        if key:
            self.refresh_accounts()  # 重新加载 (会触发头像)
        else:
            self.log("key 已清空, 头像功能关闭")

    def on_toggle_avatar(self):
        val = self.show_avatar.get()
        set_show_avatar(val)
        if val:
            self.refresh_accounts()
        else:
            # 清除已显示头像
            for acct_id, item in self._item_by_acct.items():
                self.tree.item(item, image="")
            self._avatar_imgs = {}

    def clear_cache(self):
        clear_avatar_cache()
        self.log("头像缓存已清除")

    def save_proxy(self):
        """保存代理设置到 config.json。"""
        host, port = set_proxy_config(
            self.proxy_enabled.get(),
            self.proxy_host_var.get(),
            self.proxy_port_var.get(),
        )
        self.log(
            f"代理{'已启用' if self.proxy_enabled.get() else '已禁用'}: "
            f"http://{host}:{port}"
        )


    def _label_for(self, account_id):
        for a in self.accounts:
            if a.account_id == account_id:
                return f"{a.display_name} ({a.account_id})"
        return account_id

    def _account_from_label(self, label):
        # label 形如 "bobo (52079950)"
        m = re.search(r"\((\d+)\)\s*$", label)
        if m:
            return m.group(1)
        return None

    # ---------- 多选目标切换 ----------

    def _toggle_multi(self, *_):
        if self.multi_target.get():
            self.dst_combo.pack_forget()
            self.multi_frame.pack(side="left", fill="both", expand=True)
        else:
            self.multi_frame.pack_forget()
            self.dst_combo.pack(side="left")

    def _on_multi_click(self, event):
        item = self.multi_tree.identify_row(event.y)
        if not item:
            return
        text = self.multi_tree.item(item, "text")
        if text.startswith("☐"):
            self.multi_tree.item(item, text="☑" + text[1:])
        elif text.startswith("☑"):
            self.multi_tree.item(item, text="☐" + text[1:])

    def _get_selected_targets(self):
        """返回目标 account_id 列表 (去重)。"""
        ids = []
        if self.multi_target.get():
            for item in self.multi_tree.get_children():
                text = self.multi_tree.item(item, "text")
                if text.startswith("☑"):
                    ids.append(self.multi_tree.item(item, "tags")[0])
        else:
            lbl = self.dst_var.get()
            aid = self._account_from_label(lbl)
            if aid:
                ids.append(aid)
        # 去重保序
        seen = set()
        uniq = []
        for i in ids:
            if i not in seen:
                seen.add(i)
                uniq.append(i)
        return uniq

    def _get_selected_source(self):
        lbl = self.src_var.get()
        return self._account_from_label(lbl)

    # ---------- 预览 / 同步 ----------

    def _validate(self):
        ud = self.userdata_dir.get().strip()
        if not ud or not os.path.isdir(ud):
            messagebox.showerror("错误", "Steam userdata 目录无效。")
            return None
        src = self._get_selected_source()
        if not src:
            messagebox.showerror("错误", "请选择源账号。")
            return None
        tgts = self._get_selected_targets()
        if not tgts:
            messagebox.showerror("错误", "请选择至少一个目标账号。")
            return None
        if src in tgts:
            messagebox.showerror("错误", "目标账号不能和源账号相同。")
            return None
        return (src, tgts)

    def preview(self):
        v = self._validate()
        if not v:
            return
        src, tgts = v
        ud = self.userdata_dir.get().strip()

        src_cfg = os.path.join(ud, src, CFG_REL)
        if not os.path.isdir(src_cfg):
            messagebox.showerror(
                "错误",
                f"源账号没有 Dota2 cfg 目录:\n{src_cfg}\n请先在该账号启动过 Dota2。",
            )
            return

        files = sorted(
            f for f in os.listdir(src_cfg)
            if os.path.isfile(os.path.join(src_cfg, f))
            and not any(f.endswith(s) for s in BAK_SUFFIXES)
        )

        lines = []
        lines.append(f"源账号: {self._label_for(src)}\n")
        lines.append("将复制以下文件: " + (", ".join(files) if files else "(空)") + "\n")
        lines.append("目标账号 (覆盖前自动备份 .bak1/.bak2, 最多保留 2 份):\n")
        for t in tgts:
            lines.append(f"  → {self._label_for(t)}\n")
        messagebox.showinfo("预览", "".join(lines))

    def do_sync(self):
        v = self._validate()
        if not v:
            return
        src, tgts = v
        ud = self.userdata_dir.get().strip()

        src_cfg = os.path.join(ud, src, CFG_REL)
        if not os.path.isdir(src_cfg):
            messagebox.showerror(
                "错误", f"源账号没有 Dota2 cfg 目录:\n{src_cfg}"
            )
            return

        # 二次确认
        names = ", ".join(self._label_for(t) for t in tgts)
        if not messagebox.askyesno(
            "确认同步",
            f"确定要把源账号的改键配置覆盖到以下账号吗?\n\n"
            f"源: {self._label_for(src)}\n目标: {names}\n\n"
            f"目标原有配置会被备份为 .bak1/.bak2 (最多保留 2 份)。",
        ):
            return

        self.log(f"=== 开始同步 ===")
        self.log(f"源: {self._label_for(src)}")
        self.log(f"源目录: {src_cfg}")

        total_bak = 0
        total_sync = 0
        for t in tgts:
            dst_cfg = os.path.join(ud, t, CFG_REL)
            self.log(f"-> 目标: {self._label_for(t)}")
            self.log(f"   目标目录: {dst_cfg}")
            bak_n, sync_n = sync_cfg(src_cfg, dst_cfg, log=self.log)
            self.log(f"   完成: 备份 {bak_n} 个文件, 同步 {sync_n} 个文件")
            total_bak += bak_n
            total_sync += sync_n

        self.log(f"=== 全部完成: 共备份 {total_bak}, 同步 {total_sync} ===")
        messagebox.showinfo(
            "完成",
            f"同步完成。\n共备份 {total_bak} 个文件, 同步 {total_sync} 个文件。",
        )


def main():
    root = tk.Tk()
    try:
        # Windows 高 DPI 自适应
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
