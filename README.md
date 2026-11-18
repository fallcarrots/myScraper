# myscraper

> ## ⚠️ 免责声明（请先阅读）
>
> 本项目仅用于个人学习与合法的公开信息研究。使用者须自行确保目标站点的服务条款、robots.txt 与所在地法律法规允许抓取。禁止用于绕过访问控制、抓取个人隐私数据、批量盗取受版权保护内容或任何商业转售。本项目默认遵守 robots.txt，不提供自定义 UA/Cookie/请求头能力，不落盘缓存、不发送遥测、不外传任何抓取数据。因使用本项目产生的一切后果由使用者自行承担。

## 定位

一个低并发、强合规的**静态页面**抓取与抽取工具：

- **只做 GET**：只发起幂等的 HTTP GET 请求，不提交表单、不做登录态操作。
- **只输出本地文件**：结果写到 stdout 或本地文件，不提供入库、推送、上报能力。
- **不针对任何特定站点**：通用抓取器，不含任何站点适配或反检测逻辑。
- **默认遵守 robots.txt**，且不提供任何关闭入口。

## 快速开始

```bash
# 1. 安装（二选一）
uv add myscraper            # 作为已有项目的依赖加入
uv pip install myscraper    # 安装到当前虚拟环境

# 2. 准备 URL 列表：每行一个 URL，# 开头为注释，空行自动忽略
cat > urls.txt <<'EOF'
# 示例：注释行会被忽略
https://example.com/
https://example.org/
EOF

# 3. 抓取到 JSON Lines（默认输出格式）
myscraper -u urls.txt -o out.jsonl

# 4. 抓取到 CSV
myscraper -u urls.txt -f csv -o out.csv

# 5. 从 stdin 读取
cat urls.txt | myscraper --urls - -f json

# 6. 查看最终生效的配置（YAML + 环境变量 + 命令行合并后的结果）
myscraper --show-config
```

说明：

- `-o` 省略或传 `-` 时结果写入 stdout，可直接管道给下游；进度条与日志只写 stderr。
- `--urls` 省略时同样从 stdin 读取：`cat urls.txt | myscraper -f json`。
- `--show-config` 输出最终配置的 JSON，打印后立即退出，不发起任何抓取。
- 在源码检出目录中可用 `uv run myscraper ...` 调用。

## 命令行参数

| 选项 | 短选项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--urls` | `-u` | 路径 | 无 | URL 列表文件；省略或传 `-` 表示从 stdin 读取。自动补 `https://` 前缀、跳过空行与 `#` 注释、并保序去重 |
| `--config` | `-c` | 路径 | 无 | YAML 配置文件路径；下方「配置文件」一节列出的字段均可在此设置 |
| `--format` | `-f` | 字符串 | `jsonl`（取自 `output.format`） | 输出格式，可选 `jsonl` / `json` / `csv`；非法值直接以退出码 2 失败 |
| `--output` | `-o` | 路径 | 无（写 stdout） | 输出文件路径；传 `-` 时同样写 stdout |
| `--concurrency` | `-j` | 整数 | `8`（取自 `max_concurrency`） | 最大并发数，取值 1–64；抬高并发时全局限速会同步抬到不低于并发数 |
| `--rate-limit` | 无 | 浮点 | `1.0`（取自 `rate_limit.rate_limit_per_host`） | 每主机每秒最大请求数，必须 > 0 |
| `--max-retries` | 无 | 整数 | `3`（取自 `retry.max_retries`） | 失败后的额外重试次数，取值 0–10 |
| `--verbose` | `-v` | 开关 | 关闭 | 打开 DEBUG 级日志（日志只写 stderr） |
| `--show-config` | 无 | 开关 | 关闭 | 打印最终生效配置（JSON）后退出，不发起抓取 |

> 本工具**没有** `--ignore-robots`、`--headers`、`--cookies`、`--user-agent` 之类的选项：关闭 robots 检查、注入自定义请求头或伪装身份在设计上被排除。

## 开发

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## 配置文件（YAML）

完整示例（`config.yaml`）：

