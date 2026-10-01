# WebCSVExcelCollector —— 网站 CSV / Excel 文件采集器

一个**合规优先**的 Python 命令行工具：输入起始网址和（可选的）关键词后，程序会在目标网站内按广度优先（BFS）爬取**公开可访问**的 CSV / Excel 文件链接（`.csv` / `.xls` / `.xlsx`），列出候选清单，由用户决定是否下载到本地。

> **合规声明（务必阅读）**
> - 本工具只爬取**公开可访问**的数据；
> - 爬取前自动检查并遵守目标网站的 `robots.txt`；被禁止的路径会跳过并提示；
> - 默认请求间隔 1 秒（可配置，建议不要调小），以降低对目标站点的压力；
> - **不做**任何绕过登录、验证码、付费墙、反爬机制的尝试（遇到 401/403 直接跳过）；
> - 使用前请确认目标网站的服务条款（ToS）与数据版权，下载的数据仅限合法用途；
> - 请为自己的爬取行为负责，工具作者不承担滥用责任。

## 功能特性

- BFS 爬取，`max_depth` 控制深度，`same_domain` 控制是否仅同域名；
- robots.txt 检查（含保守策略：robots.txt 无法读取时视为禁止并警告）；
- 双重文件识别：URL 扩展名匹配（`.csv/.xls/.xlsx`）或 HEAD 请求 Content-Type 匹配；
- **页面表格提取**：识别并提取网页中"文本形式"的数据——HTML `<table>` 表格、
  `<pre>/<code>/<textarea>` 中的 CSV/TSV 纯文本，与文件链接一起列为候选，
  选择下载时直接导出为本地 CSV（UTF-8 with BOM，Excel 打开中文不乱码）；
- 关键词过滤：匹配 URL / 文件名 / 链接文本 / 页面标题 / 表格列名，不区分大小写，多个关键词空格分隔、任一命中即可；
- **Kaggle 公开数据集免登录下载**：`--kaggle` 模式走 Kaggle 官方公开端点
  （无需账号），支持 **HTTP Range 断点续传**——国内访问断联后重新运行即可
  从断点继续，不必重头下载；下载的 zip 自动解压并预览其中的 CSV/Excel；
- 交互式下载：`y` 全部下载、`n` 放弃、`d` 修改目录、`1` 或 `1,3` 下载指定序号、`q` 退出；
- 流式下载 + tqdm 进度条 + 失败重试 + 重名自动加序号（`name_1.csv`）；
- 下载后可用 pandas 预览前 5 行、行列数、列名（预览失败不影响文件）；
- 配置持久化：`config.json` 不存在时自动创建，运行时修改目录会立即保存；
- 日志落盘 `app.log`，异常（超时 / SSL / 连接失败 / 权限错误 / Ctrl+C）均有处理。

## 项目结构

```
WebCSVExcelCollector/
├── main.py              # 程序入口：命令行解析 + 交互流程
├── config.py            # 配置读写（config.json 自动创建/保存）
├── crawler.py           # 爬虫：robots 检查 + BFS + 链接解析 + 候选收集
├── downloader.py        # 下载：流式下载 + 重试 + pandas 预览
├── kaggle_downloader.py # Kaggle 公开数据集免登录下载（断点续传 + 自动解压）
├── utils.py             # 工具：日志、URL 校验、文件名安全化、关键词匹配
├── config.json          # 配置文件（首次运行自动生成）
├── requirements.txt     # 依赖清单
└── README.md            # 本文件
```

## 安装

要求 **Python 3.10+**。

```bash
# 1. 进入项目目录
cd WebCSVExcelCollector

# 2. （推荐）创建虚拟环境
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux / macOS:
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt
```

## 运行

```bash
# 交互式：程序会依次询问网址和关键词
python main.py

# 命令行方式：跳过对应交互
python main.py --url https://example.com/data/
python main.py --url https://example.com/data/ --keyword 销售 报表
python main.py --url https://example.com/ --keyword sales --dir D:\mydata
python main.py --no-preview          # 下载后不做 pandas 预览

# Kaggle 公开数据集免登录下载（推荐国内用户使用，断联可续传）
python main.py --kaggle https://www.kaggle.com/datasets/owner/dataset-slug
python main.py --kaggle owner/dataset-slug
python kaggle_downloader.py owner/dataset-slug          # 也可直接运行该模块
# 交互模式下把 Kaggle 数据集页面 URL 当起始网址输入，会自动切换到该模式
```

