# -*- coding: utf-8 -*-
"""crawler.py —— 爬虫模块

职责：
1. 检查 robots.txt，遵守目标网站的爬取规则（无法读取时按保守策略处理）；
2. 从起始 URL 出发，按广度优先（BFS）遍历页面（默认仅同域名，深度受 max_depth 限制）；
3. 解析 HTML 中的 <a href> / <link href>，相对 URL 转绝对 URL；
4. 识别 CSV / Excel 文件链接（扩展名匹配，或 HEAD 请求的 Content-Type 匹配）；
5. 只收集候选链接，不在爬取阶段下载文件。

合规声明：本模块只访问公开可访问资源；遇到 401/403 等状态码直接跳过，
不提供也不尝试任何绕过登录、验证码、付费墙、反爬机制的代码。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from utils import filename_from_url, get_domain, is_valid_url, keyword_match, normalize_url

logger = logging.getLogger(__name__)

# 目标文件扩展名 -> 展示类型
FILE_EXTS = {".csv": "CSV", ".xls": "Excel", ".xlsx": "Excel"}

# 已知的电子表格 MIME 类型 -> 展示类型（Content-Type 检测用）
SPREADSHEET_MIMES = {
    "text/csv": "CSV",
    "application/csv": "CSV",
    "application/vnd.ms-excel": "Excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel",
}

# 这些扩展名视为"页面"，可以继续深入爬取（空串 = 目录形式或无扩展名的 URL）
PAGE_EXTS = {".html", ".htm", ".xhtml", ".shtml", ".php", ".asp", ".aspx",
             ".jsp", ".jspx", ".cgi", ".pl", ""}

# robots.txt 规则匹配使用的产品名（对应 User-Agent 中的标识 token）
ROBOTS_TOKEN = "WebCSVExcelCollector"

# 链接文本中出现这些词时，即使扩展名不匹配，也会用 HEAD 请求检测 Content-Type
HEAD_CHECK_HINTS = ("csv", "excel", "spreadsheet", "download", "下载", "导出", "数据表")


@dataclass
class Candidate:
    """一个候选文件链接（爬取阶段只收集信息，不下载内容）。"""
    url: str
    filename: str
    kind: str              # CSV / Excel
    link_text: str = ""    # 链接文本
    page_title: str = ""   # 发现该链接页面的 <title>
    source_page: str = ""  # 发现该链接的页面 URL


class RobotsChecker:
    """robots.txt 检查器（结果按 scheme://host 缓存）。

    保守策略（参考 RFC 9309 惯例）：
    - 正常返回 200：严格按规则判断 can_fetch；
    - 返回 404 / 410 等：视为允许爬取（站点未设置限制）；
    - 返回 401 / 403 / 5xx，或网络错误无法读取：视为禁止爬取并给出警告。
    """

    def __init__(self, timeout: float = 10, fetch_user_agent: str = "*"):
        self.timeout = timeout
        self.fetch_user_agent = fetch_user_agent
        self._cache: dict[str, tuple] = {}  # scheme://host -> (RobotFileParser|None, mode)

    def _load(self, scheme_host: str):
        if scheme_host in self._cache:
            return self._cache[scheme_host]
        robots_url = f"{scheme_host}/robots.txt"
        result = (None, "allow")
        try:
            resp = requests.get(robots_url, timeout=self.timeout,
                                headers={"User-Agent": self.fetch_user_agent})
            code = resp.status_code
            if code == 200:
                rp = robotparser.RobotFileParser()
                rp.parse(resp.text.splitlines())
                result = (rp, "rules")
            elif code in (401, 403):
                logger.warning("robots.txt 返回 %d，按保守策略视为禁止爬取该站点。", code)
                result = (None, "deny")
            elif 500 <= code < 600:
                logger.warning("robots.txt 服务器错误（%d），按保守策略视为禁止爬取该站点。", code)
                result = (None, "deny")
            else:
                # 404 / 410 等：站点未设置 robots 限制
                logger.info("robots.txt 返回 %d，视为允许爬取。", code)
                result = (None, "allow")
        except requests.RequestException as e:
            logger.warning("无法读取 robots.txt（%s），按保守策略视为禁止爬取该站点。", e)
            result = (None, "deny")
        self._cache[scheme_host] = result
        return result

    def allowed(self, url: str) -> tuple[bool, str]:
        """检查 URL 是否被 robots.txt 允许访问。返回 (是否允许, 说明)。"""
        p = urlparse(url)
        scheme_host = f"{p.scheme}://{p.netloc}"
        rp, mode = self._load(scheme_host)
        if mode == "allow":
            return True, "robots.txt 不存在或未限制"
        if mode == "deny":
            return False, "robots.txt 无法读取或明确拒绝（保守策略禁止访问）"
        if rp.can_fetch(ROBOTS_TOKEN, url):
            return True, ""
        return False, "robots.txt 禁止访问该路径"


class Crawler:
    """BFS 页面爬虫：收集公开可访问的 CSV / Excel 文件候选链接。"""

    def __init__(self, config: dict):
        self.config = config
        self.delay = max(float(config.get("delay", 1.0)), 0.0)
        self.timeout = int(config.get("timeout", 10))
        self.max_retries = int(config.get("max_retries", 3))
        self.same_domain = bool(config.get("same_domain", True))
        self.max_depth = int(config.get("max_depth", 2))
        self.user_agent = str(config.get("user_agent", "WebCSVExcelCollector/1.0"))

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self.robots = RobotsChecker(self.timeout, self.user_agent)

    # ------------------------------------------------------------ 请求封装

    def _sleep(self) -> None:
        """请求间隔限速：默认至少 1 秒（可配置），降低对目标站点的压力。"""
        if self.delay > 0:
            time.sleep(self.delay)

    def _request(self, method: str, url: str, **kwargs):
        """带限速与重试的请求封装。成功返回 Response，最终失败返回 None。"""
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            self._sleep()
            try:
                resp = self.session.request(method, url, timeout=self.timeout,
                                            allow_redirects=True, **kwargs)
                # 401/403 直接返回给上层跳过：本工具不做任何绕过尝试
                return resp
            except requests.exceptions.SSLError as e:
                last_error = f"SSL 错误：{e.__class__.__name__}"
                logger.warning("%s %s 第 %d/%d 次失败：SSL 证书校验错误", method, url, attempt, self.max_retries)
            except requests.exceptions.Timeout:
                last_error = "请求超时"
                logger.warning("%s %s 第 %d/%d 次失败：请求超时（timeout=%ss）",
                               method, url, attempt, self.max_retries, self.timeout)
            except requests.exceptions.ConnectionError:
                last_error = "连接失败（DNS 解析失败 / 拒绝连接 / 网络不可达）"
                logger.warning("%s %s 第 %d/%d 次失败：连接错误", method, url, attempt, self.max_retries)
            except requests.exceptions.RequestException as e:
                last_error = f"请求异常：{e}"
                logger.warning("%s %s 第 %d/%d 次失败：%s", method, url, attempt, self.max_retries, e)
        logger.error("%s %s 最终失败：%s", method, url, last_error)
        return None

    # ------------------------------------------------------------ 类型识别

    @staticmethod
    def _ext_kind(url: str) -> str:
        """按 URL 路径扩展名识别类型：返回 'CSV' / 'Excel' / ''。"""
        path = urlparse(url).path.lower()
        for ext, kind in FILE_EXTS.items():
            if path.endswith(ext):
                return kind
        return ""

    @staticmethod
    def _mime_kind(content_type: str) -> str:
        """按 Content-Type 识别电子表格类型（忽略 charset 等参数）。"""
        mime = (content_type or "").split(";")[0].strip().lower()
        return SPREADSHEET_MIMES.get(mime, "")

    @staticmethod
    def _is_page_link(url: str) -> bool:
        """判断链接是否像"页面"，可以继续深入爬取。"""
        ext = ""
        path = urlparse(url).path
        dot = path.rfind(".")
        slash = path.rfind("/")
        if dot > slash:  # 路径最后一段包含扩展名
            ext = path[dot:].lower()
        return ext in PAGE_EXTS

    # ------------------------------------------------------------ HTML 解析

    def _parse_links(self, base_url: str, html: str) -> tuple[str, list[tuple[str, str]]]:
        """解析 HTML，返回 (页面标题, [(绝对链接, 链接文本), ...])。"""
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        links: list[tuple[str, str]] = []
        for tag in soup.find_all(["a", "link"], href=True):
            href = (tag.get("href") or "").strip()
            # 跳过锚点 / 脚本 / 邮件 / 电话 / data 协议
            if not href or href.lower().startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
                continue
            absolute = normalize_url(base_url, href)
            if not absolute:
                continue
            text = " ".join(tag.get_text().split())  # 压平空白
            links.append((absolute, text))
        return title, links

    # ------------------------------------------------------------ HEAD 检测

    def _head_collect(self, url: str, text: str, title: str, source: str,
                      candidates: dict[str, Candidate]) -> None:
        """对扩展名无法判断但文本暗示是下载链接的 URL 发起 HEAD 请求，
        若 Content-Type 是 CSV/Excel 则纳入候选。"""
        if url in candidates:
            return
        allowed, reason = self.robots.allowed(url)
        if not allowed:
            logger.debug("HEAD 检测跳过（robots 限制）：%s", url)
            return
        resp = self._request("HEAD", url)
        if resp is None:
            return
        if resp.status_code >= 400:
            logger.debug("HEAD %s 返回 %d，跳过", url, resp.status_code)
            return
        kind = self._mime_kind(resp.headers.get("Content-Type", ""))
        if kind:
            logger.info("HEAD Content-Type 命中 %s：%s", kind, url)
            candidates[url] = Candidate(url=url, filename=filename_from_url(url),
                                        kind=kind, link_text=text, page_title=title,
                                        source_page=source)

    # ------------------------------------------------------------ 主流程

    def crawl(self, start_url: str) -> list[Candidate]:
        """从 start_url 出发 BFS 爬取，返回去重后的候选文件列表。"""
        start_domain = get_domain(start_url)
        candidates: dict[str, Candidate] = {}   # url -> Candidate（自动去重）
        visited: set[str] = set()               # 已访问的页面 URL
        enqueued: set[str] = {start_url}        # 已入队过的 URL（防止重复入队）
        queue: deque[tuple[str, int]] = deque([(start_url, 0)])
        pages = 0

        while queue:
            url, depth = queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            # 1) robots.txt 检查：被禁止则跳过并提示
            allowed, reason = self.robots.allowed(url)
            if not allowed:
                print(f"  [跳过] {url} —— {reason}")
                logger.warning("robots 限制，跳过：%s（%s）", url, reason)
                continue

            # 2) 抓取页面
            print(f"  [第 {pages + 1} 个页面 | 深度 {depth}] {url}")
            resp = self._request("GET", url)
            pages += 1
            if resp is None:
                continue
            if resp.status_code != 200:
                logger.warning("GET %s 返回 %s，跳过该页面。", url, resp.status_code)
                print(f"  [跳过] HTTP {resp.status_code}：{url}")
                continue

            # 3) 起始/页面 URL 本身就是 CSV/Excel（Content-Type 判定）
            ctype = resp.headers.get("Content-Type", "")
            # 响应头未声明 charset 时，requests 对 text/* 默认用 ISO-8859-1 解码，
            # 会导致中文乱码 -> 用 charset_normalizer 检测实际编码后重新解码
            if "charset" not in ctype.lower():
                resp.encoding = resp.apparent_encoding or "utf-8"
            kind = self._mime_kind(ctype)
            if kind:
                logger.info("页面本身即 %s 文件：%s", kind, url)
                candidates.setdefault(url, Candidate(url=url, filename=filename_from_url(url),
                                                     kind=kind, source_page=url))
                continue
            if "html" not in ctype.lower():
                logger.debug("非 HTML 内容（%s），跳过解析：%s", ctype, url)
                continue

            # 4) 解析页面链接
            title, links = self._parse_links(url, resp.text)
            logger.info("解析页面 %s：title=%r，提取链接 %d 个", url, title, len(links))
            next_depth = depth + 1

            for link, text in links:
                if not is_valid_url(link):
                    continue
                # 同域名限制
                if self.same_domain and get_domain(link) != start_domain:
                    continue

                # 5a) 扩展名命中 -> 候选
                ext_kind = self._ext_kind(link)
                if ext_kind:
                    if link not in candidates:
                        candidates[link] = Candidate(url=link, filename=filename_from_url(link),
                                                     kind=ext_kind, link_text=text,
                                                     page_title=title, source_page=url)
                        print(f"    [发现] {ext_kind}: {link}")
                    continue

                # 5b) 像页面 -> 入队继续 BFS（深度限制 + 去重）
                if next_depth <= self.max_depth and link not in enqueued:
                    if self._is_page_link(link):
                        enqueued.add(link)
                        queue.append((link, next_depth))
                    elif any(h in text.lower() for h in HEAD_CHECK_HINTS):
                        # 5c) 文本暗示是下载链接 -> HEAD 检测 Content-Type
                        self._head_collect(link, text, title, url, candidates)

        logger.info("爬取结束：访问页面 %d 个，收集候选 %d 个。", pages, len(candidates))
        print(f"\n爬取结束：共访问 {pages} 个页面，收集到 {len(candidates)} 个候选链接。")
        return list(candidates.values())


def filter_by_keywords(candidates: list[Candidate], keywords: list[str]) -> list[Candidate]:
    """按关键词过滤候选：匹配 URL / 文件名 / 链接文本 / 页面标题（不区分大小写，任一命中即可）。"""
    matched = [c for c in candidates if keyword_match(keywords, c.url, c.filename,
                                                       c.link_text, c.page_title)]
    return matched
