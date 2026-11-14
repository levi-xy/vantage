#!/usr/bin/env python3
"""API steps of the CI upgrade test (job "upgrade"), standard library only.

    upgrade_check.py seed   --base https://127.0.0.1:8443 --setup-code ABCD-... --state s.json
    upgrade_check.py verify --base https://127.0.0.1:8443 --state s.json [--release 0.0.0-ci]

``seed`` runs against the previous release: it creates the admin (through the setup
code), a viewer, a ping device, a device with a secret (an HTTP check with a bearer
token), a dashboard with a card, a map, a Telegram channel, an alert rule and a time
zone, then writes what it created to the state file. ``verify`` runs against the
upgraded stack and checks that all of it is still there with the same values, that
logins work, that no secret value is ever returned, that the audit log still holds
every earlier entry, and that /healthz is ok. Only the API of the release that
is current at the time is used, and only fields present in every 0.1.x release.

All values are obviously fake; hosts are RFC 1918 addresses. Nothing is contacted:
the devices don't exist (they just show as down) and the channel's token is fake.
"""

import argparse
import http.cookiejar
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any

ADMIN = ("upgrade-admin", "example-admin-password-1")
VIEWER = ("upgrade-viewer", "example-viewer-password-1")
BEARER_TOKEN = "example-bearer-token-do-not-use"  # noqa: S105 (fake test value)
BOT_TOKEN = "123456789:EXAMPLE-bot-token-do-not-use-0000"  # noqa: S105 (fake test value)
PING_NAME = "upgrade-ping"
HTTP_NAME = "upgrade-http"
DASHBOARD_NAME = "Upgrade dashboard"
MAP_NAME = "Upgrade map"
CHANNEL_NAME = "upgrade-channel"
RULE_NAME = "upgrade-rule"
RULE_EXPR = "vantage_instance_up == 0"
TIMEZONE = "Pacific/Auckland"
HTTP_URL = "https://10.0.0.20/health"
PING_HOST = "10.0.0.10"
CHAT_ID = "-1001234567890"
SECRETS = (ADMIN[1], VIEWER[1], BEARER_TOKEN, BOT_TOKEN)


class Failure(Exception):
    pass


class Client:
    """A browser-like session: cookie jar plus the CSRF token from the login response."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False  # the stack's self-signed certificate
        ctx.verify_mode = ssl.CERT_NONE
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), urllib.request.HTTPSHandler(context=ctx)
        )
        self.csrf: str | None = None
        self.bodies: list[str] = []  # every response body, to scan for secrets

    def call(
        self, method: str, path: str, body: Any = None, *, expect: int | tuple[int, ...] = 200
    ) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)  # noqa: S310 (our own https base)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.csrf and method not in ("GET", "HEAD"):
            req.add_header("X-CSRF-Token", self.csrf)
        try:
            with self.opener.open(req, timeout=30) as resp:
                status, raw = resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read().decode()
        self.bodies.append(raw)
        wanted = (expect,) if isinstance(expect, int) else expect
        if status not in wanted:
            raise Failure(f"{method} {path}: HTTP {status}, wanted {wanted}: {raw[:300]}")
        return json.loads(raw) if raw else None

    def api(self, method: str, path: str, body: Any = None, **kw: Any) -> Any:
        return self.call(method, "/api/v1" + path, body, **kw)

    def login(self, creds: tuple[str, str]) -> dict[str, Any]:
        me = self.api("POST", "/auth/login", {"username": creds[0], "password": creds[1]})
        self.csrf = me["csrf_token"]
        return me  # type: ignore[no-any-return]

    def assert_no_secrets(self) -> None:
        text = "\n".join(self.bodies)
        for value in SECRETS:
            if value in text:
                raise Failure("a secret value appeared in an API response")


def check(cond: bool, what: str) -> None:
    if not cond:
        raise Failure(what)
    print(f"ok: {what}", flush=True)


def wait_healthy(client: Client, seconds: int = 120) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            client.call("GET", "/healthz/live")
            return
        except (Failure, OSError):
            if time.monotonic() > deadline:
                raise Failure("/healthz/live didn't answer in time") from None
            time.sleep(2)


def wait_ok(client: Client, path: str, want: Any, seconds: int = 180) -> None:
    """Right after a start /healthz can name a transient reason (Prometheus not yet
    scraping); it has to settle to ok."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            got = client.call("GET", path, expect=(200, 503))
        except OSError:
            got = None
        if got == want:
            return
        if time.monotonic() > deadline:
            raise Failure(f"{path} answered {got!r}, wanted {want!r}")
        time.sleep(3)