参数说明：

| 参数 | 说明 |
|---|---|
| `--url` | 起始网址；不提供则交互输入 |
| `--keyword` | 检索关键词，空格分隔多个，可为空；不提供则交互输入 |
| `--dir` | 下载目录，立即生效并保存到 config.json |
| `--kaggle` | Kaggle 公开数据集免登录下载模式（URL 或 owner/slug） |
| `--no-preview` | 下载后不使用 pandas 预览 |

## 配置（config.json）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `download_dir` | `./downloads` | 下载目录，不存在自动创建 |
| `max_depth` | `2` | BFS 最大深度（起始页为 0） |
| `same_domain` | `true` | 是否只爬取同域名页面 |
| `delay` | `1.0` | 请求间隔秒数（合规建议 >= 1.0） |
| `timeout` | `10` | 单次请求超时（秒） |
| `max_retries` | `3` | 网络失败最大重试次数 |
| `user_agent` | `Mozilla/5.0 (compatible; WebCSVExcelCollector/1.0)` | User-Agent |
| `extract_tables` | `true` | 提取页面 HTML `<table>` 表格作为候选（导出为 CSV） |
| `extract_text_csv` | `true` | 提取 `<pre>/<code>/<textarea>` 中 CSV/TSV 样式纯文本 |
| `max_tables_per_page` | `10` | 每页最多提取的表格/文本块数量（防内存滥用） |

- 首次运行自动生成默认配置；
- 运行中输入 `d` 可修改下载目录并**立即保存**回 `config.json`。

## 交互命令

列出候选后：

| 输入 | 含义 |
|---|---|
| `y` | 下载全部候选文件 |
| `n` | 不下载，退出 |
| `d` | 修改下载目录（保存到 config.json 后重新确认） |
| `1` / `1,3` | 只下载指定序号的文件 |
| `q` | 退出 |

## 运行示例

```
$ python main.py
...（横幅与配置信息）...
请输入起始网址：http://127.0.0.1:8000/
请输入检索关键词，可留空：销售

开始爬取……（BFS，最大深度 2，请求间隔 1.0 秒，已启用 robots.txt 检查）
  [第 1 个页面 | 深度 0] http://127.0.0.1:8000/
    [发现] CSV: http://127.0.0.1:8000/data/sales_2023.csv
  [第 2 个页面 | 深度 1] http://127.0.0.1:8000/reports/
    [发现] Excel: http://127.0.0.1:8000/reports/sales_report.xlsx

爬取结束：共访问 2 个页面，收集到 2 个候选链接。

爬取完成：共发现 3 个候选（含文件链接与页面表格/文本数据）。
找到 3 个候选：
1. 2023年销售统计表_table1.csv | 表格（3行×3列，导出为CSV） | http://127.0.0.1:8000/stats.html
2. 2023年销售统计表_text1.csv | 文本CSV（3行×3列，导出为CSV） | http://127.0.0.1:8000/stats.html
3. sales_2023.csv | CSV | http://127.0.0.1:8000/data/sales_2023.csv
当前下载目录：./downloads
请选择：y=全部下载，n=不下载退出，d=修改下载目录，数字=下载指定序号，q=退出
> y
开始下载 2 个文件到：D:\...\downloads
[1/2] http://127.0.0.1:8000/data/sales_2023.csv
    已保存：downloads\sales_2023.csv
    预览 sales_2023.csv：12 行 × 3 列
    列名：['月份', '销售额', '地区']
...
============================================================
下载完成：成功 2 个，失败 0 个
保存目录：D:\...\downloads
============================================================
```

## 常见问题（FAQ）

**Q1：提示“robots.txt 禁止访问该路径”？**
该站点 robots.txt 明确禁止爬虫访问相关路径，本工具会遵守并跳过。请不要试图绕过；如确有需要，请联系站点管理员获取授权或使用官方 API。

**Q2：robots.txt 无法读取时的“保守策略”是什么？**
- 返回 404/410：视为站点未设置限制，允许爬取（robots 协议惯例）；
- 返回 401/403/5xx，或网络错误：**视为禁止爬取**并给出警告（保守处理，宁可少爬不越权）。

**Q3：下载 403/404？**
403 通常是权限/防盗链问题，404 是链接失效。本工具不做任何绕过，直接标记失败。

**Q4：SSL 错误？**
目标站点证书过期/配置错误。本工具默认严格校验证书（安全考虑），不做 `verify=False` 之类的放宽。

