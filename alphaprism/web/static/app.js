/* TickerView 行情页前端(里程碑6)· Vue 3 + ECharts */
const { createApp } = Vue;

// 浮窗模式(?float=1):pywebview 桌面分身加载时,只渲染悬浮面板、隐藏主界面壳
const FLOAT_MODE = new URLSearchParams(location.search).has("float");
// 不透明深色窗口:窗口=卡片尺寸(无透明边);卡片铺满窗口,圆角由宿主 DWM 裁
const FP_MARGIN = 0;

const api = (path) => fetch(path).then((r) => r.json());

const app = createApp({
  data() {
    return {
      tab: "pm",            // pm 盘前 | pz 盘中 | ph 盘后
      view: "",             // "" | map | bt
      period: "day",        // day | m30 | min
      indices: [],
      watchlist: [],
      current: "",
      currentQuote: null,
      currentName: "",
      battlemap: null,      // RuleModel dict
      mapFile: "",
      // 回测
      btStart: "2025-08-01",
      btEnd: "2026-08-01",
      btCapital: 17300,
      bt: null,
      btError: "",
      btLoading: false,
      chart: null,
      btChart: null,
      infoChart: null,
      infoStats: null,
      // 盘中技术快照(2026-08-22,与作战地图解耦)
      techCode: "",           // 当前选中的快照标的
      // 盘后建议验证(增量4 MVP:懒验证 + 日历,零任务零推送)
      vcDays: [],            // [{trade_date, items:[{symbol,name,anchor_price,state_word,category,signal_type,summary,degraded,out3,move3,out5,move5}]}]
      vcError: "",
      vcLoading: false,
      vcOpenDay: "",         // 展开详情的日期
      vcLoaded: false,       // 已拉取过(切 tab 不再重复触发)
      verifiedNote: "",      // 本次补判条数提示(如"本次补判 N 条到期建议")
      // 盘前生成(里程碑2)
      pbDraft: null,
      pbMarkdown: "",
      pbError: "",
      pbLoading: false,
      pbSaving: false,
      pbSaved: "",
      // 盘前视图(实时新闻 + LLM 提取,三块联动)
      mgNews: null,          // {summary, items:[{time,text,impact,sector,reason,source,pm_tag}]}
      mgHealth: null,        // {fetched_at, stale, latest_ts, all_failed, sources:[{source,ok,rows,error}]}
      mgState: null,         // 市场状态卡(T1 盘前定级,盘前方案 v3.3):{level,name,hint,evidence,per_symbol,bonus,at}
      mgPlaybook: [],        // [{code,name,overnight,action}] 唯一可编辑真源
      mgDiscipline: [],      // [str] 今日纪律(消息面催化驱动)
      mgError: "",
      mgLoading: false,
      mgSaved: "",
      // 自选股管理(§5.6)
      newSym: "",
      sugOpen: false,        // 标的搜索联想下拉(同花顺式:名称/拼音/代码)
      sugItems: [],          // 候选 [{symbol,name,market,kind,pinyin}]
      sugActive: -1,         // 键盘高亮行
      sugTimer: null,        // 输入防抖
      sugSeq: 0,             // 响应竞态防护:只认最后一次击键的响应
      // 设置(重构为居中弹窗 Modal · 2026-08-29):左侧导航(模型/持仓/通用)+ 右侧内容区
      settingsOpen: false,    // 弹窗开关(右上角「设置」按钮;X/遮罩/Esc 关闭)
      settingsPane: "model",  // 当前面板键: model | holdings | general
      // 模型面板:LLM 配置可编辑表单(/api/llm 读写)
      llmProfiles: [],        // [{uid,provider,model,base_url,has_key,testState}] 编辑列表
      llmKeys: [],            // [{provider,set,masked,input,visible,plain}] API Key 卡片
      llmDefaultUrls: {},     // provider -> 默认 Base URL(/api/llm 返回)
      llmError: "",
      llmSavedAt: "",
      llmSaving: false,
      editingUid: "",         // 正在行内编辑的 uid
      editingRow: null,       // {provider,model,base_url,key,keyVisible,hasKey}
      llmTesting: {},         // uid -> bool 测试中
      llmTestMsg: {},         // uid -> {ok,text}
      testMsg: null,          // 编辑表单内测试结果 {ok,text}
      testLoading: false,
      dragUid: "",            // 拖拽排序:被拖行的 uid
      dragOverUid: "",        // 拖拽排序:当前悬停目标行 uid
      advOpen: false,         // 高级配置说明卡片展开
      advMsg: "",
      _uidSeed: 0,
      // 设置(MVP):持仓卡 + 账户(架构 §八)
      holdings: [],
      account: { total_capital: "", cash: "" },
      settingsMsg: "",
      acctSaved: "",
      acctSaving: false,
      holdForm: { symbol: "", cost: "", quantity: "", status: "持仓" },
      // 设置:刷新间隔(秒,顶部指数 + 候选股池轮询;存 config/web.yaml)
      refreshSec: 12,
      refreshSaved: "",
      refreshSaving: false,
      _pollTimer: null,
      _started: false,
      wlMsg: "",
      wlError: "",
      // 盘中技术快照两张卡(2026-08-22):数据快照(确定性)+ 深入分析(LLM),完全独立
      // 2026-09-04 盘中模式切换:ETF 模式 / 个股模式(风险解读),标签过滤 chips 与按钮
      snapMode: "etf",        // 'etf' | 'stock'(仅 UI 状态,类型判定以后端 _snapshot_kind 为准)
      snapFacts: "",          // 卡1 数据快照(程序计算,不经过 LLM)
      snapFactsError: "",
      snapFactsLoading: false,
      snapAt: "",             // 最近一次快照数据时点(HH:MM)
      snapAnalysis: "",       // 卡2 深入分析(LLM 多周期矛盾推演)
      snapAnalysisError: "",
      snapAnalysisLoading: false,
      snapAnalysisAt: "",     // 深入分析基于的数据时点(HH:MM,30s 缓存/自动刷新)
      snapSignal: null,       // 一句话信号(确定性,规则引擎,与盘后验证同源)
      cfMarkdown: "",         // 卡3 反事实推演(盘后·情景分支,手动触发,非建议)
      cfError: "",
      cfLoading: false,
      snapError: "",          // 推送错误
      snapPushing: false,
      dayRows: [],      // 当前标的日K rows(供量比/均量/叠线计算)
      // 悬浮面板(里程碑7):markers[code] = facts 接口返回的确定性 panel 段
      markers: {},          // { code: {status_word, scenario, risk, anchors, ok, updated_at} }
      markersTime: {},      // { code: "HH:MM" } 最近一次成功生成时间
      refreshCode: "",      // 正在单只生成快照的标的
      floatOnlyHeld: false, // 「仅持仓」过滤(显示层,存 localStorage)
      floatCols: { marker: true, chg: true, turn: false, vol: false, amt: false },
      floatMode: false,     // 浮窗模式(?float=1):仅渲染悬浮面板
      panelStartHidden: false, // 托盘:启动即藏入系统托盘(存 config/web.yaml)
      autoStart: false,       // 开机自启(Windows 启动项)
    };
  },

  computed: {
    settingsNav() {
      // 设置弹窗左侧垂直导航(点击切换右侧内容区)
      return [
        { key: "model", label: "模型" },
        { key: "holdings", label: "持仓" },
        { key: "general", label: "通用" },
      ];
    },
    floatColDefs() {
      // 可选列定义+顺序(标记默认开;涨跌幅默认开;换手=换手率/量/额默认关)
      return [
        { key: "marker", label: "标记" },
        { key: "chg", label: "涨跌" },
        { key: "turn", label: "换手" },
        { key: "vol", label: "量" },
        { key: "amt", label: "额" },
      ];
    },
    floatWidth() {
      // 自适应宽度:按展示指标列数缩放(仅2列→窄,3列→中,全开→宽)
      // 固定部分:持标签槽24 + 名称68 + 现价48 + 左右内边距24 = 164
      // 可变部分:每开一列各加其列宽;标记列按 84 估算
      const w = { chg: 48, turn: 40, vol: 42, amt: 46 };
      let width = 164;
      ["chg", "turn", "vol", "amt"].forEach((k) => { if (this.floatCols[k]) width += w[k] + 4; });
      if (this.floatCols.marker) width += 84 + 4;
      return Math.min(Math.max(width, 250), 460);
    },
    floatRows() {
      // 面板展示行 = 自选股池(唯一真源);「仅持仓」过滤为显示层,不新增数据源
      const rows = this.floatOnlyHeld
        ? this.watchlist.filter((w) => this.holdingOf(w.symbol))
        : this.watchlist;
      return rows;
    },
    floatShown() {
      return this.floatRows.length;
    },
    floatUpdated() {
      // 面板整体最近更新时间 = 所有已生成标记的最新时间
      const times = Object.values(this.markersTime);
      return times.length ? "已更新 " + times.reduce((a, b) => (a > b ? a : b)) : "";
    },
    idxDanger() {
      return false; // 大盘破位判定后续里程碑接入
    },
    gateText() {
      const g = this.bm?.market_gate || {};
      if (!g.conclusion) return "—";
      return g.conclusion;
    },
    bm() {
      // 返回全默认结构,避免模板深层访问抛错(如 battlemap 未加载时)
      const m = this.battlemap || {};
      const g = m.global_ || {};
      return {
        market_gate: m.market_gate || {},
        instruments: m.instruments || [],
        daily: m.daily || { playbook: [] },
        unresolved: m.unresolved || [],
        global_: g,
      };
    },
    discipline() {
      // 优先用盘前视图的「今日纪律」(消息面催化驱动);未生成时退回作战地图 §六
      return this.mgDiscipline.length ? this.mgDiscipline : (this.bm?.global_?.discipline || []);
    },
    positions() {
      return (this.bm?.instruments || [])
        .filter((i) => i.position && i.position.cost != null)
        .map((i) => ({ code: i.code, name: i.name, ...i.position }));
    },
    mgHealthText() {
      // 数据健康 · 标题行:正常态显示"HH:MM 更新 · 新浪N条/东财N条"(单源失败只剩可用源)
      const h = this.mgHealth;
      if (!h) return "";
      if (h.all_failed || h.stale) return "";   // 异常态交给空状态文案
      const ok = (h.sources || []).filter((s) => s.ok);
      const t = (h.fetched_at || "").slice(11, 16);
      const parts = ok.map((s) => `${s.source === "sina" ? "新浪" : "东财"}${s.rows}条`);
      return (t ? t + " 更新 · " : "") + (parts.join("/") || "无数据");
    },
    newsEmptyText() {
      // 空状态一行诚实文案:数据源不可用 / 数据陈旧 / 真无消息
      const h = this.mgHealth;
      if (h && h.all_failed) return "数据源不可用 · 暂无实时消息";
      if (h && h.stale) return `最新消息 ${(h.latest_ts || "").slice(0, 16)} · 数据未更新`;
      return "暂无重要消息";
    },
    mgNewsMerged() {
      // 同板块同利好/同利空消息合并展示;相同 reason 去重,消除信息冗余
      const items = this.mgNews?.items || [];
      const groups = new Map();
      for (const it of items) {
        const key = (it.sector || "其他") + "|" + (it.impact || "中性");
        const g = groups.get(key);
        if (g) {
          g.texts.push(it.text);
          if (it.reason && !g.reasons.includes(it.reason)) g.reasons.push(it.reason);
        } else {
          groups.set(key, { ...it, texts: [it.text], reasons: it.reason ? [it.reason] : [] });
        }
      }
      return [...groups.values()].map((g) => {
        const { texts, reasons, ...rest } = g;
        return { ...rest, text: texts.join("；"), reason: reasons.join("；") };
      });
    },
    todayStr() {
      return this.mgNews?.date || new Date().toISOString().slice(0, 10);
    },
    // 盘后验证:当前展开日期的建议明细
    vcDayItems() {
      const d = this.vcDays.find((x) => x.trade_date === this.vcOpenDay);
      return d ? d.items : [];
    },
    // 一句话信号两行拆分:第一句=现价·状态;其余=动作链(前端展示用,内容仍为确定性)
    l1Lines() {
      const s = this.snapSignal && this.snapSignal.one_sentence;
      return this.splitSentence(s);
    },
    levelLines() {
      const ins = this.currentInstrument();
      if (!ins) return [];
      const map = {};
      (ins.levels || []).forEach((l) => { map[l.name] = l.price; });
      const out = [];
      const ln = (name, color, type = "dashed") => {
        if (map[name] != null) out.push({ name: name, value: map[name], color, type });
      };
      ln("买区下沿", "#ffd166", "dashed");
      ln("买区上沿", "#ffd166", "dashed");
      ln("突破点", "#4c8dff", "dotted");
      ln("减仓红线", "#ff9f6e", "solid");
      ln("生命线", "#ff5c6c", "solid");
      return out;
    },
    quoteMetrics() {
      // 行情指标条:实时快照 + 日K 量能(量比/MA5量/MA10量)
      const q = this.currentQuote || {};
      const vol = this.dayRows.map((r) => r.v || 0);
      const n = vol.length;
      const avg = (arr) => (arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null);
      const lastVol = n ? vol[n - 1] : null;
      const avg5prev = n >= 6 ? avg(vol.slice(-6, -1)) : n > 1 ? avg(vol.slice(0, -1)) : null;
      const volRatio = lastVol != null && avg5prev ? lastVol / avg5prev : null;
      return {
        price: q.last_price, chg: q.price_change_ratio_pct,
        open: q.open_price, high: q.high_price, low: q.low_price,
        volume: q.volume != null ? q.volume / 100 : null,   // 股→手
        amount: q.turnover,
        turnover: q.turnover_ratio_pct,
        volRatio,
        ma5v: n >= 5 ? avg(vol.slice(-5)) : null,
        ma10v: n >= 10 ? avg(vol.slice(-10)) : null,
      };
    },
    snapChips() {
      // 盘中快照 chips 按模式过滤:watchlist 显式 type 优先,否则代码前缀(5/1=ETF)
      const inMode = (w) => {
        const t = w.type || ((/^5|^1/.test(String(w.symbol)) && String(w.symbol).length === 6) ? "etf" : "stock");
        return t === this.snapMode;
      };
      return this.watchlist.filter(inMode);
    },
    latestMorningEntry() {
      // 最新一条盘前盯盘记录(含隔夜重要消息),按日期倒序取第一条
      const entries = this.bm?.daily?.journal || [];
      return entries
        .filter((e) => (e.kind || "").includes("盘前") || (e.content || "").includes("盘前"))
        .sort((a, b) => (b.date || "").localeCompare(a.date || ""))[0] || null;
    },
    playbookRows() {
      return this.bm?.daily?.playbook || [];
    },
  },

  methods: {
    currentInstrument() {
      return (this.bm?.instruments || []).find((i) => i.code === this.current) || null;
    },
    hasPlan(sym) {
      const ins = (this.bm?.instruments || []).find((i) => i.code === sym);
      return !!(ins && ins.levels && ins.levels.length);
    },
    setTab(t) { this.tab = t; this.view = ""; if (t === "ph") this.loadVerifyCalendar(); },
    toggleView(v) {
      this.view = this.view === v ? "" : v;
      if (this.view === "bt") this.initBtChart();
      else if (this.view === "map") this.loadBattlemap();
      else if (this.view === "info") this.loadNewsStats();
    },

    // ---- 格式化 ----
    fmt(v, d = 3) {
      if (v == null || isNaN(v)) return "-";
      return Number(v).toFixed(d);
    },
    pct(v) {
      if (v == null || isNaN(v)) return "-";
      const n = Number(v);
      return (n > 0 ? "+" : "") + n.toFixed(2) + "%";
    },
    pxCls(v) {
      if (v == null || isNaN(v)) return "flat";
      return Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : "flat";
    },
    shortName(name) {
      // 名称智能化简:去粒子(ETF/LOF/基金/指数/联接/公司名/交易通道),保留主题词原意。
      // 只在剥离粒子后仍超长时,用主题缩写对照表/去描述词进一步收窄;
      // 绝不补省略号——完整原名字段由 name 的 title 兜底。
      if (!name) return "";
      let s = String(name)
        // 1) 后缀/结尾粒子(顺序:先含代码的括号,再 ETF/LOF/基金/指数/联接)
        .replace(/[（(]?\d{6}[）)]?/g, "")
        .replace(/(?:ETF|LOF)/gi, "")
        .replace(/(?:基金|指数|联接|LOF)$/i, "")
        .replace(/(?:$|·)(?:精选|优选|龙头|增强|量化|平滑|主动)$/i, "")
        // 2) 交易通道域名合并:港股通→港股 / 深港通→深股 / 沪港通→沪股
        .replace(/港股通/g, "港股")
        .replace(/深港通/g, "深股")
        .replace(/沪港通/g, "沪股")
        // 3) 基金公司名(CN ETF 常作后缀;中外合资带 · )
        .replace(/(?:华泰柏瑞|国泰|华夏|嘉实|易方达|南方|广发|富国|华宝|天弘|汇添富|博时|招商|银华|鹏华|华安|平安|工银|建信|景顺长城|中欧|万家|前海开源|富国|大成)/g, "")
        .replace(/\s+/g, "");
      if (!s) return "";
      // 4) 已 ≤ 6 字符 → 直接返回(无省略号;6 字如"半导体设备"是完整主题词,保留)
      if (s.length <= 6) return s;
      // 5) 仍超长 → 主题缩写对照表(手维护,来源:常见行业/主题 ETF 简称)
      const abbr = {
        "港股创新药": "港股创新药",
        "中证港股通创新药": "港股创新药",
        "国证半导体芯片": "半导体芯片",
        "中华半导体芯片": "半导体芯片",
        "国证半导体设备": "半导体设备",
        "科创创业50": "科创创业",
        "人工智能": "人工智能",
        "中证人工智能": "人工智能",
        "中证医疗": "医疗",
        "证券公司": "证券",
        "中证军工": "军工",
        "中证白酒": "白酒",
        "中证消费": "消费",
        "中证新能源": "新能源",
        "上证50": "上证50",
        "沪深300": "沪深300",
      };
      for (const [k, v] of Object.entries(abbr)) {
        if (s.includes(k)) { s = v; break; }
      }
      if (s.length <= 6) return s;
      // 6) 极端兜底:按词切分取前 5 字符主题(不补省略号,原意由 title 保证)
      return s.slice(0, 5);
    },
    fmtQuote(snap) {
      return snap && snap.last_price != null ? this.fmt(snap.last_price, 3) : "-";
    },
    snapChg(snap) {
      // 同花顺基金快照涨跌幅字段为 price_change_ratio_pct(非 change_pct)
      return snap ? snap.price_change_ratio_pct : null;
    },
    volRatio(snap) {
      // 快照无直接量比,用换手率近似(列头为「换手」)
      if (snap && snap.turnover_ratio_pct != null) {
        return this.fmt(snap.turnover_ratio_pct, 2) + "%";
      }
      return "-";
    },

    // ---- 数据加载 ----
    async loadIndices() {
      const r = await api("/api/indices");
      if (r.ok) this.indices = r.indices;
    },
    async loadWatchlist() {
      const r = await api("/api/watchlist");
      if (r.ok && r.items.length) {
        this.watchlist = r.items;
        if (!this.current) this.select(r.items[0].symbol);
        if (!this.techCode) {
          // 默认选中当前模式的首只(可能为空:该模式尚无自选标的)
          const first = this.snapChips[0];
          this.techCode = first ? first.symbol : "";
        }
      }
    },
    async select(sym) {
      this.current = sym;
      const w = this.watchlist.find((x) => x.symbol === sym);
      this.currentName = w ? w.name : sym;
      const r = await api("/api/quote?symbol=" + sym);
      if (r.ok) this.currentQuote = r.snapshot;
      this.loadKline();
    },

    // ---- 盘中快照模式切换(2026-09-04 个股模式):切模式清卡并选中该模式首只 ----
    setSnapMode(m) {
      if (this.snapMode === m) return;
      this.snapMode = m;
      // 清空三卡与一句话,防止上个模式的残留内容误导
      this.snapFacts = ""; this.snapFactsError = "";
      this.snapAnalysis = ""; this.snapAnalysisError = ""; this.snapAnalysisAt = "";
      this.snapSignal = null; this.snapAt = "";
      this.cfMarkdown = ""; this.cfError = "";
      const first = this.snapChips[0];
      // 空模式必须清空 techCode:否则「刷新快照」会对着上一模式的目标刷新
      this.techCode = first ? first.symbol : "";
    },
    selectTech(sym) {
      // 仅切换选中标的不自动拉取(与原 chips 行为一致);跨模式点击时跟随切模式
      const w = this.watchlist.find((x) => x.symbol === sym);
      const t = (w && w.type) || ((/^5|^1/.test(String(sym)) && String(sym).length === 6) ? "etf" : "stock");
      this.snapMode = t === "etf" ? "etf" : "stock";
      this.techCode = sym;
    },

    // ---- 自选股管理(§5.6) ----
    // 标的搜索联想(同花顺式):名称/拼音缩写/代码 → 候选下拉,点选/回车直接入池
    inPool(sym) {
      return this.watchlist.some((w) => w.symbol === sym);
    },
    onSymInput() {
      if (this.sugTimer) clearTimeout(this.sugTimer);
      const q = this.newSym.trim();
      if (!q) { this.sugClose(); return; }
      // 纯6位代码不联想(现状已是直接添加的快捷路径,回车/点按钮即入池)
      if (/^\d{6}$/.test(q)) { this.sugClose(); return; }
      this.sugTimer = setTimeout(() => this.fetchSug(q), 220);
    },
    async fetchSug(q) {
      const seq = ++this.sugSeq;
      try {
        const r = await api("/api/suggest?q=" + encodeURIComponent(q));
        if (seq !== this.sugSeq) return;          // 已有更新的击键,丢弃旧响应
        this.sugItems = (r && r.ok && Array.isArray(r.items)) ? r.items : [];
        this.sugActive = this.sugItems.length ? 0 : -1;
        this.sugOpen = true;
      } catch (e) { /* 联想失败静默,不干扰手动输入 */ }
    },
    sugMove(d) {
      if (!this.sugOpen || !this.sugItems.length) return;
      const n = this.sugItems.length;
      this.sugActive = (this.sugActive + d + n) % n;
    },
    async onSymEnter() {
      const q = this.newSym.trim();
      if (this.sugOpen && this.sugItems.length && this.sugActive >= 0
          && !/^\d{6}$/.test(q)) {
        await this.pickSug(this.sugItems[this.sugActive]);
      } else {
        await this.addSymbol();
      }
    },
    async pickSug(s) {
      this.sugClose();
      this.newSym = "";
      await this.addSymbol(s.symbol, s.name);
    },
    sugClose() {
      this.sugOpen = false;
      this.sugItems = [];
      this.sugActive = -1;
      if (this.sugTimer) { clearTimeout(this.sugTimer); this.sugTimer = null; }
    },
    onSymBlur() {
      // mousedown.prevent 已保证点击候选项先于 blur 生效;延迟兜底纯点击外部收起
      setTimeout(() => { this.sugOpen = false; }, 120);
    },
    async addSymbol(symArg, nameArg) {
      const sym = (symArg || this.newSym).trim();
      if (!sym) { this.wlError = "请输入代码"; return; }
      this.sugClose();
      this.wlError = "";
      this.wlMsg = "";
      try {
        const resp = await fetch("/api/watchlist", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // 点选候选项时随带名称,后端免一次 fetch_name 兜底
          body: JSON.stringify(nameArg ? { symbol: sym, name: nameArg } : { symbol: sym }),
        });
        const r = await resp.json();
        if (!r.ok) { this.wlError = r.error || "添加失败"; return; }
        this.newSym = "";
        this.wlMsg = r.duplicated ? `${sym} 已在池中` : `已添加 ${r.item.name}(${sym})`;
        await this.loadWatchlist();
      } catch (e) { this.wlError = String(e); }
    },
    async removeSymbol(sym) {
      this.wlError = "";
      this.wlMsg = "";
      // 双向联动:持仓标的禁止从自选池删除(后端同样有 409 保护)
      if (this.holdingOf(sym)) {
        this.wlError = `${sym} 在持仓卡中——请先在「设置」清空该持仓,再删除自选股`;
        return;
      }
      try {
        const resp = await fetch("/api/watchlist?symbol=" + encodeURIComponent(sym), { method: "DELETE" });
        const r = await resp.json();
        if (!r.ok) { this.wlError = r.error || "删除失败"; return; }
        this.wlMsg = `已移除 ${sym}`;
        if (this.current === sym) this.current = "";
        await this.loadWatchlist();
      } catch (e) { this.wlError = String(e); }
    },

    // ---- 设置:持仓卡 / 账户 / key(架构 §八) ----
    holdingOf(sym) {
      return this.holdings.find((h) => h.symbol === sym);
    },
    holdPnL(sym) {
      const h = this.holdingOf(sym);
      if (!h || !h.cost) return null;
      const w = this.watchlist.find((x) => x.symbol === sym);
      const price = w && w.snapshot && w.snapshot.last_price;
      if (price == null) return null;
      return (price / h.cost - 1) * 100;
    },
    fmtPrice3(v) {
      return v == null || v === "" ? "-" : Number(v).toFixed(3);
    },
    // 设置弹窗:打开时刷新一遍数据(持仓/账户/key/LLM/刷新间隔,并收起全宽视图)
    openSettings() {
      this.view = "";
      this.settingsOpen = true;
      document.body.classList.add("modal-open");
      this.loadSettings();
    },
    closeSettings() {
      this.settingsOpen = false;
      document.body.classList.remove("modal-open");
    },
    async loadSettings(config = {}) {
      const [rh, ra] = await Promise.all([
        api("/api/holdings"), api("/api/account"),
      ]);
      if (rh.ok) this.holdings = rh.holdings || [];
      if (ra.ok) this.account = ra.account || { total_capital: "", cash: "" };
      await this.loadLlm();
      this.loadRefreshConfig(config);
      this.loadAutoStart();
    },
    // 仅拉持仓列表(浮窗模式用:持仓标记的唯一数据源,不牵动账户/LLM 等设置项)
    async loadHoldings() {
      try {
        const r = await api("/api/holdings");
        if (r.ok) this.holdings = r.holdings || [];
      } catch (e) { /* 服务不可用则忽略 */ }
    },
    async loadAutoStart() {
      try {
        const r = await api("/api/panel/autostart");
        if (r.ok) this.autoStart = !!r.enabled;
      } catch (e) { /* 非 Windows / 服务不可用则忽略 */ }
    },
    async loadLlm() {
      // 模型面板:读取生效的 LLM 配置(profiles = 尝试顺序) + 各服务商 key 掩码
      const r = await api("/api/llm");
      if (!r.ok) {
        this.llmError = r.error || "LLM 配置读取失败";
        this.cancelEdit();
        return;
      }
      this.llmProfiles = (r.profiles || []).map((p) => ({ ...p, uid: "p" + (++this._uidSeed), testState: null }));
      this.llmKeys = (r.keys || []).map((k) => ({ ...k, input: "", visible: false, plain: "" }));
      this.llmDefaultUrls = r.default_urls || {};
      this.llmError = "";
      this.cancelEdit();
    },
    // ---- 模型面板:行内编辑(添加/编辑/排序/删除/测试/保存) ----
    defaultUrlOf(provider) {
      return this.llmDefaultUrls[provider] || "";
    },
    addModel() {
      if (this.editingUid) return;                      // 已有编辑行时不允许再开
      const p = { uid: "p" + (++this._uidSeed), order: this.llmProfiles.length + 1,
                  provider: "zhipu", model: "", base_url: "", has_key: false, isNew: true };
      this.llmProfiles.push(p);
      this.llmSavedAt = "";
      this.startEdit(p);
      this.$nextTick(() => { const el = this.$refs.editModelInput; if (el) el.focus(); });
    },
    startEdit(p) {
      this.editingUid = p.uid;
      this.editingRow = {
        provider: p.provider || "zhipu",
        model: p.model || "",
        base_url: p.base_url || (this.llmDefaultUrls[p.provider] || ""),
        key: "",
        keyVisible: false,
        hasKey: !!p.has_key,
      };
      this.testMsg = null;
    },
    editModel(p) {
      if (this.editingUid === p.uid) { this.cancelEdit(); return; }
      this.startEdit(p);
      this.llmTestMsg = { ...this.llmTestMsg, [p.uid]: null };
    },
    cancelEdit() {
      // 刚添加、尚未填内容的空行随取消一起移除
      if (this.editingUid) {
        const row = this.llmProfiles.find((x) => x.uid === this.editingUid);
        if (row && row.isNew) {
          this.llmProfiles = this.llmProfiles.filter((x) => x.uid !== row.uid);
        }
      }
      this.editingUid = "";
      this.editingRow = null;
      this.testMsg = null;
    },
    saveRow() {
      const f = this.editingRow;
      if (!f.provider || !f.model.trim()) {
        this.testMsg = { ok: false, text: "服务商与模型名称必填" };
        return;
      }
      const p = this.llmProfiles.find((x) => x.uid === this.editingUid);
      if (!p) return;
      p.provider = f.provider.trim();
      p.model = f.model.trim();
      p.base_url = (f.base_url || "").trim();
      p.isNew = false;
      const newKey = (f.key || "").trim();
      if (newKey) {
        this.stageKey(p.provider, newKey);   // 新 Key 暂存,随「保存配置」一起写盘
        p.has_key = true;
      }
      const uid = p.uid;
      this.cancelEdit();
      this.llmTestMsg = { ...this.llmTestMsg, [uid]: null };
      this.llmSavedAt = "";                  // 有未保存修改
    },
    stageKey(provider, key) {
      const k = this.llmKeys.find((x) => x.provider === provider);
      if (k) { k.input = key; k.set = true; }
      else { this.llmKeys.push({ provider, set: true, masked: "", input: key, visible: false }); }
    },
    // 拖拽排序(行业标准手柄):把 dragUid 行移动到 drop 目标行位置
    dragStart(p, e) {
      this.dragUid = p.uid;
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", p.uid);
      }
    },
    dragOver(p) {
      if (this.dragUid && this.dragUid !== p.uid) this.dragOverUid = p.uid;
    },
    dragLeave(p) {
      if (this.dragOverUid === p.uid) this.dragOverUid = "";
    },
    dropOn(p) {
      const from = this.dragUid;
      if (!from || from === p.uid) { this.dragUid = ""; this.dragOverUid = ""; return; }
      const arr = this.llmProfiles.slice();
      const i = arr.findIndex((x) => x.uid === from);
      const j = arr.findIndex((x) => x.uid === p.uid);
      if (i < 0 || j < 0) { this.dragUid = ""; this.dragOverUid = ""; return; }
      const [moved] = arr.splice(i, 1);
      arr.splice(j, 0, moved);                 // 插入到目标行位置
      this.llmProfiles = arr;
      this.dragUid = "";
      this.dragOverUid = "";
      this.llmSavedAt = "";
    },
    dragEnd() {
      this.dragUid = "";
      this.dragOverUid = "";
    },
    baseUrlTitle(p) {
      const url = p.base_url || this.defaultUrlOf(p.provider);
      return p.base_url ? p.base_url : (url ? "默认地址: " + url : "使用内置地址");
    },
    delModel(p) {
      if (!confirm(`删除模型 ${p.provider} / ${p.model} ?`)) return;
      if (this.editingUid === p.uid) this.cancelEdit();
      this.llmProfiles = this.llmProfiles.filter((x) => x.uid !== p.uid);
      if (this.llmTestMsg[p.uid]) this.llmTestMsg = { ...this.llmTestMsg, [p.uid]: null };
      this.llmSavedAt = "";
    },
    // 测试连接:行内(已保存值)/ 编辑表单(当前输入值)
    async testModel(p) {
      const k = this.llmKeys.find((x) => x.provider === p.provider);
      const body = { provider: p.provider, model: p.model, base_url: p.base_url };
      const newKey = k && k.input ? k.input.trim() : "";
      if (newKey) body.key = newKey;
      this.llmTesting = { ...this.llmTesting, [p.uid]: true };
      this.llmTestMsg = { ...this.llmTestMsg, [p.uid]: null };
      try {
        const resp = await fetch("/api/llm/test", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        const r = await resp.json();
        this.llmTestMsg = { ...this.llmTestMsg, [p.uid]: { ok: !!r.ok, text: r.ok ? r.message : (r.error || r.message || "测试失败") } };
        // 测试结果驱动状态列:失败 → 已失效(红);成功 → 已配置(绿)
        this.setTestState(p, !!r.ok);
      } catch (e) {
        this.llmTestMsg = { ...this.llmTestMsg, [p.uid]: { ok: false, text: String(e) } };
        this.setTestState(p, false);
      } finally {
        this.llmTesting = { ...this.llmTesting, [p.uid]: false };
      }
    },
    setTestState(p, ok) {
      const row = this.llmProfiles.find((x) => x.uid === p.uid);
      if (row) row.testState = ok ? "" : "fail";
    },
    async testRowForm() {
      const f = this.editingRow;
      if (!f.model.trim()) { this.testMsg = { ok: false, text: "先填写模型名称" }; return; }
      const body = { provider: f.provider, model: f.model.trim(), base_url: (f.base_url || "").trim() };
      if ((f.key || "").trim()) body.key = f.key.trim();
      this.testLoading = true;
      this.testMsg = null;
      try {
        const resp = await fetch("/api/llm/test", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        const r = await resp.json();
        this.testMsg = { ok: !!r.ok, text: r.ok ? r.message : (r.error || r.message || "测试失败") };
      } catch (e) { this.testMsg = { ok: false, text: String(e) }; }
      finally { this.testLoading = false; }
    },
    // 保存(写 settings.local.yaml + reload 生效)
    async saveLlmConfig() {
      const profiles = this.llmProfiles.map((p) => ({ provider: p.provider, model: p.model, base_url: p.base_url }));
      const keys = {};
      this.llmKeys.forEach((k) => { const v = (k.input || "").trim(); if (v) keys[k.provider] = v; });
      this.llmSaving = true;
      this.llmError = "";
      this.llmSavedAt = "";
      try {
        const r = await this.postJson("/api/llm/save", { profiles, keys });
        if (!r.ok) { this.llmError = r.error || "保存失败"; return; }
        this.llmSavedAt = new Date().toTimeString().slice(0, 5) + " 已保存并立即生效";
        await this.loadLlm();
      } catch (e) { this.llmError = String(e); }
      finally { this.llmSaving = false; }
    },
    async saveKeys() {
      const keys = {};
      this.llmKeys.forEach((k) => { const v = (k.input || "").trim(); if (v) keys[k.provider] = v; });
      if (!Object.keys(keys).length) { this.llmError = "没有需要保存的 Key(输入框留空 = 不修改)"; return; }
      this.llmSaving = true;
      this.llmError = "";
      try {
        const r = await this.postJson("/api/llm/save", { keys });
        if (!r.ok) { this.llmError = r.error || "保存失败"; return; }
        this.llmSavedAt = new Date().toTimeString().slice(0, 5) + " Key 已保存并立即生效";
        await this.loadLlm();
      } catch (e) { this.llmError = String(e); }
      finally { this.llmSaving = false; }
    },
    // API Key 明文查看:先弹安全警告,确认后向后端取明文展示;再点眼睛即收起
    async peekKey(k) {
      if (!confirm("显示完整 API Key 可能被周围人看到,确认显示?")) return;
      try {
        const r = await this.postJson("/api/llm/peek", { provider: k.provider });
        if (!r.ok) { this.llmError = r.error || "获取失败"; return; }
        k.plain = r.key;
      } catch (e) { this.llmError = String(e); }
    },
    hideKey(k) {
      k.plain = "";
    },
    // 高级配置:复制配置文件路径(网页端不读取/修改文件内容)
    async copyPath(p) {
      const done = () => { this.advMsg = p + " 已复制到剪贴板"; };
      try {
        await navigator.clipboard.writeText(p);
        done();
      } catch (e) {
        // 降级:隐藏 textarea + execCommand(旧浏览器/非安全上下文)
        try {
          const ta = document.createElement("textarea");
          ta.value = p;
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
          done();
        } catch (e2) { this.advMsg = "复制失败,请手动复制路径"; }
      }
    },
    postJson(url, body) {
      return fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then((r) => r.json());
    },
    async addHolding() {
      const f = this.holdForm;
      if (!f.symbol || !f.cost || !f.quantity) {
        this.settingsMsg = "请填全 代码/成本/数量"; return;
      }
      try {
        const resp = await fetch("/api/holdings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbol: f.symbol, cost: f.cost,
                                 quantity: f.quantity, status: f.status }),
        });
        const r = await resp.json();
        if (!r.ok) { this.settingsMsg = r.error || "添加失败"; return; }
        this.settingsMsg = `已添加持仓 ${r.item.symbol}`;
        this.holdForm = { symbol: "", cost: "", quantity: "", status: "持仓" };
        // 联动:新增持仓自动加入自选股池(若不在)——双向约束的"入池"半边
        if (!this.watchlist.find((w) => w.symbol === r.item.symbol)) {
          await fetch("/api/watchlist", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ symbol: r.item.symbol }),
          });
        }
        this.loadSettings();
        this.loadWatchlist();
      } catch (e) { this.settingsMsg = String(e); }
    },
    async delHolding(sym) {
      await fetch("/api/holdings?symbol=" + encodeURIComponent(sym), { method: "DELETE" });
      this.settingsMsg = `已移除持仓 ${sym}`;
      this.loadSettings();
    },
    async saveAccount() {
      this.acctSaving = true;
      try {
        const resp = await fetch("/api/account", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ total_capital: this.account.total_capital,
                                 cash: this.account.cash }),
        });
        const r = await resp.json();
        if (r.ok) {
          this.acctSaved = new Date().toTimeString().slice(0, 5) + " 已保存";
          this.settingsMsg = "账户已保存";
        } else {
          this.settingsMsg = r.error || "保存失败";
        }
      } catch (e) { this.settingsMsg = String(e); }
      finally { this.acctSaving = false; }
    },

    // ---- 设置:刷新间隔(秒)。控制 顶部指数 + 候选股池 的轮询频率,存 config/web.yaml。
    async loadRefreshConfig(silent = false) {
      const r = await api("/api/refresh-config");
      if (r.ok && r.refresh_interval_sec != null) {
        this.refreshSec = Number(r.refresh_interval_sec);
        this.restartPolling();
      } else if (!silent) {
        this.settingsMsg = "刷新间隔读取失败";
      }
      if (r.ok && r.panel_start_hidden != null) {
        this.panelStartHidden = Boolean(r.panel_start_hidden);
      }
    },
    async saveRefreshConfig() {
      const sec = Number(this.refreshSec);
      if (!Number.isInteger(sec) || sec < 1 || sec > 3600) {
        this.settingsMsg = "刷新间隔需为 1-3600 的整数秒";
        return;
      }
      this.refreshSaving = true;
      try {
        const resp = await fetch("/api/refresh-config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_interval_sec: sec }),
        });
        const r = await resp.json();
        if (!r.ok) { this.settingsMsg = r.error || "保存失败"; return; }
        this.refreshSec = Number(r.refresh_interval_sec);
        this.refreshSaved = new Date().toTimeString().slice(0, 5) + " 已保存(新间隔立即生效)";
        this.settingsMsg = "刷新间隔已保存";
        this.restartPolling();
      } catch (e) { this.settingsMsg = String(e); }
      finally { this.refreshSaving = false; }
    },
    // ---- 悬浮面板:隐藏到托盘(仅浮窗模式,pywebview 桥,浏览器下静默) ----
    floatHide() {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.hide_to_tray) {
        try { window.pywebview.api.hide_to_tray(); } catch (e) { /* 忽略 */ }
      }
    },
    // ---- 桌面分身:把透明窗口贴合到"卡片 + 四周 FP_MARGIN 透明投影边"(仅 float 模式) ----
    fitFloatWindow() {
      if (!FLOAT_MODE) return;
      const el = document.getElementById("floatPanel");
      if (!el) return;
      const send = () => {
        const a = window.pywebview && window.pywebview.api;
        if (!(a && a.set_size)) return;
        try { a.set_size(Math.ceil(el.offsetWidth) + FP_MARGIN * 2, Math.ceil(el.offsetHeight) + FP_MARGIN * 2); } catch (e) { /* 忽略 */ }
      };
      if (this._floatRO) { try { this._floatRO.disconnect(); } catch (e) { /* 忽略 */ } }
      if (typeof ResizeObserver !== "undefined") {
        this._floatRO = new ResizeObserver(send);
        this._floatRO.observe(el);
      }
      send();
    },
    // ---- 设置:启动即藏托盘(存 config/web.yaml,重启面板后生效) ----
    async savePanelStartHidden() {
      try {
        const resp = await fetch("/api/refresh-config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ panel_start_hidden: !!this.panelStartHidden }),
        });
        const text = await resp.text();
        let r = null; try { r = JSON.parse(text); } catch (e) { /* 非 JSON → 下面显式报错 */ }
        this.settingsMsg = (r && r.ok) ? "启动即藏托盘已保存(重启面板后生效)"
          : (r && r.error) ? r.error
          : `HTTP ${resp.status}: ${text.slice(0, 140)}`;
      } catch (e) { this.settingsMsg = String(e); }
    },
    // ---- 设置:开机自启(Windows 启动项,立即生效) ----
    async saveAutoStart() {
      try {
        const resp = await fetch("/api/panel/autostart", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: !!this.autoStart }),
        });
        const text = await resp.text();
        let r = null; try { r = JSON.parse(text); } catch (e) { /* 非 JSON → 下面显式报错 */ }
        this.settingsMsg = (r && r.ok) ? (this.autoStart ? "已开启开机自启" : "已关闭开机自启")
          : (r && r.error) ? r.error
          : `HTTP ${resp.status}: ${text.slice(0, 140)}`;
      } catch (e) { this.settingsMsg = String(e); }
    },
    // 轮询:顶部指数 + 候选股池 按 refreshSec 一起刷。暂停界面时也保持(盘中行情需一直最新)。
    startPolling() {
      this.stopPolling();
      const tick = () => {
        this.loadIndices();
        this.loadWatchlist();
      };
      tick();                                  // 立即刷一轮
      this._pollTimer = setInterval(tick, this.refreshSec * 1000);
    },
    stopPolling() {
      if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
    },
    restartPolling() {
      if (!this._started) return;   // mounted 前不启动
      this.startPolling();
    },

    // ---- 盘中技术快照(数据快照 + 深入分析,两卡独立,2026-08-22 / 09-01 重构) ----
    // 2026-09 数据流重构:刷新快照(秒级零 LLM,后台归档确定性建议)与深入分析
    // (读取快照缓存 + LLM 扩展,30s 内不重复拉实时;无缓存自动先刷新)完全解耦。
    async loadTechFacts() {
      this.snapFacts = "";
      this.snapFactsError = "";
      this.snapFactsLoading = true;
      try {
        const r = await api("/api/snapshot/tech/facts?code=" + this.techCode);
        if (!r.ok) { this.snapFactsError = r.error || "数据快照失败"; return; }
        this.snapFacts = r.markdown;
        this.snapAt = r.data_at || "";
        this.snapSignal = r.signal || null;   // 一句话信号(更新即刷新,零 LLM)
        // 新快照 → 旧分析失效(避免"分析基于旧数据"的误解),清空待重新分析
        this.snapAnalysis = "";
        this.snapAnalysisError = "";
        this.snapAnalysisAt = "";
        this.applyFactsToPanel(this.techCode, r);   // 联动:侧栏按钮同时刷新面板标记
      } catch (e) { this.snapFactsError = String(e); }
      finally { this.snapFactsLoading = false; }
    },
    async loadTechAnalysis() {
      this.snapAnalysis = "";
      this.snapAnalysisError = "";
      this.snapAnalysisLoading = true;
      try {
        const r = await api("/api/snapshot/tech/analysis?code=" + this.techCode);
        if (!r.ok) { this.snapAnalysisError = r.error || "深入分析失败"; return; }
        this.snapAnalysis = r.markdown;   // degraded 时 markdown 内已含降级提示
        this.snapAnalysisAt = r.data_at || this.snapAt || "";
      } catch (e) { this.snapAnalysisError = String(e); }
      finally { this.snapAnalysisLoading = false; }
    },
    combinedSnapshotMd() {
      // 推送 = 数据快照(上) + 深入分析(下),中间分隔线;手机端先事实后推理
      if (!this.snapFacts) return this.snapAnalysis || "";
      if (!this.snapAnalysis) return this.snapFacts;
      return this.snapFacts + "\n\n---\n\n" + this.snapAnalysis;
    },
    async runCounterfactual() {
      // 反事实推演(盘后·情景分支,2026-08-26):收盘快照 + 确定性迁移表 → LLM 情景推演。
      // 手动触发;输出非投资建议(卡3 有显著标注);LLM 失败降级纯迁移表。
      if (!this.techCode) return;
      this.cfLoading = true;
      this.cfError = "";
      this.cfMarkdown = "";
      try {
        const r = await api("/api/snapshot/tech/counterfactual?code=" + this.techCode);
        if (!r.ok) { this.cfError = r.error || "反事实推演失败"; return; }
        this.cfMarkdown = r.markdown;
      } catch (e) { this.cfError = String(e); }
      finally { this.cfLoading = false; }
    },
    // ---- 悬浮面板(里程碑7) ----
    markerOf(sym) {
      return this.markers[sym] || null;
    },
    markerWord(sym) {
      // 核心词(行内只显示它):触发档×状态词,确定性字段驱动。
      // 触发档:现价≥突破加仓→突破;≤回踩加仓→回踩买区;≤砍仓→砍仓线;≤止损→破位·止损。
      // 未触发 → 沿用信号状态词(蓄势/区间震荡/超跌试多…)。
      const m = this.markers[sym];
      if (!m) return "";
      const w = this.watchlist.find((x) => x.symbol === sym);
      const price = w && w.snapshot && w.snapshot.last_price;
      let word = m.status_word || "数据不足";
      if (price != null && m.anchors) {
        if (m.anchors["突破加仓"] != null && price >= m.anchors["突破加仓"]) word = "突破";
        else if (m.anchors["回踩加仓"] != null && price <= m.anchors["回踩加仓"]) word = "回踩买区";
        else if (m.anchors["砍仓"] != null && price <= m.anchors["砍仓"]) word = "砍仓线";
        else if (m.anchors["止损"] != null && price <= m.anchors["止损"]) word = "破位·止损";
      }
      return word;
    },
    markerTitle(sym) {
      // 完整数据(鼠标悬停显示):核心词 + 距最近档 + 风险 + 更新时间
      const m = this.markers[sym];
      if (!m) return "";
      const w = this.watchlist.find((x) => x.symbol === sym);
      const price = w && w.snapshot && w.snapshot.last_price;
      const parts = [this.markerWord(sym)];
      const dist = this.floatSnapDistance(m, price);
      if (dist != null) parts.push("距" + dist);
      if (m.risk && m.risk !== "正常") parts.push("风险 " + m.risk);
      if (m.updated_at) parts.push("更新 " + m.updated_at);
      return parts.join(" · ");
    },
    markerCls(sym) {
      // 标记颜色:与核心词同源(含触发档翻转),状态词 → 色彩档;
      // 风险「关注/升级」叠闪烁;程序确定性字段驱动
      const m = this.markers[sym];
      if (!m) return "conc-normal";
      const word = this.markerWord(sym);
      const base = {
        "突破": "conc-near", "右侧初现": "conc-near",
        "回踩买区": "conc-wait", "回踩": "conc-wait",
        "蓄势": "conc-normal", "区间震荡": "conc-normal",
        "超跌试多": "conc-warn", "变盘前兆": "conc-warn",
        "左侧观望": "conc-normal", "破位退出": "conc-hit",
        "砍仓线": "conc-hit", "破位·止损": "conc-hit",
      };
      const cls = base[word] || "conc-normal";
      const rk = m.risk === "升级" ? " risk-down" : m.risk === "关注" ? " risk-warn" : "";
      return cls + rk;
    },
    floatSnapDistance(m, price) {
      // 距最近档:前端用现价 vs anchors 确定性重算(不覆盖 marker 文本)
      // anchor_price 是个股建议的记录基准价(验证日历用),不是操作价位档,
      // 不参与距离展示(v5.6.2 修复:"距0.0%anchor_price"键名溢出)。
      if (price == null || !m.anchors || !Object.keys(m.anchors).length) return null;
      let best = null, bestName = "";
      for (const [name, v] of Object.entries(m.anchors)) {
        if (v == null || name === "anchor_price") continue;
        const d = Math.abs(price - v) / price * 100;
        if (best == null || d < best) { best = d; bestName = name; }
      }
      return best != null ? `${best.toFixed(1)}%·${bestName}` : null;
    },
    async genFloatSnapshot(sym) {
      // 面板单只刷新:只刷该只,规避 fuyao 批量 429;与「数据快照」卡同源可复核。
      // 联动:同时刷新侧栏「数据快照」卡并切到该标的,两处保持一致。
      if (this.refreshCode) return;
      const prev = this.techCode;
      this.techCode = sym;
      this.refreshCode = sym;
      this.snapError = "";
      this.snapFacts = "";
      this.snapFactsError = "";
      this.snapFactsLoading = true;
      try {
        const r = await api("/api/snapshot/tech/facts?code=" + sym);
        if (r.ok) {
          this.snapFacts = r.markdown;                 // 侧栏数据快照卡
          this.snapAt = r.data_at || "";               // 快照时点(卡头展示)
          this.snapSignal = r.signal || null;          // 一句话信号
          this.applyFactsToPanel(sym, r);              // 面板标记
        } else {
          this.snapError = (r.error || "该标的快照失败") + " — 面板该行暂不更新";
        }
      } catch (e) {
        this.snapError = String(e);
        this.snapFactsError = String(e);
      } finally {
        this.snapFactsLoading = false;
        this.refreshCode = "";
        this.techCode = prev || sym;
      }
    },
    applyFactsToPanel(sym, r) {
      // 单一真源落点:把 facts 接口返回的确定性 panel 段写入悬浮面板标记
      if (r && r.panel) {
        this.markers = { ...this.markers, [sym]: r.panel };
        this.markersTime = { ...this.markersTime, [sym]: new Date().toTimeString().slice(0, 5) };
      }
    },
    toggleFloatCol(key) {
      this.floatCols = { ...this.floatCols, [key]: !this.floatCols[key] };
      this.persistFloatPrefs();
    },
    // 显示偏好存 localStorage:可选列 + 仅持仓过滤(纯显示层,不动数据源)
    defaultFloatPrefs() {
      try {
        const saved = JSON.parse(localStorage.getItem("alphaprism.float") || "{}");
        if (saved.floatCols) this.floatCols = { ...this.floatCols, ...saved.floatCols };
        if (typeof saved.floatOnlyHeld === "boolean") this.floatOnlyHeld = saved.floatOnlyHeld;
      } catch (e) { /* 忽略损坏缓存 */ }
    },
    persistFloatPrefs() {
      try {
        localStorage.setItem("alphaprism.float", JSON.stringify({
          floatCols: this.floatCols,
          floatOnlyHeld: this.floatOnlyHeld,
        }));
      } catch (e) { /* 忽略 */ }
    },
    fmtSnapVol(snap) {
      // 成交量:同花顺基金快照 volume 字段(股);列头「量」,亿/万缩写 手=股/100
      if (!snap || snap.volume == null) return "-";
      return this.fmtVol(snap.volume / 100);
    },
    fmtSnapAmt(snap) {
      // 成交额:快照 turnover(元) → 亿/万缩写
      if (!snap || snap.turnover == null) return "-";
      return this.fmtAmt(snap.turnover);
    },
    // 悬浮面板整体拖拽:面板任意区域按下鼠标即可拖动,尺寸不变。
    // 不用 setPointerCapture(会把随后的 click 重定向到面板,导致行点击失效);
    // 改为按下时在 document 临时挂 move/up,抬起后卸载——click 的 target 保持原样。
    // 点击与拖拽用 4px 阈值区分:未移动=正常点击(行选择/按钮),移动了=拖拽并抑制随后的 click。
    initFloatDrag() {
      const panel = document.getElementById("floatPanel");
      if (!panel) return;
      let dragging = false, moved = false;
      let startX = 0, startY = 0, origLeft = 0, origTop = 0;
      const isInteractive = (t) => !!(t && t.closest && t.closest("button, input, label, a"));
      const onDown = (e) => {
        // 交互控件(⟳/列toggle/仅持仓)不启动拖拽;非鼠标主键忽略
        if (isInteractive(e.target)) return;
        if (e.button != null && e.button !== 0) return;
        // 触屏在 .f-body 内让位给列表滚动;鼠标任意区域均可拖
        if (e.pointerType === "touch" && e.target.closest && e.target.closest(".f-body")) return;
        dragging = true;
        moved = false;
        const r = panel.getBoundingClientRect();
        startX = e.clientX; startY = e.clientY;
        origLeft = r.left; origTop = r.top;
        document.addEventListener("pointermove", onMove);
        document.addEventListener("pointerup", onUp);
        document.addEventListener("pointercancel", onUp);
      };
      const onMove = (e) => {
        if (!dragging) return;
        const dx = e.clientX - startX, dy = e.clientY - startY;
        if (!moved && Math.hypot(dx, dy) < 4) return;   // 阈值内视为点击,不拖
        moved = true;
        panel.classList.add("dragging");
        // 关键修复:清除 right/bottom 双锚,只留 left/top → 高度回到内容自适应,不再拉伸
        panel.style.right = "auto";
        panel.style.bottom = "auto";
        panel.style.left = Math.max(0, origLeft + dx) + "px";
        panel.style.top = Math.max(0, origTop + dy) + "px";
        if (e.cancelable) e.preventDefault();
      };
      const onUp = () => {
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
        document.removeEventListener("pointercancel", onUp);
        if (dragging && moved) {
          // 拖拽结束:抑制本次 click,避免误触行选择/按钮
          const suppress = (ev) => {
            ev.stopPropagation();
            ev.preventDefault();
            panel.removeEventListener("click", suppress, true);
          };
          panel.addEventListener("click", suppress, true);
        }
        dragging = false;
        panel.classList.remove("dragging");
      };
      panel.addEventListener("pointerdown", onDown);
    },
    // 推送到手机:当前版本已移除 UI 入口(index.html),本方法与 /api/snapshot/push
    // 保留待更高版本恢复;combinedSnapshotMd 供未来导出/推送复用。
    async pushSnapshot() {
      const md = this.combinedSnapshotMd();
      if (!md) return;
      this.snapPushing = true;
      this.snapError = "";
      try {
        const resp = await fetch("/api/snapshot/push", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ markdown: md }),
        });
        const r = await resp.json();
        if (!r.ok) { this.snapError = r.error || "推送失败"; return; }
        this.wlMsg = "已推送到手机";
      } finally {
        this.snapPushing = false;
      }
    },
    setPeriod(p) {
      this.period = p;
      this.loadKline();
    },
    async loadKline() {
      if (!this.current) return;
      const r = await api(`/api/kline?symbol=${this.current}&period=${this.period}`);
      if (!r.ok) { this.msg = r.error || "K线加载失败"; return; }
      if (r.period === "day") this.dayRows = r.rows || [];
      this.renderChart(r);
    },

    // ---- 格式化 ----
    fmtVol(v) {
      if (v == null || isNaN(v)) return "-";
      if (v >= 1e8) return (v / 1e8).toFixed(2) + "亿";
      if (v >= 1e4) return (v / 1e4).toFixed(1) + "万";
      return String(Math.round(v));
    },
    fmtAmt(v) {
      if (v == null || isNaN(v)) return "-";
      if (v >= 1e8) return (v / 1e8).toFixed(2) + "亿";
      if (v >= 1e4) return (v / 1e4).toFixed(1) + "万";
      return String(Math.round(v));
    },
    ma(arr, n) {
      // 移动平均;前 n-1 位为 null
      return arr.map((_, i) => {
        if (i < n - 1) return null;
        let s = 0;
        for (let j = i - n + 1; j <= i; j++) s += arr[j];
        return +(s / n).toFixed(4);
      });
    },
    maColor(p) {
      return { 5: "#f0b90b", 10: "#4c8dff", 20: "#a56eff", 60: "#ff9800", 120: "#26c6da", 250: "#9e9e9e" }[p] || "#4c8dff";
    },
    plainClone(v, seen = new Map()) {
      // 深拷贝为普通对象:剥离 Vue 响应式 Proxy(Proxy 会让 ECharts 内部操作偶发报
      // "Cannot read properties of undefined (reading 'type')" 并卡死 dataZoom)。函数原样保留。
      if (v === null || typeof v !== "object") return v;
      if (seen.has(v)) return seen.get(v);
      const out = Array.isArray(v) ? [] : {};
      seen.set(v, out);
      for (const k of Object.keys(v)) out[k] = this.plainClone(v[k], seen);
      return out;
    },

    // ---- markdown 渲染(轻量:表/标题/粗斜体/列表/引用/链接,先转义防注入) ----
    mdToHtml(md) {
      if (!md) return "";
      const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      const inline = (s) => s
        .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
        .replace(/\*([^*]+)\*/g, "<i>$1</i>")
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
      const lines = esc(md).split(/\r?\n/);
      let html = "";
      let inList = false, listType = "ul", tableRows = [];
      let blankPending = false;   // 2026-09-04:空行暂不断列表,下一个列表项续编号(修复"序号全是1")
      const flushTable = () => {
        if (!tableRows.length) return;
        html += "<table class='mk'>" + tableRows.map((r, i) => {
          const tag = i === 0 ? "th" : "td";
          return "<tr>" + r.map((c) => `<${tag}>${c}</${tag}>`).join("") + "</tr>";
        }).join("") + "</table>";
        tableRows = [];
      };
      const flushList = () => { if (inList) { html += `</${listType}>`; inList = false; } };
      for (const raw of lines) {
        const s = raw.trim();
        if (!s) { flushTable(); blankPending = true; continue; }
        if (/^```/.test(s)) { flushTable(); flushList(); blankPending = false; continue; }   // ```markdown 围栏行跳过,内容按正文渲染
        if (s.startsWith("|") && s.endsWith("|")) {
          const cells = s.replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
          if (cells.every((c) => /^:?-+:?$/.test(c))) continue;   // 分隔行
          flushList(); blankPending = false;
          tableRows.push(cells);
          continue;
        } else if (tableRows.length) { flushTable(); }
        const um = s.match(/^[-*]\s+(.*)$/);
        const om = s.match(/^\d+[.)]\s+(.*)$/);
        if (um || om) {
          const type = om ? "ol" : "ul";
          // 空行后的同类型列表项续接编号;类型切换才断开重开
          if (!inList || listType !== type) { flushList(); html += `<${type}>`; inList = true; listType = type; }
          blankPending = false;
          html += `<li>${inline((um || om)[1])}</li>`;
          continue;
        }
        flushList(); blankPending = false;
        const hm = s.match(/^(#{1,6})\s+(.*)$/);
        if (hm) { html += `<h${hm[1].length}>${inline(hm[2])}</h${hm[1].length}>`; continue; }
        if (s.startsWith(">")) { html += `<blockquote>${inline(s.replace(/^>\s?/, ""))}</blockquote>`; continue; }
        if (/^[-*_]{3,}$/.test(s)) { html += "<hr>"; continue; }
        html += `<p>${inline(s)}</p>`;
      }
      flushTable(); flushList();
      return html;
    },

    // ---- 图表 ----
    initChart() {
      this.chart = echarts.init(this.$refs.chartEl);
      window.addEventListener("resize", () => this.chart && this.chart.resize());
    },
    renderChart(r) {
      if (!this.chart) this.initChart();
      const rows = r.rows || [];
      if (r.period === "min") return this.renderMinute(r, rows);

      // ---- 日K / 30分K: 蜡烛图 + 均线 + 成交量(MVP:作战地图隐藏,不再画关键位线) ----
      const x = rows.map((x) => x.t);
      const kData = rows.map((x) => [x.o, x.c, x.l, x.h]);
      const vol = rows.map((x) => x.v || 0);
      const isDay = r.period === "day";
      const maPeriods = isDay ? [5, 10, 20, 60, 120, 250] : [5, 10, 20, 60];
      const closeArr = rows.map((x) => x.c);
      const n = rows.length;

      // 均线:数值并入顶部图例(右端 endLabel 已移除,保持图区干净)
      const maLast = {};
      const maSeries = maPeriods.map((p) => {
        const data = this.ma(closeArr, p);
        maLast[p] = data[n - 1];
        return {
          name: "MA" + p, type: "line", data: data, smooth: true,
          showSymbol: false, xAxisIndex: 0, yAxisIndex: 0,
          lineStyle: { width: 1, type: p >= 120 ? "dashed" : "solid", color: this.maColor(p) },
          itemStyle: { color: this.maColor(p) }, emphasis: { disabled: true }, z: 3,
        };
      });

      const legendSelected = {};
      maPeriods.forEach((p) => { legendSelected["MA" + p] = p < 250; });  // 默认隐藏 MA250 减噪
      const option = {
        backgroundColor: "transparent", animation: false,
        tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
        // 图例一行:均线+数值(关键位线已随作战地图视图隐藏而移除)
        legend: [
          { data: maPeriods.map((p) => "MA" + p), top: 0, left: 0, selectedMode: "multiple",
            textStyle: { color: "#787b86", fontSize: 10 }, itemWidth: 12, itemHeight: 8, itemGap: 6,
            selected: legendSelected,
            formatter: (name) => {
              const p = Number(name.slice(2));
              const v = maLast[p];
              return "MA" + p + ": " + (v == null ? "-" : v.toFixed(3));
            } },
        ],
        axisPointer: { link: [{ xAxisIndex: "all" }] },
        grid: [
          // 主图顶部留白:图例两行约占 0-32px,主图从 64px 起,拉开明显距离
          { left: 58, right: 18, top: 64, height: "52%" },
          { left: 58, right: 18, top: "78%", height: "15%" },
        ],
        xAxis: [
          { type: "category", data: x, gridIndex: 0, axisLine: { lineStyle: { color: "#232a38" } }, axisLabel: { color: "#787b86", fontSize: 10 } },
          { type: "category", data: x, gridIndex: 1, axisLine: { lineStyle: { color: "#232a38" } }, axisLabel: { show: false } },
        ],
        yAxis: [
          { scale: true, gridIndex: 0, axisLabel: { color: "#787b86", fontSize: 10 }, splitLine: { lineStyle: { color: "#1b2130" } } },
          { gridIndex: 1, axisLabel: { show: false }, axisLine: { show: false }, splitLine: { show: false } },
        ],
        dataZoom: [
          { type: "slider", xAxisIndex: [0, 1], start: 55, end: 100, bottom: 0, height: 14,
            borderColor: "#232a38", fillerColor: "rgba(76,141,255,0.1)",
            // 主副图 X 轴联动:xAxisIndex [0,1] 让 K线+成交量一起平移缩放,垂直光标对齐
            // 彻底清除滑块浅色残留:数据阴影(含选中区强调态)、中央拖动白线;两端手柄改深色
            dataBackground: { show: false, lineStyle: { color: "transparent" }, areaStyle: { color: "transparent" },
                              emphasis: { lineStyle: { color: "transparent" }, areaStyle: { color: "transparent" } } },
            moveHandleStyle: { color: "transparent" },
            emphasis: { moveHandleStyle: { color: "transparent" } },
            handleStyle: { color: "#232a38", borderColor: "#787b86" } },
        ],
        series: [
          { name: "K线", type: "candlestick", data: kData, xAxisIndex: 0, yAxisIndex: 0,
            itemStyle: { color: "#f23645", color0: "#089981", borderColor: "#f23645", borderColor0: "#089981" }, z: 3 },
          ...maSeries,
          { name: "成交量", type: "bar", data: vol, xAxisIndex: 1, yAxisIndex: 1,
            itemStyle: { color: (p) => (p.dataIndex > 0 && rows[p.dataIndex].c < rows[p.dataIndex - 1].c ? "#089981" : "#f23645") } },
          { name: "MA5量", type: "line", data: this.ma(vol, 5), xAxisIndex: 1, yAxisIndex: 1,
            showSymbol: false, lineStyle: { width: 1, color: "#f0b90b" }, z: 3 },
          { name: "MA10量", type: "line", data: this.ma(vol, 10), xAxisIndex: 1, yAxisIndex: 1,
            showSymbol: false, lineStyle: { width: 1, color: "#4c8dff" }, z: 3 },
        ],
      };
      try {
        // 先剥离 Vue 响应式 Proxy 再交给 ECharts(见 plainClone 注释)
        this.chart.setOption(this.plainClone(option), true);
        // ECharts5 首次 setOption 后 dataZoom 双轴联动偶发状态损坏(滑动条/缩放卡死,报
        // "Cannot read properties of undefined (reading 'type')")。等首帧渲染稳定后再补一次
        // commit 修复;用 getInstanceByDom 取实例(避免 this.chart 引用陈旧)。
        setTimeout(() => {
          try {
            const el = this.$refs.chartEl;
            const c = el && echarts.getInstanceByDom(el);
            if (c) c.setOption(c.getOption(), true);
          } catch (e) { /* 忽略 */ }
        }, 1500);
      } catch (e) {
        this.msg = "图表渲染错误: " + e;
      }
    },
    renderMinute(r, rows) {
      const x = rows.map((x) => x.t);   // 修复:分时 x 轴此前未定义(切分时会抛 ReferenceError)
      const price = rows.map((x) => x.p);
      const vol = rows.map((x) => x.v);
      const option = {
        backgroundColor: "transparent", animation: false,
        tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
        axisPointer: { link: [{ xAxisIndex: "all" }] },
        grid: [
          { left: 58, right: 18, top: 22, height: "58%" },
          { left: 58, right: 18, top: "82%", height: "14%" },
        ],
        xAxis: [
          { type: "category", data: x, gridIndex: 0, axisLine: { lineStyle: { color: "#232a38" } }, axisLabel: { color: "#787b86", fontSize: 10 } },
          { type: "category", data: x, gridIndex: 1, axisLine: { lineStyle: { color: "#232a38" } }, axisLabel: { show: false } },
        ],
        yAxis: [
          { scale: true, gridIndex: 0, axisLabel: { color: "#787b86", fontSize: 10 }, splitLine: { lineStyle: { color: "#1b2130" } } },
          { gridIndex: 1, axisLabel: { show: false }, axisLine: { show: false }, splitLine: { show: false } },
        ],
        dataZoom: [
          { type: "inside", xAxisIndex: [0, 1], start: 0, end: 100 },
          { type: "slider", xAxisIndex: [0, 1], bottom: 0, height: 14,
            borderColor: "#232a38", fillerColor: "rgba(85,167,149,0.12)",
            dataBackground: { show: false, lineStyle: { color: "transparent" }, areaStyle: { color: "transparent" },
                              emphasis: { lineStyle: { color: "transparent" }, areaStyle: { color: "transparent" } } },
            moveHandleStyle: { color: "transparent" },
            emphasis: { moveHandleStyle: { color: "transparent" } },
            handleStyle: { color: "#232a38", borderColor: "#232a38" } },
        ],
        series: [
          { name: "价格", type: "line", data: price, xAxisIndex: 0, yAxisIndex: 0,
            showSymbol: false, lineStyle: { width: 1.5, color: "#4c8dff" },
            itemStyle: { color: "#4c8dff" }, markLine: this.minAvgLine(r) },
          { name: "成交量", type: "bar", data: vol, xAxisIndex: 1, yAxisIndex: 1,
            itemStyle: { color: (p) => (p.dataIndex > 0 && price[p.dataIndex] < price[p.dataIndex - 1] ? "#089981" : "#f23645") } },
        ],
      };
      this.chart.setOption(option, true);
    },
    minAvgLine(r) {
      if (r.period !== "min") return undefined;
      const rows = r.rows || [];
      const last = rows.length ? rows[rows.length - 1] : null;
      return {
        silent: true,
        symbol: "none",
        label: { show: false },   // 关闭均价线端点数值标签(此前挤出图表右侧被截断)
        data: [{ yAxis: last && last.avg != null ? last.avg : undefined, name: "均价" }],
        lineStyle: { color: "#e8b339", type: "dashed" },
      };
    },
    // ---- 盘后建议验证(增量4 MVP:懒验证 + 日历) ----
    async loadVerifyCalendar(force = false) {
      if (this.vcLoading) return;
      if (!force && this.vcLoaded) return;
      this.vcError = "";
      this.vcLoading = true;
      try {
        const r = await api("/api/verify/calendar");
        if (!r.ok) { this.vcError = r.error || "验证日历加载失败"; return; }
        this.vcDays = r.days || [];
        this.verifiedNote = r.verified > 0 ? `本次补判 ${r.verified} 条到期建议` : "";
        this.vcLoaded = true;
        this.vcOpenDay = "";
      } finally {
        this.vcLoading = false;
      }
    },
    // 色块:默认 5 日窗口为主(应验/部分应验=绿,未应验=红,其余=灰)
    // 色块二维编码(2026-09 用户拍板):观望=灰虚线(留痕不验证);有方向=彩色块
    // (待判=蓝框,应验=绿实心,未应验=红实心)。默认 5 日窗口为主。
    vcCls(it, win = "5") {
      if (!it.direction) return "vc-watch";
      const out = win === "3" ? it.out3 : it.out5;
      if (out === "应验" || out === "部分应验") return "vc-ok";
      if (out === "未应验") return "vc-bad";
      return "vc-pend";
    },
    vcOut(it) {
      if (!it.direction) return "观望";
      const o = it.out5;
      if (!o) return "待定";
      return o === "部分应验" ? "部分" : o;
    },
    vcTitle(it) {
      const mv = (v) => (v == null ? "" : ` 涨跌${v > 0 ? "+" : ""}${v}%`);
      const head = `${this.shortName(it.name) || it.symbol}${it.name ? `(${it.symbol})` : ""}`
        + ` · 建议:${it.category || "—"} · 状态:${it.state_word || "—"}`;
      if (!it.direction) return `${head}\n无方向(观望/持有) · 不参与验证,仅留痕`;
      const d3 = it.out3 == null ? "未到期" : `${it.out3}${mv(it.move3)}`;
      const d5 = it.out5 == null ? "未到期" : `${it.out5}${mv(it.move5)}`;
      return `${head}\n锚定价 ${it.anchor_price}\n3日 ${d3}\n5日 ${d5}`;
    },
    shortDay(dt) { return dt ? dt.slice(5).replace("-", "/") : ""; },
    weekdayCN(dt) {
      if (!dt) return "";
      return "周" + "日一二三四五六"[new Date(dt + "T00:00:00").getDay()];
    },
    fmtPx(v) { return v == null ? "-" : Number(v).toFixed(3); },
    fmtMove(v) { return v == null ? "" : ` 涨跌${v > 0 ? "+" : ""}${v}%`; },
    // 一句话拆分(方法,模板可带参数调用):第一句=现价·状态;其余=动作链
    splitSentence(s) {
      if (!s) return null;
      const i = s.indexOf("。");
      if (i <= 0) return { head: s, acts: "" };
      return { head: s.slice(0, i),
               acts: s.slice(i + 1).trim().replace(/。$/, "").trim() };
    },

    // ---- 作战地图 ----
    async loadBattlemap(force = false) {
      if (this.battlemap && !force) return;
      const r = await api("/api/battlemap");
      if (r.ok) { this.battlemap = r.model; this.mapFile = r.model.meta?.source_file || ""; }
    },

    // ---- 盘前生成(里程碑2) ----
    async loadPlaybook() {
      this.pbError = "";
      this.pbSaved = "";
      this.pbLoading = true;
      try {
        const r = await api("/api/playbook");
        if (!r.ok) { this.pbError = r.error || "生成失败"; return; }
        this.pbDraft = { summary: r.summary, rows: r.rows || [] };
        this.pbMarkdown = r.markdown || "";
      } finally {
        this.pbLoading = false;
      }
    },
    async savePlaybook() {
      this.pbSaving = true;
      this.pbError = "";
      try {
        const resp = await fetch("/api/playbook", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            summary: this.pbDraft.summary || "",
            rows: (this.pbDraft.rows || []).map((r) => ({
              code: r.code, name: r.name, overnight: r.overnight, action: r.action,
            })),
          }),
        });
        const r = await resp.json();
        if (!r.ok) { this.pbError = r.error || "保存失败"; return; }
        this.pbSaved = "已写入作战地图「每日盯盘记录」(可在下方/文件中继续编辑)";
        this.pbDraft = null;
        this.loadBattlemap(true);
      } finally {
        this.pbSaving = false;
      }
    },

    // ---- 盘前视图(实时新闻+LLM,三块联动) ----
    async loadMorning() {
      this.mgError = "";
      this.mgLoading = true;
      try {
        const r = await api("/api/morning");
        if (!r.ok) { this.mgError = r.error || "盘前视图生成失败"; return; }
        this.mgNews = r.news || null;
        this.mgHealth = r.news_health || null;
        this.mgState = r.state || null;   // 市场状态灯(T1 纯消息定级)
        this.mgPlaybook = r.playbook || [];
        this.mgDiscipline = r.discipline || [];
      } finally {
        this.mgLoading = false;
      }
    },
    newsCls(impact) {
      // 消息方向 = 通用语义(非价格):利好=绿(机会/绿灯行),利空=红(风险/红灯停)
      if (impact === "利好") return "nw up";
      if (impact === "利空") return "nw down";
      return "nw mid";
    },
    impactTag(impact) {
      return impact === "利好" ? "🟢" : impact === "利空" ? "🔴" : "⚪";
    },
    gradeTag(g) {
      return { 官方: "官", 媒体: "媒", 快讯: "快", 传闻: "闻" }[g] || g;
    },
    // 盘前状态灯点色(2026-09,交通灯语义,非涨跌语义)
    stClass(level) {
      return level === "red" ? "st-red" : level === "yellow" ? "st-yellow" : "st-green";
    },
    // 消息关键词命中标签配色:通用语义(与状态灯一致)——利空=红系(风险),利好=绿系(机会),中度=琥珀
    pmTagCls(tag) {
      if (!tag) return "";
      if (tag.startsWith("利空·高危")) return "pm-bad";
      if (tag.startsWith("利空·中度")) return "pm-warn";
      if (tag.startsWith("利好")) return "pm-good";
      return "";
    },
    async saveMorningPlaybook() {
      if (!this.mgPlaybook.length) return;
      this.pbSaving = true;
      this.mgError = "";
      this.mgSaved = "";
      try {
        const resp = await fetch("/api/playbook", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            summary: this.mgNews?.summary || "",
            rows: this.mgPlaybook.map((r) => ({ code: r.code, name: r.name, overnight: r.overnight, action: r.action })),
          }),
        });
        const r = await resp.json();
        if (!r.ok) { this.mgError = r.error || "保存失败"; return; }
        this.mgSaved = "已写入作战地图「每日盯盘记录」";
        this.loadBattlemap(true);
      } finally {
        this.pbSaving = false;
      }
    },
    // ---- 信息层评估(M6) ----
    async loadNewsStats() {
      const r = await api("/api/news/stats");
      if (r.ok) this.infoStats = r;
      this.$nextTick(() => this.renderInfoChart());
    },
    renderInfoChart() {
      const el = this.$refs.infoChartEl;
      if (!el) return;
      if (!this.infoChart) {
        this.infoChart = echarts.init(el);
        window.addEventListener("resize", () => this.infoChart && this.infoChart.resize());
      }
      const curve = this.infoStats?.stats20?.alpha_curve || [];
      this.infoChart.setOption({
        backgroundColor: "transparent", animation: false,
        tooltip: { trigger: "axis" },
        grid: { left: 50, right: 16, top: 30, bottom: 30 },
        xAxis: { type: "category", data: curve.map((e) => e[0]),
                 axisLabel: { color: "#787b86", fontSize: 10 }, axisLine: { lineStyle: { color: "#232a38" } } },
        yAxis: { type: "value", scale: true, axisLabel: { color: "#787b86", fontSize: 10 },
                 splitLine: { lineStyle: { color: "#1b2130" } } },
        series: [{ name: "信息层 alpha 累计", type: "line", data: curve.map((e) => e[1]),
                  showSymbol: false, lineStyle: { width: 2, color: "#4c8dff" },
                  itemStyle: { color: "#4c8dff" } }],
      }, true);
    },

    // ---- 回测 ----
    initBtChart() {
      this.$nextTick(() => {
        if (this.$refs.btChartEl) this.btChart = echarts.init(this.$refs.btChartEl);
      });
    },
    async runBacktest() {
      this.btLoading = true;
      this.btError = "";
      this.bt = null;
      try {
        const r = await api(`/api/backtest?start=${this.btStart}&end=${this.btEnd}&capital=${this.btCapital}`);
        if (!r.ok) { this.btError = r.error || "回测失败"; return; }
        this.bt = r.result;
        this.$nextTick(() => this.renderBtChart(r.result));
      } finally {
        this.btLoading = false;
      }
    },
    renderBtChart(res) {
      if (!this.$refs.btChartEl) return;
      if (!this.btChart) this.btChart = echarts.init(this.$refs.btChartEl);
      // nav_curve / benchmark_curve 为 [(date, value), ...]
      const nav = res.nav_curve || [];
      const bench = res.benchmark_curve || [];
      const x = nav.map((e) => e[0]);
      const rule = nav.map((e) => e[1]);
      const bh = bench.map((e) => e[1]);
      this.btChart.setOption({
        backgroundColor: "transparent",
        tooltip: { trigger: "axis" },
        legend: { data: ["规则", "买入持有"], textStyle: { color: "#787b86" } },
        grid: { left: 50, right: 16, top: 30, bottom: 30 },
        xAxis: { type: "category", data: x, axisLabel: { color: "#787b86" }, axisLine: { lineStyle: { color: "#232a38" } } },
        yAxis: { scale: true, axisLabel: { color: "#787b86" }, splitLine: { lineStyle: { color: "#1b2130" } } },
        series: [
          { name: "规则", type: "line", data: rule, showSymbol: false, lineStyle: { width: 2, color: "#4c8dff" }, itemStyle: { color: "#4c8dff" } },
          { name: "买入持有", type: "line", data: bh, showSymbol: false, lineStyle: { width: 1.5, color: "#787b86", type: "dashed" }, itemStyle: { color: "#787b86" } },
        ],
      });
    },
  },

  watch: {
    // 悬浮面板显示偏好(可选列 + 仅持仓)变更即持久化到 localStorage
    floatCols: { deep: true, handler() { this.persistFloatPrefs(); } },
    floatOnlyHeld() { this.persistFloatPrefs(); },
  },

  async mounted() {
    // 浮窗模式:只加载悬浮面板所需数据(自选股 + 轮询),隐藏其余,供 pywebview 桌面分身加载
    if (FLOAT_MODE) {
      this.floatMode = true;
      document.body.classList.add("float-mode");
      this.loadWatchlist();
      this.loadHoldings();        // 持仓标记数据源(浮窗分支此前漏加载 → 桌面面板无"持"标记)
      this._started = true;       // 浮窗分支此前未置位 → restartPolling 守卫挡住 → 面板从不轮询(自选增删/行情不自动同步)
      this.loadRefreshConfig();   // 读 refresh_interval_sec 并启动轮询(自选股+指数)
      this.defaultFloatPrefs();
      // 供托盘唤醒时补刷一次(抵消 WebView 隐藏期对定时器的节流)
      window.__fpWake = () => { this.loadWatchlist(); this.loadHoldings(); };
      // 把桌面窗口贴合到卡片实际尺寸(渲染后 + pywebview 桥就绪后各试一次)
      this.$nextTick(() => this.fitFloatWindow());
      window.addEventListener("pywebviewready", () => this.fitFloatWindow());
      return;
    }
    // 图表在 renderChart 里按需初始化(数据就绪、容器尺寸已定后再 echarts.init,避免
    // 提前 init 导致 dataZoom 状态损坏——见 renderChart 注释)
    this._started = true;
    this.loadIndices();
    this.loadWatchlist();
    this.loadBattlemap();
    this.loadSettings();       // 内含 loadRefreshConfig → 按配置启动轮询(指数+候选池)
    this.defaultFloatPrefs();
    this.$nextTick(() => this.initFloatDrag());
    // 设置弹窗:按 Esc 关闭(与 X 按钮 / 点击遮罩 三路等效)
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.settingsOpen) this.closeSettings();
    });
    // 定时轮询由 loadSettings→loadRefreshConfig 依 web.yaml 的 refresh_interval_sec 启动
    // (默认 12s,顶部指数 + 候选股池一起刷;旧式固定 60s 仅刷指数的写法已移除)。
  },
});

app.mount("#app");
