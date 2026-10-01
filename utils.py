# -*- coding: utf-8 -*-
"""utils.py —— 通用工具模块

职责：
1. 日志初始化（控制台 INFO + 文件 app.log DEBUG）；
2. URL 校验、域名提取、相对转绝对、去 fragment；
3. 文件名安全化（去非法字符、保留扩展名）、重名自动加序号；
4. 关键词解析与匹配（不区分大小写，任一命中即可）。
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin, urlparse

LOG_FILE = "app.log"

# Windows / Linux 文件名非法字符（含控制字符）
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def setup_logging() -> None:
    """初始化日志：控制台输出 INFO 级别；文件 app.log 记录 DEBUG 级别（UTF-8）。"""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 文件日志
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        try:
            fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            root.addHandler(fh)
        except OSError as e:
            print(f"警告：无法创建日志文件 {LOG_FILE}：{e}")

    # 控制台日志
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in root.handlers):
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        root.addHandler(ch)

    # 降低第三方库的日志噪音
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ---------------------------------------------------------------- URL 工具

def is_valid_url(url: str) -> bool:
    """校验 URL 格式：必须是 http/https 且带有主机名。"""
    try:
        r = urlparse(url)
        return r.scheme in ("http", "https") and bool(r.netloc)
    except ValueError:
        return False


def get_domain(url: str) -> str:
    """提取 URL 的域名（小写，含端口）。"""
    return urlparse(url).netloc.lower()


def normalize_url(base: str, href: str) -> str:
    """相对 URL -> 绝对 URL，并去掉 #fragment 部分。"""
    try:
        absolute = urljoin(base, href.strip())
    except ValueError:
        return ""
    return urldefrag(absolute)[0]


def filename_from_url(url: str) -> str:
    """从 URL 提取文件名：取路径最后一段，做 URL 解码和安全化；为空时用域名兜底。"""
    path = urlparse(url).path
    name = unquote(path.rstrip("/").rpartition("/")[-1])
    name = sanitize_filename(name)
    if not name:
        name = sanitize_filename(urlparse(url).netloc) or "download"
    return name


# ---------------------------------------------------------------- 文件名工具

def sanitize_filename(name: str, max_len: int = 120) -> str:
    """文件名安全化：替换非法字符为下划线、去掉首尾空白与点号、限制长度（保留扩展名）。"""
    name = _ILLEGAL_CHARS.sub("_", (name or "").replace("\t", "_").replace("\r", "_").replace("\n", "_"))
    name = name.strip(" .")
    if len(name) > max_len:
        stem, dot, suffix = name.rpartition(".")
        if dot and len(suffix) <= 10:  # 保留扩展名
            name = stem[: max_len - len(suffix) - 1] + "." + suffix
        else:
            name = name[:max_len]
    return name


def unique_filepath(directory: Path, filename: str) -> Path:
    """如果文件已存在，自动重命名为 name_1.ext、name_2.ext ……返回可用路径。"""
    p = directory / filename
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    for i in range(1, 10000):
        candidate = directory / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法为 {filename} 生成不重复的文件名")


# ---------------------------------------------------------------- 关键词工具

def parse_keywords(raw: str) -> list[str]:
    """把用户输入按空白拆分为关键词列表（空输入返回空列表）。"""
    return [w for w in re.split(r"\s+", (raw or "").strip()) if w]


def keyword_match(keywords: list[str], *texts: str) -> bool:
    """关键词匹配：不区分大小写；关键词为空时匹配所有；多个关键词任意一个命中即可。

    texts 为参与匹配的文本（URL、文件名、链接文本、页面标题等）。
    """
    if not keywords:
        return True
    blob = " ".join(t or "" for t in texts).lower()
    return any(k.lower() in blob for k in keywords)
