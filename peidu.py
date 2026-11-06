# -*- coding: utf-8 -*-
"""AI 陪读小窗 —— 划词/截图 -> 提问 -> 连续追问。

热键：
    Ctrl+Alt+A  取当前选中的文字（模拟 Ctrl+C）
    Ctrl+Alt+S  框选截图
    Esc         隐藏窗口

依赖：Python 3.13 + tkinter + requests + Pillow（本机已有）。
"""
import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import html
import io
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import quote, urlsplit

if getattr(sys, "frozen", False):
    # PyInstaller onefile：__file__ 在临时解包目录里，配置/日志必须跟着 exe 走
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
# 项目自带的小依赖（opencc 简繁表、pystray 托盘、PySocks）：用源码直接跑时，换个解释器也能 import。
# 必须在 import requests 之前插进 sys.path：requests 只在导入时检查一次 socks，晚了就把代理支持钉死成
# "Missing dependencies for SOCKS support."
_VENDOR = os.path.join(APP_DIR, "lib")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

import requests
from PIL import Image, ImageGrab

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
PROMPTS_PATH = os.path.join(APP_DIR, "prompts.json")
LOG_PATH = os.path.join(APP_DIR, "peidu.log")
CACHE_DIR = os.path.join(APP_DIR, "images")
MATH_DIR = os.path.join(CACHE_DIR, "math")

# 公式里夹中文时 mathtext 默认字体没有汉字，会画成方框；这类表达式换微软雅黑字体集
CJK_RE = re.compile(r"[⺀-鿿＀-￯]")
CJK_MATH_RC = {
    "mathtext.fontset": "custom",
    "mathtext.rm": "Microsoft YaHei",
    "mathtext.it": "Microsoft YaHei:italic",
    "mathtext.bf": "Microsoft YaHei:bold",
    "mathtext.sf": "Microsoft YaHei",
    "mathtext.tt": "Consolas",
    "mathtext.cal": "Microsoft YaHei",
    "mathtext.fallback": "stix",
}

STRANSLATE_DIR = os.path.expandvars(
    r"%APPDATA%\STranslate\Settings\Plugins\STranslate.Plugin.Translate.OpenAI"
)
SVCS = {"本地": "4eaba30e4f8d45bf9d505871cd5be56e", "deepseek": "2d19d865d1e54312b12360be2ef3191d"}

PALETTES = {
    "dark": {
        "BG": "#1e1f22", "PANEL": "#2b2d31", "PANEL2": "#232428", "FG": "#e6e6e6",
        "MUTED": "#9aa0a6", "ACCENT": "#4c8dff", "ACCENT_ACTIVE": "#3f7ae0", "BORDER": "#3a3d43",
        "USER_FG": "#8fb7ff", "CODE_FG": "#d6e2f0", "ERR_FG": "#ff7b72", "STOP_BG": "#7a3b3b",
        "SEL_BG": "#4c8dff", "SEL_FG": "#ffffff", "BTN_FG": "#ffffff", "BTN_BG": "#2b2d31",
    },
    "light": {
        "BG": "#ffffff", "PANEL": "#f3f5f8", "PANEL2": "#e9eef5", "FG": "#1f2328",
        "MUTED": "#6b7280", "ACCENT": "#2f6feb", "ACCENT_ACTIVE": "#255bc4", "BORDER": "#d7dce3",
        "USER_FG": "#1f5fd0", "CODE_FG": "#0f172a", "ERR_FG": "#c0392b", "STOP_BG": "#c0625c",
        "SEL_BG": "#2f6feb", "SEL_FG": "#ffffff", "BTN_FG": "#ffffff", "BTN_BG": "#e9eef5",
    },
}

BG = PANEL = PANEL2 = FG = MUTED = ACCENT = ACCENT_ACTIVE = BORDER = ""
USER_FG = CODE_FG = ERR_FG = STOP_BG = SEL_BG = SEL_FG = BTN_FG = BTN_BG = ""


def set_palette(name):
    global BG, PANEL, PANEL2, FG, MUTED, ACCENT, ACCENT_ACTIVE, BORDER
    global USER_FG, CODE_FG, ERR_FG, STOP_BG, SEL_BG, SEL_FG, BTN_FG, BTN_BG
    p = PALETTES.get(name) or PALETTES["dark"]
    BG, PANEL, PANEL2, FG, MUTED = p["BG"], p["PANEL"], p["PANEL2"], p["FG"], p["MUTED"]
    ACCENT, ACCENT_ACTIVE, BORDER = p["ACCENT"], p["ACCENT_ACTIVE"], p["BORDER"]
    USER_FG, CODE_FG, ERR_FG = p["USER_FG"], p["CODE_FG"], p["ERR_FG"]
    STOP_BG, SEL_BG, SEL_FG, BTN_FG = p["STOP_BG"], p["SEL_BG"], p["SEL_FG"], p["BTN_FG"]
    BTN_BG = p["BTN_BG"]


set_palette("dark")
FONT = ("Microsoft YaHei UI", 10)
FONT_S = ("Microsoft YaHei UI", 9)
MONO = ("Consolas", 10)

SYSTEM_PROMPT = (
    "你是我的读书陪读助手。回答要直接、具体，先给结论再给必要解释；"
    "遇到古文或外文，讲清楚关键词在这里的意思。"
    "中文回答，不要客套话，不要复述我的问题。"
    "如果收到图片（书页、公式、截图），先把图里的关键内容读出来再回答。"
    "需要时可以补充背景知识，但不要离题发挥。"
)

PINYIN_SYS = ("You are a precise Chinese reading assistant. You only return the annotated text in the exact "
              "format requested — no greetings, no restating the task, no closing remarks, no translation into other languages.")
PINYIN_USR = """给下面这段文本加拼音注音和简单解释。

格式（严格照此，不要输出 JSON/表格/多余说明）：
逐句处理，每句先原样抄出原文，然后给整句拼音，最后给一句解释：

原句：<原文照抄>
拼音：<带声调，词内连写、词间空格，不加标点>
解释：<先用一句话说这句在讲什么，再用「词：释义」补充生僻词/典故，没有就省略>

多音字按上下文定音；「一」「不」按实际变调标；轻声不标调。文本里如有外文，保留原文，英文给音标、日文给罗马音。

$content"""

GUWEN1_SYS = ("You are a precise classical Chinese (文言文) translator. You only return the translation in the "
              "exact format requested — no greetings, no restating the task, no closing remarks, no translation into other languages.")
GUWEN1_USR = """把下面的文言文翻译成现代汉语。

要求（严格照此）：
1. 逐句对照，每句两行：
   原句：<原文照抄>
   译文：<现代汉语，直译为主，通顺可读，不添加原文没有的内容>
2. 通假字、古今异义、词类活用、典故、生僻官职地名，在译文后用「〔注：…〕」一句话说明，没有就不写。
3. 人名、地名、年号照原文保留，首次出现可在括号里补一句身份。
4. 不翻译成外语，不复述任务，不加总结和客套话。

$content"""

GUWEN2_SYS = ("You are a professional classical Chinese (文言文) translator. You output only the modern Chinese "
              "translation itself — no original text, no notes, no headings, no commentary.")
GUWEN2_USR = """把下面的文言文翻译成通顺的现代汉语，只输出译文本身。

要求：
1. 不抄原文，不分行对照，不加注释、标题、引号或任何说明。
2. 直译为主，语序按现代汉语调整；不增添原文没有的内容，不删减。
3. 人名、地名、年号、官职保留原样。
4. 原文分几段就译成几段。

$content"""

WIKI_SYS_OLD = ("你是读书陪读助手，回答依据是用户维基百科（本地 Kiwix 或官方 Wikipedia）的条目资料。"
                "优先用【维基资料】里的内容：先给结论，再挑关键段落讲清楚；提到条目时写出条目名。"
                "资料里没有的不要编，直接说「维基里没查到」；可以补常识，但要说明那是补充。"
                "中文回答，不要客套话，不要复述问题。")
WIKI_SYS = ("你是读书陪读助手，回答依据是维基百科（本地 Kiwix 或官方 Wikipedia）的条目资料。"
            "如果用户这条消息里没有【维基资料】，说明这次没有开启维基检索："
            "直接回答「没有开启查维基，先勾上主界面的 ☑ 查维基」，不要凭记忆作答，不要编条目内容。"
            "有【维基资料】时优先用它：先给结论，再挑关键段落讲清楚；提到条目时写出条目名；"
            "资料里没有的不要编，直接说「维基里没查到」；可以补常识，但要说明那是补充。"
            "中文回答，不要客套话，不要复述问题。")
WIKI_PROMPT = {"name": "维基百科", "system": WIKI_SYS, "template": "", "wiki": True}

# 截图 → LaTeX：给物理老师做讲义用。答案只允许一个 ```latex 代码块，好一键复制
FORMULA_SYS = ("You are a LaTeX transcription engine. You read the formulas in an image and return LaTeX code "
               "only: no explanations, no translation, no restating, no greetings. "
               "Output exactly one ```latex fenced block and nothing outside it.")
FORMULA_USR = """把图里的公式转成 LaTeX 代码。

0. 这条消息里如果没有图片（只有文字），不要猜、不要编，直接回这一行：截图转公式只认截图，请按 Alt+2 框选公式。
1. 只输出一个 ```latex 代码块，里面是纯 LaTeX：不要 $ 定界符，不要 \\begin{document}，不要中文解说。
2. 每个独立公式一行；同一串推导用 aligned 环境，按等号对齐。
3. 分数用 \\frac，根号用 \\sqrt，数字之间的乘号用 \\times，点乘用 \\cdot；
   希腊字母用 \\alpha / \\Delta 这类命令，不要写 Unicode 字母。
4. 上下标、单位、中文按图照抄，别改写、别翻译、别解释含义。
   单个拉丁字母的下标照原样写（E_k、v_x、F_1），不要改写成 \\kappa 这类希腊字母；图上明确是希腊字母时才用 \\alpha / \\Delta。
5. 图里不是公式、或实在认不出来 → 只输出一行**纯文本**（不要包 ``` 代码块）：没有识别到公式。"""
FORMULA_PROMPT = {"name": "截图转公式", "system": FORMULA_SYS, "template": FORMULA_USR}

TRANSLATE_SYS = ("You are a precise English↔Chinese translation and dictionary engine. "
                 "Include IPA pronunciation whenever a single English word is involved "
                 "(either the source word or the result). "
                 "Output only the translation itself — no greetings, no explanations, "
                 "no examples, no restating the task.")
TRANSLATE_USR = """把下面这段翻译成中文；如果原文是中文，就翻成英文。

音标规则：
1. 原文是单个英文单词 → 先给中文释义，再给这个单词的国际音标（IPA），用 / / 括起；多义词分行，每行「义项  单词 /音标/」。
2. 原文是中文 → 翻成英文；译文是单个英文单词时，在单词后给国际音标，如：书  book /bʊk/。译文是短语或句子就不给。
3. 原文是词组、短语、整句时，一律不给音标。

只输出译文（含音标），不要解释、不要例句、不要复述原文。
英美读音差别明显时先给英式，再补「美 /…/」。

$content"""

DEFAULT_PROMPTS = [
    {"name": "陪读", "system": SYSTEM_PROMPT, "template": ""},
    {"name": "翻译", "system": TRANSLATE_SYS, "template": TRANSLATE_USR},
    {"name": "润色",
     "system": "You are a professional, authentic text polishing engine. You only return the polished text, without any explanations.",
     "template": "把下面这段文字润色得更通顺自然，保持原意和语气，只输出润色后的文字：\n\n$content"},
    {"name": "总结",
     "system": "You are a professional, authentic text summarization engine. You only return the summarized text, without any explanations.",
     "template": "把下面这段总结成要点，保留关键信息，只输出总结：\n\n$content"},
    {"name": "拼音+简单解释", "system": PINYIN_SYS, "template": PINYIN_USR},
    {"name": "古文今译", "system": GUWEN1_SYS, "template": GUWEN1_USR},
    {"name": "古文纯译", "system": GUWEN2_SYS, "template": GUWEN2_USR},
    FORMULA_PROMPT,
    WIKI_PROMPT,
]

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
WM_HOTKEY = 0x0312
WM_HK_RELOAD = 0x8001


def vk_name(vk):
    if 0x70 <= vk <= 0x87:
        return "F%d" % (vk - 0x70 + 1)
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    special = {0x20: "Space", 0x09: "Tab", 0x0D: "Enter", 0x08: "Backspace",
               0xC0: "`", 0xBD: "-", 0xBB: "=", 0xDB: "[", 0xDD: "]", 0xDC: "\\",
               0xBA: ";", 0xDE: "'", 0xBC: ",", 0xBE: ".", 0xBF: "/"}
    return special.get(vk, "VK%02X" % vk)


def hotkey_label(mods, vk):
    parts = []
    if mods & MOD_CONTROL:
        parts.append("Ctrl")
    if mods & MOD_ALT:
        parts.append("Alt")
    if mods & MOD_SHIFT:
        parts.append("Shift")
    if mods & MOD_WIN:
        parts.append("Win")
    parts.append(vk_name(vk))
    return "+".join(parts)


def hotkey_to_str(mods, vk):
    parts = []
    if mods & MOD_CONTROL:
        parts.append("ctrl")
    if mods & MOD_ALT:
        parts.append("alt")
    if mods & MOD_SHIFT:
        parts.append("shift")
    if mods & MOD_WIN:
        parts.append("win")
    parts.append(vk_name(vk).lower())
    return "+".join(parts)


def parse_hotkey(s):
    mods, vk = 0, 0
    for part in [p.strip().lower() for p in str(s or "").split("+") if p.strip()]:
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "super", "meta"):
            mods |= MOD_WIN
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x70 + int(part[1:]) - 1
    if not vk:
        vk = 0x41
    if not mods:
        mods = MOD_CONTROL | MOD_ALT
    return mods, vk


# ---------------------------------------------------------------- 基础工具
def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


