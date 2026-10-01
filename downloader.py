# -*- coding: utf-8 -*-
"""downloader.py —— 下载/导出模块

职责：
1. FILE 候选：流式下载远程文件（iter_content 分块写入，避免大文件占满内存）；
2. TABLE / TEXT 候选（页面内嵌表格与文本数据）：直接把爬取阶段提取的
   DataFrame 写入本地 CSV（utf-8-sig，Excel 打开中文不乱码），不再发网络请求；
3. tqdm 显示下载进度；
4. 失败自动重试（最多 max_retries 次），4xx 客户端错误不重试；
5. 文件名安全化并保留扩展名；重名自动改为 name_1.csv、name_2.csv……
6. 下载/导出后可选使用 pandas 预览（前 5 行 / 行数列数 / 列名）；
   预览失败不影响已下载文件。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

from utils import filename_from_url, sanitize_filename, unique_filepath

logger = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024  # 流式读取块：64 KB

# 按 kind 补默认扩展名（用于 Content-Type 命中但 URL 无扩展名的情况）
DEFAULT_EXT_BY_KIND = {"CSV": ".csv", "Excel": ".xlsx"}


def _build_target_path(directory: Path, candidate) -> Path:
    """确定写入路径：安全化文件名 + 保留扩展名 + 重名自动加序号。"""
    name = filename_from_url(candidate.url)
    if not Path(name).suffix:  # 无扩展名时按类型补一个
        name += DEFAULT_EXT_BY_KIND.get(candidate.kind, ".bin")
    return unique_filepath(directory, name)


def _write_stream(resp: requests.Response, directory: Path, candidate) -> Path:
    """把响应流式写入磁盘（带 tqdm 进度条），返回本地路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = _build_target_path(directory, candidate)
    total = resp.headers.get("Content-Length")
    total = int(total) if total and total.isdigit() else None

    with open(path, "wb") as f, tqdm(total=total, unit="B", unit_scale=True,
                                     desc=f"  下载 {path.name}", leave=False) as bar:
        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                f.write(chunk)
                bar.update(len(chunk))
    return path


def _export_payload(candidate, directory: Path) -> Path:
    """把页面提取的表格/文本数据（DataFrame）写入本地 CSV。"""
    directory.mkdir(parents=True, exist_ok=True)
    filename = sanitize_filename(candidate.filename) or "table.csv"
    if not Path(filename).suffix:
        filename += ".csv"
    path = unique_filepath(directory, filename)
    candidate.payload.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def download_one(candidate, directory: Path, config: dict) -> tuple[bool, str, Path | None]:
    """处理单个候选。返回 (是否成功, 说明消息, 本地路径或 None)。

    - TABLE/TEXT 候选：直接导出已提取的 DataFrame，不发网络请求；
    - FILE 候选：每次尝试前按 delay 限速；网络错误 / 5xx / 429 自动重试，
      4xx（除 429）不重试；文件写入权限错误直接失败并提示，不重试。
    """
    # ---- 页面表格/文本数据：直接导出 ----
    if getattr(candidate, "data_type", "FILE") in ("TABLE", "TEXT") and candidate.payload is not None:
        try:
            path = _export_payload(candidate, directory)
            label = "表格" if candidate.data_type == "TABLE" else "文本数据"
            msg = f"成功（导出页面{label}，{candidate.rows} 行 × {candidate.cols} 列）"
            return True, msg, path
        except (PermissionError, OSError) as e:
            msg = f"文件写入失败（权限或磁盘问题）：{e}"
            logger.error("导出 %s 失败：%s", candidate.filename, e)
            return False, msg, None

    timeout = int(config.get("timeout", 10))
    max_retries = int(config.get("max_retries", 3))
    delay = float(config.get("delay", 1.0))
    headers = {"User-Agent": config.get("user_agent", "WebCSVExcelCollector/1.0")}

    last_error = ""
    for attempt in range(1, max_retries + 1):
        if delay > 0:
            time.sleep(delay)
        try:
            resp = requests.get(candidate.url, stream=True, timeout=timeout, headers=headers)
        except requests.exceptions.SSLError:
            last_error = "SSL 错误（证书校验失败）"
            logger.warning("下载 %s 第 %d/%d 次失败：SSL 错误", candidate.url, attempt, max_retries)
            continue
        except requests.exceptions.Timeout:
            last_error = f"请求超时（{timeout}s）"
            logger.warning("下载 %s 第 %d/%d 次失败：超时", candidate.url, attempt, max_retries)
            continue
        except requests.exceptions.ConnectionError:
            last_error = "连接失败"
            logger.warning("下载 %s 第 %d/%d 次失败：连接错误", candidate.url, attempt, max_retries)
            continue
        except requests.exceptions.RequestException as e:
            last_error = f"请求异常：{e}"
            logger.warning("下载 %s 第 %d/%d 次失败：%s", candidate.url, attempt, max_retries, e)
            continue

        with resp:
            if resp.status_code == 200:
                try:
                    path = _write_stream(resp, directory, candidate)
                    return True, "成功", path
                except (PermissionError, OSError) as e:
                    # 文件写入权限错误：重试无意义，直接失败并提示
                    last_error = f"文件写入失败（权限或磁盘问题）：{e}"
                    logger.error("写入 %s 失败：%s", candidate.url, e)
                    return False, last_error, None
            elif 400 <= resp.status_code < 500 and resp.status_code != 429:
                # 403/404 等客户端错误：不绕过、不重试，直接失败
                last_error = f"HTTP {resp.status_code}（客户端错误，不重试）"
                logger.warning("下载 %s 失败：HTTP %d", candidate.url, resp.status_code)
                return False, last_error, None
            else:
                last_error = f"HTTP {resp.status_code}"
                logger.warning("下载 %s 第 %d/%d 次失败：HTTP %d",
                               candidate.url, attempt, max_retries, resp.status_code)

    return False, last_error or "未知错误", None


