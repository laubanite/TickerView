# AlphaPrism

A股中长线 **AI 辅助**交易系统。规则引擎算确定性信号(分型→笔→结构),LLM 只做解读;**不自动交易**,所有下单由人执行。

设计文档:[设计方案.md](设计方案.md)

## 环境(本机)

- Python:**`D:\Anaconda\python.exe`**(3.12.4)
- 依赖:akshare、pandas、pyyaml、requests —— Anaconda base 已内置
- ⚠️ 命令 `python` 是 Windows 商店占位符,**一律用完整路径**,不要用 `python`

## 目录

```
config/settings.yaml        主配置(跟踪池、数据源开关、复权、新闻关键词、LLM 模型表)
config/settings.local.yaml  本地私有配置(API key / webhook,已 gitignore ⚠️严禁提交)
alphaprism/                 核心模块(config/db/fetchers/signals/pipeline/report/morning/monitor/push/llm)
alphaprism/planner/         计划层(里程碑1):作战地图 → RuleModel 解析器(§4.1)
scripts/                    入口脚本(含 3 个计划任务入口)
docs/                       设计文档、量价规则表、盘前简报流程、产品方案
tests/                      回归测试(解析器等)
data/alphaprism.db          SQLite 数据库(已 gitignore)
reports/                    盘后简报 / 盘前简报 md(已 gitignore)
```

## 运行

**CLI(推荐,`bin/` 已加入用户 PATH,任意终端直接敲 `alphaprism`;PowerShell / cmd 走 `alphaprism.bat`,Git Bash 走 `alphaprism`):**

```bash
alphaprism --help                        # 帮助
alphaprism status                        # 数据状态/健康检查
alphaprism daily                         # 抓取全池 + 生成日报
alphaprism daily 516020 515050           # 只抓指定
alphaprism report                        # 仅读库生成日报(不抓取)
alphaprism pool                          # 查看跟踪池(config/watchlist.yaml)
alphaprism pool add 512880               # 添加(自动取名)
alphaprism pool add 512880 证券ETF 行业   # 添加(指定名称/分类)
alphaprism pool remove 512880            # 移除
alphaprism signal                        # 全池结构 + 当前信号(可跟多只代码)
alphaprism signal 516020 515050          # 指定多只
alphaprism backtest                      # 全池回测(默认),单只给完整明细
alphaprism sector                        # 板块成交占比(同花顺,实时抓取+入库+展示)
alphaprism valuation                     # 板块估值体检(同花顺,成分股中位PE/PB vs 沪深300;月度定池用)
alphaprism catalyst                      # 催化剂上下文(同花顺异动+热榜,按板块归属;LLM 解读用)
alphaprism log                           # 信号日志(§6.4):list/result/backfill,交易日后自动记录
alphaprism log result 123 止盈 进场0.63离场0.75   # 记录某信号实际结果
alphaprism log backfill                  # 重建信号日志(清空重算全历史)
alphaprism push 测试消息                  # 推送测试(push 未启用时 dry-run 打印)
alphaprism morning [--dry-run]           # 盘前简报(新闻+LLM催化状态→推送;--dry-run 只生成)
alphaprism monitor                       # 盘中监测跑一轮(非交易时段跳过)
alphaprism wind financial_docs get_financial_news query=近三日光模块板块新闻 top_k=5   # 财经新闻深挖
alphaprism wind stock_data get_stock_price_indicators windcode=600519.SH indexes=最新成交价
alphaprism battlemap                        # 作战地图 → RuleModel 解析(里程碑1,默认摘要)
alphaprism battlemap <path> --json          # 指定地图路径,输出完整结构化 JSON
alphaprism backtest-map                     # 作战地图回测(里程碑2):按整套可计算规则跑历史区间
alphaprism backtest-map --start 2025-08-01 --end 2026-08-01 --capital 17300
alphaprism check                            # 盘中核对(里程碑3):RuleModel×实时行情→结论词5档+大盘门控
alphaprism check --json                     # 输出完整核对 JSON(gate + 各标的 verdict)
alphaprism close                            # 盘后生成(里程碑4):收盘对账+复盘五问+纪律评分(只打印)
alphaprism close --write                    # 生成并追加写入作战地图"每日盯盘记录"
alphaprism playbook                         # 盘前生成(里程碑2):剧本草稿→人工确认(默认只打印)
alphaprism playbook --write                 # 草稿确认后追加写入作战地图"每日盯盘记录"
```

