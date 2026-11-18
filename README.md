# myScraper

> ## ⚠️ 免责声明（请先阅读）
>
> 本项目仅用于个人学习与合法的公开信息研究。使用者须自行确保目标站点的服务条款、robots.txt 与所在地法律法规允许抓取。禁止用于绕过访问控制、抓取个人隐私数据、批量盗取受版权保护内容或任何商业转售。本项目默认遵守 robots.txt，不提供自定义 UA/Cookie/请求头能力，不落盘缓存、不发送遥测、不外传任何抓取数据。因使用本项目产生的一切后果由使用者自行承担。

## 定位

一个低并发、强合规的静态页面抓取与抽取工具。只做 GET，只输出本地文件，不针对任何特定站点。

## 使用须知

- 默认遵守 robots.txt，且**不提供任何关闭入口**（无 `--ignore-robots` 之类的开关）
- 默认 UA 为 `myscraper/<version> (+https://github.com/fallcarrots/myScraper)`，不做浏览器伪装
- 默认每 host 1 req/s、最小请求间隔 0.5s、并发 8（硬上限 64）
- 仅对幂等 GET 的 429/5xx/网络错误重试，`403` 不重试

## 安装

```bash
uv sync
uv run myscraper --help
```

## 开发

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Python 下界 3.12 由 CI 三版本矩阵验证。

## 许可证

MIT，见 [LICENSE](LICENSE)。
