"""把爆发提醒打进用户自己的 Muse 对话（浏览器输入框）。

工作原理：扫描器跑在用户自己的电脑上，Chrome 里开着 Muse 对话页。
本模块找到那个标签页，用一段 JS 在页面里找到输入框、填进提醒文本、
点发送（或回车）。消息是以用户的名义发出的——首行有结构化标记
（见 format_muse_chat_message），一眼能认出是扫描器打的，不是
用户自己敲的。

三条入口，一段 JS：
- macOS（推荐）：Safari。AppleScript 的 do JavaScript 是 Safari
  原生支持的，不走 Chrome 的桥；Safari 里打开 Muse 对话标签页，
  菜单栏 Develop（开发）→ Allow JavaScript from Apple Events 要
  勾选，首次运行还会在系统设置 → 隐私与安全性 → 自动化里弹一次
  权限（允许扫描器控制 Safari）。
- macOS：Chrome + AppleScript（系统自带，不用装东西、不用重启
  Chrome）。2026-11-15 实测：这条路在这台 Mac 上已坏掉——Chrome
  开关开着、模板改成序号定位，execute javascript 仍报 -1700；
  CDP 的 9222 端口带参数启动后也不监听。所以 macOS 当前可用的
  是 Safari，Chrome 两条路保留作备用。
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
填值：TEXTAREA/INPUT 用原生 setter + input 事件（React 受控输入框
才能认到）；contenteditable 优先 execCommand('insertText')（走编辑器
自己的处理、同步内部状态），回落 textContent + beforeinput/input
事件。发送按顺序：可用的发送按钮（和输入框同一个 form 里、或最近
容器里的优先；aria-label/title/文本含 send/submit/发送/提交，或显
式 type=submit；灰按钮永远不点）→ 输入框所在表单的 requestSubmit()
→ 回车键事件（keyCode/which 强制 13；但发送按钮是灰的时不发回车——
页面认为输入框是空的，合成回车同样会被忽略，报 send_button_disabled）。
找不到输入框时什么都不做、返回 ok:false，绝不乱打字。2026-11-16
Safari 实测：CLI 报“已发送”但消息没进对话——JS 点了按钮就报 ok 是
假阳性（按钮可能是灰的、不是真正的发送按钮、或打进了后台标签页）；
现在只在真正做了发送动作时报 ok，灰按钮报 send_button_disabled，
Safari 执行 JS 前先 activate 并把目标标签页设为当前标签页（2026-11-16
实测：只有页面在前台时发送才成功；activate 会抢焦点，见 docs/MUSE_CHAT.md）。

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
      el.dispatchEvent(new Event('input', {bubbles: true}));
      return;
    }
    // 富文本编辑器（contenteditable，Lexical/Slate 这类）：只改 textContent
    // 不动编辑器内部状态，发送按钮会一直 disabled、回车也被忽略。
    // 优先走 execCommand('insertText')，让编辑器自己的 beforeinput/input
    // 处理跑起来、内部状态同步；先全选再插，替换掉框里已有的文字。
    try {
      var sel = window.getSelection();
      if (sel) sel.selectAllChildren(el);
      if (document.execCommand('insertText', false, value)) return;
    } catch (e) {}
    el.textContent = value;
    try {
      el.dispatchEvent(new InputEvent('beforeinput', {bubbles: true, cancelable: true, inputType: 'insertText', data: value}));
    } catch (e2) {
      el.dispatchEvent(new Event('beforeinput', {bubbles: true, cancelable: true}));
    }
    el.dispatchEvent(new Event('input', {bubbles: true}));
  }
  function buttonLabel(b) {
    var s = ((b.getAttribute && (b.getAttribute('aria-label') || b.getAttribute('title'))) || b.textContent || '').trim();
    return s.slice(0, 40);
  }
  function isDisabledButton(b) {
    return !!b && (b.disabled || (b.getAttribute && b.getAttribute('aria-disabled') === 'true'));
  }
  function sendLabel(b) {
    return ((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('title') || '') + ' ' + (b.textContent || '')).toLowerCase();
  }
  function labelMatchesSend(b) {
    var label = sendLabel(b);
    return label.indexOf('send') !== -1 || label.indexOf('submit') !== -1 || label.indexOf('发送') !== -1 || label.indexOf('提交') !== -1;
  }
  function isSendButton(b, labelOnly) {
    if (!b || isDisabledButton(b)) return false;
    if (labelMatchesSend(b)) return true;
    return !labelOnly && !!(b.getAttribute && b.getAttribute('type') === 'submit');
  }
  function findSendButtonIn(root, found) {
    // 在 root 内找可用的发送按钮：先按 label 特征，再按显式 type=submit。
    // 只找到灰的（disabled）时不返回它，记下来（found.disabled）继续找，
    // 别处可能有可用的；最后实在没有可用的，调用方凭它报 send_button_disabled。
    var btns = root.querySelectorAll('button, input[type="submit"]');
    var i, b;
    for (i = 0; i < btns.length; i++) {
      if (isSendButton(btns[i], true)) return btns[i];
    }
    for (i = 0; i < btns.length; i++) {
      if (isSendButton(btns[i], false)) return btns[i];
    }
    for (i = 0; i < btns.length; i++) {
      b = btns[i];
      if (!found.disabled && isDisabledButton(b) && (labelMatchesSend(b) || (b.getAttribute && b.getAttribute('type') === 'submit'))) found.disabled = b;
    }
    return null;
  }
  function findSendButton(input) {
    // 归属优先：同一个 form 里的 > 输入框最近容器里的 > 全页的。
    // 只凭全页第一个带 send 字样的按钮就点，很容易点到别的按钮。
    // 返回 {button: 可用的发送按钮或 null, disabledButton: 找到的
    // 灰色发送按钮或 null}；灰按钮永远不点。
    var found = {disabled: null};
    var form = input.closest ? input.closest('form') : null;
    var btn = null;
    if (form) {
      btn = findSendButtonIn(form, found);
      if (btn) return {button: btn, disabledButton: found.disabled};
    }
    var node = input.parentElement;
    var depth = 0;
    while (node && depth < 5) {
      btn = findSendButtonIn(node, found);
      if (btn) return {button: btn, disabledButton: found.disabled};
      node = node.parentElement;
      depth++;
    }
    btn = findSendButtonIn(document, found);
    return {button: btn, disabledButton: found.disabled};
  }
  function makeEnterEvent(type) {
    var ev = new KeyboardEvent(type, {key: 'Enter', code: 'Enter', bubbles: true, cancelable: true, composed: true});
    // keyCode/which 不在 KeyboardEventInit 里，构造函数里给也不生效，
    // 直接在事件对象上强制写 13（有些处理函数只认 keyCode）。
    try {
      Object.defineProperty(ev, 'keyCode', {value: 13});
      Object.defineProperty(ev, 'which', {value: 13});
    } catch (e) {}
    return ev;
  }
  var input = findInput();
  if (!input) return JSON.stringify({ok: false, reason: 'no_input'});
  try {
    setValue(input, message);
  } catch (e) {
    return JSON.stringify({ok: false, reason: 'set_value_failed', error: String(e)});
  }
  var found = findSendButton(input);
  var form = input.closest ? input.closest('form') : null;
  if (found.button) {
    // 点之前再确认一次：灰按钮 click() 是空操作，点了也白点，
    // 还会谎报 ok:true（2026-11-16 Safari 实测的假阳性就是这么来的）。
    if (!found.button.disabled) {
      found.button.click();
      return JSON.stringify({ok: true, input: input.tagName.toLowerCase(), submitted: 'button', button: buttonLabel(found.button)});
    }
  }
  if (form) {
    try {
      if (form.requestSubmit) {
        form.requestSubmit();
      } else {
        form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
      }
      var out = {ok: true, input: input.tagName.toLowerCase(), submitted: 'form'};
      if (found.disabledButton) {
        out.button = buttonLabel(found.disabledButton);
        out.buttonDisabled = true;
      }
      return JSON.stringify(out);
    } catch (e) {
      // requestSubmit 失败就继续走回车
    }
  }
  if (found.disabledButton) {
    // 发送按钮是灰的：页面认为输入框是空的（值没进编辑器状态），
    // 合成回车同样会被忽略——报 ok:false，别谎报已发送。
    return JSON.stringify({ok: false, reason: 'send_button_disabled', button: buttonLabel(found.disabledButton)});
  }
  input.dispatchEvent(makeEnterEvent('keydown'));
  input.dispatchEvent(makeEnterEvent('keypress'));
  input.dispatchEvent(makeEnterEvent('keyup'));
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


# ---------------------------------------------------------------------------
# macOS：Safari。do JavaScript 是 Safari 原生支持的，不走 Chrome 的
# AppleScript 桥（2026-11-15 实测 Chrome 的桥在这台 Mac 上坏掉，
# 与这条路无关）。其余与 Chrome 模板一致：JS 写临时文件、序号定位。
# 注意：Safari 的 AppleScript 只看当前标签页组里的标签页，Muse
# 对话要放在当前打开的标签页组里，见 docs/MUSE_CHAT.md。
# ---------------------------------------------------------------------------
SAFARI_APPLESCRIPT_TEMPLATE = """on run argv
  set jsPath to item 1 of argv
  set urlMatch to item 2 of argv
  set jsCode to read POSIX file jsPath as «class utf8»
  if application "Safari" is not running then
    return "NO_SAFARI"
  end if
  tell application "Safari"
    -- 先把 Safari 带到前台（2026-11-16 实测：只有 Muse 页面在前台
    -- 被看着时发送才成功，后台标签页里点了按钮也发不出去；
    -- 09:15/09:31 两轮 muse_chat_sent: 3 全是假阳性）。activate 会
    -- 抢焦点，每 15 分钟把 Safari 弹到前台，见 docs/MUSE_CHAT.md。
    -- 失败不崩：前面已判过 NO_SAFARI，这里再包一层 try，失败就
    -- 继续往下找标签页。
    try
      activate
    end try
    -- 优先用最前面窗口的当前标签页（用户正在看的那个对话）：
    -- Safari 里开了多个 Muse 标签页时，顺序扫描可能打进后台的
    -- 标签页。切到目标标签页、等 0.5 秒让它成为活动标签页，再跑 JS。
    try
      set curTab to current tab of window 1
      if (URL of curTab) contains urlMatch then
        set current tab of window 1 to curTab
        delay 0.5
        return do JavaScript jsCode in curTab
      end if
    end try
    -- 同样用显式序号定位标签页（与 Chrome 模板同理）。
    repeat with wi from 1 to count of windows
      repeat with ti from 1 to count of tabs of window wi
        if (URL of tab ti of window wi) contains urlMatch then
          set current tab of window wi to tab ti of window wi
          delay 0.5
          return do JavaScript jsCode in tab ti of window wi
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