def seed(base: str, setup_code: str, state_path: str) -> None:
    c = Client(base)
    wait_healthy(c)
    me = c.api(
        "POST",
        "/setup",
        {"username": ADMIN[0], "password": ADMIN[1], "setup_code": setup_code},
        expect=201,
    )
    c.csrf = me["csrf_token"]
    c.api(
        "POST",
        "/users",
        {"username": VIEWER[0], "password": VIEWER[1], "role": "viewer"},
        expect=201,
    )
    ping = c.api(
        "POST",
        "/instances",
        {
            "type_id": "ping_check",
            "name": PING_NAME,
            "site": "Upgrade site",
            "tags": ["upgrade"],
            "config": {"host": PING_HOST},
        },
        expect=201,
    )
    http_dev = c.api(
        "POST",
        "/instances",
        {
            "type_id": "http_check",
            "name": HTTP_NAME,
            "color": "teal",
            "config": {"url": HTTP_URL, "expected_status": 204, "bearer_token": BEARER_TOKEN},
        },
        expect=201,
    )
    dash = c.api("POST", "/dashboards", {"name": DASHBOARD_NAME}, expect=201)
    cards = [
        {
            "instance_id": ping["id"],
            "card_type": "ping_check",
            "x": 0,
            "y": 0,
            "w": 4,
            "h": 3,
            "settings": {"title": "Upgrade card"},
        }
    ]
    c.api("PUT", f"/dashboards/{dash['id']}", {"layout": {"version": 2, "cards": cards}})
    document = {
        "version": 1,
        "vlans": [],
        "elements": [
            {
                "kind": "box",
                "id": "b1",
                "x": 40,
                "y": 40,
                "w": 160,
                "h": 80,
                "label": "Upgrade box",
                "instance_id": ping["id"],
            }
        ],
    }
    new_map = c.api("POST", "/maps", {"name": MAP_NAME}, expect=201)
    c.api("PUT", f"/maps/{new_map['id']}", {"document": document, "revision": new_map["revision"]})
    channel = c.api(
        "POST",
        "/channels",
        {
            "type": "telegram",
            "name": CHANNEL_NAME,
            "system_alerts": False,
            "config": {"chat_id": CHAT_ID, "bot_token": BOT_TOKEN},
        },
        expect=201,
    )
    rule = c.api(
        "POST",
        "/alerts/rules",
        {
            "name": RULE_NAME,
            "expr": RULE_EXPR,
            "for_seconds": 120,
            "severity": "critical",
            "channel_ids": [channel["id"]],
        },
        expect=201,
    )
    c.api("PUT", "/settings/time", {"timezone": TIMEZONE})
    audit = c.api("GET", "/audit?limit=200")
    check(len(audit) >= 8, "the audit log recorded the seeding")
    state = {
        "ping_id": ping["id"],
        "http_id": http_dev["id"],
        "dashboard_id": dash["id"],
        "map_id": new_map["id"],
        "channel_id": channel["id"],
        "rule_id": rule["id"],
        "audit": [
            [e["id"], e["action"], e["target_type"], e["target_id"], e["changes"]] for e in audit
        ],
    }
    c.assert_no_secrets()
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    print("seeded", flush=True)


def by_name(items: list[dict[str, Any]], name: str, what: str) -> dict[str, Any]:
    found = [i for i in items if i["name"] == name]
    check(len(found) == 1, f"{what} '{name}' is still there")
    return found[0]