> PATH 已在用户环境变量添加 `E:\AgentProjects\AlphaPrism\bin`,**新开终端生效**。

直接跑 Python 脚本(等价):

```powershell
# 1. 初始化数据库(建表)
D:\Anaconda\python.exe scripts\init_db.py

# 2. 每日抓取 + 生成日报
D:\Anaconda\python.exe scripts\run_daily.py
# 只抓单只
D:\Anaconda\python.exe scripts\run_daily.py 510300
```

## Web 行情页(里程碑6)

Flask + Vue 3(CDN)+ ECharts,复用 Python 引擎。

### 启动(必须先把服务跑起来)
Web 服务**不会常驻**——浏览器要能访问,必须先启动服务。两种方式:

**方式一(推荐):双击一键启动**
双击项目根目录的 `启动行情页.bat`,自动启动服务并打开浏览器。关闭该窗口即停止。

**方式二:命令行**
```powershell
alphaprism web        # 启动并自动打开浏览器(端口 8765)
alphaprism panel      # 悬浮面板(里程碑7):pywebview 置顶小窗(状态灯+结论词)
```

> ⚠️ 服务未运行时,浏览器访问 `http://127.0.0.1:8765` 会**无法访问**——这是正常的,先启动服务即可。
> 端口默认 8765(冷门,避开 5000 等常用默认端口);可用环境变量 `ALPHAPRISM_WEB_PORT` 覆盖。

打开 `http://127.0.0.1:8765`:

- **指数区**:上证/深成/创业板实时快照(同花顺)
- **自选股**:实时行情 + 换手率,点击切换图表;可增删(§5.6,同步 watchlist.yaml),区分"计划内 / 无计划"状态灯
- **图表区**:日K / 30分K(**蜡烛图** + 均线 MA5/10/20/60/120/250 + **速查卡价位叠线**:买区/突破点/减仓红线/生命线)/ **分时**(均价线);副图成交量(**MA5量/MA10量**,轴标签 万/亿 格式化);图表上方**行情指标条**:现价/涨跌/今开高低/量比/换手/成交量/成交额/5日与10日均量
- **三 tab 侧栏**(markdown 已渲染):
  - **盘前**(里程碑2,三块联动):①**隔夜重要消息**(新浪7x24 实时抓取 + LLM 提取分类 🔴利空/🟢利好/⚪中性) ②**今日剧本**(消息面催化→隔夜影响/今日动作,可编辑后写入作战地图) ③**盘前预案**(若…则… 逐只) + 今日纪律 + 持仓卡
  - **盘中**(里程碑3):实时核对表(结论词5档+大盘门控) + 盘中快照(规则事实+LLM解读+推手机,§5.4)
  - **盘后**(里程碑4):收盘对账(规则事实+LLM复盘五问+纪律评分)
- **作战地图**:RuleModel 全量渲染,按章节折叠
- **回测**:作战地图整套可计算规则跑历史区间,净值曲线 + 交易明细
- **悬浮面板**:`alphaprism panel`(里程碑7):pywebview 置顶小窗,状态灯 + 结论词,15 秒自动刷新,一键生成盘中快照
- **大盘门控 J 值**:标准 9 日 KDJ(腾讯日线,取 250 根收敛),界面标注「截至 X 收盘」避免与盘中实时 J 混淆

> 分时数据源:`web.ifzq.gtimg.cn/appstock/app/minute/query`(腾讯,2026-08-19 实测;
> 每点 时间/价格/累计量/累计额,均价 = 累计额/(累计量×100))。`fetch_minute` 见
> `alphaprism/fetchers/etf_kline.py`,测试 `tests/test_fetch_minute.py`(网络依赖,断网自动跳过)。

## 调度(Windows 计划任务)

三条推送线(均已注册,登录后运行;日志写 `data/*.log`,UTF-8):

