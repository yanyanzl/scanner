# Muse 对话提醒：爆发事件打进浏览器输入框

- 日期：2026-11-15
- 状态：已实现并测试（mock）；2026-11-15 用户 Mac 上第一次实测报 -1700
  （标签页已找到，`execute javascript` 被挡），已修模板并补了 Chrome
  开关说明；当晚再次确认：开关开着、序号定位也换了，还是 -1700，
  CDP 的 9222 端口带参数启动后也不监听——Chrome 的两条路在这台 Mac
  上都已确认不可用，已加 Safari 路径（`method: safari`），待用户实测。
- 2026-11-16 Safari 实测进展：文字能填进输入框（textarea），但没发送；
  CLI 曾报“已发送”却是假阳性——JS 点了按钮就报 ok，不能证明消息真
  发出去了。09:34 用户确认：页面在前台被看着时发送成功，后台标签页
  时点了按钮也发不出去。修法：Safari 执行 JS 前先 `activate` 并把目标
  标签页设为当前标签页（会抢焦点，见下）；提交链加固（灰按钮不点、
  按钮归属校验、form/Enter 兜底、报 `send_button_disabled` 不谎报 ok）。
  待确认：发出去的提醒是不是进了这个主对话（Safari 标签页里打开的
  是不是和 Cheese 的这个对话）。

## 这是什么

Telegram 提醒之外，爆发事件再打一份进用户自己浏览器（Chrome 或 Safari）
里的 Muse 对话输入框并发送。这样提醒直接出现在这个对话里，Cheese 看到就能
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

**注意：每日质量简报用的是另一个标记**（`quality-digest v1`，
见 `docs/QUALITY_DIGEST.md`），不是爆发提醒。`parse_scanner_marker`
不会把简报认成爆发提醒；反过来简报的识别函数也不会认爆发提醒。
两者走同一条发送通道（同一个 `send_to_muse_chat`），只是首行标记
和内容不同。

## 消息去向与计数

- 发的是同一批 `alert_bursts`：已过质量门槛（条数 ≥ 20 或总分 ≥ 5，2026-11-16
  从 10 条抬到 20 条——11 条/2.94 分的小丑短视频曾靠旧门槛漏过去）、
  已去重（同一轮归一化后同名只发第一条，与 Telegram 去重规则一致）、
  已去掉重复出现（`is_recurrence` 的不发）和陈旧快照的事件（本轮没有
  新快照的不再触发，见 `docs/P2_PREP.md` 的跨轮次重复触发一节）。两条
  提醒之间隔 0.5 秒。
- 只受 `config.yaml` 里 `muse_chat.enabled` 控制（默认 true），与
  `telegram.enabled` 无关。
- 轮次汇总里记 `muse_chat_sent` / `muse_chat_failed`。
- 任何失败（标签页没开、Chrome 没开、权限没给、端口没开）只记 warning，
  不影响本轮采集、不影响 Telegram 提醒。

## 设置步骤（只做一次）

### macOS：AppleScript，不用装东西、不用重启 Chrome

> 2026-11-15 实测：Chrome 的 AppleScript 在这台 Mac 上不可用（开关开着、
> 序号定位也换了，`execute javascript` 仍报 -1700），CDP 的 9222 端口带
> 参数启动后也不监听。macOS 建议走下面的 Safari，Chrome 这节只作备用参考。

1. 打开 Chrome，登录 Muse，打开这个对话的标签页（保持开着）。
2. Chrome 里开一个开关（macOS 的第一道门）：菜单栏 → View（视图）
   → Developer（开发者）→ 勾选 Allow JavaScript from Apple Events。
   当前 Chrome 默认是关闭的，没开的话 `execute javascript` 会报
   -1700（2026-11-15 实测最常见的失败原因）。
3. 第一次跑扫描时，macOS 会弹一次权限（第二道门）：允许扫描器
   （终端 / Python）控制 Google Chrome。点“允许”。
   如果当时点了拒绝：系统设置 → 隐私与安全性 → 自动化，找到终端
   （或 Python），把 Google Chrome 的开关打开。
   注意：第 2 步和第 3 步是两回事，各管各的：一个管 Chrome 允不允许
   AppleScript 往页面里注入 JS，一个管 macOS 允不允许扫描器控制
   Chrome。两处都要开，少一个都会失败（-1700 是第 2 步没开，
   automation_denied 是第 3 步没给）。
4. 以后不用再管：每轮有爆发就自动打进来。

### macOS（推荐）：Safari，do JavaScript 原生支持

2026-11-15 实测 Chrome 的 AppleScript 和 CDP 在这台 Mac 上都走不通，
改走 Safari：`do JavaScript` 是 Safari 原生支持的，不经过 Chrome 的桥。

1. 打开 Safari，登录 Muse，打开这个对话的标签页（保持开着）。
2. Safari → 设置 → 高级 → 勾选“在菜单栏中显示开发菜单”。
3. 菜单栏 Develop（开发）→ 勾选 Allow JavaScript from Apple Events。
4. `config.yaml` 里 `muse_chat.method: safari`（默认值 auto 在 macOS 上
   仍走 Chrome 的 AppleScript，不会自动切到 Safari，要显式改）。
5. 先测一把（发一条无害的测试消息）：
   ```bash
   python -m trend_radar.muse_chat --test "扫描器测试" --method safari
   ```

注意：Safari 的 AppleScript 只看当前标签页组里的标签页，Muse 对话要放在
当前打开的标签页组里（不在当前组的标签页会被当成没找到，报 no_muse_tab）。
第一次跑扫描时 macOS 也会弹一次自动化权限：允许扫描器（终端 / Python）
控制 Safari（拒绝了的话去系统设置 → 隐私与安全性 → 自动化里打开）。

