"""把爆发提醒打进用户自己的 Muse 对话（浏览器输入框）。

工作原理：扫描器跑在用户自己的电脑上，Chrome 里开着 Muse 对话页。
本模块找到那个标签页，用一段 JS 在页面里找到输入框、填进提醒文本、
点发送（或回车）。消息是以用户的名义发出的——首行有结构化标记
（见 format_muse_chat_message），一眼能认出是扫描器打的，不是
用户自己敲的。

两个平台，一段 JS：
- macOS：AppleScript（系统自带，不用装东西、不用重启 Chrome）。
  首次运行 macOS 会弹权限：系统设置 → 隐私与安全性 → 自动化，
  允许扫描器（终端 / Python）控制 Google Chrome。
  另外 Chrome 菜单栏 → View（视图）→ Developer（开发者）→
  Allow JavaScript from Apple Events 也要勾选（当前 Chrome 默认
  关闭，没开会报 -1700；与自动化权限是两回事，两处都要开）。
- Windows：CDP（Chrome 自带的调试接口）。Chrome 要先完全退出，
  再加 --remote-debugging-port=9222 重开；还需要 websocket-client
 （在 requirements.txt 里，安装脚本会一起装；macOS 不需要它，
  这里是懒加载，缺了也不影响 import）。

JS 那段是尽力而为（best-effort）：Muse 网页的真实 DOM 没法在
这台没登录的机器上看，只能按常见输入框形态写多层兜底——
textarea → contenteditable → input[type=text] → role=textbox，
填值用原生 setter + input 事件（React 受控输入框才能认到），
发送先找发送按钮（aria-label/title 含 send/发送、type=submit、
输入框旁边的按钮），找不到才发回车键事件。找不到输入框时
什么都不做、返回 ok:false，绝不乱打字。

失败是常态、不是异常：标签页没开、Chrome 没开、权限没给、
端口没开，都会返回 ok:false 并记一条 INFO/警告，扫描轮次
照常完成。见 docs/MUSE_CHAT.md。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 结构化标记的版本（见 docs/MUSE_CHAT.md）：格式以后改了就升 v2，
# 老版本靠 scanner-alert 这个 token 照样能认出来。
MARKER_VERSION = "v1"

DEFAULT_URL_MATCH = "muse.ai"
DEFAULT_CDP_PORT = 9222
# 两条提醒之间隔多久（别连打两条把输入框打架了）。
MESSAGE_DELAY_SECONDS = 0.5

# 标记的正则：只认首行、只认完整形状。中文正文里就算出现
# “扫描器提醒”四个字，没有这个标记也不算扫描器消息。
_MARKER_RE = re.compile(
    r"^【扫描器提醒｜scanner-alert v(\d+)｜event=([^｜]*)｜rule=([^｜]*)｜ts=([^｜]*)】$"
)


def _format_ts(value: str | None) -> str:
    """把 detected_at（或 None）规范化成 UTC ISO Z 形式。

    burst 的 detected_at 一般是 "2026-11-15T13:00:00+00:00"，
    标记里写成 "2026-11-15T13:00:00Z"。缺失或解析失败时用当前时间。
    """
    dt = None
    if value:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_muse_chat_message(burst: dict) -> str:
    """一处生成发往 Muse 对话的完整消息：标记行 + 提醒正文。

    标记是唯一的生成入口，格式只在这里定：
    【扫描器提醒｜scanner-alert v1｜event=<event_id>｜rule=<规则>｜ts=<UTC ISO>】
    event_id / rule 取自 burst（rule 是 reasons 的第一条，没有时写
    unknown），ts 取 burst 的 detected_at（UTC ISO Z 形式）。
    正文与 Telegram 用同一份 format_burst_message。
    """
    from trend_radar.alerts import format_burst_message

    event_id = burst.get("event_id", "") or ""
    reasons = burst.get("reasons") or []
    rule = reasons[0] if reasons else "unknown"
    ts = _format_ts(burst.get("detected_at"))
    marker = (
        f"【扫描器提醒｜scanner-alert {MARKER_VERSION}"
        f"｜event={event_id}｜rule={rule}｜ts={ts}】"
    )
    return marker + "\n" + format_burst_message(burst)


def parse_scanner_marker(text: str | None) -> dict | None:
    """解析一段消息首行的扫描器标记，返回字段，没标记返回 None。

    认标记只看首行是否完整匹配上面那个形状，不看中文正文——
    用户自己敲的“扫描器提醒”四个字不会误判。version 是 v 后面的
    数字（"v1" 这种形式）；以后格式升版（v2…）这里照样解析，
    调用方按 version 决定怎么处理。
    """
    if not text:
        return None
    lines = text.splitlines()
    first = lines[0].strip() if lines else ""
    match = _MARKER_RE.match(first)
    if not match:
        return None
    return {
        "version": f"v{match.group(1)}",
        "event_id": match.group(2),
        "rule": match.group(3),
        "ts": match.group(4),
    }


def is_scanner_alert(text: str | None) -> bool:
    """这段消息是不是扫描器打进来的（首行有结构化标记）。"""
    return parse_scanner_marker(text) is not None


# ---------------------------------------------------------------------------
# 共用 JS：两平台同一段。返回 JSON 字符串（不是对象），因为 AppleScript
# 的 execute javascript 只能可靠地带回字符串，CDP 那边也按字符串解析。
# ---------------------------------------------------------------------------
JS_PAYLOAD_TEMPLATE = r"""(function (message) {
  function findInput() {
    var selectors = ['textarea', '[contenteditable="true"]', 'input[type="text"]', '[role="textbox"]'];
    var i, j, el, els;
    for (i = 0; i < selectors.length; i++) {
      els = document.querySelectorAll(selectors[i]);
      for (j = 0; j < els.length; j++) {
        el = els[j];
        if (el.offsetParent !== null || el === document.activeElement) return el;
      }
    }
    for (i = 0; i < selectors.length; i++) {
      el = document.querySelector(selectors[i]);
      if (el) return el;
    }
    return null;
  }
  function setValue(el, value) {
    el.focus();
    var tag = el.tagName;
    if (tag === 'TEXTAREA' || tag === 'INPUT') {
      var proto = tag === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      setter.call(el, value);
    } else {
      el.textContent = value;
    }
    el.dispatchEvent(new Event('input', {bubbles: true}));
  }
  function findSendButton(input) {
    var i, b, label;
    var btns = document.querySelectorAll('button');
    for (i = 0; i < btns.length; i++) {
      b = btns[i];
      label = ((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('title') || '') + ' ' + (b.textContent || '')).toLowerCase();
      if (label.indexOf('send') !== -1 || label.indexOf('发送') !== -1) return b;
    }
    var submit = document.querySelector('button[type="submit"], input[type="submit"]');
    if (submit) return submit;
    if (input && input.parentElement) {
      var sib = input.parentElement.querySelector('button');
      if (sib) return sib;
    }
    return null;
  }
  var input = findInput();
  if (!input) return JSON.stringify({ok: false, reason: 'no_input'});
  try {
    setValue(input, message);
  } catch (e) {
    return JSON.stringify({ok: false, reason: 'set_value_failed', error: String(e)});
  }
  var btn = findSendButton(input);
  if (btn) {
    btn.click();
    return JSON.stringify({ok: true, input: input.tagName.toLowerCase(), submitted: 'button'});
  }
  var opts = {key: 'Enter', code: 'Enter', bubbles: true, cancelable: true};
  input.dispatchEvent(new KeyboardEvent('keydown', opts));
  input.dispatchEvent(new KeyboardEvent('keypress', opts));
  input.dispatchEvent(new KeyboardEvent('keyup', opts));
  return JSON.stringify({ok: true, input: input.tagName.toLowerCase(), submitted: 'enter'});
})(__MESSAGE_JSON__)"""


def build_js_payload(text: str) -> str:
    """把消息嵌进共用 JS（JSON 编码，不用手拼引号，消息里的引号、
    换行、中文都安全）。"""
    return JS_PAYLOAD_TEMPLATE.replace("__MESSAGE_JSON__", json.dumps(text, ensure_ascii=False))


# ---------------------------------------------------------------------------
# macOS：AppleScript。JS 写临时文件、AppleScript 读文件，不手拼嵌套引号。
# ---------------------------------------------------------------------------
APPLESCRIPT_TEMPLATE = """on run argv
  set jsPath to item 1 of argv
  set urlMatch to item 2 of argv
  set jsCode to read POSIX file jsPath as «class utf8»
  if application "Google Chrome" is not running then
    return "NO_CHROME"
  end if
  tell application "Google Chrome"
    -- 注意：必须用“tab ti of window wi”这种显式序号定位标签页。
    -- 把循环变量直接传给 execute javascript 会报 -1700
    -- （Can't make application "Google Chrome" into type
    -- specifier），2026-11-15 实测。
    repeat with wi from 1 to count of windows
      repeat with ti from 1 to count of tabs of window wi
        if (URL of tab ti of window wi) contains urlMatch then
          return execute javascript jsCode in tab ti of window wi
        end if
      end repeat
    end repeat
  end tell
  return "NO_TAB"
