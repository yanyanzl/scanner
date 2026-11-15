"""Muse 对话提醒测试（全部 mock，没有真 Chrome）。

覆盖：结构化标记（生成 / 解析 / 防误判）、共用 JS、AppleScript
（mock osascript）、CDP（mock /json + websocket）、方法选择、
批量发送（去重 / 失败不中断）、--test 自测入口。
"""
import io
import json
import re
import subprocess
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import muse_chat
from trend_radar.alerts import format_burst_message

_BURST = {
    "event_id": "evt_bc521d54d2246ae6",
    "name": "Test Live Stream",
    "name_zh": "测试直播",
    "reasons": ["new_multi_platform"],
    "is_recurrence": False,
    "current": {
        "item_count": 5,
        "platform_count": 2,
        "total_score": 12.0,
        "max_score": 8.0,
        "max_growth": 704.0,
        "platforms": "rss,youtube",
    },
    "previous": None,
    "detected_at": "2026-11-15T21:00:00+00:00",
}

_EXPECTED_MARKER = (
    "【扫描器提醒｜scanner-alert v1｜event=evt_bc521d54d2246ae6"
    "｜rule=new_multi_platform｜ts=2026-11-15T21:00:00Z】"
)


class TestMarker(unittest.TestCase):
    def test_marker_is_first_line_exact_shape(self):
        msg = muse_chat.format_muse_chat_message(_BURST)
        lines = msg.splitlines()
        self.assertEqual(lines[0], _EXPECTED_MARKER)
        self.assertEqual(lines[1:], format_burst_message(_BURST).splitlines())

    def test_parse_round_trip(self):
        msg = muse_chat.format_muse_chat_message(_BURST)
        parsed = muse_chat.parse_scanner_marker(msg)
        self.assertEqual(
            parsed,
            {
                "version": "v1",
                "event_id": "evt_bc521d54d2246ae6",
                "rule": "new_multi_platform",
                "ts": "2026-11-15T21:00:00Z",
            },
        )
        self.assertTrue(muse_chat.is_scanner_alert(msg))

    def test_ts_without_detected_at_uses_now(self):
        burst = dict(_BURST)
        burst.pop("detected_at", None)
        msg = muse_chat.format_muse_chat_message(burst)
        parsed = muse_chat.parse_scanner_marker(msg)
        self.assertIsNotNone(parsed)
        self.assertRegex(parsed["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_ts_detected_at_with_z_suffix(self):
        burst = dict(_BURST, detected_at="2026-11-15T21:00:00Z")
        parsed = muse_chat.parse_scanner_marker(muse_chat.format_muse_chat_message(burst))
        self.assertEqual(parsed["ts"], "2026-11-15T21:00:00Z")

    def test_rule_first_reason_and_unknown_fallback(self):
        burst = dict(_BURST, reasons=["growth_spike", "score_surge"])
        self.assertIn("rule=growth_spike", muse_chat.format_muse_chat_message(burst).splitlines()[0])
        burst = dict(_BURST, reasons=[])
        self.assertIn("rule=unknown", muse_chat.format_muse_chat_message(burst).splitlines()[0])

    def test_plain_message_without_marker_does_not_match(self):
        # 只有中文正文、没有标记：不是扫描器消息。
        self.assertIsNone(muse_chat.parse_scanner_marker(format_burst_message(_BURST)))
        self.assertFalse(muse_chat.is_scanner_alert(format_burst_message(_BURST)))
        # 只有中文前缀、没有 scanner-alert token：不是扫描器消息。
        self.assertIsNone(muse_chat.parse_scanner_marker("【扫描器提醒】我自己敲的提醒"))
        self.assertIsNone(muse_chat.parse_scanner_marker(""))
        self.assertIsNone(muse_chat.parse_scanner_marker(None))

    def test_marker_not_on_first_line_does_not_match(self):
        msg = "用户先说了一句话\n" + muse_chat.format_muse_chat_message(_BURST)
        self.assertIsNone(muse_chat.parse_scanner_marker(msg))

    def test_parse_accepts_future_version(self):
        text = (
            "【扫描器提醒｜scanner-alert v2｜event=evt_x｜rule=growth_spike"
            "｜ts=2026-11-15T21:00:00Z】\n正文"
        )
        parsed = muse_chat.parse_scanner_marker(text)
        self.assertEqual(parsed["version"], "v2")
        self.assertEqual(parsed["event_id"], "evt_x")


class TestJsPayload(unittest.TestCase):
    def test_selector_fallbacks_in_order(self):
        tpl = muse_chat.JS_PAYLOAD_TEMPLATE
        positions = [
            tpl.index("'textarea'"),
            tpl.index('[contenteditable="true"]'),
            tpl.index('input[type="text"]'),
            tpl.index('[role="textbox"]'),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_react_safe_setter_and_input_event(self):
        tpl = muse_chat.JS_PAYLOAD_TEMPLATE
        self.assertIn("getOwnPropertyDescriptor", tpl)
        self.assertIn("new Event('input', {bubbles: true})", tpl)
        self.assertIn("textContent", tpl)  # contenteditable 走 textContent

    def test_submit_paths(self):
        tpl = muse_chat.JS_PAYLOAD_TEMPLATE
        self.assertIn("'send'", tpl)
        self.assertIn("发送", tpl)
        self.assertIn('button[type="submit"]', tpl)
        self.assertIn("KeyboardEvent('keydown'", tpl)
        self.assertIn("JSON.stringify({ok: false, reason: 'no_input'})", tpl)
        self.assertIn("return JSON.stringify({ok: true", tpl)

    def test_build_payload_embeds_message_safely(self):
        text = '带"引号"、换行\n和中文的消息'
        payload = muse_chat.build_js_payload(text)
        self.assertIn(json.dumps(text, ensure_ascii=False), payload)
        self.assertTrue(payload.startswith("(function (message)"))
        self.assertTrue(payload.rstrip().endswith(")"))


class TestAppleScript(unittest.TestCase):
    def _run(self, stdout="", returncode=0, stderr="", side_effect=None):
        proc = mock.Mock()
        proc.stdout = stdout
        proc.stderr = stderr
        proc.returncode = returncode
        captured = {}

        def fake_run(cmd, **kwargs):
            if side_effect is not None:
                raise side_effect
            captured["cmd"] = cmd
            captured["js"] = Path(cmd[2]).read_text(encoding="utf-8")
            captured["scpt"] = Path(cmd[1]).read_text(encoding="utf-8")
            return proc

        with mock.patch("trend_radar.muse_chat.subprocess.run", side_effect=fake_run):
            result = muse_chat._send_via_applescript("hello 提醒", "muse.ai")
        return result, captured

    def test_success_and_script_contents(self):
        result, captured = self._run(
            stdout='{"ok": true, "input": "textarea", "submitted": "button"}'
        )
        self.assertEqual(result, {"ok": True, "method": "applescript", "detail": {"ok": True, "input": "textarea", "submitted": "button"}})
        self.assertEqual(captured["cmd"][0], "osascript")
        self.assertEqual(captured["cmd"][3], "muse.ai")
        self.assertIn("hello 提醒", captured["js"])
        self.assertIn("Google Chrome", captured["scpt"])
        self.assertIn("execute javascript", captured["scpt"])
        self.assertIn("NO_CHROME", captured["scpt"])
        self.assertIn("NO_TAB", captured["scpt"])

    def test_no_chrome(self):
        result, _ = self._run(stdout="NO_CHROME")
        self.assertEqual(result, {"ok": False, "reason": "chrome_not_running"})

    def test_no_tab(self):
        result, _ = self._run(stdout="NO_TAB")
        self.assertEqual(result, {"ok": False, "reason": "no_muse_tab"})

    def test_permission_denied(self):
        result, _ = self._run(
            stdout="", returncode=1, stderr="Google Chrome got an error: not allowed to send Apple events"
        )
        self.assertEqual(result["reason"], "automation_denied")

    def test_applescript_error(self):
        result, _ = self._run(stdout="", returncode=1, stderr="something else broke")
        self.assertEqual(result["reason"], "applescript_error")

    def test_timeout(self):
        result, _ = self._run(side_effect=subprocess.TimeoutExpired("osascript", 30))
        self.assertEqual(result, {"ok": False, "reason": "applescript_timeout"})

    def test_bad_js_result(self):
        result, _ = self._run(stdout="not json at all")
        self.assertEqual(result["reason"], "bad_js_result")

    def test_js_reports_no_input(self):
        result, _ = self._run(stdout='{"ok": false, "reason": "no_input"}')
        self.assertEqual(result, {"ok": False, "reason": "no_input", "detail": "{'ok': False, 'reason': 'no_input'}"})


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeWS:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def recv(self):
        return json.dumps(
            {
                "id": 1,
                "result": {
                    "result": {
                        "value": json.dumps(
                            {"ok": True, "input": "textarea", "submitted": "enter"}
                        )
                    }
                },
            }
        )

    def close(self):
        pass


_TARGETS = [
    {"type": "page", "url": "https://example.com/other", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/OTHER"},
    {"type": "page", "url": "https://muse.ai/chat/abc", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/MUSE"},
]


class TestCdp(unittest.TestCase):
    def _run(self, targets=_TARGETS, urlopen_side_effect=None, ws_module=None, recv_override=None):
        fake_ws = _FakeWS()
        if recv_override is not None:
            fake_ws.recv = recv_override
        fake_module = mock.Mock()
        fake_module.create_connection = mock.Mock(return_value=fake_ws)
        if ws_module is None:
            ws_module = fake_module
        urlopen = mock.Mock(return_value=_FakeResp(targets))
        if urlopen_side_effect is not None:
            urlopen.side_effect = urlopen_side_effect
        with (
            mock.patch("urllib.request.urlopen", urlopen),
            mock.patch.dict(sys.modules, {"websocket": ws_module}),
        ):
            result = muse_chat._send_via_cdp("hello 提醒", "muse.ai", 9222)
        return result, fake_ws, fake_module

    def test_success_picks_muse_tab(self):
        result, fake_ws, fake_module = self._run()
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["method"], "cdp")
        fake_module.create_connection.assert_called_once_with(
            "ws://127.0.0.1:9222/devtools/page/MUSE", timeout=10
        )
        sent = json.loads(fake_ws.sent[0])
        self.assertEqual(sent["method"], "Runtime.evaluate")
        self.assertTrue(sent["params"]["returnByValue"])
        self.assertIn("hello 提醒", sent["params"]["expression"])

    def test_port_unreachable(self):
        result, _, _ = self._run(
            urlopen_side_effect=urllib.error.URLError("Connection refused")
        )
        self.assertEqual(result, {"ok": False, "reason": "cdp_unreachable", "detail": "<urlopen error Connection refused>"})

    def test_no_matching_tab(self):
        result, _, fake_module = self._run(targets=[_TARGETS[0]])
        self.assertEqual(result, {"ok": False, "reason": "no_muse_tab"})
        fake_module.create_connection.assert_not_called()

    def test_websocket_client_missing(self):
        with (
            mock.patch("urllib.request.urlopen", mock.Mock(return_value=_FakeResp(_TARGETS))),
            mock.patch.dict(sys.modules, {"websocket": None}),
        ):
            result = muse_chat._send_via_cdp("hello", "muse.ai", 9222)
        self.assertEqual(result, {"ok": False, "reason": "websocket_client_missing"})

    def test_bad_js_result(self):
        result, _, _ = self._run(recv_override=lambda: "not json")
        self.assertEqual(result["reason"], "bad_js_result")


class TestSendToMuseChat(unittest.TestCase):
    def test_auto_on_linux_is_unsupported(self):
        self.assertEqual(sys.platform, "linux")  # 本机就是 Linux，方法选择可断言
        result = muse_chat.send_to_muse_chat("hi", {})
        self.assertEqual(result, {"ok": False, "reason": "unsupported_platform"})

    def test_unknown_method_is_unsupported(self):
        result = muse_chat.send_to_muse_chat("hi", {"method": "smoke-signal"})
        self.assertEqual(result, {"ok": False, "reason": "unsupported_platform"})

    def test_resolve_method(self):
        self.assertEqual(muse_chat._resolve_method("auto", "darwin"), "applescript")
        self.assertEqual(muse_chat._resolve_method("auto", "win32"), "cdp")
        self.assertEqual(muse_chat._resolve_method("auto", "linux"), "unsupported")
        self.assertEqual(muse_chat._resolve_method("cdp", "darwin"), "cdp")
        self.assertEqual(muse_chat._resolve_method("applescript", "win32"), "applescript")
        self.assertEqual(muse_chat._resolve_method("", "darwin"), "applescript")


class TestSendAlerts(unittest.TestCase):
    def _bursts(self):
        second = dict(_BURST, event_id="evt_other", name="Other Event", name_zh="其他事件")
        same_name_untranslated = dict(_BURST, event_id="evt_dup", name_zh=None)
        return _BURST, second, same_name_untranslated

    def test_sends_each_burst_with_marker(self):
        first, second, _ = self._bursts()
        with mock.patch.object(
            muse_chat, "send_to_muse_chat", mock.Mock(return_value={"ok": True})
        ) as send:
            result = muse_chat.send_alerts_to_muse_chat(
                [first, second], {}, delay_seconds=0
            )
        self.assertEqual(result, {"sent": 2, "failed": []})
        self.assertEqual(send.call_count, 2)
        for call, burst in zip(send.call_args_list, [first, second]):
            text = call.args[0]
            parsed = muse_chat.parse_scanner_marker(text)
            self.assertEqual(parsed["event_id"], burst["event_id"])
            self.assertEqual(parsed["rule"], "new_multi_platform")

    def test_dedup_same_name_only_once(self):
        first, _, dup = self._bursts()
        with mock.patch.object(
            muse_chat, "send_to_muse_chat", mock.Mock(return_value={"ok": True})
        ) as send:
            result = muse_chat.send_alerts_to_muse_chat([first, dup], {}, delay_seconds=0)
        self.assertEqual(result, {"sent": 1, "failed": []})
        send.assert_called_once()

    def test_failure_recorded_not_raised(self):
        with mock.patch.object(
            muse_chat,
            "send_to_muse_chat",
            mock.Mock(return_value={"ok": False, "reason": "no_muse_tab"}),
        ):
            result = muse_chat.send_alerts_to_muse_chat([_BURST], {}, delay_seconds=0)
        self.assertEqual(
            result,
            {"sent": 0, "failed": [{"event_id": "evt_bc521d54d2246ae6", "error": "no_muse_tab"}]},
        )

    def test_empty_bursts(self):
        with mock.patch.object(muse_chat, "send_to_muse_chat") as send:
            result = muse_chat.send_alerts_to_muse_chat([], {}, delay_seconds=0)
        self.assertEqual(result, {"sent": 0, "failed": []})
        send.assert_not_called()

    def test_delay_between_messages(self):
        first, second, _ = self._bursts()
        with (
            mock.patch.object(
                muse_chat, "send_to_muse_chat", mock.Mock(return_value={"ok": True})
            ),
            mock.patch("trend_radar.muse_chat.time.sleep") as sleep,
        ):
            muse_chat.send_alerts_to_muse_chat([first, second], {}, delay_seconds=0.5)
        sleep.assert_called_once_with(0.5)


class TestCli(unittest.TestCase):
    def test_main_success(self):
        with (
            mock.patch.object(
                muse_chat,
                "send_to_muse_chat",
                mock.Mock(
                    return_value={
                        "ok": True,
                        "method": "applescript",
                        "detail": {"input": "textarea", "submitted": "button"},
                    }
                ),
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(muse_chat.main(["--test", "hi"]), 0)

    def test_main_failure(self):
        with (
            mock.patch.object(
                muse_chat,
                "send_to_muse_chat",
                mock.Mock(return_value={"ok": False, "reason": "no_muse_tab"}),
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(muse_chat.main(["--test", "hi"]), 1)


if __name__ == "__main__":
    unittest.main()
