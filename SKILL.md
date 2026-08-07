---
name: 104-release-workflow
description: >
  104 Corp 產品上線自動化流程（Claude 手動 Steps 1–5）。
  當使用者輸入「YYYY/MM/DD 產品名」（雲端，如「2026/7/2 農場」、「7/10 農場上線」）、
  「YYYY/MM/DD 產品名 k8s」（地端，如「2026/7/16 CMS k8s」）、
  「幫我建上線單」、「開始上線流程」、「新增 {產品} 上線」、「建立上線單」等意圖時立即觸發。
  執行（Claude 手動 Steps 1–5）：Jira 上線單建立 → Epic 建立 → 相互連結 → 日期設定 →（地端另建 K8s 上線子單）→ 更新 GitHub state.json 並開 PR。
  PR merge 至 main 後 GitHub Actions 自動接手：發上線通知① → Steps 7–8（PR 搜集、狀態轉換與簽核通知）。
  只要提到「上線」「deploy」「release」「開上線單」「建上線」就應主動觸發此 skill。
---

# 104 Deploy Automation — Steps 1–5 執行指引（Claude 手動階段；Steps 7–8 為 GitHub Actions 自動）

## 總覽：工作流程架構

```
使用者說「2026/7/2 農場」
      │
      ▼ Claude 手動執行 (Steps 1–5)
  ┌─────────────────────────────────┐
  │ Step 1  建立 Jira 上線單         │
  │ Step 2  建立 Jira Epic           │
  │ Step 3  上線單 depends on Epic   │
  │ Step 4  上線單 description 插入  │
  │         Epic JQL 連結            │
  │ Step 4.5 建 K8s 子單（限地端）   │
  │ Step 5  更新 state.json、開 PR   │
  └─────────────────────────────────┘
      │
      ▼ PR merge 至 main 後，GitHub Actions 自動接手（Claude 不參與，每 30 分排程）
  ┌─────────────────────────────────────────────────────────────
  │ Merge 觸發（立即）
  │   【通知①】Teams 上線作業頻道：Adaptive Card @mention 工程師，告知上線時程
  │
  │ Step 7  上線日 −3 工作天起，每 30 分搜集 STG PR
  │         （title 含 stg/[stg] ＋ 上線日 token ＋ 已 merged）
  │         ├─ 有新 PR → 寫入 Jira Web/API 程式資訊
  │         └─ 有 STG PR → 推進「STAGING測試中」狀態（未成功每輪重試；狀態已達或超過則視為完成）
  │
  │ Step 8  上線日 −1 工作天起，每 30 分搜集 PROD PR（prod/[prod]，抓 open）
  │         └─ 有 STG PR 的 repo 全找齊 → 嘗試轉「簽核中」（帶簽核主管）
  │              ├─ 轉單成功 →【通知②】Teams 產品頻道 @mention 簽核人，終止
  │              └─ 轉單失敗 →【通知③】個人聊天提醒（僅首次發送），保持追蹤每輪重試
  │                   → 超過上線日 +1 日曆天仍失敗 → 再發通知③一次並終止
  │         安全停損：上線日 +1 仍未找齊 → 個人聊天告警，終止
  └─────────────────────────────────────────────────────────────
```

## Teams 通知情境說明

| # | 通知名稱 | 觸發時機 | 發送對象 | 目的地 | webhook 欄位 |
|---|---------|---------|---------|------|-------------|
| 1 | 上線準備通知 | `deploy_date` 變動 push 至 main | `talent_engineering_members` | 上線作業頻道 | `teams_deploy_channel_webhook_url` |
| 2 | PROD PR 簽核通知 | 有 STG PR 的 repo 之 PROD PR 全找齊、轉「簽核中」成功 | `approvers` | 產品頻道 | `teams_product_channel_webhook_url` |
| 3 | 人工轉單提醒 | PROD PR 找齊但自動轉「簽核中」失敗（僅首次失敗發送；重試至上線日 +1 仍失敗再發一次並終止） | TPM 個人聊天 | 個人聊天（Power Automate） | `tpm.teams_personal_webhook_url` |
| 4 | 安全停損告警 | 上線日 +1 仍未蒐齊 PROD PR | TPM 個人聊天 | 個人聊天（Power Automate） | `tpm.teams_personal_webhook_url` |

> STG PR 找到時**不發 Teams 通知**，只更新 Jira Web/API 程式資訊欄位並推進到「STAGING測試中」狀態（依目的地狀態轉單；當次未成功則每 30 分排程持續重試直到成功）。

> **人工轉單提醒 payload 格式**（`tpm.teams_personal_webhook_url`，Power Automate HTTP trigger）：
> ```json
> {"message": "⚠️ {產品名}：需人工轉單\n{issue_key} 無法自動轉為「簽核中」，請手動操作。\n上線單：{jira_url}"}
> ```
> Power Automate「在聊天室或管道中張貼訊息（Post a message in a chat or channel）」動作中，「訊息」欄位點 **fx** 輸入運算式：`triggerBody()?['message']`

---

## 多產品設計說明

系統以 **產品名稱為 key** 管理所有設定與狀態，支援多產品同時運行：

- `config.json → products.{產品名}` 存放每個產品的獨立設定
- `state.json → active_deployments.{產品名}` 存放每次部署的即時狀態
- 新增產品只需在 config.json 加入新 entry，程式碼無需修改

---

## 事前準備

### 讀取 config.json（GitHub repo 由 config 決定，不寫死）

> 首次導入新環境的建置步驟（從 template 建 repo、push、設 Secrets/webhook、填 config）見操作手冊 6.1。

本 skill 操作的 GitHub repo 記錄在 config.json 的 `github`（`owner` / `repo`）。**首次讀取 config.json 時**，owner/repo 由使用者告知（或沿用先前對話已知的 repo）；讀到 config 後，**之後所有 GitHub 操作（讀 state.json、push、開 PR）一律使用 `CONFIG.github.owner` / `CONFIG.github.repo`**，不再寫死。

```
# {owner}/{repo} 首次由使用者提供，並應與 config.json 的 github 欄位一致
mcp__github__get_file_contents(owner="{owner}", repo="{repo}", path="config.json")
```

從中取得：
- `CONFIG.github.owner` / `CONFIG.github.repo` → 之後所有 GitHub 操作都用這組
- `CONFIG.jira.cloud_id` → Jira Cloud ID
- `CONFIG.jira.base_url` → Jira 瀏覽器 URL 前綴
- `CONFIG.products.{產品名}` → 該產品完整設定（見下方 `product_config`）
- `product_config.approvers` → 該產品簽核人清單（含 AAD Object ID；外層 `CONFIG.approvers` 為 fallback）

### 指令型態與 hosting 檢查

產品依託管環境分兩類，對應兩種指令；以指令**結尾是否帶 `k8s`**（不分大小寫）判斷：

| 指令型態 | 範例 | 對應 `product_config.hosting` | 動作 |
|---------|------|------|------|
| 雲端（AWS） | `2026/7/16 農場` | `aws` | Steps 1–5，**不建** K8s 子單 |
| 地端（K8s） | `2026/7/16 CMS k8s` | `k8s` | Steps 1–4 →**Step 4.5 建主站 K8s 子單**→ Step 5 |
| 地端＋DR（K8s） | `2026/7/16 Program k8s dr` | `k8s` | 同地端，Step 4.5 建**主站＋DR 站**子單（每 repo 各一張，3+3） |

判斷步驟：

1. 解析指令結尾 token（不分大小寫）：帶 `k8s dr` → `mode = "k8s"`、`sites = ("primary", "dr")`；僅帶 `k8s` → `mode = "k8s"`、`sites = ("primary",)`；皆無 → `mode = "aws"`。
2. 讀 `product_config.hosting`。
3. **hosting 檢查**（不符即停，回報使用者，不建任何單）：
   - `mode = "k8s"` 但 `hosting = "aws"`：此產品為雲端託管，無需 K8s 子單，請改用「`{日期} {產品名}`」。（帶 `dr` 但 hosting=aws 同樣適用此條，一律擋下）
   - `mode = "aws"` 但 `hosting = "k8s"`：此產品為地端託管，需一併建 K8s 子單，請改用「`{日期} {產品名} k8s`」。
   - 相符 → 繼續執行。

### 解析使用者輸入

| 變數 | 說明 | 範例 |
|------|------|------|
| `product_name` | 產品名稱（中文） | `農場` |
| `deploy_date` | 上線日 | `2026-07-16` |
| `deploy_date_yyyymmdd` | 上線日（純數字） | `20260716` |
| `q_date` | 排定送Q日（**上線日 −1 工作天**） | `2026-07-15` |
| `stg_date` | 上STG日（**上線日 −2 工作天**） | `2026-07-14` |
| `stg_date_yyyymmdd` | 上STG日（純數字） | `20260714` |
| `deploy_M_D` | 顯示用日期 | `7/16` |
| `stg_M_D` | 顯示用日期 | `7/14` |

### 計算 q_date 與 stg_date（兩個不同日期，禁止共用同一變數）

```python
from datetime import date, timedelta

def subtract_working_days(d: date, n: int) -> date:
    """往前推 n 個工作天（跳過週六、週日）"""
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # 0=Mon … 4=Fri
            n -= 1
    return d

deploy_date = date(2026, 7, 16)                     # 從使用者輸入解析
q_date   = subtract_working_days(deploy_date, 1)    # 排定送Q日＝上線日 −1 工作天
stg_date = subtract_working_days(deploy_date, 2)    # 上STG日＝上線日 −2 工作天
# 2026-07-16 → 送Q 2026-07-15、上STG 2026-07-14
```

範例對照：

| 上線日（PROD） | 排定送Q日（−1 工作天） | 上STG日（−2 工作天） |
|---------------|----------------------|---------------------|
| 週四 7/16 | 週三 7/15 | 週二 7/14 |
| 週四 7/23 | 週三 7/22 | 週二 7/21 |
| 週一 7/6  | 週五 7/3  | 週四 7/2  |

---

## Step 1：建立 Jira 上線單

