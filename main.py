# -*- coding: utf-8 -*-
"""main.py —— 程序入口

交互流程：
1. 读取 / 创建 config.json，展示当前配置与默认下载目录；
2. 获取起始 URL 与关键词（命令行参数 --url / --keyword 优先，否则交互输入）；
3. BFS 爬取（robots.txt 检查 + 限速），收集候选并按关键词过滤；
4. 列出候选，用户选择：y 全部下载 / n 不下载退出 / d 修改下载目录 /
   数字（如 1 或 1,3）下载指定序号 / q 退出；
5. 下载并输出统计（成功数、失败数、保存目录、失败原因）。

用法示例：
    python main.py
    python main.py --url https://example.com/data/ --keyword 销售
    python main.py --url https://example.com/ --dir D:\\data
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from config import CONFIG_FILE, ensure_download_dir, load_config, save_config
from crawler import Crawler, filter_by_keywords
from downloader import download_batch
from utils import is_valid_url, parse_keywords, setup_logging

logger = logging.getLogger(__name__)

BANNER = r"""
============================================================
  WebCSVExcelCollector —— 网站 CSV / Excel 文件采集器 v1.0
  合规声明：仅爬取公开可访问资源，遵守 robots.txt 与服务条款，
  不绕过登录 / 验证码 / 付费墙 / 反爬机制。
