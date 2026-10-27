#!/usr/bin/env python3
"""
104 Deploy Automation - PR Collector & Notifier (v4)

排程：GitHub Actions 每 10 分鐘觸發（窗口制，窗口外即刻結束）。

STG（上線日 −3 工作天起，每 10 分）：
    搜集 STG PR（title 含 stg/[stg]、含上線日 token、已 merged）
    首次找到 STG PR → 轉主單狀態為「已更新Staging」
PROD（上線日 −1 工作天起，每 10 分）：
    搜集 PROD PR（title 含 prod/[prod]、含上線日 token、open 待簽核；簽核在 merge 前）
    有 STG PR 的 repo 全找齊對應 PROD PR → 轉主單「簽核中」
    （不是每次上線每個 repo 都在修正範圍；無 STG PR 的 repo 不等其 PROD PR）
    轉「簽核中」必須帶簽核主管（config: jira_sign_manager_account_id；Jira validator 強制）

終止（三種收尾，STG/PROD 共用；皆設 status=completed）：
    1. 轉「簽核中」成功 → 發 PROD PR 簽核通知至產品頻道（通知②，@mention 簽核人）
    2. 轉「簽核中」失敗 → 發 TPM 個人頻道人工提醒（僅首次發送），保持 active 每輪重試；
       超過停損日（上線日 +1）仍失敗才終止（stopped_reason=manual）
    3. 安全停損：PROD 一直沒找齊且 today > 上線日 +1 日曆天 → 發 TPM 個人頻道告警
    PROD 收齊時主單已被人工推進（不轉單）：
       「線上作業中」／「Production測試中」→ 直接發通知②（stopped_reason=signed）
       「Production測試完成」→ 視為已簽核，不發通知②③（stopped_reason=signed）

通知管道：依條目 notify_channel（teams／chat；缺欄位視為 teams）選 Teams 或 Google Chat 的 webhook 與訊息格式
（通知②：teams_product_channel_webhook_url／google_chat_product_webhook_url；
 TPM 個人通知：tpm.teams_personal_webhook_url／tpm.google_chat_personal_webhook_url）。

PR 搜集以「上線日 token（YYYYMMDD/YYMMDD）」比對 title + stg/prod + 已 merged，
不依賴 head/base 分支名。支援多產品：設定讀自 config.json，狀態存於 state.json。
維運搜集排除 title 含 hotfix 的 PR。

Hotfix（state.json 條目 type=hotfix，key「{產品名} hotfix {YYYYMMDD}」，以 product 對應 config）：
    不套窗口，條目進 main 後每輪搜集；title 須含上線日 token + stg/prod + hotfix（不分大小寫）
    STG 取已 merged；PROD 取 open 或已 merged；寫入 Web/API 程式資訊，不轉任何狀態
    PROD 收齊、上線單尚未到「緊急簽核」→ TPM 個人通知（僅一次），提醒手動轉「緊急簽核」
    上線單到「緊急簽核」或之後且 PROD 收齊 → 發通知②（signed）
    上線單「結案」／「取消申請」→ 結束追蹤
    停損（today > hotfix 日 +1）：PROD 已收齊但未轉緊急簽核 → TPM 個人通知；未收齊 → 不通知；皆停止追蹤（timeout）

二次上線（維運條目帶 reentry；沿用已上線的上線單，排入 pending_queue 由優先序自動升格）：
    搜集以提供日（deploy_date）與原上線日（reentry.original_deploy_date）兩個日期 token，排除第一次上線已列的 PR
    Web/API 程式資訊：首次處理時保存欄位原內容（reentry.base_text）與其中 PR 網址；讀不到時本輪暫停、每 10 分重試，首次失敗發 TPM 個人通知
    每次寫入以欄位目前內容為底：最後一條「---{M/D} 二次上線---」分隔線（含）以上一字不動，只重新產生分隔線以下的本次 PR
    （找不到分隔線時於全文後空一行加分隔線）；欄位與程式上次寫入內容不同時照常寫入，並發 TPM 個人通知列出差異
    讀寫欄位失敗：PR 照常搜集，下輪重試寫入，首次失敗發 TPM 個人通知
    STG／簽核中轉單、通知③、停損沿用維運流程；通知②標題為「{顯示名稱} {提供日} 二次上線」；不發通知①
"""

import base64
import difflib
import json
import os
import re
import time
import requests
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent

# ── Config & State ─────────────────────────────────────────────────────────────
with open(ROOT / "config.json", encoding="utf-8") as f:
    CONFIG = json.load(f)

with open(ROOT / "state.json", encoding="utf-8") as f:
    STATE = json.load(f)

GH_PAT         = os.environ["GH_PAT"]
JIRA_EMAIL     = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]

CLOUD_ID     = CONFIG["jira"]["cloud_id"]
JIRA_BASE    = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}/rest/api/3"
JIRA_BASE_V2 = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}/rest/api/2"
JIRA_API_URL = CONFIG["jira"]["base_url"]

