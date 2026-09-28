import json
import os
import re
import time
import hashlib
import threading
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
WATCH = json.loads((ROOT / "watchlist.json").read_text())
STATE_FILE = ROOT / "state.json"
STATE_LOCK = threading.Lock()
USER_AGENT = "Mozilla/5.0 (compatible; PokeStockAgent/1.6; personal stock monitor)"

AVAILABLE_TERMS = [
    "add to cart", "buy now", "preorder", "pre-order", "order pickup",
    "ship it", "shipping available", "available for shipping"
]
UNAVAILABLE_TERMS = ["coming soon", "sold out", "unavailable", "out of stock"]
BLOCK_TERMS = ["captcha", "access denied", "verify you are human"]

BESTBUY_RETRIES = int(os.getenv("BESTBUY_RETRIES", "3"))
BESTBUY_CONNECT_TIMEOUT = float(os.getenv("BESTBUY_CONNECT_TIMEOUT", "5"))
BESTBUY_READ_TIMEOUT = float(os.getenv("BESTBUY_READ_TIMEOUT", "8"))
BESTBUY_FAILURE_THRESHOLD = int(os.getenv("BESTBUY_FAILURE_THRESHOLD", "3"))


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    with STATE_LOCK:
        STATE_FILE.write_text(json.dumps(state, indent=2))


def normalize_text(html):
    soup = BeautifulSoup(html, "html.parser")
    return " ".join(soup.stripped_strings).lower()


def extract_price(html, text):
    candidates = []
    for raw in re.findall(r"\$(\d{1,4}(?:\.\d{2})?)", text):
        try:
            candidates.append(float(raw))
        except ValueError:
            pass

    for pattern in [
        r'"price"\s*:\s*"?(\d{1,4}(?:\.\d{1,2})?)"?',
        r'"salePrice"\s*:\s*"?(\d{1,4}(?:\.\d{1,2})?)"?',
    ]:
        for raw in re.findall(pattern, html, flags=re.I):
            try:
                candidates.append(float(raw))
            except ValueError:
                pass

    candidates = [p for p in candidates if 1 <= p <= 1000]
    return min(candidates) if candidates else None


def classify(text):
    available = [t for t in AVAILABLE_TERMS if t in text]
    unavailable = [t for t in UNAVAILABLE_TERMS if t in text]
    return {
        "available_signals": available,
        "unavailable_signals": unavailable,
        "available": bool(available) and not unavailable,
    }


def request_with_retries(session, product):
    retailer = product["retailer"].lower()
    attempts = BESTBUY_RETRIES if retailer == "best buy" else 1
    timeout = (
        BESTBUY_CONNECT_TIMEOUT,
        BESTBUY_READ_TIMEOUT,
    ) if retailer == "best buy" else (5, 15)

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            r = session.get(product["url"], timeout=timeout, allow_redirects=True)
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                return None, f"HTTP {r.status_code}"
            return r, None
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
            last_error = str(e)
            if attempt < attempts:
                time.sleep(0.8 * attempt)
    return None, last_error or "unknown request error"


def inspect(session, product):
    r, error = request_with_retries(session, product)
    if r is None:
        return {
            "ok": False,
            "error": error,
            "http": None,
            "price": None,
            "available": False,
            "signals": [],
            "unavailable_signals": [],
            "blocked": False,
        }

    text = normalize_text(r.text)
    status = classify(text)
    price = extract_price(r.text, text)
    blocked = any(x in text for x in BLOCK_TERMS)
    max_price = float(product["max_price"])
    min_price = float(product.get("min_price", max_price * 0.60))
    price_ok = price is not None and min_price <= price <= max_price + 0.01

    return {
        "ok": True,
        "error": None,
        "http": r.status_code,
        "price": price,
        "available": status["available"] and price_ok and not blocked,
        "signals": status["available_signals"],
        "unavailable_signals": status["unavailable_signals"],
        "blocked": blocked,
    }


def alert_text(p):
    price = f"${p['price_seen']:.2f}" if isinstance(p.get("price_seen"), (int, float)) else "precio retail"
    return (
        f"{p['retailer']}: {p['product']} CONFIRMADO PARA COMPRAR — "
        f"{price} — cantidad objetivo {p['qty_desired']} — {p['url']}"
    )


def send_ntfy_payload(title, message, priority=5, click=None, tags=None):
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return
    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or ["warning"],
    }
    if click:
        payload["click"] = click
    requests.post("https://ntfy.sh", json=payload, timeout=10).raise_for_status()


def send_ntfy(p):
    send_ntfy_payload(
        "🔥 Pokémon CONFIRMADO PARA COMPRAR",
        alert_text(p),
        priority=5,
        click=p["url"],
        tags=["rotating_light", "shopping_cart"],
    )