工具：`mcp__Atlassian_Rovo__createJiraIssue`

> **Atlassian MCP 參數版本注意（2026-07 實測）**：`additional_fields`（snake_case）、`assignee_account_id`、`parent` 為頂層參數；issue type 一律用 `issueTypeName`（`issueTypeId` 參數已移除）；`createIssueLink` 用 `type`（連結類型名稱）＋`inwardIssue`/`outwardIssue`；`editJiraIssue` 用 `issueIdOrKey`，傳 ADF 需帶 `contentFormat: "adf"`。舊參數名（如 `additionalFields`）會被靜默丟棄導致 Jira 400。以下範例皆為新版。

```json
{
  "cloudId": "{CONFIG.jira.cloud_id}",
  "projectKey": "{product_config.jira_deploy_project}",
  "issueTypeName": "一般需求上線(新)",
  "summary": "{summary}",
  "assignee_account_id": "{product_config.jira_assignee_account_id}",
  "additional_fields": {
    "customfield_11221": "{q_date}T12:00:00.000+0800",
    "customfield_11222": "{deploy_date}T{deploy_time}:00.000+0800",
    "customfield_13807": {
      "id": "{product_config.jira_domain_parent_id}",
      "child": {"id": "{product_config.jira_domain_child_id}"}
    },
    "customfield_13907": {"accountId": "{product_config.jira_assignee_account_id}"},
    "customfield_12767": {"id": "{pii_option_id}"},
    "customfield_13602": {"id": "{product_config.jira_system_level}"}
  }
}
```

> - customfield_11221 = 排定送Q日期（時間**固定 12:00**）；customfield_11222 = 排定上線日期（時間取 `product_config.deploy_time`，如 CMS＝10:00；未設定預設 12:00）
> - customfield_13807 = 事業群 - Domain（必填，農場：parent `29668` → child `45759`）
> - customfield_13907 = Engineer（同 assignee，值取 config `jira_assignee_account_id`）
> - assignee 值取 config `product_config.jira_assignee_account_id`
> - **customfield_12767 = 異動與個資讀寫相關（單選）**：建單前**先問 TPM**「本次上線是否涉及個資讀寫？」→ **否 = id `12293`；是 = id `12292`**（選「是」另需填個資申請表 P202-01；新產品加 P202-02）。`pii_option_id` 代入該 id。
>   ⚠️ 此欄是 STG 轉單「已更新Staging→STAGING測試中」的**必填驗證**：建單時沒填，STG 之後就**無法自動轉**「STAGING測試中」（排程會一直重試但過不了）。
> - **customfield_13602 = 系統等級（單選）**：值取 `product_config.jira_system_level`（存選項 id；`13204` =「1-2」，目前所有產品皆 1-2）。以 `{"id": ...}` 帶入。
> - **summary（上線單標題）**：若 `product_config.jira_deploy_summary_template` 有值，用它套版（tokens：`{deploy_date}`＝上線日 YYYY/MM/DD、`{deploy_M_D}`＝M/D、`{display_name}`；如 GO＝`GO 外籍專區 {deploy_date} 上線`）；未設則用預設 `{display_name} - {deploy_M_D} 維運上線`。

儲存結果：`deploy_issue_key`（如 `{YOUR_PROJECT}-XXXX`）

---

## Step 2：建立 Jira Epic

工具：`mcp__Atlassian_Rovo__createJiraIssue`

```json
{
  "cloudId": "{CONFIG.jira.cloud_id}",
  "projectKey": "{product_config.jira_product_project}",
  "issueTypeName": "大型工作",
  "summary": "{deploy_date_yyyymmdd} 上線",
  "additional_fields": {
    "customfield_10014": "{deploy_date_yyyymmdd} 上線"
  }
}
```

> issue type 名稱「大型工作」對應 config `jira_epic_type_id` = `10000`（MCP 現行版只吃名稱）。

儲存結果：`epic_key`（如 `{YOUR_EPIC_PROJECT}-XXXX`）

---

## Step 3：連結上線單與 Epic（depends on）

工具：`mcp__Atlassian_Rovo__createIssueLink`

```json
{
  "cloudId": "{CONFIG.jira.cloud_id}",
  "type": "Depends(WBSGantt)",
  "inwardIssue": "{epic_key}",
  "outwardIssue": "{deploy_issue_key}"
}
```

效果：上線單顯示「**is depended on by** {epic_key}」（Depends(WBSGantt) 關係）。link type id `10302` 僅供參考，MCP 現行版以名稱（`type`）呼叫。

---

## Step 4：上線單 description 插入 Epic JQL 連結

工具：`mcp__Atlassian_Rovo__editJiraIssue`

```json
{
  "cloudId": "{CONFIG.jira.cloud_id}",
  "issueIdOrKey": "{deploy_issue_key}",
  "contentFormat": "adf",
  "fields": {
    "description": {
      "version": 1,
      "type": "doc",
      "content": [
        {
          "type": "paragraph",
          "attrs": { "localId": "adf-para-01" }
        },
        {
          "type": "blockCard",
          "attrs": {
            "url": "https://{your-site}.atlassian.net/issues/?jql=%22epic%20link%22%3D%20{epic_key}%20ORDER%20BY%20created%20DESC",
            "datasource": {
              "id": "{FILL_IN_datasource_id}",
              "parameters": {
                "cloudId": "{CONFIG.jira.cloud_id}",
                "jql": "\"epic link\"= {epic_key} ORDER BY created DESC"
              },
              "views": [
                {
                  "type": "table",
                  "properties": {
                    "columns": [
                      { "key": "issuetype" },
                      { "key": "key", "width": 162 },
                      { "key": "summary" },
                      { "key": "assignee" },
                      { "key": "status" },
                      { "key": "customfield_16933" },
                      { "key": "customfield_16934" },
                      { "key": "customfield_16935" }
                    ]
                  }
                }
              ]
            }
          }
        }
      ]
    }
  }
}
```

> - 格式：`blockCard` + `attrs.datasource`（非獨立 datasource 節點）
> - datasource ID：`{FILL_IN_datasource_id}`（固定值，從你的 Jira 上線單描述欄既有 blockCard 中取得）
> - 欄位：issuetype、key（width 162）、summary、assignee、status、customfield_16933（Lab驗收）、customfield_16934（Stg驗收）、customfield_16935（Prod驗收）
> - `{epic_key}` 不需 encode（如 `{YOUR_EPIC_PROJECT}-XXXX`）；URL 中用 %22epic%20link%22 已固定

---

## Step 4.5：建置 K8s 上線子單（僅 hosting=k8s 產品）

> **僅在 `mode = "k8s"` 且 `product_config.hosting = "k8s"` 時執行**；雲端（hosting=aws）產品跳過此步。
> 子單為上線單（`deploy_issue_key`）的子工作，用來觸發各 repo 的 K8s 部署 pipeline。**子單不寫入 state.json**。

### 共用服務設計（可擴充多站點；DR 站未來只加設定、不改邏輯）

以「站點（site）」為單位產生子單規格。站點有主站 `primary` 與 DR 站 `dr`：指令未帶 `dr` → `sites=("primary",)` 只建主站；指令帶 `dr` → `sites=("primary", "dr")` 主站＋DR 站一起建。

```python
# 站點設定：ArgoCD Application 命名樣板（{name} 由 config.json 的 k8s_subtickets 值代入）
K8S_SITES = {
    "primary": {
        "argocd_stg":  "stg-stg-{name}",    # ArgoCD Application - Staging
        "argocd_prod": "prod-prod-{name}",  # ArgoCD Application - Production
        "summary":     "{name}",            # 子單名稱＝純 name（無任何前綴）
        "extra_fields": {},                 # 主站無額外欄位（組態設定不填）
    },
    "dr": {
        "argocd_stg":  "stg-dr-{name}",     # DR 站 ArgoCD Application - Staging
        "argocd_prod": "prod-dr-{name}",    # DR 站 ArgoCD Application - Production
        "summary":     "{name} dr site",    # DR 子單名稱＝name＋空格＋dr site
        "extra_fields": {"customfield_14301": "Y"},  # 組態設定＝Y（僅 DR 子單填）
    },
}

# 固定值（同一 K8s app repo 與部署分支）
K8S_GITHUB_REPO   = "https://github.com/{your-org}/{k8s-repo}"
K8S_DEPLOY_BRANCH = "main"   # Staging / Production 皆為 main

def build_k8s_specs(product_config, sites=("primary",)):
    """由 product_config.k8s_subtickets（repo → name）× 站點設定，展開所有子單規格。"""
    specs = []
    for repo, name in product_config["k8s_subtickets"].items():
        for site in sites:
            s = K8S_SITES[site]
            argocd_stg  = s["argocd_stg"].format(name=name)
            argocd_prod = s["argocd_prod"].format(name=name)
            for v in (argocd_stg, argocd_prod):        # 驗證：ArgoCD 名稱不得含空白
                assert " " not in v, f"ArgoCD 名稱不得含空白：{v!r}"
            specs.append({
                "repo": repo, "name": name, "site": site,
                "summary": s["summary"].format(name=name),
                "argocd_stg": argocd_stg, "argocd_prod": argocd_prod,
                "extra_fields": s["extra_fields"],
            })
    return specs
```

> 範例：`k8s_subtickets` = `{repo-a, repo-b, repo-c}`，主站 → 建 **3** 張子單。
> 帶 `dr` 時：`build_k8s_specs(product_config, sites=("primary","dr"))` → 3+3 = **6** 張；DR 子單名稱如 `{repo-name} dr site`。

### 逐一建立子單

對 `build_k8s_specs(product_config)` 產出的每個 `spec`，呼叫：

工具：`mcp__Atlassian_Rovo__createJiraIssue`

```json
{
  "cloudId": "{CONFIG.jira.cloud_id}",
  "projectKey": "{product_config.jira_deploy_project}",
  "issueTypeName": "(Sub) K8s上線",
  "summary": "{spec.summary}",
  "parent": "{deploy_issue_key}",
  "assignee_account_id": "{CONFIG.tpm.jira_account_id}",
  "additional_fields": {
    "customfield_13237": "https://github.com/{your-org}/{k8s-repo}",
    "customfield_13238": "{spec.argocd_stg}",
    "customfield_13239": "{spec.argocd_prod}",
    "customfield_13241": "main",
    "customfield_13243": "main"
  }
}
```

