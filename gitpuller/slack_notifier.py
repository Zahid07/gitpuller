import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
SLACK_CONVERSATIONS_HISTORY_URL = "https://slack.com/api/conversations.history"
DEFAULT_SLACK_CHANNEL = "C05MLHR55JT"

# Unified across every Mage deployment — do not prefix with MAGE_WORKSPACE_NAME.
# One hash per channel per day; field = gitpuller workspace, value = Slack thread_ts.
REDIS_THREAD_KEY_PREFIX = "gitpuller:slack_thread"
REDIS_CREATE_LOCK_TTL_SECONDS = 120
REDIS_ERROR_MAX_CHARS = 1500
REDIS_ERRORS_PER_WORKSPACE = 20

_redis_client = None
_redis_init_attempted = False


def _get_redis_client():
    """Mage io_config.yaml first, then REDIS_HOST / REDIS_PORT / REDIS_PASSWORD."""
    global _redis_client, _redis_init_attempted
    if _redis_client is not None:
        return _redis_client
    if _redis_init_attempted:
        return None
    _redis_init_attempted = True

    try:
        import redis as redis_lib
    except ImportError:
        print("⚠️ redis package not installed; Slack thread_ts will not persist across pods")
        return None

    host = None
    password = None
    port = 6379
    try:
        from mage_ai.io.config import ConfigFileLoader
        from mage_ai.settings.repo import get_repo_path

        loader = ConfigFileLoader(os.path.join(get_repo_path(), "io_config.yaml"), "default")
        host = loader["REDIS_HOST"]
        password = loader["REDIS_PASSWORD"] or None
        port = int(loader["REDIS_PORT"])
    except Exception:
        host = None

    if not host:
        host = (os.environ.get("REDIS_HOST") or "").strip() or None
        password = (os.environ.get("REDIS_PASSWORD") or "").strip() or None
        port = int(os.environ.get("REDIS_PORT") or 6379)

    if not host:
        print("⚠️ Redis is not configured; Slack thread_ts will not persist across pods")
        return None

    try:
        _redis_client = redis_lib.Redis(
            host=host,
            password=password or None,
            port=port,
            db=0,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        _redis_client.ping()
        return _redis_client
    except Exception as exc:
        print(f"⚠️ Redis connection failed: {exc}")
        _redis_client = None
        _redis_init_attempted = False
        return None


class SlackNotifier:
    """Posts formatted failure messages to Slack.

    Bot-token path (preferred): one parent message per workspace per calendar
    day, each failure as a thread reply. Daily ``thread_ts`` values live in one
    unified Redis hash (all Mage workspaces share it; TTL until midnight).
    Webhook path: a standalone channel post.
    """

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        bot_token: Optional[str] = None,
        channel: Optional[str] = None,
    ):
        self.bot_token = (bot_token or os.environ.get("CDM_PROD_SLACK_BOT_TOKEN") or "").strip() or None
        self.channel = (
            (channel or os.environ.get("CDM_PROD_DB_SLACK_CHANNEL") or "").strip()
            or DEFAULT_SLACK_CHANNEL
        )
        self.webhook_url = (webhook_url or os.environ.get("CDM_SLACK_WEBHOOK_URL") or "").strip() or None
        self._thread_ts_cache: Dict[str, str] = {}

        if not self.bot_token and not self.webhook_url:
            print(
                "⚠️ Slack not configured; skipping alerts "
                "(set CDM_PROD_SLACK_BOT_TOKEN, or CDM_SLACK_WEBHOOK_URL as fallback)"
            )
        elif not self.bot_token:
            print(
                "⚠️ CDM_PROD_SLACK_BOT_TOKEN is not set; using webhook "
                "(alerts will not be threaded)"
            )

    @staticmethod
    def _is_paused() -> bool:
        flag = os.environ.get("CDM_PAUSE_SLACK_MESSAGES", "0").lower()
        return flag in ("1", "true", "yes")

    @staticmethod
    def _thread_tz() -> str:
        return os.environ.get("CDM_SLACK_THREAD_TZ", "UTC")

    def _today_date_key(self) -> str:
        return datetime.now(ZoneInfo(self._thread_tz())).strftime("%Y-%m-%d")

    def _seconds_until_end_of_day(self) -> int:
        tz = ZoneInfo(self._thread_tz())
        now = datetime.now(tz)
        start_next_day = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return max(1, int((start_next_day - now).total_seconds()))

    @staticmethod
    def _workspace_label(workspace_name: Optional[str] = None) -> str:
        return (
            (workspace_name or "").strip()
            or (os.environ.get("MAGE_WORKSPACE_NAME") or "").strip()
            or "unknown"
        )

    def _cache_key(self, date_key: str, workspace: str) -> str:
        return f"{date_key}:{workspace}"

    def _parent_text(self, date_key: str, workspace: str) -> str:
        return f"Git Pull Failures {workspace} — daily thread ({date_key})"

    def _parent_header(self, date_key: str, workspace: str) -> str:
        return f"🚨 Git Pull Failures {workspace} — {date_key}"

    def _redis_daily_key(self, date_key: str) -> str:
        """One hash for every gitpuller workspace on this channel today."""
        return f"{REDIS_THREAD_KEY_PREFIX}:{self.channel}:{date_key}"

    def _redis_create_lock_key(self, date_key: str, workspace: str) -> str:
        return f"{self._redis_daily_key(date_key)}:{workspace}:creating"

    def _remember_thread_ts(self, date_key: str, workspace: str, thread_ts: str) -> None:
        self._thread_ts_cache[self._cache_key(date_key, workspace)] = thread_ts

    @staticmethod
    def _parse_workspace_record(raw: Any) -> Dict[str, Any]:
        """Accept JSON records and the legacy plain thread_ts string."""
        empty: Dict[str, Any] = {"workspace": None, "thread_ts": None, "errors": []}
        if raw is None:
            return empty
        if isinstance(raw, dict):
            data = raw
        else:
            text = str(raw).strip()
            if not text:
                return empty
            if text.startswith("{"):
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return {"workspace": None, "thread_ts": text, "errors": []}
            else:
                return {"workspace": None, "thread_ts": text, "errors": []}
        if not isinstance(data, dict):
            return empty
        errors = data.get("errors") or []
        if not isinstance(errors, list):
            errors = []
        thread_ts = data.get("thread_ts")
        workspace_name = data.get("workspace")
        return {
            "workspace": str(workspace_name).strip() if workspace_name else None,
            "thread_ts": str(thread_ts).strip() if thread_ts else None,
            "errors": errors,
        }

    def _load_workspace_record(self, date_key: str, workspace: str) -> Dict[str, Any]:
        client = _get_redis_client()
        if not client:
            return {"workspace": workspace, "thread_ts": None, "errors": []}
        redis_key = self._redis_daily_key(date_key)
        try:
            raw = client.hget(redis_key, workspace)
        except Exception as exc:
            print(f"⚠️ Redis HGET failed for {redis_key} field={workspace}: {exc}")
            return {"workspace": workspace, "thread_ts": None, "errors": []}
        record = self._parse_workspace_record(raw)
        record["workspace"] = record.get("workspace") or workspace
        return record

    def _save_workspace_record(
        self,
        date_key: str,
        workspace: str,
        record: Dict[str, Any],
        log_message: Optional[str] = None,
    ) -> None:
        client = _get_redis_client()
        if not client:
            return
        redis_key = self._redis_daily_key(date_key)
        ttl_seconds = self._seconds_until_end_of_day()
        record = dict(record)
        record["workspace"] = workspace
        payload = json.dumps(record, ensure_ascii=False)
        try:
            pipe = client.pipeline()
            pipe.hset(redis_key, workspace, payload)
            pipe.expire(redis_key, ttl_seconds)
            pipe.execute()
            print(
                log_message
                or (
                    f"✅ Saved gitpuller Slack record to Redis "
                    f"(key={redis_key}, workspace={workspace}, ttl={ttl_seconds}s)"
                )
            )
        except Exception as exc:
            print(f"⚠️ Redis HSET failed for {redis_key} field={workspace}: {exc}")

    def _read_cached_thread_ts(self, date_key: str, workspace: str) -> Optional[str]:
        cached = self._thread_ts_cache.get(self._cache_key(date_key, workspace))
        if cached:
            return cached

        record = self._load_workspace_record(date_key, workspace)
        thread_ts = record.get("thread_ts")
        if thread_ts:
            self._remember_thread_ts(date_key, workspace, thread_ts)
            redis_key = self._redis_daily_key(date_key)
            print(
                f"ℹ️ Loaded daily Slack thread from Redis "
                f"(key={redis_key}, workspace={workspace}, ts={thread_ts})"
            )
        return thread_ts or None

    def _write_cached_thread_ts(self, date_key: str, workspace: str, thread_ts: str) -> None:
        self._remember_thread_ts(date_key, workspace, thread_ts)
        record = self._load_workspace_record(date_key, workspace)
        record["workspace"] = workspace
        record["thread_ts"] = thread_ts
        redis_key = self._redis_daily_key(date_key)
        ttl_seconds = self._seconds_until_end_of_day()
        self._save_workspace_record(
            date_key,
            workspace,
            record,
            log_message=(
                f"✅ Saved daily Slack thread to Redis "
                f"(key={redis_key}, workspace={workspace}, ts={thread_ts}, ttl={ttl_seconds}s)"
            ),
        )

    def _append_redis_error(
        self,
        workspace: str,
        repo_name: str,
        error_output: str,
        thread_ts: Optional[str] = None,
        reply_ts: Optional[str] = None,
    ) -> None:
        if not _get_redis_client():
            return
        date_key = self._today_date_key()
        if thread_ts:
            self._remember_thread_ts(date_key, workspace, thread_ts)
        record = self._load_workspace_record(date_key, workspace)
        record["workspace"] = workspace
        if thread_ts:
            record["thread_ts"] = thread_ts
        error_entry = {
            "workspace": workspace,
            "repo": repo_name,
            "error": (error_output or "")[:REDIS_ERROR_MAX_CHARS],
            "at": datetime.now(ZoneInfo(self._thread_tz())).strftime("%Y-%m-%d %H:%M:%S %Z"),
        }
        if reply_ts:
            error_entry["reply_ts"] = reply_ts
        errors = record.get("errors") or []
        errors.append(error_entry)
        record["errors"] = errors[-REDIS_ERRORS_PER_WORKSPACE:]
        redis_key = self._redis_daily_key(date_key)
        self._save_workspace_record(
            date_key,
            workspace,
            record,
            log_message=(
                f"✅ Saved git error to Redis "
                f"(key={redis_key}, workspace={workspace}, repo={repo_name}, "
                f"errors={len(record['errors'])})"
            ),
        )

    def _acquire_create_lock(self, date_key: str, workspace: str) -> bool:
        client = _get_redis_client()
        if not client:
            return True
        lock_key = self._redis_create_lock_key(date_key, workspace)
        try:
            return bool(
                client.set(lock_key, "1", nx=True, ex=REDIS_CREATE_LOCK_TTL_SECONDS)
            )
        except Exception as exc:
            print(f"⚠️ Redis SET NX failed for {lock_key}: {exc}")
            return True

    def _release_create_lock(self, date_key: str, workspace: str) -> None:
        client = _get_redis_client()
        if not client:
            return
        lock_key = self._redis_create_lock_key(date_key, workspace)
        try:
            client.delete(lock_key)
        except Exception as exc:
            print(f"⚠️ Redis DELETE failed for {lock_key}: {exc}")

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _post_bot_message(
        self,
        text: str,
        blocks: Optional[List[Dict[str, Any]]] = None,
        thread_ts: Optional[str] = None,
    ) -> Optional[str]:
        payload: Dict[str, Any] = {
            "channel": self.channel,
            "text": text,
            "mrkdwn": True,
        }
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts

        response = requests.post(
            SLACK_POST_MESSAGE_URL,
            headers=self._auth_headers(),
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("error", "unknown Slack API error"))
        return body.get("ts")

    def _find_existing_parent_ts(self, date_key: str, workspace: str) -> Optional[str]:
        """Look in recent channel history for today's gitpuller parent message."""
        marker = self._parent_text(date_key, workspace)
        try:
            response = requests.get(
                SLACK_CONVERSATIONS_HISTORY_URL,
                headers=self._auth_headers(),
                params={"channel": self.channel, "limit": 100},
                timeout=10,
            )
            response.raise_for_status()
            body = response.json()
            if not body.get("ok"):
                print(f"⚠️ Slack history lookup failed: {body.get('error')}")
                return None
            for message in body.get("messages") or []:
                text = message.get("text") or ""
                ts = message.get("ts")
                thread_ts = message.get("thread_ts")
                if marker in text and ts and thread_ts in (None, ts):
                    return ts
        except Exception as exc:
            print(f"⚠️ Slack history lookup failed: {exc}")
        return None

    def _create_daily_parent(self, date_key: str, workspace: str) -> Optional[str]:
        parent_text = self._parent_text(date_key, workspace)
        parent_blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": self._parent_header(date_key, workspace),
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"All gitpuller failure alerts for `{workspace}` today "
                        "are posted in this thread."
                    ),
                },
            },
        ]
        return self._post_bot_message(parent_text, blocks=parent_blocks)

    def _wait_for_redis_thread_ts(
        self,
        date_key: str,
        workspace: str,
        attempts: int = 10,
        delay_seconds: float = 0.5,
    ) -> Optional[str]:
        if not _get_redis_client():
            return None
        for _ in range(attempts):
            thread_ts = self._read_cached_thread_ts(date_key, workspace)
            if thread_ts:
                return thread_ts
            time.sleep(delay_seconds)
        return None

    def _get_or_create_daily_thread_ts(self, workspace: str) -> Optional[str]:
        date_key = self._today_date_key()
        thread_ts = self._read_cached_thread_ts(date_key, workspace)
        if thread_ts:
            return thread_ts

        if not self._acquire_create_lock(date_key, workspace):
            thread_ts = self._wait_for_redis_thread_ts(date_key, workspace)
            if thread_ts:
                print(
                    f"ℹ️ Loaded daily Slack thread created by another run "
                    f"(workspace={workspace}, ts={thread_ts})"
                )
                return thread_ts
            print(
                f"❌ Timed out waiting for daily Slack thread in Redis "
                f"(workspace={workspace})"
            )
            return None

        try:
            thread_ts = self._read_cached_thread_ts(date_key, workspace)
            if thread_ts:
                return thread_ts

            thread_ts = self._find_existing_parent_ts(date_key, workspace)
            if thread_ts:
                self._write_cached_thread_ts(date_key, workspace, thread_ts)
                print(
                    f"ℹ️ Reused existing daily Slack thread "
                    f"(workspace={workspace}, ts={thread_ts})"
                )
                return thread_ts

            parent_ts = self._create_daily_parent(date_key, workspace)
            if not parent_ts:
                print("❌ Slack parent message did not return thread_ts")
                return None

            self._write_cached_thread_ts(date_key, workspace, parent_ts)
            return parent_ts
        finally:
            self._release_create_lock(date_key, workspace)

    def create_failure_payload(
        self,
        repo_name: str,
        error_output: str,
        workspace_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the Slack Block Kit payload describing a pull failure."""
        reported_at = datetime.now(ZoneInfo(self._thread_tz())).strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
        workspace = self._workspace_label(workspace_name)
        details = [
            f"*Workspace:* `{workspace}`",
            f"*Repository:* `{repo_name}`",
            f"*Reported at:* `{reported_at}`",
        ]

        payload: Dict[str, Any] = {
            "text": f"Automate Git Pull Pipeline Failed — {workspace} / {repo_name}",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": ":alert: Automate Git Pull Pipeline Failed :alert:",
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "\n".join(details),
                    },
                },
            ],
        }

        if error_output:
            payload["blocks"].append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Error Output:*\n```{error_output[:1500]}```",
                },
            })

        return payload

    def send_alert(
        self,
        repo_name: str,
        error_output: str = "",
        workspace_name: Optional[str] = None,
    ) -> bool:
        """Send the failure alert. Returns True on success, False on any error
        (delivery failures are logged but never raised, so alerting can't mask
        the original git error)."""
        if self._is_paused():
            print("ℹ️ Slack notifications paused via CDM_PAUSE_SLACK_MESSAGES")
            return False

        workspace = self._workspace_label(workspace_name)
        payload = self.create_failure_payload(repo_name, error_output, workspace)
        thread_ts: Optional[str] = None
        reply_ts: Optional[str] = None
        posted = False

        try:
            if self.bot_token:
                thread_ts = self._get_or_create_daily_thread_ts(workspace)
                if not thread_ts:
                    print("❌ Could not resolve daily Slack thread_ts")
                else:
                    reply_ts = self._post_bot_message(
                        payload["text"],
                        blocks=payload["blocks"],
                        thread_ts=thread_ts,
                    )
                    print(
                        f"✅ Posted Slack alert as thread reply "
                        f"(thread_ts={thread_ts}, reply_ts={reply_ts})"
                    )
                    posted = True
            elif self.webhook_url:
                response = requests.post(self.webhook_url, json=payload, timeout=10)
                response.raise_for_status()
                print("✅ Alert sent to Slack (webhook, not threaded)")
                posted = True
            else:
                print("⚠️ Slack not configured; skipping alert")
        except Exception as exc:
            print(f"⚠️ Failed to send Slack alert: {exc}")

        self._append_redis_error(
            workspace,
            repo_name,
            error_output,
            thread_ts=thread_ts,
            reply_ts=reply_ts,
        )
        return posted