GH_HEADERS = {
    "Authorization": f"Bearer {GH_PAT}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def jira_headers() -> dict:
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ── 工作日回推（只跳週六日；國定假日視為工作日）────────────────────────
def subtract_working_days(d: date, n: int) -> date:
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # 0=Mon … 4=Fri
            n -= 1
    return d


# ── GitHub: PR 搜集 (title 日期 token + stg/prod + merged) ──────────────────────
STG_PATTERN    = r"(?i)(\bstg\b|\[stg\])"
PROD_PATTERN   = r"(?i)(\bprod\b|\[prod\])"
HOTFIX_PATTERN = r"(?i)hotfix"


def deploy_date_tokens(deploy_date_str: str) -> list:
    """由 YYYYMMDD 產生比對用的日期 token（共用，不 per-product 寫死格式）：
    - 8 碼 YYYYMMDD（例：20260702，農場）
    - 6 碼 YYMMDD  （例：260616，CMS）
    未來要支援其他格式，只需在此處擴充。"""
    tokens = [deploy_date_str]
    if len(deploy_date_str) == 8:
        tokens.append(deploy_date_str[2:])
    out = []
    for t in tokens:
        if t not in out:
            out.append(t)
    return out


def _gh_search_with_retry(q: str, max_retries: int = 3) -> list:
    """呼叫 GitHub Search API，遇 403 自動 retry + exponential backoff。
    回傳 items list；全部重試失敗回空 list。"""
    backoff_secs = [5, 15, 30]
    for attempt in range(max_retries + 1):
        resp = requests.get(
            "https://api.github.com/search/issues",
            headers=GH_HEADERS,
            params={"q": q, "per_page": 100},
        )
        if resp.status_code == 200:
            return resp.json().get("items", [])
        if resp.status_code == 403 and attempt < max_retries:
            wait = int(resp.headers.get("Retry-After",
                                        backoff_secs[min(attempt, len(backoff_secs) - 1)]))
            print(f"  ⏳ search ({q[:60]}…): HTTP 403, "
                  f"retry {attempt + 1}/{max_retries} after {wait}s")
            time.sleep(wait)
        else:
            print(f"  ⚠️  search ({q[:60]}…): HTTP {resp.status_code}")
            return []
    return []


def search_merged_prs_by_title(repo: str, tokens: list, pr_state: str = "merged") -> list:
    """搜集 title 含任一上線日 token 的 PR（依 pr_state 篩選，跨 token 去重）。
    pr_state：STG 用 "merged"（已上 STG 才貼上線單）；PROD 用 "open"（簽核在 merge 之前）。
    使用 GitHub Search API，完全不依賴 head/base 分支名稱。
    遇 HTTP 403（secondary rate limit）自動 retry + exponential backoff。
    回傳 [{"title": ..., "html_url": ...}]。"""
    found = {}
    for token in tokens:
        q = f"repo:{repo} is:pr is:{pr_state} in:title {token}"
        items = _gh_search_with_retry(q)
        for item in items:
            found[item["html_url"]] = item.get("title", "")
        time.sleep(2)  # GitHub Search API 節流（加長間隔避免觸發 secondary rate limit）
    return [{"title": t, "html_url": u} for u, t in found.items()]


def search_hotfix_prs(repo: str, tokens: list) -> list:
    """hotfix 用：每個上線日 token 一次「不限狀態」查詢（跨 token 去重），由程式自行區分 open／merged。
    回傳 [{"title", "html_url", "state", "merged"}]。"""
    found = {}
    for token in tokens:
        q = f"repo:{repo} is:pr in:title {token}"
        for item in _gh_search_with_retry(q):
            found[item["html_url"]] = {
                "title":    item.get("title", ""),
                "html_url": item["html_url"],
                "state":    item.get("state", ""),
                "merged":   bool((item.get("pull_request") or {}).get("merged_at")),
            }
        time.sleep(2)  # GitHub Search API 節流
    return list(found.values())


def filter_by_title(prs: list, pattern: str) -> list:
    regex = re.compile(pattern, re.IGNORECASE)
    return [pr for pr in prs if regex.search(pr.get("title", ""))]


def exclude_hotfix(prs: list) -> list:
    """維運搜集用：排除 title 含 hotfix 的 PR（hotfix PR 由 hotfix 條目搜集）。"""
    regex = re.compile(HOTFIX_PATTERN)
    return [pr for pr in prs if not regex.search(pr.get("title", ""))]


# ── Jira: 依「目的地狀態」轉單（共用邏輯）───────────────────────────────────────
# 所有上線單共用同一個 ITPMTALREQ workflow。轉單不綁 transition 名稱或 id：
# 掃目前可用 transition，找「to.name == 目的地狀態」的那顆，用 Jira 當場給的 id 執行。
STG_TARGET_STATUS  = "STAGING測試中"   # 首筆 STG PR → 上線單推進到此狀態
PROD_TARGET_STATUS = "簽核中"          # PROD 全找齊 → 上線單推進到此狀態

# 轉「簽核中」（申請上線）的 workflow validator 強制必填「簽核主管」：
# 欄位 metadata 標非必填，但 validator 會擋（400：「請選擇簽核主管」），轉單時必須帶。
SIGN_MANAGER_FIELD = "customfield_11300"

# 主單狀態已達或超過 STAGING測試中 → 視為 STG 轉單完成，不再重試
# （單可能被人工提前推進；此時已無法再轉「STAGING測試中」，重試永遠失敗）
STG_REACHED_OR_PASSED = {
    "STAGING測試中", "Staging測試完成", "簽核中",
    "Production測試中", "Production測試完成", "線上作業中", "結案",
}

# PROD 收齊時主單已被人工推進過「簽核中」→ 不轉單（已無法轉回「簽核中」）
PROD_NOTIFY_WITHOUT_TRANSITION = {"線上作業中", "Production測試中"}   # 直接發通知②
PROD_SIGNED_STATUSES           = {"Production測試完成"}             # 視為已簽核，不發通知②③

# hotfix：上線單由 TPM 手動轉「緊急簽核」；程式只讀狀態、不轉單
HOTFIX_EMERGENCY_REACHED = {"緊急簽核", "線上作業中", "Production測試中", "Production測試完成"}
HOTFIX_CLOSED            = {"結案", "取消申請"}


def get_issue_status(issue_key: str) -> str:
    """取得主單目前狀態名稱；失敗回空字串。"""
    resp = requests.get(
        f"{JIRA_BASE}/issue/{issue_key}?fields=status",
        headers=jira_headers(),
    )
    if resp.status_code != 200:
        print(f"  ⚠️  取得 issue 狀態失敗: HTTP {resp.status_code}")
        return ""
    return resp.json().get("fields", {}).get("status", {}).get("name", "")


def transition_to_status(issue_key: str, target_status: str, fields: dict = None) -> bool:
    # 已在目標狀態（例如被人工或前次執行轉過）→ 視為成功，讓後續通知流程繼續
    if get_issue_status(issue_key) == target_status:
        print(f"  ✅ {issue_key} 已在「{target_status}」，視為轉單成功")
        return True
    resp = requests.get(
        f"{JIRA_BASE}/issue/{issue_key}/transitions",
        headers=jira_headers(),
    )
    if resp.status_code != 200:
        print(f"  ⚠️  取得 transitions 失敗: HTTP {resp.status_code}")
        return False
    transitions = resp.json().get("transitions", [])
    for t in transitions:
        if t.get("to", {}).get("name") == target_status:
            payload = {"transition": {"id": t["id"]}}
            if fields:
                payload["fields"] = fields
            r = requests.post(
                f"{JIRA_BASE}/issue/{issue_key}/transitions",
                headers=jira_headers(),
                json=payload,
            )
            if r.status_code != 204:
                # 必須印出 Jira 回應內容，否則無從得知 validator 錯誤（如「請選擇簽核主管」）
                print(f"  ❌ 轉單失敗: HTTP {r.status_code} {r.text[:300]}")
                return False
            return True
    avail = [f"{t.get('name')}→{t.get('to', {}).get('name')}" for t in transitions]
    print(f"  ⚠️  找不到可轉到「{target_status}」的 transition。目前可用：{avail}")
    return False


# ── Jira: Web/API 程式資訊 Field ───────────────────────────────────────────────
def build_web_api_adf(stg_prs: dict, prod_prs: dict, repo_display_names: dict) -> dict:
    # URL 以純文字輸出（不加 link mark），避免此欄位以 wiki 樣式把連結渲染成 [url|url]。
    # 多筆 PR 用 ADF 巢狀清單呈現真縮排（Jira 檢視/編輯都保留），清單項目為緊湊排版；
    # 逐行不再有段落空隙，僅 [STG PR]／[PROD PR] 兩區塊間保留一個空行分隔。
    def tpara(text: str) -> dict:
        return {"type": "paragraph", "content": [{"type": "text", "text": text}]}

    def blank() -> dict:
        return {"type": "paragraph"}

    def li(*nodes) -> dict:
        return {"type": "listItem", "content": list(nodes)}

    def stg_item(display: str, urls: list) -> dict:
        if not urls:
            return li(tpara(f"{display}："))
        if len(urls) == 1:
            return li(tpara(f"{display}：{urls[0]}"))
        # 多筆：label 一行 + 巢狀子清單，每個 URL 一個子項目（真縮排、無逐行空隙）
        sub = {"type": "bulletList", "content": [li(tpara(u)) for u in urls]}
        return li(tpara(f"{display}："), sub)

    def prod_item(display: str, url) -> dict:
        return li(tpara(f"{display}：{url}" if url else f"{display}："))

    stg_items = [stg_item(repo_display_names.get(r, r.split("/")[-1]), urls)
                 for r, urls in stg_prs.items()]
    prod_items = [prod_item(repo_display_names.get(r, r.split("/")[-1]), url)
                  for r, url in prod_prs.items()]

    content = [tpara("[STG PR]")]
    if stg_items:
        content.append({"type": "bulletList", "content": stg_items})
    content.append(blank())
    content.append(tpara("[PROD PR]"))
    if prod_items:
        content.append({"type": "bulletList", "content": prod_items})
    return {"version": 1, "type": "doc", "content": content}


def update_web_api_field(issue_key: str, field_id: str, adf: dict) -> bool:
    resp = requests.put(
        f"{JIRA_BASE}/issue/{issue_key}",
        headers=jira_headers(),
        json={"fields": {field_id: adf}},
    )
    if resp.status_code not in (200, 204):
        print(f"  ❌ Jira field update failed: {resp.status_code} {resp.text[:300]}")
        return False
    print(f"  ✅ Updated {issue_key} Web/API 程式資訊")
    return True


# ── 二次上線（reentry）：Web/API 程式資訊欄位保存與追加 ──────────────────────────
GITHUB_PR_URL_PATTERN = r"https://github\.com/[^\s/]+/[^\s/]+/pull/\d+"


def reentry_separator_text(deploy_date_str: str) -> str:
    """分隔線文字：「---{提供日 M/D} 二次上線---」。"""
    return f"---{int(deploy_date_str[4:6])}/{int(deploy_date_str[6:8])} 二次上線---"


def get_web_api_text(issue_key: str, field_id: str):
    """以 REST API v2 讀取欄位實際儲存的文字（不經 ADF 轉換）。回傳 (是否成功, 文字)；欄位為空回 (True, "")。"""
    resp = requests.get(f"{JIRA_BASE_V2}/issue/{issue_key}?fields={field_id}", headers=jira_headers())
    if resp.status_code != 200:
        print(f"  ⚠️  讀取 {issue_key} Web/API 程式資訊失敗: HTTP {resp.status_code}")
        return False, ""
    value = resp.json().get("fields", {}).get(field_id)
    if value is None:
        return True, ""
    if not isinstance(value, str):
        print(f"  ⚠️  {issue_key} Web/API 程式資訊不是文字格式，無法保存原內容")
        return False, ""
    return True, value.replace("\r\n", "\n")


def put_web_api_text(issue_key: str, field_id: str, text: str) -> bool:
    """以 REST API v2 寫入文字（原樣存放，不經 ADF 轉換）。"""
    resp = requests.put(f"{JIRA_BASE_V2}/issue/{issue_key}", headers=jira_headers(),
                        json={"fields": {field_id: text}})
    if resp.status_code not in (200, 204):
        print(f"  ❌ Jira field update failed: {resp.status_code} {resp.text[:300]}")
        return False
    print(f"  ✅ Updated {issue_key} Web/API 程式資訊")
    return True


def build_web_api_text(stg_prs: dict, prod_prs: dict, repo_display_names: dict) -> str:
    """本次 PR 文字，格式與第一次上線欄位實際儲存的樣式相同。"""
    lines = ["[STG PR]"]
    for r, urls in stg_prs.items():
        d = repo_display_names.get(r, r.split("/")[-1])
        if len(urls) == 1:
            lines.append(f"* {d}：{urls[0]}")
        else:
            lines.append(f"* {d}：")
            lines += [f"** {u}" for u in urls]
    lines += ["", "[PROD PR]"]
    for r, url in prod_prs.items():
        d = repo_display_names.get(r, r.split("/")[-1])
        lines.append(f"* {d}：{url}" if url else f"* {d}：")
    return "\n".join(lines)


def extract_pr_urls(text: str) -> list:
    found = []
    for u in re.findall(GITHUB_PR_URL_PATTERN, text or ""):
        if u not in found:
            found.append(u)
    return found


def capture_reentry_base(deployment: dict, field_id: str) -> bool:
    """二次上線首次處理：保存欄位原文字（第一次上線 PR，稽核來源）與其中的 PR 網址。"""
    ok, text = get_web_api_text(deployment["deploy_issue_key"], field_id)
    if not ok:
        return False
    deployment["reentry"]["base_text"] = text
    deployment["reentry"]["exclude_urls"] = extract_pr_urls(text)
    print(f"  📌 已保存原內容（{len(text)} 字；排除第一次上線 PR {len(deployment['reentry']['exclude_urls'])} 筆）")
    return True


def build_reentry_text(current: str, deployment: dict, repo_display_names: dict) -> str:
    """以欄位目前內容為底：最後一條分隔線（含）以上一字不動，只重新產生分隔線以下的本次 PR；
    找不到分隔線時，於目前全文後空一行加分隔線與本次 PR。"""
    sep   = reentry_separator_text(deployment["deploy_date"])
    lines = current.split("\n")
    idx   = max((i for i, l in enumerate(lines) if l.strip() == sep), default=None)
    if idx is not None:
        head = "\n".join(lines[:idx + 1])
    elif current.strip():
        head = current.rstrip("\n") + "\n\n" + sep
    else:
        head = sep
    return head + "\n" + build_web_api_text(deployment["stg_prs"], deployment["prod_prs"], repo_display_names)


def _diff_lines(old: str, new: str) -> list:
    """逐行差異：「- 舊行」「+ 新行」。"""
    return [f"{l[0]} {l[2:]}" for l in difflib.ndiff(old.split("\n"), new.split("\n")) if l[:2] in ("- ", "+ ")]


def update_reentry_field(product_name: str, deployment: dict, field_id: str,
                         repo_display_names: dict) -> bool:
    """以欄位目前內容為底寫入（分隔線以上一字不動）。欄位與程式上次寫入（未寫過則為保存的原內容）不同時照常寫入，
    並發 TPM 個人通知列出差異。讀寫失敗：不寫入、下輪重試，首次失敗發 TPM 個人通知（每張單一次）。"""
    reentry = deployment["reentry"]
    ok, current = get_web_api_text(deployment["deploy_issue_key"], field_id)
    text = build_reentry_text(current, deployment, repo_display_names) if ok else ""
    if not ok or not put_web_api_text(deployment["deploy_issue_key"], field_id, text):
        print("  ⚠️  Web/API 程式資訊讀寫失敗，本輪未寫入，下輪重試")
        if not reentry.get("write_fail_notice_sent"):
            notify_reentry_tpm(product_name, deployment, "write_failed")
            reentry["write_fail_notice_sent"] = True
        return False
    expected = reentry.get("last_written_text", reentry["base_text"])
    if current != expected:
        cur_lines, new_lines = current.split("\n"), text.split("\n")
        head_len  = next((i + 1 for i in range(len(cur_lines) - 1, -1, -1)
                          if cur_lines[i].strip() == reentry_separator_text(deployment["deploy_date"])), len(cur_lines))
        new_tail  = new_lines[head_len:]
        overwritten = [f"- {l}" for l in cur_lines[head_len:] if l.strip() and l not in new_tail]
        print("  ℹ️  欄位有人工異動，已照常寫入並通知 TPM")
        notify_reentry_tpm(product_name, deployment, "changed", _diff_lines(expected, current), overwritten)
    reentry["last_written_text"] = text
    return True


# ── Notifications（Teams／Google Chat）──────────────────────────────────────────
def _post_card(webhook_url: str, title: str, body: str, entities: list, channel: str = "teams") -> bool:
    if channel == "chat":
        resp = requests.post(webhook_url, json={"text": f"*{title}*\n\n{body}"})
        return resp.status_code in (200, 202)
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.2",
                "msteams": {"entities": entities},
                "body": [
                    {"type": "TextBlock", "text": title,
                     "weight": "Bolder", "size": "Medium"},
                    {"type": "TextBlock", "text": body, "wrap": True},
                ],
            },
        }],
    }
    resp = requests.post(webhook_url, json=payload)
    return resp.status_code in (200, 202)