end run
"""


def _looks_like_permission_denied(stderr: str) -> bool:
    """osascript 报错里是不是 macOS 自动化权限没给。"""
    lowered = (stderr or "").lower()
    return any(
        token in lowered
        for token in (
            "not allowed",
            "not permitted",
            "not authorized",
            "denied",
            "1743",
            "-10000",
        )
    )


def _looks_like_js_blocked(stderr: str) -> bool:
    """osascript 报错里是不是 Chrome 没开 JavaScript from Apple Events。

    当前 Chrome 默认关闭这个开关时，execute javascript 会报 -1700
    （Can't make application "Google Chrome" into type specifier），
    与 macOS 自动化权限（automation_denied）是两回事。
    """
    lowered = (stderr or "").lower()
    return any(
        token in lowered
        for token in (
            "-1700",
            "type specifier",
            "allow javascript",
        )
    )


def _send_via_applescript(text: str, url_match: str) -> dict:
    """macOS：osascript 找 Chrome 里含 url_match 的标签页，执行 JS。"""
    js = build_js_payload(text)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            js_path = Path(tmp) / "muse_chat_payload.js"
            scpt_path = Path(tmp) / "muse_chat_send.scpt"
            js_path.write_text(js, encoding="utf-8")
            scpt_path.write_text(APPLESCRIPT_TEMPLATE, encoding="utf-8")
            proc = subprocess.run(
                ["osascript", str(scpt_path), str(js_path), url_match],
                capture_output=True,
                text=True,
                timeout=30,
            )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "applescript_timeout"}
    except FileNotFoundError:
        return {"ok": False, "reason": "osascript_missing"}
    except OSError as exc:
        return {"ok": False, "reason": "applescript_error", "detail": str(exc)[:200]}
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        if _looks_like_js_blocked(err):
            reason = "applescript_js_blocked"
        elif _looks_like_permission_denied(err):
            reason = "automation_denied"
        else:
            reason = "applescript_error"
        return {"ok": False, "reason": reason, "detail": err[:300]}
    if out == "NO_CHROME":
        return {"ok": False, "reason": "chrome_not_running"}
    if out == "NO_TAB":
        return {"ok": False, "reason": "no_muse_tab"}
    try:
        result = json.loads(out)
    except ValueError:
        return {"ok": False, "reason": "bad_js_result", "detail": out[:200]}
    if isinstance(result, dict) and result.get("ok"):
        return {"ok": True, "method": "applescript", "detail": result}
    reason = result.get("reason", "js_failed") if isinstance(result, dict) else "js_failed"
    return {"ok": False, "reason": reason, "detail": str(result)[:200]}


# ---------------------------------------------------------------------------
# Windows：CDP。websocket-client 只在这里懒加载，macOS 永远不需要它。
# ---------------------------------------------------------------------------
def _send_via_cdp(text: str, url_match: str, port: int = DEFAULT_CDP_PORT) -> dict:
    """Windows：连 Chrome 调试端口，找含 url_match 的页面 target，
    用 Runtime.evaluate 执行 JS。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "reason": "cdp_unreachable", "detail": str(exc)[:200]}
    except ValueError as exc:
        return {"ok": False, "reason": "cdp_bad_response", "detail": str(exc)[:200]}
    target = None
    for candidate in targets or []:
        if candidate.get("type") == "page" and url_match in (candidate.get("url") or ""):
            target = candidate
            break
    if target is None:
        return {"ok": False, "reason": "no_muse_tab"}
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        return {"ok": False, "reason": "no_muse_tab"}
    try:
        import websocket
    except ImportError:
        return {"ok": False, "reason": "websocket_client_missing"}
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
        try:
            ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": build_js_payload(text),
                            "returnByValue": True,
                        },
                    }
                )
            )
            raw = ws.recv()
        finally:
            ws.close()
    except Exception as exc:  # noqa: BLE001 - CDP 连接失败只记原因，不中断轮次
        return {"ok": False, "reason": "cdp_ws_error", "detail": str(exc)[:200]}
    try:
        message = json.loads(raw)
        value = message.get("result", {}).get("result", {}).get("value")
        result = json.loads(value) if isinstance(value, str) else value
    except (ValueError, AttributeError, TypeError):
        return {"ok": False, "reason": "bad_js_result", "detail": str(raw)[:200]}
    if isinstance(result, dict) and result.get("ok"):
        return {"ok": True, "method": "cdp", "detail": result}
    reason = result.get("reason", "js_failed") if isinstance(result, dict) else "js_failed"
    return {"ok": False, "reason": reason, "detail": str(result)[:200]}