> 欄位對照（issue type `11917`「(Sub) K8s上線」，subtask=true，共用 ITPMTALREQ；MCP 現行版以名稱 `issueTypeName` 呼叫，`issueTypeId` 參數已移除。⚠️ 頂層 `parent` 與此 issue type 名稱為新版參數、尚未實測，首次跑地端時請驗證）：
> - `parent` = 上線單 `deploy_issue_key`（必填；頂層參數，直接給 issue key 字串）
> - `customfield_13237` = GitHub repository（url，固定 `{your-org/k8s-repo}`）
> - `customfield_13238` = ArgoCD Application - Staging（`stg-stg-{name}`，不得含空白）
> - `customfield_13239` = ArgoCD Application - Production（`prod-prod-{name}`，不得含空白）
> - `customfield_13241` / `customfield_13243` = Deploy Branch - Staging / Production（皆 `main`）
> - **DR 子單另帶** `customfield_14301` = `"Y"`（組態設定；即 `spec.extra_fields`，併入 additional_fields）；**主站子單不填此欄**
> - `assignee` = TPM（`CONFIG.tpm.jira_account_id`）
> - 子單初始狀態即為「Staging 部署 Preview」，後續子單狀態流程由 TPM 人工處理，本 skill 不轉子單狀態

儲存結果：`k8s_subticket_keys`（僅供回報，**不**寫入 state.json）

---

## 自動階段：Teams 上線作業頻道通知（通知①，由 GitHub Actions 發送，Claude 不做）

**此通知由 GitHub Actions 於下方 Step 5 的 PR merge 至 main 後自動發送，Claude 不送出。**

### 觸發機制

Step 5 更新 state.json 並開 PR；PR **merge 至 main** 後（`deploy_date` 有變動），觸發 `.github/workflows/notify-deploy.yml`（push 事件，paths: state.json）。

`scripts/notify_deploy.py` 比對前後兩個 commit 的 state.json，偵測 `deploy_date` 有變動的產品，對每個變動產品呼叫 `product_config.teams_deploy_channel_webhook_url` 送出 Adaptive Card 通知。

### 前提確認（執行 Step 5 前檢查）

- `config.json` 中 `product_config.teams_deploy_channel_webhook_url` **不得為** `FILL_IN_...`，否則 GitHub Actions 會跳過該產品
- `talent_engineering_members` 中 `aad_id` 為 `FILL_IN` 的成員不會被 @mention，提醒使用者補充
- `CONFIG.tpm.teams_personal_webhook_url` 需透過 Power Automate 建立個人聊天中轉流程取得（見操作手冊 6.4.3）
- AAD Object ID 查詢方式：登入 https://developer.microsoft.com/en-us/graph/graph-explorer，查自己用 `GET https://graph.microsoft.com/v1.0/me?$select=id,displayName,mail`，查同事用 email 替換路徑（最穩定），查不到時請同事自查後回傳

> **限制**：Teams 標籤（@Talent 工程）無法透過 webhook @mention，改用個別成員 AAD ID。

> **不需重發通知①時（如切換追蹤日期、同日重建部署，且先前已通知過）**：merge PR 時在 merge commit 訊息（標題或 Extended description 皆可）加入 `[skip ci]`，push 觸發的 notify-deploy workflow 即不會執行——這是 GitHub Actions 內建規則（`[ci skip]`、`[skip actions]` 亦可）。Steps 7–8 的 PR 搜集為 cron 排程觸發，不受影響；且下次其他 push 只比對該次 push 前後的 state.json，不會補發本次變動的通知。

---

## Step 5（本 skill 最後一步）：更新 GitHub state.json

### 讀取現有 state.json
```
mcp__github__get_file_contents(owner="{CONFIG.github.owner}", repo="{CONFIG.github.repo}", path="state.json")
```

### 加入新部署
在 `active_deployments` 加入（`stg_prs`／`prod_prs` 的 key 依該產品 `product_config.repos[]` 產生，以下以農場為例；`stg_date`＝上STG日（上線日 −2 工作天），供通知①顯示，**非**排定送Q日）：

```json
"{product_name}": {
  "deploy_date": "{deploy_date_yyyymmdd}",
  "stg_date": "{stg_date_yyyymmdd}",
  "deploy_issue_key": "{deploy_issue_key}",
  "deploy_issue_url": "https://{your-site}.atlassian.net/browse/{deploy_issue_key}",
  "epic_key": "{epic_key}",
  "stg_prs": {
    "{your-org}/{repo-1}": [],
    "{your-org}/{repo-2}": [],
    "{your-org}/{repo-3}": [],
    "{your-org}/{repo-4}": [],
    "{your-org}/{repo-5}": []
  },
  "prod_prs": {
    "{your-org}/{repo-1}": null,
    "{your-org}/{repo-2}": null,
    "{your-org}/{repo-3}": null,
    "{your-org}/{repo-4}": null,
    "{your-org}/{repo-5}": null
  },
  "stg_status_updated": false,
  "signing_notified": false,
  "status": "active"
}
```

> **同產品已有 active 條目時（第二張以上上線單）**：不覆蓋現有條目；改在該條目的 `pending_queue` 陣列新增一筆，僅含 5 欄位（`deploy_date`、`stg_date`、`deploy_issue_key`、`deploy_issue_url`、`epic_key`）。待前張單轉「簽核中」成功，`collect_prs.py` 自動啟用下一筆。

### 推送更新（推分支，不直接 push main）

分支命名規則：`chore/activate-{product_name}-{deploy_date_yyyymmdd}`

```
mcp__github__push_files(
  owner="{CONFIG.github.owner}",
  repo="{CONFIG.github.repo}",
  branch="chore/activate-{product_name}-{deploy_date_yyyymmdd}",
  message="chore: activate {product_name} deployment {deploy_date_yyyymmdd}",
  files=[{"path": "state.json", "content": "..."}]
)
```

### 建立 Pull Request

```
mcp__github__create_pull_request(
  owner="{CONFIG.github.owner}",
  repo="{CONFIG.github.repo}",
  title="chore: activate {product_name} deployment {deploy_date_yyyymmdd}",
  body="state.json 更新：啟動 {product_name} {deploy_date_yyyymmdd} 上線部署追蹤。\n\n請確認內容無誤後 Merge。",
  head="chore/activate-{product_name}-{deploy_date_yyyymmdd}",
  base="main"
)
```

PR 建立後，告知使用者連結，等待簽核後 merge。

> **Git 協作規範：**
> - Claude 所有程式異動（含 config.json、state.json）一律推分支並開 PR，不直接 push main
> - 使用者在 GitHub PR 頁面點「Files changed」確認 diff → 「Review changes」→「Approve」→「Merge pull request」
> - Merge 後使用者本地同步：`cd ~/Documents/104-release-automation && git checkout main && git pull origin main`

---

## 完成回報格式

所有步驟完成後，以下列格式回報：

```
✅ Step 1  上線單建立：{deploy_issue_key}
           https://{your-site}.atlassian.net/browse/{deploy_issue_key}

✅ Step 2  Epic 建立：{epic_key}
           https://{your-site}.atlassian.net/browse/{epic_key}

✅ Step 3  上線單 depends on Epic 已連結

✅ Step 4  Epic 工作項目連結已插入上線單描述欄

✅ Step 4.5 K8s 上線子單（限 hosting=k8s）：{k8s_subticket_keys}
           （指令帶 dr：主站＋DR 站共 3+3 張；hosting=aws 產品略過此步）

✅ Step 5  state.json 已更新並開 PR：{pr_url}
           （排定送Q日：{q_date} 12:00／排定上線日：{deploy_date} {deploy_time} 已於 Step 1 寫入上線單）

──（以下自動階段，Claude 不執行）──
ℹ️  PR merge 至 main 後，GitHub Actions 自動發送通知①至上線作業頻道，並自上線日 −3 工作天起搜集 STG PR

⚠️  注意事項（若有）：
- 若有成員 aad_id 未填，列出姓名提醒補充
- 若 Teams webhook 未設定，提示使用者填入 config.json
```

---

## §0 強制規範（最高優先，無例外，違反即任務失敗，須立即停止並回報）

> 本節為硬性規定，優先於任何便利性與任何「我記得／應該沒問題」的直覺。每次修正程式、或提供任何程式面方案時，都必須逐條遵循。規則**不含**「緊急／明顯正確可跳過」這類但書；**不得自訂例外**。

### 0.1 以查證取代記憶（第一優先）

1. **禁止憑記憶或憑上一輪對話的假設，斷定 `main`、任何分支、任何 PR、任何檔案、任何 Jira 單、任何設定的現況。** 任何關於「程式現在長怎樣／PR 開著還是合併了／某改動有沒有進 main／某單現在什麼狀態／某設定有沒有生效」的陳述，**一律只能來自當下的工具查證結果**。
2. 動任何程式面方案（改 config、改程式、開/合 PR、回答「現在 main 是什麼版本」）之前，**必先**：
   - `mcp__github__get_pull_request` 查相關 PR 的 `state`（open/closed）、`merged`、`merged_at`、`merge_commit_sha`、`head.sha`、`base.sha`；
   - `mcp__github__get_file_contents(..., ref="main")` 或 `list_commits(sha="main")` 取 `main` 現況；
   - 以查得結果為唯一依據再行動。
3. **具體禁止行為**：憑記憶假設某改動是否已生效、某 PR 是否仍開著、某單是否仍在某狀態，而未實際查證就下結論——**此類行為一律禁止**。正確作法：先查，再說話。
4. 未經當下查證的狀態描述，**不得**以「應該／我記得／剛剛推了所以…」帶過；不確定時，先查再答；**不得謊稱做過檢查**。

### 0.2 檔案異動授權（防擅自異動）