def load_prompts():
    """提示词库：prompts.json，首次运行时用内置默认生成。"""
    if os.path.exists(PROMPTS_PATH):
        try:
            d = json.load(open(PROMPTS_PATH, encoding="utf-8"))
            if d.get("prompts"):
                return _with_formula(_with_wiki(d["prompts"]))
        except Exception as ex:
            log("读 prompts.json 失败，用默认: %r" % ex)
    d = {"prompts": DEFAULT_PROMPTS}
    try:
        json.dump(d, open(PROMPTS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass
    return _with_formula(_with_wiki(list(DEFAULT_PROMPTS)))


def _with_wiki(prompts):
    """老 prompts.json 没有「维基百科」这一档时补上；旧版维基 system 文案升级一次。

    「只加不改」的原则保留：只有开头与旧文案逐字对得上的 system 才替换（用户自己改过的不动）。
    """
    try:
        changed = False
        for p in prompts:
            sys_txt = p.get("system")
            if p.get("wiki") and isinstance(sys_txt, str) and sys_txt.startswith(WIKI_SYS_OLD):
                p["system"] = WIKI_SYS + sys_txt[len(WIKI_SYS_OLD):]
                changed = True
        if changed:
            with open(PROMPTS_PATH, "w", encoding="utf-8") as f:
                json.dump({"prompts": prompts}, f, ensure_ascii=False, indent=2)
            log("维基档 system 升级：没开查维基时直说，不编")
        if any(p.get("wiki") for p in prompts) or any(p.get("name") == WIKI_PROMPT["name"] for p in prompts):
            return prompts
        prompts = list(prompts) + [dict(WIKI_PROMPT)]
        with open(PROMPTS_PATH, "w", encoding="utf-8") as f:
            json.dump({"prompts": prompts}, f, ensure_ascii=False, indent=2)
        log("提示词库补上「维基百科」一档")
    except Exception as ex:
        log("补维基提示词失败: %r" % ex)
    return prompts


def _with_formula(prompts):
    """老 prompts.json 没有「截图转公式」这一档时补上（只加不改，用户改过的档一个字都不动）。"""
    try:
        if any(p.get("name") == FORMULA_PROMPT["name"] for p in prompts):
            return prompts
        prompts = list(prompts) + [dict(FORMULA_PROMPT)]
        with open(PROMPTS_PATH, "w", encoding="utf-8") as f:
            json.dump({"prompts": prompts}, f, ensure_ascii=False, indent=2)
        log("提示词库补上「截图转公式」一档")
    except Exception as ex:
        log("补截图转公式提示词失败: %r" % ex)
    return prompts


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    cfg = {"default": "deepseek", "save_dir": r"Y:\drive\obsidian\yxh\AI\陪读",
           "hotkey_text": "ctrl+alt+a", "hotkey_image": "ctrl+alt+s",
           "wiki_base": WIKI_DEFAULT_BASE, "wiki_fallback": WIKI_DEFAULT_FALLBACK,
           "wiki_proxy": WIKI_DEFAULT_PROXY,
           "auto_explain": True, "auto_explain_image": True, "font_size": 10, "theme": "dark", "providers": {}}
    for name, sid in SVCS.items():
        p = os.path.join(STRANSLATE_DIR, sid + ".json")
        if not os.path.exists(p):
            continue
        d = json.load(open(p, encoding="utf-8-sig"))
        try:
            extra = json.loads(d.get("AdditionalParametersJson") or "{}")
        except Exception:
            extra = {}
        cfg["providers"][name] = {
            "base": d.get("Url", "").rstrip("/"),
            "model": d.get("Model", ""),
            "key": d.get("ApiKey", ""),
            "params": extra,
        }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def clip_seq():
    """剪贴板序号：任何程序写入剪贴板都会 +1，用来判断「这次 Ctrl+C 有没有真复制」。"""
    try:
        user32.GetClipboardSequenceNumber.restype = wt.DWORD
        return int(user32.GetClipboardSequenceNumber())
    except Exception:
        return 0


def clip_get_win32():
    """直接读 CF_UNICODETEXT，绕开 Tk 的 OLE 剪贴板。

    Tk 的 clipboard_get 在浏览器/Office 大段复制、或剪贴板被别的进程短暂持有时
    会抛 TclError，被 clip_get 吞成空串，表现就是「窗口出来了但说没取到字」。
    这里用 Win32 再读一遍兜底。
    """
    CF_UNICODETEXT = 13
    try:
        user32.OpenClipboard.argtypes = [wt.HWND]
        user32.OpenClipboard.restype = wt.BOOL
        user32.GetClipboardData.argtypes = [wt.UINT]
        user32.GetClipboardData.restype = wt.HANDLE
        kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
        kernel32.GlobalLock.restype = wt.LPVOID
        kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
        kernel32.GlobalUnlock.restype = wt.BOOL
    except Exception:
        pass
    for _ in range(10):
        if user32.OpenClipboard(None):
            try:
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return ""
                p = kernel32.GlobalLock(h)
                if not p:
                    return ""
                try:
                    return ctypes.wstring_at(p)
                finally:
                    kernel32.GlobalUnlock(h)
            finally:
                try:
                    user32.CloseClipboard()
                except Exception:
                    pass
        time.sleep(0.02)          # 别的进程正占着剪贴板，稍等再开
    return ""


def clip_get(root):
    t = clip_get_win32()
    if t:
        return t
    try:
        return root.clipboard_get()
    except Exception:
        return ""


def clip_set(root, text):
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
    except Exception:
        pass


def wait_modifiers_released(timeout=3.0):
    """等 Alt/Ctrl 松开。热键是 Alt+1，按着不放就发 Ctrl+C 会变成 Alt+Ctrl+C。"""
    end = time.time() + timeout
    while time.time() < end:
        if not (user32.GetAsyncKeyState(0x12) & 0x8000) and not (user32.GetAsyncKeyState(0x11) & 0x8000):
            return True
        time.sleep(0.02)
    return False


def _send_copy(alt=False):
    """发 Ctrl+C；alt=True 发 Ctrl+Insert（终端、老阅读器认这一手）。"""
    VK_CONTROL, KEYUP = 0x11, 0x0002
    vk = 0x2D if alt else 0x43
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.012)
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.030)
    user32.keybd_event(vk, 0, KEYUP, 0)
    time.sleep(0.012)
    user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)


def send_ctrl_c():
    _send_copy(False)


def fg_desc():
    """前台窗口的「类名|标题」，取词日志里用来定位是哪个程序没响应 Ctrl+C。"""
    try:
        user32.GetForegroundWindow.restype = wt.HWND
        hwnd = user32.GetForegroundWindow()
        n = int(user32.GetWindowTextLengthW(hwnd))
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, n + 2)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        return "%s|%s" % (cls.value, buf.value[:24])
    except Exception:
        return "?"


def grab_selection(total=1.5):
    """按 Ctrl+C 取当前选中文字，返回 (text, info)。

    2026-11-06：修「偶尔取不到选中文字」。
    旧做法是先把剪贴板清空、固定等 0.30s 再读——浏览器/Office 大段复制、
    或目标程序复制前先读一次剪贴板时，300ms 根本不够，读到的还是空串。
    现在：记剪贴板序号 → 发 Ctrl+C → 40ms 查一次序号，有新内容立刻读；
    1.5s 内没动静就重发，第 3 次换 Ctrl+Insert；原来的剪贴板一个字都不动。
    """
    seq0 = clip_seq()
    t0 = time.time()
    info = {"seq0": seq0, "tries": 0, "ms": 0, "why": ""}
    attempts = 4
    for i in range(attempts):
        info["tries"] = i + 1
        _send_copy(alt=(i >= 2))
        deadline = time.time() + (0.42 if i < attempts - 1 else 0.35)
        while time.time() < deadline and time.time() - t0 < total:
            time.sleep(0.04)
            seq = clip_seq()
            if seq != seq0:
                text = clip_get_win32() or ""
                info["ms"] = int((time.time() - t0) * 1000)
                info["seq1"] = seq
                if text.strip():
                    info["why"] = "ok"
                    return text, info
                info["why"] = "剪贴板里不是文字"
                return "", info
    info["ms"] = int((time.time() - t0) * 1000)
    info["seq1"] = clip_seq()
    info["why"] = "应用没响应 Ctrl+C"
    return "", info


def virtual_screen():
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def image_to_data_url(img, max_w=1600, quality=85):
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _split_inline(line):
    out, buf, i = [], "", 0
    while i < len(line):
        if line.startswith("**", i):
            j = line.find("**", i + 2)
            if j > 0:
                if buf:
                    out.append((buf, False, False)); buf = ""
                out.append((line[i + 2:j], True, False)); i = j + 2; continue
        if line[i] == "`":
            j = line.find("`", i + 1)
            if j > 0:
                if buf:
                    out.append((buf, False, False)); buf = ""
                out.append((line[i + 1:j], False, True)); i = j + 1; continue
        buf += line[i]; i += 1
    if buf:
        out.append((buf, False, False))
    return out


# ---------------------------------------------------------------- 公式渲染
# mathtext 没有矩阵/分段，只有这几类环境能压成一行画出来
_ENV_LINEAR = ("aligned", "align", "align*", "gather", "gather*", "split",
               "equation", "equation*", "eqnarray", "eqnarray*")


def _math_fix(expr):
    """把常见的 LaTeX 方言收敛到 matplotlib mathtext 认的子集。

    画不了的（\\begin{cases}/\\begin{matrix}、带中文的 \\text）返回 None，调用方回退成原文。
    2026-11-06：补 \\limits / \\boxed / aligned 这几类——模型写推导时常用，mathtext 不认。
    """
    s = expr.strip()
    if not s:
        return None
    # 只有能线性化成一行的环境才接；cases / matrix / array 这些 mathtext 压根没有，回退原文
    envs = re.findall(r"\\begin\{([a-zA-Z*]+)\}", s)
    if any(e not in _ENV_LINEAR for e in envs):
        return None
    for e in envs:
        s = s.replace("\\begin{%s}" % e, "").replace("\\end{%s}" % e, "")
    s = re.sub(r"\\(limits|nolimits)(?![a-zA-Z])", "", s)  # \sum\limits_{} → \sum_{}（后面常紧跟 _，别用 \b）
    s = re.sub(r"\\(tag|label|nonumber|notag)\{[^{}]*\}", "", s)
    s = s.replace("\\boxed{", "{")                        # \boxed{X} 的 {...} 本身就是分组
    pairs = (
        ("\\dfrac", "\\frac"), ("\\tfrac", "\\frac"), ("\\cfrac", "\\frac"),
        ("\\textbf{", "\\mathbf{"), ("\\textit{", "\\mathit{"),
        ("\\text{", "\\mathrm{"), ("\\operatorname{", "\\mathrm{"),
        ("\\displaystyle", ""), ("\\overrightarrow{", "\\vec{"),
        ("\\degree", "^{\\circ}"), ("\\left.", ""), ("\\right.", ""),
        ("\\;", "\\ "), ("\\!", ""), ("\\,", "\\ "),
    )
    for a, b in pairs:
        s = s.replace(a, b)
    s = s.replace("&", " ")
    if "\\\\" in s:                      # 双反斜杠（换行）mathtext 不支持，压成一行
        s = s.replace("\\\\", " \\; ")
    return s.strip() or None


def _rgba(c):
    """'#1e1f22' -> (30, 31, 34, 255)"""
    c = (c or "").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255)
    except Exception:
        return (30, 31, 34, 255)


def clean_image_cache():
    """清空 images\\ 下的截图和公式缓存，返回删掉的个数。

    这些只是运行期缓存：真要留的图，用户在「存笔记」时已经复制进 Obsidian 的
    attachments\\ 了，所以退出时整个清掉不影响笔记；公式图下次用到会重新渲染。
    """
    if not os.path.isdir(CACHE_DIR):
        return 0
    n = 0
    for name in os.listdir(CACHE_DIR):
        p = os.path.join(CACHE_DIR, name)
        try:
            if os.path.isdir(p):
                for _, _, files in os.walk(p):
                    n += len(files)
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
                n += 1
        except OSError as ex:
            log("清理图片缓存失败 %s: %r" % (name, ex))
    return n


def _math_png(expr, color, size=10, dpi=96, max_w=0, bg=None):
    """渲染一条公式成 PNG（背景已按 bg 压平），返回路径；失败返回 None。

    matplotlib 的 mathtext 只能出「白底 + 彩字」，这里把白底当透明度用：
    灰度取反就是 alpha，颜色统一换成前景色，边缘抗锯齿就不会有白边。
    """
    fixed = _math_fix(expr)
    if not fixed:
        return None
    key = hashlib.md5(("v2|%s|%s|%s|%s|%d|%s" % (fixed, color, size, dpi, max_w, bg)).encode("utf-8")).hexdigest()
    path = os.path.join(MATH_DIR, key + ".png")
    if os.path.exists(path):
        return path
    try:
        from matplotlib.font_manager import FontProperties
        from matplotlib.mathtext import math_to_image
    except Exception as ex:
        log("没有 matplotlib，公式只能按原文显示: %r" % ex)
        return None
    buf = io.BytesIO()
    src = "$" + fixed + "$"      # mathtext 只认 $...$ 包起来的表达式，不包会被当成普通文字原样画出来
    try:
        if CJK_RE.search(fixed):
            import matplotlib
            with matplotlib.rc_context(CJK_MATH_RC):
                math_to_image(src, buf, prop=FontProperties(size=size), dpi=dpi, format="png")
        else:
            math_to_image(src, buf, prop=FontProperties(size=size), dpi=dpi, format="png")
    except Exception as ex:
        log("公式渲染失败 %r: %r" % (fixed[:60], ex))
        return None
    buf.seek(0)
    try:
        gray = Image.open(buf).convert("L")
    except Exception:
        return None
    alpha = gray.point(lambda v: 255 - v)      # 白底 -> 全透明，黑字 -> 不透明
    box = alpha.getbbox()
    if not box:
        return None
    im = Image.new("RGBA", gray.size, _rgba(color))
    im.putalpha(alpha)
    im = im.crop(box)
    if max_w and im.width > max_w:
        im = im.resize((int(max_w), max(1, int(im.height * max_w / im.width))), Image.LANCZOS)
    pad = max(2, int(round(size * dpi / 72 * 0.12)))
    canvas = Image.new("RGBA", (im.width + pad * 2, im.height + pad),
                       _rgba(bg) if bg else (0, 0, 0, 0))
    if bg:                                     # Tk 把透明像素画成白色，先按背景色压平
        canvas.alpha_composite(im, (pad, 0))
    else:
        canvas.paste(im, (pad, 0), im)
    try:
        os.makedirs(MATH_DIR, exist_ok=True)
        canvas.save(path, "PNG")
    except Exception:
        return None
    return path


def _split_fences(text):
    """按 ``` 围栏切块：[(是否代码块, 内容)]，围栏那两行本身丢掉。"""
    out, buf, in_code = [], [], False
    for ln in text.split("\n"):
        if ln.strip().startswith("```"):
            out.append((in_code, "\n".join(buf)))
            buf, in_code = [], not in_code
            continue
        buf.append(ln)
    out.append((in_code, "\n".join(buf)))
    return [(c, t) for c, t in out if c or t.strip()]


def _md_segments(text):
    """把 markdown 正文切成 [(kind, ...)]：text / code / (math, 内联|块级, 表达式)。

    反引号里的内容不动，所以 `$HOME` 这种不会被当公式。
    """
    segs, buf, i, n = [], [], 0, len(text)

    def flush():
        if buf:
            segs.append(("text", "".join(buf)))
            buf.clear()

    while i < n:
        if text[i] == "`":
            j = text.find("`", i + 1)
            if j > 0 and "\n" not in text[i + 1:j]:
                flush()
                segs.append(("code", text[i + 1:j]))
                i = j + 1
                continue
        if text.startswith("$$", i) or text.startswith("\\[", i):
            close = "$$" if text.startswith("$$", i) else "\\]"
            start = i + len(close)
            j = text.find(close, start)
            if j > start:
                flush()
                segs.append(("math", "display", text[start:j].strip()))
                i = j + len(close)
                if text.startswith("\n", i):        # 块级公式自带换行，别再多插一行
                    i += 1
                continue
        if text.startswith("\\(", i):
            j = text.find("\\)", i + 2)
            if j > i + 2:
                flush()
                segs.append(("math", "inline", text[i + 2:j].strip()))
                i = j + 2
                continue
        if text[i] == "$":
            j = text.find("$", i + 1)
            nl = text.find("\n", i + 1)
            if j > i + 1 and (nl == -1 or j < nl):
                expr = text[i + 1:j]
                # "$5 到 $9" 这种别当公式：公式两侧不留空格
                if expr.strip() and not expr[:1].isspace() and not expr[-1:].isspace():
                    flush()
                    segs.append(("math", "inline", expr.strip()))
                    i = j + 1
                    continue
        buf.append(text[i])
        i += 1
    flush()
    return segs



