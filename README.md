# 104 自動化開單

複製本 template 到你自己的 GitHub repo，依「操作手冊」設定後即可用 skill 日常維運上線。
skill 會依 `config.json` 的 `github.owner` / `github.repo` 操作，**不需要改 skill**。

## 檔案

- `config.json`：設定檔。所有 `FILL_IN_...` 都要換成你的值（見下）。
- `state.json`：狀態檔，初始為 `{"active_deployments": {}}`，由 GitHub Actions 自動維護，勿手改。
- `scripts/collect_prs.py`：Step 7–8，搜 STG/PROD PR、更新 Jira、依目的地狀態轉單、發通知。
- `scripts/notify_deploy.py`：通知①（上線準備通知）。
- `.github/workflows/pr-collector.yml`：每 30 分鐘排程跑 collect_prs.py。
- `.github/workflows/notify-deploy.yml`：state.json push 觸發，跑 notify_deploy.py。
- `scripts/notify_signing.py`：手動補發通知②（PROD PR 簽核通知）工具，由 notify-signing.yml 呼叫。
- `.github/workflows/notify-signing.yml`：手動觸發（workflow_dispatch），補發 PROD PR 簽核通知至產品頻道。

## 建置步驟（摘要；詳見操作手冊 6.1 首次建置）

1. 在 GitHub 建一個新的 **private** repo（建立時不要加任何檔案，不勾 README／.gitignore）。
2. 把本 template 全部檔案（含 `.github/`）推上去（`{your-org}/{your-repo}` 換成你的）：

```bash
cd 104-release-workflow-template          # 進入解壓後的資料夾
git init
git add .
git commit -m "init from 104-release-workflow template"
git branch -M main
git remote add origin https://github.com/{your-org}/{your-repo}.git
git push -u origin main
```

3. Repo → **Actions** 分頁，若出現啟用提示就點啟用。
4. Repo → Settings → Secrets and variables → Actions，新增：`GH_PAT`、`JIRA_EMAIL`、`JIRA_API_TOKEN`（操作手冊 6.2）。
5. 建 Teams 頻道 Incoming Webhook（上線作業／產品頻道，操作手冊 6.4.2）與個人聊天 Power Automate 流程（6.4.3）。
6. 填 `config.json`：
   - `github.owner` / `github.repo`：**填你這個 repo**（skill 靠這個找 repo）。
   - `jira.cloud_id` / `jira.base_url`；`tpm`（`name` / `jira_account_id` / `email` / `teams_personal_webhook_url`，TPM 個人相關資訊集中於此）。
   - `products.{觸發關鍵字}`：依操作手冊第七章，用一張現行上線單＋Epic 帶出 Jira 欄位，再填 `hosting`（`aws` 雲端／`k8s` 地端）、repos、webhook、工程師/簽核人 AAD ID。**地端（k8s）產品**另填 `k8s_subtickets`（repo → ArgoCD app 名稱）；**雲端（aws）產品請移除 `k8s_subtickets`**。`jira_system_level` 為系統等級選項 id（全站固定共用，現值 `13204` = 1-2，沿用即可）。（選填）`jira_deploy_summary_template`：自訂上線單標題樣板（tokens：`{deploy_date}`、`{deploy_M_D}`、`{display_name}`）；未設用預設「{display_name} - {deploy_M_D} 維運上線」。
7. 完成後即可在 Cowork 用 skill：輸入「YYYY/MM/DD 產品名」開始上線。

## 前提

- 上線單所在的 Jira 專案 workflow 需具備「已更新Staging」「申請上線（→簽核中）」等轉換（程式依**目的地狀態**尋找 transition，不綁名稱/id）。
- `GH_PAT` 需能讀取所有列在 `repos` 的產品 repo。
- 各產品工程師的 STG/PROD PR，title 需含「上線日期（YYYYMMDD 或 YYMMDD）」與 `stg`/`[stg]` 或 `prod`/`[prod]`（不分大小寫）；STG PR 須**已 merged**，PROD PR 為 **open**（簽核在 merge 之前）。