5. **固定保護清單（AI 不得自行增減）**：`SKILL.md`、任何 `*.skill`、操作手冊、SA 規格書，以及本專案程式路徑 `scripts/**`、`template-repo/**`。
6. **觸發條件是「工具呼叫本身」，非 AI 主觀判斷**：只要下一步會對清單內路徑呼叫 Write / Edit / push_files / create_or_update_file，必須先停下。
7. 停下後只輸出**純文字提案**，且**必須逐檔逐處列出**：檔案完整路徑、要改的節／條號、**改動後的確切文字**、為什麼。**未列出者即未授權**；不得以「隱含在已核准範圍內」「同一目的順手一起做」擴張授權。不得先斬後奏。
8. 只有使用者針對「**這個具體改動**」講出明確同意（可以／同意／加吧／就這樣改／請執行）才算核准；**不得**用「你稱讚我／你沒反對／你交代了更大任務」推論成同意。
9. **逐次授權**：每次要動清單內檔案都重新徵得同意，不把上次的同意延伸到這次。
   > 範圍界定（非但書）：`config.json`、`state.json` 為本 skill 正常受理的設定異動，由使用者「當次指令」觸發即屬該次授權；仍一律走分支＋PR＋驗 diff。

### 0.3 版本與備份安全（防改錯 / 覆蓋新修改）

10. 一律推分支並開 PR，**不直接 push `main`**。
11. **抓當前 HEAD 當基底**：先 `get_file_contents`（`ref` = 目標分支）取當前內容為唯一底稿；不得以記憶或別的分支當基底。
12. **禁止在工具參數裡手動逐字重打整支檔案**；一律在 sandbox 對該基底做最小片段替換（如 Python `str.replace()`），再拿該本機檔案內容去 push。
13. **push 後不得只看 patch 文字**：用 `get_pull_request_files` 檢視 diff（只能出現本次預期的行），並比對 blob sha（不該變的檔 sha 不變、該變的檔 sha 等於本機目標值）。出現任何非預期 hunk 或整檔被改寫 → 判定失敗，立即回退、修正、告知使用者。
14. **「本次其實沒有異動」時，禁止製造假動作**以讓事情看起來完成：不得用 push 製造空 commit（改用 PR comment 記錄）、不得重寫同樣內容、不得產生無意義新檔。
15. **禁止改寫歷史**（`git commit --amend`、`rebase`、force-push、編輯過去 commit）；一律只用「新增 commit + 開 PR」。
16. `push_files` 前自我審查：`*.skill`、`SKILL.md`、操作手冊、任何不在本次任務範圍內的檔案，**絕不**放進 `files[]`。

### 0.4 工具使用

17. Jira / GitHub / Teams 操作一律用指定 MCP 工具，**不得**開瀏覽器（Claude in Chrome）繞道操作網頁 UI；MCP 失敗就停止並回報，不改用瀏覽器繞道。

### 0.5 態度與誠實

18. 犯錯就明白承認，不得用「這只是補充說明」等說法淡化或自我合理化。
19. 使用者要求退回 / 撤銷時，**直接退回並回報結果**，不得反問、不得重翻已核准的舊改動來混淆。
20. 未實際執行驗證的程式，不得以「已驗證／應該沒問題」帶過；不得謊稱做過檢查。
21. 不得聲稱「我不會判斷」來規避使用者已明確下達的指令。

### 0.6 交付回報

22. 交付完成後，**同一則回覆**必附：**實際寫入的完整路徑清單**與本次變更的檔案；若有開 PR，另附 PR 連結（`html_url`）＋ Files changed 預計包含的路徑清單。

### 0.7 主動提醒義務（凡「該提醒卻可能漏」一律先講；這是 Claude 的責任，不是使用者要來問）

23. **建置／設定基礎設施時（建 repo、設 CI、設 Secrets/Variables、設分支保護等），凡遇下列任一，必須在當下主動、明確告知使用者，不得沉默、不得等到出問題才講**：(a) 有業界慣例／最佳實務的設定；(b) 會影響後續維運的預設值；(c) Claude 工具能力無法涵蓋、需使用者手動處理的項目。
24. **不確定就提出，不假設沒問題**：若不確定某設定是否已套用、預設值為何，一律主動請使用者確認（對齊 §0.1 以查證取代記憶），不得以「應該有／通常這樣／別人都會」略過。
25. **「工具做不到」不減免提醒義務，反而加重**：凡因工具限制 Claude 無法自行完成的必要設定，必須當場明講「我做不到、需要你做哪一步、怎麼操作」，並列為**必辦、非選配**；不得因為自己做不到就跳過不提。
26. **具體必辦——建立 GitHub repo 後立即處理「合併後自動刪來源分支」**：建 repo 後，`Automatically delete head branches` 能用工具設就設；工具做不到就**在建 repo 的同一則回覆**請使用者到 `Settings → General` 開啟，列為建置必辦。之後每次 PR 合併，若未開 auto-delete，主動提醒刪除已合併的來源分支，避免累積廢分支。

### 0.8 規格範圍紀律（禁止擅自加需求；冗餘程式為大忌）

27. 只實作「使用者已確認的規格」與「使 happy path 正確運作所必需」的邏輯。
28. **允許**：主動指出／補齊「會影響邏輯正確性、但使用者可能沒想到」的缺口——但必須**先以純文字提案、取得同意後才寫**，不得直接寫進 code。
29. **禁止**：加入任何規格外的功能、參數、設定旋鈕、「未來彈性」hook、預留欄位；**即使目前休眠、預設不影響行為也不行**（冗餘程式＝維護負擔＝大忌）。
30. 任何超出已確認規格的東西，一律先提案（改什麼、為何必要），經明確同意才實作；不得先斬後奏、不得以「順手加個彈性」自我合理化。
31. **交付前自檢**：本次新增的每個欄位／參數／函式／設定，都要能對應到明確需求；對應不到的一律不加或移除。

### 0.9 完整足跡紀律（功能異動一次到底，不留殘缺、不回頭問）

32. 一項功能的**新增／移除／改名，其「足跡」＝所有觸及它的地方**：程式（所有檔）＋設定＋**全部文件**（SA、SKILL.md、操作手冊、references、範例、註解）。必須在**同一次任務內全部處理完**，不得只做程式、或只做一部分。
33. **收尾前做「全域掃描」**：用 grep 對「整個 repo ＋所有文件/skill 檔」搜尋該功能的識別字（欄位名／函式名／參數名／關鍵詞），確認——移除→**零殘留**；新增/修改→**所有相關處皆已一致更新**。掃描通過才算完成、才回報。
34. **不得把自己該收尾的事變成問題丟給使用者**：殘留清理、文件同步等，只要落在原指令的功能範圍內，一律**直接補完**，不得事後問「要不要一起清/改」——回頭問＝把自己的不完整轉嫁成打擾。
35. **不分次留下不一致**：同一邏輯異動不得拆成「程式先、文件之後」而留中間不一致；若確有理由須分次，必須明確標記「待補清單」並主動追完，不得靜默遺留。

### 0.10 查證優先於說明（禁止臆測、禁止改口搪塞）

36. **先查證，再說明**：對任何「為什麼會這樣／這是什麼／狀態如何／原因為何」的事實性問題，未在**本回合**用工具實際查過（`getJiraIssue`／changelog／`get_file_contents`／`searchJiraIssuesUsingJql` 等）之前，**不得輸出任何因果解釋或結論**。禁止用「應該是／可能是／因為 skill 這樣設計／通常是」這類未驗證措辭充當答案。
37. **一次查到底**：診斷類問題必須「先蒐證、後回答」，不得先丟一個未證實的理由、等使用者否定後才補查。合格標準是——**使用者只需問一次**。
38. **標示證據與邊界**：每個結論都要附可查來源（單號、欄位 ID、changelog 時間戳、檔案路徑）。凡是現有工具無法證明的，直接講「這我查不到，答案在 X」，**不得用推論冒充已驗證結論**。
39. **禁止連續改口**：不得先後拋出多個未驗證假設（先說 A、被否定改說 B、再改 C）。若初步判斷可能多因，**先把證據查齊再擇一陳述**，不得邊猜邊試。
40. **承認優先於填空**：查證需要時間時，直接說「我還沒查，先查再答」，不得先編一個理由把空白填上。違反 §0.10 任一條，視同任務失敗，須立即停止、補查、並向使用者說明。

### 0.11 工具繞道禁令（補強 §0.4）

41. **呼叫 Claude in Chrome 任一工具前，必須先取得使用者對「這一次」的明確同意。** 不分用途——包含「只是讀公開文件」「只是查 API 規格」「沒有要操作任何系統」。**觸發條件是工具呼叫本身**，不是 AI 判斷用途是否敏感。
42. **禁止以「這不算操作 X」「這只是查資料」自行認定不在禁令範圍。** 範圍認定權在使用者，不在 AI。有疑義一律先問，不得先做後說。
43. 若某資訊只能靠禁用途徑取得，正確作法是**停下來說明「這我查不到、需要 X 才能確認」**，並把該項標為待驗證；不得自行開通替代途徑。

### 0.12 能力性斷言須經實測（補強 §0.10）

44. **凡輸出「某工具做不到／不支援／查不到／沒有這個功能」這類能力性陳述，必須在本回合實際呼叫過該工具，或至少載入其 schema 確認過參數，才能說。**
45. **依工具名稱清單、程式碼註解、過往記憶、他人文件推斷「做不到」，一律視為未查證，禁止輸出。**
46. 若呼叫後因輸出過大、逾時等原因未能完成確認，須據實說「我呼叫了，但受限於 X 尚未確認」，**不得簡化成「做不到」**。

### 0.13 阻礙處理順序（禁止擅自繞道）

