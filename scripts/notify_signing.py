#!/usr/bin/env python3
"""
notify_signing.py

手動補發「PROD PR 簽核通知」（通知②）到產品頻道並 @mention 簽核人。
設計：
- 強制指定單一產品（環境變數 NOTIFY_PRODUCT）；不支援一次發送全部。
- 防呆（可用 FORCE=true 覆蓋）：
  * 未指定產品            → 直接失敗
  * 產品不在 state.json   → 直接失敗
  * webhook 未設定        → 直接失敗
  * status == completed   → 預設不發（需 FORCE）
  * 尚未收集到任何 PROD PR → 預設不發（需 FORCE）
訊息內容與 collect_prs.py 的 send_signing_notification 一致（改用 stdlib urllib，免裝 requests）。
webhook / 簽核人 / PR 皆讀自 config.json、state.json，不需任何 secret。
"""

import json
import os
import sys
import urllib.request


def post_card(webhook_url: str, title: str, body: str, entities: list) -> int:
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
                    {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium"},
                    {"type": "TextBlock", "text": body, "wrap": True},
                ],
            },
        }],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def main():
    force = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")
    product_name = os.environ.get("NOTIFY_PRODUCT", "").strip()

    if not product_name:
        print("ERROR: 必須指定單一產品名稱（NOTIFY_PRODUCT）；本工具不支援一次發送全部。", file=sys.stderr)
        sys.exit(1)

    with open("config.json", encoding="utf-8") as f:
        config = json.load(f)
    with open("state.json", encoding="utf-8") as f:
        state = json.load(f)

    active = state.get("active_deployments", {})
    if product_name not in active:
        print(f"ERROR: '{product_name}' 不在 state.json 的 active_deployments。", file=sys.stderr)
        print(f"       目前有：{list(active.keys())}", file=sys.stderr)
        sys.exit(1)

    product_config = config.get("products", {}).get(product_name)
    if not product_config:
        print(f"ERROR: '{product_name}' 不在 config.json 的 products。", file=sys.stderr)
        sys.exit(1)

    webhook_url = product_config.get("teams_product_channel_webhook_url", "")
    if not webhook_url or "FILL_IN" in webhook_url:
        print(f"ERROR: '{product_name}' 的 teams_product_channel_webhook_url 未設定。", file=sys.stderr)
        sys.exit(1)

    deployment = active[product_name]
    prod_prs = deployment.get("prod_prs", {})

    # ── 防呆（FORCE=true 可覆蓋）──
    reasons = []
    if deployment.get("status") == "completed":
        reasons.append(f"status 已是 completed（stopped_reason={deployment.get('stopped_reason')}）")
    if not any(prod_prs.values()):
        reasons.append("尚未收集到任何 PROD PR（可能還沒到簽核階段）")
    if reasons and not force:
        print(f"⛔ 未發送 '{product_name}'：" + "；".join(reasons), file=sys.stderr)
        print("   若確定要補發，請以 FORCE=true 再跑一次。", file=sys.stderr)
        sys.exit(1)
    if reasons and force:
        print(f"⚠️  FORCE 覆蓋防呆（{'；'.join(reasons)}），仍發送。")

    # ── 組訊息（與 send_signing_notification 一致）──
    jira_url     = deployment["deploy_issue_url"]
    repo_names   = product_config["repo_display_names"]
    approvers    = product_config.get("approvers", config.get("approvers", []))
    display_name = product_config["display_name"]
    deploy_date  = deployment["deploy_date"]

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
    title = f"🚀 PROD PR 簽核通知｜{display_name} {deploy_date}"

    try:
        status = post_card(webhook_url, title, body, entities)
        print(f"[{product_name}] 通知② 已發送：HTTP {status}")
    except Exception as e:
        print(f"[{product_name}] 通知② 發送失敗：{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
