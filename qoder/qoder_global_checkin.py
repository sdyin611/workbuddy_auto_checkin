#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qoder 国际版每日签到（GitHub Actions / 本机）。

在 L0NE-6/Qoder-Daily 的协议实现之上补了两件上游脚本没做的事：
  1. claim 必须带设备身份头并发送 JSON 空对象。只带最小头、无 body 时，
     国际版会返回 409 CAMPAIGN_CLAIM_PRECONDITION_FAILED（本机 2026-11-11 实测）。
  2. claim 成功不等于入账。领取前后读取额度池，逐池核对增量（默认 100），
     轮询确认到账才判成功；看不到增量就报 GRANT_NOT_LANDED，不假报成功。

凭证：QODER_GLOBAL_TOKENS（access|refresh，支持多账号换行或 & 分隔）。
设备身份：优先 QODER_MACHINE_TOKEN/CODE/TYPE；否则用本机/官方 qodercli 的
runtime-info 生成，云端会自动从官方 npm 包提取。
"""
import argparse
import datetime
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import qoder_daily as qd  # noqa: E402

CLIENT_VERSION = os.environ.get("QODER_CLIENT_VERSION", "0.5.6").strip() or "0.5.6"
qd.client_version = lambda region: CLIENT_VERSION

BJ = datetime.timezone(datetime.timedelta(hours=8))
DEFAULT_AMOUNT = 100.0
VERIFY_POLLS = int(os.environ.get("QODER_VERIFY_POLLS", "6") or 6)
VERIFY_GAP = float(os.environ.get("QODER_VERIFY_GAP", "5") or 5)
POOL_KEYS = ("user", "addon", "org", "dedicated", "total")
POOL_LABEL = {
    "user": "订阅额度", "addon": "加购额度", "org": "组织额度",
    "dedicated": "个人资源包", "total": "总额度",
}
STATE_FILE = os.environ.get("QODER_STATE_FILE") or \
    os.path.join(BASE_DIR, "qoder_state.json")


def log(message):
    print("%s %s" % (time.strftime("[%H:%M:%S]"), message), flush=True)


def amount_of(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num_of(pool):
    if isinstance(pool, dict):
        value = amount_of(pool.get("remaining"))
        if value is not None:
            return value
        total = amount_of(pool.get("total"))
        used = amount_of(pool.get("used"))
        if total is not None and used is not None:
            return total - used
    return None


def quota_detail(account):
    """读取额度池明细。返回 (snapshot|None, err|None)。
    snapshot = {user, addon, org, dedicated, total, packages[]}。"""
    risky, _note = qd.risky_sash_headers(account, account.get("uid", ""))
    bare = {
        "authorization": "Bearer %s" % account["access"],
        "accept": "application/json",
        "user-agent": "Qoder",
    }
    for name, headers in (("risk", risky), ("bare", bare)):
        response = qd.request("GET", account["base"] + qd.QUOTA_USAGE_PATH,
                              headers=headers, retries=1)
        if response.status_code == 401 or response.status_code == 403:
            payload = qd.json_of(response)
            kind, _seconds = qd.quota_cooldown(response)
            if kind:
                return None, kind
            log("   [额度·%s] HTTP %s，认证被拒" % (name, response.status_code))
            continue
        if response.status_code != 200:
            log("   [额度·%s] HTTP %s" % (name, response.status_code))
            continue
        payload = qd.json_of(response)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            log("   [额度·%s] 返回形状不认识" % name)
            continue
        packages = data.get("dedicatedResourcePackages") or []
        dedicated = 0.0
        seen_dedicated = False
        for pkg in packages:
            value = _num_of(pkg)
            if value is not None:
                dedicated += value
                seen_dedicated = True
        user = _num_of(data.get("userQuota"))
        addon = _num_of(data.get("addOnQuota"))
        org = _num_of(data.get("orgResourcePackage"))
        total = _num_of(data)
        if total is None:
            parts = [v for v in (user, addon, org, dedicated if seen_dedicated else None)
                     if v is not None]
            total = sum(parts) if parts else None
        snapshot = {
            "user": user, "addon": addon, "org": org,
            "dedicated": dedicated if seen_dedicated else None,
            "total": total, "packages": packages,
        }
        return snapshot, None
    return None, "UNREADABLE"


def quota_line(snapshot):
    if not snapshot:
        return "额度不可读"
    return "总额 %s（订阅 %s / 加购 %s / 组织 %s / 个人包 %s）" % (
        fmt(snapshot.get("total")), fmt(snapshot.get("user")),
        fmt(snapshot.get("addon")), fmt(snapshot.get("org")),
        fmt(snapshot.get("dedicated")))


def fmt(value):
    number = amount_of(value)
    return "?" if number is None else ("%g" % number)


def today_key():
    return datetime.datetime.now(BJ).date().isoformat()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(result, before=None, after=None, detail=""):
    """记录当日 claim 结论，供同日备份运行复用，避免 10:30/11:05 把
    首次运行的 GRANT_NOT_LANDED 误报成「今日已领取」。"""
    def pools(snapshot):
        if not snapshot:
            return None
        return {key: snapshot.get(key) for key in POOL_KEYS}
    data = {
        "date": today_key(),
        "result": result,
        "before": pools(before),
        "after": pools(after),
        "detail": detail,
        "saved_at": datetime.datetime.now(BJ).isoformat(),
    }
    temp = STATE_FILE + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp, STATE_FILE)
    except OSError as error:
        log("   状态文件写入失败：%s" % error)


def already_verdict(state, snapshot=None):
    """campaign 已 CLAIMED 时，用当日状态判断这次到底是「已核验」还是
    「上次 claim 未入账」。没有基线时默认按不确定处理，不假报成功。"""
    state = state if state.get("date") == today_key() else {}
    result = state.get("result")
    detail = state.get("detail") or ""
    if result == "CLAIMED_VERIFIED":
        return {"ok": True, "result": "ALREADY",
                "report": "今日已领取，且本脚本已核验入账%s" % (
                    ("（%s）" % detail) if detail else "")}
    if result == "GRANT_NOT_LANDED":
        return {"ok": False, "result": "GRANT_NOT_LANDED",
                "report": "今日 claim 曾返回成功，但入账核验失败%s；"
                          "请到 Qoder 用量明细人工核对" % (
                              ("（%s）" % detail) if detail else "")}
    if result == "VERIFY_UNAVAILABLE":
        return {"ok": False, "result": "VERIFY_UNAVAILABLE",
                "report": "今日 claim 曾返回成功，但当时额度接口不可读%s；"
                          "请到 Qoder 用量明细人工核对" % (
                              ("（%s）" % detail) if detail else "")}
    if os.environ.get("QODER_ALREADY_OK") == "1":
        return {"ok": True, "result": "ALREADY",
                "report": "今日已领取；无当日核验基线，按 QODER_ALREADY_OK=1 视为成功"}
    extra = ("；当前%s" % quota_line(snapshot)) if snapshot else ""
    return {"ok": False, "result": "VERIFY_UNAVAILABLE",
            "report": "今日活动已 CLAIMED，但本脚本没有当日「领取 + 入账」记录，"
                      "无法确认是否到账%s。若确认已手动领取，可设 "
                      "QODER_ALREADY_OK=1" % extra}



def landed_delta(before, after, amount):
    """逐池比较。返回 (landed, pool, delta)。容忍 1 点并发消耗。"""
    if not before or not after:
        return False, "", 0.0
    threshold = amount - 1.0
    for key in POOL_KEYS:
        old, new = before.get(key), after.get(key)
        if old is None or new is None:
            continue
        delta = new - old
        if delta >= threshold:
            return True, key, delta
    return False, "", 0.0


def claim_with_identity(account, campaign_id):
    """带设备身份头 + JSON 空对象领取。国际版只带最小头会 409。"""
    headers, _note = qd.risky_sash_headers(account, account.get("uid", ""))
    headers["content-type"] = "application/json"
    if account.get("region") == "global":
        headers["origin"] = "https://qoder.com"
        headers["referer"] = "https://qoder.com/"
    else:
        headers["origin"] = "https://qoder.com.cn"
        headers["referer"] = "https://qoder.com.cn/"
    return qd.request(
        "POST",
        "%s%s/%s/claim" % (account["base"], qd.CAMPAIGNS_PATH, campaign_id),
        headers=headers, body={}, retries=1,
    )


def push_message(title, text):
    sent = []
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if webhook:
        try:
            response = qd.post_json(webhook, {
                "msg_type": "text",
                "content": {"text": "%s\n%s" % (title, text)},
            })
            if qd.json_of(response).get("StatusCode") == 0 or \
                    qd.json_of(response).get("code") == 0:
                sent.append("飞书")
        except Exception as error:
            log("   飞书推送失败：%s" % error)
    sendkey = os.environ.get("SCT_SENDKEY", "").strip()
    if sendkey:
        try:
            response = qd.post_json("https://sctapi.ftqq.com/%s.send" % sendkey,
                                    {"title": title, "desp": text})
            if str(qd.json_of(response).get("code")) == "0":
                sent.append("Server酱")
        except Exception as error:
            log("   Server酱推送失败：%s" % error)
    if sent:
        log("   已推送：%s" % " / ".join(sent))


def check_account(account, verify_only=False):
    """返回 dict：ok / result / report / detail。"""
    rows, response, risk_note, error = qd.fetch_campaign_rows(account)
    if error:
        return {"ok": False, "result": "QUERY_FAILED", "report": error}
    target, already = qd.pick_daily_campaign(rows)
    if target is None:
        if already:
            return already_verdict(load_state())
        if risk_note:
            return {"ok": False, "result": "NO_IDENTITY",
                    "report": "未下发活动且设备身份异常：%s" % risk_note}
        return {"ok": False, "result": "NOT_RELEASED",
                "report": "上游尚未下发每日 100 Credits 活动"}

    status = (target.get("claimStatus") or "").upper()
    amount = qd.campaign_benefit(target)[1] or DEFAULT_AMOUNT
    campaign_id = qd.campaign_id_of(target)
    if not campaign_id:
        return {"ok": False, "result": "BAD_CAMPAIGN",
                "report": "活动数据缺少 campaignId"}

    if verify_only:
        snapshot, error = quota_detail(account)
        if error:
            return {"ok": False, "result": "VERIFY_UNAVAILABLE",
                    "report": "只读核验失败：%s" % error}
        if status == "CLAIMED":
            verdict = already_verdict(load_state(), snapshot)
            if verdict["result"] == "ALREADY":
                verdict["result"] = "VERIFIED_ALREADY"
            return verdict
        return {"ok": False, "result": "PENDING",
                "report": "活动状态 %s，尚未领取；%s" % (status, quota_line(snapshot))}

    if risk_note and not verify_only and status != "CLAIMED":
        return {"ok": False, "result": "NO_IDENTITY",
                "report": "设备身份不可用，未发送 claim：%s" % risk_note}

    if status == "CLAIMED":
        snapshot, error = quota_detail(account)
        detail = quota_line(snapshot) if not error else "额度不可读（%s）" % error
        log("   活动状态 CLAIMED；%s" % detail)
        return already_verdict(load_state(), snapshot if not error else None)

    log("   活动 %s，面值 %s，开始领取" % (campaign_id, fmt(amount)))
    before, error = quota_detail(account)
    if error:
        return {"ok": False, "result": "VERIFY_UNAVAILABLE",
                "report": "领取前额度不可读（%s），无法核验入账" % error}
    log("   领取前：%s" % quota_line(before))

    claim = claim_with_identity(account, campaign_id)
    verdict, claimed_amount, message = qd.claim_response_verdict(claim)
    log("   claim HTTP %s / %s / %s" % (claim.status_code, verdict, message))
    if verdict == "already":
        return already_verdict(load_state(), before)
    if verdict != "claimed":
        payload = qd.json_of(claim)
        return {"ok": False, "result": "CLAIM_FAILED",
                "report": "领取失败（HTTP %s）：%s" % (
                    claim.status_code, qd.mask(payload))}

    # CLAIMED ≠ 入账：轮询额度池，确认出现 >= amount-1 的增量。
    gap = VERIFY_GAP
    last = before
    readable = False
    for attempt in range(VERIFY_POLLS):
        if attempt:
            time.sleep(gap)
            gap *= 2
        after, error = quota_detail(account)
        if error:
            log("   入账核验第 %d 次：额度不可读（%s）" % (attempt + 1, error))
            continue
        readable = True
        last = after
        landed, pool, delta = landed_delta(before, after, amount)
        log("   入账核验第 %d 次：%s%s" % (
            attempt + 1, quota_line(after),
            ("；%s +%s" % (POOL_LABEL.get(pool, pool), fmt(delta))) if landed else ""))
        if landed:
            detail = "%s +%s" % (POOL_LABEL.get(pool, pool), fmt(delta))
            save_state("CLAIMED_VERIFIED", before, after, detail)
            return {"ok": True, "result": "CLAIMED",
                    "report": "领取并确认入账：%s（%s）" % (
                        quota_line(after), detail)}
    if readable:
        detail = "%d 次轮询未见额度增加（最后：%s）" % (VERIFY_POLLS, quota_line(last))
        save_state("GRANT_NOT_LANDED", before, last, detail)
        return {"ok": False, "result": "GRANT_NOT_LANDED",
                "report": "claim 返回 CLAIMED，但" + detail +
                          "。可能被服务端静默丢弃，请到 Qoder 用量明细人工核对。"}
    detail = "claim 返回 CLAIMED，但 %d 次轮询额度接口均不可读" % VERIFY_POLLS
    save_state("VERIFY_UNAVAILABLE", before, None, detail)
    return {"ok": False, "result": "VERIFY_UNAVAILABLE",
            "report": detail + "，无法确认是否到账；请到 Qoder 用量明细人工核对。"}


def write_summary(path, lines):
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("| 项目 | 结论 |\n| --- | --- |\n")
            for line in lines:
                handle.write("| %s | %s |\n" % (line[0], line[1].replace("|", "\\|")))
    except OSError as error:
        log("结果文件写入失败：%s" % error)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true",
                        help="只读核验，不发送 claim")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()

    raw = os.environ.get("QODER_GLOBAL_TOKENS", "").strip()
    if not raw:
        log("缺少 QODER_GLOBAL_TOKENS（access|refresh，多账号换行或 & 分隔）")
        return 2
    accounts = qd.parse_accounts(raw, "global")
    if not accounts:
        log("QODER_GLOBAL_TOKENS 格式无法解析")
        return 2

    log("=== Qoder 国际版每日签到 ===")
    log("账号数：%d；客户端版本头：%s；模式：%s" % (
        len(accounts), CLIENT_VERSION, "只读核验" if args.verify_only else "领取+核验"))

    results = []
    for index, account in enumerate(accounts, 1):
        qd.apply_store(account)
        log("[%d] %s" % (index, account.get("note") or ("账号%d" % index)))
        try:
            result = check_account(account, verify_only=args.verify_only)
        except Exception as error:
            result = {"ok": False, "result": "ERROR",
                      "report": "%s: %s" % (type(error).__name__, error)}
        icon = "✅" if result["ok"] else "❌"
        log("   %s %s" % (icon, result["report"]))
        results.append((index, account, result))
        time.sleep(1.0)

    overall_ok = all(item[2]["ok"] for item in results)
    summary_lines = []
    for index, account, result in results:
        summary_lines.append((
            "账号%d %s" % (index, account.get("note") or ""),
            "%s %s（%s）" % ("✅" if result["ok"] else "❌",
                            result["report"], result["result"]),
        ))
    summary_lines.append(("整体", "成功" if overall_ok else "失败"))

    text = "🧭 Qoder 每日签到报告\n" + "\n".join(
        "- %s：%s" % (key, value) for key, value in summary_lines)
    log(text)

    if not overall_ok and not args.no_notify:
        push_message("Qoder 每日签到失败", text)
    write_summary(os.environ.get("QODER_RESULT_FILE"), summary_lines)

    print("RESULT_JSON " + json.dumps(
        {"ok": overall_ok, "results": [
            {"note": account.get("note"), **result}
            for _index, account, result in results]}, ensure_ascii=False))
    print("RESULT: %s" % ",".join(result["result"] for _i, _a, result in results))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
