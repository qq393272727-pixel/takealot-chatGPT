# ChatGPT 自动化接口文档

本接口通过 `chatgpt_automation.py` 调用浏览器打开 `https://chatgpt.com/?temporary-chat=true`，
在新标签页中输入固定提示词，获取 GPT 回复并返回。

## 依赖

- Python 3.8+
- Playwright

安装:

```bash
pip install playwright
playwright install
```

## 命令行模式

脚本路径: `chatgpt_automation.py`

通用格式:

```bash
python chatgpt_automation.py <mode> "<text>"
```

参数说明:

- `mode`: `translate` 或 `brand`
- `text`: 用户输入内容
- `--headless`: 可选，无头运行
- `--start-minimized`: 可选，有头模式时最小化启动
- `--profile`: 可选，浏览器用户数据目录，默认 `.chatgpt_profile`
- `--timeout`: 可选，超时毫秒，默认 90000
- `--retries`: 可选，失败重试次数，默认 2
- `--retry-delay`: 可选，重试间隔(秒)，默认 2.0
- `--debug`: 可选，输出调试信息与截图到 `debug/` 目录
- `--warm-pages`: 可选，预热页签数
- `--max-pages`: 可选，最大页签数
- `--max-turns-per-page`: 可选，单页最大对话次数
- `--max-queue-waiters`: 可选，最大等待队列数

首次运行如需登录，请不要加 `--headless`。

### 接口 1: 翻译

功能: 只翻译，不返回其他内容。

输入: 中文或英文

调用:

```bash
python chatgpt_automation.py translate "只翻译这句话即可"
```

内部提示词:

```
只翻译,无需返回其他内容: ${入参}
```

输出: GPT 回复文本

### 接口 2: 判断品牌侵权

功能: 在 Takealot 中跟卖是否存在品牌侵权，返回是/否。

输入: 英文

调用:

```bash
python chatgpt_automation.py brand "Nike Air Max 270"
```

内部提示词:

```
只返回是/否,无需返回其他内容;在Takealot中跟卖是否存在品牌侵权: ${入参}
```

输出: `是` 或 `否`

## HTTP 服务模式

启动服务:

```bash
py chatgpt_automation.py serve --host 127.0.0.1 --port 8000 --headed
```

默认无头运行（后台模式）。如需显示界面可使用 `--headed`，同时可搭配 `--start-minimized`。

默认会预热 5 个 ChatGPT 页面，单页可连续对话 10 次，最多同时保留 10 个页面。超过上限的请求会阻塞等待可用页面。
有头模式下服务启动会先在第一个页签打开 `/config` 页面，然后再预热其他页签。
最大等待队列数默认 20，超过则返回错误。

请求格式:

- `POST /translate`
- `POST /brand`
- `Content-Type: application/json`
- Body: `{ "text": "..." }`

返回格式:

- 成功: `{ "result": "..." }`
- 失败: `{ "error": "..." }`

## 运行状态

查看页签池状态:

```
http://127.0.0.1:8000/health
```

## 模板配置（Web）

打开浏览器访问:

```
http://127.0.0.1:8000/config
```

模板使用 `{text}` 作为输入占位符，例如:

```
只翻译,无需返回其他内容: {text}
```

保存后会写入 MySQL 数据库，接口调用会自动读取最新模板。

可在该页面同时配置预热页签数/最大页签数/单页最大对话次数/最大等待队列数，保存后立即生效。

页面底部会显示最近 100 条日志，上滑可加载更多历史日志。

## MySQL 配置

默认连接:

- host: `localhost`
- port: `3306`
- user: `root`
- password: `Masu@123!`
- database: `chatgpt_automation`

可通过环境变量覆盖:

- `CHATGPT_DB_HOST`
- `CHATGPT_DB_PORT`
- `CHATGPT_DB_USER`
- `CHATGPT_DB_PASS`
- `CHATGPT_DB_NAME`

示例:

```bash
curl -X POST http://127.0.0.1:8000/translate ^
  -H "Content-Type: application/json" ^
  -d "{\"text\":\"只翻译这句话即可\"}"
```

```bash
curl -X POST http://127.0.0.1:8000/brand ^
  -H "Content-Type: application/json" ^
  -d "{\"text\":\"Nike Air Max 270\"}"
```

## 运行注意事项

- 需要可用的浏览器环境，Playwright 会自动下载浏览器。
- 使用临时会话参数 `temporary-chat=true` 打开页面。
- 默认使用持久化 Profile 以复用登录状态。
- 日志写入 `log/log.txt`，单文件超过 5MB 自动切分新文件。
