"""Persistent outbound bridge for the dedicated TE OpenWA session (Python 3.10+)."""

import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SESSION = "te-delcio"
DATA = Path(
    os.environ.get(
        "TE_BRIDGE_DIR", Path.home() / "Library/Application Support/te-openwa"
    )
)
LOCAL = Path(
    os.environ.get(
        "OPENWA_CREDENTIALS", Path.home() / ".codex/private/openwa/credentials.json"
    )
)
REMOTE = os.environ.get(
    "TE_BRIDGE_URL", "https://te-delcio.vercel.app/integracoes/whatsapp/servico/"
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api(url, headers, body=None, method=None):
    req = Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            **headers,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "TE-OpenWA/1.0",
        },
    )
    with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=35) as r:
        return json.load(r)


def openwa(path, body=None):
    cfg = json.loads(LOCAL.read_text())
    return api(
        "http://127.0.0.1:2785/api/sessions" + path, {"X-API-Key": cfg["api_key"]}, body
    )


def session_state(want_pair):
    entries = [s for s in openwa("?name=" + SESSION) if s["name"] == SESSION]
    if not entries and not want_pair:
        return "waiting", "", None
    s = entries[0] if entries else openwa("", {"name": SESSION})
    sid = str(uuid.UUID(s["id"]))
    status = s["status"]
    if status == "ready" and s.get("engineLoaded"):
        return "ready", "", sid
    if want_pair and status == "qr_ready" and s.get("engineLoaded"):
        try:
            qr = openwa(f"/{sid}/qr")["qrCode"]
            return "pairing", qr, sid
        except HTTPError as exc:
            if exc.code != 400:
                raise
    # Recover a saved linked session after OpenWA restarts. Pairing a new account is admin initiated.
    start = status in {"created", "disconnected", "failed"} or not s.get("engineLoaded")
    if start and (want_pair or (DATA / "paired").exists()) and status != "initializing":
        openwa(f"/{sid}/start", {})
    return ("starting" if want_pair else "waiting"), "", sid


def save_journal(record):
    tmp = DATA / "job.tmp"
    with tmp.open("w") as f:
        json.dump(record, f)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(DATA / "job.json")


def run():
    os.umask(0o077)
    token = (DATA / "token").read_text().strip()
    journal = DATA / "job.json"
    record = json.loads(journal.read_text()) if journal.exists() else {}
    want_pair = False
    while True:
        try:
            try:
                status, qr, sid = session_state(want_pair)
            except (OSError, ValueError, KeyError):
                status, qr, sid = "error", "", None
            payload = {"status": status, "qr": qr}
            if "outcome" in record:
                payload["ack"] = {
                    "claim": record["claim"],
                    "status": record["outcome"],
                    "provider_id": record.get("provider_id", ""),
                }
            elif status == "ready":
                (DATA / "paired").touch(exist_ok=True)
                if not record:
                    record = {"claim": str(uuid.uuid4())}
                    save_journal(record)
                payload["claim"] = record["claim"]
            result = api(REMOTE, {"Authorization": "Bearer " + token}, payload)
            recently_requested = result["pair"] and not want_pair
            want_pair = result["pair"]
            if "ack" in payload:
                journal.unlink(missing_ok=True)
                record = {}
            job = result["job"]
            if job:
                if (
                    not sid
                    or not re.fullmatch(r"\+[1-9][0-9]{6,14}", job["phone"])
                    or not job["text"]
                ):
                    raise ValueError("invalid job")
                # Write ahead of the external send. A crash or ambiguous POST is never replayed.
                record["outcome"] = "uncertain"
                save_journal(record)
                response = openwa(
                    f"/{sid}/messages/send-text",
                    {"chatId": job["phone"][1:] + "@c.us", "text": job["text"]},
                )
                message_id = response.get("messageId")
                if isinstance(message_id, str) and message_id:
                    record.update(outcome="accepted", provider_id=message_id)
                    save_journal(record)
            delay = (
                1
                if job or recently_requested or "ack" in payload
                else (3 if want_pair and status != "ready" else 18)
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Neither QR contents, authentication material nor recipients belong in logs.
            print(
                f"Serviço temporariamente indisponível ({type(exc).__name__}, {getattr(exc, 'code', '-')})",
                flush=True,
            )
            delay = 8
        time.sleep(delay)


if __name__ == "__main__":
    run()