| 任务 | 时间 | 脚本 | 内容 |
|---|---|---|---|
| **AlphaPrismMorning** | 每日 8:55(9:15 前) | `run_morning.bat` | 盘前简报:新浪7x24新闻+同花顺异动 → LLM 催化状态(综述/情景/关注) |
| **AlphaPrismMonitor** | 每 15 分钟 | `run_monitor.bat` | 盘中监测:非交易日/时段自动跳过,四类买卖点触发即推(规则+LLM 解读) |
| **AlphaPrismDaily** | 每日 17:00 | `run_daily.bat` | 抓取 → 信号日志 → 盘后简报(数据表+LLM 小结)→ 推送 |

```powershell
# 注册 / 删除 / 查询 / 立即运行
schtasks /create /tn "AlphaPrismMorning" /tr "E:\AgentProjects\AlphaPrism\scripts\run_morning.bat" /sc daily /st 08:55 /f
schtasks /create /tn "AlphaPrismMonitor" /tr "E:\AgentProjects\AlphaPrism\scripts\run_monitor.bat" /sc minute /mo 15 /f
schtasks /create /tn "AlphaPrismDaily"    /tr "E:\AgentProjects\AlphaPrism\scripts\run_daily.bat"    /sc daily /st 17:00 /f
schtasks /delete /tn "AlphaPrismMorning" /f
schtasks /query  /tn "AlphaPrismMorning"
schtasks /run    /tn "AlphaPrismMorning"
```

> **运行前提(三个任务均为"仅交互式登录")**:需 Windows 已登录(`Iolite` 账号)——锁屏/屏保仍算登录,**照常运行**;停在登录界面或关机/睡眠则不运行。
> **错过补跑已开启**(StartWhenAvailable):如 17:00 时电脑关机,下次开机登录后会补跑当天盘后;早间同理。
> **盘后 17:00 的数据对齐**:已实测同花顺收盘后 ~1.5 小时(16:32)即对齐当日,17:00 各数据源均就绪。
> 手动运行:`schtasks /run /tn AlphaPrismDaily`

## 数据源

- **K线(日/30分/周)**:腾讯行情接口直连(日线前复权 qfq、30分 K;新浪补当日 bar)。
  东方财富 push2*(akshare 主要源)在本机被 WAF 拦截,该路径不可用
- **成交额 / 换手率**:Wind 补充(`sources.wind_amount`,经 wind-mcp-skill 的
  `fund_data.get_fund_kline`,全历史;每只每轮 1 次调用,计入每日 1000 积分)。
  注:baostock 已评估弃用(ETF 仅保留近 ~7 个月,全历史不可用);东财 K线被 WAF 拦截
  (requests 与 curl 均被断连)
- **板块成交占比**(`sources.fuyao`,同花顺金融数据 API):板块成交额 / 全市场成交额,
  用于板块拥挤度(≥15% 警戒、≥20% 强警示)。key 在 `config/settings.local.yaml` `fuyao.api_key`;
  板块→同花顺行业指数映射在 `config/settings.yaml` `fuyao.sector_map`。
  命令:`alphaprism sector`(实时抓取+入库+展示)。池内新增 ETF 需补 sector_map 一条。
- **板块估值体检**(同花顺估值快照):行业指数成分股中位数 PE/PB vs 沪深300 基准,
  相对读数(便宜/中性/偏贵),支撑月度定池子(阶段 2.1)。命令:`alphaprism valuation`;
  每日快照入库 `sector_valuation`,持续累计后可用作估值分位。**注意**:基准为沪深300(偏大盘价值),
  成长板块系统性显偏贵;且为月度低频工具,勿每日跑。
- **催化剂(消息面,阶段4)**:同花顺当日异动/热榜(`alphaprism catalyst`,确定性数据层)+
  新浪 7x24 新闻(`fetchers/news.py`,盘前过滤)。日报「六、消息面」区展示板块异动/热榜;
  催化状态由 LLM 判(增强/减弱/新增/未变),产盘前简报,流程见 `docs/盘前简报流程.md`。
