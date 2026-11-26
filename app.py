import sys
from hmac import compare_digest
from http.cookies import SimpleCookie, CookieError
from io import BytesIO
from PIL import Image
from secrets import token_urlsafe
from cryptography.fernet import Fernet, InvalidToken
import hashlib, json, os, sqlite3, ssl, threading, time
from calendar import monthrange
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo
from html import escape
from ipaddress import IPv4Network
from pathlib import Path
from string import Template
from urllib.parse import quote, urlencode, urlsplit, parse_qs
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler, ProxyHandler
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

app_dir = Path(__file__).resolve().parent
logo = app_dir / "assets" / "omada-inside.png"

def render(template, **values):
    path = app_dir / "templates" / (template + ".html")
    return Template(path.read_text(encoding="utf-8")).substitute(values)

env = os.environ
app_version = (app_dir / "VERSION").read_text().strip()
app_build = env.get("APP_BUILD", "lokal")
schema_version = 7
admin_password = env.get("APP_ADMIN_PASSWORD", "").encode("utf-8")
wlan_csrf = token_urlsafe(32)
login_csrf = token_urlsafe(32)
session_name = "omada_admin"
session_lifetime = 8 * 3600
sessions = {}
login_failures = []
session_lock = threading.Lock()
network_sync_lock = threading.Lock()
logo_size_limit = 2 * 1024 * 1024
base = env["OMADA_URL"].rstrip("/")
if urlsplit(base).scheme != "https" or not urlsplit(base).hostname:
    raise SystemExit("OMADA_URL muss eine HTTPS-Adresse sein.")
root = "/openapi/v1/" + quote(env["OMADA_ID"], safe="")
context = ssl.create_default_context()
if env["OMADA_VERIFY_TLS"].lower() == "false":
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

http = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
auth_state = threading.local()
display_zone = ZoneInfo(env.get("APP_TIMEZONE", "UTC"))

def access_token():
    if time.monotonic() >= getattr(auth_state, "valid_until", 0):
        auth = request("/openapi/authorize/token?grant_type=client_credentials", {
            "omadacId": env["OMADA_ID"], "client_id": env["OMADA_CLIENT_ID"],
            "client_secret": env["OMADA_CLIENT_SECRET"],
        }, login=True)
        auth_state.token = auth["accessToken"]
        auth_state.valid_until = time.monotonic() + 900
    return auth_state.token

def request(path, payload=None, *, login=False):
    headers = {}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if not login:
        headers["Authorization"] = "AccessToken=" + access_token()
    body = json.dumps(payload).encode() if payload is not None else None
    with http.open(Request(base + path, body, headers), timeout=10) as response:
        result = json.load(response)
    if result.get("errorCode") != 0:
        raise ValueError("Omada-Fehlercode " + str(result.get("errorCode")))
    return result["result"]

def rows(path):
    records = []
    for page in range(1, 501):
        result = request(path + "?" + urlencode({"page": page, "pageSize": 100}))
        batch = result["data"]
        if not isinstance(batch, list):
            raise ValueError("Unerwartetes Listenformat")
        records.extend(batch)
        if len(records) >= int(result["totalRows"]):
            return records
        if not batch:
            break
    raise ValueError("Unvollständige Omada-Liste")

def mac(value):
    return str(value or "").replace("-", ":").upper()

key = hashlib.sha256((base + root + env["OMADA_SITE_ID"]).encode()).hexdigest()[:20]
database = "/data/" + key + ".sqlite"
status = "Warte auf die erste Abfrage."
def initialize_database():
    with closing(sqlite3.connect(database, timeout=15)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > schema_version:
            raise RuntimeError("Die Datenbank benötigt eine neuere App-Version.")
        migrations = ("""
        CREATE TABLE IF NOT EXISTS current (
            kind TEXT, id TEXT, name TEXT, value TEXT, PRIMARY KEY (kind, id)
        );
        CREATE TABLE IF NOT EXISTS events (
            ts INTEGER, kind TEXT, id TEXT, name TEXT, ap TEXT, detail TEXT
        );
        CREATE INDEX IF NOT EXISTS events_time ON events(ts);
        CREATE TABLE IF NOT EXISTS wan_points (
            gateway_id TEXT NOT NULL, port INTEGER NOT NULL,
            resolution TEXT NOT NULL, timestamp INTEGER NOT NULL,
            gateway_name TEXT NOT NULL, port_name TEXT NOT NULL,
            rx INTEGER NOT NULL, tx INTEGER NOT NULL,
            PRIMARY KEY (gateway_id, port, resolution, timestamp)
        );
        CREATE INDEX IF NOT EXISTS wan_points_timestamp ON wan_points(timestamp);
        CREATE TABLE IF NOT EXISTS wan_archive_runs (
            resolution TEXT PRIMARY KEY, finished_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wan_daily_backfills (
            month_start INTEGER PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS branding (
            id INTEGER PRIMARY KEY CHECK (id = 1), png BLOB NOT NULL
        );
    """, """
            CREATE TABLE IF NOT EXISTS wan_probe (
                resolution TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
                points INTEGER NOT NULL, PRIMARY KEY(resolution, start, end)
            );
            CREATE TABLE IF NOT EXISTS worker_activity (
                id INTEGER PRIMARY KEY, started INTEGER NOT NULL, finished INTEGER,
                status TEXT NOT NULL, detail TEXT NOT NULL, inserted INTEGER NOT NULL,
                version TEXT NOT NULL, build TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS worker_runtime (
                id INTEGER PRIMARY KEY CHECK (id = 1), version TEXT NOT NULL, build TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schema_history (
                version INTEGER PRIMARY KEY, migrated_at INTEGER NOT NULL, previous_version INTEGER NOT NULL
            );
        """, """
            CREATE TABLE IF NOT EXISTS network_objects (
                id INTEGER PRIMARY KEY, site TEXT NOT NULL, kind TEXT NOT NULL,
                remote_id TEXT NOT NULL, name TEXT NOT NULL, detail TEXT NOT NULL,
                security INTEGER, active INTEGER NOT NULL, last_seen INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '', UNIQUE(site, kind, remote_id)
            );
            CREATE TABLE IF NOT EXISTS network_access (
                id INTEGER PRIMARY KEY, ssid INTEGER NOT NULL REFERENCES network_objects(id),
                label TEXT NOT NULL, vlan INTEGER REFERENCES network_objects(id),
                secret BLOB NOT NULL, note TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS network_access_ssid ON network_access(ssid);
            CREATE TABLE IF NOT EXISTS network_sync (
                id INTEGER PRIMARY KEY CHECK(id=1), site TEXT NOT NULL,
                succeeded INTEGER, checked INTEGER NOT NULL, error TEXT NOT NULL
            );
        """, """
            CREATE TABLE IF NOT EXISTS network_import (
                id INTEGER PRIMARY KEY, ssid INTEGER NOT NULL REFERENCES network_objects(id),
                source_key TEXT NOT NULL, label TEXT NOT NULL, profile TEXT NOT NULL,
                vlan INTEGER, secret BLOB, confirmed INTEGER NOT NULL,
                active INTEGER NOT NULL, UNIQUE(ssid, source_key)
            );
        """, """
            ALTER TABLE network_objects ADD COLUMN ipv4_subnet TEXT NOT NULL DEFAULT '';
        """, """
            CREATE TABLE app_settings (id INTEGER PRIMARY KEY CHECK (id = 1), display_name TEXT NOT NULL);
        """, """
            ALTER TABLE branding ADD COLUMN logo_icon BLOB;
            ALTER TABLE app_settings ADD COLUMN favicon BLOB;
        """)
        for target in range(version + 1, schema_version + 1):
            for statement in migrations[target - 1].split(";"):
                if statement.strip():
                    db.execute(statement)
            if target == 7:
                previous_logo = db.execute("SELECT png FROM branding WHERE id=1").fetchone()
                if previous_logo:
                    try:
                        icon = render_favicon(previous_logo[0])
                    except (ValueError, OSError, Image.DecompressionBombError):
                        pass
                    else:
                        db.execute("UPDATE branding SET logo_icon=? WHERE id=1", (icon,))
            db.execute(f"PRAGMA user_version={target}")
        if version < schema_version:
            db.execute("INSERT INTO schema_history VALUES (?, ?, ?)", (schema_version, int(time.time()), version))


def ap_status(value):
    return {"0": "Getrennt", "1": "Verbunden", "2": "Ausstehend",
            "3": "Heartbeat fehlt", "4": "Isoliert"}.get(value, value)

def select_site():
    if env["OMADA_SITE_ID"]:
        return env["OMADA_SITE_ID"]
    sites = rows(root + "/sites")
    if len(sites) != 1:
        raise ValueError("OMADA_SITE_ID setzen, wenn keine eindeutige Site vorliegt")
    return sites[0]["siteId"]

def failure(stage, error):
    if hasattr(error, "code"):
        detail = "HTTP " + str(error.code)
    elif isinstance(error, URLError):
        detail = type(error.reason).__name__
    elif isinstance(error, ValueError):
        detail = str(error)
    else:
        detail = type(error).__name__
    return stage + " · " + detail

def collect():
    global status
    site = ""
    while True:
        stage = "Anmeldung"
        try:
            access_token()
            stage = "Site-Auswahl"
            site = site or select_site()
            path = root + "/sites/" + quote(site, safe="")
            stage = "Access Points"
            devices = rows(path + "/devices")
            aps = {mac(d["mac"]): (d.get("name") or d["mac"], str(d.get("status", "?")))
                   for d in devices if str(d.get("type", "")).lower() in {"ap", "eap"}}
            stage = "WLAN-Clients"
            clients = {mac(c["mac"]): (c.get("name") or c.get("hostName") or c["mac"], mac(c["apMac"]))
                       for c in rows(path + "/clients") if c.get("apMac") and c.get("active") is not False}
            stage = "Historie"
            now = int(time.time())
            with closing(sqlite3.connect(database, timeout=15)) as db, db:
                previous = {(k, i): (n, v) for k, i, n, v in db.execute("SELECT * FROM current")}
                current = {("AP", i): data for i, data in aps.items()}
                current.update({("Client", i): data for i, data in clients.items()})
                changes = []
                for (kind, ident), (name, value) in current.items():
                    old = previous.get((kind, ident))
                    if old and old[1] == value:
                        continue
                    if kind == "AP":
                        ap = ident
                        detail = (ap_status(old[1]) + " → " if old else "") + ap_status(value)
                    else:
                        ap = value
                        target = aps.get(ap, (ap,))[0]
                        origin = aps.get(old[1], (old[1],))[0] if old else None
                        detail = origin + " → " + target if origin else "Erfasst an " + target
                    changes.append((now, kind, ident, name, ap, detail))
                for (kind, ident), (name, value) in previous.items():
                    if (kind, ident) not in current:
                        ap = value if kind == "Client" else ident
                        changes.append((now, kind, ident, name, ap, "Nicht mehr in der API-Liste"))
                db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?)", changes)
                db.execute("DELETE FROM events WHERE ts < ?", (now - 86400,))
                db.execute("DELETE FROM current")
                db.executemany("INSERT INTO current VALUES (?,?,?,?)",
                               [(k, i, n, v) for (k, i), (n, v) in current.items()])
            status = "Letzte erfolgreiche Abfrage " + datetime.fromtimestamp(now).isoformat(sep=" ")
        except Exception as error:
            auth_state.valid_until = 0
            status = failure(stage, error)
            print(status, flush=True)
        time.sleep(30)

