#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qoder_checkin.py —— Qoder 国内版每日签到（GitHub Actions 云端执行）。

流程（全部零依赖，仅标准库）：
  1. 用长期 PAT（pt-，用户在 qoder.cn 网页自助创建）兑换 24h 访问令牌（jt-）：
     POST https://openapi.qoder.com.cn/api/v1/jobToken/exchange（纯 Bearer，无 COSY 签名）
     每次运行直接兑换、不持久化 —— PAT 长期有效，免去 WorkBuddy 方案 28 天换 token 的维护成本。
  2. GET /sash/api/v1/me/campaigns 列活动 → 对所有 CLAIMABLE 项 POST claim（接口幂等，重复领不重发），
     涵盖每日 100 Credits 及其他可领福利。
  3. 推送策略与 wb_auto.py 一致：只在签到未达成（AUTH / NETWORK / ERROR）时经 Server酱 推微信，
     成功（CLAIMED / ALREADY / 无可领项）静默。

凭证只读环境变量 QODER_PAT，不落盘、不打日志（日志统一 masked 脱敏）。
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OPENAPI_BASE = "https://openapi.qoder.com.cn"          # 国内版：PAT 兑换与 sash 活动平台同域
SASH_BASE = os.environ.get("QODER_SASH_BASE") or OPENAPI_BASE
REQUEST_TIMEOUT = 20
MAX_RETRY = 3

CODE_NO_NETWORK = -1

# 活动平台请求头集合（对齐官方桌面端 0.4.8，见 qoder2api-hub PR #75）。
# 注意：不发任何 Cosy-Machine* 机器身份头 —— cpa-multi-plugins campaign.go 的
# no-machine 变体实证：零机器头返回未过滤的完整活动列表；伪造机器身份反而触发风控。
COSY_CLIENT_TYPE = "10"
COSY_VERSION = "0.4.8"


def log(msg):
    print(msg, flush=True)


def masked(text):
    """把返回体里的 token 类字段脱敏后再落日志。"""
    if isinstance(text, dict):
        out = {}
        for k, v in text.items():
            if isinstance(v, (dict, list)):
                out[k] = masked(v)
            elif isinstance(v, str) and len(v) > 24:
                out[k] = v[:6] + "***" + v[-4:]
            else:
                out[k] = v
        return out
    if isinstance(text, list):
        return [masked(x) for x in text]
    if isinstance(text, str) and len(text) > 200:
        return text[:200] + "..."
    return text


def dig(obj, *keys):
    """逐层取值，任一层缺失返回 None。"""
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            obj = obj[k]
        else:
            return None
    return obj


def unwrap(body):
    """{code,msg,data} 信封则取 data，否则原样返回 dict。"""
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body if isinstance(body, dict) else {}


def request(url, headers, method="GET", payload=None, retry=True):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    return resp.status, {"raw": raw[:300]}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                err_body = json.loads(raw)
            except ValueError:
                err_body = {"raw": raw[:300]}
            if 300 <= e.code < 400 and attempt < MAX_RETRY:
                log("  · 收到 %d 跳转，%.1fs 后重试（%d/%d）"
                    % (e.code, 2.0 * attempt, attempt, MAX_RETRY))
                time.sleep(2.0 * attempt)
                continue
            return e.code, err_body
        except Exception as e:
            if not retry or attempt >= MAX_RETRY:
                return CODE_NO_NETWORK, {"error": "%s: %s" % (type(e).__name__, e)}
            log("  · 请求异常，%.1fs 后重试（%d/%d）" % (2.0, attempt, MAX_RETRY))
            time.sleep(2.0)


def get(url, headers):
    return request(url, headers, method="GET")


def post(url, headers, payload=None):
    return request(url, headers, method="POST", payload=payload)


def api_ok(code, body):
    return 200 <= code < 300 and (not isinstance(body, dict) or body.get("code", 0) in (0, None))


def auth_rejected(body):
    """上游凭证失效的判定标记（见 qoder2api-hub SESSION_DEAD_MARKERS）。"""
    if not isinstance(body, dict):
        return False
    msg = str(body.get("msg") or body.get("message") or body.get("error") or "")
    return any(m in msg for m in ("TOKEN_EXPIRE", "12153", "Offline user session not found"))