47. 遇到權限不足、寫入被拒（如 `Operation not permitted`）、工具回傳失敗時，處理順序固定且不得跳過：(1) **先檢查是否存在「請求授權」的正規途徑並使用之**（例如檔案刪除授權工具、資料夾掛載請求工具）；(2) 仍不行 → **停止並回報**，附上具體錯誤訊息、卡在哪一步、需要使用者做什麼；(3) **不得自行改用替代路徑**。
48. **明確禁止的繞道行為**：改檔名、改輸出位置、改用其他工具鏈、降低交付規格，以「讓事情看起來完成」。
49. **交付物的路徑與檔名，一律沿用使用者指定或既有的值。** AI 不得自行變更；確有必要變更時先問，取得同意才改。交付後須回報**實際寫入的完整路徑**。
50. 禁止把「我做不到」當成結論輸出，而未先窮盡第 47 條第 (1) 步。

### 0.14 指令理解確認（歧義先問，不自選讀法）

51. 使用者指令若存在**兩種以上合理解讀，且不同解讀會導致不同的工具呼叫序列**，必須先以**一句話**確認理解，不得自選一種就開始執行。
52. 特別針對「用 X 確認」這類指令：**先確認「要確認的對象是什麼」**。不得把「用 X 來驗證方案」誤作「驗證 X 本身可不可用」。
53. 確認只能是一句話。**不得展開成選項清單、方案比較、或說明自己的能力邊界。**

### 0.15 Token 紀律

54. **預期單次工具輸出可能超量時（全專案查詢、`*all` 欄位、無篩選條件的 JQL、整目錄讀取），必須先縮小範圍**（加條件、限欄位、限筆數）或改用 subagent。**不得先打了再說**，再回頭解析被截斷的暫存檔。
55. **回報只寫結論與待決事項。** 過程敘述、工具能力說明、自我檢討一律不寫，除非使用者明確要求。
56. **犯錯時：一句話認錯 + 直接修正。** 不得展開反省、不得重述規則、不得解釋動機、不得請求原諒。

### 0.17 禁止製造決策題（不存在的情境不得拿來問）

57. **「請你確認／裁決」的項目必須同時滿足三條，否則不得寫進回報**：(a) 該情境在**現況下真的可能發生**；(b) 不同選擇會導致**不同的實作**；(c) 無法從既有規格、SA、或本回合已查得的現況推導出答案。任一不滿足 → 自己決定或直接移除，**不得丟給使用者**。
58. **問之前先驗證該情境是否存在。** 例：「這張單沒有受託人怎麼辦」——先查該單的 `assignee` 是否為空；查得到就不存在該情境，不得提問。**現況查得到的事，不准當成問題。**
59. **禁止用「請你裁決」為已寫下的規格外程式補票。** 規格外邏輯若已寫進 code，一律**先移除**，要加再依 §0.8 條 28 走「純文字提案 → 取得同意 → 才寫」。「先寫了再問要不要留」＝先斬後奏。
60. **交付前自檢的執行方式（§0.8 條 31 操作化）**：逐一列出本次新增的每個函式／參數／欄位／分支，各自對應到 SA 或使用者指令的**哪一句**。對應不到 → **刪掉**，不是拿來問。
61. **不得重複詢問本對話已回答過的事。** 提問前先掃本對話：已提供的值、已定案的選擇、已否決的方案，一律不再問。

> 本節補 §0.15 條 55 的漏洞：該條只說「回報只寫結論與待決事項」，未定義什麼才算待決事項。

### 0.18 交付物集合與適用範圍（先定義，後續兩節一體適用）

62. **本專案「交付物集合」定義如下，§0.18～§0.20 各條一律一體適用，不因文件類型而豁免**：
    SA 規格書、`SKILL.md`、操作手冊、`*.skill` 打包檔、
    references／範例／註解，以及日後新增的任何文件或程式。
    「文件」與「程式」在本節不作區別待遇——**凡承載資訊者皆屬之**。
63. **衍生關係（上游異動必須同次推到下游，不得分次）**：
    SA → `SKILL.md`／操作手冊 → `*.skill` 打包檔。
    上游任一處異動，其下游全部相關處必須在**同一次任務內**同步完成；
    不得只改上游、下游留待日後。

### 0.19 否決即消滅（殘留防治，適用全交付物）

64. **使用者否決某設計時，須連同承載它的所有形式一併刪除**：設定檔、欄位、參數、旗標、函式、UI 文案，**以及描述它的敘述句**。不得以改寫措辭、搬移到其他章節或其他文件、降級為「選項／建議／未來可加／目前不啟用」等方式讓該概念存活。**刪掉容器卻保留概念＝未刪除。**
65. **刪除後必須跨全交付物掃描確認零殘留**：以該設計的識別關鍵詞（欄位名、參數名、概念詞）掃描條 62 所列**全部**項目。**「概念被改寫成不同措辭」亦屬殘留**——字串比對不足以證明清除，須另以概念為準重讀相關章節。掃描通過才算完成、才回報。
66. **移除類修訂須在同一次完成**，不得「先刪主體、敘述之後再說」，亦不得「先改程式、文件之後再說」。
67. **打包前重新掃描**：`*.skill` 等打包檔為快照，最易挾帶已刪除的舊內容。每次打包前須重新確認打包來源與最新版一致。

### 0.20 對帳與作者責任（適用全交付物）

68. **標示作者**：任一交付物中凡由 AI 自行補寫、非使用者明示的內容，須於該處標記（如 `[AI 補充]`），使後續閱讀可分辨「使用者需求」與「AI 推導」。
69. **產出前逐條對帳**：將任一份文件的內容轉為另一份文件或程式時，每一項須同時標出 (a) 來源出處，(b) 該出處所對應的**使用者決策**。兩者皆能對上才可產出。
70. **對不上時的處置分流（不得混用）**：
    - 對不上的是 **AI 自己寫的程式碼** → 依 §0.8 條 31、§0.17 條 60 **直接刪除**。
    - 對不上的是 **任一交付物中的文件條文**（使用者資產）→ **停下來提報該條請使用者裁示**；**不得自行刪改**，亦**不得因為「文件有寫」就照做**。
71. **禁止以「文件有寫」免除判斷責任**：凡交付物由 AI 起草，AI 即為共同作者，不得把自己寫的內容當成使用者需求引用。發現可疑條文而未提報即照抄沿用，視同臆測（違反 §0.10）。
72. **版本確認**：每次依任一交付物提方案、產出下游文件或實作前，須確認所讀為當下最新版（重新讀取，或確認自上次讀取後無異動），並在回報中載明所依據的版本狀態。
73. **跨文件一致性**：同一事實出現在多份交付物時，內容必須一致。發現任兩份不一致即屬缺陷，須在**當次**修正到一致，不得留待日後、不得只修其中一份，也不得回頭問使用者「要以哪份為準」——以 §0.18 條 63 的衍生關係定其上游為準，並將上游值同步至全部下游。

### 0.21 執行前強制檢核（每次動手前逐項走完，無例外）

> 本節針對的失效模式是：規則讀過一次，之後每次動手靠印象，導致同類違規反覆發生。
> 因此檢核必須是**你看得見的產出**，不是 AI 事後聲稱做過。

74. **每個任務開始時，重新讀取 §0 全文**，不憑記憶。§0.1「以查證取代記憶」**同樣適用於本規範自身**——記不得規則等於沒有規則。
75. **任何符合下列任一條件的回覆**，必須在回覆中輸出查證清單，逐節列出 §0.1～§0.23 本次適用與否、以及如何滿足。**查證清單未出現在該則回覆中，即視為未檢核，不得動手或輸出現況陳述。**
   - （a）**即將呼叫任何會改動交付物的工具**（push_files、create_or_update_file、Edit、Write 等）；
   - （b）**回覆正文中含任何現況陳述**——包括但不限於 PR 狀態（open／closed／merged）、檔案現況（sha、內容）、Jira 單狀態、state.json 欄位值。
   查證清單須含「**證據**」欄，填入**本則回覆中**實際呼叫的工具名與回傳關鍵值（如檔案 sha／md5、單號、狀態名）；僅寫「已確認」「符合」而無工具呼叫結果者，**視同未檢核**。
76. 查證清單中任一項無法確認滿足 → **停止並回報**，不得以「其餘都符合」帶過。
77. **同一則回覆不得既提案又執行。** 對保護清單內檔案的修改，提案與動手必須分屬兩則回覆，中間須有使用者的明確同意。此設計使「停下」成為可觀察的事實，而非 AI 主觀聲稱。

### 0.22 寫入的機制性限制（不依賴 AI 判斷）

> 本節條文刻意設計為機制性：即使 AI 判斷失誤、即使當下沒想起任何規則，
> 使用者的內容仍不會被整份覆蓋，且任何誤動作皆可還原。

78. **對已存在的檔案一律禁用 Write，只能用 Edit 做片段替換。** Write 僅限建立「確認不存在」的新檔。需大幅改寫時亦須拆成多次 Edit。**無任何例外**——包含「使用者說重寫」「內容改動過大」「分段太麻煩」「重建舊版」。**「寫入」以行為認定，不以工具名認定**：bash／python／shell 重導向（`>`、`>>`）／`cp`／`mv` 覆蓋等任何途徑寫入既有檔案，一律**視同 Write，同受本條禁止**；既有檔案只能經 Edit 片段替換。
79. **動任何既有檔案前先建立時間戳備份**（如 `檔名.bak-YYYYMMDDThhmm`）。備份失敗即不得進行修改；經使用者確認無誤後才可刪除備份。
80. **收到「檔案已被修改／含有不在 context 內的變更」提示時，必須立即停止寫入**，重新完整讀取該檔，並向使用者回報「我上次讀取後有變動、我不知道變了什麼」，取得指示後才可繼續。**不得以「我的編輯套用乾淨」為由繼續。**
81. **使用者的文字不是 AI 的素材。** 對使用者已編輯過的交付物，AI 只能執行使用者明確指定的機械性操作（刪除指定段落、重編號、修正交叉引用）；**不得順手改寫用語、精簡敘述或調整語氣**。

### 0.23 用語與禮貌規範（對象為產品經理；每次回應前自我檢查，無例外）

> 本節與其餘 §0 同屬硬性規定，每次回應皆須自我檢查，不得自訂例外跳過。