# ---------------------------------------------------------------- 维基检索（本地 Kiwix / 官方 Wikipedia）
WIKI_DEFAULT_BASE = "http://192.168.2.99:8060"
WIKI_DEFAULT_FALLBACK = "https://zh.wikipedia.org"   # 本地没开/没结果时改查官方
# 官方维基在国内要翻墙；留空 = 直连（或 requests 自己看到的系统代理变量）
WIKI_DEFAULT_PROXY = ""
WIKI_UA = "Peidu/1.1 (personal study widget)"


def _strip_html(t):
    """HTML -> 纯文本：去脚本/样式/公式，块级标签转换行，压掉多余空白。"""
    t = re.sub(r"(?is)<(script|style|math)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r"(?is)<(br|/p|/div|/li|/h\d|/tr)[^>]*>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"{\displaystyle[^}]*}", " ", t)
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


_WIKI_BOOKS = {}          # (base,lang) -> (时间戳, [ZIM 文件名])
_WIKI_SUGGEST_OK = {}     # (base,book) -> 有没有标题索引


def _wiki_books(base, lang, ttl=600):
    """列出某语言的 ZIM 文件名（从 /catalog 取，缓存 10 分钟）。"""
    key = (base, lang)
    now = time.time()
    got = _WIKI_BOOKS.get(key)
    if got:
        # 取书单失败时只压 30 秒：服务刚起来 / 地址换回来能马上重试，不用等满 10 分钟
        if now - got[0] < (ttl if got[1] else 30):
            return got[1]
    books = []
    try:
        r = requests.get(base.rstrip("/") + "/catalog/v2/entries",
                         params={"count": -1}, timeout=10)
        r.raise_for_status()
        for ent in re.findall(r"<entry>(.*?)</entry>", r.text, re.S):
            m = re.search(r"<language>([^<]*)</language>", ent)
            if not m or m.group(1).strip() != lang:
                continue
            m2 = re.search(r'<link type="text/html" href="/content/([^"]+)"', ent)
            if m2:
                books.append(html.unescape(m2.group(1)))
    except Exception as ex:
        log("取书单失败(%s): %r" % (lang, ex))
    _WIKI_BOOKS[key] = (now, books)
    return books


def wiki_title_hits(base, query, lang="zho", limit=4):
    """按标题找条目（/suggest 走标题索引）。

    Kiwix 的全文检索不给标题加权：搜「数学」命中两万多条，条目「数学」本身排在百名开外，
    只取前几条永远拿不到它。所以先按标题把同名/前缀条目捞出来，排在全文结果前面。
    """
    base = (base or WIKI_DEFAULT_BASE).rstrip("/")
    out = []
    for book in _wiki_books(base, lang):
        if _WIKI_SUGGEST_OK.get((base, book)) is False:
            continue
        try:
            r = requests.get(base + "/suggest",
                             params={"content": book, "term": query}, timeout=8)
            if r.status_code != 200:
                _WIKI_SUGGEST_OK[(base, book)] = False   # 这本书没标题索引，别再问
                continue
            data = r.json()
        except Exception as ex:
            log("标题联想失败 %s/%s: %r" % (book, query, ex))
            continue
        _WIKI_SUGGEST_OK[(base, book)] = True
        for it in data:
            if it.get("kind") != "path":
                continue
            path = it.get("path") or ""
            name = it.get("value") or path
            if not path or (query not in name and query not in path):
                continue
            out.append({"href": "/content/%s/%s" % (book, quote(path, safe="/")),
                        "title": name, "snippet": ""})
    out.sort(key=lambda h: (0 if h["title"] == query else
                            1 if h["title"].startswith(query) else 2, len(h["title"])))
    seen, uniq = set(), []
    for h in out:
        if h["href"] not in seen:
            seen.add(h["href"])
            uniq.append(h)
    return uniq[:limit]


def wiki_search(base, query, lang="zho", limit=8):
    """在本地 Kiwix 里搜，返回 [{href,title,snippet}]。

    服务要求按语言过滤（books.filter.lang），否则多语言混搜直接 400。
    标题命中的条目排在最前：中文短词（数学/物理）纯按词频排序时条目本身会掉出前几十名。
    """
    base = (base or WIKI_DEFAULT_BASE).rstrip("/")
    hits = []
    try:
        hits += wiki_title_hits(base, query, lang)
    except Exception as ex:
        log("标题检索失败 %s: %r" % (query, ex))
    url = base + "/search"
    r = requests.get(url, params={"pattern": query, "books.filter.lang": lang,
                                  "pageLength": limit}, timeout=12)
    r.raise_for_status()
    txt = r.content.decode("utf-8", "replace")
    seen = {h["href"] for h in hits}
    for m in re.finditer(r'<li>\s*<a href="([^"]+)"[^>]*>\s*(.*?)\s*</a>\s*<cite>(.*?)</cite>',
                         txt, re.S):
        if m.group(1) in seen:
            continue
        seen.add(m.group(1))
        hits.append({"href": m.group(1), "title": _strip_html(m.group(2)),
                     "snippet": _strip_html(m.group(3))})
    return hits


def wiki_article(base, href, limit=1400):
    """抓一个条目，取正文前 limit 字。"""
    r = requests.get((base or WIKI_DEFAULT_BASE).rstrip("/") + href, timeout=15)
    r.raise_for_status()
    t = _strip_html(r.content.decode("utf-8", "replace"))
    t = re.sub(r"\[\d+\]", "", t)        # 去掉 [1] 这种引用编号
    lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
    body = []
    for ln in lines:
        if not body and len(ln) < 8:        # 开头常是标题重复/目录，跳过
            continue
        if body and ln == body[-1]:         # 标题在两行里重复出现
            continue
        body.append(ln)
    return "\n".join(body)[:limit]


def _wiki_kind(base):
    """地址是本地 Kiwix 还是官方 MediaWiki（wikipedia.org）。"""
    b = (base or "").lower()
    return "mediawiki" if ("wikipedia.org" in b or "mediawiki" in b or "/api.php" in b) else "kiwix"


def _wiki_alive(base, timeout=2.0):
    """Kiwix 地址能不能连上（TCP 探测）：NAS 没开时直接跳过，别对着关机的机器逐个词等超时。"""
    try:
        u = urlsplit(base if "://" in base else "http://" + base)
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((u.hostname, port), timeout=timeout):
            return True
    except Exception:
        return False


def _mw_host(base, lang="zho"):
    """把用户填的地址归一到「协议+域名」（粘贴条目页 URL 也能用），再按语言换域名。"""
    b = (base or "").strip().rstrip("/")
    for suf in ("/w/api.php", "/api.php"):
        if b.endswith(suf):
            b = b[: -len(suf)]
    if b and not re.match(r"^https?://", b, re.I):
        b = "https://" + b          # 只写了域名也能用
    m = re.match(r"^(https?://[^/]+)", b, re.I)
    if m:
        b = m.group(1)
    if lang == "eng":
        b = re.sub(r"//[a-z-]+\.wikipedia\.org", "//en.wikipedia.org", b, count=1, flags=re.I)
    elif lang == "zho":
        b = re.sub(r"//en\.wikipedia\.org", "//zh.wikipedia.org", b, count=1, flags=re.I)
    return b.rstrip("/")


def _wiki_proxies(proxy):
    p = (proxy or "").strip()
    return {"http": p, "https": p} if p else None


def _mw_api(base, params, proxy="", timeout=12):
    """官方维基的 Action API（JSON）。base 可以是域名，也可以是 …/w/api.php。"""
    url = (base or "").rstrip("/")
    if not url.endswith("api.php"):
        url += "/w/api.php"
    q = {"format": "json", "formatversion": 2, "utf8": 1}
    if re.search(r"//zh\.wikipedia\.org", url, re.I):
        q["variant"] = "zh-cn"        # 官方中文维基默认可能回繁体，统一转简体
    q.update(params)
    r = None
    for attempt in range(2):
        r = requests.get(url, params=q, headers={"User-Agent": WIKI_UA},
                         proxies=_wiki_proxies(proxy), timeout=timeout)
        if r.status_code == 429 and attempt == 0:
            # 走公共代理时出口 IP 可能被维基限流；按 Retry-After 等一次再试
            try:
                wait = float(r.headers.get("Retry-After") or 3)
            except Exception:
                wait = 3.0
            wait = max(1.0, min(wait, 5.0))
            log("官方维基限流(429)，%.1fs 后重试" % wait)
            time.sleep(wait)
            continue
        break
    r.raise_for_status()
    return r.json()


def mw_title_hits(base, query, lang="zho", limit=4, proxy=""):
    """官方维基按标题前缀找条目（prefixsearch）。"""
    b = _mw_host(base, lang)
    data = _mw_api(b, {"action": "query", "list": "prefixsearch",
                       "pssearch": query, "pslimit": limit}, proxy=proxy)
    out = []
    for it in ((data.get("query") or {}).get("prefixsearch") or []):
        t = (it.get("title") or "").strip()
        if not t:
            continue
        out.append({"href": b + "/wiki/" + quote(t.replace(" ", "_"), safe="/"),
                    "title": t, "snippet": "", "base": b})
    return out


def mw_search(base, query, lang="zho", limit=8, proxy=""):
    """官方维基全文搜索（list=search）。"""
    b = _mw_host(base, lang)
    data = _mw_api(b, {"action": "query", "list": "search", "srsearch": query,
                       "srlimit": limit, "srprop": "snippet"}, proxy=proxy)
    out = []
    for it in ((data.get("query") or {}).get("search") or []):
        t = (it.get("title") or "").strip()
        if not t:
            continue
        out.append({"href": b + "/wiki/" + quote(t.replace(" ", "_"), safe="/"),
                    "title": t, "snippet": _strip_html(it.get("snippet") or ""), "base": b})
    return out


def mw_article(base, title, limit=1400, proxy=""):
    """官方维基取条目正文（TextExtracts 的纯文本）。"""
    data = _mw_api(base, {"action": "query", "prop": "extracts", "explaintext": 1,
                          "exsectionformat": "plain", "redirects": 1,
                          "titles": title, "exlimit": 1}, proxy=proxy)
    pages = (data.get("query") or {}).get("pages") or []
    if isinstance(pages, dict):
        pages = list(pages.values())
    if not pages:
        return ""
    txt = (pages[0].get("extract") or "").strip()
    txt = re.sub(r"\{\\?displaystyle[^}]*\}", " ", txt)      # 纯文本里的公式残留
    txt = re.sub(r"[ \t]+", " ", txt)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()[:limit]


def wiki_search_any(base, query, lang="zho", proxy=""):
    """按地址类型分流：Kiwix 走 /suggest + /search，官方维基走 MediaWiki API。"""
    if _wiki_kind(base) == "mediawiki":
        b = _mw_host(base, lang)
        hits = []
        try:
            hits = mw_title_hits(b, query, lang, proxy=proxy)
        except Exception as ex:
            log("官方标题检索失败 %s: %r" % (query, ex))
        seen = {h["href"] for h in hits}
        for h in mw_search(b, query, lang, proxy=proxy):
            if h["href"] not in seen:
                seen.add(h["href"])
                hits.append(h)
        return hits
    return wiki_search(base, query, lang)


_CC_CACHE = {}


def _zh_variants(q):
    """检索词的简↔繁变体（用 opencc；没装就只查原词）。

    中文维基里很多条目只有一种字形的标题（蜀道難 有、蜀道难 搜不到），
    只查一种字形会漏，所以两种都查、结果合并。
    """
    out = [q]
    try:
        import opencc
        for cfg in ("s2t", "t2s"):
            cc = _CC_CACHE.get(cfg)
            if cc is None:
                cc = opencc.OpenCC(cfg)
                _CC_CACHE[cfg] = cc
            v = cc.convert(q)
            if v and v != q and v not in out:
                out.append(v)
    except Exception as ex:
        log("简繁转换不可用，只查原词: %r" % ex)
    return out



def _parse_keywords(txt):
    """把模型吐的关键词整理成列表（容忍编号、顿号、换行、书名号）。"""
    out = []
    for part in re.split(r"[\n、,，;；|/]+", txt or ""):
        w = part.strip()
        w = re.sub(r"^\s*(?:[-*·•]|\d+[.、)])\s*", "", w)
        w = w.strip("「」『』《》\"'()（）[]【】 ")
        if 2 <= len(w) <= 24 and w not in out:
            out.append(w)
    return out[:8]