def verify(base: str, state_path: str, release: str | None) -> None:
    with open(state_path, encoding="utf-8") as fh:
        st = json.load(fh)
    c = Client(base)
    wait_healthy(c)

    me = c.login(ADMIN)
    check(me["role"] == "admin", "the admin can log in")
    if release is not None:
        check(me.get("server_version") == release, f"GET /me reports version {release}")
    check(c.api("GET", "/me")["username"] == ADMIN[0], "GET /me works with the session")

    viewer = Client(base)
    check(viewer.login(VIEWER)["role"] == "viewer", "the viewer can log in")
    viewer.api("GET", "/instances")
    viewer.call("POST", "/api/v1/dashboards", {"name": "nope"}, expect=(401, 403))  # read-only
    viewer.bodies.clear()

    users = c.api("GET", "/users")
    check(
        {u["username"]: u["role"] for u in users} == {ADMIN[0]: "admin", VIEWER[0]: "viewer"},
        "both users are there with their roles",
    )

    instances = c.api("GET", "/instances")
    ping = by_name(instances, PING_NAME, "instance")
    check(
        ping["id"] == st["ping_id"]
        and ping["type_id"] == "ping_check"
        and ping["site"] == "Upgrade site"
        and ping["tags"] == ["upgrade"]
        and ping["config"].get("host") == PING_HOST,
        "the ping device keeps its values",
    )
    http_dev = by_name(instances, HTTP_NAME, "instance")
    check(
        http_dev["id"] == st["http_id"]
        and http_dev["color"] == "teal"
        and http_dev["config"]["url"] == HTTP_URL
        and http_dev["config"]["expected_status"] == 204,
        "the HTTP device keeps its values",
    )
    check(
        http_dev["secrets_set"] == {"bearer_token": True}, "the device's secret still reads 'set'"
    )
    check("bearer_token" not in http_dev["config"], "the secret is not in the config")
    one = c.api("GET", f"/instances/{st['http_id']}")
    check(one["secrets_set"] == {"bearer_token": True}, "GET /instances/{id} says 'set' too")
    # The stored secret still decrypts: an update that omits it keeps it, and the
    # response is built by decrypting the blob with the (unchanged) master key.
    c.api("PATCH", f"/instances/{st['http_id']}", {"site": "Still decrypts"})
    after = c.api("GET", f"/instances/{st['http_id']}")
    check(
        after["secrets_set"] == {"bearer_token": True} and after["site"] == "Still decrypts",
        "an update without the secret keeps it (it decrypts with the same master key)",
    )

    dashboards = c.api("GET", "/dashboards")
    dash = by_name(dashboards, DASHBOARD_NAME, "dashboard")
    layout = c.api("GET", f"/dashboards/{dash['id']}")["layout"]
    check(
        len(layout["cards"]) == 1
        and layout["cards"][0]["instance_id"] == st["ping_id"]
        and layout["cards"][0]["settings"] == {"title": "Upgrade card"}
        and layout["cards"][0]["card_type"] == "ping_check",
        "the dashboard keeps its card",
    )

    maps = c.api("GET", "/maps")
    the_map = by_name(maps, MAP_NAME, "map")
    doc = c.api("GET", f"/maps/{the_map['id']}")["document"]
    boxes = [e for e in doc["elements"] if e["kind"] == "box"]
    check(
        len(boxes) == 1
        and boxes[0]["label"] == "Upgrade box"
        and boxes[0]["instance_id"] == st["ping_id"],
        "the map keeps its box",
    )

    channel = by_name(c.api("GET", "/channels"), CHANNEL_NAME, "channel")
    check(
        channel["config"] == {"chat_id": CHAT_ID}
        and channel["system_alerts"] is False
        and channel["secrets_set"] == {"bot_token": True},
        "the channel keeps its values, its token reads 'set'",
    )

    rule = by_name(c.api("GET", "/alerts/rules"), RULE_NAME, "alert rule")
    check(
        rule["expr"] == RULE_EXPR
        and rule["for_seconds"] == 120
        and rule["severity"] == "critical"
        and rule["channel_ids"] == [st["channel_id"]]
        and rule["enabled"] is True,
        "the alert rule keeps its values",
    )

    tz = c.api("GET", "/settings/time")
    check(tz["timezone"] == TIMEZONE, "the timezone setting is kept")

    audit = c.api("GET", "/audit?limit=200")
    now = {
        e["id"]: [e["id"], e["action"], e["target_type"], e["target_id"], e["changes"]]
        for e in audit
    }
    missing = [e for e in st["audit"] if now.get(e[0]) != e]
    check(not missing, f"all {len(st['audit'])} earlier audit entries are intact")
    check(
        not any(
            BEARER_TOKEN in json.dumps(e["changes"]) or BOT_TOKEN in json.dumps(e["changes"])
            for e in audit
        ),
        "no secret value in the audit log",
    )

    wait_ok(c, "/healthz", {"status": "ok"})
    check(True, "/healthz is ok")

    for client in (c, viewer):
        client.assert_no_secrets()
    check(True, "no secret value in any response")
    print("upgrade check: all checks passed", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("step", choices=("seed", "verify"))
    ap.add_argument("--base", default="https://127.0.0.1:8443")
    ap.add_argument("--state", required=True)
    ap.add_argument("--setup-code", default="")
    ap.add_argument("--release", default=None, help="the version GET /me must report")
    args = ap.parse_args()
    try:
        if args.step == "seed":
            seed(args.base, args.setup_code, args.state)
        else:
            verify(args.base, args.state, args.release)
    except Failure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
