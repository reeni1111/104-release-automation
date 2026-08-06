#!/usr/bin/env python3
"""
104 Deploy Automation - PR Collector & Notifier (v4)

排程：GitHub Actions 每 30 分鐘觸發（窗口制，窗口外即刻結束）。

STG（上線日 −3 工作天起，每 30 分）：
    搜集 STG PR（title 含 stg/[stg]、含上線日 token、已 merged）
    首次找到 STG PR → 轉主單狀態為「已更新Staging」
PROD（上線日 −1 工作天起，每 30 分）：
    搜集 PROD PR（title 含 prod/[prod]、含上線日 token、open 待簽核；簽核在 merge 前）
    有 STG PR 的 repo 全找齊對應 PROD PR → 轉主單「簽核中」
    （不是每次上線每個 repo 都在修正範圍；無 STG PR 的 repo 不等其 PROD PR）
    轉「簽核中」必須帶簽核主管（config: jira_sign_manager_account_id；Jira validator 強制）

終止（三種收尾，STG/PROD 共用；皆設 status=completed）：
    1. 轉「簽核中」成功 → 發 PROD PR 簽核通知至產品頻道（通知②，@mention 簽核人）
    2. 轉「簽核中」失敗 → 發 TPM 個人頻道人工提醒（僅首次發送），保持 active 每輪重試；
       超過停損日（上線日 +1）仍失敗才終止（stopped_reason=manual）
    3. 安全停損：PROD 一直沒找齊且 today > 上線日 +1 日曆天 → 發 TPM 個人頻道告警

PR 搜集以「上線日 token（YYYYMMDD/YYMMDD）」比對 title + stg/prod + 已 merged，
不依賴 head/base 分支名。支援多產品：設定讀自 config.json，狀態存於 state.json。
"""

import base64
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
STG_PATTERN  = r"(?i)(\bstg\b|\[stg\])"
PROD_PATTERN = r"(?i)(\bprod\b|\[prod\])"


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


def search_merged_prs_by_title(repo: str, tokens: list, pr_state: str = "merged") -> list:
    """搜集 title 含任一上線日 token 的 PR（依 pr_state 篩選，跨 token 去重）。
    pr_state：STG 用 "merged"（已上 STG 才貼上線單）；PROD 用 "open"（簽核在 merge 之前）。
    使用 GitHub Search API，完全不依賴 head/base 分支名稱。
    回傳 [{"title": ..., "html_url": ...}]。"""
    found = {}
    for token in tokens:
        q = f"repo:{repo} is:pr is:{pr_state} in:title {token}"
        resp = requests.get(
            "https://api.github.com/search/issues",
            headers=GH_HEADERS,
            params={"q": q, "per_page": 100},
        )
        if resp.status_code != 200:
            print(f"  ⚠️  search {repo} (token={token}): HTTP {resp.status_code}")
            continue
        for item in resp.json().get("items", []):
            found[item["html_url"]] = item.get("title", "")
        time.sleep(1)  # GitHub Search API 節流
    return [{"title": t, "html_url": u} for u, t in found.items()]


def filter_by_title(prs: list, pattern: str) -> list:
    regex = re.compile(pattern, re.IGNORECASE)
    return [pr for pr in prs if regex.search(pr.get("title", ""))]


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


# ── Teams Notifications ────────────────────────────────────────────────────────
def _post_card(webhook_url: str, title: str, body: str, entities: list) -> bool:
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
    """轉「簽核中」成功 → 發 PROD PR 簽核通知至產品頻道（通知②），@mention 簽核人。"""
    webhook_url = product_config.get("teams_product_channel_webhook_url", "")
    if not webhook_url or "FILL_IN" in webhook_url:
        print(f"  ⚠️  Teams product webhook not configured for {product_name}")
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

    body = (
        f"{mention_str} Hi all，{display_name}本次上線所有 PROD PR 已就緒，"
        f"請抽空簽核上線單與 PR，感謝\n\n"
        f"- 上線單：{jira_url}\n- PR：\n" + "\n".join(pr_lines)
    )

    ok = _post_card(webhook_url, f"🚀 PROD PR 簽核通知｜{display_name} {deploy_date}", body, entities)
    if ok:
        print(f"  ✅ Signing notification sent for {product_name}")
    else:
        print(f"  ❌ Teams signing notification failed")
    return ok


def notify_manual_transition_needed(product_name: str, deployment: dict):
    """轉「簽核中」失敗，發純文字提醒至 TPM 個人聊天。
    payload 格式：{"message": "..."}；Power Automate 運算式：triggerBody()?['message']"""
    webhook_url = CONFIG.get("tpm", {}).get("teams_personal_webhook_url", "")
    if not webhook_url or "FILL_IN" in webhook_url:
        print(f"  ⚠️  teams_personal_webhook_url 未設定，跳過人工轉單提醒")
        return
    issue_key = deployment["deploy_issue_key"]
    jira_url  = deployment["deploy_issue_url"]
    message = (
        f"⚠️ {product_name}：需人工轉單\n"
        f"{issue_key} 無法自動轉為「簽核中」，請手動操作。\n"
        f"上線單：{jira_url}"
    )
    resp = requests.post(webhook_url, json={"message": message})
    if resp.status_code in (200, 202):
        print(f"  ✅ 人工轉單提醒已發送")
    else:
        print(f"  ❌ 人工轉單提醒失敗: HTTP {resp.status_code}")


