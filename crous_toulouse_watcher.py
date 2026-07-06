#!/usr/bin/env python3
"""
CROUS Toulouse Housing Watcher
================================
Surveille les nouvelles offres de logement du Crous a Toulouse sur
https://trouverunlogement.lescrous.fr (tool 42, filtre sur Toulouse
via locationName + bounds) et notifie quand de nouveaux logements
apparaissent.

Installation :
    pip install requests beautifulsoup4

Configuration (variables d'environnement, au choix) :
    NTFY_TOPIC          -> notification via https://ntfy.sh (gratuit, sans compte)
    TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID -> notification Telegram

Utilisation :
    python3 crous_toulouse_watcher.py
    python3 crous_toulouse_watcher.py --debug     (sauvegarde le HTML brut pour inspection)

A lancer regulierement via cron, par exemple toutes les 15 minutes :
    */15 * * * * cd /chemin/vers/script && /usr/bin/python3 crous_toulouse_watcher.py >> watcher.log 2>&1
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://trouverunlogement.lescrous.fr/tools/42/search"
STATE_FILE = Path(__file__).parent / "seen_logements.json"
DEBUG_DIR = Path(__file__).parent / "debug_html"

# Filtrage cote serveur (identique a ce que fait le site quand tu tapes
# "Toulouse" dans la barre de recherche et que tu laisses la carte a la
# vue de Toulouse). Le "bounds" correspond a la zone visible sur la carte
# (lon_min_lat_max_lon_max_lat_min). A adapter si tu veux elargir/reduire
# la zone geographique.
SEARCH_PARAMS = {
    "locationName": "Toulouse",
    "bounds": "1.3503956_43.668708_1.5153795_43.532654",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def notify(title: str, message: str) -> None:
    sent = False

    if NTFY_TOPIC:
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": title, "Priority": "high"},
                timeout=10,
            )
            sent = True
        except Exception as e:
            print(f"[ntfy] erreur d'envoi: {e}")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": f"{title}\n\n{message}",
                },
                timeout=10,
            )
            sent = True
        except Exception as e:
            print(f"[telegram] erreur d'envoi: {e}")

    if not sent:
        # Aucun canal configure : au moins on l'affiche dans les logs
        print(f"[NOTIF] {title}\n{message}")


def load_seen() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_total_pages(soup: BeautifulSoup) -> int:
    title = soup.title.string if soup.title else ""
    m = re.search(r"page\s+\d+\s+sur\s+(\d+)", title or "")
    return int(m.group(1)) if m else 1


def parse_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for link in soup.select("a[href*='/accommodations/']"):
        href = link.get("href", "")
        m = re.search(r"/accommodations/(\d+)", href)
        if not m:
            continue
        acc_id = m.group(1)
        name = link.get_text(strip=True)
        if not name:
            continue
        card = link.find_parent("li") or link.find_parent("article") or link.parent
        text = card.get_text(" ", strip=True) if card else name
        url = (
            f"https://trouverunlogement.lescrous.fr{href}"
            if href.startswith("/")
            else href
        )
        results.append({"id": acc_id, "name": name, "url": url, "text": text})
    return results, soup


def fetch_page(page_num: int, session: requests.Session) -> str:
    params = dict(SEARCH_PARAMS)
    if page_num > 1:
        params["page"] = page_num
    r = session.get(BASE_URL, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


def scan(debug: bool = False) -> dict:
    session = requests.Session()
    html = fetch_page(1, session)

    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / "page_1.html").write_text(html, encoding="utf-8")

    items, soup = parse_page(html)
    total_pages = get_total_pages(soup)
    print(
        f"[info] {total_pages} page(s) a explorer (deja filtrees sur Toulouse par le site)"
    )

    matches = {item["id"]: item for item in items}

    for page in range(2, total_pages + 1):
        time.sleep(1.5)  # pour ne pas surcharger le serveur du Crous
        try:
            html = fetch_page(page, session)
        except requests.RequestException as e:
            print(f"[warn] page {page} echouee: {e}")
            continue

        if debug:
            (DEBUG_DIR / f"page_{page}.html").write_text(html, encoding="utf-8")

        items, _ = parse_page(html)
        for item in items:
            matches[item["id"]] = item

    print(f"[info] {len(matches)} logement(s) trouves au total")
    return matches


def main():
    parser = argparse.ArgumentParser(
        description="Surveille les logements Crous a Toulouse"
    )
    parser.add_argument(
        "--debug", action="store_true", help="sauvegarde le HTML brut de chaque page"
    )
    args = parser.parse_args()

    if not NTFY_TOPIC and not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print(
            "[avertissement] Aucun canal de notification configure (NTFY_TOPIC ou TELEGRAM_*)."
        )
        print(
            "Le script fonctionnera mais affichera seulement les resultats dans le terminal.\n"
        )

    seen = load_seen()
    matches = scan(debug=args.debug)

    # On ne se base PAS sur "deja vu un jour" mais sur "actif lors du dernier passage".
    # Ca permet de redetecter un logement qui disparait puis reapparait.
    prev_active_ids = {aid for aid, v in seen.items() if v.get("active")}
    new_ids = [aid for aid in matches if aid not in prev_active_ids]
    # notify(
    #     title="Crous Toulouse - execution",
    #     message=f"{len(matches)} logement(s) trouve(s) au total.",
    # )
    if new_ids:
        print(f"[alerte] {len(new_ids)} nouveau(x)/redevenu(s) disponible(s) !")
        lines = [f"- {matches[aid]['name']} : {matches[aid]['url']}" for aid in new_ids]
        notify(
            title=f"{len(new_ids)} logement(s) disponible(s) Crous a Toulouse !",
            message="\n".join(lines),
        )
    else:
        print("[info] Aucun nouveau logement depuis le dernier passage.")

    now = time.time()

    # Tout ce qui etait actif devient inactif par defaut, sauf si retrouve ci-dessous
    for v in seen.values():
        v["active"] = False

    for aid, item in matches.items():
        seen[aid] = {
            "name": item["name"],
            "url": item["url"],
            "last_seen": now,
            "active": True,
        }

    # on garde une trace des logements inactifs pendant 30 jours max
    # (juste pour l'historique, n'affecte plus la detection de nouveaute)
    cutoff = now - 30 * 24 * 3600
    seen = {
        aid: v
        for aid, v in seen.items()
        if v.get("active") or v.get("last_seen", 0) > cutoff
    }

    save_seen(seen)


if __name__ == "__main__":
    sys.exit(main())