def send_twilio_sms(p):
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    frm = os.getenv("TWILIO_FROM_NUMBER", "").strip()
    to = os.getenv("SMS_TO_NUMBER", "").strip()
    if all([sid, token, frm, to]):
        requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"From": frm, "To": to, "Body": alert_text(p)},
            auth=(sid, token),
            timeout=10,
        ).raise_for_status()


def notify(p):
    print("ALERT", json.dumps(p, ensure_ascii=False), flush=True)
    try:
        send_ntfy(p)
    except Exception as e:
        print(f"NTFY error: {e}", flush=True)
    try:
        send_twilio_sms(p)
    except Exception as e:
        print(f"SMS error: {e}", flush=True)


def notify_monitor_problem(message):
    print("MONITOR_ALERT", message, flush=True)
    try:
        send_ntfy_payload(
            "⚠️ PokéStock: monitor Best Buy con problemas",
            message,
            priority=5,
            tags=["warning", "satellite"],
        )
    except Exception as e:
        print(f"NTFY error: {e}", flush=True)


def process_result(product, first, state):
    key = f"{product['retailer']}:{product.get('sku') or product.get('tcin') or product['url']}"

    if not first.get("ok"):
        print(
            f"{datetime.now(timezone.utc).isoformat()} {key} ERROR {first.get('error')}",
            flush=True,
        )
        return False

    confirmed = False
    final = first

    if first["available"]:
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })
        time.sleep(2)
        second = inspect(session, product)
        if second.get("ok") and second["available"]:
            confirmed = True
            final = second

    fingerprint = {
        "price": final.get("price"),
        "confirmed_buy": confirmed,
        "signals": final.get("signals", []),
        "blocked": final.get("blocked", False),
    }
    fp = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()

    with STATE_LOCK:
        old = state.get(key, {}).get("fingerprint")
        if old != fp:
            state[key] = {
                "fingerprint": fp,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "snapshot": fingerprint,
            }
            STATE_FILE.write_text(json.dumps(state, indent=2))

    if old != fp and confirmed:
        qty = min(
            int(product.get("qty", 1)),
            int(product.get("max_qty", product.get("qty", 1))),
        )
        notify({
            "type": "confirmed_buy",
            "retailer": product["retailer"],
            "product": product["name"],
            "qty_desired": qty,
            "price_seen": final.get("price"),
            "url": product["url"],
            "store": product.get("store"),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        })

    return True


def update_bestbuy_health(state, successes, failures):
    health = state.setdefault("__health__", {})
    consecutive = int(health.get("bestbuy_full_failure_streak", 0))
    alerted = bool(health.get("bestbuy_alerted", False))

    if failures > 0 and successes == 0:
        consecutive += 1
        health["bestbuy_full_failure_streak"] = consecutive
        health["bestbuy_last_failure_at"] = datetime.now(timezone.utc).isoformat()

        if consecutive >= BESTBUY_FAILURE_THRESHOLD and not alerted:
            health["bestbuy_alerted"] = True
            save_state(state)
            notify_monitor_problem(
                f"Best Buy falló en todos los productos durante {consecutive} ciclos seguidos. "
                "El bot sigue vivo, pero la vigilancia de Best Buy no es confiable hasta que responda de nuevo."
            )
            return
    else:
        if alerted and successes > 0:
            print("MONITOR_RECOVERY Best Buy volvió a responder.", flush=True)
        health["bestbuy_full_failure_streak"] = 0
        health["bestbuy_alerted"] = False
        if successes > 0:
            health["bestbuy_last_success_at"] = datetime.now(timezone.utc).isoformat()

    save_state(state)


def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    })
    return session


def run_bestbuy(products, state):
    successes = 0
    failures = 0

    def inspect_one(product):
        session = make_session()
        return product, inspect(session, product)

    workers = max(1, min(4, len(products)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(inspect_one, p) for p in products]
        for future in as_completed(futures):
            product, result = future.result()
            ok = process_result(product, result, state)
            if ok:
                successes += 1
            else:
                failures += 1

    update_bestbuy_health(state, successes, failures)


def run_other_retailers(products, state):
    session = make_session()
    for product in products:
        result = inspect(session, product)
        process_result(product, result, state)
        time.sleep(1)


def main():
    poll = int(os.getenv("POLL_SECONDS", WATCH.get("poll_seconds", 60)))
    state = load_state()

    print(
        f"PokéStock Agent v1.6 started. Polling every {poll}s "
        f"(Best Buy parallel + retries enabled)",
        flush=True,
    )

    while True:
        cycle_started = time.monotonic()
        bestbuy = [p for p in WATCH["products"] if p["retailer"].lower() == "best buy"]
        others = [p for p in WATCH["products"] if p["retailer"].lower() != "best buy"]

        if bestbuy:
            run_bestbuy(bestbuy, state)
        if others:
            run_other_retailers(others, state)

        elapsed = time.monotonic() - cycle_started
        time.sleep(max(5, poll - elapsed))


if __name__ == "__main__":
    main()