# ---------------------------------------------------------------------------
# 入口：方法选择 + 失败提示。
# ---------------------------------------------------------------------------
def _resolve_method(method: str | None, platform_name: str | None = None) -> str:
    """method: auto | applescript | cdp。auto 按平台选；其他平台返回
    unsupported（本机 Linux 就是，模块照常 import、测试照常跑）。"""
    chosen = (method or "auto").strip().lower()
    if chosen == "auto":
        plat = platform_name or sys.platform
        if plat == "darwin":
            return "applescript"
        if plat == "win32":
            return "cdp"
        return "unsupported"
    if chosen in ("applescript", "cdp"):
        return chosen
    return "unsupported"


def _log_send_failure(result: dict) -> None:
    """按失败原因给一条人能看懂的提示（不含任何凭证，消息正文也不进日志）。"""
    reason = result.get("reason", "unknown")
    hints = {
        "chrome_not_running": "Chrome 未运行：打开 Chrome 并登录 Muse 对话后，下一轮会自动再试",
        "no_muse_tab": "未找到 Muse 对话标签页：请在 Chrome 里打开 Muse 对话（URL 含 muse.ai），下一轮自动再试",
        "automation_denied": "macOS 未允许自动化：系统设置 → 隐私与安全性 → 自动化，允许扫描器/终端控制 Google Chrome（见 docs/MUSE_CHAT.md）",
        "applescript_js_blocked": "Chrome 未开启 Apple Events 的 JavaScript 注入：Chrome 菜单栏 → View（视图）→ Developer（开发者）→ 勾选 Allow JavaScript from Apple Events（当前 Chrome 默认关闭，是 -1700 失败最常见的原因；与 macOS 自动化权限是两回事，两处都要开，见 docs/MUSE_CHAT.md）",
        "cdp_unreachable": "连不上 Chrome 调试端口：请完全退出 Chrome 后加 --remote-debugging-port=9222 重开（见 docs/MUSE_CHAT.md）",
        "websocket_client_missing": "缺少 websocket-client：运行 pip install -r requirements.txt（或重跑安装脚本）",
        "unsupported_platform": "当前平台不支持浏览器输入（仅 macOS / Windows）",
    }
    hint = hints.get(reason)
    if hint:
        logger.warning("Muse 对话提醒未发送（%s）：%s", reason, hint)
    else:
        detail = result.get("detail", "")
        logger.warning("Muse 对话提醒未发送（%s）%s", reason, f"：{detail}" if detail else "")


