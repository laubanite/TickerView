/* AlphaPrism 行情页前端(里程碑6)· Vue 3 + ECharts */
const { createApp } = Vue;

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
      // 盘中核对(里程碑3)
      checkGate: null,
      checkVerdicts: [],
      checkError: "",
      // 盘后生成(里程碑4)
      closeMd: "",
      closeError: "",
      closeLoading: false,
      // 盘前生成(里程碑2)
      pbDraft: null,
      pbMarkdown: "",
      pbError: "",
      pbLoading: false,
      pbSaving: false,
      pbSaved: "",
      // 自选股管理(§5.6)
      newSym: "",
      wlMsg: "",
      wlError: "",
      // 盘中快照(§5.4)
      snapMd: "",
      snapError: "",
      snapLoading: false,
      snapPushing: false,
    };
  },

  computed: {
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
      return this.bm?.global_?.discipline || [];
    },
    positions() {
      return (this.bm?.instruments || [])
        .filter((i) => i.position && i.position.cost != null)
        .map((i) => ({ code: i.code, name: i.name, ...i.position }));
    },
    levelLines() {
      const ins = this.currentInstrument();
      if (!ins) return [];
      const map = {};
      (ins.levels || []).forEach((l) => { map[l.name] = l.price; });
      const out = [];
      const ln = (name, color, type = "dashed") => {
        if (map[name] != null) out.push({ yAxis: map[name], name: name, lineStyle: { color, type } });
      };
      ln("买区下沿", "#ffd166");
      ln("买区上沿", "#ffd166");
      ln("突破点", "#4c8dff", "dotted");
      ln("减仓红线", "#ff9f6e", "solid");
      ln("生命线", "#ff5c6c", "solid");
      return out;
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
    setTab(t) { this.tab = t; this.view = ""; },
    toggleView(v) {
      this.view = this.view === v ? "" : v;
      if (this.view === "bt") this.initBtChart();
      else if (this.view === "map") this.loadBattlemap();
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
      return name ? name.replace(/ETF.*$/, "").replace(/.*ETF/, "") : "";
    },
    fmtQuote(snap) {
      return snap && snap.last_price != null ? this.fmt(snap.last_price, 3) : "-";
    },
    volRatio(snap) {
      // 快照无直接量比,用换手率近似(标签为换手)
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

    // ---- 自选股管理(§5.6) ----
    async addSymbol() {
      const sym = this.newSym.trim();
      if (!sym) { this.wlError = "请输入代码"; return; }
      this.wlError = "";
      this.wlMsg = "";
      try {
        const resp = await fetch("/api/watchlist", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbol: sym }),
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
      try {
        const resp = await fetch("/api/watchlist?symbol=" + encodeURIComponent(sym), { method: "DELETE" });
        const r = await resp.json();
        if (!r.ok) { this.wlError = r.error || "删除失败"; return; }
        this.wlMsg = `已移除 ${sym}`;
        if (this.current === sym) this.current = "";
        await this.loadWatchlist();
      } catch (e) { this.wlError = String(e); }
    },

    // ---- 盘中快照(§5.4) ----
    async loadSnapshot() {
      this.snapError = "";
      this.snapMd = "";
      this.snapLoading = true;
      try {
        const r = await api("/api/snapshot");
        if (!r.ok) { this.snapError = r.error || "快照失败"; return; }
        this.snapMd = r.markdown;
      } finally {
        this.snapLoading = false;
      }
    },
    async pushSnapshot() {
      if (!this.snapMd) return;
      this.snapPushing = true;
      this.snapError = "";
      try {
        const resp = await fetch("/api/snapshot/push", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ markdown: this.snapMd }),
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
      this.renderChart(r);
    },

    // ---- 图表 ----
    initChart() {
      this.chart = echarts.init(this.$refs.chartEl);
      window.addEventListener("resize", () => this.chart && this.chart.resize());
    },
    renderChart(r) {
      if (!this.chart) this.initChart();
      const rows = r.rows || [];
      const isMin = r.period === "min";
      const x = rows.map((x) => x.t);
      const price = rows.map((x) => (isMin ? x.p : x.c));
      const vol = rows.map((x) => (isMin ? x.v : x.v));
      const color = (arr) =>
        arr.map((v, i) => (i > 0 && v < arr[i - 1] ? "#089981" : "#f23645"));

      const option = {
        backgroundColor: "transparent",
        animation: false,
        tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
        axisPointer: { link: [{ xAxisIndex: "all" }] },
        grid: [
          { left: 50, right: 16, top: 12, height: "58%" },
          { left: 50, right: 16, top: "76%", height: "14%" },
        ],
        xAxis: [
          { type: "category", data: x, gridIndex: 0, axisLine: { lineStyle: { color: "#232a38" } }, axisLabel: { color: "#787b86" } },
          { type: "category", data: x, gridIndex: 1, axisLine: { lineStyle: { color: "#232a38" } }, axisLabel: { show: false } },
        ],
        yAxis: [
          { scale: true, gridIndex: 0, axisLabel: { color: "#787b86" }, splitLine: { lineStyle: { color: "#1b2130" } } },
          { gridIndex: 1, axisLabel: { color: "#787b86" }, splitLine: { show: false } },
        ],
        dataZoom: [
          { type: "inside", xAxisIndex: [0, 1], start: 0, end: 100 },
          { type: "slider", xAxisIndex: [0, 1], bottom: 0, height: 16, borderColor: "#232a38", fillerColor: "rgba(76,141,255,0.1)" },
        ],
        series: [
          { name: "价格", type: "line", data: price, xAxisIndex: 0, yAxisIndex: 0,
            showSymbol: false, lineStyle: { width: 1.5, color: "#4c8dff" },
            itemStyle: { color: "#4c8dff" },
            markLine: r.period === "min" ? this.minAvgLine(r) : this.levelMarkLine() },
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
        data: [{ yAxis: last && last.avg != null ? last.avg : undefined, name: "均价" }],
        lineStyle: { color: "#e8b339", type: "dashed" },
      };
    },
    levelMarkLine() {
      // 速查卡价位自动叠线(买区/突破/减仓红线/生命线),越界自动隐藏由 ECharts 处理
      return {
        silent: true,
        symbol: "none",
        label: { show: true, position: "insideEndTop", fontSize: 10, color: "#d1d4dc" },
        data: this.levelLines,
      };
    },

    // ---- 盘后生成(里程碑4) ----
    async loadClose() {
      this.closeError = "";
      this.closeMd = "";
      this.closeLoading = true;
      try {
        const r = await api("/api/close");
        if (!r.ok) { this.closeError = r.error || "盘后生成失败"; return; }
        this.closeMd = r.markdown;
      } finally {
        this.closeLoading = false;
      }
    },

    // ---- 盘中核对(里程碑3) ----
    async loadCheck() {
      this.checkError = "";
      const r = await api("/api/check");
      if (!r.ok) { this.checkError = r.error || "核对失败"; return; }
      this.checkGate = r.gate;
      this.checkVerdicts = r.verdicts || [];
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

  async mounted() {
    this.initChart();
    this.loadIndices();
    this.loadWatchlist();
    this.loadBattlemap();
    this.loadCheck();
    setInterval(() => this.loadIndices(), 60000);
  },
});

app.mount("#app");