# ---------------------------------------------------------------- 第一步：PAT 兑换

def exchange_token(pat):
    """pt- 长期令牌 → jt- 访问令牌（24h）。返回 (access_token, err)。

    社区实现存在三种请求形状（契约未统一），按序尝试直到命中：
      A. body {"personal_token": pat}，无 Authorization 头 —— mmqz/cpa-multi-plugins
         oauth.go 的官方客户端逆向实现（2026-11 活跃维护，首选）
      B. body {"pat": pat}，无 Authorization 头
      C. 空 body + Authorization: Bearer pt-... —— 旧逆向，2026-11-08 实测 400 已证伪
    """
    url = OPENAPI_BASE + "/api/v1/jobToken/exchange"
    base_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Qoder",
    }
    shapes = [
        ("body:personal_token", dict(base_headers), {"personal_token": pat}),
        ("body:pat", dict(base_headers), {"pat": pat}),
        ("header:bearer", dict(base_headers, Authorization="Bearer %s" % pat), {}),
    ]
    last_code, last_body = None, None
    for name, headers, payload in shapes:
        code, body = post(url, headers, payload)
        log("  [兑换·%s] HTTP %s %s" % (name, code, json.dumps(masked(body), ensure_ascii=False)))
        if code == CODE_NO_NETWORK:
            return None, "NETWORK"
        if api_ok(code, body):
            data = unwrap(body)
            token = (data.get("token") or data.get("accessToken")
                     or data.get("access_token") or data.get("jobToken"))
            if token:
                return str(token), None
            log("  · 该形状返回 2xx 但未解析到 token，继续尝试下一形状")
        last_code, last_body = code, body
        if code in (401, 403) or auth_rejected(body):
            return None, "AUTH"
    # 三种形状都被拒：400 多为 PAT 无效/格式不对（2026-11-08 实测：空 body 错形状也回 400）
    if last_code in (400, 401, 403) or auth_rejected(last_body):
        return None, "AUTH"
    return None, "ERROR"


# ---------------------------------------------------------------- 第二步：签到

def campaign_headers(jt):
    return {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % jt,
        "Content-Type": "application/json",
        "User-Agent": "Qoder",
        "Cosy-ClientType": COSY_CLIENT_TYPE,
        "Cosy-Version": COSY_VERSION,
    }


def _campaign_list(data):
    """从返回体里尽量兼容地找出活动列表。"""
    for key in ("campaigns", "list", "items", "records"):
        v = data.get(key) if isinstance(data, dict) else None
        if isinstance(v, list):
            return v
    if isinstance(data, list):
        return data
    return []


def _is_claimable(item):
    # 官方客户端逆向：状态字段是 claimStatus（取值 CLAIMABLE / CLAIMED ...）
    state = str(item.get("claimStatus") or item.get("status")
                or item.get("state") or "").upper()
    return state in ("CLAIMABLE", "CAN_CLAIM", "AVAILABLE")


def _campaign_id(item):
    # campaignId 必须是 UUID（act- 开头的 key 形式会被 claim 拒绝 400）
    return item.get("campaignId") or item.get("id") or item.get("activityId")


def _claimable_desc(item):
    title = (item.get("campaignTitle") or item.get("title")
             or item.get("campaignKey") or "?")
    ben = item.get("benefit")
    amount = ben.get("amount") if isinstance(ben, dict) else None
    kind = (ben.get("kind") or "") if isinstance(ben, dict) else ""
    unit = "积分" if str(kind).upper() == "CREDITS" else kind
    return "%s%s" % (title, (" +%s %s" % (amount, unit)) if amount else "")