def cell(value):
    return "<td>" + escape(str(value)) + "</td>"

def wlan_view(params):
    query = params.get("q", [""])[0][:150]
    since = int(time.time()) - 86400
    with closing(sqlite3.connect(database, timeout=15)) as db:
        aps = list(db.execute("""
            SELECT a.id, a.name, a.value,
                   (SELECT COUNT(*) FROM current c WHERE c.kind='Client' AND c.value=a.id),
                   (SELECT COUNT(*) FROM events e WHERE e.ap=a.id AND e.ts>=?
                    AND e.kind='Client' AND e.detail='Nicht mehr in der API-Liste')
            FROM current a WHERE a.kind='AP' ORDER BY a.name
        """, (since,)))
        events = list(db.execute("""
            SELECT e.ts,e.kind,e.id,coalesce(c.name,e.name),e.ap,e.detail
            FROM events e
            LEFT JOIN current c ON c.kind=e.kind AND c.id=e.id
            LEFT JOIN current a ON a.kind='AP' AND a.id=e.ap
            WHERE e.ts>=? AND instr(lower(e.name || ' ' || e.id || ' ' || e.ap ||
            ' ' || coalesce(c.name, '') || ' ' || coalesce(a.name, '')), lower(?))>0
            ORDER BY e.ts DESC, e.rowid DESC LIMIT 300
        """, (since, query)))
    ap_rows = "".join("<tr><td><a href='/wlan?" + urlencode({"q": i}) + "'>" +
                      escape(n) + "</a></td>" + cell(ap_status(v)) + cell(c) + cell(d) + "</tr>"
                      for i, n, v, c, d in aps)
    event_rows = "".join(
        f'<tr class="event-{"offline" if d == "Nicht mehr in der API-Liste" else "ap" if k == "AP" else "client"}">'
        + "".join(map(cell, (
            datetime.fromtimestamp(t, timezone.utc).strftime("%d.%m. %H:%M:%S"), k, n, i, d
        ))) + "</tr>" for t, k, i, n, a, d in events
    )
    return render("wlan", status=escape(status), query=escape(query),
                  ap_rows=ap_rows or '<tr><td colspan="4">Keine AP-Daten</td></tr>',
                  event_rows=event_rows or '<tr><td colspan="5">Keine passenden Ereignisse</td></tr>')

