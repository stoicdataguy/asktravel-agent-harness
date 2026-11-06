"""End-to-end test: only Open WebUI can call the agent API (#27).

Against the running stack (docker compose up -d, uv run travel-agent) with AGENT_API_KEY set
in .env (Open WebUI must have been started after it was set):
1. /health answers without a token;
2. every other route answers 401 without the token or with a wrong one;
3. the right token is accepted;
4. a signed-in Open WebUI user can still chat with the agent (Open WebUI sends the key);
5. with --expect-pending: a brand-new sign-up gets the "pending" role (start Open WebUI with
   DEFAULT_USER_ROLE=pending for this one).

Usage: uv run python scripts/e2e_auth.py [--api http://localhost:8000] [--expect-pending]
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid

from owui import get_token

from travel_agent.api.auth import api_key_headers

OWUI = "http://localhost:3000"
PROTECTED = [
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
    ("GET", "/api/itineraries"),
    ("GET", "/api/memory"),
    ("DELETE", "/api/memory"),
]


def status(url: str, method: str = "GET", headers: dict | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json", **(headers or {})}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as err:
        return err.code, None


def check(results: list, name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")


def main(args) -> int:
    if not api_key_headers():
        print("AGENT_API_KEY is not set in .env, so there's nothing to test.")
        return 1
    results: list[bool] = []

    code, _ = status(f"{args.api}/health")
    check(results, "/health is open", code == 200, str(code))

    for wrong in ({}, {"authorization": "Bearer wrong-key"}):
        codes = {
            f"{method} {path}": status(f"{args.api}{path}", method, wrong)[0]
            for method, path in PROTECTED
        }
        label = "no token" if not wrong else "a wrong token"
        check(results, f"{label} gets 401 everywhere", set(codes.values()) == {401}, str(codes))

    code, body = status(f"{args.api}/v1/models", headers=api_key_headers())
    check(results, "the key is accepted", code == 200 and body["data"][0]["id"] == "travel-agent")

    token = get_token(OWUI, "e2e auth", "e2e-auth@travel.local", "e2e-pass-123")
    code, body = status(
        f"{OWUI}/api/chat/completions",
        "POST",
        {"authorization": f"Bearer {token}"},
        {
            "model": "travel-agent",
            "stream": False,
            "messages": [{"role": "user", "content": "Reply with just the word: ready"}],
        },
    )
    reply = body["choices"][0]["message"]["content"] if body else ""
    check(results, "Open WebUI users can still chat", code == 200 and bool(reply), reply[:80])

    if args.expect_pending:
        email = f"e2e-pending-{uuid.uuid4().hex[:8]}@travel.local"
        code, body = status(
            f"{OWUI}/api/v1/auths/signup",
            "POST",
            body={"name": "e2e pending", "email": email, "password": "e2e-pass-123"},
        )
        role = body.get("role") if body else None
        check(results, "a new sign-up waits for approval", role == "pending", f"{code} {role}")

    print(f"{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000", help="agent API base URL")
    parser.add_argument("--expect-pending", action="store_true")
    sys.exit(main(parser.parse_args()))
