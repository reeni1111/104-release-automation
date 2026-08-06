#!/usr/bin/env python3
"""
notify_deploy.py

偵測 state.json 新增的部署，發送 Teams 上線作業頻道通知。
- push 觸發：deploy_date 有變動的產品才發送
- workflow_dispatch 觸發：發送 NOTIFY_PRODUCT 指定的產品，若未指定則發送所有 active 產品
"""

import json
import os
import subprocess
import urllib.request
import urllib.error
import ssl
import sys


def get_previous_state():
    result = subprocess.run(
        ['git', 'show', 'HEAD~1:state.json'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return json.loads(result.stdout)
    return {"active_deployments": {}}


def send_teams_notification(webhook_url, payload):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        return resp.status


def build_payload(product_name, deployment, product_config):
    deploy_date  = deployment['deploy_date']
    stg_date     = deployment['stg_date']
    deploy_M_D   = f"{int(deploy_date[4:6])}/{int(deploy_date[6:8])}"
    stg_M_D      = f"{int(stg_date[4:6])}/{int(stg_date[6:8])}"
    jira_url     = deployment['deploy_issue_url']
    display_name = product_config['display_name']
    deploy_time  = product_config.get('deploy_time', '12:00')

    members = product_config.get('talent_engineering_members', [])
    valid_members = [m for m in members if m.get('aad_id', 'FILL_IN') != 'FILL_IN']

    entities = [
        {
            "type": "mention",
            "text": f"<at>{m['name']}</at>",
            "mentioned": {"id": m['aad_id'], "name": m['name']}
        }
        for m in valid_members
    ]
    mention_str = " ".join(f"<at>{m['name']}</at>" for m in valid_members)
    if not mention_str:
        mention_str = "(成員 AAD ID 尚未設定)"

    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.2",
                "msteams": {"entities": entities},
                "body": [
                    {
                        "type": "TextBlock",
                        "text": f"{deploy_date} {display_name} 上線單",
                        "weight": "Bolder",
                        "size": "Medium"
                    },
                    {
                        "type": "TextBlock",
                        "text": (
                            f"預計 {stg_M_D} 上STG，{deploy_M_D} {deploy_time} 上線\n"
                            f"• 上線單：{jira_url}"
                        ),
                        "wrap": True
                    },
                    {
                        "type": "TextBlock",
                        "text": (
                            "以下內容請於討論串留言\n"
                            "1. STG PR 簽核\n"
                            "2. PROD PR\n"
                            "3. Develop PR\n"
                            "4. APIM 會簽\n"
                            "5. SRE 開單申請\n"
                            "6. Rundown"
                        ),
                        "wrap": True
                    },
                    {
                        "type": "TextBlock",
                        "text": mention_str,
                        "wrap": True
                    }
                ]
            }
        }]
    }


def main():
    with open('config.json', encoding='utf-8') as f:
        config = json.load(f)
    with open('state.json', encoding='utf-8') as f:
        new_state = json.load(f)

    event_name     = os.environ.get('GITHUB_EVENT_NAME', 'push')
    notify_product = os.environ.get('NOTIFY_PRODUCT', '').strip()

    if event_name == 'workflow_dispatch':
        # 手動觸發：指定產品 or 全部 active 產品
        active = new_state.get('active_deployments', {})
        if notify_product and notify_product in active:
            to_notify = [notify_product]
        elif notify_product:
            print(f"WARN: '{notify_product}' not in active_deployments, sending all.")
            to_notify = list(active.keys())
        else:
            to_notify = list(active.keys())
        print(f"[workflow_dispatch] Notifying: {to_notify}")
    else:
        # push 觸發：deploy_date 有變動的產品才發送
        old_state = get_previous_state()
        old_deployments = old_state.get('active_deployments', {})
        new_deployments = new_state.get('active_deployments', {})
        to_notify = [
            p for p in new_deployments
            if new_deployments[p]['deploy_date'] != old_deployments.get(p, {}).get('deploy_date')
        ]
        if not to_notify:
            print("No deploy_date changes detected, nothing to notify.")
            return

    errors = []
    for product_name in to_notify:
        print(f"[{product_name}] Sending notification...")
        deployment     = new_state['active_deployments'][product_name]
        product_config = config['products'].get(product_name)

        if not product_config:
            print(f"[{product_name}] WARN: not found in config.json, skipping.")
            continue

        webhook_url = product_config.get('teams_deploy_channel_webhook_url', '')
        if 'FILL_IN' in webhook_url or not webhook_url:
            print(f"[{product_name}] WARN: webhook URL not set, skipping.")
            continue

        payload = build_payload(product_name, deployment, product_config)
        try:
            status = send_teams_notification(webhook_url, payload)
            print(f"[{product_name}] OK: HTTP {status}")
        except Exception as e:
            msg = f"[{product_name}] ERROR: {e}"
            print(msg, file=sys.stderr)
            errors.append(msg)

    if errors:
        sys.exit(1)


if __name__ == '__main__':
    main()