def send_signing_notification(product_name: str, deployment: dict, product_config: dict) -> bool:
    """轉「簽核中」成功（或 PROD 收齊時主單已在「線上作業中」／「Production測試中」）→ 發 PROD PR 簽核通知至產品頻道（通知②），@mention 簽核人。"""
    channel     = deployment.get("notify_channel", "teams")
    webhook_key = "google_chat_product_webhook_url" if channel == "chat" else "teams_product_channel_webhook_url"
    webhook_url = product_config.get(webhook_key, "")
    if not webhook_url or "FILL_IN" in webhook_url:
        print(f"  ⚠️  {webhook_key} not configured for {product_name}")
        return False

    jira_url       = deployment["deploy_issue_url"]
    prod_prs       = deployment["prod_prs"]
    repo_names     = product_config["repo_display_names"]
    approvers      = product_config.get("approvers", CONFIG.get("approvers", []))
    display_name   = product_config["display_name"]
    deploy_date    = deployment["deploy_date"]

    pr_lines = [
        f"   - {repo_names.get(repo, repo.split('/')[-1])}：{url}"
        for repo, url in prod_prs.items() if url
    ]
    entities = [
        {"type": "mention", "text": f"<at>{a['name']}</at>",
         "mentioned": {"id": a["aad_id"], "name": a["name"]}}
        for a in approvers if a.get("aad_id")
    ]
    mention_str = " ".join(f"<at>{a['name']}</at>" for a in approvers if a.get("aad_id"))
    if channel == "chat":
        entities    = []
        mention_str = " ".join(f"<users/{a['google_chat_id']}>" for a in approvers
                               if a.get("google_chat_id") and "FILL_IN" not in a["google_chat_id"])

    if deployment.get("type") == "hotfix":
        title = f"🚀 PROD PR 簽核通知｜{display_name} {deploy_date} hotfix"
        body = (
            f"{mention_str} Hi all，{display_name}本次 hotfix 所有 PROD PR 已就緒，"
            f"請抽空簽核 hotfix 單與 PR，感謝\n\n"
            f"- hotfix 單：{jira_url}\n- PR：\n" + "\n".join(pr_lines)
        )
    else:
        title = f"🚀 PROD PR 簽核通知｜{display_name} {deploy_date}"
        if deployment.get("reentry"):
            title += " 二次上線"
        body = (
            f"{mention_str} Hi all，{display_name}本次上線所有 PROD PR 已就緒，"
            f"請抽空簽核上線單與 PR，感謝\n\n"
            f"- 上線單：{jira_url}\n- PR：\n" + "\n".join(pr_lines)
        )

    ok = _post_card(webhook_url, title, body, entities, channel)
    if ok:
        print(f"  ✅ Signing notification sent for {product_name} ({channel})")
    else:
        print(f"  ❌ Signing notification failed ({channel})")
    return ok