def run_checkin(jt):
    """查活动 → 领所有 CLAIMABLE。返回 (ok, 汇报 dict)。"""
    headers = campaign_headers(jt)
    lcode, lbody = get(SASH_BASE + "/sash/api/v1/me/campaigns", headers)
    log("  [活动列表] HTTP %s %s" % (lcode, json.dumps(masked(lbody), ensure_ascii=False)))

    if lcode == CODE_NO_NETWORK:
        return False, {"result": "NETWORK", "report": "网络不可达，签到未完成"}
    if lcode in (401, 403) or auth_rejected(lbody):
        return False, {"result": "AUTH", "report": "认证被拒（HTTP %s）" % lcode}
    if not (200 <= lcode < 300):
        return False, {"result": "ERROR", "report": "活动列表接口异常（HTTP %s）" % lcode}

    items = _campaign_list(unwrap(lbody))
    claimable = [it for it in items if isinstance(it, dict) and _is_claimable(it)]
    log("  活动共 %d 项，可领 %d 项" % (len(items), len(claimable)))

    if not claimable:
        # 无可领项：要么今天已领过，要么资格未放出/已领完 —— 幂等正常态，不算失败
        return True, {"result": "ALREADY", "report": "今日无可领活动（已领过或资格未放出）"}

    claimed, failed = [], []
    for it in claimable:
        cid = _campaign_id(it)
        if cid is None:
            failed.append("%s（缺少 campaignId）" % _claimable_desc(it))
            continue
        desc = _claimable_desc(it)
        ccode, cbody = post("%s/sash/api/v1/me/campaigns/%s/claim" % (SASH_BASE, cid),
                            headers, {})
        log("  [领取 %s] HTTP %s %s" % (cid, ccode, json.dumps(masked(cbody), ensure_ascii=False)))
        if ccode == CODE_NO_NETWORK:
            failed.append("%s（网络未送达）" % desc)
            continue
        if ccode in (401, 403):
            return False, {"result": "AUTH", "report": "认证被拒（HTTP %s）" % ccode}

        data = unwrap(cbody)
        status = str(data.get("status") or "").upper()
        failure = str(data.get("failureCode") or data.get("errorCode") or "").upper()

        # 判定顺序（对齐 cpa-multi-plugins campaign.go 的线上契约）：
        # 1) 显式 failureCode；2) HTTP 409 = 幂等重放（已领过）；3) status 判成功
        if failure:
            if any(k in failure for k in ("ALREADY", "SAME_PERSON", "REPLAYED")):
                claimed.append("%s（服务端判定已领过）" % desc)
            elif any(k in failure for k in ("OUT_OF_STOCK", "NOT_ACTIVE", "SOLD_OUT")):
                claimed.append("%s（名额已发完/活动未生效）" % desc)
            else:
                # RISK_BLOCKED / RISK_DEPENDENCY_UNAVAILABLE / ACHIEVEMENT_NOT_COMPLETED
                # / FIELD_VALIDATION_FAILED 等属真失败，原样上报
                failed.append("%s：%s（HTTP %s）" % (desc, failure, ccode))
        elif ccode == 409:
            claimed.append("%s（服务端判定已领过，HTTP 409）" % desc)
        elif api_ok(ccode, cbody):
            got = dig(data, "benefit", "amount")
            if got is None:
                got = data.get("rewardCredits") or data.get("reward_credits")
            if status in ("CLAIMED", "GRANTED", "SUCCESS", ""):
                claimed.append("%s%s" % (desc, ("（+%s）" % got) if got else ""))
            else:
                claimed.append("%s（状态 %s）" % (desc, status))
        else:
            msg = dig(cbody, "msg") or dig(cbody, "message") \
                or dig(cbody, "errorMessage") or ""
            failed.append("%s：%s（HTTP %s）" % (desc, msg or "未知", ccode))

    if claimed and not failed:
        return True, {"result": "CLAIMED", "report": "成功领取：%s" % "；".join(claimed)}
    if claimed and failed:
        # 部分成功：整体算成功（核心的每日 Credits 大概率已到手），失败项写进汇报
        return True, {"result": "CLAIMED",
                      "report": "成功领取：%s；失败：%s" % ("；".join(claimed), "；".join(failed))}
    if failed:
        return False, {"result": "ERROR", "report": "领取失败：%s" % "；".join(failed)}
    return False, {"result": "UNKNOWN", "report": "未识别的活动列表返回"}


# ---------------------------------------------------------------- Server酱（微信）