def _looks_like_safari_js_blocked(stderr: str) -> bool:
    """osascript 报错里是不是 Safari 没开 JavaScript from Apple Events。

    与 Chrome 的 _looks_like_js_blocked 同理，先于自动化权限检查：
    Safari 菜单栏 Develop → Allow JavaScript from Apple Events 没勾，
    do JavaScript 会被挡。命中条件（小写后匹配）：含 "allow
    javascript"，或同时含 "javascript" 与 "not allowed" /
    "not permitted" / "denied" 任一。
    """
    lowered = (stderr or "").lower()
    if "allow javascript" in lowered:
        return True
    return "javascript" in lowered and any(
        token in lowered for token in ("not allowed", "not permitted", "denied")
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


def _send_via_safari(text: str, url_match: str) -> dict:
    """macOS：osascript 找 Safari 里含 url_match 的标签页，执行 JS。

    与 _send_via_applescript 同一套流程（临时文件 + osascript +
    30 秒超时），只是模板换成 Safari 的 do JavaScript。Safari 的
    JS 被禁先于自动化权限检查（见 _looks_like_safari_js_blocked）。
    """
    js = build_js_payload(text)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            js_path = Path(tmp) / "muse_chat_payload.js"
            scpt_path = Path(tmp) / "muse_chat_send.scpt"
            js_path.write_text(js, encoding="utf-8")
            scpt_path.write_text(SAFARI_APPLESCRIPT_TEMPLATE, encoding="utf-8")
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
        if _looks_like_safari_js_blocked(err):
            reason = "safari_js_blocked"
        elif _looks_like_permission_denied(err):
            reason = "automation_denied"
        else:
            reason = "applescript_error"
        return {"ok": False, "reason": reason, "detail": err[:300]}
    if out == "NO_SAFARI":
        return {"ok": False, "reason": "safari_not_running"}
    if out == "NO_TAB":
        return {"ok": False, "reason": "no_muse_tab"}
    try:
        result = json.loads(out)
    except ValueError:
        return {"ok": False, "reason": "bad_js_result", "detail": out[:200]}
    if isinstance(result, dict) and result.get("ok"):
        return {"ok": True, "method": "safari", "detail": result}
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
    """method: auto | applescript | cdp | safari。auto 按平台选
    （macOS 走 applescript，Windows 走 cdp）；safari 只在 macOS
    上可用；其他平台返回 unsupported（本机 Linux 就是，模块照常
    import、测试照常跑）。"""
    chosen = (method or "auto").strip().lower()
    if chosen == "auto":
        plat = platform_name or sys.platform
        if plat == "darwin":
            return "applescript"
        if plat == "win32":
            return "cdp"
        return "unsupported"
    if chosen == "safari":
        plat = platform_name or sys.platform
        return "safari" if plat == "darwin" else "unsupported"
    if chosen in ("applescript", "cdp"):
        return chosen
    return "unsupported"


def _log_send_failure(result: dict, method: str | None = None) -> None:
    """按失败原因给一条人能看懂的提示（不含任何凭证，消息正文也不进日志）。

    method 只影响 no_muse_tab 的提示：Safari 路径说 Safari，Chrome
    路径说 Chrome，不传时默认 Chrome 的说法（老调用方不受影响）。
    """
    reason = result.get("reason", "unknown")
    hints = {
        "chrome_not_running": "Chrome 未运行：打开 Chrome 并登录 Muse 对话后，下一轮会自动再试",
        "safari_not_running": "Safari 未运行：打开 Safari 并登录 Muse 对话后，下一轮会自动再试",
        "no_muse_tab": "未找到 Muse 对话标签页：请在 Chrome 里打开 Muse 对话（URL 含 muse.ai），下一轮自动再试",
        "automation_denied": "macOS 未允许自动化：系统设置 → 隐私与安全性 → 自动化，允许扫描器/终端控制 Google Chrome（见 docs/MUSE_CHAT.md）",
        "applescript_js_blocked": "Chrome 未开启 Apple Events 的 JavaScript 注入：Chrome 菜单栏 → View（视图）→ Developer（开发者）→ 勾选 Allow JavaScript from Apple Events（当前 Chrome 默认关闭，是 -1700 失败最常见的原因；与 macOS 自动化权限是两回事，两处都要开，见 docs/MUSE_CHAT.md）",
        "safari_js_blocked": "Safari 未开启 Apple Events 的 JavaScript：Safari → 设置 → 高级 → 勾选“在菜单栏中显示开发菜单”，然后菜单栏 Develop（开发）→ 勾选 Allow JavaScript from Apple Events（见 docs/MUSE_CHAT.md）",
        "cdp_unreachable": "连不上 Chrome 调试端口：请完全退出 Chrome 后加 --remote-debugging-port=9222 重开（见 docs/MUSE_CHAT.md）",
        "websocket_client_missing": "缺少 websocket-client：运行 pip install -r requirements.txt（或重跑安装脚本）",
        "send_button_disabled": "发送按钮是灰的（disabled）：页面认为输入框是空的——值可能没进编辑器状态，合成回车同样发不出去；看 --test 打印的输入框类型和按钮信息再定位（见 docs/MUSE_CHAT.md）",
        "unsupported_platform": "当前平台不支持浏览器输入（仅 macOS / Windows）",
    }
    hint = hints.get(reason)
    if reason == "no_muse_tab" and method == "safari":
        hint = "未找到 Muse 对话标签页：请在 Safari 里打开 Muse 对话（URL 含 muse.ai），下一轮自动再试"
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
    elif method == "safari":
        result = _send_via_safari(text, url_match)
    else:
        try:
            port = int(cfg.get("cdp_port", DEFAULT_CDP_PORT) or DEFAULT_CDP_PORT)
        except (TypeError, ValueError):
            port = DEFAULT_CDP_PORT
        result = _send_via_cdp(text, url_match, port)
    if result.get("ok"):
        logger.info("Muse 对话提醒已发送（%s）", result.get("method"))
    else:
        _log_send_failure(result, method)
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
        description="测试：把一条消息打进本机浏览器（Chrome 或 Safari）里的 Muse 对话输入框并发送。"
    )
    parser.add_argument(
        "--test",
        metavar="TEXT",
        default="扫描器测试：这条是测试消息，不是爆发提醒，收到请忽略。",
        help="要发送的测试文本（默认一条无害的测试消息）",
    )
    parser.add_argument("--url-match", default=DEFAULT_URL_MATCH, help="标签页 URL 匹配串（默认 muse.ai）")
    parser.add_argument("--method", default="auto", help="auto | applescript | cdp | safari（默认 auto）")
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
        if detail.get("button"):
            suffix = "（disabled，本次没点它）" if detail.get("buttonDisabled") else ""
            print(f"发送按钮：{detail['button']}{suffix}")
        print("结果：已发送")
        return 0
    print(f"结果：未发送（{result.get('reason', 'unknown')}）")
    if result.get("detail"):
        print(f"详情：{result['detail']}")
    print("排查见 docs/MUSE_CHAT.md（macOS 看自动化权限、Chrome 的 Allow JavaScript from Apple Events 或 Safari 的 Develop 设置，Windows 看 Chrome 调试端口）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
