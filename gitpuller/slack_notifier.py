"""Slack incoming-webhook notifier for git-pull failures."""

import os
import requests
from typing import Optional, Dict, Any


class SlackNotifier:
    """Posts formatted failure messages to a Slack incoming webhook."""

    def __init__(self, webhook_url: Optional[str] = None):
        # Explicit arg wins; otherwise fall back to the shared env var.
        self.webhook_url = webhook_url or os.environ.get("CDM_SLACK_WEBHOOK_URL")
        if not self.webhook_url:
            raise ValueError(
                "Slack webhook URL is required. "
                "Provide it as parameter or set CDM_SLACK_WEBHOOK_URL environment variable."
            )

    def create_failure_payload(self, repo_name: str, error_output: str) -> Dict[str, Any]:
        """Build the Slack Block Kit payload describing a pull failure."""
        payload = {
            # "text" is the notification/fallback shown in previews.
            "text": "Automate Git Pull Pipeline Failed",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": ":alert: Automate Git Pull Pipeline Failed :alert:"
                    }
                },
                {
                    "type": "divider"
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Repository:* `{repo_name}`"
                    }
                }
            ]
        }

        # Append the captured git error as a code block when present.
        if error_output:
            payload["blocks"].append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    # Cap length so we stay within Slack's 3000-char block limit.
                    "text": f"*Error Output:*\n```{error_output[:1500]}```"
                }
            })

        return payload

    def send_alert(self, repo_name: str, error_output: str = "") -> bool:
        """Send the failure alert. Returns True on success, False on any error
        (delivery failures are logged but never raised, so alerting can't mask
        the original git error)."""
        try:
            payload = self.create_failure_payload(repo_name, error_output)
            response = requests.post(self.webhook_url, json=payload, timeout=10)
            response.raise_for_status()
            print("✅ Alert sent to Slack")
            return True
        except Exception as e:
            print(f"⚠️ Failed to send Slack alert: {e}")
            return False