def push_serverchan(sendkey, checkin_line, overall_ok):
    """推送到 Server酱（微信服务号）。返回 (ok, reason)。只在签到未达成时调用。"""
    title = "Qoder 每日签到 · %s" % ("签到成功" if overall_ok else "签到失败")
    desp = "\n\n".join([
        "**签到**\n" + checkin_line,
        "---\nQoder PAT 长期有效。若上方显示**认证被拒**：多为 PAT 已被删除或账号退出，"
        "到 qoder.cn 设置 → Personal Access Token 重新创建，"
        "再到 GitHub 仓库 Settings → Secrets → Actions 更新 `QODER_PAT`。",
    ])
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    url = "https://sctapi.ftqq.com/%s.send" % sendkey
    attempt = 0
    while True:
        attempt += 1
        try:
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
                log("  [Server酱] HTTP %s code=%s" % (resp.status, body.get("code")))
                if body.get("code") == 0:
                    return True, ""
                return False, "Server酱返回码 %s（%s）" % (body.get("code"), body.get("message", ""))
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < MAX_RETRY:
                log("  [Server酱] HTTP %s，退避后重试（%d/%d）" % (e.code, attempt, MAX_RETRY))
                time.sleep(2.0 * attempt)
                continue
            if e.code >= 500:
                return False, "重试 %d 次后仍 HTTP %s" % (MAX_RETRY, e.code)
            return False, "HTTP %s（SendKey 无效或格式不对）" % e.code
        except Exception as e:
            if attempt < MAX_RETRY:
                log("  [Server酱] 请求异常，退避后重试（%d/%d）：%s: %s"
                    % (attempt, MAX_RETRY, type(e).__name__, e))
                time.sleep(2.0 * attempt)
                continue
            return False, "重试 %d 次后仍异常：%s: %s" % (MAX_RETRY, type(e).__name__, e)


# ---------------------------------------------------------------- 主流程

def main():
    pat = os.environ.get("QODER_PAT")
    sct_key = os.environ.get("SCT_SENDKEY")

    if not pat:
        log("缺少环境变量 QODER_PAT（在 qoder.cn 网页创建 pt- 令牌后加入 GitHub Secret）")
        print("RESULT: ERROR")
        return 2

    log("=== 1/2 PAT 兑换访问令牌 ===")
    jt, err = exchange_token(pat)
    if err:
        log("  兑换失败：%s" % err)
        checkin_ok = False
        checkin = {"result": err,
                   "report": {"NETWORK": "网络不可达，兑换未完成",
                              "AUTH": "认证被拒：PAT 无效、已删除或复制不完整，"
                                      "请到 qoder.cn 重新创建并更新 Secret QODER_PAT",
                              "ERROR": "兑换接口异常"}.get(err, "兑换失败")}
    else:
        log("=== 2/2 签到 ===")
        checkin_ok, checkin = run_checkin(jt)
    log("  结论：%s" % checkin["report"])

    overall_ok = checkin_ok
    checkin_line = ("✅ " if checkin_ok else "❌ ") + checkin["report"]

    # 推送策略与 wb_auto.py 一致：签到未达成才推送，成功静默
    if checkin_ok:
        push_ok, push_reason = None, "签到正常，按策略不推送"
    elif sct_key:
        push_ok, push_reason = push_serverchan(sct_key, checkin_line, overall_ok)
    else:
        push_ok, push_reason = None, "签到未达成，但未配置推送渠道（缺少 SCT_SENDKEY）"
    if not checkin_ok:
        log("  推送：%s" % push_reason)

    result = {
        "checkin": {"ok": checkin_ok, **checkin},
        "push": {"ok": push_ok, "reason": push_reason},
        "overall_ok": overall_ok,
    }
    print("RESULT_JSON " + json.dumps(result, ensure_ascii=False))
    print("RESULT: %s" % checkin["result"])

    # 结果文件只含结论文案，不含凭证，供 Actions 写入 step summary
    result_file = os.environ.get("QODER_RESULT_FILE")
    if result_file:
        try:
            push_line = ("✅ 已推送（%s）" % push_reason) if push_ok else \
                        (("❌ 推送失败：%s" % push_reason) if push_ok is not None
                         else "－ %s" % push_reason)
            with open(result_file, "w", encoding="utf-8") as f:
                f.write("| 项目 | 结论 |\n| --- | --- |\n")
                f.write("| 签到 | %s |\n" % checkin_line)
                f.write("| 消息推送 | %s |\n" % push_line)
                f.write("| 整体 | %s |\n" % ("成功（签到已达成）" if overall_ok else "失败（签到未达成）"))
        except OSError as e:
            log("结果文件写入失败：%s" % e)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
