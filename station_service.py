
import os
import argparse
import csv
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
SOURCE_URLS = {
    'nws_forecast': 'https://api.weather.gov/gridpoints/BOU/55,70/forecast/hourly',
    'kbjc_observation': 'https://api.weather.gov/stations/KBJC/observations/latest',
    'nws_alerts': 'https://api.weather.gov/alerts/active?point=39.9095,-105.225',
}


def now():
    return datetime.now(UTC).isoformat()


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('时间必须包含时区 / Time zone is required')
    return parsed.astimezone(UTC).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def wind_mps(value, unit):
    if value is None:
        return None
    factors = {'wmoUnit:km_h-1': 1 / 3.6, 'wmoUnit:m_s-1': 1, 'wmoUnit:kn': 0.514444, 'mph': 0.44704}
    if unit not in factors:
        return None
    return float(value) * factors[unit]


def fetch_json(url):
    request = Request(url, headers={'User-Agent': 'FlatironsResearchPrototype/1.0 (local academic weather viewer)', 'Accept': 'application/geo+json, application/json'})
    with urlopen(request, timeout=25) as response:
        raw = response.read(12_000_000)
    return json.loads(raw), raw


class Store:
    def __init__(self, database, root=ROOT):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(root)
        self.refresh_lock = threading.Lock()
        self.last_refresh_started = 0.0
        self.assets = self.read_assets()
        with self.connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS snapshots (
                    hash TEXT PRIMARY KEY, source TEXT, url TEXT, source_time TEXT,
                    captured_at TEXT, checked_at TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS observations (
                    station TEXT, observed_at TEXT, wind_mps REAL, temperature_c REAL,
                    humidity_pct REAL, height_m REAL, source TEXT, source_hash TEXT,
                    PRIMARY KEY(station, observed_at));
                CREATE TABLE IF NOT EXISTS source_status (
                    source TEXT PRIMARY KEY, checked_at TEXT, success INTEGER, error TEXT);
                CREATE TABLE IF NOT EXISTS imports (hash TEXT PRIMARY KEY, path TEXT, imported_at TEXT);
                CREATE TABLE IF NOT EXISTS research_versions (
                    version TEXT PRIMARY KEY, kind TEXT, source_hash TEXT, model_hash TEXT,
                    imported_at TEXT, available INTEGER, generation INTEGER);
                CREATE TABLE IF NOT EXISTS research_forecasts (
                    version TEXT, issue_time TEXT, valid_time TEXT, payload TEXT,
                    PRIMARY KEY(version, issue_time, valid_time));
                CREATE TABLE IF NOT EXISTS workorders (
                    id TEXT PRIMARY KEY, asset_id TEXT, category TEXT, title TEXT,
                    description TEXT, status TEXT, event_at TEXT, author TEXT,
                    created_at TEXT, updated_at TEXT, revision INTEGER);
                CREATE TABLE IF NOT EXISTS workorder_history (
                    id INTEGER PRIMARY KEY, record_id TEXT, changed_at TEXT, payload TEXT);
            ''')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def read_assets(self):
        payload = (self.root / 'data.js').read_text(encoding='utf-8').split('=', 1)[1].strip().rstrip(';')
        features = json.loads(payload)['geospatial_assets']['features']
        return {feature['properties']['asset_id']: feature['properties'] for feature in features if feature['properties']['kind'] != 'site_boundary'}

    def import_history(self):
        imported = 0
        for filename, start, end in [
            ('m2_hourly_2025.csv', '2025-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
            ('m2_hourly_2026_Q1.csv', '2026-01-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00'),
        ]:
            path = self.root / 'research/01 输入数据/standardized' / filename
            if not path.is_file():
                continue
            file_hash = digest(path.read_bytes())
            with self.connect() as connection:
                if connection.execute('SELECT 1 FROM imports WHERE hash=?', (file_hash,)).fetchone():
                    continue
                with path.open(encoding='utf-8-sig', newline='') as stream:
                    for row in csv.DictReader(stream):
                        valid = timestamp(row['valid_time'])
                        value = row.get('observed_mean_wind_80m_mps')
                        if not value or not math.isfinite(float(value)) or int(row['valid_minute_count']) < 45 or not start <= valid < end:
                            continue
                        connection.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?)', ('M2', valid, float(value), None, None, 80, 'M2 archived hourly observations', file_hash))
                        imported += 1
                connection.execute('INSERT INTO imports VALUES (?,?,?)', (file_hash, str(path.relative_to(self.root)), now()))
        return imported

    def refresh(self, fetcher=fetch_json, force=False):
        if not self.refresh_lock.acquire(blocking=False):
            return False
        try:
            if not force and time.monotonic() - self.last_refresh_started < 60:
                return False
            self.last_refresh_started = time.monotonic()
            for source, url in SOURCE_URLS.items():
                checked = now()
                try:
                    payload, raw = fetcher(url)
                    properties = payload.get('properties', {})
                    if source == 'nws_forecast' and not properties.get('periods'):
                        raise ValueError('Forecast response has no periods')
                    if source == 'nws_forecast':
                        timestamp(properties['updateTime'])
                        for period in properties['periods']:
                            if timestamp(period['startTime']) >= timestamp(period['endTime']):
                                raise ValueError('Forecast interval is invalid')
                    if source == 'kbjc_observation' and not properties.get('timestamp'):
                        raise ValueError('Observation timestamp is missing')
                    if source == 'nws_alerts' and not isinstance(payload.get('features'), list):
                        raise ValueError('Alert response is not a feature collection')
                    upstream = properties.get('updateTime') or properties.get('timestamp') or payload.get('updated')
                    source_hash = digest(raw)
                    with self.connect() as connection:
                        connection.execute('INSERT INTO snapshots VALUES (?,?,?,?,?,?,?) ON CONFLICT(hash) DO UPDATE SET checked_at=excluded.checked_at', (source_hash, source, url, upstream, checked, checked, raw.decode('utf-8-sig')))
                        if source == 'kbjc_observation':
                            wind = properties.get('windSpeed') or {}
                            temperature = properties.get('temperature') or {}
                            connection.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(station,observed_at) DO UPDATE SET wind_mps=excluded.wind_mps,temperature_c=excluded.temperature_c,humidity_pct=excluded.humidity_pct,source_hash=excluded.source_hash', ('KBJC', timestamp(properties['timestamp']), wind_mps(wind.get('value'), wind.get('unitCode')), temperature.get('value') if temperature.get('unitCode') == 'wmoUnit:degC' else None, (properties.get('relativeHumidity') or {}).get('value'), None, 'NWS KBJC nearby station, not on-site M2', source_hash))
                        connection.execute('INSERT OR REPLACE INTO source_status VALUES (?,?,?,?)', (source, checked, 1, None))
                except Exception as error:
                    with self.connect() as connection:
                        connection.execute('INSERT OR REPLACE INTO source_status VALUES (?,?,?,?)', (source, checked, 0, str(error)[:400]))
            return True
        finally:
            self.refresh_lock.release()

    def latest(self):
        result = {'server_time_utc': now(), 'refreshing': self.refresh_lock.locked(), 'poll_interval_seconds': 600, 'sources': {}, 'forecast': [], 'observation': None, 'alerts': []}
        with self.connect() as connection:
            for source in SOURCE_URLS:
                status = connection.execute('SELECT * FROM source_status WHERE source=?', (source,)).fetchone()
                snapshot = connection.execute('SELECT * FROM snapshots WHERE source=? ORDER BY source_time DESC,checked_at DESC LIMIT 1', (source,)).fetchone()
                info = dict(status) if status else {'success': None, 'error': 'Awaiting first response'}
                result['sources'][source] = info
                if not snapshot:
                    continue
                payload = json.loads(snapshot['payload'])
                props = payload.get('properties', {})
                info.update({key: snapshot[key] for key in ('hash', 'url', 'source_time', 'captured_at')})
                info['snapshot_checked_at'] = snapshot['checked_at']
                if source == 'nws_forecast':
                    for period in props['periods']:
                        wind_text = period.get('windSpeed', '')
                        values = [float(value) for value in re.findall(r'\d+(?:\.\d+)?', wind_text)]
                        factor = 0.44704 if 'mph' in wind_text else 1 / 3.6 if 'km/h' in wind_text else None
                        temperature = period.get('temperature')
                        result['forecast'].append({'valid_from': timestamp(period['startTime']), 'valid_to': timestamp(period['endTime']), 'wind_low_mps': min(values) * factor if values and factor else None, 'wind_high_mps': max(values) * factor if values and factor else None, 'wind_original': wind_text, 'wind_direction': period.get('windDirection'), 'temperature_c': (temperature - 32) / 1.8 if temperature is not None and period.get('temperatureUnit') == 'F' else temperature if period.get('temperatureUnit') == 'C' else None, 'precipitation_probability_pct': (period.get('probabilityOfPrecipitation') or {}).get('value'), 'summary': period.get('shortForecast'), 'source_hash': snapshot['hash'], 'data_role': 'official_hourly_forecast', 'height_m': None})
                    result['forecast_issue_time'] = props.get('updateTime')
                    result['forecast_generated_at'] = props.get('generatedAt')
                    age = (datetime.now(UTC) - datetime.fromisoformat(timestamp(props['updateTime']))).total_seconds()
                    info['stale'] = age > 43200 or age < -300 or not any(row['valid_to'] > result['server_time_utc'] for row in result['forecast'])
                elif source == 'kbjc_observation':
                    row = connection.execute('SELECT * FROM observations WHERE station=? ORDER BY observed_at DESC LIMIT 1', ('KBJC',)).fetchone()
                    result['observation'] = dict(row) if row else None
                    if row:
                        age = (datetime.now(UTC) - datetime.fromisoformat(row['observed_at'])).total_seconds()
                        info['age_seconds'] = age
                        info['stale'] = age > 7200 or age < -300
                    result['observation_coordinates'] = payload.get('geometry', {}).get('coordinates')
                else:
                    age = (datetime.now(UTC) - datetime.fromisoformat(snapshot['checked_at'])).total_seconds()
                    info['stale'] = age > 1200 or age < -300 or info.get('success') != 1
                    result['alerts'] = [{'id': item.get('id'), **{key: item.get('properties', {}).get(key) for key in ('event', 'headline', 'severity', 'onset', 'expires', 'instruction')}} for item in payload['features']]
        return result

    def history(self, station, start, end):
        if station not in ('M2', 'KBJC'):
            raise ValueError('测站只接受 M2 或 KBJC / Station must be M2 or KBJC')
        if not start or not end:
            raise ValueError('缺 start/end 参数 / Missing start and end parameters')
        start, end = timestamp(start), timestamp(end)
        if not start < end or datetime.fromisoformat(end) - datetime.fromisoformat(start) > timedelta(days=400):
            raise ValueError('请选择不超过400天且开始早于结束的范围')
        with self.connect() as connection:
            rows = connection.execute('SELECT * FROM observations WHERE station=? AND observed_at>=? AND observed_at<? ORDER BY observed_at LIMIT 12000', (station, start, end)).fetchall()
        return {'station': station, 'start_utc': start, 'end_utc_exclusive': end, 'rows': [dict(row) for row in rows], 'count': len(rows), 'missing_values_filled': False}

    def coverage(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute('SELECT station, MIN(observed_at) first_time, MAX(observed_at) last_time, COUNT(*) count FROM observations GROUP BY station')]

    def forecast_archive(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT hash,source_time,captured_at,checked_at FROM snapshots WHERE source='nws_forecast' ORDER BY captured_at DESC LIMIT 200")]

    def import_research_forecasts(self, kind='corrected', rebuild='incremental'):
        if kind not in ('raw', 'corrected') or rebuild not in ('incremental', 'full'):
            raise ValueError('Invalid replay kind or rebuild mode')
        path = self.root / 'research/01 输入数据/standardized/external_2026_predictions.csv'
        source_hash = digest(path.read_bytes())
        model_path = self.root / 'research/04 实验结果/external_2026_frozen_selection.json'
        model_hash = digest(model_path.read_bytes())
        decisions = json.loads(model_path.read_text(encoding='utf-8'))['decisions']
        event_threshold = float(decisions['event_threshold_mps'])
        probability_threshold = float(decisions['selected_thresholds']['Gradient-boosting'])
        version = kind + '-' + digest((source_hash + model_hash).encode())[:20]
        rows = []
        with path.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                issue, valid = timestamp(row['issue_time']), timestamp(row['valid_time'])
                lead = int(row['lead_hours'])
                if (datetime.fromisoformat(valid) - datetime.fromisoformat(issue)).total_seconds() != lead * 3600:
                    raise ValueError('Replay forecast time and lead disagree')
                speed = float(row['corrected_wind_speed_mps'] if kind == 'corrected' else row['nwp_wind_speed_80m_mps'])
                probability = float(row['Gradient-boosting_probability']) if kind == 'corrected' else None
                if not math.isfinite(speed) or speed < 0 or probability is not None and not 0 <= probability <= 1:
                    raise ValueError('Invalid replay numerical value')
                value = {'issue_time': issue, 'valid_time': valid, 'lead_hours': lead,
                    'wind_mps': speed, 'raw_wind_mps': float(row['nwp_wind_speed_80m_mps']),
                    'station_event_probability': probability, 'probability_threshold': probability_threshold if probability is not None else None,
                    'station_event_screen': probability >= probability_threshold if probability is not None else speed >= event_threshold,
                    'event_threshold_mps': event_threshold, 'target_station': 'M2', 'target_height_m': 80,
                    'gfs_message_hash': row['nwp_message_hash'], 'data_role': 'retrospective_station_corrected_forecast' if kind == 'corrected' else 'archived_nwp_forecast',
                    'spatial_scope': 'shared_site_reference', 'equipment_risk': None,
                    'equipment_risk_status': 'requires_asset_specific_limits_and_exposure_data',
                    'forecast_grid': {'latitude': 40.0, 'longitude': -105.0, 'resolution_degrees': 0.5},
                    'station_query_coordinate': {'latitude': 39.9106045, 'longitude': -105.2347780}}
                rows.append((version, issue, valid, json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)))
        if len({(row[1], row[2]) for row in rows}) != len(rows):
            raise ValueError('Duplicate replay forecast key')
        with self.connect() as connection:
            previous = connection.execute('SELECT * FROM research_versions WHERE version=?', (version,)).fetchone()
            if previous and previous['available'] and rebuild == 'incremental':
                return {'version': version, 'count': len(rows), 'rebuilt': 0, 'generation': previous['generation']}
            generation = previous['generation'] + 1 if previous else 1
            connection.execute('INSERT OR REPLACE INTO research_versions VALUES (?,?,?,?,?,?,?)',
                (version, kind, source_hash, model_hash, now(), 1, generation))
            connection.execute('DELETE FROM research_forecasts WHERE version=?', (version,))
            connection.executemany('INSERT INTO research_forecasts VALUES (?,?,?,?)', rows)
        return {'version': version, 'count': len(rows), 'rebuilt': len(rows), 'generation': generation}

    def research_versions(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute('SELECT v.*, MIN(f.valid_time) first_time, MAX(f.valid_time) last_time, COUNT(f.valid_time) count FROM research_versions v LEFT JOIN research_forecasts f ON f.version=v.version GROUP BY v.version ORDER BY v.kind')]

    def set_research_availability(self, version, available):
        with self.connect() as connection:
            if not connection.execute('SELECT 1 FROM research_versions WHERE version=?', (version,)).fetchone():
                raise ValueError('Unknown replay version')
            connection.execute('UPDATE research_versions SET available=? WHERE version=?', (int(bool(available)), version))

    def research_weather(self, asset_id, version, start, end):
        if asset_id not in self.assets:
            raise ValueError('Unknown site object')
        start, end = timestamp(start), timestamp(end)
        if not start < end or datetime.fromisoformat(end) - datetime.fromisoformat(start) > timedelta(days=100):
            raise ValueError('Replay query must span at most 100 days')
        asset = self.assets[asset_id]
        result = {'asset_id': asset_id, 'asset_coordinates': {'latitude': asset.get('center_lat'), 'longitude': asset.get('center_lon')},
            'version': version, 'start_utc': start, 'end_utc_exclusive': end, 'rows': [],
            'spatial_scope': 'shared_site_reference', 'missing_values_filled': False}
        with self.connect() as connection:
            source = connection.execute('SELECT * FROM research_versions WHERE version=?', (version,)).fetchone()
            if not source:
                raise ValueError('Unknown replay version')
            result.update({'source_hash': source['source_hash'], 'model_hash': source['model_hash'], 'generation': source['generation']})
            if not source['available']:
                result['status'] = 'source_unavailable'
                return result
            result['rows'] = [json.loads(row['payload']) for row in connection.execute('SELECT payload FROM research_forecasts WHERE version=? AND valid_time>=? AND valid_time<? ORDER BY valid_time,issue_time', (version, start, end))]
        result['status'] = 'available' if result['rows'] else 'missing'
        return result

    def workorders(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM workorders ORDER BY created_at DESC')]

    def save_workorder(self, value, identifier=None):
        required = ('asset_id', 'category', 'title', 'description', 'status', 'event_at', 'author')
        if not all(isinstance(value.get(key), str) for key in required):
            raise ValueError('记录字段缺失 / Required fields are missing')
        if value['asset_id'] not in self.assets or value['status'] not in ('待处理', '处理中', '已完成') or value['category'] not in ('巡检', '维修', '天气复核', '其他'):
            raise ValueError('对象、类别或状态不合法')
        if not value['title'].strip() or not value['description'].strip() or not value['author'].strip():
            raise ValueError('请填写标题、记录内容和记录人')
        if len(value['description']) > 6000 or any(len(value[key]) > 180 for key in ('title', 'author')):
            raise ValueError('记录文本过长')
        event_at = timestamp(value['event_at'])
        updated = now()
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            prior = connection.execute('SELECT * FROM workorders WHERE id=?', (identifier,)).fetchone() if identifier else None
            if identifier and not prior:
                raise ValueError('记录不存在')
            if prior and value.get('revision') != prior['revision']:
                raise ValueError('记录已改变，请刷新后再保存')
            row = {'id': identifier or str(uuid.uuid4()), **{key: value[key].strip() for key in required}, 'event_at': event_at, 'created_at': prior['created_at'] if prior else updated, 'updated_at': updated, 'revision': prior['revision'] + 1 if prior else 1}
            columns = list(row)
            connection.execute('INSERT OR REPLACE INTO workorders (' + ','.join(columns) + ') VALUES (' + ','.join('?' for key in columns) + ')', [row[key] for key in columns])
            connection.execute('INSERT INTO workorder_history(record_id,changed_at,payload) VALUES (?,?,?)', (row['id'], updated, json.dumps(row, ensure_ascii=False)))
        return row


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def safe_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.safe_host():
            return self.send_json({'error': 'Invalid local host'}, 403)
        parts = urlsplit(self.path)
        store = self.server.store
        query = parse_qs(parts.query)
        try:
            if parts.path == '/api/status':
                return self.send_json({'service': 'flatirons-operational', 'version': 1, 'server_time_utc': now(), 'coverage': store.coverage(), 'refreshing': store.refresh_lock.locked(), 'device_telemetry_connected': False, 'workorders_origin': 'manual_local_entries'})
            if parts.path == '/api/weather/latest':
                return self.send_json(store.latest())
            if parts.path == '/api/weather/history':
                return self.send_json(store.history(query.get('station', ['M2'])[0], query.get('start', [''])[0], query.get('end', [''])[0]))
            if parts.path == '/api/weather/forecasts':
                return self.send_json(store.forecast_archive())
            if parts.path == '/api/weather/forecast':
                snapshot_hash = query.get('hash', [''])[0]
                if not snapshot_hash:
                    return self.send_json({'error': '缺 hash 参数 / Missing hash parameter'}, 400)
                with store.connect() as connection:
                    row = connection.execute("SELECT * FROM snapshots WHERE hash=? AND source='nws_forecast'", (snapshot_hash,)).fetchone()
                return self.send_json(dict(row), 200) if row else self.send_json({'error': '查不到该报文快照 / Unknown forecast snapshot'}, 404)
            if parts.path == '/api/assets':
                return self.send_json(store.assets)
            if parts.path == '/api/research/versions':
                return self.send_json(store.research_versions())
            if parts.path == '/api/research/weather':
                asset_id = query.get('asset_id', [''])[0]
                version = query.get('version', [''])[0]
                start = query.get('start', [''])[0]
                end = query.get('end', [''])[0]
                if not start or not end:
                    return self.send_json({'error': '缺 start/end 参数 / Missing start and end parameters'}, 400)
                try:
                    datetime.fromisoformat(start.replace('Z', '+00:00'))
                    datetime.fromisoformat(end.replace('Z', '+00:00'))
                except ValueError:
                    return self.send_json({'error': 'start/end 需为 ISO 时间 / start and end must be ISO 8601'}, 400)
                return self.send_json(store.research_weather(asset_id, version, start, end))
            if parts.path == '/api/workorders':
                return self.send_json(store.workorders())
            if parts.path == '/api/workorders/history':
                with store.connect() as connection:
                    rows = connection.execute('SELECT * FROM workorder_history WHERE record_id=? ORDER BY id', (query.get('id', [''])[0],)).fetchall()
                return self.send_json([dict(row) for row in rows])
            if parts.path.startswith('/api/'):
                return self.send_json({'error': 'Unknown route'}, 404)
            translated = Path(self.translate_path(parts.path)).resolve()
            if not translated.is_relative_to(ROOT) or translated.is_relative_to(ROOT / '业务记录 Operational Records') or translated.suffix in ('.sqlite3', '.db'):
                return self.send_error(403)
            super().do_GET()
        except ValueError as error:
            self.send_json({'error': str(error)}, 400)

    def do_HEAD(self):
        self.method = 'GET'
        return self.do_GET()

    def do_POST(self):
        origin = self.headers.get('Origin')
        allowed = (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
        if not self.safe_host() or origin not in (*allowed, None) or self.headers.get('Sec-Fetch-Site') == 'cross-site' or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send_json({'error': 'Local same-origin JSON requests only'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 32000:
                raise ValueError('Invalid request size')
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError('Expected JSON object')
            path = urlsplit(self.path).path
            if path == '/api/weather/refresh':
                threading.Thread(target=self.server.store.refresh, daemon=True).start()
                return self.send_json({'accepted': True, 'minimum_interval_seconds': 60}, 202)
            if path == '/api/workorders':
                return self.send_json(self.server.store.save_workorder(value), 201)
            if path.startswith('/api/workorders/'):
                identifier = path.rsplit('/', 1)[-1]
                return self.send_json(self.server.store.save_workorder(value, identifier))
            self.send_json({'error': 'Unknown route'}, 404)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({'error': str(error)}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8876)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    if args.open:
        import webbrowser
        try:
            with urlopen(f'http://127.0.0.1:{args.port}/api/status', timeout=2) as response:
                active = json.load(response)
            if active.get('service') == 'flatirons-operational':
                webbrowser.open(f'http://127.0.0.1:{args.port}/index.html?lang=zh')
                return
        except Exception:
            pass
    store = Store(ROOT / '业务记录 Operational Records/场站记录.sqlite3')
    store.import_history()
    store.import_research_forecasts('raw')
    store.import_research_forecasts('corrected')
    server = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
    server.store = store

    def worker():
        while True:
            store.refresh()
            time.sleep(600)

    threading.Thread(target=worker, daemon=True).start()
    print(f'Flatirons local service: http://127.0.0.1:{args.port}/index.html?lang=zh', flush=True)
    if args.open:
        webbrowser.open(f'http://127.0.0.1:{args.port}/index.html?lang=zh')
    server.serve_forever()


if __name__ == '__main__':
    main()