def _tpm_personal_target(deployment: dict):
    """依條目 notify_channel 取 TPM 個人通知的 (channel, 設定欄位名, webhook URL)。"""
    channel = deployment.get("notify_channel", "teams")
    url_key = "google_chat_personal_webhook_url" if channel == "chat" else "teams_personal_webhook_url"
    return channel, url_key, CONFIG.get("tpm", {}).get(url_key, "")


def notify_manual_transition_needed(product_name: str, deployment: dict):
    """轉「簽核中」失敗（hotfix：PROD 已收齊、尚未轉「緊急簽核」），發純文字提醒至 TPM 個人聊天。
    payload 格式：teams {"message": "..."}（Power Automate 運算式：triggerBody()?['message']）；chat {"text": "..."}"""
    channel, url_key, webhook_url = _tpm_personal_target(deployment)
    if not webhook_url or "FILL_IN" in webhook_url:
        print(f"  ⚠️  {url_key} 未設定，跳過人工轉單提醒")
        return
    issue_key = deployment["deploy_issue_key"]
    jira_url  = deployment["deploy_issue_url"]
    if deployment.get("type") == "hotfix":
        message = (
            f"⚠️ {deployment.get('product', product_name)}：hotfix PROD PR 已收齊\n"
            f"{issue_key} 所有 PROD PR 已收齊，請手動轉「緊急簽核」，轉單後將自動發送通知②。\n"
            f"hotfix 單：{jira_url}"
        )
    else:
        message = (
            f"⚠️ {product_name}：需人工轉單\n"
            f"{issue_key} 無法自動轉為「簽核中」，請手動操作。\n"
            f"上線單：{jira_url}"
        )
    resp = requests.post(webhook_url, json={"text" if channel == "chat" else "message": message})
    if resp.status_code in (200, 202):
        print(f"  ✅ 人工轉單提醒已發送（{channel}）")
    else:
        print(f"  ❌ 人工轉單提醒失敗: HTTP {resp.status_code}")