def _extract_keywords(prov, img, timeout=90):
    """让模型看一眼图，提炼 5~8 个适合查百科的关键词（一次短请求，不流式）。"""
    base = prov["base"].rstrip("/")
    url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
    payload = {
        "model": prov["model"], "stream": False, "temperature": 0.2, "max_tokens": 160,
        "messages": [
            {"role": "system", "content": "你看图，提炼 5~8 个能在百科里查到的关键词：专有名词、术语、"
                                          "人名地名书名标题优先。只输出关键词，一行一个，不要编号、不要解释。"},
            {"role": "user", "content": [
                {"type": "text", "text": "这张图里有哪些值得查百科的词？"},
                {"type": "image_url", "image_url": {"url": image_to_data_url(img)}},
            ]},
        ],
    }
    for k, v in (prov.get("params") or {}).items():
        if k not in ("model", "messages", "stream"):
            payload[k] = v
    headers = {"Authorization": "Bearer " + prov.get("key", ""), "Content-Type": "application/json"}
    r = requests.post(url, json=payload, headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError("HTTP %s: %s" % (r.status_code, r.text[:200]))
    data = r.json()
    txt = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return _parse_keywords(txt)


def wiki_context(base, query, max_articles=2, fallback="", proxy=""):
    """搜索 + 取正文，拼成给模型看的【维基资料】。

    返回 (资料文本, 条目名列表, 来源说明)。主地址没结果或连不上时再试备用地址
    （默认官方维基）；官方维基在国内要代理，代理地址由 wiki_proxy 配置提供。
    """
    queries = [query] if isinstance(query, str) else [q for q in query if q]
    variants = []
    for q in queries:
        for v in _zh_variants(q):
            if v and v not in variants:
                variants.append(v)

    bases = []
    for b in (base, fallback):
        b = (b or "").strip().rstrip("/")
        if b and b not in bases:
            bases.append(b)

    for b in bases:
        kind = _wiki_kind(b)
        if kind == "kiwix" and not _wiki_alive(b):
            log("维基地址连不上，跳过：%s" % b)
            continue
        hits = []
        for lang in ("zho", "eng"):
            for qv in variants[:8]:
                try:
                    hits += wiki_search_any(b, qv, lang, proxy)
                except Exception as ex:
                    log("维基搜索失败 %s/%s/%s: %r" % (kind, lang, qv, ex))
            if hits:
                break                      # 中文库有结果就不用翻英文
        if not hits:
            continue

        seen, uniq = set(), []
        for h in hits:
            if h["href"] in seen:
                continue
            seen.add(h["href"])
            if h["title"].startswith(("Category:", "分类:", "分類:")):
                continue                      # 分类页不是条目，别占名额
            uniq.append(h)
        if not uniq:
            continue

        def rank(h):
            t = h["title"]
            snip = h.get("snippet", "")
            matched = 0
            best = 2
            v_idx = 99                     # 命中的是第几个关键词（越靠前越重要）
            for i, v in enumerate(variants):
                if t == v:
                    best = 0
                    v_idx = min(v_idx, i)
                elif v in t or t in v:
                    best = min(best, 1)
                    v_idx = min(v_idx, i)
                if v in t or v in snip or t in v:
                    matched -= 1
            near = 0 if ("wikipedia" in h["href"] or "/wiki/" in h["href"]) else 1
            return (near, best, v_idx, matched)

        parts, srcs, seen_bodies = [], [], set()
        for h in sorted(uniq, key=rank)[:max(1, max_articles) * 3]:
            if len(parts) >= max_articles:
                break
            try:
                if kind == "mediawiki":
                    body = mw_article(h.get("base") or b, h["title"], proxy=proxy)
                else:
                    body = wiki_article(b, h["href"])
            except Exception as ex:
                log("维基条目读取失败 %r: %r" % (h["href"], ex))
                body = h.get("snippet", "")
            if not body:
                continue
            key = body[:300]
            if key in seen_bodies:
                continue                  # 简繁两条通常是同一个条目，别重复占名额
            seen_bodies.add(key)
            parts.append("【条目：%s】\n%s" % (h["title"], body))
            srcs.append(h["title"])
        if parts:
            label = "官方维基" if kind == "mediawiki" else "本地 Kiwix"
            return "【维基资料】\n" + "\n\n".join(parts), srcs, label
    return "", [], ""

# ---------------------------------------------------------------- 截图框选
class RegionPicker:
    def __init__(self, root, callback):
        self.root = root
        self.callback = callback
        vx, vy, vw, vh = virtual_screen()
        self.vx, self.vy = vx, vy
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{vw}x{vh}{vx:+d}{vy:+d}")
        try:
            self.win.attributes("-alpha", 0.30)
        except Exception:
            pass
        self.canvas = tk.Canvas(self.win, bg="#101010", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(vw // 2, 46, text="拖动框选要问的区域 · Esc 取消",
                                fill="#ffffff", font=("Microsoft YaHei UI", 16, "bold"))
        self.start = None
        self.rect = None
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.win.bind("<Escape>", lambda e: self.finish(None))
        self.win.focus_force()

    def on_press(self, e):
        self.start = (e.x, e.y)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline=ACCENT, width=2)

    def on_move(self, e):
        if self.rect and self.start:
            self.canvas.coords(self.rect, self.start[0], self.start[1], e.x, e.y)

    def on_release(self, e):
        if not self.start:
            return self.finish(None)
        x1, y1 = self.start
        box = (min(x1, e.x), min(y1, e.y), max(x1, e.x), max(y1, e.y))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            return self.finish(None)
        self.win.withdraw()
        self.root.after(140, lambda: self.grab(box))

    def grab(self, box):
        try:
            full = ImageGrab.grab(bbox=(self.vx + box[0], self.vy + box[1],
                                        self.vx + box[2], self.vy + box[3]), all_screens=True)
            os.makedirs(CACHE_DIR, exist_ok=True)
            path = os.path.join(CACHE_DIR, time.strftime("shot_%Y%m%d_%H%M%S.png"))
            full.save(path)
            self.finish((full, path))
        except Exception as ex:
            log("截图失败: %r" % ex)
            self.finish(None)

    def finish(self, result):
        try:
            self.win.grab_release()
            self.win.destroy()
        except Exception:
            pass
        self.callback(result)


# ---------------------------------------------------------------- 主窗口
class PeiduApp:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        provs = list(self.cfg.get("providers") or {})
        self.provider = self.cfg.get("default") if self.cfg.get("default") in provs else (provs[0] if provs else "")
        self.messages = []
        self.quote_text = ""
        self.quote_image = None
        self.busy = False
        self.cancel_flag = threading.Event()
        self.q = queue.Queue()
        self.tokens = 0
        self.t0 = 0.0
        self.stream_start = None
        self.img_paths = []
        self.img_by_idx = {}
        self.img_saved = {}
        self.chat_imgs = []
        self.shot_at = {}
        self._math_photos = {}
        self.pinned = bool(self.cfg.get("pin", True))
        self.has_tray = False
        self.prompts = load_prompts()
        names = [p["name"] for p in self.prompts]
        self.prompt_name = self.cfg.get("default_prompt") if self.cfg.get("default_prompt") in names else names[0]

        self.theme = self.cfg.get("theme", "dark")
        set_palette(self.theme)
        self.build_ui()
        self.apply_pin()
        self.start_tray()
        self.start_hotkeys()
        self.root.after(60, self.pump)
        self.root.after(600, self.watch_show_request)

    # ---------------- UI
    def build_ui(self):
        r = self.root
        r.title("陪读")
        r.geometry("620x820+80+50")
        r.minsize(460, 520)
        r.configure(bg=BG)
        r.attributes("-topmost", True)
        r.protocol("WM_DELETE_WINDOW", self.hide)
        r.bind("<Escape>", lambda e: self.hide())
        r.bind("<Unmap>", self.on_unmap)

        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=14, pady=(12, 0))
        tk.Label(head, text="陪读", bg=BG, fg=FG, font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        self.model_var = tk.StringVar(value=self.provider)
        cb = ttk.Combobox(head, textvariable=self.model_var, values=list(self.cfg.get("providers") or {}),
                          width=9, state="readonly")
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", lambda e: self.set_provider(self.model_var.get()))
        self.model_cb = cb
        self.pin_btn = tk.Button(head, text="📌 已锁定", bg=BG, fg=ACCENT, bd=0, font=FONT_S,
                                 activebackground=BG, activeforeground=FG, command=self.toggle_pin)
        self.pin_btn.pack(side="right", padx=(0, 10))
        tk.Button(head, text="⚙ 参数", bg=BG, fg=MUTED, bd=0, font=FONT_S, activebackground=BG,
                  activeforeground=FG, command=self.open_settings).pack(side="right", padx=(0, 10))
        tk.Button(head, text="⌨ 热键", bg=BG, fg=MUTED, bd=0, font=FONT_S, activebackground=BG,
                  activeforeground=FG, command=self.open_hotkeys).pack(side="right", padx=(0, 10))

        prow = tk.Frame(r, bg=BG)
        prow.pack(fill="x", padx=14, pady=(4, 2))
        tk.Label(prow, text="提示词", bg=BG, fg=MUTED, font=FONT_S).pack(side="left")
        self.prompt_var = tk.StringVar(value=self.prompt_name)
        pc = ttk.Combobox(prow, textvariable=self.prompt_var, values=[p["name"] for p in self.prompts],
                          width=16, state="readonly")
        pc.pack(side="left", padx=(6, 6))
        pc.bind("<<ComboboxSelected>>", lambda e: self.set_prompt(self.prompt_var.get()))
        self.prompt_cb = pc
        tk.Button(prow, text="编辑", bg=PANEL, fg=FG, bd=0, font=FONT_S, activebackground=BORDER,
                  activeforeground=FG, command=self.open_prompts).pack(side="left")
        # 截图转公式：一键切档 + 框选，物理老师做讲义的高频入口
        self.formula_btn = tk.Button(prow, text="📐 截公式", bg=PANEL, fg=MUTED, bd=0, font=FONT_S,
                                     activebackground=BORDER, activeforeground=FG, cursor="hand2",
                                     command=self.grab_formula)
        self.formula_btn.pack(side="left", padx=(6, 0))
        # 主界面开关行：左边「查维基」（高频，勾上亮强调色），右边两个「自动」（设置型、低频）
        orow = tk.Frame(r, bg=BG)
        orow.pack(fill="x", padx=14, pady=(0, 2))
        self.wiki_var = tk.BooleanVar(value=bool(self.cfg.get(
            "wiki_enabled", bool((self.current_prompt() or {}).get("wiki")))))
        self.wiki_cb = tk.Checkbutton(orow, text="查维基", variable=self.wiki_var, bg=BG,
                                      fg=ACCENT if self.wiki_var.get() else MUTED,
                                      selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                                      font=FONT_S, bd=0, highlightthickness=0,
                                      command=self.toggle_wiki)
        self.wiki_cb.pack(side="left")
        self.auto_var = tk.BooleanVar(value=bool(self.cfg.get("auto_explain", True)))
        self.auto_img_var = tk.BooleanVar(value=bool(self.cfg.get("auto_explain_image", True)))
        tk.Checkbutton(orow, text="截图后自动", variable=self.auto_img_var, bg=BG, fg=MUTED,
                       selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                       font=FONT_S, bd=0, highlightthickness=0,
                       command=self.toggle_auto_img).pack(side="right")
        tk.Checkbutton(orow, text="取词后自动", variable=self.auto_var, bg=BG, fg=MUTED,
                       selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                       font=FONT_S, bd=0, highlightthickness=0,
                       command=self.toggle_auto).pack(side="right", padx=(0, 12))

        # 引用区（默认不显示）
        self.quote_frame = tk.Frame(r, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        qh = tk.Frame(self.quote_frame, bg=PANEL)
        qh.pack(fill="x", padx=8, pady=(6, 0))
        self.quote_title = tk.Label(qh, text="选中的文字", bg=PANEL, fg=MUTED, font=FONT_S)
        self.quote_title.pack(side="left")
        tk.Button(qh, text="×", bg=PANEL, fg=MUTED, bd=0, font=FONT_S, activebackground=PANEL,
                  activeforeground=FG, command=self.clear_quote).pack(side="right")
        self.quote_body = tk.Text(self.quote_frame, height=5, wrap="word", bg=PANEL2, fg=FG,
                                  font=FONT_S, bd=0, padx=8, pady=6, insertbackground=FG,
                                  spacing1=1, spacing3=2)
        self.quote_thumb = tk.Label(self.quote_frame, bg=PANEL)

        # 对话区
        self.chat_wrap = tk.Frame(r, bg=BG)
        self.chat = tk.Text(self.chat_wrap, bg=BG, fg=FG, font=FONT, wrap="word", bd=0, height=12,
                            padx=2, pady=2, insertbackground=FG, spacing1=2, spacing3=5)
        sb = tk.Scrollbar(self.chat_wrap, command=self.chat.yview, width=10)
        self.chat.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.chat.pack(side="left", fill="both", expand=True)
        self.chat.tag_configure("user", foreground=USER_FG, font=("Microsoft YaHei UI", 10, "bold"),
                                spacing1=10)
        self.chat.tag_configure("bot", foreground=FG)
        self.chat.tag_configure("bold", font=("Microsoft YaHei UI", 10, "bold"))
        self.chat.tag_configure("code", font=MONO, background=PANEL2, foreground=CODE_FG)
        self.chat.tag_configure("err", foreground=ERR_FG)
        self.chat.tag_configure("muted", foreground=MUTED, font=FONT_S)
        self.chat.bind("<Button-1>", self.on_chat_click)
        self.chat.configure(state="disabled")
        self.chat_imgs = []
        self.shot_at = {}
        self.code_btns = []

        # 底部状态栏（先 pack，落在最底）
        foot = tk.Frame(r, bg=BG)
        foot.pack(side="bottom", fill="x", padx=16, pady=(0, 8))
        self.status = tk.Label(foot, text="就绪", bg=BG, fg=MUTED, font=FONT_S, anchor="w")
        tk.Button(foot, text="☀" if self.theme == "dark" else "🌙", bg=BG, fg=MUTED, bd=0, font=FONT_S,
                  activebackground=BG, activeforeground=FG,
                  command=self.toggle_theme).pack(side="right", padx=(0, 8))
        tk.Button(foot, text="A+", bg=BG, fg=MUTED, bd=0, font=FONT_S, activebackground=BG,
                  activeforeground=FG, command=lambda: self.change_font(1)).pack(side="right", padx=(0, 8))
        tk.Button(foot, text="A-", bg=BG, fg=MUTED, bd=0, font=FONT_S, activebackground=BG,
                  activeforeground=FG, command=lambda: self.change_font(-1)).pack(side="right", padx=(0, 4))
        tk.Button(foot, text="隐藏", bg=BG, fg=MUTED, bd=0, font=FONT_S, activebackground=BG,
                  activeforeground=FG, command=self.hide).pack(side="right", padx=(0, 10))
        tk.Button(foot, text="退出", bg=BG, fg=MUTED, bd=0, font=FONT_S, activebackground=BG,
                  activeforeground=ERR_FG, command=self.quit).pack(side="right")
        # 状态文字最后 pack：先让右侧按钮占好位，文字过长时被裁掉，不会盖住按钮
        self.status.pack(side="left", fill="x", expand=True)

        # 输入区
        inp = tk.Frame(r, bg=BG)
        inp.pack(side="bottom", fill="x", padx=14, pady=(2, 4))
        self.entry = tk.Text(inp, height=3, width=10, wrap="word", bg=PANEL, fg=FG, font=FONT, bd=0,
                             padx=10, pady=8, insertbackground=FG, highlightthickness=1,
                             highlightbackground=BORDER, highlightcolor=ACCENT)
        self.entry.bind("<Return>", self.on_enter)

        side = tk.Frame(inp, bg=BG)
        side.pack(side="right", padx=(8, 0))
        self.send_btn = tk.Button(side, text="发送", bg=ACCENT, fg=BTN_FG, bd=0, font=FONT,
                                  activebackground=ACCENT_ACTIVE, activeforeground=BTN_FG,
                                  command=self.on_send, width=7, height=2)
        self.send_btn.pack(fill="x")
        # 新话题 / 存笔记：与「发送」同宽竖排，新话题做得更显眼（高频，按 Ctrl+N 也行）
        self.new_btn = tk.Button(side, text="新话题", bg=BTN_BG, fg=ACCENT, bd=0, font=FONT,
                                 width=7, height=2, cursor="hand2",
                                 activebackground=BORDER, activeforeground=ACCENT,
                                 command=self.new_topic)
        self.new_btn.pack(fill="x", pady=(6, 0))
        self.note_btn = tk.Button(side, text="存笔记", bg=BTN_BG, fg=MUTED, bd=0, font=FONT_S,
                                  width=7, cursor="hand2",
                                  activebackground=BORDER, activeforeground=FG,
                                  command=self.save_note)
        self.note_btn.pack(fill="x", pady=(5, 0))
        self._add_hover(self.new_btn)
        self._add_hover(self.note_btn)
        self.entry.pack(side="left", fill="both", expand=True)

        self.chat_wrap.pack(fill="both", expand=True, padx=14, pady=6)
        self.apply_fonts()
        self.entry.focus_set()
        # Ctrl+N = 新话题（焦点在输入框里也能用；Text 自己的类绑定会先吃掉，所以 entry 上单独绑一份）
        r.bind("<Control-n>", self.on_new_topic_key)
        self.entry.bind("<Control-n>", self.on_new_topic_key)

    def on_new_topic_key(self, e=None):
        self.new_topic()
        return "break"

    @staticmethod
    def _add_hover(btn):
        """鼠标悬停时底色提亮，让这两个小按钮看起来能点。"""
        normal = btn.cget("bg")
        btn.bind("<Enter>", lambda e: btn.config(bg=BORDER))
        btn.bind("<Leave>", lambda e: btn.config(bg=normal))

    # ---------------- 生命周期
    def start_hotkeys(self):
        def loop():
            self.hk_tid = kernel32.GetCurrentThreadId()
            self.register_hotkeys()
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
                if msg.message == WM_HOTKEY:
                    log("热键触发 %s" % msg.wParam)
                    self.q.put(("hotkey", "text" if msg.wParam == 1 else "image"))
                elif msg.message == WM_HK_RELOAD:
                    self.register_hotkeys()

        threading.Thread(target=loop, daemon=True).start()

    def register_hotkeys(self):
        user32.UnregisterHotKey(None, 1)
        user32.UnregisterHotKey(None, 2)
        m1, v1 = parse_hotkey(self.cfg.get("hotkey_text", "ctrl+alt+a"))
        m2, v2 = parse_hotkey(self.cfg.get("hotkey_image", "ctrl+alt+s"))
        ok1 = user32.RegisterHotKey(None, 1, m1, v1)
        ok2 = user32.RegisterHotKey(None, 2, m2, v2)
        la, lb = hotkey_label(m1, v1), hotkey_label(m2, v2)
        log("热键注册 %s=%s %s=%s" % (la, bool(ok1), lb, bool(ok2)))
        msg = "%s 取词 · %s 截图" % (la, lb)
        if not (ok1 and ok2):
            msg += "（有冲突，可能被别的软件占了）"
        self.q.put(("status", msg))

    def reload_hotkeys(self):
        try:
            user32.PostThreadMessageW(self.hk_tid, WM_HK_RELOAD, 0, 0)
        except Exception as ex:
            log("重载热键失败: %r" % ex)

    def set_provider(self, name):
        self.provider = name
        self.model_var.set(name)
        self.cfg["default"] = name
        self.save_cfg()

    # ---------------- 锁定 / 提示词 / 参数
    def save_cfg(self):
        try:
            if not (self.cfg.get("providers") or {}):
                log("配置里没有服务，跳过保存以免覆盖原配置")
                return
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
            if os.path.exists(CONFIG_PATH):
                try:
                    shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak")
                except Exception:
                    pass
            os.replace(tmp, CONFIG_PATH)
        except Exception as ex:
            log("保存配置失败: %r" % ex)

    def toggle_theme(self):
        if self.busy:
            self.status.config(text="正在回答，等它说完再换颜色")
            return
        new = "light" if self.theme == "dark" else "dark"
        saved = {
            "messages": list(self.messages),
            "quote": self.quote_text,
            "image": self.quote_image,
            "entry": self.entry.get("1.0", "end-1c"),
        }
        self.theme = new
        self.cfg["theme"] = new
        self.save_cfg()
        set_palette(new)
        for w in self.root.winfo_children():
            w.destroy()
        self.build_ui()
        self.apply_fonts()
        self.apply_pin()
        if saved["entry"].strip():
            self.entry.insert("1.0", saved["entry"])
        if saved["image"]:
            self.set_quote(image=saved["image"])
        elif saved["quote"]:
            self.set_quote(text=saved["quote"])
        self.messages = saved["messages"]
        if self.messages:
            self.replay_chat()
        self.status.config(text="已切换到%s" % ("亮色" if new == "light" else "暗色"))

    def replay_chat(self):
        self._drop_code_btns()
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        for idx, m in enumerate(self.messages):
            if m["role"] == "system":
                continue
            c = m["content"]
            if m["role"] == "user":
                if isinstance(c, list):
                    txt = next((x.get("text", "") for x in c if x.get("type") == "text"), "")
                    has_img = any(x.get("type") == "image_url" for x in c)
                else:
                    txt = c
                    has_img = False
                txt = txt.replace("【选中原文】\n", "〔引用〕").replace("【问题】", "\n→ ")
                self.chat.insert("end", "我：" + txt.strip() + "\n", "user")
                src = (getattr(self, "img_by_idx", {}) or {}).get(idx)
                if has_img:
                    self.chat.insert("end", "〔截图〕" + (os.path.basename(src) if src else "") + "\n", "muted")
                if has_img and src:
                    self.insert_shot(src)
            else:
                self.chat.insert("end", "陪读：", "bot")
                self.insert_markdown(c, "bot")
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def change_font(self, delta):
        size = int(self.cfg.get("font_size", 10)) + delta
        size = max(8, min(20, size))
        if size == self.cfg.get("font_size"):
            return
        self.cfg["font_size"] = size
        self.save_cfg()
        self.apply_fonts()
        self.status.config(text="字号 %d" % size)

    def apply_fonts(self):
        s = int(self.cfg.get("font_size", 10))
        base = ("Microsoft YaHei UI", s)
        small = ("Microsoft YaHei UI", max(7, s - 1))
        bold = ("Microsoft YaHei UI", s, "bold")
        mono = ("Consolas", s)
        self.chat.configure(font=base, spacing1=max(2, s // 4), spacing3=max(4, s // 2))
        self.chat.tag_configure("user", foreground=USER_FG, font=bold, spacing1=max(6, s))
        self.chat.tag_configure("bold", font=bold)
        self.chat.tag_configure("code", font=mono, background=PANEL2, foreground=CODE_FG)
        self.chat.tag_configure("muted", foreground=MUTED, font=small)
        self.chat.tag_configure("err", foreground=ERR_FG, font=base)
        self.chat.tag_configure("bot", foreground=FG, font=base)
        self.entry.configure(font=base)
        self.quote_body.configure(font=small)
        self.status.configure(font=small)

    def toggle_auto(self):
        self.cfg["auto_explain"] = bool(self.auto_var.get())
        self.save_cfg()
        self.status.config(text="取词后自动解释：%s" % ("开" if self.auto_var.get() else "关"))

    def toggle_auto_img(self):
        self.cfg["auto_explain_image"] = bool(self.auto_img_var.get())
        self.save_cfg()
        self.status.config(text="截图后自动解释：%s" % ("开" if self.auto_img_var.get() else "关"))

    def toggle_wiki(self):
        """主界面的「查维基」：全局开关，跟提示词无关，存 config 跨重启保持。"""
        on = bool(self.wiki_var.get())
        self.cfg["wiki_enabled"] = on
        self.save_cfg()
        try:
            self.wiki_cb.config(fg=ACCENT if on else MUTED)
        except Exception:
            pass
        self.status.config(text="查维基：%s" % ("开" if on else "关"))

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.apply_pin()

    def apply_pin(self):
        try:
            self.root.attributes("-topmost", bool(self.pinned))
        except Exception:
            pass
        self.pin_btn.config(text="📌 已锁定" if self.pinned else "📌 未锁定",
                            fg=ACCENT if self.pinned else MUTED)
        self.cfg["pin"] = bool(self.pinned)
        self.save_cfg()

    def current_prompt(self):
        for p in self.prompts:
            if p["name"] == self.prompt_name:
                return p
        return self.prompts[0]

    def current_system(self):
        return self.current_prompt().get("system") or SYSTEM_PROMPT

    def set_prompt(self, name):
        if name == self.prompt_name:
            return
        self.prompt_name = name
        self.prompt_var.set(name)
        self.cfg["default_prompt"] = name
        self.save_cfg()
        if self.messages:
            self.messages[0] = {"role": "system", "content": self.current_system()}
            self.append_chat("— 提示词：%s —" % name, "muted")

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("服务设置")
        win.configure(bg=BG)
        win.geometry("540x800")
        win.transient(self.root)
        win.attributes("-topmost", bool(self.pinned))

        # 底部按钮先占位：上面内容再多，也不会把「保存/取消」挤成看不见
        foot = tk.Frame(win, bg=BG)
        foot.pack(side="bottom", fill="x", padx=14, pady=(0, 12))

        tk.Label(win, text="服务", bg=BG, fg=MUTED, font=FONT_S).pack(anchor="w", padx=14, pady=(12, 2))
        pname = tk.StringVar(value=self.provider)
        pc = ttk.Combobox(win, textvariable=pname, values=list(self.cfg.get("providers") or {}),
                          state="readonly")
        pc.pack(fill="x", padx=14)
        prow = tk.Frame(win, bg=BG)
        prow.pack(fill="x", padx=14, pady=(6, 0))

        def add_provider():
            name = simpledialog.askstring("添加服务", "服务名称（例如 deepseek2 / 本地2）", parent=win)
            if not name:
                return
            name = name.strip()
            if not name:
                return
            if name in self.cfg["providers"]:
                messagebox.showerror("重名", "已经有叫「%s」的服务" % name, parent=win)
                return
            self.cfg["providers"][name] = {
                "base": "https://api.deepseek.com/v1", "model": "deepseek-flash", "key": "",
                "params": {"thinking": {"type": "disabled"}},
            }
            self.save_cfg()
            pc.configure(values=list(self.cfg["providers"]))
            pname.set(name)
            load_for()
            self.refresh_providers()

        def del_provider():
            name = pname.get()
            if len(self.cfg["providers"]) <= 1:
                messagebox.showinfo("不能删", "至少要留一个服务", parent=win)
                return
            if not messagebox.askyesno("删除服务", "删除服务「%s」？" % name, parent=win):
                return
            self.cfg["providers"].pop(name, None)
            self.save_cfg()
            left = list(self.cfg["providers"])
            pc.configure(values=left)
            pname.set(left[0])
            load_for()
            self.refresh_providers()

        tk.Button(prow, text="添加服务", bg=PANEL, fg=FG, bd=0, font=FONT_S, activebackground=BORDER,
                  activeforeground=FG, command=add_provider).pack(side="left")
        tk.Button(prow, text="删除服务", bg=PANEL, fg=FG, bd=0, font=FONT_S, activebackground=BORDER,
                  activeforeground=FG, command=del_provider).pack(side="left", padx=(8, 0))

        fields = {}
        for key, label in [("base", "地址 Base URL"), ("model", "模型名"), ("key", "API Key")]:
            tk.Label(win, text=label, bg=BG, fg=MUTED, font=FONT_S).pack(anchor="w", padx=14, pady=(8, 2))
            e = tk.Entry(win, bg=PANEL, fg=FG, insertbackground=FG, bd=0, font=FONT_S)
            e.pack(fill="x", padx=14, ipady=5)
            fields[key] = e

        tk.Label(win, text="维基地址（本地 Kiwix 或官方 Wikipedia；留空 = 不查）", bg=BG, fg=MUTED,
                 font=FONT_S).pack(anchor="w", padx=14, pady=(8, 2))
        wiki_var = tk.StringVar(value=self.cfg.get("wiki_base", WIKI_DEFAULT_BASE))
        wiki_cb = ttk.Combobox(win, textvariable=wiki_var,
                               values=[WIKI_DEFAULT_BASE, WIKI_DEFAULT_FALLBACK])
        wiki_cb.pack(fill="x", padx=14)

        tk.Label(win, text="备用地址（主地址连不上或没结果时再试；留空 = 不回退）", bg=BG, fg=MUTED,
                 font=FONT_S).pack(anchor="w", padx=14, pady=(8, 2))
        fb_e = tk.Entry(win, bg=PANEL, fg=FG, insertbackground=FG, bd=0, font=FONT_S)
        fb_e.pack(fill="x", padx=14, ipady=5)
        fb_e.insert(0, self.cfg.get("wiki_fallback", WIKI_DEFAULT_FALLBACK))

        tk.Label(win, text="代理（官方维基要翻墙；如 socks5h://127.0.0.1:10808；留空 = 直连/系统代理）",
                 bg=BG, fg=MUTED, font=FONT_S, wraplength=500, justify="left").pack(
            anchor="w", padx=14, pady=(8, 2))
        proxy_e = tk.Entry(win, bg=PANEL, fg=FG, insertbackground=FG, bd=0, font=FONT_S)
        proxy_e.pack(fill="x", padx=14, ipady=5)
        proxy_e.insert(0, self.cfg.get("wiki_proxy", ""))

        tk.Label(win, text="存笔记目录（「存笔记」写到这里；留空 = 程序目录下 notes）", bg=BG, fg=MUTED,
                 font=FONT_S).pack(anchor="w", padx=14, pady=(8, 2))
        sd_row = tk.Frame(win, bg=BG)
        sd_row.pack(fill="x", padx=14)
        sd_e = tk.Entry(sd_row, bg=PANEL, fg=FG, insertbackground=FG, bd=0, font=FONT_S)
        sd_e.pack(side="left", fill="x", expand=True, ipady=5)
        sd_e.insert(0, self.cfg.get("save_dir", ""))

        def pick_dir():
            d = filedialog.askdirectory(parent=win, title="选择存笔记目录",
                                        initialdir=sd_e.get().strip() or APP_DIR)
            if d:
                sd_e.delete(0, "end")
                sd_e.insert(0, os.path.normpath(d))

        tk.Button(sd_row, text="浏览…", bg=PANEL, fg=FG, bd=0, font=FONT_S, activebackground=BORDER,
                  activeforeground=FG, command=pick_dir).pack(side="left", padx=(8, 0))

        tk.Label(win, text="附加请求参数（JSON 并入请求根对象；不能覆盖 model / messages / temperature / stream）",
                 bg=BG, fg=MUTED, font=FONT_S, wraplength=500, justify="left").pack(anchor="w", padx=14, pady=(10, 2))
        params = tk.Text(win, height=7, bg=PANEL, fg=FG, insertbackground=FG, bd=0, font=MONO, padx=8, pady=6)
        params.pack(fill="both", expand=False, padx=14, pady=(0, 8))

        def load_for(*_):
            p = self.cfg["providers"].get(pname.get(), {})
            for k, e in fields.items():
                e.delete(0, "end")
                e.insert(0, p.get(k, "") or "")
            params.delete("1.0", "end")
            params.insert("1.0", json.dumps(p.get("params") or {}, ensure_ascii=False, indent=2))

        pc.bind("<<ComboboxSelected>>", load_for)
        load_for()

        def save():
            try:
                obj = json.loads(params.get("1.0", "end").strip() or "{}")
                if not isinstance(obj, dict):
                    raise ValueError("根节点必须是 JSON 对象")
            except Exception as ex:
                messagebox.showerror("附加请求参数有问题", str(ex), parent=win)
                return
            p = self.cfg["providers"].setdefault(pname.get(), {})
            for k, e in fields.items():
                p[k] = e.get().strip()
            p["params"] = obj
            for label_, u in (("维基地址", wiki_var.get()), ("备用地址", fb_e.get())):
                u = (u or "").strip()
                if u and not u.lower().startswith(("http://", "https://")):
                    messagebox.showerror("地址不对", "%s 要以 http:// 或 https:// 开头：\n%s" % (label_, u),
                                         parent=win)
                    return
            self.cfg["wiki_base"] = wiki_var.get().strip()
            self.cfg["wiki_fallback"] = fb_e.get().strip()
            self.cfg["wiki_proxy"] = proxy_e.get().strip()
            sd = sd_e.get().strip().strip('"')
            if sd and not os.path.isabs(sd):
                sd = os.path.join(APP_DIR, sd)
            self.cfg["save_dir"] = sd
            self.save_cfg()
            self.status.config(text="已保存服务设置（下一句生效）")
            win.destroy()

        tk.Button(foot, text="保存", bg=ACCENT, fg=BTN_FG, bd=0, font=FONT, command=save,
                  activebackground=ACCENT_ACTIVE, activeforeground=BTN_FG).pack(side="right")
        tk.Button(foot, text="取消", bg=PANEL, fg=FG, bd=0, font=FONT, command=win.destroy,
                  activebackground=BORDER, activeforeground=FG).pack(side="right", padx=(0, 8))

        # 按实际内容定高（125% 缩放下固定 800 可能装不下）；再夹到屏幕高度
        win.update_idletasks()
        need = max(660, win.winfo_reqheight() + 8)
        win.geometry("540x%d" % min(need, self.root.winfo_screenheight() - 80))

    def refresh_providers(self):
        names = list(self.cfg["providers"])
        self.model_cb.configure(values=names)
        if self.provider not in names:
            self.provider = names[0]
            self.cfg["default"] = self.provider
            self.save_cfg()
        self.model_var.set(self.provider)

    # ---------------- 提示词管理
    def reload_prompts(self):
        self.prompts = load_prompts()
        names = [p["name"] for p in self.prompts]
        self.prompt_cb.configure(values=names)
        if self.prompt_name not in names:
            self.prompt_name = names[0]
        self.prompt_var.set(self.prompt_name)
        if self.messages:
            self.messages[0] = {"role": "system", "content": self.current_system()}

    def open_prompts(self):
        win = tk.Toplevel(self.root)
        win.title("提示词管理")
        win.configure(bg=BG)
        win.geometry("780x760")
        win.minsize(640, 520)
        win.transient(self.root)
        win.attributes("-topmost", bool(self.pinned))

        data = [dict(p) for p in self.prompts]
        start = [p["name"] for p in data].index(self.prompt_name) if self.prompt_name in [p["name"] for p in data] else 0
        idx = {"i": max(0, start)}
        state = {"dirty": False, "loading": False}

        # 底部按钮栏必须先占位：左右两栏都是 fill/expand，先摆它们会把窗口高度吃光，
        # 后摆的按钮被挤成 1×1，看起来像压根没有保存按钮。
        foot = tk.Frame(win, bg=BG)
        foot.pack(side="bottom", fill="x", padx=14, pady=(0, 12))
        hint = tk.Label(foot, text="改完点「保存」（Ctrl+S）", bg=BG, fg=MUTED, font=FONT_S)
        hint.pack(side="left")

        body = tk.Frame(win, bg=BG)
        body.pack(side="top", fill="both", expand=True)

        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="y", padx=(12, 6), pady=12)
        tk.Label(left, text="提示词列表", bg=BG, fg=MUTED, font=FONT_S).pack(anchor="w")
        lb = tk.Listbox(left, bg=PANEL, fg=FG, width=20, bd=0, font=FONT_S, highlightthickness=0,
                        selectbackground=ACCENT, selectforeground=SEL_FG, activestyle="none",
                        exportselection=False)
        lb.pack(fill="y", expand=True, pady=(4, 6))
        lbtns = tk.Frame(left, bg=BG)
        lbtns.pack(fill="x")

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(6, 12), pady=12)
        tk.Label(right, text="名称", bg=BG, fg=MUTED, font=FONT_S).pack(anchor="w")
        name_var = tk.StringVar()
        name_entry = tk.Entry(right, textvariable=name_var, bg=PANEL, fg=FG, insertbackground=FG, bd=0,
                              font=FONT)
        name_entry.pack(fill="x", ipady=5, pady=(2, 8))
        tk.Label(right, text="system（身份/约束）", bg=BG, fg=MUTED, font=FONT_S).pack(anchor="w")
        sys_txt = tk.Text(right, height=7, bg=PANEL, fg=FG, insertbackground=FG, bd=0,
                          font=FONT_S, padx=8, pady=6, wrap="word")
        sys_txt.pack(fill="x", pady=(2, 8))
        tk.Label(right, text="user 模板（输入框空着直接回车时用；$content = 选中的文字，留空则问「这段讲的什么？」）",
                 bg=BG, fg=MUTED, font=FONT_S, wraplength=520, justify="left").pack(anchor="w")
        usr_txt = tk.Text(right, height=8, bg=PANEL, fg=FG, insertbackground=FG, bd=0,
                          font=FONT_S, padx=8, pady=6, wrap="word")
        usr_txt.pack(fill="x", pady=(2, 8))
        # 查维基已改成主界面的全局开关（存 config）；这里只提示，不再放勾，避免两处控制打架
        tk.Label(right, text="查维基由主界面的「☑ 查维基」统一控制（全局开关，跟提示词无关）",
                 bg=BG, fg=MUTED, font=FONT_S, wraplength=520, justify="left").pack(anchor="w", pady=(0, 6))

        def refresh(select=None):
            state["loading"] = True
            try:
                lb.delete(0, "end")
                for p in data:
                    lb.insert("end", p["name"])
                i = idx["i"] if select is None else select
                i = max(0, min(i, len(data) - 1))
                idx["i"] = i
                lb.selection_clear(0, "end")
                lb.selection_set(i)
                lb.see(i)
                load(i)
            finally:
                state["loading"] = False

        def load(i):
            if not data:
                return
            p = data[i]
            name_var.set(p.get("name", ""))
            sys_txt.delete("1.0", "end")
            sys_txt.insert("1.0", p.get("system", ""))
            usr_txt.delete("1.0", "end")
            usr_txt.insert("1.0", p.get("template", ""))

        def apply_fields():
            if not data:
                return
            i = idx["i"]
            data[i]["name"] = name_var.get().strip() or data[i]["name"]
            data[i]["system"] = sys_txt.get("1.0", "end").strip()
            data[i]["template"] = usr_txt.get("1.0", "end").strip()

        def on_pick(_=None):
            if state["loading"]:
                return
            sel = lb.curselection()
            if not sel or sel[0] == idx["i"]:
                return
            apply_fields()
            idx["i"] = sel[0]
            load(idx["i"])

        lb.bind("<<ListboxSelect>>", on_pick)

        def uniq(base):
            names = [p["name"] for p in data]
            if base not in names:
                return base
            n = 2
            while "%s %d" % (base, n) in names:
                n += 1
            return "%s %d" % (base, n)

        def do_add():
            apply_fields()
            data.append({"name": uniq("新提示词"), "system": "You are a helpful assistant.", "template": ""})
            refresh(len(data) - 1)

        def do_dup():
            apply_fields()
            cur = data[idx["i"]]
            data.append({"name": uniq(cur["name"] + " 副本"), "system": cur.get("system", ""),
                         "template": cur.get("template", "")})
            refresh(len(data) - 1)

        def do_del():
            if len(data) <= 1:
                messagebox.showinfo("不能删", "至少要留一条提示词", parent=win)
                return
            if not messagebox.askyesno("删除提示词", "删除「%s」？" % data[idx["i"]]["name"], parent=win):
                return
            data.pop(idx["i"])
            refresh()
            state["dirty"] = True
            hint.config(text="有改动还没保存（Ctrl+S）", fg=ACCENT)

        for t, cmd in (("添加", do_add), ("复制", do_dup), ("删除", do_del)):
            tk.Button(lbtns, text=t, bg=PANEL, fg=FG, bd=0, font=FONT_S, width=4,
                      activebackground=BORDER, activeforeground=FG,
                      command=cmd).pack(side="left", padx=(0, 4))

        def do_save(_=None):
            apply_fields()
            names = [p["name"] for p in data]
            if len(set(names)) != len(names):
                messagebox.showerror("名称重复", "提示词名称不能重复", parent=win)
                return "break"
            try:
                tmp = PROMPTS_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"prompts": data}, f, ensure_ascii=False, indent=2)
                if os.path.exists(PROMPTS_PATH):
                    try:
                        shutil.copy2(PROMPTS_PATH, PROMPTS_PATH + ".bak")
                    except Exception:
                        pass
                os.replace(tmp, PROMPTS_PATH)
            except Exception as ex:
                messagebox.showerror("保存失败", str(ex), parent=win)
                return "break"
            before = self.prompt_name
            self.reload_prompts()
            if before not in [p["name"] for p in self.prompts]:
                # 改名了：把当前选中的这条接上，别让主窗口掉回第一条
                self.prompt_name = data[idx["i"]]["name"]
                self.prompt_var.set(self.prompt_name)
                self.cfg["default_prompt"] = self.prompt_name
                self.save_cfg()
                if self.messages:
                    self.messages[0] = {"role": "system", "content": self.current_system()}
            state["dirty"] = False
            refresh(idx["i"])
            hint.config(text="已保存 %d 条 · %s" % (len(data), time.strftime("%H:%M:%S")), fg=MUTED)
            self.status.config(text="提示词已保存（%d 条）" % len(data))
            return "break"

        def do_close():
            if state["dirty"] and not messagebox.askyesno(
                    "还没保存", "提示词有改动还没保存，直接关掉？", parent=win):
                return
            win.destroy()

        def mark_dirty(e=None):
            if e is not None and getattr(e, "keysym", "") in ("s", "S") and (e.state & 0x4):
                return          # Ctrl+S 自己不算改动，否则刚存完又提示"没保存"
            if state["dirty"]:
                return
            state["dirty"] = True
            hint.config(text="有改动还没保存（Ctrl+S）", fg=ACCENT)

        for w in (name_entry, sys_txt, usr_txt):
            w.bind("<KeyRelease>", mark_dirty)
        win.bind("<Control-s>", do_save)
        win.bind("<Escape>", lambda e: do_close())
        win.protocol("WM_DELETE_WINDOW", do_close)

        tk.Button(foot, text="保存", bg=ACCENT, fg=BTN_FG, bd=0, font=FONT, width=8,
                  activebackground=ACCENT_ACTIVE, activeforeground=BTN_FG,
                  command=do_save).pack(side="right")
        tk.Button(foot, text="关闭", bg=PANEL, fg=FG, bd=0, font=FONT, width=8,
                  activebackground=BORDER, activeforeground=FG,
                  command=do_close).pack(side="right", padx=(0, 8))

        refresh()
        win.focus_force()

    # ---------------- 热键设置
    def open_hotkeys(self):
        win = tk.Toplevel(self.root)
        win.title("热键设置")
        win.configure(bg=BG)
        win.geometry("460x300")
        win.transient(self.root)
        win.attributes("-topmost", bool(self.pinned))

        state = {"target": "text"}
        m1, v1 = parse_hotkey(self.cfg.get("hotkey_text", "ctrl+alt+a"))
        m2, v2 = parse_hotkey(self.cfg.get("hotkey_image", "ctrl+alt+s"))
        cap = {"text": (m1, v1), "image": (m2, v2)}

        tk.Label(win, text="要改哪个热键", bg=BG, fg=MUTED, font=FONT_S).pack(anchor="w", padx=14, pady=(12, 2))
        row = tk.Frame(win, bg=BG)
        row.pack(anchor="w", padx=14)
        target_var = tk.StringVar(value="text")
        first_rb = None
        for label, key in [("取词", "text"), ("截图", "image")]:
            rb = tk.Radiobutton(row, text=label, value=key, variable=target_var,
                                bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG,
                                activeforeground=FG, font=FONT_S,
                                command=lambda k=key: (state.update(target=k), show_cur()))
            rb.pack(side="left", padx=(0, 12))
            if key == "text":
                first_rb = rb

        big = tk.Label(win, text="", bg=PANEL, fg=ACCENT, font=("Microsoft YaHei UI", 16, "bold"),
                       pady=14)
        big.pack(fill="x", padx=14, pady=12)
        hint = tk.Label(win, text="焦点在这窗口里，直接按新的组合键（至少带 Ctrl / Alt / Win）",
                        bg=BG, fg=MUTED, font=FONT_S)
        hint.pack(anchor="w", padx=14)

        def show_cur():
            m, v = cap[state["target"]]
            big.config(text=hotkey_label(m, v))

        def on_key(e):
            if e.keysym in ("Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
                            "Super_L", "Super_R", "Caps_Lock"):
                return
            ctrl = bool(e.state & 0x4)
            shift = bool(e.state & 0x1)
            alt = bool(e.state & 0x20000) or bool(e.state & 0x8)
            winm = bool(e.state & 0x40)
            if not (ctrl or alt or winm):
                hint.config(text="必须带 Ctrl / Alt / Win", fg=ERR_FG)
                return
            mods = 0
            if ctrl:
                mods |= MOD_CONTROL
            if alt:
                mods |= MOD_ALT
            if shift:
                mods |= MOD_SHIFT
            if winm:
                mods |= MOD_WIN
            vk = e.keycode
            if not vk:
                return
            cap[state["target"]] = (mods, vk)
            big.config(text=hotkey_label(mods, vk))
            hint.config(text="已记录，点保存生效", fg=MUTED)
            return "break"

        win.bind("<KeyPress>", on_key)
        self.suppress_hotkeys = True
        win.bind("<Destroy>", lambda e: setattr(self, "suppress_hotkeys", False), add="+")

        def save():
            m, v = cap["text"]
            m2_, v2_ = cap["image"]
            if (m, v) == (m2_, v2_):
                messagebox.showerror("重复", "两个热键不能一样", parent=win)
                return
            self.cfg["hotkey_text"] = hotkey_to_str(m, v)
            self.cfg["hotkey_image"] = hotkey_to_str(m2_, v2_)
            self.save_cfg()
            self.reload_hotkeys()
            win.destroy()

        foot = tk.Frame(win, bg=BG)
        foot.pack(side="bottom", fill="x", padx=14, pady=(0, 12))
        tk.Button(foot, text="保存", bg=ACCENT, fg=BTN_FG, bd=0, font=FONT, width=8,
                  activebackground=ACCENT_ACTIVE, activeforeground=BTN_FG,
                  command=save).pack(side="right")
        tk.Button(foot, text="取消", bg=PANEL, fg=FG, bd=0, font=FONT, width=8,
                  activebackground=BORDER, activeforeground=FG,
                  command=win.destroy).pack(side="right", padx=(0, 8))

        show_cur()
        win.focus_force()
        first_rb.select()

    def force_front(self):
        """真正把窗口拉到前台。

        deiconify/lift 只在「从隐藏恢复」时管用；窗口只是被别的程序盖住
        （state 仍是 normal）时，lift 会被 Windows 的前台锁挡掉，表现就是
        「取词了，窗口没跳上来」。所以直接走 Win32：恢复 → 按 pin 设 z 序 →
        AttachThreadInput 抢前台（不抢的话键盘还在原窗口里，追问打不了字）。
        """
        try:
            self.root.deiconify()
            self.root.update_idletasks()
        except Exception:
            pass
        try:
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            hwnd = u.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 9)                       # SW_RESTORE
            u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            u.SetWindowPos.restype = wt.BOOL
            # hWndInsertAfter 必须声明 argtypes 后再传负数，否则 64 位下会被截断、静默失败
            # 不管有没有锁 pin，唤出时先临时置顶：有些阅读器/浏览器会抢回前台，
            # 只靠 SetForegroundWindow 会「前台是它、眼睛还是看不见」。
            u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x43)          # HWND_TOPMOST
            if not getattr(self, "pinned", False):
                self.root.after(1200, self.drop_temp_topmost)
            fg0 = u.GetForegroundWindow()
            fg = fg0
            if fg != hwnd:
                tid_fg = u.GetWindowThreadProcessId(fg, None)
                tid_me = k.GetCurrentThreadId()
                attached = False
                if tid_fg and tid_fg != tid_me:
                    attached = bool(u.AttachThreadInput(tid_me, tid_fg, True))
                u.SetForegroundWindow(hwnd)
                if attached:
                    u.AttachThreadInput(tid_me, tid_fg, False)
                self.root.update_idletasks()
                log("show: hwnd=%s vis=%s iconic=%s attach=%s fg0=%s fg1=%s" % (
                    hwnd, u.IsWindowVisible(hwnd), u.IsIconic(hwnd), attached, fg0,
                    u.GetForegroundWindow()))
        except Exception as ex:
            log("前置窗口失败: %r" % (ex,))
        try:
            self.root.focus_force()
            self.entry.focus_set()
        except Exception:
            pass

    def drop_temp_topmost(self):
        """唤出 1.2 秒后撤掉临时置顶（只在没锁 pin 时挂这个定时器）。"""
        if getattr(self, "pinned", False):
            return
        try:
            u = ctypes.windll.user32
            hwnd = u.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            u.SetWindowPos.restype = wt.BOOL
            u.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x43)       # HWND_NOTOPMOST
        except Exception as ex:
            log("撤临时置顶失败: %r" % (ex,))

    def show(self):
        self.force_front()

    def on_unmap(self, e):
        """点最小化时收进托盘，不留在任务栏（没托盘就老实留在任务栏）。"""
        try:
            if (e.widget is self.root and self.root.state() == "iconic"
                    and getattr(self, "has_tray", False)):
                self.root.withdraw()
        except Exception:
            pass

    def start_tray(self):
        try:
            import pystray
            from PIL import ImageDraw, ImageFont
        except Exception as ex:
            log("托盘不可用: %r" % ex)
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(76, 141, 255, 255))
        try:
            f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 34)
        except Exception:
            f = ImageFont.load_default()
        d.text((32, 32), "读", fill="white", font=f, anchor="mm")

        def act(fn):
            return lambda *a: self.root.after(0, fn)

        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", act(self.show), default=True),
            pystray.MenuItem("取词提问", act(self.do_grab_text)),
            pystray.MenuItem("截图提问", act(self.do_grab_image)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", act(self.quit)),
        )
        self.tray = pystray.Icon("peidu", img, "陪读", menu)
        try:
            self.tray.run_detached()
        except Exception:
            threading.Thread(target=self.tray.run, daemon=True).start()
        self.has_tray = True
        log("托盘图标已启动")

    def hide(self):
        if getattr(self, "has_tray", False):
            self.root.withdraw()          # 有托盘：收进托盘，热键唤回
        else:
            self.root.iconify()           # 没托盘：缩到任务栏，别让用户找不着

    def quit(self):
        self.cancel_flag.set()
        try:
            if getattr(self, "tray", None):
                self.tray.stop()
        except Exception:
            pass
        try:
            n = clean_image_cache()
            if n:
                log("退出：已清理图片缓存 %d 个" % n)
        except Exception as ex:
            log("退出清理缓存失败: %r" % (ex,))
        try:
            os.remove(os.path.join(APP_DIR, "run.lock"))
        except Exception:
            pass
        self.root.destroy()
        os._exit(0)

    def watch_show_request(self):
        flag = os.path.join(APP_DIR, "show.request")
        if os.path.exists(flag):
            try:
                os.remove(flag)
            except Exception:
                pass
            log("收到显示请求，弹窗")
            self.show()
        self.root.after(700, self.watch_show_request)

    # ---------------- 引用
    def set_quote(self, text="", image=None):
        self.quote_text = text or ""
        self.quote_image = image
        if image:
            img, path = image
            self.quote_title.config(text="截图 · " + os.path.basename(path))
            thumb = img.copy()
            thumb.thumbnail((460, 260))
            self._thumb = tk.PhotoImage(data=base64.b64encode(_png_bytes(thumb)).decode())
            self.quote_thumb.config(image=self._thumb)
            self.quote_body.pack_forget()
            self.quote_thumb.pack(fill="x", padx=8, pady=(2, 8))
        else:
            self.quote_title.config(text="选中的文字 · %d 字" % len(self.quote_text))
            self.quote_thumb.pack_forget()
            self.quote_body.pack(fill="x", padx=8, pady=6)
            self.quote_body.configure(state="normal")
            self.quote_body.delete("1.0", "end")
            shown = self.quote_text[:1200] + ("…" if len(self.quote_text) > 1200 else "")
            self.quote_body.insert("1.0", shown)
            self.quote_body.configure(state="disabled")
        self.quote_frame.pack(fill="x", padx=14, pady=(4, 0), before=self.chat_wrap)

    def clear_quote(self):
        self.quote_text = ""
        self.quote_image = None
        self.quote_frame.pack_forget()

    # ---------------- 发送
    def on_enter(self, e):
        if e.state & 0x0001:
            return None
        self.on_send()
        return "break"

    def on_send(self):
        if self.busy:
            self.cancel_flag.set()
            self.status.config(text="已中断")
            return
        text = self.entry.get("1.0", "end").strip()
        quote, img = self.quote_text.strip(), self.quote_image
        if not text and not quote and not img:
            return
        self.entry.delete("1.0", "end")
        self.start_round(text, quote, img)
        self.clear_quote()

    def start_round(self, question, quote, img):
        # 这一轮要不要先查维基（主界面的全局开关，不跟提示词走）
        self.wiki_srcs = []
        self.wiki_query = ""
        self.wiki_img = None
        self.wiki_label = ""
        is_formula = (self.current_prompt() or {}).get("name") == FORMULA_PROMPT["name"]
        # 截图转公式：只认截图。纯文字发过去，模型会把文字硬编成公式（用户实测「乱写」），
        # 所以这里直接拦掉、不发请求、不动 messages —— 显式失败，不猜。
        if is_formula and not img:
            shown = (question or "").strip() or (quote or "").strip() or "（空）"
            self.append_chat("我：" + shown[:200], "user")
            self.append_chat("〔截图转公式〕这一档只认截图：纯文字不发请求。"
                             "按 Alt+2 或点「📐 截公式」框住公式；要问文字或追问，把提示词切回「陪读」。", "err")
            self.status.config(text="截图转公式：需要截图")
            log("截图转公式拦截：没有截图，未发请求")
            return
        wiki_on = (self.wiki_var.get() if getattr(self, "wiki_var", None) is not None
                   else bool(self.cfg.get("wiki_enabled")))
        if is_formula:
            wiki_on = False        # 转公式不查维基：省一次看图提关键词 + 检索
        # 维基档 + 开关关着：不发给模型，直接说没开，避免它凭记忆编条目内容
        if (self.current_prompt() or {}).get("wiki") and not wiki_on:
            shown = (question or "").strip() or (quote or "").strip() or "（截图）"
            self.append_chat("我：" + shown, "user")
            self.append_chat("〔维基〕没有开启查维基。先勾上主界面的「查维基」再问。", "err")
            self.status.config(text="维基：没开")
            log("维基档拦截：没开查维基，未发请求")
            return
        if wiki_on:
            q = (question or "").strip() or (quote or "").strip()
            self.wiki_query = q.splitlines()[0][:80] if q else ""
            if not self.wiki_query and img:
                self.wiki_img = img[0]      # 没输入词：让模型看图提关键词，再去查   # 检索词别太长，取第一行
        if not self.messages:
            self.messages = [{"role": "system", "content": self.current_system()}]
        tpl = (self.current_prompt().get("template") or "").strip()
        if tpl and is_formula and img:
            # 截图转公式：模板必须带上（图才是主体），用户打的字只当「补充要求」
            extra = question.strip()
            content = tpl + ("\n\n补充要求：" + extra if extra else "")
            shown = "〔%s〕" % self.prompt_name
        elif tpl and not question.strip() and (quote or img):
            # 有引用 / 截图又没打字：直接套这个提示词的模板（截图时占位成「见截图」）
            content = tpl.replace("$content", quote or "（见截图）")
            shown = "〔%s〕" % self.prompt_name
        else:
            q = question.strip() or "解释这段"
            parts = []
            if quote:
                parts.append("【选中原文】\n" + quote)
            parts.append("【问题】" + q)
            content = "\n\n".join(parts)
            shown = q
        question = shown
        user_msg = {"role": "user", "content": content}
        if img:
            user_msg["content"] = [{"type": "text", "text": content},
                                   {"type": "image_url", "image_url": {"url": image_to_data_url(img[0])}}]
            self.img_paths.append(img[1])
            self.img_by_idx[len(self.messages)] = img[1]
        self.messages.append(user_msg)

        label = "我：" + question
        if quote:
            label += "\n〔引用〕" + (quote[:70] + "…" if len(quote) > 70 else quote)
        self.append_chat(label, "user")
        if img:
            self.append_chat("〔截图〕" + os.path.basename(img[1]), "muted")
            self.insert_shot(img[1])
        self.chat.configure(state="normal")
        self.chat.insert("end", "陪读：", "bot")
        self.stream_start = self.chat.index("end-1c")
        self.chat.configure(state="disabled")

        self.busy = True
        self.cancel_flag.clear()
        self.tokens = 0
        self.t0 = time.time()
        self.send_btn.config(text="停止", bg=STOP_BG)
        self.status.config(text="思考中…")
        threading.Thread(target=self.worker, daemon=True).start()

    def worker(self):
        prov = self.cfg["providers"].get(self.provider)
        if not prov:
            self.q.put(("error", "没有配置模型服务"))
            return
        base = prov["base"].rstrip("/")
        url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        msgs = self.messages
        wiki_q = getattr(self, "wiki_query", "")
        wiki_img = getattr(self, "wiki_img", None)
        if wiki_img is not None and not wiki_q:
            self.q.put(("status", "看图提关键词…"))
            try:
                kws = _extract_keywords(prov, wiki_img)
                wiki_q = kws
                log("截图关键词: %r" % (kws,))
            except Exception as ex:
                kws = []
                log("提炼关键词失败: %r" % ex)
            if kws:
                self.q.put(("status", "关键词：" + "、".join(kws[:4])))
        if wiki_q:
            self.q.put(("status", "查维基…"))
            ctx, srcs, label = "", [], ""
            try:
                ctx, srcs, label = wiki_context(
                    self.cfg.get("wiki_base") or WIKI_DEFAULT_BASE, wiki_q,
                    fallback=self.cfg.get("wiki_fallback", WIKI_DEFAULT_FALLBACK),
                    proxy=self.cfg.get("wiki_proxy", ""))
            except Exception as ex:
                log("维基检索失败: %r" % ex)
            if ctx:
                msgs = [dict(m) for m in self.messages]      # 只改本次请求，历史里不留资料
                last = msgs[-1]
                if isinstance(last.get("content"), str):
                    last["content"] = ctx + "\n\n" + last["content"]
                elif isinstance(last.get("content"), list):
                    parts = [dict(x) for x in last["content"]]
                    for x in parts:
                        if x.get("type") == "text":
                            x["text"] = ctx + "\n\n" + x.get("text", "")
                            break
                    last["content"] = parts
                self.wiki_srcs = srcs
                self.wiki_label = label
                self.q.put(("status", "维基 %d 条 · %s · 思考中…" % (len(srcs), label)))
            else:
                self.q.put(("status", "维基没查到，按常识回答"))
        payload = {"model": prov["model"], "messages": msgs, "stream": True, "temperature": 0.7}
        payload.update(prov.get("params") or {})
        headers = {"Authorization": "Bearer " + prov.get("key", ""), "Content-Type": "application/json"}
        buf = ""
        try:
            with requests.post(url, json=payload, headers=headers, stream=True, timeout=180) as resp:
                if resp.status_code != 200:
                    self.q.put(("error", "HTTP %s: %s" % (resp.status_code, resp.text[:300])))
                    return
                for raw in resp.iter_lines(decode_unicode=False):
                    if self.cancel_flag.is_set():
                        break
                    if not raw:
                        continue
                    # 服务端 Content-Type 是 text/event-stream，requests 会按 ISO-8859-1 解，
                    # 中文会变成 ä¡½ 这种乱码；这里自己按 UTF-8 解。
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    ch = obj.get("choices") or []
                    if not ch:
                        continue
                    piece = (ch[0].get("delta") or {}).get("content")
                    if piece:
                        buf += piece
                        self.q.put(("chunk", buf))
                    usage = obj.get("usage")
                    if usage:
                        self.q.put(("usage", usage.get("completion_tokens") or 0))
            self.q.put(("done", buf))
        except Exception as ex:
            self.q.put(("error", repr(ex)))

    def pump(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "chunk":
                    self.render_stream(data)
                elif kind == "usage":
                    self.tokens = data
                elif kind == "done":
                    self.finish_round(data)
                elif kind == "error":
                    self.chat.configure(state="normal")
                    self.chat.insert("end", "\n[出错] " + str(data), "err")
                    self.chat.configure(state="disabled")
                    self.status.config(text="出错：" + str(data)[:60])
                    self.busy = False
                    self.send_btn.config(text="发送", bg=ACCENT)
                elif kind == "status":
                    self.status.config(text=str(data))
                elif kind == "grab_done":
                    text, info = data
                    self.finish_grab_text(text, info)
                elif kind == "hotkey":
                    if getattr(self, "suppress_hotkeys", False):
                        continue
                    if data == "text":
                        self.do_grab_text()
                    else:
                        self.do_grab_image()
        except queue.Empty:
            pass
        self.root.after(60, self.pump)

    def do_grab_text(self):
        # 不先 show：抢焦点会让 Ctrl+C 落到自己窗口上，取到的就是旧内容
        if getattr(self, "grabbing", False):
            return                      # 上一次还没取完，连按不叠加
        self.grabbing = True
        self.status.config(text="取词中…")

        def job():
            try:
                fg = fg_desc()
                if not wait_modifiers_released(3.0):
                    log("取词: 跳过，Alt/Ctrl 一直没松开")
                    self.q.put(("grab_done", ("", {"why": "按键没松开", "tries": 0, "ms": 0})))
                    return
                time.sleep(0.06)        # 让目标程序先把热键的 keyup 处理完
                text, info = grab_selection()
                log("取词: %s %s fg=%s" % ("OK" if text.strip() else "失败", info, fg))
                self.q.put(("grab_done", (text, info)))
            except Exception as ex:
                log("取词线程异常: %r" % (ex,))
                self.q.put(("grab_done", ("", {"why": "内部异常", "tries": 0, "ms": 0})))

        threading.Thread(target=job, daemon=True).start()

    def finish_grab_text(self, text="", info=None):
        self.grabbing = False
        info = info or {}
        text = text or ""
        if not text.strip():
            self.show()
            why = info.get("why", "")
            if why == "按键没松开":
                msg = "没取到：热键还按着，松开 Alt 再按一次"
            elif why == "剪贴板里不是文字":
                msg = "没取到：复制出来的不是文字"
            else:
                msg = "没取到选中文字：先选中文字再按热键"
            self.status.config(text=msg)
            return
        self.show()
        self.set_quote(text=text)
        self.status.config(text="取到 %d 字" % len(text))
        if self.auto_var.get() and not self.busy:
            self.on_send()          # 直接解释；有问题在后面追问
        else:
            self.entry.focus_set()

    def do_grab_image(self):
        self.root.withdraw()      # 截图时先让开，抓完再 show()；这里必须真隐藏，不能只最小化
        self.root.after(160, lambda: RegionPicker(self.root, self.on_region))

    def grab_formula(self):
        """一键截图转公式：切到「截图转公式」档再框选，框完自动发（AutoImg 开的时候）。"""
        if self.busy:
            self.status.config(text="等这一轮答完再截")
            return
        name = FORMULA_PROMPT["name"]
        if name not in [p.get("name") for p in self.prompts]:
            self.prompts = _with_formula(list(self.prompts))
            self.prompt_cb.configure(values=[p["name"] for p in self.prompts])
        self.set_prompt(name)
        self.prompt_var.set(name)
        self.do_grab_image()

    def on_region(self, result):
        self.show()
        if not result:
            self.status.config(text="已取消截图")
            return
        self.set_quote(image=result)
        self.status.config(text="截图已附上")
        if self.auto_img_var.get() and not self.busy:
            self.on_send()          # 直接解释图里的内容，不满意再追问
        else:
            self.status.config(text="截图已附上，输入问题后发送")
            self.entry.focus_set()

    def render_stream(self, text):
        self.chat.configure(state="normal")
        self.chat.delete(self.stream_start, "end")
        # 流式阶段先显示 LaTeX 原文：每 60ms 重排一次，公式图片这时候渲染太慢
        self.insert_markdown(text, "bot", rich=False)
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def finish_round(self, text):
        self.chat.configure(state="normal")
        self.chat.delete(self.stream_start, "end")
        self.insert_markdown(text, "bot")
        self.chat.configure(state="disabled")
        if text.strip():
            self.messages.append({"role": "assistant", "content": text})
        if self.prompt_name == FORMULA_PROMPT["name"]:
            self._autocopy_latex(text)
        srcs = getattr(self, "wiki_srcs", [])
        if srcs:
            head = "〔维基·官方〕" if getattr(self, "wiki_label", "") == "官方维基" else "〔维基〕"
            self.append_chat(head + " · ".join(srcs), "muted")
            self.wiki_srcs = []
            self.wiki_label = ""
        dt = time.time() - self.t0
        self.status.config(text="%s · %.1fs · %s tokens" % (self.provider, dt, self.tokens or "?"))
        self.busy = False
        self.send_btn.config(text="发送", bg=ACCENT)

    def math_photo(self, expr, display=False):
        """公式 → PhotoImage。按表达式/颜色/字号缓存，换主题颜色变了就重渲染。"""
        size = int(self.cfg.get("font_size", 10)) + (1 if display else 0)
        try:
            dpi = int(round(float(self.root.tk.call("tk", "scaling")) * 72))
        except Exception:
            dpi = 96
        max_w = 0
        if display:
            try:
                avail = self.chat.winfo_width() - 56
            except Exception:
                avail = 0
            max_w = avail if avail > 240 else 480
        key = (expr, display, size, dpi, max_w, FG)
        ph = self._math_photos.get(key)
        if ph is None:
            path = _math_png(expr, FG, size, dpi, max_w, BG)
            if not path:
                return None
            try:
                ph = tk.PhotoImage(file=path)
            except Exception as ex:
                log("公式图片加载失败: %r" % ex)
                return None
            self._math_photos[key] = ph
        return ph

    def _insert_math(self, expr, display):
        ph = self.math_photo(expr, display)
        if ph is None:          # 渲染不了就退回原文，别把内容弄丢
            self.chat.insert("end", ("$%s$" % expr) if not display else expr, "code")
            return
        self.chat_imgs.append(ph)
        self.chat.image_create("end", image=ph, align="center",
                               padx=4 if display else 2, pady=6 if display else 1)

    def insert_markdown(self, text, tag, rich=True):
        for is_code, chunk in _split_fences(text):
            if is_code:
                for ln in chunk.split("\n"):
                    self.chat.insert("end", ln + "\n", "code")
                if rich and chunk.strip():
                    # 流式阶段不建按钮：render_stream 每 60ms 删掉重排一次，按钮会一直闪
                    self._insert_copy_btn(chunk)
                continue
            self._render_md(chunk, tag, rich)

    def _insert_copy_btn(self, code):
        """代码块下面挂一个「复制」小按钮：公式 LaTeX 一键进剪贴板。"""
        btn = tk.Button(self.chat, text="⧉ 复制", bg=PANEL, fg=MUTED, bd=0, font=FONT_S,
                        padx=6, pady=0, cursor="hand2", activebackground=BORDER,
                        activeforeground=FG, command=lambda c=code: self.copy_code(c))
        self.chat.window_create("end", window=btn, padx=2, pady=2)
        getattr(self, "code_btns", []).append(btn)
        self.chat.insert("end", "\n", "bot")

    def copy_code(self, code):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            self.root.update_idletasks()
            self.status.config(text="已复制 %d 字到剪贴板" % len(code))
        except Exception as ex:
            self.status.config(text="复制失败：" + str(ex)[:40])

    def _drop_code_btns(self):
        """清空对话区前先销毁内嵌的「复制」按钮：Text 删掉窗口不会自己回收控件。"""
        for b in getattr(self, "code_btns", []) or []:
            try:
                b.destroy()
            except Exception:
                pass
        self.code_btns = []

    def _autocopy_latex(self, text):
        """「截图转公式」答完顺手把 LaTeX 放进剪贴板，直接去 Word/PPT 里粘。"""
        codes = [c.strip() for is_code, c in _split_fences(text) if is_code and c.strip()]
        # 模型把「没有识别到公式」也包进代码块的情况（本地模型会这样）别复制
        codes = [c for c in codes
                 if not (len(c) < 40 and "识别到公式" in c)]
        if not codes:
            self.append_chat("〔这张图里没识别到公式：没有复制任何东西〕", "muted")
            return
        self.copy_code("\n".join(codes))
        self.append_chat("〔LaTeX 已复制到剪贴板，可直接粘贴〕", "muted")

    def _render_md(self, chunk, tag, rich):
        line_start = True
        for seg in _md_segments(chunk):
            kind = seg[0]
            if kind == "text":
                parts = seg[1].split("\n")
                for k, part in enumerate(parts):
                    if k:
                        self.chat.insert("end", "\n", tag)
                        line_start = True
                    if not part:
                        continue
                    self._insert_inline(part, tag, line_start)
                    line_start = False
            elif kind == "code":
                self.chat.insert("end", seg[1], "code")
                line_start = False
            else:
                mkind, expr = seg[1], seg[2]
                if mkind == "display":
                    if not line_start:
                        self.chat.insert("end", "\n", tag)
                    if rich:
                        self._insert_math(expr, True)
                    else:
                        self.chat.insert("end", expr, "code")
                    self.chat.insert("end", "\n", tag)
                    line_start = True
                else:
                    if rich:
                        self._insert_math(expr, False)
                    else:
                        self.chat.insert("end", "$" + expr + "$", "code")
                    line_start = False
        if not line_start:
            self.chat.insert("end", "\n", tag)

    def _insert_inline(self, part, tag, line_start):
        if line_start:
            stripped = part.lstrip()
            indent = part[:len(part) - len(stripped)]
            if stripped[:2] in ("* ", "- "):
                part = indent + "• " + stripped[2:]
        for chunk, is_bold, is_code in _split_inline(part):
            t = ("bold",) if (is_bold and not is_code) else ("code",) if is_code else (tag,)
            self.chat.insert("end", chunk, t)

    def append_chat(self, text, tag):
        self.chat.configure(state="normal")
        self.chat.insert("end", text + "\n", tag)
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def insert_shot(self, path):
        """把截图贴进对话区（缩略图，点一下用系统看图打开原图）。"""
        try:
            im = Image.open(path).copy()
        except Exception:
            return
        try:
            avail = self.chat.winfo_width()
        except Exception:
            avail = 0
        if avail < 160:
            avail = 520
        im.thumbnail((min(avail - 36, 480), 320))
        try:
            photo = tk.PhotoImage(data=base64.b64encode(_png_bytes(im)).decode())
        except Exception:
            return
        self.chat_imgs.append(photo)
        state = str(self.chat.cget("state"))
        self.chat.configure(state="normal")
        name = self.chat.image_create("end", image=photo, padx=3, pady=4)
        try:
            self.shot_at[self.chat.index(name)] = path
        except Exception:
            pass
        self.chat.insert("end", "\n", "user")
        self.chat.configure(state=state)
        self.chat.see("end")

    def on_chat_click(self, e):
        """点截图缩略图 → 用系统看图打开原图。"""
        for idx, path in list(getattr(self, "shot_at", {}).items()):
            try:
                bb = self.chat.bbox(idx)
            except Exception:
                bb = None
            if bb and bb[0] <= e.x <= bb[0] + bb[2] and bb[1] <= e.y <= bb[1] + bb[3]:
                self.open_shot(path)
                return

    def open_shot(self, path):
        try:
            if os.path.exists(path):
                os.startfile(path)
                self.status.config(text="已用系统看图打开截图")
            else:
                self.status.config(text="截图原文件已被清理")
        except Exception as ex:
            self.status.config(text="打不开图片：" + str(ex)[:40])

    # ---------------- 其它
    def new_topic(self):
        self.messages = []
        self.img_paths = []
        self.img_by_idx = {}
        self.chat_imgs = []
        self.shot_at = {}
        self._drop_code_btns()
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self.clear_quote()
        self.status.config(text="新话题")

    def save_note(self):
        convo = [m for m in self.messages if m["role"] in ("user", "assistant")]
        if not convo:
            self.status.config(text="还没有对话")
            return
        try:
            d = self.cfg.get("save_dir") or os.path.join(APP_DIR, "notes")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, time.strftime("%Y-%m-%d") + ".md")
            attach_dir = os.path.join(d, "attachments")
            by_idx = getattr(self, "img_by_idx", {}) or {}
            pending = list(getattr(self, "img_paths", []) or [])
            out = ["\n## %s 陪读 · %s · %s\n" % (time.strftime("%H:%M"), self.provider, self.prompt_name)]
            for idx, m in enumerate(self.messages):
                if m["role"] not in ("user", "assistant"):
                    continue
                c = m["content"]
                img = False
                if isinstance(c, list):
                    txt = next((x.get("text", "") for x in c if x.get("type") == "text"), "")
                    img = any(x.get("type") == "image_url" for x in c)
                else:
                    txt = c
                if m["role"] == "user":
                    quote, _, question = txt.partition("【问题】")
                    quote = quote.replace("【选中原文】", "").strip()
                    question = question.strip()
                    if quote:
                        out.append("> " + "\n> ".join(quote.splitlines()) + "\n")
                    if img:
                        src = by_idx.get(idx) or (pending.pop(0) if pending else "")
                        if src and os.path.exists(src):
                            name = self.img_saved.get(src)
                            if not name:
                                os.makedirs(attach_dir, exist_ok=True)
                                name = os.path.basename(src)
                                dst = os.path.join(attach_dir, name)
                                if os.path.exists(dst):
                                    name = time.strftime("%Y%m%d_%H%M%S") + "_" + name
                                    dst = os.path.join(attach_dir, name)
                                shutil.copy2(src, dst)
                                self.img_saved[src] = name
                            out.append("![[%s]]\n" % name)
                        else:
                            out.append("> （截图原文件已被清理，没存下来）\n")
                    out.append("**问：** " + (question or "这段讲的什么？") + "\n")
                else:
                    out.append("**答：**\n\n" + txt.strip() + "\n")
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(out))
            self.status.config(text="已存到 " + path)
        except Exception as ex:
            self.status.config(text="存笔记失败：" + str(ex)[:60])