def send_to_muse_chat(text: str, muse_cfg: dict | None = None) -> dict:
    """把一段文本打进 Muse 对话输入框并发送。返回 {"ok": bool,
    "reason": ...}，reason 只在失败时有。任何失败都不抛异常。"""
    cfg = muse_cfg or {}
    url_match = (cfg.get("url_match") or DEFAULT_URL_MATCH).strip() or DEFAULT_URL_MATCH
    method = _resolve_method(cfg.get("method", "auto"))
    if method == "unsupported":
        logger.info("Muse 对话提醒跳过：当前平台 %s 不支持浏览器输入（仅 macOS/Windows）", sys.platform)
        return {"ok": False, "reason": "unsupported_platform"}
    if method == "applescript":
        result = _send_via_applescript(text, url_match)
    else:
        try:
            port = int(cfg.get("cdp_port", DEFAULT_CDP_PORT) or DEFAULT_CDP_PORT)
        except (TypeError, ValueError):
            port = DEFAULT_CDP_PORT
        result = _send_via_cdp(text, url_match, port)
    if result.get("ok"):
        logger.info("Muse 对话提醒已发送（%s）", result.get("method"))
    else:
        _log_send_failure(result)
    return result


def send_alerts_to_muse_chat(
    bursts: list[dict] | None,
    muse_cfg: dict | None = None,
    delay_seconds: float = MESSAGE_DELAY_SECONDS,
) -> dict:
    """把一轮的爆发事件逐条打进 Muse 对话。去重规则与 Telegram
    完全一致（同一轮归一化后同名只发第一条，见 alerts.py），
    每条消息都是 format_muse_chat_message 的完整文本（标记行 + 正文）。

    一条失败不影响其他条；返回 {"sent": n, "failed": [{event_id, error}]}，
    本身不抛异常。
    """
    from trend_radar.alerts import _normalize_name, _normalize_raw_name

    bursts = list(bursts or [])
    seen_names: set[str] = set()
    seen_raw_names: set[str] = set()
    unique_bursts: list[dict] = []
    for burst in bursts:
        display_key = _normalize_name(burst)
        raw_key = _normalize_raw_name(burst)
        if display_key in seen_names or (raw_key and raw_key in seen_raw_names):
            continue
        seen_names.add(display_key)
        if raw_key:
            seen_raw_names.add(raw_key)
        unique_bursts.append(burst)
    if len(unique_bursts) != len(bursts):
        logger.info(
            "Muse 对话提醒去重：%d 条爆发里 %d 条与前一条同名，只发 %d 条",
            len(bursts),
            len(bursts) - len(unique_bursts),
            len(unique_bursts),
        )
    sent = 0
    failed: list[dict] = []
    for index, burst in enumerate(unique_bursts):
        if index > 0 and delay_seconds:
            time.sleep(delay_seconds)
        result = send_to_muse_chat(format_muse_chat_message(burst), muse_cfg)
        if result.get("ok"):
            sent += 1
        else:
            failed.append(
                {"event_id": burst.get("event_id", ""), "error": result.get("reason", "unknown")}
            )
    return {"sent": sent, "failed": failed}