```yaml
# 全局最大并发数，1–64；默认 8
max_concurrency: 8

# HTTP 超时与响应体体积上限
http:
  connect_timeout_s: 10.0   # TCP 连接超时秒数，必须 > 0
  read_timeout_s: 30.0      # 读取响应超时秒数，必须 > 0
  max_html_bytes: 5242880   # 单个 HTML 响应体最大字节数（5 MiB）

# 速率限制
rate_limit:
  rate_limit_per_host: 1.0    # 每主机每秒请求数，必须 > 0
  min_request_interval: 0.5  # 同主机两次请求的最小间隔秒数，>= 0
  global_rate_limit: 10.0    # 全局每秒请求数上限（所有 host 合计），(0, 64]

# 失败重试与退避
retry:
  max_retries: 3        # 额外重试次数，0–10
  base_backoff_s: 0.5   # 指数退避基准秒数，必须 > 0
  max_backoff_s: 30.0   # 单次退避上限秒数，必须 > 0

# robots.txt 合规（respect_robots 不可关闭）
robots:
  respect_robots: true      # 只能为 true，写 false 会直接校验失败
  max_crawl_delay_s: 30.0   # 愿意等待的最大 Crawl-delay 秒数，超过则跳过该站点
  cache_ttl_s: 3600.0       # robots.txt 解析结果的进程内缓存 TTL 秒数

# 页面抽取限制
extract:
  max_text_chars: 20000  # 单页正文最大字符数，>= 1
  max_links: 500         # 单页链接最大数量，>= 0
  selectors: {}          # 可选：槽位 -> CSS 选择器映射（title / body / links）

# 输出
output:
  format: jsonl        # jsonl / json / csv
  csv_utf8_bom: false  # CSV 是否带 UTF-8 BOM（便于 Excel 打开）
```

### `respect_robots` 不可关闭

`robots.respect_robots` **恒为 `true`**。在 YAML 里写 `false`（或 `no`、`0` 等任何非 `true` 取值）会直接触发配置校验失败，进程以退出码 `2` 结束；项目在模型层做了「字段校验器 + 模型级断言」双层拦截，没有绕过路径。

### 优先级

**YAML < 环境变量 < 命令行参数**（后者覆盖前者）。

### 环境变量白名单

只有下列 `MYSCRAPER_` 前缀变量会被读取，其余同前缀变量一律报错（退出码 2）：

| 环境变量 | 对应配置项 | 类型 |
| --- | --- | --- |
| `MYSCRAPER_MAX_CONCURRENCY` | `max_concurrency` | 整数 |
| `MYSCRAPER_RATE_LIMIT_PER_HOST` | `rate_limit.rate_limit_per_host` | 浮点 |
| `MYSCRAPER_MIN_REQUEST_INTERVAL` | `rate_limit.min_request_interval` | 浮点 |
| `MYSCRAPER_GLOBAL_RATE_LIMIT` | `rate_limit.global_rate_limit` | 浮点 |
| `MYSCRAPER_CONNECT_TIMEOUT_S` | `http.connect_timeout_s` | 浮点 |
| `MYSCRAPER_READ_TIMEOUT_S` | `http.read_timeout_s` | 浮点 |
| `MYSCRAPER_MAX_HTML_BYTES` | `http.max_html_bytes` | 整数 |
| `MYSCRAPER_MAX_RETRIES` | `retry.max_retries` | 整数 |
| `MYSCRAPER_BASE_BACKOFF_S` | `retry.base_backoff_s` | 浮点 |
| `MYSCRAPER_MAX_BACKOFF_S` | `retry.max_backoff_s` | 浮点 |
| `MYSCRAPER_MAX_TEXT_CHARS` | `extract.max_text_chars` | 整数 |
| `MYSCRAPER_MAX_LINKS` | `extract.max_links` | 整数 |
| `MYSCRAPER_CACHE_TTL_S` | `robots.cache_ttl_s` | 浮点 |
| `MYSCRAPER_MAX_CRAWL_DELAY_S` | `robots.max_crawl_delay_s` | 浮点 |

> `MYSCRAPER_RESPECT_ROBOTS` **会被显式拒绝**：只要在环境中出现该变量，加载配置即失败（退出码 2）。同理，配置模型不接受任何 `headers` / `cookies` / `user_agent` 字段。

## 输出格式

三种格式由 `--format`（或 `output.format`）选择，结果内容一致，只是封装方式不同：

| 格式 | 结构 | 适用场景 |
| --- | --- | --- |
| `jsonl`（默认） | 每行一个独立 JSON 对象（JSON Lines） | 流式追加、大数据量、逐行消费 |
| `json` | 一个 JSON 数组，元素为记录对象 | 需要整体交给 JSON 工具处理；空结果集输出 `[]` |
| `csv` | 带表头的 CSV，列固定 | 表格分析、Excel 打开 |

### 记录字段