def preview_file(path: Path, max_rows: int = 5) -> None:
    """使用 pandas 预览文件：前 5 行、行数列数、列名。任何失败都不影响已下载文件。"""
    try:
        suffix = path.suffix.lower()
        if suffix == ".csv":
            df = pd.read_csv(path)
        elif suffix == ".xlsx":
            df = pd.read_excel(path, engine="openpyxl")
        elif suffix == ".xls":
            df = pd.read_excel(path, engine="xlrd")
        else:
            print(f"    （暂不支持预览 {suffix or '无扩展名'} 格式，已跳过）")
            return
        print(f"    预览 {path.name}：{df.shape[0]} 行 × {df.shape[1]} 列")
        print(f"    列名：{list(df.columns)}")
        with pd.option_context("display.max_columns", None, "display.width", 200):
            print(df.head(max_rows).to_string(index=False))
    except Exception as e:  # 预览失败不影响下载结果
        print(f"    预览失败（不影响已下载文件）：{e}")
        logger.debug("预览 %s 失败：%s", path, e)


def download_batch(candidates: list, directory: Path, config: dict,
                   preview: bool = True) -> list[tuple]:
    """批量下载候选文件并打印统计：成功数量、失败数量、保存目录、失败原因。

    返回 [(candidate, ok, msg, path|None), ...]。
    """
    results: list[tuple] = []
    total = len(candidates)
    print(f"\n开始下载/导出 {total} 个候选到：{directory.resolve()}\n")

    for i, cand in enumerate(candidates, 1):
        print(f"[{i}/{total}] {cand.url}")
        ok, msg, path = download_one(cand, directory, config)
        results.append((cand, ok, msg, path))
        if ok:
            print(f"    已保存：{path}")
            logger.info("下载成功：%s -> %s", cand.url, path)
            if preview:
                preview_file(path)
        else:
            print(f"    失败：{msg}")
            logger.warning("下载失败：%s（%s）", cand.url, msg)
        print()

    # ---- 汇总报告 ----
    success = [r for r in results if r[1]]
    failed = [r for r in results if not r[1]]
    print("=" * 60)
    print(f"下载完成：成功 {len(success)} 个，失败 {len(failed)} 个")
    print(f"保存目录：{directory.resolve()}")
    if failed:
        print("失败原因：")
        for cand, _, msg, _ in failed:
            print(f"  - {cand.filename}（{cand.url}）：{msg}")
    print("=" * 60)
    return results