def notify_prod_timeout(product_name: str, deployment: dict):
    """安全停損：上線日+1 仍未蒐齊 PROD → 發告警至 TPM 個人聊天（僅個人頻道）。"""
    webhook_url = CONFIG.get("tpm", {}).get("teams_personal_webhook_url", "")
    if not webhook_url or "FILL_IN" in webhook_url:
        print(f"  ⚠️  teams_personal_webhook_url 未設定，跳過安全停損告警")
        return
    issue_key = deployment["deploy_issue_key"]
    jira_url  = deployment["deploy_issue_url"]
    message = (
        f"⚠️ {product_name}：上線日 +1 仍未蒐齊 PROD PR，已停止自動搜集。\n"
        f"請人工確認 {issue_key}，並視情況手動轉「簽核中」。\n"
        f"上線單：{jira_url}"
    )
    resp = requests.post(webhook_url, json={"message": message})
    if resp.status_code in (200, 202):
        print(f"  ✅ 安全停損告警已發送")
    else:
        print(f"  ❌ 安全停損告警失敗: HTTP {resp.status_code}")


def _activate_next_pending(deployment: dict, repos: list) -> bool:
    """active 條目簽核完成後，從 pending_queue 啟用下一筆部署。"""
    queue = deployment.get("pending_queue", [])
    if not queue:
        return False
    nxt = queue.pop(0)
    deployment.update({
        "deploy_date":        nxt["deploy_date"],
        "stg_date":           nxt["stg_date"],
        "deploy_issue_key":   nxt["deploy_issue_key"],
        "deploy_issue_url":   nxt["deploy_issue_url"],
        "epic_key":           nxt["epic_key"],
        "stg_prs":            {repo: [] for repo in repos},
        "prod_prs":           {repo: None for repo in repos},
        "stg_status_updated": False,
        "signing_notified":   False,
        "status":             "active",
        "pending_queue":      queue,
    })
    deployment.pop("manual_notice_sent", None)
    deployment.pop("stopped_reason", None)
    print(f"  🔄 Activated next pending: {nxt['deploy_issue_key']} ({nxt['deploy_date']})")
    return True


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

        product_config = CONFIG.get("products", {}).get(product_name)
        if not product_config:
            print(f"⚠️  '{product_name}' not in config.json")
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

        print(f"\n{'='*55}")
        print(f"📦 {product_name} | deploy={deploy_date_str} | today={today} | {issue_key}")
        print(f"   STG 起算={stg_start} | PROD 起算={prod_start} | 停損={prod_cutoff} | tokens={tokens}")

        if today < stg_start:
            print("  ⏳ 未到 STG 窗口，跳過")
            continue

        # ── STG 窗口 ──
        print("🔍 STG: collecting...")
        any_new = False
        for repo in repos:
            display  = repo_names.get(repo, repo.split("/")[-1])
            existing = set(deployment["stg_prs"].get(repo, []))
            prs      = search_merged_prs_by_title(repo, tokens)
            stg      = filter_by_title(prs, STG_PATTERN)
            new_urls = [pr["html_url"] for pr in stg if pr["html_url"] not in existing]
            if new_urls:
                deployment["stg_prs"][repo] = list(existing) + new_urls
                any_new = True
                for u in new_urls:
                    print(f"  ✅ STG {display}: {u}")
            else:
                print(f"  ⏳ STG {display}: no new")

        if any_new:
            state_changed = True
            adf = build_web_api_adf(deployment["stg_prs"], deployment["prod_prs"], repo_names)
            update_web_api_field(issue_key, field_id, adf)

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
                prod = filter_by_title(prs, PROD_PATTERN)
                if prod:
                    deployment["prod_prs"][repo] = prod[0]["html_url"]
                    any_new = True
                    print(f"  ✅ PROD {display}: {prod[0]['html_url']}")
                else:
                    print(f"  ⏳ PROD {display}: none yet")

            if any_new:
                state_changed = True
                adf = build_web_api_adf(deployment["stg_prs"], deployment["prod_prs"], repo_names)
                update_web_api_field(issue_key, field_id, adf)

            # 收齊範圍 = 有 STG merged PR 的 repo；無 STG PR 的 repo 不在本次修正範圍，不等其 PROD PR
            scoped_repos = [r for r in repos if deployment["stg_prs"].get(r)]
            all_found = bool(scoped_repos) and all(deployment["prod_prs"].get(r) for r in scoped_repos)
            if all_found:
                scope_str = ", ".join(repo_names.get(r, r.split("/")[-1]) for r in scoped_repos)
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
                    # 保持 active 由排程每 30 分重試（如 QA 尚未轉「Staging測試完成」的時序問題可自癒）；
                    # 超過停損日（上線日 +1）仍失敗 → 再提醒一次並終止
                    if today > prod_cutoff:
                        print("  ⏰ 轉「簽核中」重試至停損日仍失敗 → 個人頻道提醒，終止")
                        notify_manual_transition_needed(product_name, deployment)
                        deployment["status"] = "completed"
                        deployment["stopped_reason"] = "manual"
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

    if state_changed:
        with open(ROOT / "state.json", "w", encoding="utf-8") as f:
            json.dump(STATE, f, indent=2, ensure_ascii=False)
        print("\n📝 state.json saved")


if __name__ == "__main__":
    main()