**页面必须在前台（2026-11-16 实测结论）：** 发送成功与否和 Muse 标签页
是不是前台、正在被看着有关。后台标签页时 JS 点了按钮也没发出去（09:15
和 09:31 两轮 `muse_chat_sent: 3` 全是假阳性，消息没进对话）；用户在前台
看着页面时就发出去了。具体机制没定论（可能是按钮在页面不可见时是灰的、
应用侧对可见性/焦点有要求、或后台标签页里 focus() 不生效导致编辑器状态
没更新），工程上直接复现验证过的成功条件：执行 JS 之前，AppleScript 先
`activate` 把 Safari 带到前台，再把目标标签页设为当前标签页（`set current
tab of window … to …`），等 0.5 秒让标签页切过来，再跑 JS。Safari 没运行
时仍走原来的 `NO_SAFARI` 路径，不会因此崩掉。

**取舍（activate 会抢焦点）：** 每 15 分钟有爆发要发时，Safari 会被弹到
前台、打断用户正在做的事。这是当前让发送可靠的做法，先这样用；如果以后
发现只切标签页不 `activate` 也够用，再降级（去掉 `activate`、只留切标签页）。
**标签页必须是和 Cheese 的主对话（2026-11-16 09:45 已确认）：** 09:45 之前
Safari 里开的是另一个对话，提醒就算发出去了也到不了这个对话——这就是
“已发送却没收到”的第一个原因。用户已于 09:45 把标签页切到了这个主对话。
以后换了标签页、开了新对话，都要先确认 Safari 里打开的是这个主对话，
否则提醒会发到别的对话里。

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
发送的）、发没发出。成功时还会打印发送按钮的 label（用来定位点到的是
不是真正的发送按钮）；发送按钮是灰的时会报 `send_button_disabled`
（页面认为输入框是空的——值没进编辑器状态，合成回车同样发不出去），
不再谎报“已发送”。没发出时会给排查提示（macOS 看自动化权限和
Chrome 的 Allow JavaScript from Apple Events，Windows 看 Chrome
调试端口）。**先跑通这条，再等真实爆发。**

提交链（2026-11-16 加固，按顺序尝试，返回里写明实际走的那条）：
填值先 focus；TEXTAREA/INPUT 用原生 setter + input 事件；contenteditable
优先 `execCommand('insertText')`（走编辑器自己的处理、同步内部状态），
回落 textContent + beforeinput/input 事件。发送：先找可用的发送按钮
（和输入框同一个 form 里、或最近容器里的优先；aria-label/title/文本含
send/submit/发送/提交，或显式 type=submit；**灰按钮永远不点**）→ 输入框
所在表单的 `requestSubmit()` → 回车键事件（keyCode/which 强制 13；但发送
按钮是灰的时不发回车——页面认为输入框是空的，合成回车同样会被忽略，
报 `send_button_disabled`）。找不到输入框时什么都不做、返回 ok:false，
绝不乱打字。

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
- **标签页必须是和 Cheese 的主对话（2026-11-16 09:45 的教训）**：Safari
  里开的是哪个对话，提醒就发到哪个对话。09:45 之前开的是另一个对话，
  提醒就算发出去了也到不了 Cheese 的这个对话——“已发送却没收到”的第一
  个原因。用户已于 09:45 切到主对话；以后换标签页、开新对话都要先确认。
- **页面必须在前台才发得出去（2026-11-16 实测）**：后台标签页时 JS 点了
  按钮也没发出去（09:15/09:31 两轮 `muse_chat_sent: 3` 全是假阳性），
  前台看着时就发出去了——“已发送却没收到”的第二个原因。修法：Safari
  执行 JS 前先 `activate` 并把目标标签页设为当前标签页（会抢焦点，见
  上面“取舍”）。
- **CLI 的“已发送”曾是假阳性（2026-11-16）**：JS 点了按钮就报 ok，不能
  证明消息真发出了。灰按钮、错按钮、后台标签页、错对话都会造成假阳性。
  现在只在真正做了发送动作时报 ok；发送按钮是灰的报
  `send_button_disabled`，不再谎报；成功时打印按钮的 label 供定位。
- **选择器是尽力而为**：Muse 网页的真实 DOM 没法在没登录的机器上看，
  只能按常见形态写兜底；真机实测前不能保证一次就通，先跑 `--test`。
- **AppleScript 的 `execute javascript`（-1700 实测）**：2026-11-15
  用户 Mac 上第一次实测就报了 -1700（Can't make application
  "Google Chrome" into type specifier），标签页其实已经找到了，
  是 `execute javascript` 这一步被挡了。两个原因都已处理：(1) 模板
  改成显式序号定位（`tab ti of window wi`，循环变量传给
  `execute javascript` 会触发 -1700）；(2) 没开 Chrome 的
  Allow JavaScript from Apple Events 也会报 -1700，见上面设置步骤
  第 2 步，报错原因现在单独记为 `applescript_js_blocked`，不再混
  在通用的 applescript_error 里。2026-11-15 晚再次确认：开关开着、
  序号定位也换了，还是 -1700；CDP 的 9222 端口带参数启动后也不监听。
  Chrome 的两条路在这台 Mac 上都已确认不可用，macOS 请走 Safari
  （`method: safari`，见上面设置步骤）。
- **Windows 上 Chrome 必须带调试端口启动**：没加参数重开的话，
  端口连不上，每轮只记一条提示，不会弹窗打扰。
- 提醒正文与 Telegram 同文，但不截到 500 字（对话输入框没这个限制）；
  首行标记另算，不占正文字数。