============================================================
"""


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="WebCSVExcelCollector",
        description="合规爬取网站中公开可访问的 CSV / Excel 文件链接，并可选择下载到本地。")
    parser.add_argument("--url", help="起始网址 URL（不提供则运行时交互输入）")
    parser.add_argument("--keyword", help="检索关键词，多个用空格分隔，可为空")
    parser.add_argument("--dir", dest="download_dir", help="下载目录（会保存到 config.json）")
    parser.add_argument("--no-preview", action="store_true", help="下载后不使用 pandas 预览文件")
    return parser.parse_args(argv)


def show_config(config: dict) -> None:
    """启动时显示当前配置和默认下载目录。"""
    print("当前配置：")
    print(f"  下载目录       download_dir : {config['download_dir']}")
    print(f"  最大爬取深度   max_depth    : {config['max_depth']}")
    print(f"  仅同域名       same_domain  : {config['same_domain']}")
    print(f"  请求间隔(秒)   delay        : {config['delay']}")
    print(f"  超时(秒)       timeout      : {config['timeout']}")
    print(f"  最大重试       max_retries  : {config['max_retries']}")
    print(f"  User-Agent                  : {config['user_agent']}")
    print(f"  配置文件：{CONFIG_FILE}")
    print(f"  日志文件：app.log")
    print("-" * 60)


def ask_url() -> str | None:
    """交互获取起始 URL；格式错误循环提示，输入 q 放弃。"""
    while True:
        try:
            raw = input("请输入起始网址：").strip()
        except EOFError:
            return None
        if raw.lower() in ("q", "quit", "exit"):
            return None
        if is_valid_url(raw):
            return raw
        print("  URL 格式错误：需要以 http:// 或 https:// 开头的完整网址（输入 q 退出）。")


def ask_keyword() -> str:
    """交互获取关键词（可为空）。"""
    try:
        return input("请输入检索关键词，可留空：").strip()
    except EOFError:
        return ""


def list_candidates(cands: list, download_dir: str) -> None:
    """打印候选清单（文件链接 + 页面表格/文本数据）与操作提示。"""
    print(f"\n找到 {len(cands)} 个候选：")
    for i, c in enumerate(cands, 1):
        extra = ""
        if getattr(c, "data_type", "FILE") != "FILE":
            extra = f"（{c.rows}行×{c.cols}列，导出为CSV）"
        print(f"{i}. {c.filename} | {c.kind}{extra} | {c.url}")
    print(f"当前下载目录：{download_dir}")
    print("请选择：y=全部下载，n=不下载退出，d=修改下载目录，数字=下载指定序号，q=退出")


def parse_choice_indices(text: str, total: int) -> list[int] | None:
    """把 '1' / '1,3' 这类输入解析为下标列表；非法输入返回 None。"""
    parts = [p for p in re.split(r"[，,\s]+", text.strip()) if p]
    if not parts:
        return None
    indices: list[int] = []
    for p in parts:
        if not p.isdigit():
            return None
        n = int(p)
        if not (1 <= n <= total):
            print(f"  序号 {n} 超出范围（1-{total}）。")
            return None
        indices.append(n - 1)
    return indices


def change_download_dir(config: dict) -> None:
    """交互修改下载目录：立即创建目录并保存到 config.json。"""
    try:
        raw = input("请输入新的下载目录：").strip().strip('"').strip("'")
    except EOFError:
        return
    if not raw:
        print("  目录为空，保持不变。")
        return
    try:
        path = ensure_download_dir(raw)
    except (OSError, PermissionError) as e:
        print(f"  目录创建失败：{e}")
        return
    config["download_dir"] = raw
    save_config(config)
    print(f"  下载目录已更新并保存到 {CONFIG_FILE}：{path.resolve()}")


def prepare_download_dir(config: dict) -> Path | None:
    """确保下载目录可用（不存在则自动创建）；失败返回 None。"""
    try:
        return ensure_download_dir(config["download_dir"])
    except (OSError, PermissionError) as e:
        print(f"下载目录无法创建或不可写：{e}")
        logger.error("下载目录不可用：%s", e)
        return None


def main(argv=None) -> int:
    # Windows 控制台避免中文乱码；行缓冲保证日志与输出顺序稳定
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    setup_logging()
    logger.info("========== WebCSVExcelCollector 启动 ==========")

    args = parse_args(argv)
    config = load_config()

    # 命令行 --dir 优先，立即生效并持久化
    if args.download_dir:
        try:
            ensure_download_dir(args.download_dir)
            config["download_dir"] = args.download_dir
            save_config(config)
        except (OSError, PermissionError) as e:
            print(f"命令行指定的下载目录无法创建：{e}")
            return 1

    print(BANNER)
    show_config(config)

    # ---- 获取 URL 与关键词 ----
    url = args.url if args.url else ask_url()
    if not url:
        print("已退出。")
        return 0
    if not is_valid_url(url):
        print(f"URL 格式错误：{url}\n需要以 http:// 或 https:// 开头的完整网址。")
        return 1

    keyword_raw = args.keyword if args.keyword is not None else ask_keyword()
    keywords = parse_keywords(keyword_raw)
    if keywords:
        print(f"关键词：{'、'.join(keywords)}（任一命中即匹配，不区分大小写）")
    else:
        print("未输入关键词，将匹配所有 CSV / Excel 文件。")

    # ---- 爬取阶段（只收集候选，不下载）----
    try:
        crawler = Crawler(config)
        print(f"\n开始爬取……（BFS，最大深度 {crawler.max_depth}，"
              f"请求间隔 {crawler.delay:.1f} 秒，已启用 robots.txt 检查）")
        candidates = crawler.crawl(url)
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C，已优雅退出。")
        logger.info("用户中断，退出。")
        return 130

    # ---- 关键词过滤 ----
    print(f"\n爬取完成：共发现 {len(candidates)} 个候选"
          f"（含文件链接与页面表格/文本数据）。")
    selected = filter_by_keywords(candidates, keywords)
    dropped = len(candidates) - len(selected)
    if dropped:
        print(f"按关键词过滤掉 {dropped} 个，剩余 {len(selected)} 个候选。")

    if not selected:
        print("没有符合关键词的候选文件，程序结束。")
        return 0

    # ---- 交互选择与下载 ----
    try:
        while True:
            list_candidates(selected, config["download_dir"])
            try:
                choice = input("> ").strip().lower()
            except EOFError:
                print("\n输入流结束，程序退出。")
                break

            if choice == "y":  # 全部下载
                directory = prepare_download_dir(config)
                if directory is None:
                    continue
                download_batch(selected, directory, config, preview=not args.no_preview)
                break
            elif choice == "n":  # 不下载退出
                print("已选择不下载，程序结束。")
                break
            elif choice == "d":  # 修改下载目录后重新确认
                change_download_dir(config)
                continue
            elif choice == "q":
                print("已退出。")
                break
            else:  # 数字：下载指定序号（如 1 或 1,3）
                indices = parse_choice_indices(choice, len(selected))
                if not indices:
                    print("  输入无效，请输入 y / n / d / q 或数字序号（如 1 或 1,3）。")
                    continue
                directory = prepare_download_dir(config)
                if directory is None:
                    continue
                chosen = [selected[i] for i in indices]
                download_batch(chosen, directory, config, preview=not args.no_preview)
                break
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C，已优雅退出。")
        logger.info("用户中断，退出。")
        return 130

    logger.info("========== WebCSVExcelCollector 正常结束 ==========")
    return 0


if __name__ == "__main__":
    sys.exit(main())
