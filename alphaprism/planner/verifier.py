"""llm-as-a-verifier 适配模块(304 架构:候选生成 → 规则过滤 → verifier 打分选优 → refine)。

移植自 llm-as-a-verifier(准则分解 + 细粒度打分)的"退化实现":
- 免费档(glm-flash / DeepSeek-V3 / Qwen 均实测 logprobs=NO)→ 不用 logprob 期望,
  改为 多次采样取均值(稳定性近似)。
- 盲评:verifier 只见「事实卡片 + 该候选」,不见其他候选,防自我强化。
- Ground Truth Note:唯一可靠证据 = 事实卡片(程序确定性输出),与 AlphaPrism
  "数据快照=唯一事实源"铁律同构。

生产路径不动;本模块只服务 A/B 实验与后续接入。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

CRITERIA_TEXT = """
你是报告质量评审。对下面【事实卡片】与【候选报告】按 5 条准则打分。

Ground Truth Note:唯一可靠的事实是【事实卡片】(数据快照+盘面状态中的程序确定性输出:
状态词/操作参数/大盘趋势维度+动能维度/KDJ标注/位置档/相对强弱/资金解读映射)。候选报告的任何
数字、方向、定性若与卡片冲突即判低分;不要因为报告写得流畅就加分,不要相信报告的自评。

1. assertiveness(结论判断力,0-100):先读报告中的【结论卡】JSON(one_sentence/signal_type/
   priority/trigger/invalidation)再判断——结论是否给出「方向 + 条件触发 + 失效」,
   而非复述事实/状态词、非"等待观察"套话;无结论卡或结论卡极简(纯复述)→ 判低分;
2. priority(多周期优先级,0-100):是否按"日线MACD定方向、30分KDJ定节奏"权衡;
   零下空头环境不被评"偏暖/强势"(判动能维度 DIF/DEA 零轴,不打包);30分超买区 J 上翘不被说成"动能积蓄/修复";
3. fact_align(事实一致性,0-100):引用的价位/涨跌幅/方向词(含相对强弱 跑赢/跑输 符号)
   与卡片一致;未出现卡片外的操作价位;
4. executable(操作可执行性,0-100):触发是否带确认刻度(放量/日线收盘/30分K收盘),
   失效是否带来源;未出现"可考虑加仓/可试多"等无触发措辞;试多符合准入链(当前缺项列明);
5. clarity(叙事结构,0-100):每段=事实→分析→结论;无跨段复读("需回踩确认支撑"只出现一次);
   术语准确("背离"仅指价格-指标背离)。

对任一准则得分 < 80,必须同时给出扣分证据:在该准则后追加 "<key>_evidence":"报告原句摘录(扣分依据)"。
输出【仅一个 JSON】,不要任何其他内容:
{"assertiveness":0-100,"priority":0-100,"fact_align":0-100,"executable":0-100,
"clarity":0-100,"overall":0-100,"weakest":"assertiveness|priority|fact_align|executable|clarity",
 "feedback":"针对 weakest 的一句中文修正建议",
 "assertiveness_evidence":"…","priority_evidence":"…","fact_align_evidence":"…",
 "executable_evidence":"…","clarity_evidence":"…"}