82. **保持禮貌、客氣**：用語須尊重對方、語氣友善。
83. **禁止祈使句**：不得使用命令、指使語氣；改以陳述或請託語氣表達。
84. **用詞簡潔、只說重點**：不展開冗述、不重述規則、不自我檢討，除非使用者要求。
85. **禁止製造議題**（與 §0.17 一致）：不虛構情境、不製造非必要的決策題。
86. **適時使用「請」「謝謝」「對不起」**。
87. **一律使用繁體中文與台灣用語**。

---

## Git 協作規範

### 角色分工

| 角色 | 負責 |
|------|------|
| Claude | 建立分支、push 檔案、開 PR |
| 使用者（TPM） | review diff、Approve、Merge |

### 分支命名規則

| 類型 | 格式 | 範例 |
|------|------|------|
| 上線部署 | `chore/activate-{產品}-{日期}` | `chore/activate-農場-20260716` |
| 設定更新 | `config/簡述` | `config/update-aad-ids` |
| 修 bug | `fix/簡述` | `fix/notify-deploy-logic` |
| 新功能 | `feat/簡述` | `feat/add-product-b` |

> **規則：Claude 所有程式異動一律推分支並開 PR，不直接 push main。**

---

### 使用者 PR 簽核流程（每次 Claude 開 PR 後執行）

**方式一：GitHub 網頁**

1. 點開 Claude 提供的 PR 連結
2. 點「**Files changed**」分頁，確認 diff 內容
3. 點右上角「**Review changes**」→ 選「**Approve**」→「**Submit review**」
4. 回「**Conversation**」分頁 → 點「**Merge pull request**」→「**Confirm merge**」
5. 本地同步：
```bash
cd ~/Documents/104-release-automation && git checkout main && git pull origin main
```

**方式二：VS Code**

1. 左側點「**Source Control**」圖示
2. 點「**...」→「Pull**」同步遠端
3. 切換到 main branch 後即可看到最新內容

---

### 使用者自行修改並推送流程

**Terminal 指令（建議）：**

```bash
# 1. 同步 main
cd ~/Documents/104-release-automation && git checkout main && git pull origin main

# 2. 建立分支
git checkout -b config/update-webhook-url

# 3. 修改檔案後 stage 並 commit
git add config.json
git commit -m "config: update teams webhook url"

# 4. 推到遠端
git push origin config/update-webhook-url
```

**VS Code 操作（不用 Terminal）：**

1. 用 VS Code 開啟 `~/Documents/104-release-workflow` 資料夾
2. 左下角點目前分支名稱（如 `main`）→ 選「**Create new branch**」→ 輸入分支名稱（如 `config/update-webhook-url`）→ Enter
3. 修改檔案
4. 左側點「**Source Control**」圖示 → 在「Changes」區點「**+**」stage 要提交的檔案
5. 上方輸入 commit 說明 → 點「**✓ Commit**」
6. 點「**Publish Branch**」或「**Sync Changes**」推到 GitHub

推完後到 GitHub 開 PR。

---

### 常用 git 指令速查

| 動作 | 指令 |
|------|------|
| 切到 main 並同步 | `cd ~/Documents/104-release-automation && git checkout main && git pull origin main` |
| 建立新分支 | `git checkout -b 分支名稱` |
| 查看目前狀態 | `git status` |
| Stage 所有修改 | `git add .` |
| Stage 特定檔案 | `git add config.json` |
| Commit | `git commit -m "說明"` |
| Push 分支 | `git push origin 分支名稱` |
| 查看所有分支 | `git branch -a` |
| 刪除本地分支 | `git branch -d 分支名稱` |

---

## 技術速查表

### Jira 欄位 ID（此為 104corp 範例值；你的值請依新增產品 Checklist 章節，從現有上線單自動帶出）

| 欄位 | 值 |
|------|-----|
| Cloud ID | `{CONFIG.jira.cloud_id}`（讀自 config.json `jira.cloud_id`） |
| 上線單 project | `{jira_deploy_project}`（config.json 中設定） |
| 上線單 issue type ID | `{jira_deploy_issue_type_id}`（config.json）｜MCP 呼叫用 `issueTypeName`（見你的 Jira issue type 名稱） |
| Epic project | `{jira_product_project}`（config.json 中設定） |
| Epic issue type ID | `{jira_epic_type_id}`（config.json）｜MCP 呼叫用 `issueTypeName`（見你的 Jira Epic 類型名稱） |
| depends on link type ID | （依你的 Jira link type 設定）｜MCP 呼叫用 `type`（見你的 Jira issue link 類型名稱） |
| 排定送Q日期 field | `customfield_11221`（datetime） |
| 排定上線日期 field | `customfield_11222`（datetime） |
| Domain field | `{jira_domain_field}`（config.json）（parent/child id 各取 config.json `jira_domain_parent_id` / `jira_domain_child_id`） |
| Engineer field | `{jira_engineer_field}`（config.json） |
| Datasource ID（列表） | `{FILL_IN_datasource_id}`（從你的 Jira 上線單 description blockCard 中取得） |
| Lab/Stg/Prod 驗收 fields | `customfield_16933` / `16934` / `16935` |
| Web/API 程式資訊 field | `customfield_12324`（Steps 7–8 使用） |
| 異動與個資讀寫相關 field | `customfield_12767`（radio：否 `12293`／是 `12292`）｜Step 1 建單填；為 STG 轉單「已更新Staging」的必填驗證 |
| 簽核主管 field | `customfield_11300`（user）｜轉「簽核中」validator 必填；值取 config `jira_sign_manager_account_id` |
| 系統等級 field | `customfield_13602`（單選）｜Step 1 建單填；值取 config `jira_system_level`（存選項 id，`13204` = 1-2）；全站固定 |

### 日期格式
- Jira datetime：`2026-07-02T12:00:00.000+0800`
- state.json / branch name：`20260702`（純數字）
- Teams 顯示：`7/2`

### GitHub Repo
- Owner／Repo：讀自 `config.json` 的 `github.owner` / `github.repo`（即你在 config.json 中設定的 owner/repo）
- 主要檔案：`config.json`、`state.json`

### 上線單狀態轉換（共用邏輯，非 per-product）

所有上線單共用同一個上線單 workflow。程式**依「目的地狀態」尋找 transition**（掃可用 transition，找 `to.name` 符合的那顆執行），**不依賴 transition 名稱或 id**：

| 目的地狀態 | 由哪個 transition 到達 |
|---|---|
| STAGING測試中（STG，首筆 STG PR 後轉） | 已更新Staging（目的地狀態名稱，程式動態尋找） |
| 簽核中（PROD，有 STG PR 的 repo 全找齊後轉） | 申請上線（目的地狀態名稱，程式動態尋找） |

> - 轉「簽核中」時**必帶簽核主管**（`customfield_11300`，值取 `product_config.jira_sign_manager_account_id`）：workflow validator 強制，未帶回 400「請選擇簽核主管」
> - 轉單韌性：單已在目標狀態 → 視為轉單成功（繼續發通知）；STG 若狀態已達或超過「STAGING測試中」→ 視為完成、不再重試；PROD 轉「簽核中」失敗 → 通知③僅首次發送，之後每輪重試，超過上線日 +1 仍失敗才終止；轉單失敗時 log 印出 Jira 完整錯誤訊息（HTTP code＋回應內容）

---

## 新增產品設定 Checklist

在 `config.json` 的 `products` 下新增一個產品時，逐項確認以下設定與跨團隊前置作業。
依據：`config.json`（現有農場設定）＋ `scripts/notify_deploy.py`／`scripts/collect_prs.py`（實際讀取的欄位）＋ Steps 1–5。

### 頻道 / 人員（基本項）

- [ ] 上線作業頻道 webhook URL → `teams_deploy_channel_webhook_url`（通知①用）
- [ ] 產品頻道 webhook URL → `teams_product_channel_webhook_url`（通知②用）
- [ ] 需 @mention 的工程師 AAD Object ID → `talent_engineering_members[].aad_id`
- [ ] 需 @mention 的主管／簽核人 AAD Object ID → `approvers[].aad_id`（per-product；外層 `approvers: []` 為 fallback）
- [ ] 所有 repo → `repos[]`
- [ ] `hosting`：`aws`（雲端，如農場／New Index）或 `k8s`（地端，如 CMS）。地端另填 `k8s_subtickets`（repo → ArgoCD app 名稱，如 `blog-web`）
- [ ] 上線時間 → `deploy_time`（如 CMS＝10:00；未填預設 12:00。用於排定上線日期與通知①文案；排定送Q時間固定 12:00 不參數化）
- [ ] 簽核主管 **Jira accountId** → `jira_sign_manager_account_id`（轉「簽核中」validator 必填；為 Jira 帳號 ID，與 approvers 的 AAD ID 不同體系，需另查）
- [ ] 系統等級 → `jira_system_level`（存選項 id；`13204` = 系統等級「1-2」，全站固定共用，沿用即可）
- [ ] （選填）上線單標題樣板 → `jira_deploy_summary_template`（未設用預設「{display_name} - {deploy_M_D} 維運上線」；tokens：`{deploy_date}`=YYYY/MM/DD、`{deploy_M_D}`=M/D、`{display_name}`）

### A. Jira 設定群組（免手動查——給兩張單，Claude 自動帶出）

新增產品時不需逐項查 customfield，只要提供：
1. 一張該產品**現行上線單** URL（`.../browse/{YOUR_PROJECT}-XXXX`）
2. 一張該產品**現行 Epic** URL（`.../browse/{YOUR_EPIC_PROJECT}-XXXX`）
3. 產品**觸發關鍵字**（如「農場」）與**顯示名稱**（如「關鍵人才庫」）

Claude 以 Jira MCP 自動讀出並填好：`jira_deploy_project`、`jira_deploy_issue_type_id`、`jira_product_project`、`jira_epic_type_id`、`jira_web_api_field`、`jira_q_date_field`、`jira_deploy_date_field`、`jira_domain_field`＋`parent/child`、`jira_assignee_account_id`、`jira_engineer_field`。TPM 只需核對。

