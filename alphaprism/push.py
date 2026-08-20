"""推送模块(阶段5,设计方案.md §6.3):信号强提醒 + 简报静默。

支持三种通道:飞书 / 企业微信 / Server酱(PushPlus 同为 URL 型,可走 serverchan 分支改 URL)。
webhook 与密钥在 config/settings.local.yaml 的 push 段(已 gitignore),开关在 settings.yaml。

两级推送,避免疲劳轰炸:
- alert:信号出现 / 激活 / 失效 → 强提醒(人工下单)
- quiet:每日盘后简报 → 静默推送
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

import requests

from .config import Config
from .net import apply_network_policy

logger = logging.getLogger(__name__)


def _feishu_sign(secret: str, timestamp: str) -> str:
    """飞书自定义机器人加签:HMAC-SHA256(timestamp\nsecret) 后 Base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def send(title: str, body: str, level: str = "quiet", cfg: Config | None = None) -> dict:
    """按配置通道推送一条消息;未启用/未配置/失败均返回 {'ok': False, ...}。"""
    cfg = cfg or Config()
    if not cfg.get("push", "enabled", default=False):
        return {"ok": False, "error": "push disabled (settings.yaml push.enabled)"}
    channel = cfg.get("push", "channel", default="feishu")
    apply_network_policy(True)
    try:
        if channel == "feishu":
            return _send_feishu(cfg, title, body)
        if channel == "wecom":
            return _send_wecom(cfg, title, body)
        if channel == "serverchan":
            return _send_serverchan(cfg, title, body)
        return {"ok": False, "error": f"未知推送通道 {channel}"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("推送失败(%s): %s", channel, exc)
        return {"ok": False, "error": str(exc)}


def _send_feishu(cfg, title, body) -> dict:
    url = str(cfg.get("push", "feishu_webhook", default="") or "").strip()
    if not url:
        return {"ok": False, "error": "feishu_webhook 未配置(settings.local.yaml)"}
    payload = {"msg_type": "text", "content": {"text": f"{title}\n{body}"}}
    secret = str(cfg.get("push", "feishu_secret", default="") or "").strip()
    if secret:  # 加签安全设置:消息体携带 timestamp + sign
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _feishu_sign(secret, ts)
    requests.post(url, json=payload, timeout=10).raise_for_status()
    return {"ok": True}


def _send_wecom(cfg, title, body) -> dict:
    url = str(cfg.get("push", "wecom_webhook", default="") or "").strip()
    if not url:
        return {"ok": False, "error": "wecom_webhook 未配置(settings.local.yaml)"}
    payload = {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}
    requests.post(url, json=payload, timeout=10).raise_for_status()
    return {"ok": True}


def _send_serverchan(cfg, title, body) -> dict:
    key = str(cfg.get("push", "serverchan_key", default="") or "").strip()
    if not key:
        return {"ok": False, "error": "serverchan_key 未配置(settings.local.yaml)"}
    requests.post(f"https://sctapi.ftqq.com/{key}.send",
                  data={"title": title, "desp": body}, timeout=10).raise_for_status()
    return {"ok": True}


def event_message(ev: dict) -> tuple[str, str]:
    """信号事件 → (标题, 正文)。ev 来自 pipeline.refresh_signal_log。"""
    kind, sig, state = ev["kind"], ev["signal"], ev["state"]
    sym, date = ev["symbol"], ev["trade_date"]
    note = ev.get("note") or ""
    sup = f"支撑 {ev['support']}" if ev.get("support") else ""
    res = f"压力 {ev['resistance']}" if ev.get("resistance") else ""
    lv = f"{sup} / {res}" if sup or res else ""
    if kind == "fail":
        return f"⚠️ 信号失效 {sym}", f"{date} {sig}信号失效({note})\n{lv}"
    if kind == "activate":
        return f"🔔 {sig}确认 {sym}", f"{date} {sig}({state}) @ {ev.get('price')}\n{lv}\n{note}"
    return f"📋 {sig}出现 {sym}", f"{date} {sig}({state}) @ {ev.get('price')}\n{lv}\n{note}"


def _report_digest(report_md: str) -> str:
    """简报正文:提取「一、跟踪池状态」表格作为每日摘要。"""
    lines = report_md.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("## 一")), None)
    end = next((i for i, l in enumerate(lines[start + 1:], start + 1) if l.startswith("## 二")), len(lines))
    if start is None:
        return report_md[:500]
    return "\n".join(lines[start:end]).strip()


def _llm_summary(report_md: str, cfg: Config) -> str:
    """盘后 LLM 小结(2~3 句);失败返回空,推送不阻塞。"""
    from .llm import chat

    digest = _report_digest(report_md)
    prompt = (f"这是 A股ETF 盘后简报的数据表:\n{digest[:1500]}\n\n"
              f"用 2-3 句话总结今日要点(结构/信号/量价/板块拥挤度变化),简洁专业,面向中长线持仓者。")
    return (chat([{"role": "user", "content": prompt}], cfg=cfg, max_tokens=200) or "").strip()


def notify_daily(events: list[dict], report_md: str, cfg: Config | None = None) -> int:
    """run_daily 收尾:信号事件逐条强提醒 + 盘后简报(LLM 小结)静默推送。返回成功推送数。"""
    cfg = cfg or Config()
    if not cfg.get("push", "enabled", default=False):
        return 0
    sent = 0
    for ev in events:
        title, body = event_message(ev)
        if send(title, body, level="alert", cfg=cfg).get("ok"):
            sent += 1
    if report_md:
        title = "AlphaPrism 盘后简报"
        body = _report_digest(report_md)
        if cfg.get("push", "llm_summary", default=True):
            summary = _llm_summary(report_md, cfg)
            if summary:
                body = f"**今日小结**:{summary}\n\n{body}"
        if send(title, body, level="quiet", cfg=cfg).get("ok"):
            sent += 1
    return sent