# ---------------------------------------------------------------- 启动
def single_instance():
    lock = os.path.join(APP_DIR, "run.lock")
    if os.path.exists(lock):
        try:
            pid = int(open(lock).read().strip())
            h = kernel32.OpenProcess(0x1000, False, pid)
            if h:
                kernel32.CloseHandle(h)
                open(os.path.join(APP_DIR, "show.request"), "w").close()
                log("已有实例在跑，写入显示请求")
                return False
        except Exception:
            pass
    open(lock, "w").write(str(os.getpid()))
    return True


def detach_self():
    """把自己从启动它的控制台 / 编辑器里摘出来：关掉那个窗口也不影响本程序。

    做法：用 pythonw.exe 再起一份自己（标记 --bg），父进程立刻退出。
    子进程用 DETACHED_PROCESS 启动 —— 不挂在任何控制台上，所以关控制台、
    关 Thonny / VS Code 都不会把它带走；再带 CREATE_BREAKAWAY_FROM_JOB，
    防止子进程被编辑器的 Job 对象一起收掉（不支持就退回普通启动）。
    返回 True 表示已经另起了后台实例，当前进程应当退出。
    """
    if os.name != "nt" or "--bg" in sys.argv:
        return False
    if getattr(sys, "frozen", False):
        # 打包件是 GUI 程序，本来就不挂在任何控制台上，关控制台/编辑器带不走它；
        # 不在这里再起一份，免得 onefile 重复解包、启动变慢。
        return False
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(exe):
        exe = sys.executable
    args = [os.path.abspath(__file__), "--bg"]
    env = dict(os.environ, PEIDU_BG="1")
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    for extra in (CREATE_BREAKAWAY_FROM_JOB, 0):
        try:
            subprocess.Popen([exe] + args, cwd=APP_DIR, env=env, close_fds=True,
                             creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | extra,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except OSError as ex:
            log("后台启动失败（flags=%#x）: %r" % (extra, ex))
    return False


def main():
    if any(a.startswith("--selftest-wiki") for a in sys.argv):
        # 打包后自查：确认 opencc 的简繁表和维基检索在 exe 里都能用
        # 用法：陪读.exe --selftest-wiki[=查询词]，默认查「蜀道难」
        _arg = next(a for a in sys.argv if a.startswith("--selftest-wiki"))
        _q = _arg.split("=", 1)[1] if "=" in _arg else "蜀道难"
        out = os.path.join(APP_DIR, "selftest_wiki.txt")
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write("query=%r\n" % _q)
                f.write("variants=%r\n" % (_zh_variants(_q),))
                cfg = load_config()
                ctx, srcs, label = wiki_context(
                    cfg.get("wiki_base") or WIKI_DEFAULT_BASE, _q,
                    fallback=cfg.get("wiki_fallback", WIKI_DEFAULT_FALLBACK),
                    proxy=cfg.get("wiki_proxy", ""))
                f.write("label=%s srcs=%r len=%d\n%s\n" % (label, srcs, len(ctx), ctx[:200]))
        except Exception:
            import traceback
            with open(out, "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        return
    if "--selftest-math" in sys.argv:
        # 打包后自查：确认 matplotlib / ft2font / 字体在 exe 里能跑（无控制台，结果写文件）
        out = os.path.join(APP_DIR, "selftest.txt")
        try:
            lines = []
            for expr in ("a^2 + b^2 = c^2", "v_{Ax} × 常数"):
                p = _math_png(expr, "#e6e6e6", 11, 120, 480, "#1e1f22")
                lines.append("%s -> %s (%s bytes)" % (expr, p, os.path.getsize(p) if p else 0))
            with open(out, "w", encoding="utf-8") as f:
                f.write("ok\n" + "\n".join(lines) + "\n")
        except Exception:
            import traceback
            with open(out, "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        return
    if detach_self():
        log("已转到后台实例，本次启动进程退出")
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    os.makedirs(APP_DIR, exist_ok=True)
    log("启动 frozen=%s python=%s" % (bool(getattr(sys, "frozen", False)), sys.version.split()[0]))
    if not single_instance():
        return
    root = tk.Tk()
    def _err(exc, val, tb):
        import traceback
        log("回调异常: " + "".join(traceback.format_exception(exc, val, tb)))
    root.report_callback_exception = _err
    app = PeiduApp(root)
    try:
        root.mainloop()
    finally:
        try:
            os.remove(os.path.join(APP_DIR, "run.lock"))
        except Exception:
            pass


if __name__ == "__main__":
    main()
