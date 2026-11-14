# Global Trend Radar（全球热点雷达）

本地优先的个人 AI 全球热点监控系统：Windows / macOS · Python 3.12 · 每 15 分钟扫描一次 · 免费优先 · 模块化。

## 第一性原理（设计从这里推导，不是照搬热榜）

把“热点”拆到最基本的东西：

- 热点不是总量，而是变化：`增长速度 = (互动量_t - 互动量_{t-1}) / Δt`，加速是增长速度的变化。没有两次测量，就没有趋势。
- 要测量变化，必须有三样东西：内容的唯一身份（URL）、时间（发布时间 / 首次发现时间）、重复快照（每次采集的互动量）。这就是 `items + snapshots` 两张表存在的理由。
- 不同平台的数字单位不同，不能直接比较：先在平台内部标准化，再跨平台综合。这就是评分里对数标准化和权重存在的理由。
- 一个话题只在一个平台火，可能是噪音；同时在多个平台出现，才更可能是事件。这就是跨平台分占 20% 的理由。
- 成本也是一条基本约束：先用能满足信息需求的最便宜来源，付费只在没有免费可选方案时才考虑。见下节。

## 免费优先（硬性规则）

- 每个数据源、每个功能，先找免费方案；没有任何免费可选方案时，才考虑付费方案，并写清楚为什么免费方案不行。
- 默认只启用免费 / 免费额度内的来源：RSS、GDELT、Reddit、YouTube 免费额度。X 按付费处理，默认关闭；TikTok 默认关闭，只留官方授权占位。
- AI 分析也一样：先用规则 + 本地方法（去重、评分、聚类），付费 LLM 只在没有免费 / 本地方案能满足需求时才接入，代码里目前没有接入任何付费 LLM。
- 托管也一样：GitHub 免费额度 + 本地电脑，不需要先买服务器。

目标不是收集最多的信息，而是尽可能早地发现真正有传播潜力的信息：哪个话题过去 15 分钟增长最快、增长是否在加速、是否正在跨平台扩散。

## 工程边界（重要）

所有平台都接入，不代表可以免费、无限量地读取所有平台：

- RSS、GDELT：低成本基础数据源，无需凭证。
- Reddit：配置 API 凭证后采集（官方 OAuth）。
- YouTube：配置 Data API Key 后采集（默认配额每天 10,000 units）。
- X：配置有权限的官方 API 后采集，按付费服务处理，不假设存在普通开发者长期免费读取途径。
- TikTok：预留官方授权数据接口适配器（stub），不使用绕过访问限制的爬虫。

没有配置凭证的平台会显示为“未启用”，而不是假装已经采集到数据。

## 项目结构

```
global_trend_radar/
├── app.py                  # 可视化 Dashboard（Streamlit）
├── run_scheduler.py        # 15 分钟定时调度
├── config.yaml             # 数据源和评分配置
├── .env.example            # API 密钥模板（复制为 .env 后填写）
├── requirements.txt
├── README.md
├── .gitignore
└── .github/workflows/ci.yml  # CI：编译 + 单元测试 + --once 冒烟扫描
├── data/
│   └── trends.sqlite3      # 历史数据库（首次运行自动生成）
└── trend_radar/
    ├── collector.py        # 采集编排：去重、评分、入库、快照
    ├── scoring.py          # 热度评分：增长速度优先
    ├── db.py               # SQLite：内容 + 历史快照
    ├── models.py           # 统一数据模型
    └── adapters/
        ├── rss.py
        ├── gdelt.py
        ├── reddit.py
        ├── youtube.py
        ├── x_api.py
        └── tiktok.py       # 预留 stub，不采集
```

## GitHub（代码的唯一来源）

代码以 GitHub 为准：后续开发、改动和版本都在 GitHub 上进行，本地只是运行环境。

- GitHub 是代码的来源（source of truth）；Windows 或 macOS 本地电脑是真正每 15 分钟运行扫描的地方。
- CI（`.github/workflows/ci.yml`）只验证代码：安装依赖、编译检查、跑评分单元测试、跑一次 `--once` 冒烟扫描。它不保存 SQLite 历史数据——GitHub Actions 的运行环境是临时的，每次跑完就销毁，历史快照只存在于本地 `data/trends.sqlite3`。
- 真实 API 密钥绝不进仓库；CI 如需使用，请用 GitHub Secrets（例如 `REDDIT_CLIENT_ID`、`YOUTUBE_API_KEY`、`X_BEARER_TOKEN`），本地则用 `.env`。
- `.gitignore` 已排除 `.env`、`venv/`、`__pycache__/`、`data/*.sqlite3`、`.streamlit/`。

## Windows 安装与运行（PowerShell）

```powershell
# 1. 进入项目目录，创建虚拟环境
python -m venv venv
.\venv\Scripts\Activate.ps1

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置凭证（可选；不配置则只跑 RSS + GDELT）
copy .env.example .env
# 用记事本打开 .env，填写需要的 Key

# 4. 检查代码能否通过编译（已修复 scoring.py 的换行语法错误）
python -m compileall trend_radar

# 5. 运行单元测试
python -m unittest discover -s tests -v

# 6. 启动定时采集（每 15 分钟一轮，先立即跑一轮）
python run_scheduler.py

# 只跑一轮就退出（冒烟测试，不启动循环）：
python run_scheduler.py --once
```

另开一个 PowerShell：

```powershell
.\venv\Scripts\Activate.ps1
streamlit run app.py
```

然后在浏览器打开 Streamlit 显示的本地地址查看 Dashboard。

## macOS 安装与运行（终端 / zsh）

代码本身不依赖 Windows 专属接口（已检查：无 `win32`、`ctypes`、Windows 路径硬编码），同一份代码可以在 macOS 上运行，只是激活虚拟环境和复制文件的命令不同：

```bash
# 1. 进入项目目录，创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置凭证（可选；不配置则只跑 RSS + GDELT）
cp .env.example .env

# 4. 检查代码能否通过编译
python3 -m compileall trend_radar

# 5. 运行单元测试
python3 -m unittest discover -s tests -v

# 6. 启动定时采集（每 15 分钟一轮，先立即跑一轮）
python3 run_scheduler.py

# 只跑一轮就退出（冒烟测试）：
python3 run_scheduler.py --once
```

另开一个终端：

```bash
source venv/bin/activate
streamlit run app.py
```

macOS 注意：系统偏好设置里关闭“显示器关闭后电脑进入睡眠”，或在运行时用 `caffeinate` 保持唤醒；长期运行也可以考虑用 `launchd` 做开机自启（不在第一版范围内）。

## 评分方法

权重（初始假设，需用真实数据校准）：增长速度 30%、传播规模 20%、增长加速度 20%、跨平台传播 20%、内容新颖性 10%。

示例：A 1000 万播放、过去 1 小时新增 1 万；B 30 万播放、新增 20 万；C 5 万播放、新增 4 万。B 和 C 的增长速度高于 A，更值得关注。不同平台的互动口径不同，先在平台内部标准化，再跨平台综合评分。

每次采集都会保存互动量快照到 `snapshots` 表，才能算出增长速度和加速度；只看单次总量无法判断爆款。

## 注意事项

- 电脑必须保持开机、不进入睡眠，否则定时任务无法执行；长期稳定运行可考虑 NAS 或低功耗小主机。
- API 的可用数据、商业使用许可、调用成本和频率限制，请按各平台当前条款核实。
- `.env` 不要提交到公开仓库。