"""


def _verifier_prompt(snapshot_md: str, candidate_md: str,
                     rule_issues: list[str] | None = None) -> str:
    head = (
        CRITERIA_TEXT
        + "\n\n【事实卡片】\n" + snapshot_md
        + "\n\n【候选报告】\n" + candidate_md
    )
    if rule_issues:
        head += ("\n\n【程序规则链前置发现】\n(该报告生成时,以下问题句已被确定性规则链判定违规并删除;"
                 "这些是强证据——对应准则应显著扣分,不要因为正文已删就放行):\n- "
                 + "\n- ".join(rule_issues[:8]))
    return head + "\n\n输出 JSON:"


def _parse_scores(text: str) -> dict:
    """容错解析 verifier JSON:截取首个 {…} 块,数值钳位 0-100;另收集 <key>_evidence 证据句。"""
    out: dict = {}
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return out
    try:
        js = json.loads(m.group(0))
    except json.JSONDecodeError:
        return out
    for key in ("assertiveness", "priority", "fact_align", "executable",
                "clarity", "overall"):
        v = js.get(key)
        if isinstance(v, (int, float)):
            out[key] = max(0, min(100, int(round(float(v)))))
    if isinstance(js.get("weakest"), str):
        out["weakest"] = js["weakest"]
    if isinstance(js.get("feedback"), str):
        out["feedback"] = js["feedback"].strip()
    for key in ("assertiveness", "priority", "fact_align", "executable", "clarity"):
        ev = js.get(f"{key}_evidence")
        if isinstance(ev, str) and ev.strip():
            out[f"{key}_evidence"] = ev.strip()[:220]
    return out


def score_candidate(snapshot_md: str, candidate_md: str, cfg,
                    chat_call=None, samples: int = 2,
                    rule_issues: list[str] | None = None) -> dict:
    """满分 1 次调用;samples>1 → 多次采样取均值(替代 logprob 期望的退化);
    rule_issues: 规则链删句记录(强证据,喂给 verifier 做扣分依据)。

    chat_call: 可注入(测试用),默认 alphaprism.llm.chat。
    """
    import sys
    sys.path.insert(0, ".")
    from ..llm import chat as _default_chat

    chat = chat_call or _default_chat
    scores: dict = {}
    for _ in range(max(1, samples)):
        text = chat([{"role": "user",
                      "content": _verifier_prompt(snapshot_md, candidate_md,
                                                  rule_issues)}],
                    cfg, temperature=0.1, max_tokens=500, timeout=150) or ""
        ps = _parse_scores(text)
        for k, v in ps.items():
            if isinstance(v, (int, float)):
                scores.setdefault(k, []).append(v)
            elif v:
                scores.setdefault(k, v)      # weakest/feedback/evidence 原样(取最后一次)
    if not scores:
        return {"overall": 0, "reason": "verifier 无有效输出"}
    avg = {k: (sum(v) / len(v) if isinstance(v, list) else v)
           for k, v in scores.items()}
    avg["_n"] = samples
    return avg


def select_best(snapshot_md: str, candidates: list[str], cfg,
                chat_call=None, samples: int = 2,
                rule_issues: list[list[str]] | None = None) -> tuple[str, list[dict]]:
    """对候选集盲评打分,返回 (最优候选, 各候选得分)。空输入 → ("", [])。

    rule_issues: 与 candidates 等长的规则链删句记录列表(每候选一份)。
    """
    scored = []
    for i, cand in enumerate(candidates):
        ri = (rule_issues[i] if rule_issues and i < len(rule_issues) else None)
        sc = score_candidate(snapshot_md, cand, cfg, chat_call=chat_call,
                             samples=samples, rule_issues=ri)
        sc["_candidate"] = i
        scored.append(sc)
        logger.info("候选#%d overall=%s (weakest=%s)", i,
                    sc.get("overall"), sc.get("weakest"))
    if not scored:
        return "", []
    best_i = max(range(len(scored)), key=lambda i: scored[i].get("overall") or 0)
    return candidates[best_i], scored


def select_best_with_refine(snapshot_md: str, candidates: list[str], cfg,
                            chat_call=None, samples: int = 2,
                            rule_issues: list[list[str]] | None = None) -> tuple[str, list[dict], bool]:
    """选优 + 按 weakest refine 一轮 + 复评退路。

    返回 (最终报告, 各候选得分, refine 是否被采纳)。refine 后 re-score,
    分数未升 → 保留原选优候选(防 refine 劣化,不倒退)。
    """
    best, scored = select_best(snapshot_md, candidates, cfg, chat_call=chat_call,
                               samples=samples, rule_issues=rule_issues)
    if not scored:
        return "", [], False
    idx_best = max(range(len(scored)), key=lambda i: scored[i].get("overall") or 0)
    weakest = scored[idx_best].get("weakest")
    feedback = scored[idx_best].get("feedback")
    if not weakest or not feedback:
        return best, scored, False
    refined = refine(best, weakest, feedback, snapshot_md, cfg, chat_call=chat_call)
    if not refined or refined == best:
        return best, scored, False
    ri = (rule_issues[idx_best] if rule_issues and idx_best < len(rule_issues) else None)
    rs = score_candidate(snapshot_md, refined, cfg, chat_call=chat_call,
                         samples=max(1, samples), rule_issues=ri)
    if (rs.get("overall") or 0) > (scored[idx_best].get("overall") or 0):
        logger.info("refine 采纳: %s → %.1f", scored[idx_best].get("overall"), rs.get("overall"))
        return refined, scored, True
    logger.info("refine 分数未升(%.1f ≤ %.1f),保留原候选",
                rs.get("overall"), scored[idx_best].get("overall"))
    return best, scored, False


# --------------------------------------------------------------------------- 步骤2:evidence 结构化归档(喂回结论卡先行)

_EVIDENCE_DIR = None  # 延迟绑定(repo data/evidence),便于测试注入

MAX_EVIDENCE_AGE_DAYS = 30    # 过期天数:盘面会变,超期评审教训不注入
MAX_EVIDENCE_KEEP = 10        # 每标的保留最近条数


def _evidence_path(code: str) -> str:
    global _EVIDENCE_DIR
    if _EVIDENCE_DIR is None:
        import os
        _EVIDENCE_DIR = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "data", "evidence")
    import os
    return os.path.join(_EVIDENCE_DIR, f"{code}.json")


def persist_evidence(code: str, date: str, best_scores: dict) -> bool:
    """把选中候选的 verifier 扣分证据结构化归档(per-symbol,JSON)。

    条目:{date, overall, weakest, feedback, criteria_evidence{...}};
    仅存有证据/反馈的条目;每标的保留最近 MAX_EVIDENCE_KEEP 条。
    """
    import json
    import os

    evidences = {}
    for k in ("assertiveness", "priority", "fact_align", "executable", "clarity"):
        ev = best_scores.get(f"{k}_evidence")
        if ev:
            evidences[k] = ev
    if not evidences and not best_scores.get("feedback"):
        return False
    entry = {"date": date, "overall": best_scores.get("overall"),
             "weakest": best_scores.get("weakest"),
             "feedback": best_scores.get("feedback"),
             "criteria_evidence": evidences}
    path = _evidence_path(code)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    items = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                items = json.load(f)
        except (OSError, ValueError):
            items = []
    items = [i for i in items if isinstance(i, dict)]
    items.insert(0, entry)
    items = items[:MAX_EVIDENCE_KEEP]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    return True


def load_evidence(code: str, max_items: int = 5) -> list[tuple[str, str]]:
    """读取该标的最新评审缺陷(未过期): [(date, "准则:证据句"), ...]。

    供结论卡先行/报告生成注入("该标的已知坑");超 MAX_EVIDENCE_AGE_DAYS 条目不注入。
    """
    import json
    import os
    from datetime import datetime

    path = _evidence_path(code)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    today = datetime.now().date()
    for it in items:
        if not isinstance(it, dict):
            continue
        d = str(it.get("date") or "")[:10]
        try:
            age = (today - datetime.strptime(d, "%Y-%m-%d").date()).days
        except ValueError:
            age = 0
        if age > MAX_EVIDENCE_AGE_DAYS:
            continue
        for crit, ev in (it.get("criteria_evidence") or {}).items():
            if ev:
                out.append((d, f"{crit}:{ev}"))
        if it.get("feedback") and len(out) < max_items * 4:
            out.append((d, f"feedback:{it['feedback']}"))
        if len(out) >= max_items:
            break
    return out[:max_items]


def refine(md: str, weakest: str, feedback: str, snapshot_md: str, cfg,
           chat_call=None) -> str:
    """针对 weakest 准则做 1 轮修订:只改文字表述,禁止改价/新增价位/改类别。

    修订后由调用方再过规则校验链。失败返回原 md。
    """
    import sys
    sys.path.insert(0, ".")
    from ..llm import chat as _default_chat

    chat = chat_call or _default_chat
    prompt = (
        "你是分析报告修改员。有一份候选报告被评为 weakest 准则 = {w}(feedback: {fb})。\n"
        "请只针对该准则修订报告:可改表述/补判断/删注水,但 ① 所有价位与方向必须与【事实卡片】一致"
        "(禁止改价、禁止新增卡片外价位);② 不得出现'建议类别'变体与指令性抄作词;③ 其余准则高分段落"
        "不要劣化;④ 输出修订后的**完整报告**。\n\n"
        "【事实卡片】\n" + snapshot_md + "\n\n【候选报告】\n" + md +
        "\n\n输出修订后完整报告:"
    )
    try:
        text = chat([{"role": "user", "content": prompt}], cfg,
                    temperature=0.3, max_tokens=2200, timeout=180)
        return text.strip() if text else md
    except Exception as exc:  # noqa: BLE001
        logger.warning("refine 失败(保留原候选): %s", exc)
        return md