"""Pobiera plan z plan.ue.wroc.pl i zapisuje docs/data.json, docs/plan.ics, docs/status.json.

Tylko biblioteka standardowa - działa na GitHub Actions bez instalowania czegokolwiek.
Uruchomienie:  python scraper/scrape.py
"""

import datetime as dt
import hashlib
import html
import json
import re
import sys
import time
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

BASE = "https://plan.ue.wroc.pl/"
ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

TYPE_SHORT = {
    "wykład": "W",
    "ćwiczenia": "Ćw",
    "lektoraty": "Lekt",
    "laboratorium": "Lab",
    "konwersatorium": "Konw",
    "seminarium": "Sem",
    "projekt": "Proj",
}

_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
_opener.addheaders = [("User-Agent", "plan-ue-mirror/1.0 (osobisty podglad planu; github actions)")]


def fetch(path, tries=3):
    last = None
    for attempt in range(tries):
        try:
            with _opener.open(BASE + path, timeout=40) as r:
                body = r.read()
            time.sleep(0.3)  # grzecznie dla serwera uczelni
            return body.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Nie udało się pobrać {path}: {last}")


def clean(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- strefa czasowa

def _last_sunday(year, month):
    d = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(year, 12, 31)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def warsaw_offset(utc):
    """Przesunięcie Europe/Warsaw dla danej chwili UTC (reguły UE, bez zależności od tzdata)."""
    start = dt.datetime.combine(_last_sunday(utc.year, 3), dt.time(1))
    end = dt.datetime.combine(_last_sunday(utc.year, 10), dt.time(1))
    return dt.timedelta(hours=2 if start <= utc.replace(tzinfo=None) < end else 1)


def now_warsaw():
    utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return utc + warsaw_offset(utc)


# ---------------------------------------------------------------- parsowanie

def parse_position(pid):
    """Szczegóły pozycji planu: przedmiot, typ, uwagi i lista terminów."""
    page = fetch(f"p_pozycja.php?id={pid}")
    m = re.search(r"<b>Przedmiot</b></td><td[^>]*>(.*?)</td>", page, re.S)
    if not m:
        raise RuntimeError(f"Nieoczekiwany format pozycji {pid}")
    subj = clean(m.group(1))
    head, _, typ = subj.rpartition(",")
    head = re.sub(r"\s*\([^)]*\)\s*$", "", head.strip())
    name, _, code = head.rpartition(" ")
    typ = typ.strip()
    remarks = clean((re.search(r"<b>Uwagi</b></td><td[^>]*>(.*?)</td>", page, re.S) or [None, ""])[1])

    terms = []
    for row in re.findall(r"<tr class=h3><td class=h2 align=center>(.*?)</tr>", page, re.S):
        cells = [clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 6 and re.fullmatch(r"\d{4}\.\d\d\.\d\d", cells[0]):
            date, start, _hours, end, room, teacher = cells[:6]
            terms.append({
                "date": date.replace(".", "-"),
                "start": start,
                "end": end,
                "room": room,
                "teacher": teacher,
            })
    return {
        "pid": pid,
        "name": name.strip() or head,
        "code": code.strip(),
        "type": typ,
        "type_short": TYPE_SHORT.get(typ.lower(), typ[:4]),
        "remarks": remarks,
        "terms": terms,
    }


def teacher_rooms(teacher_id):
    """Sale z kalendarza prowadzącego: {(data, godzina): sala}."""
    ics = fetch(f"l_plan_pracownika_vcs.php?id={teacher_id}")
    out = {}
    for ev in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", ics, re.S):
        start = re.search(r"^DTSTART:(\d{8}T\d{6})Z", ev, re.M)
        summ = re.search(r"^SUMMARY[^:]*:(.*)$", ev, re.M)
        if not start or not summ:
            continue
        utc = dt.datetime.strptime(start.group(1), "%Y%m%dT%H%M%S")
        local = utc + warsaw_offset(utc)
        room = re.sub(r"^.*\)\s*", "", summ.group(1)).strip()
        out[(local.strftime("%Y-%m-%d"), local.strftime("%H:%M"))] = room
    return out


def language_position():
    """Pozycja lektoratu: legenda planu SJO -> sekcja bloku -> link z nazwiskiem lektora."""
    cfg = CONFIG["language"]
    page = fetch(f"l_pozycjaplanu1.php?se={CONFIG['semester_id']}&gr={cfg['sjo_group']}")
    legend = page[page.find("LEGENDA"):]
    sections = re.split(r"<b>JO\s+", legend)
    generic = None
    for sec in sections[1:]:
        if not clean(sec[:80]).startswith(cfg["block"]):
            continue
        for pid, label in re.findall(r"p_pozycja\.php\?id=(\d+)'>(.*?)</a>", sec.split("<b>JO")[0], re.S):
            label = clean(label)
            if cfg["teacher"].lower() in label.lower():
                return pid
            generic = generic or pid
    return generic


# ---------------------------------------------------------------- budowa danych

def build():
    sem, grp = CONFIG["semester_id"], CONFIG["group"]
    grid = fetch(f"l_pozycjaplanu1.php?se={sem}&gr={grp}")
    pids = sorted(set(re.findall(r"p_pozycja\.php\?id=(\d+)", grid)) - {"0"}, key=int)
    if not pids:
        raise RuntimeError("Na stronie grupy nie znaleziono żadnych pozycji planu")

    positions = [parse_position(p) for p in pids]
    lang_pid = language_position() if CONFIG.get("language") else None
    if lang_pid:
        # konkretna grupa lektoratu zastępuje ogólny blok "Język obcy" z planu kierunku
        positions = [p for p in positions if p["code"] != "JO"] + [parse_position(lang_pid)]

    electives = {}
    for el in CONFIG.get("electives", []):
        el = dict(el)
        el["rooms"] = teacher_rooms(el["teacher_id"]) if el.get("teacher_id") else {}
        electives[(el["code"], el["weekday"])] = el

    events = []
    for pos in positions:
        for t in pos["terms"]:
            day = dt.date.fromisoformat(t["date"])
            ev = {
                "date": t["date"],
                "start": t["start"],
                "end": t["end"],
                "code": pos["code"],
                "name": pos["name"],
                "type": pos["type"],
                "type_short": pos["type_short"],
                "room": t["room"],
                "teacher": t["teacher"],
                "pid": pos["pid"],
                "kind": "regular",
            }
            if pos["pid"] == lang_pid:
                ev["kind"] = "language"
                ev["code"] = "JA"
                ev["name"] = CONFIG["language"].get("label", pos["name"])
            el = electives.get((pos["code"], day.weekday()))
            if el:
                ev.update(kind="elective", name=el["name"], teacher=el["teacher"],
                          room=ev["room"] or el["rooms"].get((t["date"], t["start"]), ""))
            elif pos["code"] in CONFIG.get("not_enrolled", []):
                ev["kind"] = "not_enrolled"
            ev["key"] = f"{ev['code']}|{ev['type_short']}|{ev['date']}|{ev['start']}"
            events.append(ev)

    # to samo zajęcie może być w dwóch pozycjach (np. wspólny wykład) - usuń duplikaty
    uniq = {}
    for ev in events:
        uniq.setdefault(ev["key"], ev)
    events = sorted(uniq.values(), key=lambda e: (e["date"], e["start"], e["code"]))
    if not events:
        raise RuntimeError("Nie znaleziono żadnych terminów zajęć")
    return events


# ---------------------------------------------------------------- wykrywanie zmian

def fmt_ev(e):
    d = dt.date.fromisoformat(e["date"])
    days = ["pon", "wt", "śr", "czw", "pt", "sob", "niedz"]
    return f"{days[d.weekday()]} {d.day:02d}.{d.month:02d} {e['start']}–{e['end']}"


def diff(old, new):
    o = {e["key"]: e for e in old}
    n = {e["key"]: e for e in new}
    removed = [o[k] for k in o.keys() - n.keys()]
    added = [n[k] for k in n.keys() - o.keys()]
    items = []

    # przeniesienia: usunięty i dodany termin tego samego przedmiotu i typu
    for r in sorted(removed, key=lambda e: (e["date"], e["start"])):
        match = next((a for a in sorted(added, key=lambda e: (e["date"], e["start"]))
                      if a["code"] == r["code"] and a["type_short"] == r["type_short"]), None)
        if match:
            added.remove(match)
            items.append({"kind": "moved", "code": r["code"], "name": r["name"], "type_short": r["type_short"],
                          "date": match["date"], "text": f"{fmt_ev(r)} → {fmt_ev(match)}"})
        else:
            items.append({"kind": "removed", "code": r["code"], "name": r["name"], "type_short": r["type_short"],
                          "date": r["date"], "text": fmt_ev(r)})
    for a in added:
        items.append({"kind": "added", "code": a["code"], "name": a["name"], "type_short": a["type_short"],
                      "date": a["date"], "text": fmt_ev(a)})
    for k in o.keys() & n.keys():
        a, b = o[k], n[k]
        if a["room"] != b["room"]:
            items.append({"kind": "room", "code": b["code"], "name": b["name"], "type_short": b["type_short"],
                          "date": b["date"],
                          "text": f"{fmt_ev(b)}: sala {a['room'] or '—'} → {b['room'] or '—'}"})
        if a["teacher"] != b["teacher"]:
            items.append({"kind": "teacher", "code": b["code"], "name": b["name"], "type_short": b["type_short"],
                          "date": b["date"],
                          "text": f"{fmt_ev(b)}: prowadzi {b['teacher'] or '—'} (było: {a['teacher'] or '—'})"})
    return sorted(items, key=lambda i: i["date"])


# ---------------------------------------------------------------- ICS

VTIMEZONE = """BEGIN:VTIMEZONE
TZID:Europe/Warsaw
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE"""


def ics_escape(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    raw = line.encode("utf-8")
    if len(raw) <= 74:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 73:
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def build_ics(events, stamp):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//plan-ue//ZR//PL", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", "X-WR-CALNAME:UEW – plan zajęć", "X-WR-TIMEZONE:Europe/Warsaw",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H",
    ]
    lines += VTIMEZONE.split("\n")
    for e in events:
        if e["kind"] == "not_enrolled":
            continue
        d = e["date"].replace("-", "")
        desc = f"{e['name']} ({e['type']})\nProwadzący: {e['teacher'] or '—'}\nSala: {e['room'] or 'brak w planie'}"
        uid = hashlib.md5(e["key"].encode()).hexdigest()
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@plan-ue",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID=Europe/Warsaw:{d}T{e['start'].replace(':', '')}00",
            f"DTEND;TZID=Europe/Warsaw:{d}T{e['end'].replace(':', '')}00",
            f"SUMMARY:{ics_escape(e['name'] + ' · ' + e['type_short'])}",
            f"LOCATION:{ics_escape(e['room'] or '')}",
            f"DESCRIPTION:{ics_escape(desc)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


# ---------------------------------------------------------------- main

def main():
    DOCS.mkdir(exist_ok=True)
    data_path, status_path = DOCS / "data.json", DOCS / "status.json"
    now = now_warsaw().strftime("%Y-%m-%dT%H:%M:%S")
    prev = json.loads(data_path.read_text(encoding="utf-8")) if data_path.exists() else None

    try:
        events = build()
    except Exception as e:  # noqa: BLE001
        status_path.write_text(json.dumps({"checked_at": now, "ok": False, "error": str(e)}, ensure_ascii=False),
                               encoding="utf-8")
        print("BŁĄD:", e, file=sys.stderr)
        return 0  # zostawiamy ostatnie dobre dane; strona pokaże komunikat

    status_path.write_text(json.dumps({"checked_at": now, "ok": True}, ensure_ascii=False), encoding="utf-8")
    if prev and prev["events"] == events and prev.get("config") == CONFIG:
        print(f"Bez zmian ({len(events)} terminów)")
        return 0

    changes = list(prev.get("changes", [])) if prev else []
    if prev:
        items = diff(prev["events"], events)
        if items:
            changes.insert(0, {"detected_at": now, "items": items})
    data = {
        "generated_at": now,
        "config": CONFIG,
        "source": f"{BASE}l_pozycjaplanu1.php?se={CONFIG['semester_id']}&gr={CONFIG['group']}",
        "events": events,
        "changes": changes[:60],
    }
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    stamp = now.replace("-", "").replace(":", "")
    (DOCS / "plan.ics").write_text(build_ics(events, stamp), encoding="utf-8", newline="")
    print(f"Zapisano {len(events)} terminów; zmian w tym przebiegu: {len(changes[0]['items']) if prev and changes and changes[0]['detected_at'] == now else 0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
