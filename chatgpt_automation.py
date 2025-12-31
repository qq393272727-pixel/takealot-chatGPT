import argparse
import asyncio
import json
import os
import sys
import threading
import time
import socket
import re
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import parse_qs
import urllib.error
import urllib.request

from playwright.async_api import TimeoutError as AsyncPlaywrightTimeoutError
from playwright.async_api import async_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


CHATGPT_URL = "https://chatgpt.com/?temporary-chat=true"
DEFAULT_TIMEOUT_MS = 90_000
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 2.5
DEBUG_DIR = "debug"
DEFAULT_STABILIZE_DELAY = 1.0
DEFAULT_TRANSLATE_TEMPLATE = "只翻译,无需返回其他内容: {text}"
DEFAULT_BRAND_TEMPLATE = "只返回是/否,无需返回其他内容;在Takealot中跟卖是否存在品牌侵权: {text}"
DEFAULT_QA_MODE = "api"
DEFAULT_API_BASE_URL = "https://grsaiapi.com"
DEFAULT_QA_MODEL = "gemini-3-pro"
DEFAULT_API_TIMEOUT_SEC = 120
SUPPORTED_API_MODELS = [
    "nano-banana-fast",
    "nano-banana",
    "gemini-3-pro",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]
PRODUCT_ENDPOINT = "/product"
DEFAULT_WARM_PAGES = 5
DEFAULT_MAX_PAGES = 10
DEFAULT_MAX_TURNS_PER_PAGE = 10
DEFAULT_MAX_QUEUE_WAITERS = 20
DEFAULT_RESPONSE_TIMEOUT_SEC = 30
LOG_DIR = "log"
LOG_FILE = "log.txt"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_TAIL_BYTES = 1024 * 1024


class Logger:
    def __init__(self, directory=LOG_DIR, filename=LOG_FILE, max_bytes=LOG_MAX_BYTES):
        self.dir = Path(directory)
        self.file = self.dir / filename
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def _rotate_if_needed(self):
        if not self.file.exists():
            return
        if self.file.stat().st_size < self.max_bytes:
            return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        rotated = self.dir / f"log_{timestamp}.txt"
        self.file.rename(rotated)

    def write(self, level, message):
        self.dir.mkdir(parents=True, exist_ok=True)
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {message}\n"
        with self._lock:
            self._rotate_if_needed()
            with self.file.open("a", encoding="utf-8") as f:
                f.write(line)


def _read_log_tail(limit):
    path = Path(LOG_DIR) / LOG_FILE
    if not path.exists():
        return []
    lines = _read_log_tail_lines(path)
    return lines[-limit:]


def _read_log_page(offset, limit):
    path = Path(LOG_DIR) / LOG_FILE
    if not path.exists():
        return []
    lines = _read_log_tail_lines(path)
    if offset < 0:
        offset = 0
    start = max(0, len(lines) - offset - limit)
    end = len(lines) - offset
    return lines[start:end]


def _read_log_tail_lines(path):
    try:
        size = path.stat().st_size
        start = max(0, size - LOG_TAIL_BYTES)
        with path.open("rb") as f:
            f.seek(start)
            data = f.read()
        text = data.decode("utf-8", errors="ignore")
        return text.splitlines()
    except Exception:
        return []


class PageState:
    def __init__(self, page):
        self.page = page
        self.turns = 0
        self.busy = False


def _db_config():
    return {
        "host": os.environ.get("CHATGPT_DB_HOST", "localhost"),
        "port": int(os.environ.get("CHATGPT_DB_PORT", "3306")),
        "user": os.environ.get("CHATGPT_DB_USER", "root"),
        "password": os.environ.get("CHATGPT_DB_PASS", "Masu@123!"),
        "database": os.environ.get("CHATGPT_DB_NAME", "chatgpt_automation"),
    }


def _db_connect(database=None):
    try:
        import mysql.connector
    except Exception as exc:
        raise RuntimeError("缺少 mysql-connector-python，请先安装。") from exc

    cfg = _db_config()
    if database is None:
        cfg.pop("database", None)
    else:
        cfg["database"] = database
    return mysql.connector.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg.get("database"),
        connection_timeout=int(os.environ.get("CHATGPT_DB_TIMEOUT", "5")),
        autocommit=True,
    )


