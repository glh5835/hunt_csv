# -*- coding: utf-8 -*-
"""config.py —— 配置管理模块

职责：
1. 读取 config.json（与脚本同目录）；
2. 文件不存在时自动创建默认配置；
3. 保存运行时修改的配置（例如交互中输入 d 修改下载目录）；
4. 确保下载目录存在。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 配置文件路径（与本文件同目录）
CONFIG_FILE = Path(__file__).resolve().parent / "config.json"

# 默认配置
DEFAULT_CONFIG = {
    "download_dir": "./downloads",
    "max_depth": 2,
    "same_domain": True,
    "delay": 1.0,
    "timeout": 10,
    "max_retries": 3,
    "user_agent": "Mozilla/5.0 (compatible; WebCSVExcelCollector/1.0)",
    # 新功能：提取页面中"文本形式"的数据表格
    "extract_tables": True,       # 提取 HTML <table> 表格并可作为候选导出为 CSV
    "extract_text_csv": True,     # 提取 <pre>/<code>/<textarea> 中 CSV/TSV 样式的纯文本
    "max_tables_per_page": 10,    # 每个页面最多提取的表格数量（防内存滥用）
}


def load_config() -> dict:
    """读取 config.json；不存在或损坏时使用/重建默认配置。"""
    config = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                config.update(stored)
            else:
                logger.warning("config.json 内容不是 JSON 对象，已使用默认配置。")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("读取 config.json 失败（%s），已使用默认配置。", e)
    else:
        # 首次运行：自动创建默认配置文件
        save_config(DEFAULT_CONFIG)
        print(f"未找到 config.json，已自动创建默认配置：{CONFIG_FILE}")

    # 数值/布尔字段规范化，避免手改配置引入类型问题
    try:
        config["delay"] = float(config.get("delay", 1.0))
        config["max_depth"] = int(config.get("max_depth", 2))
        config["timeout"] = int(config.get("timeout", 10))
        config["max_retries"] = int(config.get("max_retries", 3))
        config["same_domain"] = bool(config.get("same_domain", True))
        config["extract_tables"] = bool(config.get("extract_tables", True))
        config["extract_text_csv"] = bool(config.get("extract_text_csv", True))
        config["max_tables_per_page"] = max(int(config.get("max_tables_per_page", 10)), 1)
        config["user_agent"] = str(config.get("user_agent", DEFAULT_CONFIG["user_agent"]))
    except (TypeError, ValueError) as e:
        logger.warning("config.json 中存在非法数值（%s），相关项已回退默认值。", e)

    if config["delay"] < 1.0:
        # 合规建议：请求间隔至少 1 秒
        logger.warning("当前 delay=%.2f 秒小于 1 秒，可能对目标站点造成压力，建议设置为 >= 1.0。",
                       config["delay"])
    return config


def save_config(config: dict) -> None:
    """把配置保存回 config.json（UTF-8，缩进 2 空格）。"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("保存 config.json 失败：%s", e)
        print(f"保存 config.json 失败：{e}")


def ensure_download_dir(path_str: str) -> Path:
    """确保下载目录存在（不存在则自动创建，含多级目录），返回 Path 对象。"""
    p = Path(path_str).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p