- **盘中实时数据(阶段5)**:同花顺场内 ETF 快照(实时价/量/换手率,`fuyao.fetch_fund_snapshot`)
  + 腾讯 30 分 K(盘中结构)。交易日历:同花顺 `calendar/trading-days`(近一年,进程内缓存)。
- **新闻(盘前)**:新浪 7x24 快讯(免费可用)。财联社/网易/腾讯/雪球均被反爬签名拦(同东财性质),
  故只用新浪,关键词过滤表在 `settings.yaml` `news.keywords`。
- **Wind AIFinMarket**(每日 1000 积分):行业信息 / 宏观 —— 经 **Wind MCP skill** 访问
  (已全局安装 wind-mcp-skill / wind-find-finance-skill 并验证,agent 通道,非管线);
  key 在全局配置 `%USERPROFILE%\.wind-aifinmarket\config`
- **ETF 份额/规模**:暂缓(数据源待定,见 `alphaprism/fetchers/etf_fund.py`)

## 推送与信号日志(阶段5)

**三线推送,规则+LLM 混合**(飞书已启用加签;计划任务见上文「调度」):

| 线 | 时间 | 内容 | LLM 角色 |
|---|---|---|---|
| 盘前 | 8:55 | 新闻(新浪7x24)+异动 → 催化状态(增强/减弱/新增/未变)+综述/情景/关注 | LLM 写全简报 |
| 盘中 | 每15分钟 | 日线+30m+量能三重触发(买点确认/回踩确认/买点失效/卖点确认) | 规则触发+LLM 补解读 |
| 盘后 | 17:00 | 数据简报 + 信号日志 | LLM 今日小结 |

- **LLM(催化判定/解读/小结)**:多免费模型自动切换(配额用尽/失败换下一个),
  顺序见 `settings.yaml` `llm.profiles`(智谱 GLM → 硅基流动 DeepSeek → OpenRouter 兜底)。
  ⚠️ **key 敏感**:全部在 `settings.local.yaml` `llm` 段(已 gitignore),严禁回显/外传/提交。
- **信号日志**:每个交易日自动记录信号 出现/激活/失效(`signal_log` 表,首次 daily 自动回填全历史)。
  `alphaprism log` 查看,`alphaprism log result <ID> <止盈/止损/持有/放弃/回避/踏空> [备注]` 记录实际结果;
  长期坚持是评估系统有效性的唯一客观依据(§6.4)。
- **推送**:`alphaprism/push.py` 支持 **飞书 / 企业微信 / Server酱** 三通道。
  `alphaprism push 测试消息` 验证(push 未启用时 dry-run 打印消息格式)。

## 阶段

| 阶段 | 状态 |
|---|---|
| 1 数据管线 + 日报 | ✅ |
| 2 信号引擎 + 回测 | ✅ |
| 3 量价规则进引擎 | ✅(7 条书规则已编码,见 docs/volume-price-rules.md) |
| 4 消息面 LLM | ✅(异动/热榜 + 新闻 + 催化状态) |
| 5 推送 + 信号日志 | ✅(三线推送,规则+LLM 混合) |
| 6 计划层(产品方案 v0.1.0) | ✅ 里程碑1 解析器、里程碑2 作战地图回测/盘前剧本草稿、里程碑3 盘中核对(引擎+快照)、里程碑4 盘后生成、里程碑6 Web 行情页、里程碑7 悬浮面板已落地(见 alphaprism/planner/ + backtest_map.py) |
| 7 模拟盘 1~2 个月 → 小资金实盘 | ⏳ 待启动 |

阶段 1→5 已落地;阶段 6(产品方案 v0.1.0)里程碑1 解析器与里程碑2 作战地图回测已上线。回测引擎(`alphaprism backtest-map`)按作战地图整套可计算规则跑历史区间,输出收益率/最大回撤/与买入持有对比/交易明细。

> ⚠️ **回测定位与局限**:测的是"规则骨架"(可计算部分),**不含消息/情绪信息层**——信息层有效性由模拟盘逐日评估。且关键位取作战地图当前值(静态),对**近期区间**(作战地图实际使用期)最有效;越往历史推,静态关键位越失真。