def notify_prod_timeout(product_name: str, deployment: dict):
    """安全停損：上線日+1 仍未蒐齊 PROD（hotfix：PROD 已收齊但仍未轉「緊急簽核」）→ 發告警至 TPM 個人聊天（僅個人頻道）。"""
    channel, url_key, webhook_url = _tpm_personal_target(deployment)
    if not webhook_url or "FILL_IN" in webhook_url:
        print(f"  ⚠️  {url_key} 未設定，跳過安全停損告警")
        return
    issue_key = deployment["deploy_issue_key"]
    jira_url  = deployment["deploy_issue_url"]
    if deployment.get("type") == "hotfix":
        message = (
            f"⚠️ {deployment.get('product', product_name)}：hotfix 日 +1 仍未轉「緊急簽核」，已停止自動追蹤。\n"
            f"{issue_key} 所有 PROD PR 已收齊，轉「緊急簽核」後請手動補發通知②。\n"
            f"hotfix 單：{jira_url}"
        )
    else:
        message = (
            f"⚠️ {product_name}：上線日 +1 仍未蒐齊 PROD PR，已停止自動搜集。\n"
            f"請人工確認 {issue_key}，並視情況手動轉「簽核中」。\n"
            f"上線單：{jira_url}"
        )
    resp = requests.post(webhook_url, json={"text" if channel == "chat" else "message": message})
    if resp.status_code in (200, 202):
        print(f"  ✅ 安全停損告警已發送（{channel}）")
    else:
        print(f"  ❌ 安全停損告警失敗: HTTP {resp.status_code}")


def notify_reentry_tpm(product_name: str, deployment: dict, kind: str,
                       diff: list = None, overwritten: list = None):
    """二次上線 TPM 個人通知：
    changed       ：欄位有人工異動，已照常寫入（列出差異與被覆蓋的分隔線以下內容）
    capture_failed：首次讀不到原內容，本輪暫停搜集，每 10 分重試
    write_failed  ：寫入時讀寫失敗，本輪未寫入，PR 搜集照常，每 10 分重試
    unreadable    ：至提供日 +1 仍讀不到原內容，已停止追蹤"""
    channel, url_key, webhook_url = _tpm_personal_target(deployment)
    if not webhook_url or "FILL_IN" in webhook_url:
        print(f"  ⚠️  {url_key} 未設定，跳過二次上線 TPM 個人通知")
        return
    issue_key = deployment["deploy_issue_key"]
    jira_url  = deployment["deploy_issue_url"]
    if kind == "changed":
        lines = [f"⚠️ {product_name}：二次上線 Web/API 程式資訊欄位有人工異動，已照常寫入本次 PR。",
                 f"{issue_key} 異動內容（- 程式上次寫入／+ 目前欄位）："] + (diff or [])
        if overwritten:
            lines += ["本次寫入覆蓋的分隔線以下內容："] + overwritten
        lines += ["分隔線以上（第一次上線內容）程式未變動。", f"上線單：{jira_url}"]
        message = "\n".join(lines)
    elif kind == "capture_failed":
        message = (
            f"⚠️ {product_name}：二次上線讀不到 Web/API 程式資訊原內容，本輪暫停搜集。\n"
            f"{issue_key} 每 10 分鐘重試；至提供日 +1 仍讀不到即停止追蹤（不寫入任何內容）。\n"
            f"上線單：{jira_url}"
        )
    elif kind == "write_failed":
        message = (
            f"⚠️ {product_name}：二次上線 Web/API 程式資訊欄位讀寫失敗，本輪未寫入。\n"
            f"{issue_key} PR 搜集照常進行，每 10 分鐘重試寫入，成功後不另行通知。\n"
            f"上線單：{jira_url}"
        )
    else:
        message = (
            f"⚠️ {product_name}：二次上線讀不到 Web/API 程式資訊原內容，已停止自動追蹤。\n"
            f"{issue_key} 至提供日 +1 仍無法讀取第一次上線內容，程式未寫入任何內容，請人工確認。\n"
            f"上線單：{jira_url}"
        )
    resp = requests.post(webhook_url, json={"text" if channel == "chat" else "message": message})
    if resp.status_code in (200, 202):
        print(f"  ✅ 二次上線 TPM 個人通知已發送（{channel}）")
    else:
        print(f"  ❌ 二次上線 TPM 個人通知失敗: HTTP {resp.status_code}")


def _activate_next_pending(deployment: dict, repos: list) -> bool:
    """active 條目簽核完成後，從 pending_queue 啟用下一筆部署。"""
    queue = deployment.get("pending_queue", [])
    if not queue:
        return False
    queue.sort(key=lambda e: e["deploy_date"])  # 確保升格的是上線日最早的條目
    nxt = queue.pop(0)
    deployment.update({
        "deploy_date":        nxt["deploy_date"],
        "stg_date":           nxt["stg_date"],
        "deploy_issue_key":   nxt["deploy_issue_key"],
        "deploy_issue_url":   nxt["deploy_issue_url"],
        "epic_key":           nxt["epic_key"],
        "notify_channel":     nxt.get("notify_channel", "teams"),
        "stg_prs":            {repo: [] for repo in repos},
        "prod_prs":           {repo: None for repo in repos},
        "stg_status_updated": False,
        "signing_notified":   False,
        "status":             "active",
        "pending_queue":      queue,
    })
    deployment.pop("manual_notice_sent", None)
    deployment.pop("stopped_reason", None)
    if "reentry" in nxt:        # 二次上線資料隨條目搬移；一般條目不得殘留
        deployment["reentry"] = nxt["reentry"]
    else:
        deployment.pop("reentry", None)
    print(f"  🔄 Activated next pending: {nxt['deploy_issue_key']} ({nxt['deploy_date']})")
    return True


