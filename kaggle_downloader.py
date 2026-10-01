# -*- coding: utf-8 -*-
"""kaggle_downloader.py —— Kaggle 公开数据集匿名下载模块（带断点续传）

背景：
Kaggle 数据集页面是 JavaScript 动态渲染，静态爬取拿不到数据表格；且网页下载
需要登录。但 Kaggle 官方为【公开数据集】提供了无需认证的下载端点（官方文档
明确：认证仅私有资源需要）：
    https://www.kaggle.com/api/v1/datasets/download/{owner}/{slug}
该端点 302 重定向到 Google Cloud Storage 的临时签名地址，支持 HTTP Range
断点续传。本模块利用它实现"不登录、断联可续传"的稳定下载，专为国内访问
Kaggle 经常断联的场景设计。

合规声明：
- 只下载【公开】数据集；若端点返回 401/403（非公开/需授权），直接报错提示，
  不做任何绕过；
- 请求失败指数退避重试，尊重服务端；
- 断点续传依赖 Range 请求，属于 HTTP 标准能力。

用法：
    python kaggle_downloader.py                                  # 交互输入
    python kaggle_downloader.py owner/dataset-slug
    python kaggle_downloader.py https://www.kaggle.com/datasets/owner/slug
    python kaggle_downloader.py owner/slug --dir D:\\data --no-preview
也可以在 main.py 中用 --kaggle 参数调用。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import zipfile
from pathlib import Path

import requests
from tqdm import tqdm

from config import ensure_download_dir, load_config
from downloader import preview_file
from utils import request_with_direct_fallback, setup_logging

import logging

logger = logging.getLogger(__name__)

# Kaggle 官方公开数据集下载端点（无需认证；私有资源会返回 401/403）
KAGGLE_DOWNLOAD_URL = "https://www.kaggle.com/api/v1/datasets/download/{handle}"

CHUNK_SIZE = 256 * 1024          # 流式下载块大小
MAX_BACKOFF = 60                  # 重试退避上限（秒）
DATA_FILE_EXTS = (".csv", ".xls", ".xlsx")


def is_kaggle_dataset_url(url: str) -> bool:
    """判断 URL 是否为 Kaggle 数据集页面（https://www.kaggle.com/datasets/...）。"""
    return bool(re.match(r"^https?://(www\.)?kaggle\.com/datasets/[\w-]+/[\w-]+",
                         (url or "").strip(), re.IGNORECASE))


def parse_kaggle_handle(text: str) -> str | None:
    """从用户输入解析 'owner/slug' 标识。

    支持完整页面 URL（含 /data、/versions 等后缀）与 owner/slug 两种形式。
    """
    text = (text or "").strip()
    m = re.search(r"kaggle\.com/datasets/([\w-]+)/([\w-]+)", text, re.IGNORECASE)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    if re.fullmatch(r"[\w-]+/[\w-]+", text):
        return text
    return None


