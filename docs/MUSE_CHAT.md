# Muse 对话提醒：爆发事件打进浏览器输入框

- 日期：2026-11-15
- 状态：已实现并测试（mock），未在真机 Chrome 上实测。

## 这是什么

Telegram 提醒之外，爆发事件再打一份进用户自己 Chrome 里的 Muse
对话输入框并发送。这样提醒直接出现在这个对话里，Cheese 看到就能
立刻反应，不用等用户来问。实现在 `trend_radar/muse_chat.py`，接线在
`trend_radar/collector.py` 的 `collect_once()` 里（Telegram 发送步骤之后）。

## 结构化标记（重点）

每条打进去的消息，第一行必须是结构化标记，第二行起才是与 Telegram
相同的提醒正文。标记的唯一生成入口是
`trend_radar/muse_chat.py::format_muse_chat_message`，格式只在那里定：

```text
【扫描器提醒｜scanner-alert v1｜event=<event_id>｜rule=<规则>｜ts=<UTC ISO>】
```

例子：

```text
【扫描器提醒｜scanner-alert v1｜event=evt_bc521d54d2246ae6｜rule=new_multi_platform｜ts=2026-11-15T21:00:00Z】
🚨 热点爆发提醒
事件：……
……
```

字段：

- `scanner-alert v1`：标记令牌 + 版本。`v1` 是当前版本。
- `event`：burst 的 event_id（如 `evt_bc521d54d2246ae6`）。
- `rule`：burst 的 reasons 第一条（如 `new_multi_platform`；
  reasons 为空时写 `unknown`）。
- `ts`：burst 的 detected_at，UTC ISO Z 形式（如 `2026-11-15T21:00:00Z`）；
  缺失时用发送当时的 UTC 时间。

识别规则（`parse_scanner_marker` / `is_scanner_alert`）：只看首行是否
完整匹配这个形状，不看中文正文。用户自己敲的“扫描器提醒”四个字、
或者只有中文前缀没有标记的，都不算扫描器消息。

版本管理：格式以后改了，`MARKER_VERSION` 升到 `v2`（只改这一处），
解析函数照样认 `scanner-alert v<N>` 的形状并返回 version 字段，调用方
按 version 决定怎么处理；老版本的消息靠 `scanner-alert` 这个 token
一直能认出来。

## 消息去向与计数

- 发的是同一批 `alert_bursts`：已过质量门槛（条数 ≥ 10 或总分 ≥ 5）、
  已去重（同一轮归一化后同名只发第一条，与 Telegram 去重规则一致）、
  已去掉重复出现（`is_recurrence` 的不发）。两条提醒之间隔 0.5 秒。
- 只受 `config.yaml` 里 `muse_chat.enabled` 控制（默认 true），与
  `telegram.enabled` 无关。
- 轮次汇总里记 `muse_chat_sent` / `muse_chat_failed`。
- 任何失败（标签页没开、Chrome 没开、权限没给、端口没开）只记 warning，
  不影响本轮采集、不影响 Telegram 提醒。

## 设置步骤（只做一次）

### macOS：AppleScript，不用装东西、不用重启 Chrome

1. 打开 Chrome，登录 Muse，打开这个对话的标签页（保持开着）。
2. 第一次跑扫描时，macOS 会弹一次权限：允许扫描器（终端 / Python）
   控制 Google Chrome。点“允许”。
   如果当时点了拒绝：系统设置 → 隐私与安全性 → 自动化，找到终端
   （或 Python），把 Google Chrome 的开关打开。
3. 以后不用再管：每轮有爆发就自动打进来。

### Windows：CDP，要重开一次 Chrome + 装一个依赖

1. 先**完全退出 Chrome**（关掉所有窗口；只关一个窗口不算，Chrome 还在
   后台跑着的话启动参数不生效）。
2. 用下面两种办法之一重开 Chrome：
   - 命令行（Win+R → cmd）：
     ```bat
     "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
     ```
     （路径按你自己的 Chrome 安装位置改；也可以在桌面 Chrome 快捷方式
     的“目标”后面加上 ` --remote-debugging-port=9222`，以后双击快捷方式
     就自带这个参数。）
   - 重开后 Chrome 看起来和平时一样，不用再做什么。
3. 装依赖（安装脚本已包含，`pip install -r requirements.txt` 或重跑
   `安装.bat` 即可，里面有 `websocket-client`）。
4. 打开 Muse 对话标签页（保持开着），重启扫描。

## 先测一把（爆发来之前验证打字通不通）

在项目根目录（先激活 venv）：

```bash
# macOS
source venv/bin/activate
python -m trend_radar.muse_chat --test "扫描器测试"
# Windows
.\venv\Scripts\Activate.ps1
python -m trend_radar.muse_chat --test "扫描器测试"
```

这条命令发一条无害的测试消息（不是爆发提醒），然后清楚地报告：
平台/方法、标签页找没找到、输入框找没找到（哪种输入框、按钮还是回车
发送的）、发没发出。没发出时会给排查提示（macOS 看自动化权限，
Windows 看 Chrome 调试端口）。**先跑通这条，再等真实爆发。**

## 找不到输入框时怎么办

找不到输入框，程序什么都不做、返回失败，不会乱打字。常见原因：

- Muse 对话标签页没开（最常见）：打开标签页，下一轮自动再试。
- Muse 网页改版、输入框结构变了：`trend_radar/muse_chat.py` 里的
  JS 是按常见形态写的兜底（textarea → contenteditable →
  input[type=text] → role=textbox，填值用原生 setter + input 事件，
  发送先找发送按钮、找不到发回车键事件）。改版后可能要跟着调
  选择器——先跑 `--test` 看它报的是 `no_input` 还是别的，再调。

## 已知的限制（实话）

- **消息是以用户的名义发出的**：它是替用户打进输入框的，对话里看
  起来就是用户发的。靠首行结构化标记区分，不是靠发送者。
- **标签页必须开着**：页没开就发不出去（这时只走 Telegram）。
- **选择器是尽力而为**：Muse 网页的真实 DOM 没法在没登录的机器上看，
  只能按常见形态写兜底；真机实测前不能保证一次就通，先跑 `--test`。
- **AppleScript 的 `execute javascript`**：macOS 路线依赖 Chrome 的
  AppleScript 支持；如果将来 Chrome 堵了这条路，macOS 也可以改
  `method: cdp` 走 CDP（Chrome 同样加调试端口重开）。
- **Windows 上 Chrome 必须带调试端口启动**：没加参数重开的话，
  端口连不上，每轮只记一条提示，不会弹窗打扰。
- 提醒正文与 Telegram 同文，但不截到 500 字（对话输入框没这个限制）；
  首行标记另算，不占正文字数。