def _normalize_priority(deployment: dict, repos: list) -> bool:
    """
    若 pending_queue 中有比目前 active 更早的 deploy_date，
    自動升格為 active，原 active 降回 pending_queue（並按 deploy_date 排序）。
    回傳 True 表示有發生調整。
    """
    queue = deployment.get("pending_queue", [])
    if not queue:
        return False
    earliest = min(queue, key=lambda e: e["deploy_date"])
    if earliest["deploy_date"] >= deployment["deploy_date"]:
        return False  # 目前 active 已是最早，無需調整
    # 建立舊 active 的 pending entry（6 個欄位，含 notify_channel）
    old_entry = {k: deployment[k] for k in
                 ("deploy_date", "stg_date", "deploy_issue_key", "deploy_issue_url", "epic_key")}
    old_entry["notify_channel"] = deployment.get("notify_channel", "teams")
    if "reentry" in deployment:     # 二次上線資料隨條目搬移
        old_entry["reentry"] = deployment["reentry"]
    # 新 pending_queue：移除被升格的條目，加入舊 active，按日期排序
    new_queue = [e for e in queue if e is not earliest] + [old_entry]
    new_queue.sort(key=lambda e: e["deploy_date"])
    print(f"  🔄 優先序調整：{earliest['deploy_issue_key']}（{earliest['deploy_date']}）→ active；"
          f"{old_entry['deploy_issue_key']}（{old_entry['deploy_date']}）→ pending_queue")
    deployment.update({
        "deploy_date":        earliest["deploy_date"],
        "stg_date":           earliest["stg_date"],
        "deploy_issue_key":   earliest["deploy_issue_key"],
        "deploy_issue_url":   earliest["deploy_issue_url"],
        "epic_key":           earliest["epic_key"],
        "notify_channel":     earliest.get("notify_channel", "teams"),
        "stg_prs":            {repo: [] for repo in repos},
        "prod_prs":           {repo: None for repo in repos},
        "stg_status_updated": False,
        "signing_notified":   False,
        "status":             "active",
        "pending_queue":      new_queue,
    })
    deployment.pop("manual_notice_sent", None)
    deployment.pop("stopped_reason", None)
    if "reentry" in earliest:       # 二次上線資料隨條目搬移；一般條目不得殘留
        deployment["reentry"] = earliest["reentry"]
    else:
        deployment.pop("reentry", None)
    return True