> 其中 `jira_deploy_issue_type_id`、`jira_web_api_field`、`jira_q_date_field`、`jira_deploy_date_field`、`jira_domain_field`、`jira_engineer_field` 在同一 Jira 站台為**固定共用值**（各產品相同），沿用即可；per-product 真正會變的只有 `jira_product_project`、`jira_domain_parent_id`／`jira_domain_child_id`、`jira_assignee_account_id`。

> ⚠️ Domain parent/child 依上線單當下值帶出，請確認是該產品正確事業群。

### B. GitHub / Repo

- [ ] `repo_display_names`：每個 repo 的短標籤（農場 = B / C / CRM / API / Search），顯示於 Jira 欄位與 Teams 通知
- [ ] **GH_PAT 權限**：現有 `GH_PAT` secret 必須能讀取新產品的**所有 repo**（尤其跨 org 或私有 repo），否則 PR 搜集抓不到任何 PR
- [ ] **跨團隊協議**：PR 搜集**不依賴分支命名**，改看 PR title——新產品工程師的 STG/PROD PR **title 必須含上線日期（YYYYMMDD 或 YYMMDD）與 `stg`/`[stg]` 或 `prod`/`[prod]`（不分大小寫）**；STG PR 須**已 merged**，PROD PR 為 **open**（簽核在 merge 之前）

### C. 人員 AAD ID 注意事項

- [ ] 每位工程師、簽核人的 AAD ID 都已填（未填者不會被 @mention，但流程仍會跑）
- [ ] 被 @mention 的人**確實是該 Teams 頻道成員**（非成員 mention 無效）

### D. 可沿用 / 共用的項目（新增產品時通常不用改）

- [ ] `CONFIG.tpm.teams_personal_webhook_url`：人工轉單提醒（通知③），置於 top-level **`tpm` 區塊**（與 `tpm.jira_account_id` / `tpm.email` 同組）。除非換不同 TPM 負責，否則沿用即可
- [ ] `jira.cloud_id` / `jira.base_url`：同一 Jira cloud 共用
- [ ] Step 4 的 datasource ID（`d8b75300-...`）：本檔內固定值，同 Jira cloud 共用（建議建完第一張單目視確認表格有正常渲染）

### config.json 產品 entry 骨架（複製後填值）

```json
"{產品名(觸發關鍵字)}": {
  "display_name": "",
  "hosting": "aws",
  "deploy_time": "12:00",
  "jira_deploy_project": "",
  "jira_deploy_issue_type_id": "",
  "jira_product_project": "",
  "jira_epic_type_id": "",
  "jira_web_api_field": "",
  "jira_q_date_field": "",
  "jira_deploy_date_field": "",
  "jira_domain_field": "",
  "jira_domain_parent_id": "",
  "jira_domain_child_id": "",
  "jira_assignee_account_id": "",
  "jira_engineer_field": "",
  "jira_sign_manager_account_id": "",
  "jira_system_level": "13204",
  "repos": [],
  "repo_display_names": {},
  "teams_deploy_channel_webhook_url": "",
  "teams_product_channel_webhook_url": "",
  "talent_engineering_members": [
    { "name": "", "aad_id": "" }
  ],
  "approvers": [
    { "name": "", "aad_id": "" }
  ]
}
```

> 地端產品（`hosting: "k8s"`）另加 `k8s_subtickets`（repo → ArgoCD app 名稱，如 `"{your-org}/{your-k8s-repo}": "your-argocd-app"`）；雲端產品不需要。

> `jira_system_level`（存選項 id，現值一律 `13204` = 系統等級「1-2」）：所有產品必填。（選填）`jira_deploy_summary_template`：自訂上線單標題樣板（tokens `{deploy_date}`=YYYY/MM/DD、`{deploy_M_D}`=M/D、`{display_name}`）；未設用預設 `{display_name} - {deploy_M_D} 維運上線`。

---

## 非 MCP 備援模式（本機 CLI 執行）

> **觸發**：使用者說「用備援模式」「MCP 連不上、用本機」等，並附正常開單指令（`YYYY/MM/DD 產品名 [k8s [dr]]`）。
> **執行者**：Claude 在使用者本機（Claude Code），全程用本機 shell——Jira 走 REST API（curl）、GitHub 走 git。
> **本模式下不得呼叫任何 MCP 工具，也不得開瀏覽器操作網頁 UI。**
> **共用不重寫**：除下方「執行方式」與「§0.4 改寫」外，其餘一律沿用正式流程——指令解析與 hosting 檢查、讀 `config.json` 取 per-product 值、送Q/上STG/上線日計算、K8s 站點展開（primary/dr）、完成回報格式，以及 §0 強制規範。

### 認證前置（每次執行前先做，缺了就停）

1. **Jira 認證 token 放這裡：`~/.104-release.env`**（repo 外的家目錄，權限 600）。內容為兩行：
   ```
   export JIRA_EMAIL="your-jira-email@company.com"
   export JIRA_API_TOKEN="<Jira API token>"
   ```
   **每個要打 Jira 的 shell 指令前，先 `source ~/.104-release.env`** 把這兩個變數載入當下 shell。
   - 若 `~/.104-release.env` 不存在，或 `source` 後 `$JIRA_API_TOKEN` 為空 → **停止並引導使用者建立**：到 <https://id.atlassian.com/manage-profile/security/api-tokens> 產 token，再：
     ```bash
     echo 'export JIRA_EMAIL="your-jira-email@company.com"' >  ~/.104-release.env
     echo 'export JIRA_API_TOKEN="<貼上token>"'       >> ~/.104-release.env
     chmod 600 ~/.104-release.env
     ```
   - **不得**把 token 寫進 repo 內任何檔、不得憑空繼續、不得把 token 印進 commit 或 PR。
2. **Jira REST base**：由 `config.json` 的 `jira.base_url`（如 `https://{your-site}.atlassian.net/browse`）推得站台，REST base＝`https://<site>/rest/api/3`。
3. **GitHub 認證**：用本機既有 git 認證即可；開 PR 用 `gh`（若已安裝）或輸出 compare 連結給使用者手動開。

### 執行方式（Steps 1–5 的本機對應）

欄位內容與正式版 Steps 1–5 完全相同（欄位 ID／值見各步與「技術速查表」，此處不重列）；差別只在「改用 curl/git 送出、並自行解析回傳」。

每個 Jira 呼叫的共用起手式：
```bash
source ~/.104-release.env
BASE="https://{your-site}.atlassian.net/rest/api/3"
AUTH=(-u "$JIRA_EMAIL:$JIRA_API_TOKEN" -H "Content-Type: application/json")
```

- **Step 1 上線單**：`curl -sS "${AUTH[@]}" -X POST "$BASE/issue" -d '<Step 1 fields JSON>'` → 取回傳 `.key` 存為 `DEPLOY_KEY`。
- **Step 2 Epic**：POST `$BASE/issue`（`projectKey`＝`product_config.jira_product_project`、issuetype「大型工作」、summary「{yyyymmdd} 上線」、`customfield_10014` 同名）→ `.key` 存 `EPIC_KEY`。
- **Step 3 depends**：POST `$BASE/issueLink`，`{"type":{"name":"Depends(WBSGantt)"},"inwardIssue":{"key":"<EPIC_KEY>"},"outwardIssue":{"key":"<DEPLOY_KEY>"}}`。
- **Step 4 描述**：PUT `$BASE/issue/<DEPLOY_KEY>`，body 為 Step 4 的 ADF `blockCard`（jql 帶 `<EPIC_KEY>`，datasource id `{FILL_IN_datasource_id}`）。
- **Step 4.5 K8s 子單**（僅 `hosting=k8s`）：對 `build_k8s_specs(product_config, sites)` 產出的每個 spec，POST `$BASE/issue`（issuetype「(Sub) K8s上線」、頂層 `parent`＝`<DEPLOY_KEY>`、assignee＝`CONFIG.tpm.jira_account_id`、DR 子單另帶 `customfield_14301:"Y"`）。
- **Step 5 state.json＋PR**（本機 git，取代 GitHub MCP）：
  ```bash
  cd <repo 根目錄>          # 與 state.json 同層
  git checkout main && git pull origin main
  git checkout -b chore/activate-{product}-{yyyymmdd}
  # 以「程式化最小修改」更新 state.json：在 active_deployments 覆蓋/新增該產品 entry
  #   （stg_prs 值=[]、prod_prs 值=null，key 依 product_config.repos 產生；含 deploy_issue_key/epic_key/日期/status=active）
  git add state.json && git commit -m "chore: activate {product} deployment {yyyymmdd}"
  git push origin chore/activate-{product}-{yyyymmdd}
  # 開 PR：gh pr create --base main --head chore/activate-{product}-{yyyymmdd} ...
  #        或輸出：https://github.com/<owner>/<repo>/compare/main...chore/activate-{product}-{yyyymmdd}?expand=1
  ```

### 執行紀律（本模式強制）

- **建完第一張上線單先停**，請使用者到 `.../browse/<DEPLOY_KEY>` 核對欄位（送Q/上線日、事業群-Domain、異動與個資讀寫相關、系統等級、Engineer）無誤，再續建 Epic／子單。
- **任一 curl／git 失敗即停**，把**原始回應（HTTP 碼＋body）完整貼給使用者**；不臆測、不自行改欄位重試超過一次、不繞道。
- state.json 一律**推分支＋開 PR**；push 後檢視 diff，只能出現本次預期變動（同 §0.3）。
- state.json 需帶入的 `deploy_issue_key`／`epic_key` 一律用**本次實際建立回傳的單號**，不得沿用記憶或猜測（§0.1）。

### §0.4 於本模式的改寫

> 本模式下，§0.4（工具使用）改為：**Jira／GitHub 操作一律用本機 CLI（curl／git／選配 gh），不得開瀏覽器操作網頁 UI；CLI 失敗就停止並回報，不改用瀏覽器或其他繞道。**
> §0 其餘各條（§0.1 以查證取代記憶、§0.2 檔案異動授權、§0.3 推分支＋驗 diff、§0.5 態度誠實、§0.8 規格紀律、§0.9 完整足跡、§0.10 查證優先…）**原則不變**；凡條文提到「MCP 工具查證／操作」，於本模式一律以「本機 CLI（curl GET／git／API 呼叫）」等效替代。

