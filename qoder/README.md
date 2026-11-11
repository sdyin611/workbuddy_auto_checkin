# Qoder 国际版每日签到

把 [L0NE-6/Qoder-Daily](https://github.com/L0NE-6/Qoder-Daily) 的国际版协议实现合入本项目，
并补上了它没有做的两件事：

- **claim 用对请求形状**：国际版只带最小鉴权头、无 body 会返回
  `409 CAMPAIGN_CLAIM_PRECONDITION_FAILED`（2026-11-11 本机实测）。
  本目录的脚本改为带设备身份头 + JSON 空对象，实测返回 200。
- **入账核验**：claim 返回 `CLAIMED` 不算成功。领取前后逐池读取额度，
  轮询确认出现约 100 的增量才判成功；看不到增量就报 `GRANT_NOT_LANDED`，
  不把「接口说成功」当成「积分已到账」。

## 本机实测记录（2026-11-11）

| 时点 | 总额 | 订阅 | 加购 | 个人资源包 | 活动状态 |
| --- | ---: | ---: | ---: | ---: | --- |
| 领取前 | 1571 | 173 | 397 | 1001 | CLAIMABLE |
| 领取后 | 1670 | 172 | 497 | 1001 | CLAIMED |

- claim 返回 HTTP 200，面值 100；
- 加购额度 +100；订阅额度同期被后台消耗 1，净增 99；
- 活动状态由 `CLAIMABLE` 变为 `CLAIMED`。

## 文件

| 文件 | 用途 |
| --- | --- |
| `qoder_daily.py` | 上游 L0NE-6/Qoder-Daily 协议实现（MIT），负责 token、活动、UMID、续期 |
| `qoder_global_checkin.py` | 本项目入口：正确 claim + 额度入账核验 + 当日状态文件 |
| `qoder_login.py` | 官方设备授权登录工具，token 失效时重新取 |
| `requirements.txt` | 仅 `requests` |
| `LICENSE` / `NOTICE.md` | 上游 MIT 许可证与使用边界，保留原署名 |

## 一、配置 GitHub Actions（上云）

### 1. 放代码

把本目录 `qoder/` 和仓库根目录的 `.github/workflows/qoder-daily.yml` 放进你自己的仓库。
若已有 `workbuddy_auto_checkin` 仓库，直接覆盖/新增对应文件即可。

### 2. 添加两个必填 Secret

仓库 → Settings → Secrets and variables → Actions → New repository secret：

| Name | 值 |
| --- | --- |
| `QODER_GLOBAL_TOKENS` | 一行 `access|refresh`，形如 `dt-...|drt-...` |
| `QODER_CACHE_KEY` | 任意长随机字符串，用来加密缓存里的轮换 token |

本机已经通过官方设备授权拿到过一份 token；交付时放在**仓库外**的临时文件里，
填入 Secret 后请删除该文件，避免误提交。
如果 token 已失效，在本目录运行：

```bash
python qoder_login.py --region global
```

浏览器会打开官方授权页，确认账号后终端会输出 `dt-...|drt-...`。

### 3. 可选 Secret

| Name | 用途 |
| --- | --- |
| `FEISHU_WEBHOOK` | 失败时推飞书群机器人 |
| `SCT_SENDKEY` | 失败时推 Server酱（微信） |
| `QODER_MACHINE_TOKEN` / `QODER_MACHINE_CODE` / `QODER_MACHINE_TYPE` | 云端设备身份三件套。默认不填，脚本会自动从官方 `@qoder-ai/qodercli` 包提取 runtime-info；若云端报设备身份/风控错误，再填这三项 |

> `QODER_ALREADY_OK` 不是 Secret：云端用 workflow_dispatch 的 `already_ok` 勾选项；
> 本机运行时才用同名环境变量。

### 4. 启用定时

- 进 Actions 页签，若提示 fork 的 workflow 未启用，点
  **I understand my workflows, go ahead and enable them**。
- 左侧选 **Qoder 每日签到（国际版）** → Run workflow 手动跑一次。
- 定时为北京时间每天 **10:05 / 10:35 / 11:05** 三次互备（UTC 02:05 / 02:35 / 03:05）。

> **今天已经在本机领过的话**，云端第一次手动跑会报 `VERIFY_UNAVAILABLE`：
> 云端没有今天的「领取 + 入账」基线，严格模式不会把「已领」当成「已到账」。
> 做云端冒烟测试时，在 Run workflow 里同时勾选 `verify_only` 和 `already_ok`；
> 明天 10:05 的定时首跑才是真正的「领取 + 核验」。

### 5. 看结果

Actions 的 step summary 会显示：

- `领取并确认入账：总额 ...（加购额度 +100）` → 成功；
- `今日已领取，且本脚本已核验入账（...）` → 同日备份运行，成功；
- `GRANT_NOT_LANDED` → claim 说成功但额度没涨，**算失败**，脚本会推飞书/微信；
- `VERIFY_UNAVAILABLE` → 活动已 CLAIMED，但没有当日核验基线，**也算失败**，
  避免把「已领」误当成「已到账」。

## 二、本机运行（可选）

```bash
cd qoder
pip install -r requirements.txt

# 默认：领取 + 核验
python qoder_global_checkin.py

# 只读：不 claim，只核对今日状态
python qoder_global_checkin.py --verify-only
```

本机运行需要 `QODER_GLOBAL_TOKENS` 环境变量，或先在本目录生成
`qoder_tokens.json`（脚本续期时会自动写）。设备身份优先用
`QODER_MACHINE_*`，否则用本机 Qoder / qodercli 的 `runtime-info`。

## 三、常见问题

**409 `CAMPAIGN_CLAIM_PRECONDITION_FAILED`**
本目录的 claim 已改成「设备身份头 + JSON 空对象」，本机实测通过。若上游再次改协议，
先更新 `qoder_daily.py`（上游仓库）并确认客户端版本头；必要时补
`QODER_MACHINE_*`。

**`GRANT_NOT_LANDED`**
不要只看 claim 返回。打开 Qoder 客户端 → 用量面板 → 个人资源包 / 加购额度，
核对当天是否有 100 的增量；没有就按脚本提示联系官方并保留截图。

**token 多久失效**
配置了 refresh token 时会自动续期，轮换后的 token 存在 `qoder_tokens.json`，
并由 workflow 用 `QODER_CACHE_KEY` 加密后写入 Actions cache。
cache 被清理且旧 refresh token 已失效时，需要重新运行 `qoder_login.py` 并更新 Secret。

**cloud 上的设备身份**
默认从官方 `@qoder-ai/qodercli` 包提取 `runtime-info`，需要 runner 能访问 npm registry。
若提取失败或 claim 被风控，填入本机 `~/.qoder/umid-cache.json` 里的
`machineToken` / `machineCode` / `machineType` 三项 Secret。

## 安全

- 仓库不提交 token；`qoder_tokens.json`、`qoder_machine_id`、`qoder_state.json`、
  `qoder_umid/` 均已忽略。
- 本机导出的 token / cache key 放在仓库外，只用于粘进 GitHub Secret，粘完请删除。
- 脚本日志对 token 做掩码；不要把完整 token 贴到 Issue / 聊天 / 截图里。
- 本项目使用 Qoder 客户端私有接口，客户端升级可能改变协议；上游更新后请同步
  `qoder_daily.py`。