# ── Hotfix ─────────────────────────────────────────────────────────────────────
def process_hotfix(name: str, deployment: dict, product_config: dict, today: date) -> bool:
    """處理一筆 hotfix 條目（type=hotfix）；回傳 state 是否有變動。
    與維運條目完全獨立：不套窗口、不轉單、不使用 pending_queue。"""
    changed    = False
    repos      = product_config["repos"]
    repo_names = product_config["repo_display_names"]
    field_id   = product_config["jira_web_api_field"]
    if "stg_prs" not in deployment:
        deployment["stg_prs"] = {repo: [] for repo in repos}
        changed = True
    if "prod_prs" not in deployment:
        deployment["prod_prs"] = {repo: None for repo in repos}
        changed = True

    deploy_date_str = deployment["deploy_date"]
    deploy_date     = datetime.strptime(deploy_date_str, "%Y%m%d").date()
    tokens          = deploy_date_tokens(deploy_date_str)
    issue_key       = deployment["deploy_issue_key"]
    cutoff          = deploy_date + timedelta(days=1)   # 停損：hotfix 日 +1 日曆天

    print(f"\n{'='*55}")
    print(f"🔥 {name} | hotfix={deploy_date_str} | today={today} | {issue_key}")
    print(f"   停損={cutoff} | tokens={tokens}")

    status = get_issue_status(issue_key)
    print(f"   Jira 狀態：{status or '（取得失敗）'}")
    if status in HOTFIX_CLOSED:
        print(f"  ⏭️  上線單已「{status}」→ 結束追蹤")
        deployment["status"] = "completed"
        return True

    # ── 搜集（STG：已 merged；PROD：open 或已 merged；title 皆須含 hotfix）──
    print("🔍 hotfix: collecting...")
    any_new = False
    for repo in repos:
        display = repo_names.get(repo, repo.split("/")[-1])
        prs     = filter_by_title(search_hotfix_prs(repo, tokens), HOTFIX_PATTERN)

        existing = list(deployment["stg_prs"].get(repo, []))
        stg_new  = [pr["html_url"] for pr in filter_by_title(prs, STG_PATTERN)
                    if pr["merged"] and pr["html_url"] not in existing]
        if stg_new:
            deployment["stg_prs"][repo] = existing + stg_new
            any_new = True
            for u in stg_new:
                print(f"  ✅ STG {display}: {u}")

        if not deployment["prod_prs"].get(repo):
            prod = [pr for pr in filter_by_title(prs, PROD_PATTERN)
                    if pr["state"] == "open" or pr["merged"]]
            if prod:
                deployment["prod_prs"][repo] = prod[0]["html_url"]
                any_new = True
                print(f"  ✅ PROD {display}: {prod[0]['html_url']}")

    if any_new:
        changed = True
        adf = build_web_api_adf(deployment["stg_prs"], deployment["prod_prs"], repo_names)
        update_web_api_field(issue_key, field_id, adf)

    # 收齊範圍 = 有 STG PR 的 repo（判斷標準與維運相同）
    scoped_repos = [r for r in repos if deployment["stg_prs"].get(r)]
    all_found    = bool(scoped_repos) and all(deployment["prod_prs"].get(r) for r in scoped_repos)

    # ── 緊急簽核判斷（狀態已到「緊急簽核」或之後即視為已轉，避免排程錯過短暫停留）──
    if status in HOTFIX_EMERGENCY_REACHED:
        if not deployment.get("emergency_reached"):
            deployment["emergency_reached"] = True
            changed = True
        if all_found:
            print("  🎉 已轉緊急簽核且 PROD 收齊 → 通知②")
            if send_signing_notification(name, deployment, product_config):
                deployment["signing_notified"] = True
                deployment["status"] = "completed"
                deployment["stopped_reason"] = "signed"
                changed = True
            return changed
    elif status and all_found and not deployment.get("ready_notice_sent"):
        print("  ⚠️  PROD 已收齊、尚未轉緊急簽核 → TPM 個人通知（僅一次）")
        notify_manual_transition_needed(name, deployment)
        deployment["ready_notice_sent"] = True
        changed = True

    # ── 停損 ──
    if today > cutoff:
        if all_found and not deployment.get("emergency_reached"):
            print("  ⏰ hotfix 日 +1：PROD 已收齊但未轉緊急簽核 → TPM 個人通知，停止追蹤")
            notify_prod_timeout(name, deployment)
        else:
            print("  ⏰ hotfix 日 +1 仍未收齊 PROD → 停止追蹤（不通知）")
        deployment["status"] = "completed"
        deployment["stopped_reason"] = "timeout"
        changed = True

    return changed


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    today = date.today()
    state_changed = False

    active = STATE.get("active_deployments", {})
    if not active:
        print("ℹ️  No active deployments.")
        return

    for product_name, deployment in active.items():
        if deployment.get("status") == "completed":
            print(f"⏭️  {product_name}: completed, skipping")
            continue

        product_config = CONFIG.get("products", {}).get(deployment.get("product", product_name))
        if not product_config:
            print(f"⚠️  '{product_name}' not in config.json")
            continue

        # hotfix 條目：獨立流程（不套窗口、不轉單、不使用 pending_queue）
        if deployment.get("type") == "hotfix":
            if process_hotfix(product_name, deployment, product_config, today):
                state_changed = True
            continue

        deploy_date_str = deployment["deploy_date"]
        deploy_date     = datetime.strptime(deploy_date_str, "%Y%m%d").date()
        tokens          = deploy_date_tokens(deploy_date_str)
        issue_key       = deployment["deploy_issue_key"]
        field_id        = product_config["jira_web_api_field"]
        repos           = product_config["repos"]
        repo_names      = product_config["repo_display_names"]

        stg_start   = subtract_working_days(deploy_date, 3)
        prod_start  = subtract_working_days(deploy_date, 1)
        prod_cutoff = deploy_date + timedelta(days=1)   # 安全停損：上線日 +1 日曆天

        if "stg_prs" not in deployment:
            deployment["stg_prs"] = {repo: [] for repo in repos}
            state_changed = True
        if "prod_prs" not in deployment:
            deployment["prod_prs"] = {repo: None for repo in repos}
            state_changed = True
        if "pending_queue" not in deployment:
            deployment["pending_queue"] = []
            state_changed = True

        # 優先序正規化：若 pending_queue 有更早的上線日，自動升為 active
        if _normalize_priority(deployment, repos):
            state_changed = True
            deploy_date_str = deployment["deploy_date"]
            deploy_date     = datetime.strptime(deploy_date_str, "%Y%m%d").date()
            tokens          = deploy_date_tokens(deploy_date_str)
            issue_key       = deployment["deploy_issue_key"]
            stg_start       = subtract_working_days(deploy_date, 3)
            prod_start      = subtract_working_days(deploy_date, 1)
            prod_cutoff     = deploy_date + timedelta(days=1)

        # 二次上線：原上線日 token 一併搜集（提供日＝原上線日當天或隔天）
        reentry = deployment.get("reentry")
        if reentry:
            for tok in deploy_date_tokens(reentry["original_deploy_date"]):
                if tok not in tokens:
                    tokens.append(tok)

        print(f"\n{'='*55}")
        print(f"📦 {product_name} | deploy={deploy_date_str} | today={today} | {issue_key}"
              + (f" | 二次上線（原上線日 {reentry['original_deploy_date']}）" if reentry else ""))
        print(f"   STG 起算={stg_start} | PROD 起算={prod_start} | 停損={prod_cutoff} | tokens={tokens}")

        if today < stg_start:
            print("  ⏳ 未到 STG 窗口，跳過")
            continue

        # 二次上線：首次處理先保存欄位原內容；保存失敗本輪不做任何事，下輪重試
        if reentry and "base_text" not in reentry:
            if not capture_reentry_base(deployment, field_id):
                if today > prod_cutoff:     # 停損日仍無法保存 → 比照安全停損收尾，避免佔住追蹤位置
                    print("  ⏰ 提供日 +1 仍讀不到原內容 → 停止追蹤（未寫入任何內容），個人頻道通知，接回下一張")
                    notify_reentry_tpm(product_name, deployment, "unreadable")
                    deployment["status"] = "completed"
                    deployment["stopped_reason"] = "timeout"
                    state_changed = True
                    if _activate_next_pending(deployment, repos):
                        state_changed = True
                else:
                    print("  ⚠️  原內容保存失敗，本輪略過，下輪重試")
                    if not reentry.get("capture_fail_notice_sent"):
                        notify_reentry_tpm(product_name, deployment, "capture_failed")
                        reentry["capture_fail_notice_sent"] = True
                        state_changed = True
                continue
            state_changed = True
        exclude_urls = set(reentry.get("exclude_urls", [])) if reentry else set()

        # ── STG 窗口 ──
        print("🔍 STG: collecting...")
        any_new = False
        for repo in repos:
            display  = repo_names.get(repo, repo.split("/")[-1])
            existing = set(deployment["stg_prs"].get(repo, []))
            prs      = search_merged_prs_by_title(repo, tokens)
            stg      = exclude_hotfix(filter_by_title(prs, STG_PATTERN))
            new_urls = [pr["html_url"] for pr in stg
                        if pr["html_url"] not in existing and pr["html_url"] not in exclude_urls]
            if new_urls:
                deployment["stg_prs"][repo] = list(existing) + new_urls
                any_new = True
                for u in new_urls:
                    print(f"  ✅ STG {display}: {u}")
            else:
                print(f"  ⏳ STG {display}: no new")

        if any_new:
            state_changed = True
            if reentry:
                reentry["field_pending"] = True
            else:
                adf = build_web_api_adf(deployment["stg_prs"], deployment["prod_prs"], repo_names)
                update_web_api_field(issue_key, field_id, adf)
        if reentry and reentry.get("field_pending"):
            if update_reentry_field(product_name, deployment, field_id, repo_names):
                reentry.pop("field_pending")
            state_changed = True

        # STG 轉單：每次排程都重試（不綁 any_new），只要已有 STG PR 且尚未轉過即嘗試，
        # 避免「首次轉單失敗後、因無新 PR 而永遠不再重試」而卡住。
        # 若主單狀態已達或超過「STAGING測試中」（例如被人工推進），直接視為完成，不再重試。
        if not deployment.get("stg_status_updated") and any(urls for urls in deployment["stg_prs"].values()):
            current_status = get_issue_status(issue_key)
            if current_status in STG_REACHED_OR_PASSED:
                print(f"  ✅ {issue_key} 已在「{current_status}」（達到或超過 {STG_TARGET_STATUS}），視為完成")
                deployment["stg_status_updated"] = True
                state_changed = True
            else:
                print(f"  🔄 Transitioning {issue_key} → {STG_TARGET_STATUS}...")
                if transition_to_status(issue_key, STG_TARGET_STATUS):
                    deployment["stg_status_updated"] = True
                    state_changed = True
                    print(f"  ✅ Transitioned to {STG_TARGET_STATUS}")

        # ── PROD 窗口 ──
        if today >= prod_start:
            print("🚀 PROD: collecting...")
            any_new = False
            for repo in repos:
                display = repo_names.get(repo, repo.split("/")[-1])
                if deployment["prod_prs"].get(repo):
                    print(f"  ✅ PROD {display}: already collected")
                    continue
                prs  = search_merged_prs_by_title(repo, tokens, "open")  # PROD 抓 open：簽核在 merge 之前
                prod = [pr for pr in exclude_hotfix(filter_by_title(prs, PROD_PATTERN))
                        if pr["html_url"] not in exclude_urls]
                if prod:
                    deployment["prod_prs"][repo] = prod[0]["html_url"]
                    any_new = True
                    print(f"  ✅ PROD {display}: {prod[0]['html_url']}")
                else:
                    print(f"  ⏳ PROD {display}: none yet")

            if any_new:
                state_changed = True
                if reentry:
                    reentry["field_pending"] = True
                else:
                    adf = build_web_api_adf(deployment["stg_prs"], deployment["prod_prs"], repo_names)
                    update_web_api_field(issue_key, field_id, adf)
            if reentry and reentry.get("field_pending"):
                if update_reentry_field(product_name, deployment, field_id, repo_names):
                    reentry.pop("field_pending")
                state_changed = True

            # 收齊範圍 = 有 STG merged PR 的 repo；無 STG PR 的 repo 不在本次修正範圍，不等其 PROD PR
            scoped_repos = [r for r in repos if deployment["stg_prs"].get(r)]
            all_found = bool(scoped_repos) and all(deployment["prod_prs"].get(r) for r in scoped_repos)
            if all_found:
                scope_str = ", ".join(repo_names.get(r, r.split("/")[-1]) for r in scoped_repos)
                current_status = get_issue_status(issue_key)
                if current_status in PROD_NOTIFY_WITHOUT_TRANSITION | PROD_SIGNED_STATUSES:
                    # 主單已被人工推進過「簽核中」：不轉單、不發通知③
                    if current_status in PROD_SIGNED_STATUSES:
                        print(f"  ✅ All PROD found (scope: {scope_str}). {issue_key} 已在「{current_status}」→ 視為已簽核，不發通知②③")
                        done = True
                    else:
                        print(f"  ✅ All PROD found (scope: {scope_str}). {issue_key} 已在「{current_status}」→ 不轉單，直接發通知②")
                        done = send_signing_notification(product_name, deployment, product_config)
                        if done:
                            deployment["signing_notified"] = True
                    if done:
                        deployment["status"] = "completed"
                        deployment["stopped_reason"] = "signed"
                        state_changed = True
                        if _activate_next_pending(deployment, repos):
                            state_changed = True
                    continue
                print(f"  🎉 All PROD found (scope: {scope_str}). Transitioning {issue_key} → {PROD_TARGET_STATUS}...")
                # 轉「簽核中」必須帶簽核主管（validator 必填）
                sign_manager_id = product_config.get("jira_sign_manager_account_id", "")
                prod_fields = {SIGN_MANAGER_FIELD: {"accountId": sign_manager_id}} if sign_manager_id else None
                if not sign_manager_id:
                    print("  ⚠️  jira_sign_manager_account_id 未設定，轉單不帶簽核主管，可能被 validator 擋下")
                if transition_to_status(issue_key, PROD_TARGET_STATUS, prod_fields):
                    print(f"  ✅ Transitioned to {PROD_TARGET_STATUS}")
                    # 轉單成功：發 PROD PR 簽核通知至產品頻道（通知②）
                    if send_signing_notification(product_name, deployment, product_config):
                        deployment["signing_notified"] = True
                        deployment["status"] = "completed"
                        deployment["stopped_reason"] = "signed"
                        state_changed = True
                        if _activate_next_pending(deployment, repos):
                            state_changed = True
                else:
                    # 轉單失敗：通知③僅首次發送（manual_notice_sent 防重複），
                    # 保持 active 由排程每 10 分重試（如 QA 尚未轉「Staging測試完成」的時序問題可自癒）；
                    # 超過停損日（上線日 +1）仍失敗 → 再提醒一次並終止
                    if today > prod_cutoff:
                        print("  ⏰ 轉「簽核中」重試至停損日仍失敗 → 個人頻道提醒，終止")
                        notify_manual_transition_needed(product_name, deployment)
                        deployment["status"] = "completed"
                        deployment["stopped_reason"] = "manual"
                        state_changed = True
                        if _activate_next_pending(deployment, repos):
                            state_changed = True
                    else:
                        print("  ⚠️  轉「簽核中」失敗 → 保持 active，下輪重試（通知③僅首次發送）")
                        if not deployment.get("manual_notice_sent"):
                            notify_manual_transition_needed(product_name, deployment)
                            deployment["manual_notice_sent"] = True
                            state_changed = True
            elif today > prod_cutoff:
                print("  ⏰ 上線日+1 仍未蒐齊 PROD → 安全停損，個人頻道告警，終止")
                notify_prod_timeout(product_name, deployment)
                deployment["status"] = "completed"
                deployment["stopped_reason"] = "timeout"
                state_changed = True
                if _activate_next_pending(deployment, repos):
                    state_changed = True

    if state_changed:
        with open(ROOT / "state.json", "w", encoding="utf-8") as f:
            json.dump(STATE, f, indent=2, ensure_ascii=False)
        print("\n📝 state.json saved")


if __name__ == "__main__":
    main()