两种 JSON 输出中的每条记录字段相同，对应 `PageRecord`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `url` | string | 请求的初始 URL（未经重定向） |
| `final_url` | string | 重定向链结束后的最终 URL |
| `status_code` | integer | HTTP 状态码；未拿到响应时为 `0` |
| `title` | string / null | 页面标题，缺失时为 `null` |
| `text` | string | 抽取出的正文纯文本 |
| `links` | string[] | 页面上发现的绝对链接列表 |
| `fetched_at` | string | 抓取完成时间，ISO-8601（tz-aware UTC） |
| `status` | string | 处理状态：`ok` / `skipped` / `failed` |
| `skip_reason` | string / null | `status` 为 `skipped` 时的原因（如 `robots/robots`） |
| `error` | string / null | `status` 为 `failed` 时的错误摘要 |
| `compliance` | object | 合规元数据，键值均为字符串 |

### CSV 规则

- **表头固定来自 `PageRecord` 的字段声明顺序**，末尾追加派生列 `links_count`；与首条数据无关——**即使结果集为空，也会输出且仅输出这一行表头**。
- CSV 列序：`url, final_url, status_code, title, text, links, fetched_at, status, skip_reason, error, compliance, links_count`。
- `links` 是**一个 JSON 字符串**（`json.dumps(..., ensure_ascii=False)`），不是多列或分隔符拼接；解析时对该单元格做一次 JSON 反序列化即可。
- `links_count` 是**派生列**：`len(links)` 的字符串形式，便于表格排序与筛选。
- `compliance` 同样是 JSON 字符串，序列化时按键名排序（`sort_keys=True`），保证输出稳定。
- `None` 值统一写成空字符串；`fetched_at` 写成 ISO-8601 字符串。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部成功；**或全部 URL 因 robots 合规被跳过**（合规跳过视为正常收尾，不算失败） |
| `1` | 部分失败：有成功也有失败 |
| `2` | 配置或输入错误：配置文件缺失 / YAML 格式错误 / 字段校验失败 / 非法环境变量 / URL 列表为空或不可读 |
| `3` | 全部失败：尝试过的 URL 没有任何一条成功 |
| `5` | 内部错误：未预期异常，或结果写出失败（输出不完整） |
| `130` | 被用户中断（Ctrl+C / SIGINT） |

> 判定细节：中断优先级最高（`130`）；没有尝试过任何 URL（`total == 0`）时按配置/输入错误处理（`2`），但若其中存在 robots 跳过记录则回落为 `0`。

## 合规与安全

- **限速**：默认每 host **1 req/s**，同主机最小请求间隔 **0.5s**，全局（所有 host 合计）**10 req/s**；实际间隔取 `max(1/每host速率, 最小间隔, robots 的 Crawl-delay)`。用 `--concurrency` 抬高并发时，全局限速会同步抬到不低于并发数，避免瞬时聚合速率突破全局上限。
- **并发**：默认 **8**，硬上限 **64**（`--concurrency 65` 会以退出码 `2` 失败）。
- **重试**：只对幂等 GET 的 `429`、`5xx` 与网络层错误重试；**`403` 不重试**，其余 `4xx` 也不重试；退避为指数退避（基准 `0.5s`、单次上限 `30s`），服务端给出 `Retry-After` 时优先采用（同样受上限约束）。
- **robots.txt**：请求前判定一次，重定向终点再复核一次；robots.txt 拉不到（5xx / 网络错误）时保守判为整站不可抓，不存在静默放行路径；robots.txt 请求本身也计入限速。
- **User-Agent**：固定为 `myscraper/<version> (+https://github.com/fallcarrots/myScraper)`，**不做浏览器伪装**。
- **不提供自定义 headers / cookie 能力**：配置模型禁止 `headers` / `cookies` / `user_agent` 字段，客户端只设置上述 UA 与必要的连接参数。
- **不落盘缓存**：robots 规则仅进程内缓存（TTL 默认 3600s），响应体不缓存，日志只写 stderr、不落盘。
- **不发遥测**：不上报统计、不外传抓取数据，结果只写到 stdout 或你指定的本地文件。

## 开发

```bash
uv sync                              # 安装依赖
uv run ruff check .                  # 静态检查
uv run ruff format --check .         # 格式检查
uv run mypy                          # 类型检查（strict）
uv run python -m compileall -q src/ tests/   # 字节码编译检查
uv run pytest                        # 运行测试
```

Python 下界 3.12 由 CI 三版本矩阵验证。

## 许可证

MIT，见 [LICENSE](LICENSE)。
