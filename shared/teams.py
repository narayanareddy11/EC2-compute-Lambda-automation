
import json
from urllib.request import Request, urlopen

def post_to_teams(webhook_url: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    req = Request(webhook_url, data=body, headers={"Content-Type": "application/json"})
    with urlopen(req) as resp:
        return {"ok": True, "status": resp.status}

def simple_card(title, message):
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium"},
                    {"type": "TextBlock", "text": message, "wrap": True}
                ]
            }
        }]
    }