---

## Steps 7–8：GitHub Actions 自動 PR 搜集與通知

這部分由 `scripts/collect_prs.py` 透過 GitHub Actions 每 30 分鐘（窗口制）執行。

### Step 7：STG PR 搜集（上線日 −3 工作天起）

#### 觸發條件
`today >= 上線日 −3 工作天`（且未終止；工作日只跳週末）

#### PR 搜尋邏輯
- 搜尋來源：用 GitHub Search API 對各 repo 搜 title 含上線日期 token（`YYYYMMDD` 與 `YYMMDD`）且 `is:merged` 的 PR（不依賴 head/base 分支名稱）
- 篩選條件：PR title 符合 `(?i)(\bstg\b|\[stg\])` 正則
- 每個 repo 可有多筆 STG PR，全部收集

#### 自動動作
1. 有新 STG PR → 更新 Jira `customfield_12324`（Web/API 程式資訊）
2. **找到 STG PR 後** → 將主單推進到「STAGING測試中」狀態（依目的地狀態尋找 transition）；**當次未轉成功，之後每 30 分排程會持續重試，直到成功**
   > 前提：上線單「異動與個資讀寫相關」（`customfield_12767`）需已於 Step 1 建單時填值，否則此轉換的 workflow 必填驗證會擋下（回 400、轉單失敗）。
3. 持續每 30 分搜集，直到 Step 8 的終止事件發生

#### Jira 欄位格式（`customfield_12324`）

每次更新都依「目前累積的**全部** STG／PROD PR」**重畫整格**（非逐筆追加，舊資料也會一起用最新格式重寫）。URL 以純文字呈現（避免被 Jira 渲染成 `[url|url]`）；內容以 Jira 原生巢狀清單（ADF bulletList）呈現：同一 repo **單筆** → URL 接在名稱同一清單項目，**多筆** → 名稱一個項目、URL 逐筆為巢狀子項目（真縮排，檢視／編輯都保留），[STG PR]／[PROD PR] 兩區塊間僅一個空行。

範例（Resume API 多筆 STG、Rundeck 單筆；PROD 各一筆）：

```
[STG PR]
• Repo A：
    ◦ https://github.com/{your-org}/{repo-a}/pull/58
    ◦ https://github.com/{your-org}/{repo-a}/pull/60
• Repo B：https://github.com/{your-org}/{repo-b}/pull/12
• Repo C：

[PROD PR]
• Repo A：https://github.com/{your-org}/{repo-a}/pull/64
• Repo B：
```

> 單筆 → URL 同一清單項目；多筆 → 名稱一個項目、URL 為巢狀子項目。清單為 Jira 原生 bullet list（ADF bulletList），縮排不會因 wiki 樣式流失。

---

### Step 8：PROD PR 搜集（上線日 −1 工作天起）

#### 觸發條件
`today >= 上線日 −1 工作天`（且未終止；可與 STG 窗口重疊）

#### PR 搜尋邏輯
- 搜尋來源：用 GitHub Search API 對各 repo 搜 title 含上線日期 token（`YYYYMMDD` 與 `YYMMDD`）且 **open 狀態**（`is:open`，簽核在 merge 之前）的 PR（不依賴 head/base 分支名稱）
- 篩選條件：PR title 符合 `(?i)(\bprod\b|\[prod\])` 正則
- 每個 repo **只取第一筆**（一個 repo 只會有一個 PROD PR）

#### 自動動作（有 STG PR 的 repo 全找齊對應 PROD PR 後；無 STG PR 的 repo 不列入、不等待）
將主單推進到「簽核中」狀態（依目的地狀態尋找 transition）：
1. **轉單成功** → 發 PROD PR 簽核通知至產品頻道（通知②，@mention 簽核人），終止（`stopped_reason=signed`）
2. **轉單失敗** → 發 TPM 個人頻道人工提醒（`tpm.teams_personal_webhook_url`，**僅首次失敗發送**，以 `manual_notice_sent` 防重複），保持 active、之後每 30 分重試（如 QA 尚未把單轉到「Staging測試完成」的時序問題會自癒）；`today > 上線日 +1 日曆天` 仍失敗 → 再提醒一次並終止（`stopped_reason=manual`）
3. **安全停損**：PROD 一直沒找齊、`today > 上線日 +1 日曆天` → 發 TPM 個人頻道告警，終止（`stopped_reason=timeout`）

終止收尾皆設 `status=completed`，STG 也隨之停止；轉單失敗當下不設 completed。

#### Teams 簽核通知格式（Adaptive Card，通知②）
```
🚀 PROD PR 簽核通知｜{display_name} {deploy_date_yyyymmdd}

{@簽核人} Hi all，{display_name}本次上線所有 PROD PR 已就緒，
請抽空簽核上線單與 PR，感謝

- 上線單：{jira_url}
- PR：
   - {repo 短標籤}：{PROD PR 連結}   ← 只列已收齊 PROD PR 的 repo，每個一行
```

---

### 上線單狀態轉換（共用邏輯）

所有上線單共用同一個上線單 workflow。程式**依目的地狀態尋找 transition**（掃可用 transition，找 `to.name` 符合的那顆，用 Jira 當場提供的 id 執行），不依賴 transition 名稱或 id：

| 目的地狀態 | 由哪個 transition 到達 |
|---|---|
| STAGING測試中（STG） | 已更新Staging（目的地狀態名稱，程式動態尋找） |
| 簽核中（PROD） | 申請上線（目的地狀態名稱，程式動態尋找） |

> - 轉「簽核中」時**必帶簽核主管**（`customfield_11300`，值取 `product_config.jira_sign_manager_account_id`）：workflow validator 強制，未帶回 400「請選擇簽核主管」
> - 轉單韌性：單已在目標狀態 → 視為轉單成功（繼續發通知）；STG 若狀態已達或超過「STAGING測試中」→ 視為完成、不再重試；PROD 轉「簽核中」失敗 → 通知③僅首次發送，之後每輪重試，超過上線日 +1 仍失敗才終止；轉單失敗時 log 印出 Jira 完整錯誤訊息（HTTP code＋回應內容）

---

### state.json 欄位說明

```json
{
  "active_deployments": {
    "農場": {
      "deploy_date": "20260702",
      "stg_date": "20260630",
      "deploy_issue_key": "{YOUR_PROJECT}-XXXX",
      "deploy_issue_url": "https://{your-site}.atlassian.net/browse/{YOUR_PROJECT}-XXXX",
      "epic_key": "{YOUR_EPIC_PROJECT}-XXXX",
      "stg_prs": {
        "{your-org}/{repo-1}": ["https://github.com/..."],
        "{your-org}/{repo-2}": [],
        "{your-org}/{repo-3}": [],
        "{your-org}/{repo-4}": [],
        "{your-org}/{repo-5}": []
      },
      "prod_prs": {
        "{your-org}/{repo-1}": null,
        "{your-org}/{repo-2}": null,
        "{your-org}/{repo-3}": null,
        "{your-org}/{repo-4}": null,
        "{your-org}/{repo-5}": null
      },
      "stg_status_updated": false,
      "signing_notified": false,
      "status": "active"
    }
  }
}
```

| 欄位 | 說明 |
|------|------|
| `stg_date` | 上STG日（上線日 −2 工作天），通知①顯示用；與 Jira「排定送Q日期」（上線日 −1 工作天）是**不同日期** |
| `stg_status_updated` | 是否已推進到「STAGING測試中」 |
| `signing_notified` | 是否已發簽核通知（通知②） |
| `status` | `active` / `completed` |
| `stopped_reason` | 收尾原因：`signed` / `manual` / `timeout`（終止時才寫入；Step 5 建立時不含此欄位） |
| `manual_notice_sent` | 轉「簽核中」失敗時設 `true`，防止通知③重複發送（執行期才寫入；Step 5 建立時不含） |
| `pending_queue` | 同產品第二張以上上線單暫存陣列；每筆含 `deploy_date`、`stg_date`、`deploy_issue_key`、`deploy_issue_url`、`epic_key`；現有條目簽核完成後由 `collect_prs.py` 自動啟用下一筆（執行期才寫入；Step 5 建立時初始為 `[]`） |

三種終止收尾（轉單成功 signed／重試至停損日仍失敗 manual／安全停損 timeout）皆將 `status` 設為 `completed`，下次執行自動跳過。

---

### GitHub Actions Workflow

檔案：`.github/workflows/pr-collector.yml`
- Cron：`*/30 * * * *`（每 30 分鐘 UTC）；窗口外由腳本即刻結束
- 手動觸發：`workflow_dispatch`
- 執行：`python scripts/collect_prs.py`
- 完成後自動 commit & push 更新的 `state.json`（最多重試 5 次：每次先 `git pull --rebase --autostash origin main` 再 `git push`；5 次皆失敗時透過 `tpm.teams_personal_webhook_url` 發 Teams 個人告警並以 exit 1 終止）

所需 GitHub Secrets：
- `GH_PAT`（需有 repo + workflow 權限）
- `JIRA_EMAIL`
- `JIRA_API_TOKEN`

Teams webhook URL 直接存放於 `config.json`（私有 repo）。

---

### 手動補發簽核通知（通知②）

若通知②因 webhook 當機或其他原因未成功送達，可手動補發：

**方式：GitHub Actions → Notify Signing Channel（`notify-signing.yml`）→ Run workflow**

- `product_name`：必填，指定單一產品名稱（如「農場」）；不支援一次發送全部
- `force`：可選，預設 `false`；`true` 可覆蓋防呆（`status=completed` 或尚未收集到 PROD PR 時仍發送）

**防呆規則（`force=false` 時拒絕發送）：**
- `status` 已是 `completed`（上線單已結案）
- `prod_prs` 全為 null（尚未收集到任何 PROD PR）

**實作檔案：**`scripts/notify_signing.py`、`.github/workflows/notify-signing.yml`（工具讀 `config.json` 與 `state.json`，不需額外 Secret）