def _ensure_db():
    cfg = _db_config()
    conn = _db_connect(database=None)
    try:
        cur = conn.cursor()
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{cfg['database']}`")
    finally:
        conn.close()

    conn = _db_connect(database=cfg["database"])
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS prompt_templates (
                template_key VARCHAR(64) PRIMARY KEY,
                template_value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS prompt_settings (
                setting_key VARCHAR(64) PRIMARY KEY,
                setting_value VARCHAR(255) NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            "INSERT IGNORE INTO prompt_templates (template_key, template_value) VALUES (%s, %s)",
            ("translate", DEFAULT_TRANSLATE_TEMPLATE),
        )
        cur.execute(
            "INSERT IGNORE INTO prompt_templates (template_key, template_value) VALUES (%s, %s)",
            ("brand", DEFAULT_BRAND_TEMPLATE),
        )
        cur.execute(
            "INSERT IGNORE INTO prompt_settings (setting_key, setting_value) VALUES (%s, %s)",
            ("warm_pages", str(DEFAULT_WARM_PAGES)),
        )
        cur.execute(
            "INSERT IGNORE INTO prompt_settings (setting_key, setting_value) VALUES (%s, %s)",
            ("max_pages", str(DEFAULT_MAX_PAGES)),
        )
        cur.execute(
            "INSERT IGNORE INTO prompt_settings (setting_key, setting_value) VALUES (%s, %s)",
            ("max_turns_per_page", str(DEFAULT_MAX_TURNS_PER_PAGE)),
        )
        cur.execute(
            "INSERT IGNORE INTO prompt_settings (setting_key, setting_value) VALUES (%s, %s)",
            ("max_queue_waiters", str(DEFAULT_MAX_QUEUE_WAITERS)),
        )
        cur.execute(
            "INSERT IGNORE INTO prompt_settings (setting_key, setting_value) VALUES (%s, %s)",
            ("qa_mode", DEFAULT_QA_MODE),
        )
        cur.execute(
            "INSERT IGNORE INTO prompt_settings (setting_key, setting_value) VALUES (%s, %s)",
            ("api_base_url", DEFAULT_API_BASE_URL),
        )
        cur.execute(
            "INSERT IGNORE INTO prompt_settings (setting_key, setting_value) VALUES (%s, %s)",
            ("api_key", ""),
        )
        cur.execute(
            "INSERT IGNORE INTO prompt_settings (setting_key, setting_value) VALUES (%s, %s)",
            ("api_model", DEFAULT_QA_MODEL),
        )
        cur.execute(
            "INSERT IGNORE INTO prompt_settings (setting_key, setting_value) VALUES (%s, %s)",
            ("api_timeout_sec", str(DEFAULT_API_TIMEOUT_SEC)),
        )
    finally:
        conn.close()


def _get_templates():
    _ensure_db()
    cfg = _db_config()
    conn = _db_connect(database=cfg["database"])
    templates = {
        "translate": DEFAULT_TRANSLATE_TEMPLATE,
        "brand": DEFAULT_BRAND_TEMPLATE,
    }
    try:
        cur = conn.cursor()
        cur.execute("SELECT template_key, template_value FROM prompt_templates")
        for key, value in cur.fetchall():
            if key in templates and value:
                templates[key] = value
    finally:
        conn.close()
    return templates


def _save_templates(translate_template, brand_template):
    _ensure_db()
    cfg = _db_config()
    conn = _db_connect(database=cfg["database"])
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO prompt_templates (template_key, template_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE template_value = VALUES(template_value)
            """,
            ("translate", translate_template),
        )
        cur.execute(
            """
            INSERT INTO prompt_templates (template_key, template_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE template_value = VALUES(template_value)
            """,
            ("brand", brand_template),
        )
    finally:
        conn.close()


def _get_settings():
    _ensure_db()
    cfg = _db_config()
    conn = _db_connect(database=cfg["database"])
    settings = {
        "warm_pages": DEFAULT_WARM_PAGES,
        "max_pages": DEFAULT_MAX_PAGES,
        "max_turns_per_page": DEFAULT_MAX_TURNS_PER_PAGE,
        "max_queue_waiters": DEFAULT_MAX_QUEUE_WAITERS,
        "qa_mode": DEFAULT_QA_MODE,
        "api_base_url": DEFAULT_API_BASE_URL,
        "api_key": "",
        "api_model": DEFAULT_QA_MODEL,
        "api_timeout_sec": DEFAULT_API_TIMEOUT_SEC,
    }
    int_keys = {"warm_pages", "max_pages", "max_turns_per_page", "max_queue_waiters", "api_timeout_sec"}
    try:
        cur = conn.cursor()
        cur.execute("SELECT setting_key, setting_value FROM prompt_settings")
        for key, value in cur.fetchall():
            if key in settings:
                if key in int_keys:
                    try:
                        settings[key] = int(value)
                    except ValueError:
                        continue
                else:
                    settings[key] = value
    finally:
        conn.close()
    qa_mode = (settings.get("qa_mode") or DEFAULT_QA_MODE).strip().lower()
    if qa_mode not in ("api", "browser"):
        qa_mode = DEFAULT_QA_MODE
    settings["qa_mode"] = qa_mode
    if not (settings.get("api_base_url") or "").strip():
        settings["api_base_url"] = DEFAULT_API_BASE_URL
    if not (settings.get("api_model") or "").strip():
        settings["api_model"] = DEFAULT_QA_MODEL
    try:
        api_timeout = int(settings.get("api_timeout_sec") or DEFAULT_API_TIMEOUT_SEC)
    except (TypeError, ValueError):
        api_timeout = DEFAULT_API_TIMEOUT_SEC
    settings["api_timeout_sec"] = api_timeout
    return settings


def _save_settings(
    warm_pages,
    max_pages,
    max_turns_per_page,
    max_queue_waiters,
    qa_mode,
    api_base_url,
    api_key,
    api_model,
    api_timeout_sec,
):
    _ensure_db()
    cfg = _db_config()
    conn = _db_connect(database=cfg["database"])
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO prompt_settings (setting_key, setting_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
            """,
            ("warm_pages", str(warm_pages)),
        )
        cur.execute(
            """
            INSERT INTO prompt_settings (setting_key, setting_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
            """,
            ("max_pages", str(max_pages)),
        )
        cur.execute(
            """
            INSERT INTO prompt_settings (setting_key, setting_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
            """,
            ("max_turns_per_page", str(max_turns_per_page)),
        )
        cur.execute(
            """
            INSERT INTO prompt_settings (setting_key, setting_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
            """,
            ("max_queue_waiters", str(max_queue_waiters)),
        )
        cur.execute(
            """
            INSERT INTO prompt_settings (setting_key, setting_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
            """,
            ("qa_mode", (qa_mode or DEFAULT_QA_MODE).strip().lower()),
        )
        cur.execute(
            """
            INSERT INTO prompt_settings (setting_key, setting_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
            """,
            ("api_base_url", (api_base_url or DEFAULT_API_BASE_URL).strip()),
        )
        cur.execute(
            """
            INSERT INTO prompt_settings (setting_key, setting_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
            """,
            ("api_key", api_key),
        )
        cur.execute(
            """
            INSERT INTO prompt_settings (setting_key, setting_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
            """,
            ("api_model", (api_model or DEFAULT_QA_MODEL).strip()),
        )
        cur.execute(
            """
            INSERT INTO prompt_settings (setting_key, setting_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
            """,
            ("api_timeout_sec", str(api_timeout_sec)),
        )
    finally:
        conn.close()


def _apply_template(template, text):
    if "{text}" in template:
        return template.replace("{text}", text)
    return f"{template} {text}".strip()


def _build_chat_api_url(base_url):
    base = (base_url or "").strip()
    if not base:
        base = DEFAULT_API_BASE_URL
    if base.endswith("/"):
        base = base[:-1]
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _call_chat_api(text, settings, timeout_sec=None):
    api_key = (settings.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("API Key 为空，请先在配置页填写。")
    base_url = settings.get("api_base_url") or DEFAULT_API_BASE_URL
    model = settings.get("api_model") or DEFAULT_QA_MODEL
    if timeout_sec is None:
        timeout_sec = settings.get("api_timeout_sec") or DEFAULT_API_TIMEOUT_SEC
    url = _build_chat_api_url(base_url)
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": text}],
    }
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    logger = Logger()
    start_ts = time.time()
    logger.write("INFO", f"API request start model={model} url={url}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        logger.write("ERROR", f"API request HTTP {exc.code}: {detail}")
        raise RuntimeError(f"API 请求失败: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        logger.write("ERROR", f"API request URLError: {exc}")
        raise RuntimeError(f"API 请求失败: {exc}") from exc
    except socket.timeout as exc:
        logger.write("ERROR", f"API request timeout: {exc}")
        raise RuntimeError("API 请求超时，请稍后重试。") from exc

    _write_api_response_debug(data)

    if "data:" in data:
        chunks = []
        for line in data.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            try:
                piece = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                delta = piece["choices"][0].get("delta") or {}
                content = delta.get("content")
                if content:
                    chunks.append(content)
                    continue
            except (KeyError, IndexError, TypeError):
                pass
            try:
                message = piece["choices"][0].get("message") or {}
                content = message.get("content")
                if content:
                    chunks.append(content)
            except (KeyError, IndexError, TypeError):
                continue
        if chunks:
            content = "".join(chunks)
            logger.write("INFO", f"API request completed in {time.time() - start_ts:.2f}s")
            return _strip_think(content)

    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuntimeError("API 响应不是有效 JSON。") from exc

    try:
        content = payload["choices"][0]["message"]["content"]
        logger.write("INFO", f"API request completed in {time.time() - start_ts:.2f}s")
        return _strip_think(content)
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"API 响应格式异常: {payload}") from exc


def _extract_json_object(text):
    if not text:
        return ""
    start = text.find("{")
    if start == -1:
        return ""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return ""


def _to_number(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    cleaned = ""
    for ch in text:
        if ch.isdigit() or ch == "." or ch == "-":
            cleaned += ch
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _normalize_product_payload(data):
    brand = (data.get("brand") or "").strip()
    title_cn = (data.get("title_cn") or "").strip()
    descption_cn = (data.get("descption_cn") or "").strip()
    length = _to_number(data.get("length"))
    width = _to_number(data.get("width"))
    height = _to_number(data.get("height"))
    weight_g = _to_number(data.get("weight_g") or data.get("weight"))
    volume_weight_g = 0.0
    if length > 0 and width > 0 and height > 0:
        volume_weight_g = (length * width * height / 6000.0) * 1000.0
    return {
        "brand": brand,
        "title_cn": title_cn,
        "descption_cn": descption_cn,
        "length": length,
        "width": width,
        "height": height,
        "weight_g": weight_g,
        "volume_weight_g": volume_weight_g,
    }


def _build_product_prompt(title, description):
    return (
        "You are a strict JSON generator. "
        "Return ONLY a JSON object, no extra text. "
        "Extract brand from description field named 'brand' if present; otherwise empty string. "
        "Translate title and description to Chinese. "
        "Extract length/width/height in cm and weight in g from description if present; if not found use 0. "
        "Always output numeric values for length/width/height/weight_g. "
        "Output schema exactly: "
        "{brand:'', title_cn:'', descption_cn:'', length:0, width:0, height:0, weight_g:0}. "
        f"Input title: {title}. Input description: {description}."
    )


def _call_product_api(title, description, settings):
    prompt = _build_product_prompt(title, description)
    text = _call_chat_api(prompt, settings)
    json_text = _extract_json_object(text)
    if not json_text:
        raise RuntimeError("API 返回未包含 JSON。")
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("API 返回 JSON 解析失败。") from exc
    return _normalize_product_payload(payload)


def _write_api_response_debug(data):
    try:
        Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
        path = Path(LOG_DIR) / "api_response.log"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        thread_id = threading.get_ident()
        entry = f"{timestamp} [thread {thread_id}] {data}\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass


def _strip_think(text):
    if not text:
        return text
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def _render_config_page(templates, settings, logs, message=None, error=None, base_url="/"):
    translate = escape(templates.get("translate", DEFAULT_TRANSLATE_TEMPLATE))
    brand = escape(templates.get("brand", DEFAULT_BRAND_TEMPLATE))
    warm_pages = escape(str(settings.get("warm_pages", DEFAULT_WARM_PAGES)))
    max_pages = escape(str(settings.get("max_pages", DEFAULT_MAX_PAGES)))
    max_turns = escape(str(settings.get("max_turns_per_page", DEFAULT_MAX_TURNS_PER_PAGE)))
    max_waiters = escape(str(settings.get("max_queue_waiters", DEFAULT_MAX_QUEUE_WAITERS)))
    qa_mode = settings.get("qa_mode", DEFAULT_QA_MODE)
    api_base_url = escape(str(settings.get("api_base_url", DEFAULT_API_BASE_URL)))
    api_key = escape(str(settings.get("api_key", "")))
    api_model = escape(str(settings.get("api_model", DEFAULT_QA_MODEL)))
    api_timeout_sec = escape(str(settings.get("api_timeout_sec", DEFAULT_API_TIMEOUT_SEC)))
    model_value = (settings.get("api_model") or DEFAULT_QA_MODEL).strip()
    options = []
    if model_value and model_value not in SUPPORTED_API_MODELS:
        custom_value = escape(model_value)
        options.append(f'<option value="{custom_value}" selected>{custom_value} (custom)</option>')
    for model in SUPPORTED_API_MODELS:
        selected = " selected" if model == model_value else ""
        model_escaped = escape(model)
        options.append(f'<option value="{model_escaped}"{selected}>{model_escaped}</option>')
    api_model_options = "\n".join(options)
    base_url = (base_url or "/").strip()
    if not base_url.endswith("/"):
        base_url += "/"
    base_url = escape(base_url)
    logs_json = json.dumps(logs, ensure_ascii=True)
    logs_len = len(logs)
    status = ""
    if message:
        status = f"<p style=\"color: #0a7;\">{escape(message)}</p>"
    if error:
        status = f"<p style=\"color: #c00;\">{escape(error)}</p>"
    html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ChatGPT 模板配置</title>
  <style>
    :root {
      --bg: #f3f2ee;
      --panel: #ffffff;
      --ink: #1f2328;
      --muted: #6b7280;
      --accent: #0f766e;
      --accent-2: #115e59;
      --line: #e6e2d8;
    }
    body {
      font-family: "Segoe UI", "Noto Serif", serif;
      margin: 0;
      background: var(--bg);
      color: var(--ink);
    }
    .wrap {
      max-width: 980px;
      margin: 28px auto 60px;
      padding: 0 20px;
    }
    .hero {
      background: linear-gradient(135deg, #efe7db, #f6f3ec);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px 22px;
      box-shadow: 0 10px 24px rgba(15, 23, 42, 0.06);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }
    h1 { margin: 0; font-size: 22px; letter-spacing: 0.4px; }
    .status { font-size: 13px; }
    .grid {
      display: grid;
      grid-template-columns: 1.1fr 0.7fr 0.7fr;
      gap: 20px;
      margin-top: 18px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 8px 18px rgba(31, 35, 40, 0.06);
    }
    label { font-weight: 600; display: block; margin-top: 14px; }
    textarea {
      width: 100%;
      height: 110px;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
      font-size: 14px;
      background: #fbfaf7;
    }
    input[type="number"] {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px 10px;
      background: #fbfaf7;
    }
    .hint { color: var(--muted); font-size: 12px; margin-top: 4px; }
    .actions { margin-top: 16px; }
    button {
      background: var(--accent);
      color: #fff;
      border: 0;
      border-radius: 10px;
      padding: 10px 16px;
      cursor: pointer;
      font-weight: 600;
    }
    button:hover { background: var(--accent-2); }
    select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px 10px;
      background: #fbfaf7;
    }
    .section-title { margin: 18px 0 8px; font-size: 15px; }
    #log-container {
      border: 1px solid var(--line);
      padding: 10px;
      height: 280px;
      overflow: auto;
      white-space: pre-wrap;
      background: #0b0f14;
      color: #d6e3f0;
      border-radius: 12px;
      font-family: "Consolas", "Courier New", monospace;
      font-size: 12px;
    }
    @media (max-width: 900px) {
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <h1>ChatGPT 模板配置</h1>
      <div class="status">__STATUS__</div>
    </div>
    <form method="post" action="__BASE_URL__config/save">
    <div class="grid">
      <div class="card">
        <label>翻译模板</label>
        <textarea name="translate_template">__TRANSLATE__</textarea>
        <div class="hint">使用 {text} 作为用户输入占位符</div>
        <label>品牌侵权模板</label>
        <textarea name="brand_template">__BRAND__</textarea>
        <div class="hint">使用 {text} 作为用户输入占位符</div>
        <div class="actions">
          <button type="submit">保存</button>
        </div>
      </div>
      <div class="card">
        <label>预热页签数</label>
        <input type="number" name="warm_pages" value="__WARM_PAGES__" min="1" max="10" />
        <label>最大页签数</label>
        <input type="number" name="max_pages" value="__MAX_PAGES__" min="1" max="10" />
        <label>单页最大对话次数</label>
        <input type="number" name="max_turns_per_page" value="__MAX_TURNS__" min="1" max="50" />
        <label>最大等待队列数</label>
        <input type="number" name="max_queue_waiters" value="__MAX_WAITERS__" min="0" max="100" />
      </div>
      <div class="card">
        <div class="section-title">问答 API</div>
        <label>问答模式</label>
        <select name="qa_mode">
          <option value="api" __QA_MODE_API__>API</option>
          <option value="browser" __QA_MODE_BROWSER__>浏览器</option>
        </select>
        <label>API 地址</label>
        <input type="text" name="api_base_url" value="__API_BASE_URL__" />
        <div class="hint">默认: https://grsaiapi.com</div>
        <label>API Key</label>
        <input type="password" name="api_key" value="__API_KEY__" />
        <label>默认模型</label>
        <select name="api_model">
          __API_MODEL_OPTIONS__
        </select>
        <label>API 超时(秒)</label>
        <input type="number" name="api_timeout_sec" value="__API_TIMEOUT__" min="10" max="600" />
      </div>
    </div>
    </form>
    <div class="card" style="margin-top:18px;">
      <h2 style="margin-top:0;">接口示例</h2>
      <label>接口类型</label>
      <select id="demo-endpoint">
        <option value="qa">/qa</option>
        <option value="translate">/translate</option>
        <option value="brand">/brand</option>
        <option value="product">/product</option>
      </select>
      <div id="demo-text-block">
        <label>text</label>
        <textarea id="demo-text">test</textarea>
      </div>
      <div id="demo-product-block" style="display:none;">
        <label>title</label>
        <input type="text" id="demo-title" value="Nike Air Max 270" />
        <label>description</label>
        <textarea id="demo-description">brand: Nike; size: 30cm x 20cm x 10cm; weight: 500g</textarea>
      </div>
      <div class="actions">
        <button type="button" id="demo-send">发送请求</button>
      </div>
      <div class="hint">返回结果</div>
      <div id="demo-result" style="white-space:pre-wrap; background:#0b0f14; color:#d6e3f0; padding:10px; border-radius:10px; font-family:Consolas, 'Courier New', monospace; font-size:12px;"></div>
    </div>
    <div class="card" style="margin-top:18px;">
      <h2 style="margin-top:0;">日志</h2>
      <div id="log-container"></div>
      <div id="log-loading" class="hint">上滑加载更多</div>
    </div>
  </div>
  <script>
    const baseUrl = "__BASE_URL__";
    const logContainer = document.getElementById('log-container');
    let offset = 0;
    const pageSize = 100;
    function appendLogs(lines, toTop) {
      const text = lines.join('\\n') + '\\n';
      if (toTop) {
        const prev = logContainer.textContent;
        logContainer.textContent = text + prev;
      } else {
        logContainer.textContent += text;
      }
    }
    function loadMore(toTop) {
      fetch(baseUrl + "logs?offset=" + offset + "&limit=" + pageSize)
        .then(r => r.json())
        .then(data => {
          if (!data.lines || data.lines.length === 0) return;
          appendLogs(data.lines, toTop);
          offset += data.lines.length;
        });
    }
    appendLogs(__LOGS_JSON__, false);
    offset = __LOGS_LEN__;
    logContainer.addEventListener('scroll', () => {
      if (logContainer.scrollTop === 0) {
        loadMore(true);
      }
    });

    const endpointSelect = document.getElementById('demo-endpoint');
    const textBlock = document.getElementById('demo-text-block');
    const productBlock = document.getElementById('demo-product-block');
    const resultBox = document.getElementById('demo-result');
    function toggleDemoFields() {
      const isProduct = endpointSelect.value === 'product';
      textBlock.style.display = isProduct ? 'none' : 'block';
      productBlock.style.display = isProduct ? 'block' : 'none';
    }
    endpointSelect.addEventListener('change', toggleDemoFields);
    toggleDemoFields();

    document.getElementById('demo-send').addEventListener('click', () => {
      const endpoint = endpointSelect.value;
      let body = {};
      if (endpoint === 'product') {
        body = {
          title: document.getElementById('demo-title').value,
          description: document.getElementById('demo-description').value,
        };
      } else {
        body = { text: document.getElementById('demo-text').value };
      }
      resultBox.textContent = '请求中...';
      fetch(baseUrl + endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
        .then(r => r.json())
        .then(data => {
          resultBox.textContent = JSON.stringify(data, null, 2);
        })
        .catch(err => {
          resultBox.textContent = String(err);
        });
    });
  </script>
</body>
</html>"""
    html = html.replace("__STATUS__", status)
    html = html.replace("__TRANSLATE__", translate)
    html = html.replace("__BRAND__", brand)
    html = html.replace("__WARM_PAGES__", warm_pages)
    html = html.replace("__MAX_PAGES__", max_pages)
    html = html.replace("__MAX_TURNS__", max_turns)
    html = html.replace("__MAX_WAITERS__", max_waiters)
    html = html.replace("__LOGS_JSON__", logs_json)
    html = html.replace("__LOGS_LEN__", str(logs_len))
    html = html.replace("__BASE_URL__", base_url)
    html = html.replace("__QA_MODE_API__", "selected" if qa_mode == "api" else "")
    html = html.replace("__QA_MODE_BROWSER__", "selected" if qa_mode == "browser" else "")
    html = html.replace("__API_BASE_URL__", api_base_url)
    html = html.replace("__API_KEY__", api_key)
    html = html.replace("__API_MODEL__", api_model)
    html = html.replace("__API_MODEL_OPTIONS__", api_model_options)
    html = html.replace("__API_TIMEOUT__", api_timeout_sec)
    return html


class ChatGPTClient:
    def __init__(
        self,
        headless=False,
        user_data_dir=".chatgpt_profile",
        timeout_ms=DEFAULT_TIMEOUT_MS,
        retries=DEFAULT_RETRIES,
        retry_delay=DEFAULT_RETRY_DELAY,
        start_minimized=False,
        debug=False,
        template_provider=None,
        stabilize_delay=DEFAULT_STABILIZE_DELAY,
        warm_pages=DEFAULT_WARM_PAGES,
        max_pages=DEFAULT_MAX_PAGES,
        max_turns_per_page=DEFAULT_MAX_TURNS_PER_PAGE,
        max_queue_waiters=DEFAULT_MAX_QUEUE_WAITERS,
        warm_on_start=True,
    ):
        self.headless = headless
        self.user_data_dir = Path(user_data_dir)
        self.timeout_ms = timeout_ms
        self.retries = retries
        self.retry_delay = retry_delay
        self.start_minimized = start_minimized
        self.debug = debug
        self.response_timeout_sec = min(DEFAULT_RESPONSE_TIMEOUT_SEC, timeout_ms / 1000.0)
        self.template_provider = template_provider or _get_templates
        self.stabilize_delay = stabilize_delay
        self.warm_pages = warm_pages
        self.max_pages = max_pages
        self.max_turns_per_page = max_turns_per_page
        self.max_queue_waiters = max_queue_waiters
        self._playwright = None
        self._context = None
        self._pool = []
        self._pool_cond = threading.Condition()
        self._waiters = 0
        self._logger = Logger()
        self._warm_on_start = warm_on_start

    def __enter__(self):
        self._playwright = sync_playwright().start()
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        if not self.headless and self.start_minimized:
            args.append("--start-minimized")
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            headless=self.headless,
            args=args,
            viewport={"width": 1280, "height": 900},
        )
        self._context.set_default_timeout(self.timeout_ms)
        if self._warm_on_start:
            self._warm_pool()
        self._logger.write("INFO", "ChatGPTClient started")
        return self

    def __exit__(self, exc_type, exc, tb):
        for state in self._pool:
            try:
                state.page.close()
            except Exception:
                pass
        if self._context:
            self._context.close()
        if self._playwright:
            self._playwright.stop()
        self._logger.write("INFO", "ChatGPTClient stopped")

    def translate(self, text):
        templates = self.template_provider()
        prompt = _apply_template(templates.get("translate", DEFAULT_TRANSLATE_TEMPLATE), text)
        return self._ask_in_new_tab(prompt)

    def brand_infringement(self, text):
        templates = self.template_provider()
        prompt = _apply_template(templates.get("brand", DEFAULT_BRAND_TEMPLATE), text)
        return self._ask_in_new_tab(prompt)

    def ask(self, text):
        return self._ask_in_new_tab(text)

    def _ask_in_new_tab(self, prompt):
        last_error = None
        for attempt in range(self.retries + 1):
            state = self._acquire_page()
            page = state.page
            try:
                self._logger.write("INFO", f"Request start attempt={attempt}")
                if page.url != CHATGPT_URL:
                    page.goto(CHATGPT_URL, wait_until="domcontentloaded")
                if self.debug:
                    self._debug_dump(page, f"loaded_attempt_{attempt}")
                self._wait_for_ready(page)
                if self.stabilize_delay:
                    time.sleep(self.stabilize_delay)
                if self.debug:
                    self._debug_dump(page, f"ready_attempt_{attempt}")
                self._send_prompt(page, prompt)
                if self.stabilize_delay:
                    time.sleep(self.stabilize_delay)
                if self.debug:
                    self._debug_dump(page, f"sent_attempt_{attempt}")
                answer = self._read_last_assistant_message(page)
                self._logger.write("INFO", "Request completed")
                state.turns += 1
                self._release_page(state, recycle=state.turns >= self.max_turns_per_page)
                return answer.strip()
            except Exception as exc:
                last_error = exc
                self._logger.write("ERROR", f"Request failed: {exc}")
                if self.debug:
                    self._debug_dump(page, f"error_attempt_{attempt}")
                self._release_page(state, recycle=True)
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
                continue
        raise RuntimeError(f"请求失败，已重试 {self.retries} 次: {last_error}")


    def open_config_page(self, url):
        page = self._context.new_page()
        try:
            templates = _get_templates()
            settings = _get_settings()
            logs = _read_log_tail(100)
            base_url = f"{url.rsplit('/', 1)[0]}/"
            html = _render_config_page(templates, settings, logs, base_url=base_url)
            page.set_content(html, wait_until="domcontentloaded")
        except Exception:
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception:
                try:
                    page.close()
                except Exception:
                    pass

    def warm_pool(self):
        for _ in range(self.warm_pages):
            state = self._open_page_state()
            if state:
                self._pool.append(state)
        self._logger.write("INFO", f"Warm pages ready: {len(self._pool)}")

    def _open_page_state(self):
        page = self._context.new_page()
        try:
            page.goto(CHATGPT_URL, wait_until="domcontentloaded")
            self._wait_for_ready(page)
            return PageState(page)
        except Exception:
            try:
                page.close()
            except Exception:
                pass
            return None

    def _page_is_usable(self, page):
        try:
            if page.is_closed():
                return False
        except Exception:
            return False
        if self._is_generating(page):
            return False
        try:
            _, loc, _ = self._find_input_in_frames(page)
            if loc:
                return True
        except Exception:
            return False
        return False

    def _acquire_page(self):
        with self._pool_cond:
            while True:
                for state in self._pool:
                    if not state.busy and self._page_is_usable(state.page):
                        state.busy = True
                        self._logger.write("INFO", "Page acquired")
                        return state
                if len(self._pool) < self.max_pages:
                    state = self._open_page_state()
                    if state:
                        state.busy = True
                        self._pool.append(state)
                        self._logger.write("INFO", "Page opened and acquired")
                        return state
                if self._waiters >= self.max_queue_waiters:
                    self._logger.write("WARN", "Queue full")
                    raise RuntimeError("等待队列已满，请稍后重试。")
                self._waiters += 1
                try:
                    self._pool_cond.wait(timeout=1)
                finally:
                    self._waiters -= 1

    def _release_page(self, state, recycle=False):
        with self._pool_cond:
            if recycle or not self._page_is_usable(state.page):
                try:
                    state.page.close()
                except Exception:
                    pass
                if state in self._pool:
                    self._pool.remove(state)
                replacement = self._open_page_state()
                if replacement:
                    self._pool.append(replacement)
            else:
                state.busy = False
                if state in self._pool:
                    self._pool.remove(state)
                    self._pool.append(state)
            self._pool_cond.notify()

    def get_pool_status(self):
        with self._pool_cond:
            total = len(self._pool)
            busy = len([s for s in self._pool if s.busy])
            idle = total - busy
            return {
                "total": total,
                "busy": busy,
                "idle": idle,
                "max_pages": self.max_pages,
                "warm_pages": self.warm_pages,
                "max_turns_per_page": self.max_turns_per_page,
                "max_queue_waiters": self.max_queue_waiters,
                "waiters": self._waiters,
            }

    def _wait_for_ready(self, page):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            # networkidle can hang on ChatGPT due to long-lived connections
        except PlaywrightTimeoutError:
            pass
        try:
            found = page.evaluate(
                """() => {
                    return !!(
                        document.querySelector('#prompt-textarea') ||
                        document.querySelector('textarea[name="prompt-textarea"]') ||
                        document.querySelector('textarea.wcDTda_fallbackTextarea')
                    );
                }"""
            )
            if found:
                return
        except Exception:
            pass
        try:
            page.get_by_role("textbox", name="询问任何问题").wait_for(state="visible", timeout=10_000)
            return
        except PlaywrightTimeoutError:
            pass
        selectors = [
            "textarea[placeholder='询问任何问题']",
            "textarea[placeholder='Ask anything']",
            "textarea[aria-label='Message']",
            "textarea[aria-label='Send a message']",
            "textarea[name='prompt-textarea']",
            "textarea.wcDTda_fallbackTextarea",
            "[role='textbox']",
            "textarea[data-testid='prompt-textarea']",
            "textarea#prompt-textarea",
            "div#prompt-textarea",
            "div[contenteditable='true'][data-testid='prompt-textarea']",
            "div[contenteditable='true']",
        ]
        for sel in selectors:
            try:
                page.wait_for_selector(sel, state="visible", timeout=2_000)
                page.wait_for_function(
                    """(selector) => {
                        const el = document.querySelector(selector);
                        if (!el) return false;
                        if (el.tagName === 'TEXTAREA') return !el.disabled;
                        if (el.getAttribute('contenteditable') === 'true') return true;
                        return false;
                    }""",
                    arg=sel,
                    timeout=2_000,
                )
                return
            except PlaywrightTimeoutError:
                continue
        deadline = time.time() + 10
        while time.time() < deadline:
            frame, loc, _ = self._find_input_in_frames(page)
            if loc:
                return
            time.sleep(0.5)
        if self.debug:
            self._debug_dump(page, "wait_timeout")
        raise RuntimeError("无法定位输入框，请确认已登录且页面可正常加载。")

    def _find_input_in_frames(self, page):
        role_loc = self._find_role_textbox_in_frames(page)
        if role_loc:
            return role_loc
        selectors = [
            "textarea[placeholder='询问任何问题']",
            "textarea[placeholder='Ask anything']",
            "textarea[aria-label='Message']",
            "textarea[aria-label='Send a message']",
            "textarea[name='prompt-textarea']",
            "textarea.wcDTda_fallbackTextarea",
            "[role='textbox']",
            "textarea[data-testid='prompt-textarea']",
            "textarea#prompt-textarea",
            "div#prompt-textarea",
            "div[contenteditable='true'][data-testid='prompt-textarea']",
            "div[contenteditable='true']",
        ]
        for frame in page.frames:
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        return frame, loc, sel
                except Exception:
                    continue
        return None, None, None

    def _find_role_textbox_in_frames(self, page):
        for frame in page.frames:
            try:
                loc = frame.get_by_role("textbox", name="询问任何问题")
                if loc.count() > 0 and loc.first.is_visible():
                    return frame, loc.first, None
            except Exception:
                continue
        return None

    def _select_frame_for_messages(self, page, selector):
        best_frame = None
        best_count = 0
        for frame in page.frames:
            try:
                count = frame.locator(selector).count()
                if count > best_count:
                    best_count = count
                    best_frame = frame
            except Exception:
                continue
        return best_frame

    def _send_prompt(self, page, prompt):
        try:
            page.bring_to_front()
        except Exception:
            pass
        input_frame = page
        if self._send_via_codegen_flow(page, prompt):
            return
        role_loc = None
        try:
            role_loc = page.get_by_role("textbox", name="询问任何问题")
        except Exception:
            role_loc = None
        input_el = None
        input_selectors = [
            "textarea[placeholder='询问任何问题']",
            "textarea[placeholder='Ask anything']",
            "textarea[aria-label='Message']",
            "textarea[aria-label='Send a message']",
            "textarea[name='prompt-textarea']",
            "textarea.wcDTda_fallbackTextarea",
            "[role='textbox']",
            "textarea[data-testid='prompt-textarea']",
            "textarea#prompt-textarea",
            "div#prompt-textarea",
            "div[contenteditable='true'][data-testid='prompt-textarea']",
            "div[contenteditable='true']",
        ]
        input_selector = None
        if role_loc and role_loc.count() > 0 and role_loc.first.is_visible():
            input_el = role_loc.first
        else:
            for sel in input_selectors:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    input_el = loc
                    input_selector = sel
                    break

        if input_el is None:
            input_frame, input_el, input_selector = self._find_input_in_frames(page)

        if input_el is None:
            input_el = self._find_best_input(page)
            input_selector = None
            input_frame = page

        if input_el is None:
            if self._send_prompt_with_polling(page, prompt):
                return
            raise RuntimeError("无法找到输入框。")

        self._apply_input_attempts(page, input_frame, input_el, input_selector, prompt)
        try:
            page.wait_for_timeout(200)
        except Exception:
            pass

        user_selector = "div[data-message-author-role='user']"
        user_frame = self._select_frame_for_messages(page, user_selector) or input_frame
        user_count = user_frame.locator(user_selector).count() if user_frame else 0

        send_selectors = [
            "button[data-testid='send-button']",
            "button[aria-label='Send message']",
            "button[aria-label='Send prompt']",
            "button[aria-label='Send']",
            "button[type='submit']",
        ]
        sent = False
        for sel in send_selectors:
            btn = input_frame.locator(sel).first if input_frame else page.locator(sel).first
            if btn.count() == 0:
                btn = page.locator(sel).first
            if btn.count() == 0:
                continue
            try:
                page.wait_for_function(
                    "(selector) => { const b = document.querySelector(selector); return b && !b.disabled; }",
                    arg=sel,
                    timeout=5_000,
                )
            except PlaywrightTimeoutError:
                pass
            try:
                btn.click()
                sent = True
                break
            except Exception:
                continue

        if not sent:
            try:
                input_el.press("Enter")
                sent = True
            except Exception:
                try:
                    page.keyboard.press("Enter")
                    sent = True
                except Exception:
                    pass
        if not sent:
            try:
                input_el.press("Control+Enter")
                sent = True
            except Exception:
                pass

        try:
            (user_frame or page).wait_for_function(
                """(args) => {
                    const nodes = document.querySelectorAll(args.selector);
                    return nodes.length > args.count;
                }""",
                arg={"selector": user_selector, "count": user_count},
                timeout=10_000,
            )
        except PlaywrightTimeoutError:
            if self._send_prompt_with_polling(page, prompt):
                return
            raise RuntimeError("输入未发送成功，请检查输入框或按钮状态。")

    def _send_via_codegen_flow(self, page, prompt):
        try:
            textbox = page.get_by_role("textbox", name="询问任何问题")
            if textbox.count() == 0:
                return False
            textbox.first.click()
            textbox.first.fill(prompt)
            send_button = page.get_by_test_id("send-button")
            if send_button.count() == 0:
                return False
            send_button.first.click()
            return True
        except Exception:
            return False

    def _apply_input_attempts(self, page, input_frame, input_el, input_selector, prompt):
        attempts = 6
        for _ in range(attempts):
            try:
                input_el.scroll_into_view_if_needed()
            except Exception:
                pass
            try:
                input_el.click()
            except Exception:
                pass
            try:
                if self._type_via_locator(page, prompt):
                    return
                if self._fill_via_role(input_frame, prompt):
                    if self._frame_has_prompt(input_frame, prompt):
                        return
                if self._set_prompt_in_frame(input_frame, prompt):
                    if self._frame_has_prompt(input_frame, prompt):
                        return
                if input_el.evaluate("el => el.getAttribute('contenteditable') === 'true'"):
                    try:
                        input_el.press("Control+A")
                    except Exception:
                        pass
                    try:
                        input_el.type(prompt, delay=20)
                    except Exception:
                        pass
                    if self._input_has_value(input_el, prompt):
                        return
                if input_el.evaluate("el => el.classList && el.classList.contains('ProseMirror')"):
                    if self._fill_prosemirror(input_frame, input_selector, prompt):
                        if self._input_has_value(input_el, prompt):
                            return
                if input_el.evaluate("el => el.tagName === 'TEXTAREA'"):
                    if input_selector:
                        self._force_textarea_input(input_frame, input_selector, prompt)
                    input_el.fill("")
                    input_el.type(prompt, delay=20)
                else:
                    input_el.evaluate(
                        """(el, value) => {
                            el.textContent = '';
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.textContent = value;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                        }""",
                        prompt,
                    )
            except Exception:
                if input_selector:
                    try:
                        input_frame.evaluate(
                            """(selector, value) => {
                                const el = document.querySelector(selector);
                                if (!el) return;
                                if (el.tagName === 'TEXTAREA') {
                                    el.value = value;
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                    el.dispatchEvent(new Event('change', { bubbles: true }));
                                } else if (el.classList && el.classList.contains('ProseMirror')) {
                                    el.innerHTML = '<p>' + value.replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</p>';
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                } else {
                                    el.textContent = value;
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                }
                            }""",
                            input_selector,
                            prompt,
                        )
                    except Exception:
                        pass
                else:
                    try:
                        page.keyboard.type(prompt, delay=20)
                    except Exception:
                        pass
            if self._input_has_value(input_el, prompt):
                return
            self._focus_and_type_with_mouse(page, input_el, prompt)
            if self._input_has_value(input_el, prompt):
                return
            self._paste_via_clipboard(page, input_el, prompt)
            if self._input_has_value(input_el, prompt):
                return
            if self._fill_prompt_via_dom_scan(page, prompt):
                return
            self._focus_placeholder_and_type(page, prompt)
            if self._input_has_value(input_el, prompt):
                return
            if self._frame_has_prompt(input_frame, prompt):
                return
            time.sleep(0.8)
        if self.debug:
            self._debug_dump(page, "input_failed")

    def _type_via_locator(self, page, prompt):
        selectors = [
            "div#prompt-textarea",
            "textarea[name='prompt-textarea']",
            "textarea.wcDTda_fallbackTextarea",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0 or not loc.is_visible():
                    continue
                try:
                    loc.click()
                except Exception:
                    pass
                try:
                    loc.type(prompt, delay=30)
                except Exception:
                    try:
                        page.keyboard.type(prompt, delay=30)
                    except Exception:
                        continue
                if self._input_has_value(loc, prompt):
                    return True
            except Exception:
                continue
        return False

    def _fill_via_role(self, frame, prompt):
        try:
            loc = frame.get_by_role("textbox", name="询问任何问题")
            if loc.count() == 0 or not loc.first.is_visible():
                return False
            target = loc.first
            target.click()
            try:
                target.fill(prompt)
            except Exception:
                target.type(prompt, delay=20)
            return True
        except Exception:
            return False

    def _force_textarea_input(self, frame, selector, prompt):
        try:
            return frame.evaluate(
                """(selector, value) => {
                    const ta = document.querySelector(selector);
                    if (!ta) return false;
                    ta.focus();
                    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
                    setter.call(ta, '');
                    setter.call(ta, value);
                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                    ta.dispatchEvent(new Event('change', { bubbles: true }));
                    return (ta.value || '').includes(value);
                }""",
                selector,
                prompt,
            )
        except Exception:
            return False

    def _frame_has_prompt(self, frame, prompt):
        try:
            return frame.evaluate(
                """(value) => {
                    const pm = document.querySelector('#prompt-textarea');
                    if (pm && (pm.textContent || '').includes(value)) return true;
                    const ta =
                        document.querySelector('textarea[name="prompt-textarea"]') ||
                        document.querySelector('textarea.wcDTda_fallbackTextarea');
                    if (ta && (ta.value || '').includes(value)) return true;
                    return false;
                }""",
                prompt,
            )
        except Exception:
            return False

    def _set_prompt_in_frame(self, frame, prompt):
        try:
            return frame.evaluate(
                """(value) => {
                    const pm = document.querySelector('#prompt-textarea');
                    if (pm && (pm.getAttribute('contenteditable') === 'true' || pm.classList.contains('ProseMirror'))) {
                        pm.focus();
                        document.execCommand('selectAll', false, null);
                        document.execCommand('delete', false, null);
                        const ok = document.execCommand('insertText', false, value);
                        if (!ok) {
                            const escaped = value.replace(/</g, '&lt;').replace(/>/g, '&gt;');
                            pm.innerHTML = '<p>' + escaped + '</p>';
                        }
                        pm.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, inputType: 'insertText', data: value }));
                        pm.dispatchEvent(new Event('input', { bubbles: true }));
                        return true;
                    }
                    const ta =
                        document.querySelector('textarea[name="prompt-textarea"]') ||
                        document.querySelector('textarea.wcDTda_fallbackTextarea');
                    if (ta) {
                        ta.focus();
                        const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
                        setter.call(ta, value);
                        ta.dispatchEvent(new Event('input', { bubbles: true }));
                        ta.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                    return false;
                }""",
                prompt,
            )
        except Exception:
            return False

    def _fill_prosemirror(self, frame, selector, prompt):
        try:
            return frame.evaluate(
                """(selector, value) => {
                    const el = selector ? document.querySelector(selector) : document.querySelector('#prompt-textarea');
                    if (!el) return false;
                    el.focus();
                    document.execCommand('selectAll', false, null);
                    document.execCommand('delete', false, null);
                    const ok = document.execCommand('insertText', false, value);
                    if (!ok) {
                        const escaped = value.replace(/</g, '&lt;').replace(/>/g, '&gt;');
                        el.innerHTML = '<p>' + escaped + '</p>';
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    return true;
                }""",
                selector,
                prompt,
            )
        except Exception:
            return False

    def _focus_placeholder_and_type(self, page, prompt):
        try:
            page.evaluate(
                """(value) => {
                    const texts = ['询问任何问题', 'Ask anything'];
                    const nodes = Array.from(document.querySelectorAll('*')).filter(el => {
                        const rect = el.getBoundingClientRect();
                        if (!rect || rect.width === 0 || rect.height === 0) return false;
                        const t = (el.textContent || '').trim();
                        return texts.some(x => t.includes(x));
                    });
                    if (!nodes.length) return false;
                    const target = nodes[0];
                    target.click();
                    return true;
                }""",
                prompt,
            )
            page.keyboard.type(prompt, delay=20)
        except Exception:
            return False
        return True

    def _debug_dump(self, page, label):
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        debug_path = Path(DEBUG_DIR)
        debug_path.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_path / f"{timestamp}_{label}.png"
        json_path = debug_path / f"{timestamp}_{label}.json"
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass
        try:
            data = {
                "url": page.url,
                "frames": [],
            }
            for frame in page.frames:
                try:
                    frame_info = frame.evaluate(
                        """() => {
                            function visible(el) {
                                const r = el.getBoundingClientRect();
                                return r.width > 0 && r.height > 0;
                            }
                            const nodes = Array.from(
                                document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]')
                            );
                            return nodes.map(el => ({
                                tag: el.tagName.toLowerCase(),
                                class: el.className || '',
                                id: el.id || '',
                                name: el.getAttribute('name') || '',
                                placeholder: el.getAttribute('placeholder') || '',
                                aria_label: el.getAttribute('aria-label') || '',
                                role: el.getAttribute('role') || '',
                                contenteditable: el.getAttribute('contenteditable') || '',
                                display: getComputedStyle(el).display || '',
                                disabled: !!el.disabled,
                                readonly: !!el.readOnly,
                                bbox: el.getBoundingClientRect().toJSON(),
                                visible: visible(el),
                            }));
                        }"""
                    )
                except Exception:
                    frame_info = []
                data["frames"].append(
                    {
                        "url": frame.url,
                        "inputs": frame_info,
                    }
                )
            json_path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
        except Exception:
            pass
    def _input_has_value(self, input_el, prompt):
        try:
            return input_el.evaluate(
                """(el, value) => {
                    if (!el) return false;
                    if (el.tagName === 'TEXTAREA') return (el.value || '').includes(value);
                    return (el.textContent || '').includes(value);
                }""",
                prompt,
            )
        except Exception:
            return False

    def _focus_and_type_with_mouse(self, page, input_el, prompt):
        try:
            box = input_el.bounding_box()
            if not box:
                return False
            x = box["x"] + (box["width"] / 2)
            y = box["y"] + (box["height"] / 2)
            page.mouse.click(x, y)
            page.keyboard.type(prompt, delay=20)
            return True
        except Exception:
            return False

    def _paste_via_clipboard(self, page, input_el, prompt):
        try:
            page.evaluate(
                """(value) => {
                    window.__codexClipboard = value;
                }""",
                prompt,
            )
            input_el.click()
            input_el.press("Control+A")
            input_el.press("Backspace")
            page.keyboard.insert_text(prompt)
            return True
        except Exception:
            return False

    def _fill_prompt_via_dom_scan(self, page, prompt):
        for frame in page.frames:
            try:
                ok = frame.evaluate(
                    """(value) => {
                        function collectInputs(root, out) {
                            const nodes = root.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]');
                            nodes.forEach(n => out.push(n));
                            const all = root.querySelectorAll('*');
                            all.forEach(el => {
                                if (el.shadowRoot) collectInputs(el.shadowRoot, out);
                            });
                        }
                        const inputs = [];
                        collectInputs(document, inputs);
                        if (!inputs.length) return false;
                        inputs.sort((a, b) => {
                            const ra = a.getBoundingClientRect();
                            const rb = b.getBoundingClientRect();
                            const aa = Math.max(0, ra.width) * Math.max(0, ra.height);
                            const ab = Math.max(0, rb.width) * Math.max(0, rb.height);
                            return ab - aa;
                        });
                        const target = inputs[0];
                        if (!target) return false;
                        target.focus();
                        if (target.tagName === 'TEXTAREA') {
                            const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
                            setter.call(target, value);
                            target.dispatchEvent(new Event('input', { bubbles: true }));
                            target.dispatchEvent(new Event('change', { bubbles: true }));
                        } else {
                            target.textContent = value;
                            target.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                        return true;
                    }""",
                    prompt,
                )
                if ok:
                    return True
            except Exception:
                continue
        return False
    def _find_best_input(self, page):
        best = None
        best_area = 0
        textareas = page.locator("textarea")
        count = textareas.count()
        for i in range(count):
            el = textareas.nth(i)
            try:
                if not el.is_visible() or not el.is_enabled():
                    continue
                box = el.bounding_box()
                if not box:
                    continue
                area = box["width"] * box["height"]
                if area > best_area:
                    best = el
                    best_area = area
            except Exception:
                continue
        if best:
            return best

        editables = page.locator("div[contenteditable='true']")
        count = editables.count()
        for i in range(count):
            el = editables.nth(i)
            try:
                if not el.is_visible():
                    continue
                box = el.bounding_box()
                if not box:
                    continue
                area = box["width"] * box["height"]
                if area > best_area:
                    best = el
                    best_area = area
            except Exception:
                continue
        return best

    def _send_prompt_with_polling(self, page, prompt):
        user_selector = "div[data-message-author-role='user']"
        user_frame = self._select_frame_for_messages(page, user_selector)
        initial_user_count = user_frame.locator(user_selector).count() if user_frame else 0
        deadline = time.time() + 20
        while time.time() < deadline:
            for frame in page.frames:
                try:
                    sent = frame.evaluate(
                        """(value) => {
                            function collectInputs(root, out) {
                                const nodes = root.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]');
                                nodes.forEach(n => out.push(n));
                                const all = root.querySelectorAll('*');
                                all.forEach(el => {
                                    if (el.shadowRoot) collectInputs(el.shadowRoot, out);
                                });
                            }
                            const inputs = [];
                            collectInputs(document, inputs);
                            inputs.sort((a, b) => (b.clientWidth * b.clientHeight) - (a.clientWidth * a.clientHeight));
                            const textareas = inputs.filter(el => el.tagName === 'TEXTAREA');
                            const candidates = textareas.filter(t => t.offsetParent !== null && !t.disabled);
                            const target = candidates[0] || inputs[0];
                            if (!target) return false;
                            target.focus();
                            if (target.tagName === 'TEXTAREA') {
                                target.value = value;
                                target.dispatchEvent(new Event('input', { bubbles: true }));
                                target.dispatchEvent(new Event('change', { bubbles: true }));
                            } else {
                                target.textContent = value;
                                target.dispatchEvent(new Event('input', { bubbles: true }));
                            }
                            const btn =
                                document.querySelector("button[data-testid='send-button']") ||
                                document.querySelector("button[aria-label='Send message']") ||
                                document.querySelector("button[aria-label='Send prompt']") ||
                                document.querySelector("button[aria-label='Send']") ||
                                document.querySelector("button[type='submit']");
                            if (btn && !btn.disabled) {
                                btn.click();
                                return true;
                            }
                            return false;
                        }""",
                        prompt,
                    )
                    if sent:
                        (user_frame or frame).wait_for_function(
                            """(args) => {
                                const nodes = document.querySelectorAll(args.selector);
                                return nodes.length > args.count;
                            }""",
                            arg={"selector": user_selector, "count": initial_user_count},
                            timeout=5_000,
                        )
                        return True
                except PlaywrightTimeoutError:
                    return False
                except Exception:
                    continue
            time.sleep(1)
        return False

    def _read_last_assistant_message(self, page):
        start_texts = self._get_assistant_texts(page)
        start_last = start_texts[-1] if start_texts else ""
        deadline = time.time() + self.response_timeout_sec
        if self._detect_page_error(page):
            raise RuntimeError("页面出现错误提示，请重试。")
        while time.time() < deadline:
            if self._detect_page_error(page):
                raise RuntimeError("页面出现错误提示，请重试。")
            texts = self._get_assistant_texts(page)
            if texts:
                last = texts[-1].strip()
                if last and (len(texts) > len(start_texts) or last != start_last):
                    if not self._is_generating(page):
                        return last
            time.sleep(0.5)
        raise RuntimeError("等待回复超时，请重试。")

    def _is_generating(self, page):
        try:
            return page.evaluate(
                """() => {
                    const btn = document.querySelector("button[data-testid='stop-button']");
                    return !!(btn && btn.offsetParent !== null);
                }"""
            )
        except Exception:
            return False

    def _detect_page_error(self, page):
        keywords = [
            "something went wrong",
            "please try again",
            "try again later",
            "we're having trouble",
            "there was a problem",
            "too many requests",
            "network error",
            "服务异常",
            "网络错误",
            "请求失败",
            "发生错误",
            "出现错误",
            "出了问题",
            "请重试",
        ]
        strict_keywords = [
            "something went wrong",
            "we're having trouble",
            "there was a problem",
            "too many requests",
            "network error",
            "服务异常",
            "网络错误",
            "请求失败",
            "发生错误",
            "出现错误",
            "出了问题",
            "请重试",
        ]
        try:
            return page.evaluate(
                """(args) => {
                    const keys = args.keys || [];
                    const strictKeys = args.strictKeys || [];
                    const selectors = [
                        '[role="alert"]',
                        '[data-testid*="error"]',
                        '[data-testid*="retry"]',
                        '[aria-live="assertive"]',
                        '.error',
                        '.error-message'
                    ];
                    const nodes = [];
                    selectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(n => nodes.push(n));
                    });
                    const text = nodes.map(n => (n.innerText || '').trim()).join(' ').toLowerCase();
                    const body = (document.body && document.body.innerText || '').toLowerCase();
                    const has = (hay, list) => list.some(k => hay.includes(k));
                    if (text && has(text, keys)) return true;
                    return has(body, strictKeys);
                }""",
                arg={"keys": [k.lower() for k in keywords], "strictKeys": [k.lower() for k in strict_keywords]},
            )
        except Exception:
            return False

    def _get_assistant_texts(self, page):
        best = []
        for frame in page.frames:
            try:
                texts = frame.evaluate(
                    """() => {
                        const nodes = Array.from(
                            document.querySelectorAll("div[data-message-author-role='assistant']")
                        );
                        return nodes.map(n => (n.innerText || '').trim()).filter(Boolean);
                    }"""
                )
            except Exception:
                texts = []
            if len(texts) > len(best):
                best = texts
        return best


class AsyncChatGPTClient:
    def __init__(
        self,
        headless=False,
        user_data_dir=".chatgpt_profile",
        timeout_ms=DEFAULT_TIMEOUT_MS,
        retries=DEFAULT_RETRIES,
        retry_delay=DEFAULT_RETRY_DELAY,
        start_minimized=False,
        debug=False,
        stabilize_delay=DEFAULT_STABILIZE_DELAY,
        warm_pages=DEFAULT_WARM_PAGES,
        max_pages=DEFAULT_MAX_PAGES,
        max_turns_per_page=DEFAULT_MAX_TURNS_PER_PAGE,
        max_queue_waiters=DEFAULT_MAX_QUEUE_WAITERS,
        warm_on_start=True,
    ):
        self.headless = headless
        self.user_data_dir = Path(user_data_dir)
        self.timeout_ms = timeout_ms
        self.retries = retries
        self.retry_delay = retry_delay
        self.start_minimized = start_minimized
        self.debug = debug
        self.response_timeout_sec = min(DEFAULT_RESPONSE_TIMEOUT_SEC, timeout_ms / 1000.0)
        self.stabilize_delay = stabilize_delay
        self.warm_pages = warm_pages
        self.max_pages = max_pages
        self.max_turns_per_page = max_turns_per_page
        self.max_queue_waiters = max_queue_waiters
        self._warm_on_start = warm_on_start
        self._playwright = None
        self._context = None
        self._pool = []
        self._queue = asyncio.Queue()
        self._pool_lock = asyncio.Lock()
        self._waiters = 0
        self._logger = Logger()

    async def start(self):
        self._playwright = await async_playwright().start()
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        if not self.headless and self.start_minimized:
            args.append("--start-minimized")
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            headless=self.headless,
            args=args,
            viewport={"width": 1280, "height": 900},
        )
        self._context.set_default_timeout(self.timeout_ms)
        if self.warm_pages > self.max_pages:
            self.warm_pages = self.max_pages
        if self._warm_on_start:
            await self.warm_pool()
        self._logger.write("INFO", "AsyncChatGPTClient started")

    async def close(self):
        for state in list(self._pool):
            try:
                await state.page.close()
            except Exception:
                pass
        self._pool.clear()
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        self._logger.write("INFO", "AsyncChatGPTClient stopped")

    async def warm_pool(self):
        for _ in range(max(0, self.warm_pages)):
            state = await self._open_page_state()
            if state:
                self._pool.append(state)
                await self._queue.put(state)
        self._logger.write("INFO", f"Warm pages ready: {len(self._pool)}")

    async def open_config_page(self, html):
        page = None
        try:
            pages = self._context.pages
            if pages:
                candidate = pages[0]
                if candidate.url in ("about:blank", ""):
                    page = candidate
        except Exception:
            page = None
        if page is None:
            page = await self._context.new_page()
        try:
            await page.set_content(html, wait_until="domcontentloaded")
        except Exception:
            try:
                await page.close()
            except Exception:
                pass

    async def get_pool_status(self):
        async with self._pool_lock:
            total = len(self._pool)
            busy = len([s for s in self._pool if s.busy])
            idle = total - busy
            return {
                "total": total,
                "busy": busy,
                "idle": idle,
                "max_pages": self.max_pages,
                "warm_pages": self.warm_pages,
                "max_turns_per_page": self.max_turns_per_page,
                "max_queue_waiters": self.max_queue_waiters,
                "waiters": self._waiters,
            }

    async def ask(self, prompt):
        last_error = None
        for attempt in range(self.retries + 1):
            state = await self._acquire_page()
            page = state.page
            try:
                self._logger.write("INFO", f"Request start attempt={attempt}")
                if page.url != CHATGPT_URL:
                    await page.goto(CHATGPT_URL, wait_until="domcontentloaded")
                if self.debug:
                    await self._debug_dump(page, f"loaded_attempt_{attempt}")
                await self._wait_for_ready(page)
                if self.stabilize_delay:
                    await asyncio.sleep(self.stabilize_delay)
                if self.debug:
                    await self._debug_dump(page, f"ready_attempt_{attempt}")
                await self._send_prompt(page, prompt)
                if self.stabilize_delay:
                    await asyncio.sleep(self.stabilize_delay)
                if self.debug:
                    await self._debug_dump(page, f"sent_attempt_{attempt}")
                answer = await self._read_last_assistant_message(page)
                state.turns += 1
                await self._release_page(state, recycle=state.turns >= self.max_turns_per_page)
                self._logger.write("INFO", "Request completed")
                return answer.strip()
            except Exception as exc:
                last_error = exc
                self._logger.write("ERROR", f"Request failed: {exc}")
                if self.debug:
                    await self._debug_dump(page, f"error_attempt_{attempt}")
                await self._release_page(state, recycle=True)
                if attempt < self.retries:
                    await asyncio.sleep(self.retry_delay)
                continue
        raise RuntimeError(f"请求失败，已重试 {self.retries} 次: {last_error}")

    async def _open_page_state(self):
        page = await self._context.new_page()
        try:
            await page.goto(CHATGPT_URL, wait_until="domcontentloaded")
            await self._wait_for_ready(page)
            return PageState(page)
        except Exception:
            try:
                await page.close()
            except Exception:
                pass
            return None

    async def _page_is_usable(self, page):
        try:
            if page.is_closed():
                return False
        except Exception:
            return False
        if await self._is_generating(page):
            return False
        selectors = [
            "#prompt-textarea",
            "textarea[name='prompt-textarea']",
            "textarea.wcDTda_fallbackTextarea",
            "div[contenteditable='true']",
            "[role='textbox']",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _acquire_page(self):
        async with self._pool_lock:
            if not self._queue.empty():
                state = self._queue.get_nowait()
                state.busy = True
                return state
            if len(self._pool) < self.max_pages:
                state = await self._open_page_state()
                if state:
                    state.busy = True
                    self._pool.append(state)
                    return state
            if self._waiters >= self.max_queue_waiters:
                raise RuntimeError("等待队列已满，请稍后重试。")
            self._waiters += 1
        try:
            state = await self._queue.get()
            state.busy = True
            return state
        finally:
            async with self._pool_lock:
                self._waiters -= 1

    async def _release_page(self, state, recycle=False):
        async with self._pool_lock:
            if recycle or not await self._page_is_usable(state.page):
                try:
                    await state.page.close()
                except Exception:
                    pass
                if state in self._pool:
                    self._pool.remove(state)
                replacement = await self._open_page_state()
                if replacement:
                    self._pool.append(replacement)
                    await self._queue.put(replacement)
            else:
                state.busy = False
                await self._queue.put(state)

    async def _wait_for_ready(self, page):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except AsyncPlaywrightTimeoutError:
            pass
        selectors = [
            "#prompt-textarea",
            "textarea[name='prompt-textarea']",
            "textarea.wcDTda_fallbackTextarea",
            "div[contenteditable='true']",
            "[role='textbox']",
        ]
        for sel in selectors:
            try:
                await page.wait_for_selector(sel, state="visible", timeout=5_000)
                return
            except AsyncPlaywrightTimeoutError:
                continue
        raise RuntimeError("无法定位输入框，请确认已登录且页面可正常加载。")

    async def _fill_prompt(self, page, prompt):
        try:
            textbox = page.get_by_role("textbox", name="询问任何问题")
            if await textbox.count() > 0 and await textbox.first.is_visible():
                await textbox.first.click()
                try:
                    await textbox.first.fill(prompt)
                except Exception:
                    await textbox.first.type(prompt, delay=20)
                return True
        except Exception:
            pass
        selectors = [
            "div#prompt-textarea",
            "div[contenteditable='true']",
            "textarea[name='prompt-textarea']",
            "textarea.wcDTda_fallbackTextarea",
            "[role='textbox']",
        ]
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0 or not await loc.is_visible():
                    continue
                await loc.click()
                try:
                    await loc.fill(prompt)
                except Exception:
                    try:
                        await loc.type(prompt, delay=20)
                    except Exception:
                        await page.keyboard.type(prompt, delay=20)
                return True
            except Exception:
                continue
        return False

    async def _click_send(self, page):
        selectors = [
            "button[data-testid='send-button']",
            "button[aria-label='Send message']",
            "button[aria-label='Send prompt']",
            "button[aria-label='Send']",
            "button[type='submit']",
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0:
                    continue
                try:
                    await page.wait_for_function(
                        "(selector) => { const b = document.querySelector(selector); return b && !b.disabled; }",
                        arg=sel,
                        timeout=5_000,
                    )
                except AsyncPlaywrightTimeoutError:
                    pass
                await btn.click()
                return True
            except Exception:
                continue
        return False

    async def _count_messages(self, page, selector):
        best = 0
        for frame in page.frames:
            try:
                count = await frame.evaluate("(sel) => document.querySelectorAll(sel).length", selector)
            except Exception:
                count = 0
            if count > best:
                best = count
        return best

    async def _wait_for_message_increase(self, page, selector, start_count, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            count = await self._count_messages(page, selector)
            if count > start_count:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _send_prompt_via_dom(self, page, prompt, start_count):
        try:
            sent = await page.evaluate(
                """(value) => {
                    function isVisible(el) {
                        const r = el.getBoundingClientRect();
                        if (!r || r.width <= 0 || r.height <= 0) return false;
                        const style = window.getComputedStyle(el);
                        return style.display !== 'none' && style.visibility !== 'hidden';
                    }
                    const inputs = Array.from(
                        document.querySelectorAll('textarea, [contenteditable=\"true\"], [role=\"textbox\"]')
                    ).filter(isVisible);
                    if (!inputs.length) return false;
                    inputs.sort((a, b) => {
                        const ra = a.getBoundingClientRect();
                        const rb = b.getBoundingClientRect();
                        return (rb.width * rb.height) - (ra.width * ra.height);
                    });
                    const target = inputs[0];
                    if (!target) return false;
                    target.focus();
                    if (target.tagName === 'TEXTAREA') {
                        const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
                        setter.call(target, value);
                        target.dispatchEvent(new Event('input', { bubbles: true }));
                        target.dispatchEvent(new Event('change', { bubbles: true }));
                    } else {
                        target.textContent = value;
                        target.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                    const btn =
                        document.querySelector("button[data-testid='send-button']") ||
                        document.querySelector("button[aria-label='Send message']") ||
                        document.querySelector("button[aria-label='Send prompt']") ||
                        document.querySelector("button[aria-label='Send']") ||
                        document.querySelector("button[type='submit']");
                    if (btn && !btn.disabled) {
                        btn.click();
                        return true;
                    }
                    return false;
                }""",
                prompt,
            )
        except Exception:
            return False
        if not sent:
            return False
        return await self._wait_for_message_increase(
            page,
            "div[data-message-author-role='user']",
            start_count,
            timeout=8,
        )

    async def _send_prompt(self, page, prompt):
        user_selector = "div[data-message-author-role='user']"
        start_count = await self._count_messages(page, user_selector)
        await self._fill_prompt(page, prompt)
        if not await self._click_send(page):
            try:
                await page.keyboard.press("Enter")
            except Exception:
                pass
        if await self._wait_for_message_increase(page, user_selector, start_count, timeout=8):
            return
        if await self._send_prompt_via_dom(page, prompt, start_count):
            return
        raise RuntimeError("输入未发送成功，请检查输入框或按钮状态。")

    async def _get_assistant_texts(self, page):
        best = []
        for frame in page.frames:
            try:
                texts = await frame.evaluate(
                    """() => {
                        const nodes = Array.from(
                            document.querySelectorAll("div[data-message-author-role='assistant']")
                        );
                        return nodes.map(n => (n.innerText || '').trim()).filter(Boolean);
                    }"""
                )
            except Exception:
                texts = []
            if len(texts) > len(best):
                best = texts
        return best

    async def _is_generating(self, page):
        try:
            return await page.evaluate(
                """() => {
                    const btn = document.querySelector("button[data-testid='stop-button']");
                    return !!(btn && btn.offsetParent !== null);
                }"""
            )
        except Exception:
            return False

    async def _detect_page_error(self, page):
        keywords = [
            "something went wrong",
            "please try again",
            "try again later",
            "we're having trouble",
            "there was a problem",
            "too many requests",
            "network error",
            "服务异常",
            "网络错误",
            "请求失败",
            "发生错误",
            "出现错误",
            "出了问题",
            "请重试",
        ]
        strict_keywords = [
            "something went wrong",
            "we're having trouble",
            "there was a problem",
            "too many requests",
            "network error",
            "服务异常",
            "网络错误",
            "请求失败",
            "发生错误",
            "出现错误",
            "出了问题",
            "请重试",
        ]
        try:
            return await page.evaluate(
                """(args) => {
                    const keys = args.keys || [];
                    const strictKeys = args.strictKeys || [];
                    const selectors = [
                        '[role="alert"]',
                        '[data-testid*="error"]',
                        '[data-testid*="retry"]',
                        '[aria-live="assertive"]',
                        '.error',
                        '.error-message'
                    ];
                    const nodes = [];
                    selectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(n => nodes.push(n));
                    });
                    const text = nodes.map(n => (n.innerText || '').trim()).join(' ').toLowerCase();
                    const body = (document.body && document.body.innerText || '').toLowerCase();
                    const has = (hay, list) => list.some(k => hay.includes(k));
                    if (text && has(text, keys)) return true;
                    return has(body, strictKeys);
                }""",
                arg={"keys": [k.lower() for k in keywords], "strictKeys": [k.lower() for k in strict_keywords]},
            )
        except Exception:
            return False

    async def _read_last_assistant_message(self, page):
        start_texts = await self._get_assistant_texts(page)
        start_last = start_texts[-1] if start_texts else ""
        deadline = time.time() + self.response_timeout_sec
        if await self._detect_page_error(page):
            raise RuntimeError("页面出现错误提示，请重试。")
        while time.time() < deadline:
            if await self._detect_page_error(page):
                raise RuntimeError("页面出现错误提示，请重试。")
            texts = await self._get_assistant_texts(page)
            if texts:
                last = texts[-1].strip()
                if last and (len(texts) > len(start_texts) or last != start_last):
                    if not await self._is_generating(page):
                        return last
            await asyncio.sleep(0.5)
        raise RuntimeError("等待回复超时，请重试。")

    async def _debug_dump(self, page, label):
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        debug_path = Path(DEBUG_DIR)
        debug_path.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_path / f"{timestamp}_{label}.png"
        json_path = debug_path / f"{timestamp}_{label}.json"
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass
        try:
            data = {
                "url": page.url,
                "frames": [],
            }
            for frame in page.frames:
                try:
                    frame_info = await frame.evaluate(
                        """() => {
                            function visible(el) {
                                const r = el.getBoundingClientRect();
                                return r.width > 0 && r.height > 0;
                            }
                            const nodes = Array.from(
                                document.querySelectorAll('textarea, [contenteditable=\"true\"], [role=\"textbox\"]')
                            );
                            return nodes.map(el => ({
                                tag: el.tagName.toLowerCase(),
                                class: el.className || '',
                                id: el.id || '',
                                name: el.getAttribute('name') || '',
                                placeholder: el.getAttribute('placeholder') || '',
                                aria_label: el.getAttribute('aria-label') || '',
                                role: el.getAttribute('role') || '',
                                contenteditable: el.getAttribute('contenteditable') || '',
                                display: getComputedStyle(el).display || '',
                                disabled: !!el.disabled,
                                readonly: !!el.readOnly,
                                bbox: el.getBoundingClientRect().toJSON(),
                                visible: visible(el),
                            }));
                        }"""
                    )
                except Exception:
                    frame_info = []
                data["frames"].append(
                    {
                        "url": frame.url,
                        "inputs": frame_info,
                    }
                )
            json_path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
        except Exception:
            pass


class AsyncEngine:
    def __init__(self, client_kwargs):
        self._client_kwargs = client_kwargs
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._client = None

    def start(self):
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._client = AsyncChatGPTClient(**self._client_kwargs)
        self._loop.run_until_complete(self._client.start())
        self._ready.set()
        self._loop.run_forever()
        self._loop.run_until_complete(self._client.close())
        self._loop.close()

    def stop(self):
        if not self._loop:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=10)

    def open_config_page(self, html):
        fut = asyncio.run_coroutine_threadsafe(self._client.open_config_page(html), self._loop)
        return fut.result()

    def warm_pool(self):
        fut = asyncio.run_coroutine_threadsafe(self._client.warm_pool(), self._loop)
        return fut.result()

    def run_prompt(self, prompt):
        fut = asyncio.run_coroutine_threadsafe(self._client.ask(prompt), self._loop)
        return fut.result()

    def get_pool_status(self):
        fut = asyncio.run_coroutine_threadsafe(self._client.get_pool_status(), self._loop)
        return fut.result()


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _make_handler(engine_provider):
    class Handler(BaseHTTPRequestHandler):
        def _get_engine(self, start_if_missing=True):
            return engine_provider(start_if_missing)

        def _send_json(self, status, payload):
            data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json_body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return None
            except UnicodeDecodeError:
                return None

        def _read_form_body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return parse_qs(raw.decode("utf-8"))
            except UnicodeDecodeError:
                return {}

        def _base_url(self):
            host = self.headers.get("Host")
            if host:
                return f"http://{host}/"
            return "/"

        def do_GET(self):
            if self.path in ("/config", "/"):
                try:
                    templates = _get_templates()
                    settings = _get_settings()
                    logs = _read_log_tail(100)
                    page = _render_config_page(templates, settings, logs, base_url=self._base_url())
                except Exception as exc:
                    page = _render_config_page({}, {}, [], error=str(exc), base_url=self._base_url())
                data = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if self.path == "/config.json":
                try:
                    templates = _get_templates()
                    settings = _get_settings()
                except Exception as exc:
                    self._send_json(500, {"error": "internal_error", "detail": str(exc)})
                    return
                self._send_json(200, {"templates": templates, "settings": settings})
                return

            if self.path == "/health":
                engine = engine_provider(False)
                if engine is None:
                    self._send_json(
                        200,
                        {
                            "pool": {
                                "total": 0,
                                "busy": 0,
                                "idle": 0,
                                "max_pages": 0,
                                "warm_pages": 0,
                                "max_turns_per_page": 0,
                                "max_queue_waiters": 0,
                                "waiters": 0,
                            }
                        },
                    )
                else:
                    self._send_json(200, {"pool": engine.get_pool_status()})
                return

            if self.path.startswith("/logs"):
                query = self.path.split("?", 1)[-1] if "?" in self.path else ""
                params = parse_qs(query)
                try:
                    offset = int((params.get("offset") or ["0"])[0])
                    limit = int((params.get("limit") or ["100"])[0])
                except ValueError:
                    self._send_json(400, {"error": "invalid_params"})
                    return
                lines = _read_log_page(offset, limit)
                self._send_json(200, {"lines": lines})
                return

            self._send_json(404, {"error": "not_found"})

        def do_POST(self):
            if self.path == "/config/save":
                form = self._read_form_body()
                translate_template = (form.get("translate_template") or [""])[0].strip()
                brand_template = (form.get("brand_template") or [""])[0].strip()
                warm_pages = (form.get("warm_pages") or [""])[0].strip()
                max_pages = (form.get("max_pages") or [""])[0].strip()
                max_turns_per_page = (form.get("max_turns_per_page") or [""])[0].strip()
                max_queue_waiters = (form.get("max_queue_waiters") or [""])[0].strip()
                qa_mode = (form.get("qa_mode") or [""])[0].strip()
                api_base_url = (form.get("api_base_url") or [""])[0].strip()
                api_key = (form.get("api_key") or [""])[0].strip()
                api_model = (form.get("api_model") or [""])[0].strip()
                api_timeout_sec = (form.get("api_timeout_sec") or [""])[0].strip()
                if qa_mode not in ("api", "browser"):
                    qa_mode = DEFAULT_QA_MODE
                api_base_url = api_base_url or DEFAULT_API_BASE_URL
                api_model = api_model or DEFAULT_QA_MODEL
                try:
                    warm_pages_val = int(warm_pages or DEFAULT_WARM_PAGES)
                except ValueError:
                    warm_pages_val = DEFAULT_WARM_PAGES
                try:
                    max_pages_val = int(max_pages or DEFAULT_MAX_PAGES)
                except ValueError:
                    max_pages_val = DEFAULT_MAX_PAGES
                try:
                    max_turns_val = int(max_turns_per_page or DEFAULT_MAX_TURNS_PER_PAGE)
                except ValueError:
                    max_turns_val = DEFAULT_MAX_TURNS_PER_PAGE
                try:
                    max_waiters_val = int(max_queue_waiters or DEFAULT_MAX_QUEUE_WAITERS)
                except ValueError:
                    max_waiters_val = DEFAULT_MAX_QUEUE_WAITERS
                try:
                    api_timeout_val = int(api_timeout_sec or DEFAULT_API_TIMEOUT_SEC)
                except ValueError:
                    api_timeout_val = DEFAULT_API_TIMEOUT_SEC
                if not translate_template or not brand_template:
                    settings = {
                        "warm_pages": warm_pages_val,
                        "max_pages": max_pages_val,
                        "max_turns_per_page": max_turns_val,
                        "max_queue_waiters": max_waiters_val,
                        "qa_mode": qa_mode,
                        "api_base_url": api_base_url,
                        "api_key": api_key,
                        "api_model": api_model,
                        "api_timeout_sec": api_timeout_val,
                    }
                    page = _render_config_page(
                        {"translate": translate_template, "brand": brand_template},
                        settings,
                        _read_log_tail(100),
                        error="模板不能为空",
                        base_url=self._base_url(),
                    )
                else:
                    try:
                        _save_templates(translate_template, brand_template)
                        _save_settings(
                            warm_pages_val,
                            max_pages_val,
                            max_turns_val,
                            max_waiters_val,
                            qa_mode,
                            api_base_url,
                            api_key,
                            api_model,
                            api_timeout_val,
                        )
                        page = _render_config_page(
                            {"translate": translate_template, "brand": brand_template},
                            _get_settings(),
                            _read_log_tail(100),
                            message="保存成功",
                            base_url=self._base_url(),
                        )
                    except Exception as exc:
                        page = _render_config_page(
                            {"translate": translate_template, "brand": brand_template},
                            _get_settings(),
                            _read_log_tail(100),
                            error=str(exc),
                            base_url=self._base_url(),
                        )
                data = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if self.path not in ("/translate", "/brand", "/qa", PRODUCT_ENDPOINT):
                self._send_json(404, {"error": "not_found"})
                return

            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "invalid_json"})
                return

            if self.path == PRODUCT_ENDPOINT:
                title = (data.get("title") or "").strip()
                description = (data.get("description") or data.get("descption") or "").strip()
                if not title and not description:
                    self._send_json(400, {"error": "missing_title_or_description"})
                    return
            else:
                text = (data.get("text") or "").strip()
                if not text:
                    self._send_json(400, {"error": "missing_text"})
                    return

            try:
                settings = _get_settings()
                if self.path == PRODUCT_ENDPOINT:
                    result = _call_product_api(title, description, settings)
                elif settings.get("qa_mode") == "api":
                    if self.path == "/translate":
                        templates = _get_templates()
                        prompt = _apply_template(templates.get("translate", DEFAULT_TRANSLATE_TEMPLATE), text)
                        result = _call_chat_api(prompt, settings)
                    elif self.path == "/brand":
                        templates = _get_templates()
                        prompt = _apply_template(templates.get("brand", DEFAULT_BRAND_TEMPLATE), text)
                        result = _call_chat_api(prompt, settings)
                    else:
                        result = _call_chat_api(text, settings)
                else:
                    if self.path == "/translate":
                        templates = _get_templates()
                        prompt = _apply_template(templates.get("translate", DEFAULT_TRANSLATE_TEMPLATE), text)
                    elif self.path == "/brand":
                        templates = _get_templates()
                        prompt = _apply_template(templates.get("brand", DEFAULT_BRAND_TEMPLATE), text)
                    else:
                        prompt = text
                    engine = self._get_engine(True)
                    if engine is None:
                        raise RuntimeError("浏览器引擎未初始化，请重试。")
                    result = engine.run_prompt(prompt)
            except Exception as exc:
                self._send_json(500, {"error": "internal_error", "detail": str(exc)})
                return

            if self.path == PRODUCT_ENDPOINT:
                self._send_json(200, result)
            else:
                self._send_json(200, {"result": result})

        def log_message(self, format, *args):
            return

    return Handler


def build_run_parser():
    parser = argparse.ArgumentParser(description="ChatGPT automation for translation and brand infringement check.")
    parser.add_argument(
        "mode",
        choices=["translate", "brand", "qa"],
        help="translate: 只翻译; brand: 品牌侵权判断; qa: 问答",
    )
    parser.add_argument("text", nargs="+", help="要输入的内容")
    parser.add_argument("--headless", action="store_true", help="无头模式运行浏览器")
    parser.add_argument("--start-minimized", action="store_true", help="有头模式时最小化启动")
    parser.add_argument("--profile", default=".chatgpt_profile", help="浏览器用户数据目录")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS, help="超时时间(毫秒)")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="失败重试次数")
    parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY, help="重试间隔(秒)")
    parser.add_argument("--debug", action="store_true", help="输出调试信息与截图到 debug/ 目录")
    parser.add_argument("--warm-pages", type=int, help="预热页签数")
    parser.add_argument("--max-pages", type=int, help="最大页签数")
    parser.add_argument("--max-turns-per-page", type=int, help="单页最大对话次数")
    parser.add_argument("--max-queue-waiters", type=int, help="最大等待队列数")
    return parser


def build_serve_parser():
    parser = argparse.ArgumentParser(description="ChatGPT automation HTTP server.")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--headless", action="store_true", help="无头模式运行浏览器")
    parser.add_argument("--headed", action="store_true", help="有头模式运行浏览器")
    parser.add_argument("--start-minimized", action="store_true", help="有头模式时最小化启动")
    parser.add_argument("--profile", default=".chatgpt_profile", help="浏览器用户数据目录")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS, help="超时时间(毫秒)")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="失败重试次数")
    parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY, help="重试间隔(秒)")
    parser.add_argument("--debug", action="store_true", help="输出调试信息与截图到 debug/ 目录")
    parser.add_argument("--warm-pages", type=int, help="预热页签数")
    parser.add_argument("--max-pages", type=int, help="最大页签数")
    parser.add_argument("--max-turns-per-page", type=int, help="单页最大对话次数")
    parser.add_argument("--max-queue-waiters", type=int, help="最大等待队列数")
    parser.set_defaults(headless=True)
    return parser


def _run_cli(args):
    text = " ".join(args.text).strip()
    if not text:
        print("请输入内容。", file=sys.stderr)
        return 2
    settings = _get_settings()
    warm_pages = args.warm_pages if args.warm_pages is not None else settings["warm_pages"]
    max_pages = args.max_pages if args.max_pages is not None else settings["max_pages"]
    max_turns_per_page = (
        args.max_turns_per_page if args.max_turns_per_page is not None else settings["max_turns_per_page"]
    )
    max_queue_waiters = (
        args.max_queue_waiters if args.max_queue_waiters is not None else settings["max_queue_waiters"]
    )

    if settings.get("qa_mode") == "api":
        if args.mode == "translate":
            templates = _get_templates()
            prompt = _apply_template(templates.get("translate", DEFAULT_TRANSLATE_TEMPLATE), text)
            result = _call_chat_api(prompt, settings)
        elif args.mode == "brand":
            templates = _get_templates()
            prompt = _apply_template(templates.get("brand", DEFAULT_BRAND_TEMPLATE), text)
            result = _call_chat_api(prompt, settings)
        else:
            result = _call_chat_api(text, settings)
    else:
        with ChatGPTClient(
            headless=args.headless,
            user_data_dir=args.profile,
            timeout_ms=args.timeout,
            retries=args.retries,
            retry_delay=args.retry_delay,
            start_minimized=args.start_minimized,
            debug=args.debug,
            warm_pages=warm_pages,
            max_pages=max_pages,
            max_turns_per_page=max_turns_per_page,
            max_queue_waiters=max_queue_waiters,
        ) as client:
            if args.mode == "translate":
                result = client.translate(text)
            elif args.mode == "brand":
                result = client.brand_infringement(text)
            else:
                result = client.ask(text)

    print(result)
    return 0


def _run_server(args):
    if args.headed:
        args.headless = False
    settings = _get_settings()
    warm_pages = args.warm_pages if args.warm_pages is not None else settings["warm_pages"]
    max_pages = args.max_pages if args.max_pages is not None else settings["max_pages"]
    max_turns_per_page = (
        args.max_turns_per_page if args.max_turns_per_page is not None else settings["max_turns_per_page"]
    )
    max_queue_waiters = (
        args.max_queue_waiters if args.max_queue_waiters is not None else settings["max_queue_waiters"]
    )
    client_kwargs = {
        "headless": args.headless,
        "user_data_dir": args.profile,
        "timeout_ms": args.timeout,
        "retries": args.retries,
        "retry_delay": args.retry_delay,
        "start_minimized": args.start_minimized,
        "debug": args.debug,
        "warm_pages": warm_pages,
        "max_pages": max_pages,
        "max_turns_per_page": max_turns_per_page,
        "max_queue_waiters": max_queue_waiters,
        "warm_on_start": False,
    }
    engine_ref = {"engine": None}

    def _start_engine():
        if engine_ref["engine"] is not None:
            return engine_ref["engine"]
        engine = AsyncEngine(client_kwargs)
        engine.start()
        engine.warm_pool()
        engine_ref["engine"] = engine
        return engine

    def _engine_provider(start_if_missing=True):
        if engine_ref["engine"] is not None:
            return engine_ref["engine"]
        if not start_if_missing:
            return None
        return _start_engine()

    server = _ThreadingHTTPServer((args.host, args.port), _make_handler(_engine_provider))
    print(f"HTTP server listening on http://{args.host}:{args.port}")
    if not args.headless:
        base_url = f"http://{args.host}:{args.port}/config"
        try:
            webbrowser.open(base_url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if engine_ref["engine"] is not None:
            engine_ref["engine"].stop()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        args = build_serve_parser().parse_args(sys.argv[2:])
        return _run_server(args)

    args = build_run_parser().parse_args()
    return _run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
