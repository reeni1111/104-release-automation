# 104 Release Workflow 操作手冊

> 適用對象：TPM
> 最後更新：2026-07-16

---

## 目錄

- [零、背景說明](#ch0)
- [一、名詞說明](#ch1)
- [二、系統架構總覽](#ch2)
- [三、日常上線流程（每次上線時執行）](#ch3)
- [四、自動化後台（GitHub Actions）](#ch4)
- [五、使用到的工具](#ch5)
- [六、前置作業（一次性設定）](#ch6)
- [七、新增產品 完整 Checklist](#ch7)
- [八、config.json 維護](#ch8)
- [九、Git 協作規範](#ch9)
- [十、常見問題](#ch10)

## 導讀

**參考各章時機：**

- **了解本系統**：依序閱讀 [零、背景說明](#ch0) → [一、名詞說明](#ch1) → [二、系統架構總覽](#ch2) → [三、日常上線流程](#ch3)
- **初次設定（一次性）**：[五、使用到的工具](#ch5) → [六、前置作業（一次性設定）](#ch6)（含 6.1 首次建置：從 template 建 repo、push、設 Secrets/webhook、填 config）
-  **新增產品**：[七、新增產品 完整 Checklist](#ch7)
- **想了解 PR 搜集／通知的排程如何運作，或通知沒送出要排查時** → [四、自動化後台（GitHub Actions）](#ch4)
- **要 review／merge PR、開分支、本地 git 同步或處理衝突時** → [九、Git 協作規範](#ch9)
- **要更新 webhook／AAD ID，或新增產品設定時** → [八、config.json 維護](#ch8)
- **遇到通知沒收到、git 衝突、同產品兩張上線單等狀況時** → [十、常見問題](#ch10)

---

<a id="ch0"></a>

## 零、背景說明

本系統用於管理產品上線流程的追蹤與通知。每次上線時，TPM 輸入日期與產品名稱，由 Claude 自動建立 Jira 上線單與 Epic、更新部署追蹤檔，並在各關鍵節點（上線準備就緒、PROD PR 全數就緒）自動發送 Teams 通知給相關工程師與簽核人員，取代人工逐一通知與狀態追蹤。

---

<a id="ch1"></a>

## 一、名詞說明

| 名詞 | 說明 |
|------|------|
| **上線單** | Jira 上的部署工單，記錄本次上線的日期、負責人、Domain 等資訊 |
| **Epic** | Jira 上的功能群組單，匯集本次上線相關的所有工作項目 |
| **STG PR** | 工程師提交到 Staging 環境的 Pull Request，代表功能準備好進測試 |
| **PROD PR** | 工程師提交到 Production 環境的 Pull Request，代表功能準備好上線 |
| **簽核** | PROD PR 全數備妥後，通知簽核人員確認可執行上線的流程 |
| **排定送Q日期** | Jira 上線單欄位（customfield_11221），＝**上線日 −1 工作天**；建單（Step 1）時填入 |
| **上STG日** | 通知①顯示的預計上 Staging 日期，＝**上線日 −2 工作天**；存於 state.json 的 `stg_date`。**與排定送Q日期是兩個不同日期** |
| **state.json** | 系統追蹤檔，記錄目前各產品的上線日期、Jira 單號、PR 蒐集狀態與通知進度 |
| **config.json** | 系統設定檔，記錄各產品的 Jira 欄位、工程師名單、Teams webhook 等固定設定 |
| **`deploy_date`（上線日期）** | 本次的正式上線日，存於 state.json（格式如 `20260702`）。排程窗口、PR 搜集的日期 token、Jira 日期欄位都以它為基準 |
| **webhook** | 自動發送 Teams 通知所使用的 URL；頻道通知用 Teams Incoming Webhook，個人聊天通知用 Power Automate 中轉流程 |
| **AAD Object ID** | 微軟帳號的唯一識別碼，用於在 Teams 通知中 @mention 指定成員 |
| **Description（上線單）** | Jira 上線單的描述欄位。系統建立上線單後，會在此欄位插入一個動態表格，自動列出本次 Epic 底下所有工作項目（依 JQL 查詢即時更新） |
| **Adaptive Card** | Microsoft Teams 的訊息卡片格式，可在 Teams 頻道或聊天中顯示結構化內容（標題、內文、連結等），並支援 @mention 成員。本系統的頻道通知皆以此格式發送 |
| **GitHub Actions** | GitHub 內建的自動化執行環境。本系統用它執行兩個排程：state.json 有變動時立即發通知①，以及每 30 分鐘（窗口制）搜集 STG/PROD PR、轉換 Jira 狀態並發簽核通知 |
| **Power Automate** | 微軟的自動化流程工具（Microsoft 365 服務）。本系統用它建立一個 HTTP trigger 流程，接收 GitHub Actions 的呼叫後，將訊息發送到 TPM 的 Teams 個人聊天（用於通知③） |
| **Jira Cloud ID** | 104 Jira 站台的唯一識別碼（`config.json` 的 `jira.cloud_id`）。每次呼叫 Jira API 都要帶它以指定「對哪個 Jira 站台操作」。全公司只有一個 Jira，所有產品共用、固定不變 |
| **Jira Base URL** | Jira 單子的網址前綴（`config.json` 的 `jira.base_url`，如 `https://104corp.atlassian.net/browse`）。用來把單號組成可點擊連結（如 `…/browse/ITPMTALREQ-8600`）放進 Teams 通知與 state.json。全站共用 |
| **Datasource ID（Step 4）** | Step 4 在上線單描述欄插入「Epic 工作項目動態表格」時，指定要用哪個 Jira JQL 資料來源來渲染該表格的固定識別碼（`d8b75300-…`）。屬此 Jira 站台的內部固定值，所有產品共用；僅在 Step 4 用到 |
| **Jira Workflow（工作流程）** | 一張 Jira 單子「有哪些狀態、狀態之間怎麼流動」的規則圖。所有上線單共用同一個（19_Requirement Wf V4.6） |
| **狀態（status）** | 上線單目前所處的階段，如「staging 作業中」「STAGING測試中」「簽核中」「結案」 |
| **transition（狀態轉換）** | 把單子從一個狀態推進到下一個狀態的「動作／按鈕」，如「已更新Staging」「申請上線」。每個 transition 有自己的名稱、id，以及「按下去會到達的目的地狀態」 |
| **`to.name`（目的地狀態名）** | 某個 transition「按下去會到達的狀態」的名稱。程式靠比對它挑出正確的 transition（不依賴 transition 名稱或 id） |
| **hosting（託管環境）** | 產品部署所在環境：`aws`（雲端）或 `k8s`（地端），記於 `config.json` 每個產品的 `hosting`。地端產品上線時需額外建立 K8s 上線子單 |
| **K8s 上線子單** | 地端（hosting=k8s）產品專用：上線單底下的子工作（Jira sub-task），每個部署 repo 一張，帶 ArgoCD Application 與部署分支資訊，用來觸發該 repo 的 K8s 部署 pipeline。子單名稱＝純 `{name}`（無前綴） |
| **DR 站子單** | 指令帶 `dr` 時，每個 repo 於主站子單外**另建一張** DR 站子單：名稱 `{name} dr site`、ArgoCD 樣板 `stg-dr-{name}`／`prod-dr-{name}`、組態設定=Y（主站子單不填組態設定） |

---

<a id="ch2"></a>

## 二、系統架構總覽

### 主要業務流程（每次上線時執行）

```
TPM 輸入「YYYY/MM/DD {產品名}」（地端產品輸入「YYYY/MM/DD {產品名} k8s」）
      │
      ▼ Claude 自動執行
  Jira 上線單建立 → Epic 建立 → 連結 → 插入 Epic 工作項目連結至上線單描述欄
  →（地端 hosting=k8s：另建 K8s 上線子單）→ 開 PR（含 state.json 異動）
      │
      ▼ TPM review PR → Merge（state.json 正式生效）
      │
      ├─▶ 【通知①】Teams 上線作業頻道（merge 後立即觸發）
      │       Adaptive Card @mention 全體工程師
      │       內容：上線日期、上STG日期、Jira 上線單連結
      │
      ▼ GitHub Actions 窗口制排程（每 30 分鐘）
  上線日 −3 工作天起：搜集 STG PR → 更新 Jira Web/API 程式資訊 → 首筆轉狀態「已更新Staging」
      │
      ▼ 上線日 −1 工作天起
  搜集 PROD PR（open，簽核在 merge 前）→ 有 STG PR 的 repo 全到齊 → 嘗試轉 Jira 狀態「簽核中」（帶簽核主管）
      │
      ├─ 轉單成功 ──▶ 【通知②】Teams 產品頻道（Happy Path）
      │                   Adaptive Card @mention 簽核人
      │                   內容：PROD PR 就緒、Jira 上線單連結、各 repo PR 連結
      │
      ├─ 轉單失敗 ──▶ 【通知③】TPM 個人聊天（Exception）
      │                   Power Automate 發訊息：「{產品名}：需人工轉單」
      │                   → 排程每 30 分自動重試（通知③僅首次發送），可等自動重試或手動轉單
      │                   （超過上線日 +1 日曆天仍失敗 → 再發通知③一次並終止）
      │
      └─ 安全停損 ──▶ 上線日 +1 日曆天仍未蒐齊 PROD PR
                          → 【個人聊天告警】TPM 個人聊天，終止
```

### 程式／設定異動流程（需要改 code 或 config 時才用）

```
Claude 開分支 → push 異動 → 開 PR → TPM review → Approve → Merge
```

---

<a id="ch3"></a>

## 三、日常上線流程（每次上線時執行）

### 3.1 啟動流程

打開 Claude Cowork，直接說：

```
YYYY/MM/DD {產品名}              # 雲端（AWS）產品
YYYY/MM/DD {產品名} k8s          # 地端（K8s）產品，會一併建立主站 K8s 上線子單
YYYY/MM/DD {產品名} k8s dr       # 地端＋DR 站：主站與 DR 站子單一起建（每 repo 各一張）
```

Claude 會自動依序執行以下步驟。

> 若指令的雲端／地端型態與該產品設定（`hosting`）不符，Claude 會提醒你改用正確指令，不會建立任何單。

### 3.2 Claude 執行步驟說明

**Claude 自動完成（Steps 1–5 + 開 PR）：**

| Step | 動作 | 使用工具 |
|------|------|---------|
| Step 1 | 建立 Jira 上線單，設定 assignee、日期、Domain、Engineer、個資欄位（異動與個資讀寫相關） | Jira MCP |
| Step 2 | 建立 Jira Epic，summary = `{日期} 上線` | Jira MCP |
| Step 3 | 上線單 depends on Epic（Depends(WBSGantt) 連結） | Jira MCP |
| Step 4 | 上線單 description 插入 Epic JQL 工作項目列表 | Jira MCP |
| Step 4.5 | （限地端 hosting=k8s）建立 K8s 上線子單，每個部署 repo 一張 | Jira MCP |
| Step 5 | 更新 state.json，推分支並開 PR | GitHub MCP |

> Step 1 建單時，Claude 會先問你「本次上線**是否涉及個資讀寫**」，把答案填入上線單「異動與個資讀寫相關」欄（否／是）。**此欄未填，STG 階段將無法自動轉「STAGING測試中」**（該轉換的必填驗證）。

**PR merge 後由 GitHub Actions 自動觸發：**

| 時機 | 動作 |
|------|------|
| 立即 | Teams 上線頻道通知（通知①） |
| 每 30 分（窗口制） | 上線日 −3 工作天起搜 STG、−1 工作天起搜 PROD → 更新 Jira → 轉狀態 → 轉「簽核中」成功發簽核通知（通知②） |

### 3.3 你需要做的：PR 簽核

Claude 完成後會給你 PR 連結，執行以下步驟：

1. 點開 PR 連結
2. 點「**Files changed**」確認 state.json 內容
3. 點「**Review changes**」→「**Approve**」→「**Submit review**」
4. 點「**Merge pull request**」→「**Confirm merge**」
   > **不需重發通知①時**（如切換追蹤日期，先前已通知過）：點「Merge pull request」後，在 commit 訊息（標題或 Extended description 皆可）加入 `[skip ci]` 再 Confirm merge，通知① workflow 即不會執行（GitHub Actions 內建規則）；每 30 分的 PR 搜集排程不受影響。
5. 本地同步：

```bash
cd ~/Documents/{repo-name} && git checkout main && git pull origin main
```

Merge 完成後，GitHub Actions 自動觸發 Teams 上線頻道通知。

---

<a id="ch4"></a>

## 四、自動化後台（GitHub Actions）

### 4.1 Teams 通知情境總覽

| # | 通知名稱 | 觸發時機 | 發送對象 | 頻道 |
|---|---------|---------|---------|------|
| 1 | 上線準備通知 | state.json 的 `deploy_date`（上線日期）有變動並 push 至 main | 工程師（`talent_engineering_members`，工程師名單） | 上線作業頻道 |
| 2 | PROD PR 簽核通知 | 有 STG PR 的 repo 之 PROD PR 全找齊、上線單轉「簽核中」成功 | 簽核人（`approvers`，簽核人名單） | 產品頻道 |
| 3 | 人工轉單提醒 | PROD PR 全找到但自動轉「簽核中」失敗（僅首次失敗發送；重試至停損日仍失敗會再發一次並終止） | TPM 個人聊天 | TPM 個人聊天（Power Automate） |
| 4 | 安全停損告警 | 超過上線日 +1 日曆天（`today > 上線日+1`，即上線日 +2 起）仍未蒐齊 PROD PR | TPM 個人聊天 | TPM 個人聊天（Power Automate） |

### 4.2 notify-deploy.yml（上線準備通知）

- **觸發條件：** state.json 被 push 到 main，且（1）`deploy_date` 有變動，或（2）`pending_queue` 有新增條目
- **執行內容：** 發送 Adaptive Card 至上線作業頻道，內容包含上線日期、STG 日期、上線單連結，並列出工程師需在討論串留言的事項（STG PR / PROD PR / Develop PR / APIM / SRE / Rundown）
- **@mention：** `talent_engineering_members` 中已填 `aad_id` 的工程師
- **手動補發：** GitHub Actions → Notify Deploy Channel →「Run workflow」，可指定產品名稱（留空＝所有 active 產品）。手動觸發時，active 條目與所有 `pending_queue` 條目的通知都會一併發送。

**通知內容版型**（順序、項目符號皆以程式 `notify_deploy.py` 實際輸出為準）：

```
{上線日YYYYMMDD} {顯示名稱} 上線單          ← 標題（粗體）

預計 {上STG M/D} 上STG，{上線 M/D} {deploy_time} 上線
• 上線單：{Jira 連結}

以下內容請於討論串留言
1. STG PR 簽核
2. PROD PR
3. Develop PR
4. APIM 會簽
5. SRE 開單申請
6. Rundown

{@工程師們}
```

範例（職場力、上線日 20260707、10:00；上STG＝上線日 −2 工作天＝7/3）：

```
20260707 職場力 上線單

預計 7/3 上STG，7/7 10:00 上線
• 上線單：https://104corp.atlassian.net/browse/ITPMTALREQ-8686

以下內容請於討論串留言
1. STG PR 簽核
2. PROD PR
3. Develop PR
4. APIM 會簽
5. SRE 開單申請
6. Rundown

@Jimmy Chuang 莊泓桀 @James YJ Lin 林毓鈞 @Haining Hsu 許海寧
```

### 4.3 pr-collector.yml（PR 搜集、狀態轉換、簽核通知、人工提醒）

- **觸發條件：** 每 30 分鐘自動執行（UTC）；窗口外由腳本即刻結束
- **PR 搜集方式：** 以 PR **title** 為準——用「上線日期 token（`YYYYMMDD` 與 `YYMMDD`）」比對 title、再以 title 的 `stg`/`prod`（不分大小寫）分類，STG 只取**已 merged** 的 PR、PROD 取 **open** 的 PR（簽核在 merge 之前）。不依賴 head/base 分支名稱（各 repo 命名不一致）。工作日回推只跳週末（國定假日視為工作日）。
- **STG（上線日 −3 工作天起）：** 搜集各 repo 中 title 含上線日期 + `stg`/`[stg]` 且已 merged 的 PR，寫入 Jira Web/API 程式資訊欄位；**找到 STG PR 後即嘗試將上線單推進到「STAGING測試中」狀態，若當次未轉成功，之後每 30 分排程會持續重試直到成功**，**不發 Teams 通知**
- **PROD（上線日 −1 工作天起）：** 搜集 `prod`/`[prod]` PR（同樣 title 比對，取 **open**），**有 STG PR 的 repo** 全找齊對應 PROD PR 後嘗試轉「簽核中」（無 STG PR 的 repo 不列入、不等待；轉單自動帶簽核主管）：
  - **成功** → 發 PROD PR 簽核通知至產品頻道（通知②，@mention 簽核人），終止
  - **失敗** → 發人工轉單提醒至 TPM 個人聊天（**僅首次失敗發送**），保持追蹤、之後每 30 分自動重試（如 QA 尚未把單轉到「Staging測試完成」的時序問題會自癒）；**超過上線日 +1 日曆天仍失敗**才再發一次提醒並終止
- **安全停損：** PROD 一直沒找齊，且 `today > 上線日 +1 日曆天`（即上線日 +2 起）→ 發告警至 TPM 個人聊天，終止
- 終止收尾（轉單成功 `signed`／重試至停損日仍失敗 `manual`／安全停損 `timeout`）皆設 `status=completed`，之後排程跳過；STG 也隨之停止。轉「簽核中」失敗**當下不會**設 completed，會持續重試

**轉狀態邏輯：** 所有上線單共用同一個 ITPMTALREQ workflow（19_Requirement Wf V4.6），程式**依目的地狀態尋找 transition**（掃可用 transition，找 `to.name` 符合的執行），不綁 transition 名稱或 id：

| 目的地狀態 | 由哪個 transition 到達 | transition id | 狀態 id |
|---|---|---|---|
| STAGING測試中（STG，有 STG PR 後轉；未成功每輪重試） | 已更新Staging | 471 | 11359 |
| 簽核中（PROD，有 STG PR 的 repo 全找齊後轉；未成功每輪重試，通知③僅首次發送） | 申請上線 | 411 | 11606 |

> ⚠️ 「已更新Staging→STAGING測試中」有 workflow 必填驗證：上線單「異動與個資讀寫相關」(`customfield_12767`) 需先有值（於 Step 1 建單時填），否則 STG 轉單會失敗、雖每輪重試仍過不了。

> ⚠️ 「申請上線→簽核中」也有 workflow 必填驗證：**簽核主管**（`customfield_11300`）——程式轉單時自動帶入 config 該產品的 `jira_sign_manager_account_id`；此欄未設定會轉單失敗（400「請選擇簽核主管」）。

> 轉單韌性：單已在目標狀態 → 視為轉單成功（繼續後續通知）；STG 若單已被人工推進到「STAGING測試中」或之後的狀態 → 視為完成、不再重試；轉單失敗時 log 會印出 Jira 完整錯誤訊息（HTTP code＋回應內容），排查以此為準。

**Web/API 程式資訊欄位格式（`customfield_12324`）**

程式每次更新此欄位，都會依「目前累積的**全部** STG／PROD PR」**重畫整格**（非逐筆追加，所以舊資料也會一起用最新格式重寫）。URL 以純文字呈現（避免被 Jira 渲染成 `[url|url]`）；內容以 Jira 原生巢狀清單（ADF bulletList）呈現：同一 repo **單筆** → URL 接在名稱同一清單項目，**多筆** → 名稱一個項目、URL 逐筆為巢狀子項目（真縮排，檢視／編輯都保留），[STG PR]／[PROD PR] 兩區塊間僅一個空行。

範例（Resume API 多筆 STG、Rundeck 單筆；PROD 各一筆）：

```
[STG PR]
• Resume API：
    ◦ https://github.com/104corp/104-talent-resume-api/pull/58
    ◦ https://github.com/104corp/104-talent-resume-api/pull/60
• Rundeck：https://github.com/104corp/104-rundeck-talent-resume/pull/12

[PROD PR]
• Resume API：https://github.com/104corp/104-talent-resume-api/pull/64
• Rundeck：https://github.com/104corp/104-rundeck-talent-resume/pull/13
```

> 清單為 Jira 原生 bullet list（ADF bulletList），縮排不會因 wiki 樣式流失。

> 每個 repo 都會列出；本次無 PR 的 repo 仍會出現、僅剩標籤（如 `Rundeck：`）。

**通知②（PROD PR 簽核通知）版型**（只列已收齊 PROD PR 的 repo，每個一行；以程式 `collect_prs.py` 實際輸出為準）：

```
🚀 PROD PR 簽核通知｜{顯示名稱} {上線日YYYYMMDD}          ← 標題（粗體）

{@簽核人們} Hi all，{顯示名稱}本次上線所有 PROD PR 已就緒，請抽空簽核上線單與 PR，感謝

- 上線單：{Jira 連結}
- PR：
   - {repo 短標籤}：{PROD PR 連結}
```

範例（職場力 20260707；本次只有 Frontend 有 PROD PR）：

```
🚀 PROD PR 簽核通知｜職場力 20260707

@Ascii Huang 黃柏源 @AK Lee 李建寬 Hi all，職場力本次上線所有 PROD PR 已就緒，請抽空簽核上線單與 PR，感謝

- 上線單：https://104corp.atlassian.net/browse/ITPMTALREQ-8686
- PR：
   - Frontend：https://github.com/104corp/104-CMS-Frontend/pull/4298
```

### 4.4 多張上線單管理規則

`state.json` 以產品名為 key 管理各產品的上線追蹤。若同一產品同時有兩張（或多張）上線單，系統使用 **`pending_queue`** 暫存後續待啟用的條目：

1. **Claude 在 Step 5 自動判斷**：若該產品已有 active 條目，不覆蓋，改在現有條目的 `pending_queue` 陣列新增一筆（含 `deploy_date`、`stg_date`、`deploy_issue_key`、`deploy_issue_url`、`epic_key`）
2. **通知①同步發送**：PR merge 後，`notify_deploy.py` 會同時替 `pending_queue` 新增的條目發送上線準備通知（不只發 active 條目）
3. **自動啟用**：待前張單轉「簽核中」成功，`collect_prs.py` 自動將 `pending_queue` 第一筆提升為 active，**無需人工操作**

---

<a id="ch5"></a>

## 五、使用到的工具

| 工具 | 用途 | 安裝位置 |
|------|------|---------|
| **Claude Cowork** | 主要操作介面，執行上線流程 | Claude 桌面版 |
| **Jira MCP（Atlassian）** | 建立上線單、Epic、連結、更新欄位 | Cowork Plugins |
| **GitHub MCP** | 讀寫 config.json、state.json、開 PR | Cowork Plugins |
| **GitHub Actions** | 自動排程搜集 PR、發 Teams 通知 | {org}/{repo-name} |
| **Power Automate** | 接收 webhook 並發送 Teams Adaptive Card | Microsoft 365 |
| **VS Code** | 本地編輯程式、管理 git | 本機 |

---

<a id="ch6"></a>

## 六、前置作業（一次性設定）

### 6.1 首次建置（從 template 建立你的 repo）

> 只有第一次導入本系統時做一次；若 repo 已建好，跳過本節、直接看 6.2。

1. 取得 template：前往 https://github.com/reeni1111/104-release-automation，點「**Code**」→「**Download ZIP**」，解壓到本機（資料夾名可自訂）。
2. 在 GitHub 建一個新的 **private** repo（例：`your-org/your-release-workflow`）；建立時**不要**加任何檔案（不勾 README／.gitignore）。
3. 把 template 檔案推上去（Terminal，`{your-org}/{your-repo}` 換成你的）：

```bash
cd 104-release-workflow-template          # 進入解壓後的資料夾（含 config.json、scripts/、.github/）
git init
git add .
git commit -m "init from 104-release-workflow template"
git branch -M main
git remote add origin https://github.com/{your-org}/{your-repo}.git
git push -u origin main
```

4. 到 Repo → **Actions** 分頁：若頁面顯示「GitHub Actions is not enabled for this repository」或「I understand my workflows, go ahead and enable them」，點按鈕啟用。**未啟用則排程不會執行，通知不會送出。**
5. 接著依序做 6.2（Secrets）→ 6.3（本地 Git）→ 6.4（config.json）→ **6.5（安裝 Skill）**。**6.4 務必把 `github.owner` / `github.repo` 填成你這個 repo**，skill 才會操作到正確的位置。

> **Jira workflow 前提**：上線單所在 Jira 專案的 workflow 需具備「已更新Staging」（→STAGING測試中）與「申請上線」（→簽核中）等 transition——程式依**目的地狀態**尋找 transition，不綁 transition 名稱或 id。跨到其他 Jira 專案導入前請先確認，否則自動轉單會失敗。

### 6.2 GitHub Repo Secrets

**設定位置：**
👉 https://github.com/{org}/{repo-name}/settings/secrets/actions

**步驟：**
1. 開啟上方連結
2. 點「**New repository secret**」
3. 依序新增以下三個 Secret：

| Secret 名稱 | 說明 | 如何取得 |
|------------|------|---------|
| `GH_PAT` | GitHub Personal Access Token | 見下方 6.2.1 |
| `JIRA_EMAIL` | 你的 Jira 登入 Email | TPM 登入 Jira 的 email |
| `JIRA_API_TOKEN` | Jira API Token | 見下方 6.2.2 |

#### 6.2.1 取得 GitHub Personal Access Token（GH_PAT）

👉 https://github.com/settings/tokens

1. 點「**Generate new token**」→「**Generate new token (classic)**」
2. Note 建議填 Repo 名稱
3. Expiration 選「**No expiration**」或自訂
4. 勾選以下權限：
   - `repo`（全選）
   - `workflow`
5. 點「**Generate token**」→ 複製 token（只會顯示一次）
6. 回到 Secrets 頁面貼上

#### 6.2.2 取得 Jira API Token

👉 https://id.atlassian.com/manage-profile/security/api-tokens

1. 點「**建立 API 權杖（Create API token）**」
2. Name 建議填 Repo Name
3. 點「**建立（Create）**」→ 複製 token（只會顯示一次），到期日建議設置一年
4. 回到 Secrets 頁面貼上

### 6.3 本地 Git 環境

```bash
# Clone repo（只需做一次）
cd ~/Documents && git clone https://github.com/{org}/{repo-name}.git

# 確認在正確位置
cd ~/Documents/{repo-name} && git status
```

### 6.4 config.json 欄位確認

確認 `config.json` 中以下欄位已填入：

| 欄位 | 說明 |
|------|------|
| `github.owner` / `github.repo` | 本 repo 的 GitHub owner 與 repo 名稱；skill 靠這個決定要操作哪個 repo |
| `teams_deploy_channel_webhook_url` | 上線作業頻道 webhook（通知 1 用） |
| `teams_product_channel_webhook_url` | 產品頻道 webhook（通知 2 用） |
| `tpm.name` / `tpm.jira_account_id` / `tpm.email` / `tpm.teams_personal_webhook_url` | TPM 個人相關資訊，集中於 top-level `tpm` 區塊：`teams_personal_webhook_url` 供通知 3／安全停損告警；`jira_account_id` 供 K8s 子單指派；`email` 供個人聊天 webhook 設定 |
| `products.{產品名}.hosting` | 託管環境：`aws`（雲端）或 `k8s`（地端） |
| `products.{產品名}.k8s_subtickets` | （限地端 hosting=k8s）repo → ArgoCD app 名稱；上線時據此建立 K8s 子單 |
| `products.{產品名}.deploy_time` | 該產品上線時間（如 CMS＝10:00；未填預設 12:00）。用於排定上線日期與通知①文案；排定送Q時間固定 12:00 |
| `talent_engineering_members[].aad_id` | 每位工程師的 Teams AAD Object ID |
| `products.{產品名}.approvers[].aad_id` | 該產品簽核人 Teams AAD Object ID（per-product；外層 `approvers` 保留為空陣列 fallback） |
| `products.{產品名}.jira_sign_manager_account_id` | 該產品**簽核主管的 Jira accountId**（轉「簽核中」validator 必填；為 Jira 帳號 ID，非 AAD Object ID） |
| `products.{產品名}.jira_system_level` | 系統等級（開上線單「系統等級」欄用）；存選項 id，`13204` = 「1-2」，全站固定共用 |

#### 6.4.1 如何查詢成員的 Teams AAD Object ID

AAD Object ID 是 Microsoft 365 給每個使用者分配的唯一識別碼，格式如：`a1b2c3d4-e5f6-7890-abcd-ef1234567890`。

**Graph Explorer**

👉 https://developer.microsoft.com/en-us/graph/graph-explorer

1. 前往上方連結
2. 右上角用公司 M365 帳號登入
3. 在網址列輸入查詢語法，點「**Run query**」，回傳結果裡的 `id` 欄位即為 Object ID

**查自己：**

```
GET https://graph.microsoft.com/v1.0/me?$select=id,displayName,mail
```

**查同事（依 email，最穩定）：**

```
GET https://graph.microsoft.com/v1.0/users/{同事email}?$select=id,displayName,mail
```

例：`GET https://graph.microsoft.com/v1.0/users/jodie.lin@104.com.tw?$select=id,displayName,mail`

**查同事（依姓名，中文可能有編碼問題）：**

```
GET https://graph.microsoft.com/v1.0/users?$filter=startswith(displayName,'王')&$select=id,displayName,mail
```

**查完之後：**

把所有需要 @mention 的人員（工程師、簽核人）的 Object ID 整理後告知系統管理者，填入 config.json 對應的 `aad_id` 欄位。

#### 6.4.2 如何取得頻道 Incoming Webhook URL

Teams 頻道原生支援 Incoming Webhook，不需要 Power Automate。此步驟需對**上線作業頻道**與**產品頻道**各執行一次，分別取得對應的 URL。

**設定步驟：**

1. 在目標頻道名稱旁點「**...**」→ 選「**工作流程**」
2. 搜尋「**將 webhook 警示傳送到頻道**」 → 點「**+ 從頭開始建立**」
3. 預設參數不需修改，點「**儲存**」
4. 點「**複製 Webhook 連結**」 → 填入 config.json 對應欄位
   - 上線作業頻道 → `teams_deploy_channel_webhook_url`
   - 產品頻道 → `teams_product_channel_webhook_url`

#### 6.4.3 如何設定個人聊天 Webhook（人工轉單提醒用）

Teams 個人聊天不支援直接 webhook，需透過 Power Automate 建立中轉流程。

👉 https://make.powerautomate.com

**建立流程：**

1. 點左上角「**+ 建立**」→ 選「**即時雲端流程**」
2. 流程名稱填 `notify-manual-transition`
3. 觸發器搜尋並選「**當收到 HTTP 要求時**」→ 點「**建立**」

**新增 Teams 步驟：**

1. 畫面中央點「**+**」→ 右側出現「新增動作」面板
2. 在面板下方「依連接器」找到「**Microsoft Teams**」→ 點進去
3. 找到「**在聊天室或管道中張貼訊息**」（Post a message in a chat or channel）→ 點選 → 切換到「**在聊天室或管道中張貼訊息**」節點

**設定欄位：**

7. **張貼為（Post as）**：選「**流程機器人（Flow bot）**」
8. **張貼於（Post in）**：選「**與流程機器人聊天（Chat with Flow bot）**」
9. **Recipient**：填 TPM 的 email（即 `config.json` 的 `tpm.email`）
10. **訊息（Message**）：先點選訊息輸入框，再點選旁的 fx 圖示（插入運算式）→ 上半部輸入框填入 `triggerBody()?['message'] `

**取得 URL：**

11. 右上角點「**儲存**」
12. 點畫面上方的觸發器卡片「**manual**」
13. 誰可以觸發流程選擇「**任何人**」
14. 複製「**HTTP URL**」→ 填入 Github config.json 的 `tpm.teams_personal_webhook_url`

### 6.5 安裝 Skill 到 Cowork [AI 補充]

> 只有第一次導入時做一次；更新 skill 版本時重複本節即可。

1. 取得 `104-release-automation-clean.skill` 檔（向系統維護者索取，或從 template repo 下載釋出版本）。
2. 打開 **Claude 桌面版**。
3. 進入 **Settings**（左下角齒輪圖示）→ 選 **Cowork** → 選 **Skills**。
4. 點「**Install from file…**」，選取 `104-release-automation-clean.skill`。
5. 安裝完成後，Skills 清單中會出現「**104-release-workflow**」。

> ⚠️ Skill 需在**有 Jira MCP（Atlassian）與 GitHub MCP** 的 Cowork 環境下才能正常運作。請確認這兩個 MCP 已安裝並已授權（Settings → Cowork → Plugins）。

### 6.6 第一次使用驗證 [AI 補充]

設定完成後，在 Claude Cowork 輸入下列指令確認系統正常運作：

```
YYYY/MM/DD {產品名}
```

（例：`2026/9/1 農場`）

Skill 觸發後需要知道你的 GitHub repo 位置（`{owner}/{repo}`）——這是因為 GitHub MCP 可存取多個 repo，Skill 需要先知道要去哪個 repo 讀取 `config.json`。告知一次後，後續步驟全自動執行；同一次對話內不需再次提供。

> 若 Skill 沒有自動觸發：確認 Settings → Cowork → Skills → 104-release-workflow 是否已啟用（toggle 開啟）。

---

<a id="ch7"></a>

## 七、新增產品 完整 Checklist

逐項確認以下設定與跨團隊前置作業。
依據：`config.json`＋ `scripts/notify_deploy.py`／`scripts/collect_prs.py`＋ Steps 1–5。

**頻道 / 人員（基本項）**

- [ ] 上線作業頻道 webhook URL → `teams_deploy_channel_webhook_url`（通知①用）
- [ ] 產品頻道 webhook URL → `teams_product_channel_webhook_url`（通知②用）
- [ ] 需 @mention 的工程師 AAD Object ID → `talent_engineering_members[].aad_id`
- [ ] 需 @mention 的主管／簽核人 AAD Object ID → `approvers[].aad_id`（per-product；外層 `approvers: []` 為 fallback）
- [ ] 所有 repo → `repos[]`
- [ ] `hosting`：`aws`（雲端）或 `k8s`（地端）；地端另填 `k8s_subtickets`（repo → ArgoCD app 名稱，如 `blog-web`）
- [ ] 上線時間 → `deploy_time`（如 CMS＝10:00；未填預設 12:00）
- [ ] 簽核主管 Jira accountId → `jira_sign_manager_account_id`（轉「簽核中」validator 必填；為 Jira 帳號 ID，非 AAD） 
- [ ] 系統等級 → `jira_system_level`（開上線單「系統等級」欄；存選項 id，`13204` = 1-2，全站固定共用）

**A. Jira 設定群組**

新增產品時不需逐項查 customfield，只要提供：
1. 一張該產品**現行上線單** URL（`.../browse/ITPMTALREQ-XXXX`）
2. 一張該產品**現行 Epic** URL（`.../browse/CMS-XXXX`）
3. 產品**觸發關鍵字**（如「農場」）與**顯示名稱**（如「關鍵人才庫」）

**B. GitHub / Repo**

- [ ] `repo_display_names`：每個 repo 的短標籤，顯示於 Jira 欄位與 Teams 通知
- [ ] **GH_PAT 權限**：現有 `GH_PAT` secret 必須能讀取新產品的**所有 repo**（尤其跨 org 或私有 repo），否則 PR 搜集抓不到任何 PR
- [ ] **跨團隊協議**：PR 搜集**不依賴分支命名**，改看 PR title——新產品工程師的 STG/PROD PR **title 必須含上線日期（YYYYMMDD 或 YYMMDD）與 `stg`/`[stg]` 或 `prod`/`[prod]`（不分大小寫）**；STG PR 須**已 merged**，PROD PR 為 **open**（簽核在 merge 之前）

**C. 可沿用 / 共用的項目（新增產品時通常不用改）**

- [ ] `tpm`（`name` / `jira_account_id` / `email` / `teams_personal_webhook_url`）：TPM 個人資訊集中於 top-level `tpm` 區塊，**跨產品共用**。除非換不同 TPM 負責，否則沿用即可
- [ ] `jira.cloud_id` / `jira.base_url`：同一 Jira cloud 共用
- [ ] Step 4 的 datasource ID：內部固定值，同 Jira cloud 共用（建議建完第一張單目視確認表格有正常渲染）

---

<a id="ch8"></a>

## 八、config.json 維護

### 更新 webhook URL 或 aad_id

由 Claude 代為更新：直接告訴 Claude 要改的欄位與值，Claude 會開 PR 讓你確認後 merge。

### 新增產品

在 `config.json` 的 `products` 下新增一個產品 entry，參考現有產品的結構填入：

```json
"{產品名}": {
  "display_name": "...",
  "hosting": "aws",
  "deploy_time": "12:00",
  "jira_deploy_project": "...",
  "jira_deploy_issue_type_id": "...",
  "jira_product_project": "...",
  "jira_epic_type_id": "...",
  "jira_web_api_field": "...",
  "jira_q_date_field": "...",
  "jira_deploy_date_field": "...",
  "jira_domain_field": "...",
  "jira_domain_parent_id": "...",
  "jira_domain_child_id": "...",
  "jira_assignee_account_id": "...",
  "jira_engineer_field": "...",
  "jira_sign_manager_account_id": "...",
  "jira_system_level": "13204",
  "repos": [...],
  "repo_display_names": {...},
  "k8s_subtickets": {...},
  "teams_deploy_channel_webhook_url": "...",
  "teams_product_channel_webhook_url": "...",
  "talent_engineering_members": [...],
  "approvers": [...]
}
```

> `hosting` ＝ `aws`（雲端）或 `k8s`（地端）；`k8s_subtickets` 僅地端產品需要（雲端產品請移除該欄位）。

> `jira_system_level`：系統等級選項 id（現值一律 `13204` = 1-2），所有產品必填。（選填）`jira_deploy_summary_template`：自訂上線單標題樣板（tokens `{deploy_date}`=YYYY/MM/DD、`{deploy_M_D}`=M/D、`{display_name}`）；未設用預設 `{display_name} - {deploy_M_D} 維運上線`。

---

<a id="ch9"></a>

## 九、Git 協作規範

### 9.1 角色分工

| 角色 | 負責 |
|:------:|------|
| AI | 建立分支、push 檔案、開 PR |
| TPM | review diff、Approve、Merge PR |

### 9.2 分支命名

| 類型 | 格式 | 範例 |
|------|------|------|
| 上線部署 | `chore/activate-{產品}-{日期}` | `chore/activate-{產品}-20260716` |
| 設定更新 | `config/簡述` | `config/update-aad-ids` |
| 修 bug | `fix/簡述` | `fix/notify-deploy-logic` |
| 新功能 | `feat/簡述` | `feat/add-product-b` |

### 9.3 你自行修改檔案的流程

**Terminal：**

```bash
# 1. 同步 main
cd ~/Documents/{repo-name} && git checkout main && git pull origin main

# 2. 建立分支
git checkout -b config/update-webhook-url

# 3. 修改後 stage 並 commit
git add config.json
git commit -m "config: update webhook url"

# 4. 推到遠端
git push origin config/update-webhook-url
```

**VS Code（不用 Terminal）：**

1. 用 VS Code 開啟 `~/Documents/{repo-name}`
2. 左下角點目前分支名稱 → 選「**Create new branch**」→ 輸入分支名稱 → Enter
3. 修改檔案
4. 左側點「**Source Control**」→ 在「Changes」點「**+**」stage 檔案
5. 輸入 commit 說明 → 點「**✓ Commit**」
6. 點「**Publish Branch**」推到 GitHub

推完後到 GitHub 開 PR。

### 9.4 常用 git 指令

| 動作 | 指令 |
|------|------|
| 切到 main 並同步 | `cd ~/Documents/{repo-name} && git checkout main && git pull origin main` |
| 建立新分支 | `git checkout -b 分支名稱` |
| 查看目前狀態 | `cd ~/Documents/{repo-name} && git status` |
| Stage 特定檔案 | `git add config.json` |
| Stage 所有修改 | `git add .` |
| Commit | `git commit -m "說明"` |
| Push 分支 | `git push origin 分支名稱` |
| 查看所有分支 | `git branch -a` |
| 刪除本地分支 | `git branch -d 分支名稱` |
| 對齊遠端（捨棄本地差異） | `cd ~/Documents/{repo-name} && git fetch origin && git reset --hard origin/main` |

---

<a id="ch10"></a>

## 十、常見問題

### Q：Teams 上線頻道沒收到通知？

1. 確認 state.json 的 `deploy_date` 是否有更新（去 GitHub 確認 commit 記錄）
2. 去 GitHub Actions → Notify Deploy Channel → 查看最新一次 run 的 log
3. 若 log 顯示 `WARN: webhook URL not set`，表示 config.json 的 webhook URL 未填
4. 需要補發時：GitHub Actions → Notify Deploy Channel →「Run workflow」，可指定產品名稱（留空＝所有 active 產品）

### Q：PROD PR 找齊了，但沒收到簽核通知（通知②）？

1. 去 GitHub Actions → **PR Collector & Deploy Notifier** → 看最新 run 的「Run PR collector」log
2. log 印「`❌ 轉單失敗: HTTP 400 …`」→ 依 Jira 錯誤訊息處理（如「請選擇簽核主管」＝ config 的 `jira_sign_manager_account_id` 未設定）；log 印「`⚠️ 找不到可轉到「簽核中」的 transition`」＝ 上線單還停在較早狀態（例如 QA 尚未轉「Staging測試完成」）。轉單失敗會發通知③到 TPM 個人聊天（僅首次），之後每 30 分自動重試；超過上線日 +1 日曆天仍失敗才終止
3. log 印「`❌ Teams signing notification failed`」→ 產品頻道 webhook 有問題；此情況下一輪排程會自動重試
4. 確認 state.json 該產品 `status` 是否已是 `completed`（completed 後排程不再處理該產品）

### Q：本地 git 跟遠端衝突？

```bash
cd ~/Documents/{repo-name} && git fetch origin && git reset --hard origin/main
```

> ⚠️ 此指令會捨棄本地未 push 的修改，確認不需要再執行。

### Q：同一產品有兩張上線單怎麼辦？

直接告訴 Claude 建立第二張上線單即可。若該產品 state.json 已有 active 條目，Claude 會自動將第二張加入 `pending_queue`，不覆蓋現有追蹤。

- 前張上線單轉「簽核中」成功後，系統自動啟用 `pending_queue` 的下一筆，無需手動操作
- PR merge 後，通知①也會同時替 `pending_queue` 的條目發送
- 手動補發（Run workflow）時，active 與所有 `pending_queue` 條目的通知都會一併發送

### Q：PR 簽核後本地如何同步？

```bash
cd ~/Documents/{repo-name} && git checkout main && git pull origin main
```