def local_timestamp(value):
    local = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    first = local.replace(tzinfo=display_zone, fold=0)
    second = local.replace(tzinfo=display_zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("Diese Uhrzeit liegt in der Zeitumstellung. Wähle eine eindeutige Zeit.")
    return int(first.timestamp())

def format_bytes(value):
    units = ((10**12, "TB"), (10**9, "GB"), (10**6, "MB"), (10**3, "kB"), (1, "B"))
    for factor, unit in units:
        if value >= factor or factor == 1:
            amount = f"{value / factor:,.2f}".translate(str.maketrans(",.", ".,"))
            return amount + " " + unit

def wan_gateways():
    site_path = root + "/sites/" + quote(select_site(), safe="")
    gateways = [device for device in rows(site_path + "/devices")
                if str(device.get("type", "")).lower() == "gateway"]
    if not gateways:
        raise ValueError("Für diese Site meldet Omada kein verwaltetes Gateway.")
    for gateway in gateways:
        address = quote(mac(gateway["mac"]).replace(":", "-"), safe="")
        ports = {
            port["port"]: port.get("name") or str(port["port"])
            for port in request(f"{site_path}/gateways/{address}/wan-status")
            if port.get("type") == 0 or (port.get("type") == 1 and port.get("mode") == 0)
        }
        if ports:
            stats = site_path.replace("/v1/", "/v2/", 1) + "/stat/" + address
            yield gateway.get("name") or gateway["mac"], stats, ports


def month_start(value):
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def day_start(timestamp):
    return datetime.fromtimestamp(timestamp, display_zone).replace(hour=0, minute=0, second=0, microsecond=0)


def archive_range_points(start, end):
    lo = int(day_start(start).timestamp())
    hi = int((day_start(end) + timedelta(days=1)).timestamp())
    with closing(sqlite3.connect(database, timeout=15)) as db:
        return db.execute("""
            SELECT gateway_id, port, resolution, timestamp, gateway_name, port_name, rx, tx
            FROM wan_points WHERE timestamp >= ? AND timestamp < ?
            ORDER BY timestamp
        """, (lo, hi)).fetchall()


def aggregate_wan(points, start, end):
    by_port = {}
    for gateway, port, resolution, timestamp, name, port_name, rx, tx in points:
        if resolution == "daily":
            midnight = day_start(timestamp)
            lo = int(midnight.timestamp())
            hi = int((midnight + timedelta(days=1)).timestamp())
        else:
            lo = timestamp
            hi = lo + (3600 if resolution == "hourly" else 300)
        if start <= lo < hi <= end:
            by_port.setdefault((gateway, port), {}).setdefault((lo, hi), (name, port_name, rx, tx))
    totals = {}
    for port, intervals in by_port.items():
        occupied = set()
        received = sent = 0
        for (lo, hi), (name, port_name, rx, tx) in sorted(
            intervals.items(), key=lambda item: (item[0][0] - item[0][1], item[0][0])
        ):
            slots = range(lo, hi, 300)
            if occupied.isdisjoint(slots):
                occupied.update(slots)
                received += rx
                sent += tx
        totals[port] = (name, port_name, received, sent, len(occupied))
    return totals


def range_wan_totals(start, end):
    points = archive_range_points(start, end)
    current = int(month_start(datetime.now(display_zone)).timestamp())
    live_error = ""
    if end > current:
        gateways = None
        begin = day_start(max(start, current))
        while begin.timestamp() < end:
            following = begin + timedelta(days=1)
            lo = max(start, int(begin.timestamp()))
            hi = min(end, int(following.timestamp()))
            local = aggregate_wan(points, lo, hi)
            known_ports = {(point[0], point[1]) for point in points if point[3] >= current}
            if not known_ports or any(local.get(port, ("", "", 0, 0, 0))[4] < (hi - lo) // 300
                                      for port in known_ports):
                try:
                    if gateways is None:
                        gateways = list(wan_gateways())
                        if not gateways:
                            raise ValueError("Omada meldet keine Ports im WAN-Modus.")
                    # A Tageswert is only eligible for a complete, finished local day.
                    resolutions = ("daily", "hourly", "5min") if (
                        lo == int(begin.timestamp()) and hi == int(following.timestamp())
                    ) else ("hourly", "5min")
                    for resolution in resolutions:
                        port_ids = {(stats, index) for _, stats, ports in gateways for index in ports}
                        covered = aggregate_wan(points, lo, hi)
                        if port_ids and all(covered.get(port, ("", "", 0, 0, 0))[4] == (hi-lo)//300
                                            for port in port_ids):
                            break
                        points.extend((gateway, port, resolution, timestamp, name, port_name, rx, tx)
                                      for gateway, port, timestamp, name, port_name, rx, tx in
                                      wan_month_points(gateways, begin, datetime.fromtimestamp(hi, display_zone), resolution))
                except Exception as error:
                    live_error = failure("Live-Abfrage", error)
                    break
            begin = following
    totals = aggregate_wan(points, start, end)
    # Preserve zero coverage for a port seen in the requested period.
    for gateway, port, timestamp_type, timestamp, name, port_name, rx, tx in points:
        if start <= timestamp < end:
            totals.setdefault((gateway, port), (name, port_name, 0, 0, 0))
    return list(totals.values()), live_error


def internet_view(params):
    end_default = int(time.time()) // 300 * 300
    local = lambda ts: datetime.fromtimestamp(ts, display_zone).strftime("%Y-%m-%dT%H:%M")
    start_text = params.get("start", [local(end_default - 86400)])[0]
    end_text = params.get("end", [local(end_default)])[0]
    form = render("traffic-form", start=escape(start_text), end=escape(end_text),
                  timezone=escape(display_zone.key))
    if "start" not in params or "end" not in params:
        return form
    try:
        start, end = local_timestamp(start_text), local_timestamp(end_text)
        if not (start < end <= time.time()):
            raise ValueError("Wähle einen vergangenen Zeitraum mit Ende nach dem Start.")
        if start % 300 or end % 300:
            raise ValueError("Richte Start und Ende am 5-Minuten-Raster aus.")
        results, live_error = range_wan_totals(start, end)
    except Exception as error:
        return form + '<p role="status">' + escape(failure("WAN-Auswertung", error)) + "</p>"
    expected = (end - start) // 300
    with closing(sqlite3.connect(database, timeout=15)) as db:
        worker = db.execute("SELECT status FROM worker_activity ORDER BY id DESC LIMIT 1").fetchone()
    importing = bool(worker and worker[0] == "Läuft")
    if not any(count for _, _, _, _, count in results):
        message = "WAN-Import läuft. Für diesen Zeitraum liegen noch keine Messwerte vor." if importing else "Für diesen Zeitraum sind keine WAN-Messwerte verfügbar."
        return form + '<p role="status">' + escape(live_error or message) + "</p>"
    partial = any(count < expected for _, _, _, _, count in results)
    rx_total = sum(rx for _, _, rx, _, _ in results)
    tx_total = sum(tx for _, _, _, tx, _ in results)
    rows_html = "".join("<tr>" + "".join(map(cell, (
        gateway, port, format_bytes(rx) if count else "–", format_bytes(tx) if count else "–",
        f"{count} / {expected}",
    ))) + "</tr>" for gateway, port, rx, tx, count in results)
    coverage = "<p>Messwerte fehlen. Die Summen enthalten nur die verfügbaren Intervalle.</p>" if partial else ""
    if partial and importing:
        coverage += "<p>Der WAN-Import läuft noch.</p>"
    if live_error:
        coverage += "<p>" + escape(live_error) + "</p>"
    return form + render("traffic-results", coverage=coverage, received=format_bytes(rx_total),
                         sent=format_bytes(tx_total), total=format_bytes(rx_total + tx_total),
                         rows=rows_html)

def wan_month_points(gateways, month, end, resolution="daily"):
    start_ts, end_ts = int(month.timestamp()), int(end.timestamp())
    if end <= month:
        return
    for gateway, stats, ports in gateways:
        points = request(f"{stats}/{resolution}?type=gateway", {
            "start": start_ts, "end": end_ts - 1,
            "attrs": ["rx", "tx"], "ports": list(ports),
        })
        if not isinstance(points, list):
            raise ValueError("Omada lieferte ein unerwartetes WAN-Statistikformat.")
        for point in points:
            timestamp = point.get("time")
            if type(timestamp) is not int or not start_ts <= timestamp < end_ts:
                continue
            for entry in point.get("ports", []):
                index, rx, tx = entry.get("port"), entry.get("rx"), entry.get("tx")
                if index in ports and all(type(n) is int and 0 <= n < 2**63 for n in (rx, tx)):
                    yield stats, index, timestamp, gateway, ports[index], rx, tx


def wan_calendar_days(points):
    days = {}
    for gateway_id, port, timestamp, name, port_name, rx, tx in points:
        day = datetime.fromtimestamp(timestamp, display_zone).day
        days.setdefault(day, {})[(gateway_id, port)] = (name, port_name, rx, tx)
    return days


def archived_wan_month(month, end):
    start, stop = int(month.timestamp()), int(end.timestamp())
    points = archive_range_points(start, stop)
    port_ids = {(p[0], p[1]) for p in points if start <= p[3] < stop}
    days, complete_days = {}, set()
    day = month
    # Group once by local date so every day only processes its own measurements.
    grouped = {}
    for point in points:
        grouped.setdefault(datetime.fromtimestamp(point[3], display_zone).date(), []).append(point)
    while day < end:
        following = day + timedelta(days=1)
        lo, hi = int(day.timestamp()), int(following.timestamp())
        totals = aggregate_wan(grouped.get(day.date(), []), lo, hi)
        if totals:
            days[day.day] = {port: data[:4] for port, data in totals.items()}
            if all(totals.get(port, ("", "", 0, 0, 0))[4] == (hi - lo) // 300 for port in port_ids):
                complete_days.add(day.day)
        day = following
    return days, len(port_ids), complete_days


def wan_retention():
    defaults = {"5min": 7, "hourly": 31, "daily": 366}
    for path in ("/retention", "/controller/setting/retention"):
        try:
            values = request(root + path)
        except (HTTPError, ValueError):
            continue
        if not isinstance(values, dict):
            continue
        reported = {resolution: values.get(field) for resolution, field in
                    (("5min", "fiveMin"), ("hourly", "hourly"), ("daily", "daily"))}
        for resolution, days in reported.items():
            if type(days) is int and days > 0:
                defaults[resolution] = days
        break
    return defaults


def save_wan_points(gateways, resolution, start, end, probe=False):
    points = [
        (gateway, port, resolution, timestamp, gateway_name, port_name, rx, tx)
        for gateway, port, timestamp, gateway_name, port_name, rx, tx in
        wan_month_points(gateways, start, end, resolution)
    ]
    with closing(sqlite3.connect(database, timeout=15)) as db, db:
        db.executemany("""
            INSERT INTO wan_points VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gateway_id, port, resolution, timestamp) DO NOTHING
        """, points)
        inserted = db.total_changes
        if probe:
            db.execute("""
                INSERT INTO wan_probe VALUES (?, ?, ?, ?)
                ON CONFLICT(resolution, start, end) DO UPDATE SET points=excluded.points
            """, (resolution, int(start.timestamp()), int(end.timestamp()), len(points)))
        return inserted


def regular_wan_windows(resolution, end, retention_days):
    start = end - timedelta(days=retention_days)
    cursor = end
    batch_days = 31 if resolution == "daily" else 1
    while cursor > start:
        begin = max(start, cursor - timedelta(days=batch_days))
        yield begin, cursor
        cursor = begin


def historical_wan_windows(today):
    start = month_start(today - timedelta(days=366))
    configured = env.get("WAN_ARCHIVE_START", "")
    if configured:
        start = min(start, datetime.strptime(configured, "%Y-%m").replace(tzinfo=display_zone))
    with closing(sqlite3.connect(database, timeout=15)) as db:
        earliest = db.execute("SELECT MIN(timestamp) FROM wan_points").fetchone()[0]
        checked = set(db.execute("SELECT resolution, start, end FROM wan_probe"))
    if earliest is not None:
        start = min(start, month_start(day_start(earliest)))
    # Use stable day/month boundaries so interrupted imports resume at unqueried windows.
    for resolution in ("daily", "hourly", "5min"):
        end = month_start(today) if resolution == "daily" else today
        while end > start:
            previous_day = end - timedelta(days=1)
            begin = month_start(previous_day) if resolution == "daily" else previous_day
            if (resolution, int(begin.timestamp()), int(end.timestamp())) not in checked:
                yield resolution, begin, end
            end = begin


def worker_event(run, state, detail, inserted):
    with closing(sqlite3.connect(database, timeout=15)) as db, db:
        if run is None:
            db.execute("""
                UPDATE worker_activity SET status='Unterbrochen', finished=?
                WHERE finished IS NULL
            """, (int(time.time()),))
            return db.execute("""
                INSERT INTO worker_activity (started, status, detail, inserted, version, build)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (int(time.time()), state, detail, inserted, app_version, app_build)).lastrowid
        db.execute("""
            UPDATE worker_activity SET status=?, detail=?, inserted=?, finished=? WHERE id=?
        """, (state, detail, inserted, None if state == "Läuft" else int(time.time()), run))
        db.execute("""
            DELETE FROM worker_activity WHERE id NOT IN
            (SELECT id FROM worker_activity ORDER BY id DESC LIMIT 30)
        """)


def archive_wan_history():
    with closing(sqlite3.connect(database, timeout=15)) as db, db:
        db.execute("INSERT OR REPLACE INTO worker_runtime VALUES (1, ?, ?)", (app_version, app_build))
        db.execute("UPDATE worker_activity SET status='Unterbrochen', finished=? WHERE finished IS NULL",
                   (int(time.time()),))
    while True:
        run = None
        inserted = 0
        try:
            now = datetime.now(display_zone)
            today = day_start(now.timestamp())
            scheduled = today.replace(hour=2)
            if now.timestamp() < scheduled.timestamp():
                scheduled -= timedelta(days=1)
            with closing(sqlite3.connect(database, timeout=15)) as db:
                finished = dict(db.execute("SELECT resolution, finished_at FROM wan_archive_runs"))
            due = [r for r in ("5min", "hourly", "daily") if finished.get(r, 0) < scheduled.timestamp()]
            pending = list(historical_wan_windows(today))
            if due or pending:
                run = worker_event(None, "Läuft", "Verbinde mit Omada", 0)
                gateways = list(wan_gateways())
                if not gateways:
                    raise ValueError("Omada meldet keine Ports im WAN-Modus.")
                retention = wan_retention() if due else {}
                errors = {}
                for resolution in due:
                    try:
                        cutoff = scheduled.replace(hour=0) if resolution in finished else today
                        for begin, end in regular_wan_windows(resolution, cutoff, retention[resolution]):
                            worker_event(run, "Läuft", f"{resolution} · {begin:%d.%m.%Y}–{end:%d.%m.%Y}", inserted)
                            inserted += save_wan_points(gateways, resolution, begin, end, probe=True)
                        with closing(sqlite3.connect(database, timeout=15)) as db, db:
                            db.execute("""
                                INSERT INTO wan_archive_runs VALUES (?, ?)
                                ON CONFLICT(resolution) DO UPDATE SET finished_at=excluded.finished_at
                            """, (resolution, int(scheduled.timestamp())))
                    except Exception as error:
                        errors[resolution] = failure(resolution, error)
                for resolution, begin, end in historical_wan_windows(today):
                    if resolution in errors:
                        continue
                    try:
                        worker_event(run, "Läuft", f"Historie · {resolution} · {begin:%d.%m.%Y}–{end:%d.%m.%Y}", inserted)
                        inserted += save_wan_points(gateways, resolution, begin, end, probe=True)
                        time.sleep(.05)
                    except Exception as error:
                        errors[resolution] = failure(f"Historie {resolution}", error)
                state = "Fehler" if errors else "Erfolgreich"
                detail = " · ".join(errors.values()) if errors else "Import abgeschlossen"
                worker_event(run, state, detail, inserted)
                print(f"WAN-Archiv: {inserted} neue Messpunkte · {detail}", flush=True)
        except Exception as error:
            detail = failure("WAN-Archiv", error)
            if run is None:
                run = worker_event(None, "Läuft", detail, inserted)
            worker_event(run, "Fehler", detail, inserted)
            print(detail, flush=True)
        now = datetime.now(display_zone)
        next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now.timestamp() >= next_run.timestamp():
            next_run += timedelta(days=1)
        time.sleep(min(900, max(1, next_run.timestamp() - now.timestamp())))


def calendar_summary(days, month, end, port_count, complete_days=None):
    expected = max(0, (end.date() - month.date()).days)
    if not days:
        return f"Keine Daten · 0 / {expected} Tage"
    total = sum(rx + tx for ports in days.values() for _, _, rx, tx in ports.values())
    complete = (sum(len(ports) == port_count for ports in days.values())
                if complete_days is None else len(complete_days))
    return f"{format_bytes(total)} · {complete} / {expected} Tage vollständig"


def render_calendar(month, current_month, results):
    names = ("Januar", "Februar", "März", "April", "Mai", "Juni",
             "Juli", "August", "September", "Oktober", "November", "Dezember")
    months = "".join(
        f'<option value="{number:02d}"' + (" selected" if number == month.month else "")
        + f'>{name}</option>' for number, name in enumerate(names, 1)
    )
    years = "".join(
        f'<option value="{year}"' + (" selected" if year == month.year else "")
        + f'>{year}</option>' for year in range(current_month.year, 1969, -1)
    )
    return render("wan-calendar", months=months, years=years, results=results)


def wan_calendar(params):
    now = datetime.now(display_zone)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    current_month = today.replace(day=1)
    default = (current_month - timedelta(days=1)).replace(day=1)
    month = default
    try:
        try:
            if "year" in params:
                month_number = int(params.get("month", [""])[0])
                year = int(params["year"][0])
                selected = current_month.replace(year=year, month=month_number)
            else:
                month_text = params.get("month", [default.strftime("%Y-%m")])[0]
                selected = datetime.strptime(month_text, "%Y-%m").replace(tzinfo=display_zone)
            if selected.year < 1970 or selected > current_month:
                raise ValueError
        except ValueError:
            raise ValueError("Wähle einen gültigen Monat bis zum aktuellen Monat.") from None
        month = selected
        previous = (month - timedelta(days=1)).replace(day=1)
        next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = min(next_month, today)
        complete_days = None
        if month == current_month:
            gateways = list(wan_gateways())
            if not gateways:
                raise ValueError("Omada meldet keine Ports im WAN-Modus.")
            days = wan_calendar_days(wan_month_points(gateways, month, end))
            port_count = sum(len(ports) for _, _, ports in gateways)
        else:
            days, port_count, complete_days = archived_wan_month(month, end)
        previous_days, previous_port_count, previous_complete = archived_wan_month(previous, month)
    except Exception as error:
        message = escape(failure("Verbrauchskalender", error))
        return render_calendar(month, current_month, f'<p role="status">{message}</p>')

    current_day_rows, today_label = {}, ""
    if month == current_month:
        today_label = "noch keine Messwerte"
        cutoff = int(now.timestamp()) // 300 * 300
        start = int(today.timestamp())
        if cutoff > start:
            try:
                points = [
                    (gateway, port, "5min", timestamp, name, port_name, rx, tx)
                    for gateway, port, timestamp, name, port_name, rx, tx in wan_month_points(
                        gateways, today, datetime.fromtimestamp(cutoff, display_zone), "5min"
                    )
                    if start <= timestamp <= cutoff - 300 and timestamp % 300 == 0
                ]
                current_day_rows = aggregate_wan(points, start, cutoff)
                if current_day_rows:
                    volume = sum(row[2] + row[3] for row in current_day_rows.values())
                    latest = datetime.fromtimestamp(max(row[3] for row in points) + 300, display_zone)
                    today_label = f"{format_bytes(volume)} · bis {latest:%H:%M}"
            except Exception as error:
                today_label = failure("Live-Daten nicht verfügbar", error)

    totals = {day: sum(rx + tx for _, _, rx, tx in ports.values()) for day, ports in days.items()}
    peak = max(totals.values(), default=0)
    cells = ['<span aria-hidden="true"></span>'] * month.weekday()
    for day in range(1, monthrange(month.year, month.month)[1] + 1):
        if today_label and day == today.day:
            label = f'<b>{day} · Heute</b><small>{escape(today_label)}</small>'
            if current_day_rows:
                query = urlencode({"month": month.strftime("%Y-%m"), "day": day})
                cells.append(f'<a href="?{query}#day-detail" class="calendar-cell today">{label}</a>')
            else:
                cells.append(f'<span class="calendar-cell today">{label}</span>')
            continue
        value = totals.get(day)
        if value is None:
            label = "Offen" if month.replace(day=day) >= end else "Keine Daten"
            cells.append(f'<span class="calendar-cell missing"><b>{day}</b><small>{label}</small></span>')
            continue
        level = min(4, 1 + 4 * value // peak) if value and peak else 0
        query = urlencode({"month": month.strftime("%Y-%m"), "day": day})
        subtotal = format_bytes(value)
        if len(days[day]) < port_count or (complete_days is not None and day not in complete_days):
            subtotal += " · teilweise"
        cells.append(f'<a href="?{query}#day-detail" class="calendar-cell level-{level}">'
                     f'<b>{day}</b><small>{subtotal}</small></a>')
    selected = params.get("day", [""])[0]
    selected_day = int(selected) if len(selected) <= 2 and selected.isascii() and selected.isdecimal() else 0
    selected_today = bool(today_label and selected_day == today.day)
    selected_rows = current_day_rows if selected_today else days.get(selected_day, {})
    detail_rows = "".join("<tr>" + "".join(map(cell, (
        gateway, port, format_bytes(rx), format_bytes(tx), format_bytes(rx + tx),
    ))) + "</tr>" for gateway, port, rx, tx, *_ in selected_rows.values())
    results = render("wan-calendar-results", cells="".join(cells),
                     current_month=month.strftime("%m/%Y"), previous_month=previous.strftime("%m/%Y"),
                     current_summary=calendar_summary(days, month, end, port_count, complete_days),
                     previous_summary=calendar_summary(previous_days, previous, month, previous_port_count, previous_complete),
                     detail_heading=f"{selected_day:02d}.{month:%m.%Y}" if detail_rows else "",
                     detail_rows=detail_rows, detail_hidden="" if detail_rows else " hidden",
                     today_summary=f"<small>Heute bisher: {escape(today_label)}</small>" if today_label else "",
                     day_hint="<p>Vorläufiger Tageswert aus verfügbaren 5-Minuten-Intervallen.</p>"
                     if selected_today and detail_rows else "",
                     timezone=escape(display_zone.key))
    return render_calendar(month, current_month, results)


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True

def display_name():
    with closing(sqlite3.connect(database, timeout=15)) as db:
        row = db.execute("SELECT display_name FROM app_settings WHERE id=1").fetchone()
    return row[0] if row else ""


def favicon():
    with closing(sqlite3.connect(database, timeout=15)) as db:
        row = db.execute("""
            SELECT coalesce((SELECT favicon FROM app_settings WHERE id=1),
                            (SELECT logo_icon FROM branding WHERE id=1))
        """).fetchone()
    return row[0] or (app_dir / "assets" / "favicon.ico").read_bytes()


def favicon_version():
    return hashlib.sha256(favicon()).hexdigest()[:16]


def render_layout(content, path, authenticated=False, login_message=""):
    tabs = [("Internet Traffic", "/"), ("WLAN Detektiv", "/wlan")]
    if authenticated:
        tabs.extend([("WLAN & VLANs", "/wlan-vlans"), ("Admin", "/admin")])
    selected = "/" if path == "/wan-calendar" else path
    links = []
    for label, href in tabs:
        style = ' class="protected-tab"' if href in {"/admin", "/wlan-vlans"} else ""
        active = ' aria-current="page"' if href == selected else ""
        links.append(f'<a href="{href}"{style}{active}>{escape(label)}</a>')
    account = ""
    if authenticated:
        account = render("logout", csrf=wlan_csrf)
    elif admin_password:
        account = render("login", csrf=login_csrf,
                         message=f'<small role="status">{escape(login_message)}</small>' if login_message else "")
    return render("layout", app_name=escape(display_name() or "omada-inside"),
                  favicon_version=favicon_version(), tabs="".join(links), account=account, content=content)


def admin_view():
    now = datetime.now(display_zone)
    next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)
    if now.timestamp() >= next_run.timestamp():
        next_run += timedelta(days=1)
    months = [month_start(now)]
    for _ in range(11):
        months.append(month_start(months[-1] - timedelta(days=1)))
    months.reverse()
    ends = months[1:] + [(months[-1].replace(day=28) + timedelta(days=4)).replace(day=1)]
    with closing(sqlite3.connect(database, timeout=15)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        migration = db.execute(
            "SELECT version, migrated_at, previous_version FROM schema_history ORDER BY version DESC LIMIT 1"
        ).fetchone()
        worker = db.execute("SELECT version, build FROM worker_runtime WHERE id = 1").fetchone()
        size = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
        summaries = dict((r, (count, first, last)) for r, count, first, last in db.execute(
            "SELECT resolution, COUNT(*), MIN(timestamp), MAX(timestamp) FROM wan_points GROUP BY resolution"
        ))
        probes = list(db.execute("SELECT resolution, start, end FROM wan_probe ORDER BY start"))
        runs = list(db.execute("""
            SELECT started, finished, status, detail, inserted, version, build
            FROM worker_activity ORDER BY id DESC LIMIT 8
        """))
        counts = {}
        for resolution, timestamp in db.execute("SELECT resolution, timestamp FROM wan_points WHERE timestamp >= ?",
                                                (int(months[0].timestamp()),)):
            clock = datetime.fromtimestamp(timestamp, display_zone)
            key = resolution, clock.year, clock.month
            counts[key] = counts.get(key, 0) + 1

    def stamp(value):
        return datetime.fromtimestamp(value, display_zone).strftime("%d.%m.%Y %H:%M") if value is not None else "–"

    coverage_rows, summary_rows = [], []
    for resolution, label in (("5min", "5 Minuten"), ("hourly", "Stunde"), ("daily", "Tag")):
        count, first, last = summaries.get(resolution, (0, None, None))
        summary_rows.append("<tr>" + "".join(map(cell, (label, count, stamp(first), stamp(last)))) + "</tr>")
        tiles = []
        for month, end in zip(months, ends):
            items = counts.get((resolution, month.year, month.month), 0)
            lo, hi = int(month.timestamp()), int(end.timestamp())
            checked_until, touched = lo, False
            for kind, start, stop in probes:
                if kind == resolution and stop > lo and start < hi:
                    touched = True
                    if start <= checked_until:
                        checked_until = max(checked_until, stop)
            if items:
                style, name = "present", f"{items} Messpunkte"
            elif checked_until >= hi:
                style, name = "empty", "Abgefragt, keine Werte"
            elif touched:
                style, name = "partial", "Teilweise abgefragt"
            else:
                style, name = "unknown", "Noch nicht abgefragt"
            tiles.append(f'<span class="coverage-tile {style}" title="{month:%m/%Y} · {name}">'
                         f'<span class="sr-only">{month:%m/%Y} · {name}</span></span>')
        coverage_rows.append(f"<b>{label}</b>" + "".join(tiles))
    run_rows = "".join("<tr>" + "".join(map(cell, (
        stamp(started), f"{finished - started} s" if finished is not None else "–", state, count, detail,
    ))) + "</tr>" for started, finished, state, detail, count, _, _ in runs)
    latest = runs[0] if runs else None
    return render("admin", display_name=escape(display_name()), csrf=wlan_csrf,
                  favicon_version=favicon_version(),
                  app_version=escape(f"{app_version} · {app_build[:12]}"),
                  worker=escape(f"{worker[0]} · {worker[1][:12]}" if worker else "Noch nicht gemeldet"),
                  schema=f"{version} / {schema_version}",
                  migration=escape(f"{migration[2]} → {migration[0]} · {stamp(migration[1])}" if migration else "–"),
                  size=format_bytes(size), point_count=str(sum(row[0] for row in summaries.values())),
                  next_run=escape(next_run.strftime("%d.%m.%Y · %H:%M") + " · " + display_zone.key),
                  worker_status=escape(latest[2] if latest else "Noch kein Lauf"),
                  worker_class="warning" if latest and latest[2] in {"Fehler", "Unterbrochen"} else "",
                  worker_detail=escape(latest[3] if latest else "Der Worker hat noch keinen Import gestartet."),
                  last_run=stamp(latest[0] if latest else None),
                  months="".join(f"<small>{m:%m/%y}</small>" for m in months),
                  coverage_rows="".join(coverage_rows), summary_rows="".join(summary_rows),
                  run_rows=run_rows or '<tr><td colspan="5">Noch keine Importläufe</td></tr>')


def respond(start_response, status, body, content_type="text/plain; charset=utf-8", headers=()):
    start_response(status, [
        ("Content-Type", content_type),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Content-Security-Policy", "frame-ancestors 'none'"),
        *headers,
    ])
    return [body]


def session_token(environ):
    cookie = SimpleCookie()
    try:
        cookie.load(environ.get("HTTP_COOKIE", ""))
    except CookieError:
        return ""
    return cookie[session_name].value if session_name in cookie else ""


def admin_authenticated(environ):
    with session_lock:
        return bool(admin_password and sessions.get(session_token(environ), 0) > time.monotonic())


def session_cookie(environ, token, max_age):
    secure = (environ.get("wsgi.url_scheme") == "https"
              or environ.get("HTTP_X_FORWARDED_PROTO", "").lower() == "https")
    return (f"{session_name}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"
            + ("; Secure" if secure else ""))


def account_action(environ, start_response, logout):
    if not admin_password:
        return respond(start_response, "404 Not Found", b"Anmeldung deaktiviert.")
    if environ["REQUEST_METHOD"] != "POST":
        return respond(start_response, "405 Method Not Allowed", b"POST erforderlich.",
                       headers=(("Allow", "POST"),))
    try:
        size = int(environ.get("CONTENT_LENGTH") or "0")
        if not 0 < size <= 8192:
            raise ValueError
        form = parse_qs(environ["wsgi.input"].read(size).decode(), max_num_fields=3)
    except (ValueError, UnicodeError):
        return respond(start_response, "400 Bad Request", b"Ungueltige Anmeldung.")
    token = form.get("csrf", [""])[0]
    if not compare_digest(token.encode(), (wlan_csrf if logout else login_csrf).encode()):
        return respond(start_response, "403 Forbidden", b"Bitte Seite neu laden.")
    previous = session_token(environ)
    with session_lock:
        if logout:
            sessions.pop(previous, None)
            cookie = session_cookie(environ, "", 0)
        else:
            now = time.monotonic()
            login_failures[:] = [stamp for stamp in login_failures if stamp > now - 60]
            if len(login_failures) >= 8:
                return respond(start_response, "429 Too Many Requests",
                               b"Zu viele Anmeldeversuche. Bitte warte eine Minute.",
                               headers=(("Retry-After", "60"),))
            valid = compare_digest(form.get("password", [""])[0].encode(), admin_password)
            if form.get("username", [""])[0] != "admin" or not valid:
                login_failures.append(now)
                return respond(start_response, "303 See Other", b"", headers=(("Location", "/?login=failed"),))
            for key, expiry in list(sessions.items()):
                if expiry <= now or key == previous:
                    del sessions[key]
            if len(sessions) >= 64:
                del sessions[next(iter(sessions))]
            token = token_urlsafe(32)
            sessions[token] = now + session_lifetime
            cookie = session_cookie(environ, token, session_lifetime)
    return respond(start_response, "303 See Other", b"",
                   headers=(("Location", "/" if logout else "/wlan-vlans"), ("Set-Cookie", cookie)))


def save_display_name(environ, start_response):
    try:
        size = int(environ.get("CONTENT_LENGTH") or "0")
        if not 0 < size <= 4096:
            raise ValueError
        form = parse_qs(environ["wsgi.input"].read(size).decode(), keep_blank_values=True, max_num_fields=2)
        name = form.get("display_name", [""])[0].strip()
        if len(name) > 80 or (name and not name.isprintable()):
            raise ValueError
    except (ValueError, UnicodeError):
        return respond(start_response, "400 Bad Request", b"Ungueltiger Anzeigename.")
    if not compare_digest(form.get("csrf", [""])[0].encode(), wlan_csrf.encode()):
        return respond(start_response, "403 Forbidden", b"Bitte Seite neu laden.")
    with closing(sqlite3.connect(database, timeout=15)) as db, db:
        db.execute("""
            INSERT INTO app_settings (id, display_name) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name
        """, (name,))
    return respond(start_response, "303 See Other", b"", headers=(("Location", "/admin#appearance"),))


def render_favicon(png):
    with Image.open(BytesIO(png), formats=["PNG"]) as image:
        if not (0 < image.width <= 4096 and 0 < image.height <= 4096):
            raise ValueError("Ungültige Bildgröße")
        icon = image.convert("RGBA")
        target = tuple(max(1, round(dimension * 32 / max(icon.size))) for dimension in icon.size)
        icon = icon.resize(target, Image.Resampling.LANCZOS)
        square = Image.new("RGBA", (32, 32))
        square.paste(icon, ((32 - icon.width) // 2, (32 - icon.height) // 2))
        output = BytesIO()
        square.save(output, format="ICO", sizes=[(16, 16), (32, 32)])
        return output.getvalue()


def save_brand_image(environ, start_response, kind):
    if environ.get("HTTP_X_OMADA_UPLOAD") != kind:
        return respond(start_response, "403 Forbidden", b"Upload-Header fehlt.")
    if environ.get("CONTENT_TYPE", "").lower() != "image/png":
        return respond(start_response, "415 Unsupported Media Type", b"Bitte ein PNG hochladen.")
    try:
        size = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError:
        return respond(start_response, "400 Bad Request", b"Dateigroesse ungueltig.")
    if size > logo_size_limit:
        return respond(start_response, "413 Payload Too Large", b"Das PNG darf maximal 2 MiB gross sein.")
    if size < 45:
        return respond(start_response, "400 Bad Request", b"Ungueltiges PNG.")
    png = environ["wsgi.input"].read(size)
    try:
        if len(png) != size:
            raise ValueError
        icon = render_favicon(png)
    except (ValueError, OSError, Image.DecompressionBombError):
        return respond(start_response, "400 Bad Request", b"PNG ungueltig oder groesser als 4096 x 4096 Pixel.")
    with closing(sqlite3.connect(database, timeout=15)) as db, db:
        if kind == "logo":
            db.execute("""
                INSERT INTO branding (id, png, logo_icon) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET png=excluded.png, logo_icon=excluded.logo_icon
            """, (png, icon))
        else:
            db.execute("""
                INSERT INTO app_settings (id, display_name, favicon) VALUES (1, '', ?)
                ON CONFLICT(id) DO UPDATE SET favicon=excluded.favicon
            """, (icon,))
    return respond(start_response, "200 OK", b"Bild gespeichert.")


def reset_favicon(environ, start_response):
    if environ.get("HTTP_X_OMADA_UPLOAD") != "favicon":
        return respond(start_response, "403 Forbidden", b"Upload-Header fehlt.")
    with closing(sqlite3.connect(database, timeout=15)) as db, db:
        db.execute("UPDATE app_settings SET favicon=NULL WHERE id=1")
    return respond(start_response, "200 OK", b"Automatische Auswahl aktiv.")


def inventory_list(path):
    result = []
    expected = None
    for page in range(1, 501):
        payload = request(f"{path}?page={page}&pageSize=100")
        batch, total = payload.get("data"), payload.get("totalRows")
        if not isinstance(batch, list) or type(total) is not int or total < 0:
            break
        if expected is not None and expected != total:
            break
        expected = total
        result.extend(batch)
        if len(result) == expected:
            return result
        if not batch or len(result) > expected:
            break
    raise ValueError("Unvollständige Netzwerkliste")


def api_wlan_secret(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 64:
        return None
    if not value.isascii() or not value.isprintable() or not value.strip():
        return None
    if set(value.strip()) <= set("*xX"):
        return None
    if value.strip().casefold() in {"redacted", "********", "[redacted]", "<redacted>", "masked", "<masked>", "not available"}:
        return None
    if len(value) == 64 and any(c not in "0123456789abcdefABCDEF" for c in value):
        return None
    return value.encode()


def imported_access(path, ssid_path, remote_id, security, profiles):
    if security not in (3, 4):
        return []
    detail = request(f"{ssid_path}/{quote(remote_id, safe='')}")
    if not isinstance(detail, dict) or detail.get("ssidId") != remote_id or detail.get("security") != security:
        raise ValueError("SSID-Details stimmen nicht mit dem Inventar überein.")
    if security == 3:
        psk = detail.get("pskSetting") or {}
        vlan = detail.get("vlanId") if detail.get("vlanEnable") is True else None
        return [("wpa", "Gemeinsames Kennwort", "", vlan, api_wlan_secret(psk.get("securityKey")))]
    ppsk = detail.get("ppskSetting") or {}
    profile_id = ppsk.get("ppskProfileId")
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("Der SSID fehlt die PPSK-Profil-ID.")
    if profile_id not in profiles:
        profile = request(f"{path}/ppsk-profile/{quote(profile_id, safe='')}")
        if (not isinstance(profile, dict) or profile.get("id") != profile_id
                or not isinstance(profile.get("ppsk"), list)):
            raise ValueError("PPSK-Profil unvollständig.")
        profile_name = str(profile.get("profileName") or "")
        entries = {}
        for row in profile["ppsk"]:
            name, mac_address = row.get("name"), row.get("mac") or ""
            if not isinstance(name, str) or not name or not isinstance(mac_address, str):
                raise ValueError("PPSK-Eintrag unvollständig.")
            # Omada does not expose an ID for individual profile rows.
            key = json.dumps([profile_id, name, mac(mac_address)], ensure_ascii=False)
            if key in entries:
                raise ValueError("PPSK-Einträge sind nicht eindeutig.")
            entries[key] = (name, profile_name, row.get("vlan"), api_wlan_secret(row.get("psk")))
        profiles[profile_id] = [(key, *entry) for key, entry in entries.items()]
    return profiles[profile_id]


def save_imported_access(db, site, discovered):
    cipher = wlan_cipher()
    db.execute("""
        UPDATE network_import SET active=0 WHERE ssid IN
        (SELECT id FROM network_objects WHERE site=? AND kind='ssid' AND active=0)
    """, (site,))
    for remote_id, entries in discovered.items():
        ssid, = db.execute("SELECT id FROM network_objects WHERE site=? AND kind='ssid' AND remote_id=?",
                           (site, remote_id)).fetchone()
        db.execute("UPDATE network_import SET active=0 WHERE ssid=?", (ssid,))
        for key, name, profile, vlan, password in entries:
            encrypted = cipher.encrypt(password) if cipher and password is not None else None
            vlan = vlan if type(vlan) is int and 1 <= vlan <= 4094 else None
            db.execute("""
                INSERT INTO network_import
                    (ssid,source_key,label,profile,vlan,secret,confirmed,active)
                VALUES (?,?,?,?,?,?,?,1)
                ON CONFLICT(ssid,source_key) DO UPDATE SET
                    label=excluded.label, profile=excluded.profile, vlan=excluded.vlan,
                    secret=coalesce(excluded.secret,network_import.secret),
                    confirmed=excluded.confirmed, active=1
            """, (ssid, key, name, profile, vlan, encrypted, int(encrypted is not None)))


def sync_networks(site):
    path = root + "/sites/" + quote(site, safe="")
    wlans = request(path + "/wireless-network/wlans")
    if not isinstance(wlans, list):
        raise ValueError("Ungültige WLAN-Gruppenliste")

    objects = {}
    networks = inventory_list(path + "/lan-networks")
    for network in networks:
        remote_id, name = network.get("id"), network.get("name")
        if not isinstance(remote_id, str) or not remote_id or not isinstance(name, str):
            raise ValueError("Unvollständiger VLAN-Eintrag")
        vlan = network.get("vlans") or network.get("vlan")
        key = ("vlan", remote_id)
        if key in objects:
            raise ValueError("Doppelte VLAN-ID in der API-Antwort")
        gateway_subnet = network.get("gatewaySubnet")
        ipv4_subnet = ""
        if network.get("purpose") != 0 and isinstance(gateway_subnet, str) and "/" in gateway_subnet:
            try:
                ipv4_subnet = str(IPv4Network(gateway_subnet, strict=False))
            except ValueError:
                pass
        objects[key] = (name, str(vlan) if vlan is not None else "", None, ipv4_subnet)
    groups = set()
    discovered, profiles, detail_errors = {}, {}, 0
    for group in wlans:
        group_id = group.get("wlanId")
        if not isinstance(group_id, str) or not group_id or group_id in groups:
            raise ValueError("Ungültige WLAN-Gruppen-ID")
        groups.add(group_id)
        ssid_path = f"{path}/wireless-network/wlans/{quote(group_id, safe='')}/ssids"
        for ssid in inventory_list(ssid_path):
            remote_id, name = ssid.get("ssidId"), ssid.get("name")
            if not isinstance(remote_id, str) or not remote_id or not isinstance(name, str):
                raise ValueError("Unvollständiger SSID-Eintrag")
            key = ("ssid", remote_id)
            if key in objects:
                raise ValueError("Doppelte SSID-ID in der API-Antwort")
            detail = str(group.get("name") or "")
            if ssid.get("vlanEnable") is True and type(ssid.get("vlanId")) is int:
                detail = " · ".join(filter(None, (detail, f"Standard-VLAN {ssid['vlanId']}")))
            security = ssid.get("security")
            objects[key] = (name, detail, security if type(security) is int else None, "")
            if type(security) is int:
                try:
                    discovered[remote_id] = imported_access(path, ssid_path, remote_id, security, profiles)
                except Exception:
                    # Keep the previous credential snapshot if any detail call failed.
                    detail_errors += 1
    now = int(time.time())
    with closing(sqlite3.connect(database, timeout=15)) as db, db:
        previous = {(kind, remote): (name, detail, security, subnet, active)
                    for kind, remote, name, detail, security, subnet, active in db.execute("""
                        SELECT kind,remote_id,name,detail,security,ipv4_subnet,active FROM network_objects WHERE site=?
                    """, (site,))}
        added = sum(key not in previous for key in objects)
        changed = sum(key in previous and (*value, 1) != previous[key] for key, value in objects.items())
        inactive = sum(key not in objects and row[-1] for key, row in previous.items())
        db.execute("UPDATE network_objects SET active=0 WHERE site=? AND active=1", (site,))
        db.executemany("""
            INSERT INTO network_objects (site,kind,remote_id,name,detail,security,ipv4_subnet,active,last_seen)
            VALUES (?,?,?,?,?,?,?,1,?)
            ON CONFLICT(site,kind,remote_id) DO UPDATE SET name=excluded.name,
              detail=excluded.detail, security=excluded.security, ipv4_subnet=excluded.ipv4_subnet,
              active=1, last_seen=excluded.last_seen
        """, [(site, kind, remote, *data, now) for (kind, remote), data in objects.items()])
        save_imported_access(db, site, discovered)
        warning = (f"Zugangsdetails für {detail_errors} SSID(s) nicht verfügbar. "
                   "Bisherige Importe wurden beibehalten.") if detail_errors else ""
        db.execute("INSERT OR REPLACE INTO network_sync VALUES (1,?,?,?,?)", (site, now, now, warning))
    return added, changed, inactive


def run_network_sync():
    if not network_sync_lock.acquire(blocking=False):
        return None
    site = ""
    try:
        site = select_site()
        return sync_networks(site)
    except Exception as error:
        detail = failure("Netzwerkabgleich", error)
        print(detail, flush=True)
        auth_state.valid_until = 0
        with closing(sqlite3.connect(database, timeout=15)) as db, db:
            db.execute("""
                INSERT INTO network_sync VALUES (1,?,NULL,?,?)
                ON CONFLICT(id) DO UPDATE SET checked=excluded.checked, error=excluded.error
            """, (site, int(time.time()), detail))
        return ()
    finally:
        network_sync_lock.release()


def wlan_cipher():
    try:
        return Fernet(env.get("APP_WLAN_KEY", "").encode("ascii"))
    except (ValueError, UnicodeError):
        return None


def credential_password(source, entry, item, inactive, cipher, revealed):
    if not entry["secret"]:
        return "–"
    masked = '<span aria-label="Kennwort verborgen">••••••••</span>'
    if cipher is None:
        return masked
    visible = revealed and revealed[:2] == (source, entry["id"])
    mask = f'<span class="password-mask"{" hidden" if visible else ""}>{masked}</span>'
    password = '<code class="revealed-secret">' + escape(revealed[2]) + '</code>' if visible else ""
    object_id = item["id"]
    query = {"id": object_id}
    if inactive:
        query["inactive"] = 1
    return render("network-reveal", csrf=wlan_csrf, object_id=object_id,
                  access_id=entry["id"], inactive="1" if inactive else "",
                  action="reveal_import" if source == "import" else "reveal",
                  mask=mask, password=password,
                  reveal_hidden=" hidden" if visible else "",
                  hide_hidden="" if visible else " hidden",
                  hide_url=escape("/wlan-vlans?" + urlencode(query) + f"#network-{object_id}"))


def imported_network_editor(item, imports, manual, vlan_options, inactive, cipher, revealed, editable):
    html = []
    for entry in imports:
        if not entry["active"] and not inactive:
            continue
        label = entry["label"]
        detail = [entry["profile"] or "WPA-Personal"]
        vlan = "–"
        if entry["vlan"] is not None:
            vlan = str(entry["vlan"])
            names = [row["name"] for row in vlan_options if row["detail"] == vlan and row["active"]]
            if len(names) == 1:
                vlan += f" · {names[0]}"
        if not entry["active"]:
            detail.append("Inaktiv")
        if not entry["secret"]:
            detail.append("Kennwort nicht übernommen")
        elif not entry["confirmed"]:
            detail.append("Zuletzt gespeichertes Kennwort")
        matches = [row for row in manual if (item["security"] == 3 or row["label"] == label)]
        conflict = False
        if cipher and entry["confirmed"] and entry["secret"] and len(matches) == 1:
            try:
                conflict = not compare_digest(cipher.decrypt(entry["secret"]), cipher.decrypt(matches[0]["secret"]))
            except InvalidToken:
                pass
        if conflict:
            detail.append("Weicht vom manuellen Kennwort ab")
        html.append(render("network-credential", label=escape(label), detail=escape(" · ".join(detail)),
                           vlan=escape(vlan), source="Omada",
                           password=credential_password("import", entry, item, inactive, cipher, revealed),
                           actions="<td></td>" if editable else ""))
    return "".join(html)


def reveal_network_access(cipher, encrypted, object_id, access_id, inactive, source, start_response):
    revealed = cipher.decrypt(encrypted).decode()
    params = {"id": [str(object_id)], "inactive": [inactive]}
    content = network_view(params, revealed=(source, access_id, revealed))
    html = render_layout(content, "/wlan-vlans", authenticated=True)
    return respond(start_response, "200 OK", html.encode(), "text/html; charset=utf-8")


def network_editor(item, access, imports, vlan_options, inactive, cipher, saved=False, revealed=None):
    unlocked = cipher is not None
    ppsk = item["security"] in (4, 5)
    mode = {0: "Offen", 2: "WPA-Enterprise", 3: "Gemeinsames Kennwort",
            4: "PPSK ohne RADIUS", 5: "PPSK mit RADIUS"}.get(item["security"], "")
    credentials = ""
    if item["kind"] == "ssid":
        editable = unlocked and bool(access)
        columns = 5 if editable else 4
        rows = [imported_network_editor(item, imports, access, vlan_options, inactive, cipher, revealed, editable)]
        add_form = ""
        entries = [*access, None] if unlocked and (ppsk or not access) else access
        for entry in entries:
            entry_id = entry["id"] if entry else 0
            editor_id = f"access-{item['id']}-{entry_id}"
            options = ['<option value="">Kein Ziel-VLAN dokumentiert</option>']
            target_vlan = "–"
            for vlan in vlan_options:
                flag = " selected" if entry and entry["vlan"] == vlan["id"] else ""
                name = " · ".join(filter(None, (vlan["detail"], vlan["name"],
                                                "" if vlan["active"] else "inaktiv")))
                options.append(f'<option value="{vlan["id"]}"{flag}>{escape(name)}</option>')
                if flag:
                    target_vlan = name
            form = render("network-access", csrf=wlan_csrf, object_id=item["id"], access_id=entry_id,
                          inactive="1" if inactive else "", label=escape(entry["label"] if entry else ""),
                          note=escape(entry["note"] if entry else ""), options="".join(options),
                          password_label="Neues Kennwort (optional)" if entry else "Kennwort",
                          required="" if entry else " required") if unlocked else ""
            if entry is None:
                add_form = f'<details class="network-add"><summary>+ Zugang</summary>{form}</details>'
                continue
            actions = ""
            if unlocked:
                actions = (f'<td><button type="button" class="access-toggle" aria-expanded="false" '
                           f'aria-controls="{editor_id}">Bearbeiten</button></td>')
            rows.append(render("network-credential", label=escape(entry["label"] or mode or "Zugang"),
                               detail=escape(entry["note"]), vlan=escape(target_vlan), source="Manuell",
                               password=credential_password("manual", entry, item, inactive, cipher, revealed),
                               actions=actions))
            if form:
                rows.append(f'<tr id="{editor_id}" class="access-editor" hidden><td colspan="{columns}">{form}</td></tr>')
        help_text = "" if unlocked else '<p class="muted">Zum Speichern und Anzeigen APP_WLAN_KEY setzen.</p>'
        credentials = render("network-credentials", rows="".join(rows) or
                             f'<tr><td colspan="{columns}">Keine Zugänge dokumentiert.</td></tr>',
                             action_header='<th scope="col">Aktionen</th>' if editable else "",
                             action_column='<col class="credential-action-column">' if editable else "",
                             add_form=add_form, help_text=help_text)
    return render("network-edit", detail=escape(mode), note=escape(item["note"]),
                  csrf=wlan_csrf, object_id=item["id"], inactive="1" if inactive else "",
                  credentials=credentials, note_open=" open" if item["kind"] != "ssid" else "",
                  saved='<p class="status-chip" role="status">Gespeichert.</p>' if saved else "")


def network_view(params, notice="", revealed=None):
    inactive = params.get("inactive", [""])[0] == "1"
    selected_text = params.get("id", [""])[0]
    selected = int(selected_text) if selected_text.isascii() and selected_text.isdigit() and len(selected_text) <= 18 else 0
    with closing(sqlite3.connect(database, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        sync = db.execute("SELECT * FROM network_sync WHERE id=1").fetchone()
        objects = list(db.execute("""
            SELECT * FROM network_objects ORDER BY active DESC, name COLLATE NOCASE, id
        """))
        access_by_ssid = {}
        for entry in db.execute("SELECT id, ssid, label, vlan, note, secret FROM network_access ORDER BY id"):
            access_by_ssid.setdefault(entry["ssid"], []).append(entry)
        imports_by_ssid = {}
        for entry in db.execute("SELECT * FROM network_import ORDER BY active DESC,label,id"):
            imports_by_ssid.setdefault(entry["ssid"], []).append(entry)
    vlans_by_site = {}
    for item in objects:
        if item["kind"] == "vlan":
            vlans_by_site.setdefault(item["site"], []).append(item)
    cards = {"ssid": [], "vlan": []}
    cipher = wlan_cipher()
    saved = "saved" in params
    for row in objects:
        if not inactive and not row["active"] and row["id"] != selected:
            continue
        opened = row["id"] == selected
        editor = network_editor(row, access_by_ssid.get(row["id"], []),
                                imports_by_ssid.get(row["id"], []),
                                vlans_by_site.get(row["site"], []), inactive, cipher,
                                saved=opened and saved, revealed=revealed if opened else None)
        detail = row["detail"]
        if row["kind"] == "vlan":
            detail = " · ".join(filter(None, (f"VLAN {detail}" if detail else "", row["ipv4_subnet"])))
        cards[row["kind"]].append(render(
            "network-item", object_id=row["id"], opened=" open" if opened else "",
            name=escape(row["name"]), detail=escape(detail),
            status="Aktiv" if row["active"] else "Inaktiv", editor=editor,
        ))
    success = datetime.fromtimestamp(sync["succeeded"], display_zone).strftime("%d.%m.%Y %H:%M") if sync and sync["succeeded"] is not None else "Noch kein erfolgreicher Abgleich"
    counts = params.get("sync", [""])[0].split(",")
    if len(counts) == 3 and all(c.isascii() and c.isdigit() and len(c) <= 6 for c in counts):
        notice = f"Abgleich abgeschlossen: {counts[0]} neu, {counts[1]} geändert, {counts[2]} inaktiv."
    elif "sync_busy" in params or network_sync_lock.locked():
        notice = "Der Abgleich läuft bereits."
    return render("networks", csrf=wlan_csrf, sync_disabled=" disabled" if network_sync_lock.locked() else "",
                  sync_id=selected, sync_inactive="1" if inactive else "", ssids="".join(cards["ssid"]) or "<li>Keine SSIDs</li>",
                  vlans="".join(cards["vlan"]) or "<li>Keine VLANs</li>",
                  notice='<p role="status">' + escape(notice) + "</p>" if notice else "",
                  success=escape(success),
                  sync_error=('<p role="status">' + escape(sync["error"]) + "</p>") if sync and sync["error"] else "",
                  filter_link="?" + urlencode({"id": selected} if inactive else {**{"inactive": 1}, **({"id": selected} if selected else {})}),
                  filter_label="Nur aktive anzeigen" if inactive else "Inaktive anzeigen")


def network_action(environ, start_response):
    if environ.get("CONTENT_TYPE", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        return respond(start_response, "415 Unsupported Media Type", b"Formular erwartet.")
    try:
        size = int(environ.get("CONTENT_LENGTH", "0"))
        if not 0 < size <= 20000:
            raise ValueError
        form = parse_qs(environ["wsgi.input"].read(size).decode(), keep_blank_values=True, max_num_fields=12)
        value = lambda name: form.get(name, [""])[0]
        if not compare_digest(value("csrf").encode(), wlan_csrf.encode()):
            return respond(start_response, "403 Forbidden", b"Bitte Seite neu laden.")
        action = value("action")
        if action == "sync":
            result = run_network_sync()
            target = {"id": value("id"), "inactive": value("inactive")}
            if result is None:
                target["sync_busy"] = 1
            elif result:
                target["sync"] = ",".join(map(str, result))
            return respond(start_response, "303 See Other", b"",
                           headers=(("Location", "/wlan-vlans?" + urlencode(target)),))
        object_id = int(value("id"))
        note = value("note")
        if not 0 < object_id < 2**63 or len(note) > 4000:
            raise ValueError
        with closing(sqlite3.connect(database, timeout=15)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT site, kind, security FROM network_objects WHERE id=?", (object_id,)).fetchone()
            if item is None:
                raise ValueError
            if action == "note":
                db.execute("UPDATE network_objects SET note=? WHERE id=?", (note, object_id))
            else:
                cipher = wlan_cipher()
                if item[1] != "ssid" or cipher is None:
                    raise ValueError
                access_id = int(value("access"))
                if action in {"reveal", "reveal_import"}:
                    source = "import" if action == "reveal_import" else "manual"
                    table = "network_import" if source == "import" else "network_access"
                    secret = db.execute(f"SELECT secret FROM {table} WHERE id=? AND ssid=?",
                                        (access_id, object_id)).fetchone()
                    if not secret or not secret[0]:
                        raise ValueError
                    return reveal_network_access(cipher, secret[0], object_id, access_id,
                                                 value("inactive"), source, start_response)
                existing = db.execute("SELECT secret FROM network_access WHERE id=? AND ssid=?",
                                      (access_id, object_id)).fetchone()
                if access_id and not existing:
                    raise ValueError
                if action != "save":
                    raise ValueError
                label, password = value("label").strip(), value("password")
                vlan = int(value("vlan")) if value("vlan") else None
                if vlan is not None and not db.execute(
                    "SELECT 1 FROM network_objects WHERE id=? AND kind='vlan' AND site=?", (vlan, item[0])
                ).fetchone():
                    raise ValueError
                if len(label) > 120 or len(password.encode()) > 512 or (not password and not existing):
                    raise ValueError
                if not existing and item[2] not in (4, 5) and db.execute(
                    "SELECT 1 FROM network_access WHERE ssid=?", (object_id,)
                ).fetchone():
                    raise ValueError
                encrypted = cipher.encrypt(password.encode()) if password else existing[0]
                if existing:
                    db.execute("UPDATE network_access SET label=?,vlan=?,secret=?,note=? WHERE id=?",
                               (label, vlan, encrypted, note, access_id))
                else:
                    db.execute("INSERT INTO network_access (ssid,label,vlan,secret,note) VALUES (?,?,?,?,?)",
                               (object_id, label, vlan, encrypted, note))
    except InvalidToken:
        return respond(start_response, "409 Conflict", b"Kennwort mit APP_WLAN_KEY nicht entschluesselbar.")
    except (ValueError, OverflowError):
        return respond(start_response, "400 Bad Request", b"Ungueltige Angaben. Bitte zurueck zum Formular.")
    location = "/wlan-vlans?" + urlencode({"id": object_id, "inactive": value("inactive"), "saved": 1})
    location += f"#network-{object_id}"
    return respond(start_response, "303 See Other", b"", headers=(("Location", location),))


def dashboard(environ, start_response):
    path = environ["PATH_INFO"]
    method = environ["REQUEST_METHOD"]
    if path in {"/login", "/logout"}:
        return account_action(environ, start_response, logout=path == "/logout")
    authenticated = admin_authenticated(environ)
    if path in {"/admin", "/admin/logo", "/admin/name", "/admin/favicon", "/admin/favicon/reset",
                "/wlan-vlans", "/admin/networks"}:
        if not admin_password:
            return respond(start_response, "404 Not Found", b"Geschuetzte Seite deaktiviert.")
        if not authenticated:
            if method == "GET":
                return respond(start_response, "303 See Other", b"", headers=(("Location", "/?login=required"),))
            return respond(start_response, "401 Unauthorized", b"Bitte erneut anmelden.")
        if path in {"/wlan-vlans", "/admin/networks"} and method == "POST":
            return network_action(environ, start_response)
        if path == "/admin/networks" and method == "GET":
            query = urlencode(parse_qs(environ.get("QUERY_STRING", ""), max_num_fields=20), doseq=True)
            return respond(start_response, "303 See Other", b"",
                           headers=(("Location", "/wlan-vlans" + ("?" + query if query else "")),))
        if path in {"/admin/name", "/admin/logo", "/admin/favicon", "/admin/favicon/reset"}:
            if method != "POST":
                return respond(start_response, "405 Method Not Allowed", b"POST erforderlich.",
                               headers=(("Allow", "POST"),))
            if path == "/admin/name":
                return save_display_name(environ, start_response)
            if path == "/admin/favicon/reset":
                return reset_favicon(environ, start_response)
            return save_brand_image(environ, start_response, path.rsplit("/", 1)[-1])
    if method != "GET":
        return respond(start_response, "405 Method Not Allowed", b"GET erforderlich.",
                       headers=(("Allow", "GET"),))
    if path == "/assets/omada-inside.png":
        with closing(sqlite3.connect(database, timeout=15)) as db:
            row = db.execute("SELECT png FROM branding WHERE id=1").fetchone()
        return respond(start_response, "200 OK", row[0] if row else logo.read_bytes(), "image/png")
    if path == "/favicon.ico":
        return respond(start_response, "200 OK", favicon(), "image/x-icon")
    if path in {"/favicon-16.png", "/favicon-32.png"}:
        size = int(path[9:11])
        with Image.open(BytesIO(favicon()), formats=["ICO"]) as icon:
            output = BytesIO()
            icon.ico.getimage((size, size)).save(output, format="PNG")
        return respond(start_response, "200 OK", output.getvalue(), "image/png")
    assets = {"/assets/style.css": ("style.css", "text/css; charset=utf-8"),
              "/assets/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
              "/assets/network-sync.js": ("network-sync.js", "text/javascript; charset=utf-8"),
              "/assets/theme.js": ("theme.js", "text/javascript; charset=utf-8")}
    if path in assets:
        name, content_type = assets[path]
        return respond(start_response, "200 OK", (app_dir / "assets" / name).read_bytes(), content_type)

    params = parse_qs(environ.get("QUERY_STRING", ""), max_num_fields=20)
    pages = {
        "/": lambda: internet_view(params),
        "/wlan": lambda: wlan_view(params),
        "/wan-calendar": lambda: wan_calendar(params),
        "/admin": admin_view,
        "/wlan-vlans": lambda: network_view(params),
    }
    if path not in pages:
        return respond(start_response, "404 Not Found", b"Seite nicht gefunden.")
    message = {"failed": "Benutzername oder Passwort ist falsch.",
               "required": "Bitte melde dich für diese Seite an."}.get(params.get("login", [""])[0], "")
    html = render_layout(pages[path](), path, authenticated, message)
    return respond(start_response, "200 OK", html.encode(), "text/html; charset=utf-8")

if __name__ == "__main__":
    initialize_database()
    if sys.argv[1:] == ["worker"]:
        archive_wan_history()
    else:
        threading.Thread(target=collect, daemon=True).start()
        make_server("0.0.0.0", 8080, dashboard, server_class=ThreadedServer).serve_forever()