# ---------------------------------------------------------------------------
# 自测入口：python -m trend_radar.muse_chat --test "扫描器测试"
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """在用户自己的电脑上验证打字是否通：发一条无害的测试消息，
    清楚地报告每一步（标签页找没找到、输入框找没找到、发没发出）。
    返回 0 表示已发送，1 表示未发送。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="测试：把一条消息打进本机 Chrome 里的 Muse 对话输入框并发送。"
    )
    parser.add_argument(
        "--test",
        metavar="TEXT",
        default="扫描器测试：这条是测试消息，不是爆发提醒，收到请忽略。",
        help="要发送的测试文本（默认一条无害的测试消息）",
    )
    parser.add_argument("--url-match", default=DEFAULT_URL_MATCH, help="标签页 URL 匹配串（默认 muse.ai）")
    parser.add_argument("--method", default="auto", help="auto | applescript | cdp（默认 auto）")
    parser.add_argument("--cdp-port", type=int, default=DEFAULT_CDP_PORT, help="CDP 端口（默认 9222）")
    args = parser.parse_args(argv)
    cfg = {"url_match": args.url_match, "method": args.method, "cdp_port": args.cdp_port}
    method = _resolve_method(args.method)
    print(f"平台：{sys.platform}，方法：{method}")
    print(f"标签页 URL 匹配：{args.url_match}")
    result = send_to_muse_chat(args.test, cfg)
    if result.get("ok"):
        detail = result.get("detail") or {}
        print(f"标签页：已找到；输入框：已找到（{detail.get('input', '?')}），已通过 {detail.get('submitted', '?')} 发送")
        print("结果：已发送")
        return 0
    print(f"结果：未发送（{result.get('reason', 'unknown')}）")
    if result.get("detail"):
        print(f"详情：{result['detail']}")
    print("排查见 docs/MUSE_CHAT.md（macOS 看自动化权限和 Chrome 的 Allow JavaScript from Apple Events，Windows 看 Chrome 调试端口）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