**Q5：中文乱码？**
程序已尽量使用 UTF-8。Windows 旧终端（cmd/PowerShell 5）下可先执行 `chcp 65001`，或使用 Windows Terminal。

**Q6：`.xls` 预览报错？**
读取旧版 `.xls` 需要 `xlrd>=2.0.1`（2.x 仅支持 .xls 格式），确认已安装。预览失败不影响已下载的文件。

**Q7：下载的文件重名怎么办？**
自动重命名为 `name_1.csv`、`name_2.csv`，不会覆盖已有文件。

**Q8：想爬慢一点/快一点？**
修改 `config.json` 的 `delay`（秒）。**建议保持 >= 1.0**，这是合规的基本要求。

**Q9：页面上明明看得到表格，为什么提取不到？（Kaggle 等 JS 渲染站点）**
本工具只解析服务器返回的**静态 HTML**。Kaggle、各类单页应用（SPA）的数据表格是
浏览器执行 JavaScript 后从后端 API 动态加载的，静态 HTML 里既没有 `<table>` 也没有
`.csv` 链接（实测 Kaggle 数据集页静态 HTML 仅约 13KB 且 0 个表格），因此无法提取。
这是 requests 静态抓取的固有限制，本工具按合规原则不做浏览器模拟绕过。解决办法：

- **Kaggle 数据集（推荐）**：直接用本项目内置的免登录下载模式——
  `python main.py --kaggle owner/dataset-slug`。它走 Kaggle 官方公开端点
  `https://www.kaggle.com/api/v1/datasets/download/{owner}/{slug}`（官方文档明确
  认证仅私有资源需要），无需登录，且支持断点续传，适合国内不稳定网络；
- 其他站点：尝试找到数据的原始来源页（多为静态 HTML 或直接的文件下载地址）。
- 提取页面表格功能适用于静态渲染表格的网站（维基百科、政府/统计机构公开数据页等）。

**Q10：Kaggle 下载模式断联了怎么办？**
下载进度保存在 `{文件}.zip.part` 与 `{文件}.zip.meta.json` 中，断联后程序会
指数退避自动重试；若重试耗尽或你按了 Ctrl+C，**重新运行同一条命令即可从断点
继续**，不会重头下载。若远端数据集更新过（ETag/大小变化），程序会自动改为
重新下载完整文件。注意：私有数据集（401/403）会被拒绝并提示，本工具不做绕过。

**Q11：提取的"文本CSV"会误判吗？**
`<pre>` 里的代码块若恰好行结构规整（每行逗号数量一致）可能被误判为 CSV。
判定条件已尽量保守（至少 2 行、分隔符数量行间稳定），且最终由你在候选清单中
人工确认选择下载，可用关键词过滤减小干扰。

## 本地测试建议

用 Python 内置 HTTP 服务器即可安全测试（不涉及第三方网站）：

```bash
# 1. 准备测试站点目录
mkdir test_site && cd test_site

# 2. 写一个测试页面 index.html，内容：
#    <html><head><title>数据下载</title></head><body>
#    <a href="data/sales_2023.csv">2023销售数据CSV</a>
#    <a href="reports/sales_report.xlsx">销售报表</a>
#    <a href="reports/other.xls">其他数据</a>
#    <a href="reports/index.html">报表列表页</a>
#    </body></html>
#    并放入对应的 .csv / .xls / .xlsx 样例文件（CSV 用记事本即可创建）

# 3. 启动本地服务器（在 test_site 目录）
python -m http.server 8000

# 4. 另开终端运行本项目
python main.py --url http://127.0.0.1:8000/ --keyword 销售
```

也可以用 pandas 快速生成样例文件：

```python
import pandas as pd
df = pd.DataFrame({"月份": ["1月", "2月"], "销售额": [100, 200], "地区": ["北京", "上海"]})
df.to_csv("test_site/data/sales_2023.csv", index=False, encoding="utf-8-sig")
df.to_excel("test_site/reports/sales_report.xlsx", index=False)
```

测试 robots.txt 逻辑：在 `test_site/robots.txt` 写入
`User-agent: *\nDisallow: /reports/`，再运行程序，应看到 `/reports/` 下的文件被跳过并提示。

## 免责声明

本工具仅用于学习与合法数据采集。使用者应遵守目标网站 robots.txt、服务条款及所在地法律法规，尊重数据版权。对滥用本工具造成的任何后果，使用者自行承担。