class KaggleDownloader:
    """匿名下载 Kaggle 公开数据集：分块下载 + 断点续传 + 自动解压。"""

    def __init__(self, config: dict):
        self.timeout = int(config.get("timeout", 10))
        self.max_retries = max(int(config.get("max_retries", 3)), 1)
        self.user_agent = str(config.get("user_agent", "WebCSVExcelCollector/1.0"))
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})

    # ------------------------------------------------------------ 元数据

    @staticmethod
    def _meta_path(zip_path: Path) -> Path:
        return zip_path.with_name(zip_path.name + ".meta.json")

    def _load_progress(self, zip_path: Path) -> dict:
        """读取断点进度：{downloaded, total, etag}。无进度返回空 dict。"""
        part = zip_path.with_suffix(zip_path.suffix + ".part")
        meta_file = self._meta_path(zip_path)
        if not (part.exists() and meta_file.exists()):
            return {}
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            downloaded = part.stat().st_size
            if downloaded and meta.get("total") and downloaded <= meta["total"]:
                meta["downloaded"] = downloaded
                return meta
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    @staticmethod
    def _save_progress(zip_path: Path, total: int, etag: str) -> None:
        try:
            KaggleDownloader._meta_path(zip_path).write_text(
                json.dumps({"total": total, "etag": etag}), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ 下载核心

    def _open_request(self, url: str, offset: int):
        """发起（可续传的）下载请求。

        返回 Response（200/206）；或 ("DENIED", code) / ("NOT_FOUND", 404)
        / None（其他失败，由调用方重试）。网络异常直接抛给调用方处理。
        经代理发生 SSL 握手失败时会自动改直连重试一次（见 utils）。
        """
        headers = {}
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"
        resp = request_with_direct_fallback(
            self.session, "GET", url, stream=True, timeout=self.timeout,
            allow_redirects=True, headers=headers)
        # 私有/需授权数据集：明确提示，不绕过
        if resp.status_code in (401, 403):
            return ("DENIED", resp.status_code)
        if resp.status_code == 404:
            return ("NOT_FOUND", 404)
        if resp.status_code not in (200, 206):
            logger.warning("下载请求返回 HTTP %s", resp.status_code)
            resp.close()
            return None
        return resp
        # 私有/需授权数据集：明确提示，不绕过
        if resp.status_code in (401, 403):
            return ("DENIED", resp.status_code)
        if resp.status_code == 404:
            return ("NOT_FOUND", 404)
        if resp.status_code not in (200, 206):
            logger.warning("下载请求返回 HTTP %s", resp.status_code)
            return None
        return resp

    def download(self, handle: str, directory: Path) -> Path | None:
        """下载数据集 zip 到 directory。返回 zip 本地路径；失败返回 None。

        断点续传逻辑：
        - 已有 {file}.zip.part 进度时，带 Range 请求从断点继续（服务器返回 206）；
        - 若资源 ETag/总大小与上次不一致（数据集更新过），自动重新下载；
        - 每次断联/超时后指数退避再重试，重试次数用尽才放弃；
        - 进度保存在 .part 与 .meta.json，Ctrl+C 中断后再次运行即可续传。
        """
        url = KAGGLE_DOWNLOAD_URL.format(handle=handle)
        zip_path = directory / f"{handle.split('/')[-1]}.zip"
        part_path = zip_path.with_suffix(zip_path.suffix + ".part")
        directory.mkdir(parents=True, exist_ok=True)

        # 已完整下载过：直接复用
        if zip_path.exists():
            print(f"文件已存在，跳过下载：{zip_path}")
            return zip_path

        attempt = 0
        progress = self._load_progress(zip_path)
        if progress:
            print(f"检测到未完成的下载（{progress['downloaded']}/{progress['total']} 字节），将从断点续传……")

        while attempt < self.max_retries + 2:  # 外层：断联重试（比页面爬取更宽容）
            attempt += 1
            offset = self._load_progress(zip_path).get("downloaded", 0) \
                if part_path.exists() else 0
            if not part_path.exists():
                offset = 0

            try:
                result = self._open_request(url, offset)
            except requests.exceptions.SSLError:
                logger.warning("第 %d 次尝试失败：SSL 错误", attempt)
                result = None
            except requests.exceptions.Timeout:
                logger.warning("第 %d 次尝试失败：请求超时（%ss）", attempt, self.timeout)
                result = None
            except requests.exceptions.ConnectionError as e:
                logger.warning("第 %d 次尝试失败：连接中断（%s）", attempt, e.__class__.__name__)
                result = None
            except requests.exceptions.RequestException as e:
                logger.warning("第 %d 次尝试失败：%s", attempt, e)
                result = None

            if isinstance(result, tuple):  # 明确被拒绝/不存在：不重试
                status = result[1]
                if status in (401, 403):
                    print(f"HTTP {status}：该数据集不是公开数据集（或需要授权）。"
                          f"\n本工具只下载公开数据集，不做任何绕过。请确认 handle 是否正确，"
                          f"或登录 Kaggle 官网 / 使用官方 kagglehub 获取。")
                else:
                    print(f"HTTP {status}：数据集不存在或已下架，请检查 owner/slug 是否正确。")
                return None
            if result is None:
                if part_path.exists():
                    done = part_path.stat().st_size
                    total = self._load_progress(zip_path).get("total", "?")
                    print(f"  [断联] 进度已保存：{done}/{total} 字节，退避后重试……")
                time.sleep(min(2 ** attempt, MAX_BACKOFF))
                continue

            resp = result
            # 计算总大小与写入模式
            if resp.status_code == 206:
                cr = resp.headers.get("Content-Range", "")   # 形如 "bytes 100-20122/20123"
                m = re.search(r"/(\d+)$", cr)
                if not m:
                    logger.warning("无法解析 Content-Range，放弃本次响应")
                    resp.close()
                    time.sleep(min(2 ** attempt, MAX_BACKOFF))
                    continue
                total = int(m.group(1))
                mode = "ab"                                    # 追加续传
            else:  # 200：从头下载
                offset = 0
                total = int(resp.headers.get("Content-Length") or 0)
                mode = "wb"

            etag = resp.headers.get("ETag", "")
            old = self._load_progress(zip_path)
            # 数据集在服务器端更新过（大小/ETag 变化）：作废旧进度重新下载
            if old and old.get("etag") and etag and old["etag"] != etag:
                logger.info("远端文件已更新（ETag 变化），重新下载。")
                offset, mode = 0, "wb"
            if old and old.get("total") and total and old["total"] != total:
                offset, mode = 0, "wb"
            if offset >= total and total:
                resp.close()
                part_path.rename(zip_path)  # 断点文件已完整
                self._meta_path(zip_path).unlink(missing_ok=True)
                print(f"断点文件已完整，直接完成：{zip_path}")
                return zip_path

            self._save_progress(zip_path, total, etag)
            try:
                with open(part_path, mode) as f, tqdm(
                        total=total or None, initial=offset, unit="B",
                        unit_scale=True, desc=f"  下载 {zip_path.name}",
                        leave=False) as bar:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            bar.update(len(chunk))
                # 循环正常结束 = 下载完成
                if not total or part_path.stat().st_size >= total:
                    part_path.rename(zip_path)
                    self._meta_path(zip_path).unlink(missing_ok=True)
                    return zip_path
                logger.warning("连接提前结束（%s/%s 字节），将续传……",
                               part_path.stat().st_size, total)
                time.sleep(min(2 ** attempt, MAX_BACKOFF))
            except (requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    ConnectionError) as e:
                logger.warning("传输中断（%s），进度已保存，退避后从断点续传……",
                               e.__class__.__name__)
                time.sleep(min(2 ** attempt, MAX_BACKOFF))
            except (PermissionError, OSError) as e:
                print(f"文件写入失败（权限或磁盘问题）：{e}")
                return None
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

        print(f"重试 {attempt - 1} 次后仍失败。进度已保存在 {part_path}，"
              f"稍后重新运行本程序可从断点继续。")
        return None

    # ------------------------------------------------------------ 解压与预览

    @staticmethod
    def _fix_zip_name(info: zipfile.ZipInfo) -> str:
        """修复 zip 内非 UTF-8 标记的中文文件名（cp437 -> gbk 尝试）。"""
        if info.flag_bits & 0x800:  # 已是 UTF-8
            return info.filename
        try:
            return info.filename.encode("cp437").decode("gbk")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return info.filename

    def unzip(self, zip_path: Path, directory: Path) -> list[Path]:
        """解压 zip 到子目录，返回其中的 CSV/Excel 数据文件列表。"""
        target = directory / zip_path.stem
        target.mkdir(parents=True, exist_ok=True)
        print(f"\n正在解压到：{target.resolve()}")
        out_files: list[Path] = []
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = self._fix_zip_name(info)
                # zipfile 会把反斜杠当文件名的一部分，统一转换为路径分隔符
                out_path = target / Path(name.replace("\\", "/"))
                if ".." in out_path.parts:  # 防路径穿越
                    logger.warning("跳过可疑压缩条目：%s", name)
                    continue
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(out_path, "wb") as dst:
                    dst.write(src.read())
                if out_path.suffix.lower() in DATA_FILE_EXTS:
                    out_files.append(out_path)
        return out_files

    def run(self, handle: str, directory: Path, preview: bool = True) -> int:
        """完整流程：下载 -> 解压 -> （可选）预览。返回 0 成功 / 1 失败。"""
        print(f"Kaggle 公开数据集下载（无需登录）：{handle}")
        zip_path = self.download(handle, directory)
        if zip_path is None:
            return 1
        print(f"\n下载完成：{zip_path}（{zip_path.stat().st_size:,} 字节）")
        try:
            data_files = self.unzip(zip_path, directory)
        except (zipfile.BadZipFile, OSError) as e:
            print(f"解压失败（zip 文件可能不完整或已损坏）：{e}\n"
                  f"可删除 {zip_path} 后重新运行下载。")
            return 1
        if not data_files:
            print("解压完成（压缩包内没有 CSV / Excel 文件）。")
            return 0
        print(f"\n压缩包内共 {len(data_files)} 个 CSV/Excel 数据文件：")
        for p in data_files:
            print(f"  - {p.relative_to(directory)}")
        if preview:
            print()
            for p in data_files:
                print(f"[预览] {p.name}")
                preview_file(p)
                print()
        return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    setup_logging()
    parser = argparse.ArgumentParser(
        prog="kaggle_downloader",
        description="匿名下载 Kaggle 公开数据集（官方公开端点，支持断点续传；私有数据集会被拒绝且不绕过）")
    parser.add_argument("dataset", nargs="?", help="数据集页面 URL 或 owner/slug")
    parser.add_argument("--dir", dest="download_dir", help="下载目录（默认读 config.json 的 download_dir/kaggle）")
    parser.add_argument("--no-preview", action="store_true", help="不解压后预览数据文件")
    args = parser.parse_args(argv)

    text = args.dataset
    if not text:
        try:
            text = input("请输入 Kaggle 数据集 URL 或 owner/slug（例如 eishatuzzuhra/ai-usage-and-impact-on-students-and-professionals）：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 0
    handle = parse_kaggle_handle(text)
    if not handle:
        print(f"无法解析数据集标识：{text}\n"
              f"支持形式：https://www.kaggle.com/datasets/{'{owner}'}/{'{slug}'} 或 owner/slug")
        return 1

    config = load_config()
    base_dir = args.download_dir or str(Path(config["download_dir"]) / "kaggle")
    try:
        directory = ensure_download_dir(base_dir)
    except (OSError, PermissionError) as e:
        print(f"下载目录无法创建：{e}")
        return 1

    downloader = KaggleDownloader(config)
    try:
        return downloader.run(handle, directory, preview=not args.no_preview)
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C：下载进度已保存（.part 文件），"
              "再次运行本程序将从断点继续。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
