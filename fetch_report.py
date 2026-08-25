import os
import sys
import json
import time
import re
import requests
from datetime import datetime, timedelta

# ============================================================
# НАСТРОЙКИ ПРОЕКТА
# ============================================================

PLAN_PER_CAMP_BUDGET = 500       # общий бюджет на весь период продвижения ОДНОГО кемпа, $

FETCH_SINCE = '2026-07-01'
CHUNK_DAYS = 30
API_VERSION = 'v25.0'

# Главная метрика проекта — начатые переписки (а не лиды/формы).
# ПРЕДПОЛОЖЕНИЕ, требует проверки диагностическим блоком внизу файла на первом запуске.
MESSAGING_ACTION_TYPES = {'onsite_conversion.messaging_conversation_started_7d'}

# Подписчики Instagram — ПРЕДПОЛОЖЕНИЕ, требует проверки диагностикой (см. низ файла).
# У Meta нет отдельного стандартного поля "подписчики" — это тоже один из action_type.
FOLLOWER_ACTION_TYPES = {'follow'}

# Коды кемпов — ищутся как отдельный сегмент через "_" в названии кампании,
# независимо от регистра. Значение справа — как кемп будет подписан на дашборде.
CAMP_KEYWORDS = {
    'БАЛИ': 'Бали',
    'КЫРГЫЗСТАН': 'Кыргызстан',
    'БАКУ': 'Баку',
    'КЕЙПТАУН': 'Кейптаун',
    'ГРУЗИЯ': 'Грузия',
    'ТУРЦИЯ': 'Турция',
}

ACCESS_TOKEN = os.getenv('FACEBOOK_ACCESS_TOKEN')
ACCOUNT_ID = os.getenv('FACEBOOK_ACT_ID')

if not ACCESS_TOKEN or not ACCOUNT_ID:
    print("Ошибка: не заданы FACEBOOK_ACCESS_TOKEN или FACEBOOK_ACT_ID")
    sys.exit(1)

if not ACCOUNT_ID.startswith('act_'):
    ACCOUNT_ID = f'act_{ACCOUNT_ID}'

end_date = datetime.now().strftime('%Y-%m-%d')
start_date = FETCH_SINCE


def date_chunks(since_str, until_str, chunk_days):
    since = datetime.strptime(since_str, '%Y-%m-%d')
    until = datetime.strptime(until_str, '%Y-%m-%d')
    cur = since
    while cur <= until:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), until)
        yield cur.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')
        cur = chunk_end + timedelta(days=1)


def api_get(path, params):
    url = f"https://graph.facebook.com/{API_VERSION}/{path}"
    params = {**params, 'access_token': ACCESS_TOKEN}
    all_data = []

    while url:
        resp = None
        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, timeout=120)
                if resp.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code >= 400:
                    print(f"Meta API вернул {resp.status_code} на {path}: {resp.text}")
                    if attempt == 2:
                        sys.exit(1)
                    time.sleep(30 * (attempt + 1))
                    continue
                resp.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    print(f"Ошибка запроса к {path}: {e}")
                    sys.exit(1)
                time.sleep(2 ** attempt)
        else:
            print(f"Meta API стабильно возвращает 5xx на {path}")
            sys.exit(1)

        payload = resp.json()
        if 'error' in payload:
            print(f"Meta API вернул ошибку на {path}: {payload['error']}")
            sys.exit(1)

        all_data.extend(payload.get('data', []))
        url = payload.get('paging', {}).get('next')
        params = {}

    return all_data


def api_get_chunked(path, base_params, since, until):
    all_data = []
    for chunk_since, chunk_until in date_chunks(since, until, CHUNK_DAYS):
        params = {**base_params, 'time_range': json.dumps({'since': chunk_since, 'until': chunk_until})}
        all_data.extend(api_get(path, params))
    return all_data


def count_actions(actions, action_types):
    total = 0
    for action in actions or []:
        if action.get('action_type') in action_types:
            total += int(action.get('value', 0))
    return total


def parse_camp(name):
    for segment in re.split(r'[_\s]+', name or ''):
        upper = segment.upper()
        if upper in CAMP_KEYWORDS:
            return CAMP_KEYWORDS[upper]
    return None


def day_metrics(raw):
    spend = float(raw.get('spend', 0))
    messages = count_actions(raw.get('actions'), MESSAGING_ACTION_TYPES)
    followers = count_actions(raw.get('actions'), FOLLOWER_ACTION_TYPES)
    clicks = int(raw.get('clicks', 0))
    link_clicks = int(raw.get('inline_link_clicks', 0))
    impressions = int(raw.get('impressions', 0))
    return spend, messages, followers, clicks, link_clicks, impressions


def to_day_row(raw):
    spend, messages, followers, clicks, link_clicks, impressions = day_metrics(raw)
    return {
        "date": raw['date_start'], "spend": round(spend, 2), "messages": messages,
        "followers": followers, "clicks": clicks, "link_clicks": link_clicks,
        "impressions": impressions,
    }


def dedup_by_date(raw_rows):
    by_date = {}
    for r in raw_rows:
        by_date[r['date_start']] = r
    return [to_day_row(r) for _, r in sorted(by_date.items())]


def dedup_by_entity_date(raw_rows, id_field, name_field, parent_fields=None):
    by_id = {}
    for r in raw_rows:
        entity_id = r.get(id_field)
        entry = by_id.setdefault(entity_id, {
            "id": entity_id,
            "name": r.get(name_field, ''),
            "parents": {pf: r.get(pf) for pf in (parent_fields or [])},
            "daily_by_date": {},
        })
        entry["daily_by_date"][r['date_start']] = r

    entities = []
    for entity_id, entry in by_id.items():
        daily = [to_day_row(r) for _, r in sorted(entry["daily_by_date"].items())]
        entity = {"id": entity_id, "name": entry["name"], "daily": daily}
        entity.update(entry["parents"])
        entities.append(entity)
    return entities


# ============================================================
# 1. Аккаунт — по дням (для общих графиков)
# ============================================================
account_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'fields': 'spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)
account_daily = dedup_by_date(account_raw)

# ============================================================
# 2. Кампании — по дням + camp + статус активности
# ============================================================
campaigns_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'campaign',
    'fields': 'campaign_id,campaign_name,spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)
campaigns = dedup_by_entity_date(campaigns_raw, 'campaign_id', 'campaign_name')
for c in campaigns:
    c["camp"] = parse_camp(c["name"])

campaign_status_raw = api_get(f"{ACCOUNT_ID}/campaigns", {
    'fields': 'id,effective_status',
    'limit': 200,
})
status_by_campaign_id = {c['id']: c.get('effective_status') for c in campaign_status_raw}
for c in campaigns:
    c["status"] = status_by_campaign_id.get(c["id"], 'UNKNOWN')

# ============================================================
# 3. Аудитории (adsets) — по дням, с привязкой к кампании
# ============================================================
adsets_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'adset',
    'fields': 'adset_id,adset_name,campaign_id,spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)
adsets = dedup_by_entity_date(adsets_raw, 'adset_id', 'adset_name', parent_fields=['campaign_id'])

# ============================================================
# 4. Объявления (креативы) — по дням, с привязкой к аудитории и кампании
# ============================================================
ads_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'ad',
    'fields': 'ad_id,ad_name,adset_id,campaign_id,spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)
creatives = dedup_by_entity_date(ads_raw, 'ad_id', 'ad_name', parent_fields=['adset_id', 'campaign_id'])

ads_meta_raw = api_get(f"{ACCOUNT_ID}/ads", {
    'fields': 'id,creative.thumbnail_width(720).thumbnail_height(720){thumbnail_url}',
    'limit': 25,
})
thumb_by_ad_id = {a['id']: a.get('creative', {}).get('thumbnail_url') for a in ads_meta_raw}
for c in creatives:
    c["thumbnail_url"] = thumb_by_ad_id.get(c["id"])

# ============================================================
# 5. Демография (возраст + пол) — на уровне кампаний, чтобы фильтровать по кемпу.
# Meta не разрешает комбинировать age+gender+country вместе с action-метриками
# (пробовали — жёсткая ошибка "(#100) Current combination... is invalid"), поэтому
# страны внутри демографии не показываем — только сама демография.
# ============================================================
age_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'campaign',
    'breakdowns': 'age,gender',
    'fields': 'campaign_id,spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)

demo_by_bucket = {}
for r in age_raw:
    bucket = (r.get('campaign_id'), r.get('age', 'unknown'), r.get('gender', 'unknown'))
    demo_by_bucket.setdefault(bucket, {})[r['date_start']] = r

age_groups = []
for (campaign_id, age, gender), by_date in demo_by_bucket.items():
    daily = [to_day_row(r) for _, r in sorted(by_date.items())]
    age_groups.append({"campaign_id": campaign_id, "age": age, "gender": gender, "daily": daily})

# ============================================================
# 6. Устройства — на уровне кампаний
# ============================================================
device_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'campaign',
    'breakdowns': 'impression_device',
    'fields': 'campaign_id,spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)

device_by_bucket = {}
for r in device_raw:
    bucket = (r.get('campaign_id'), r.get('impression_device', 'unknown'))
    device_by_bucket.setdefault(bucket, {})[r['date_start']] = r

devices = []
for (campaign_id, device), by_date in device_by_bucket.items():
    daily = [to_day_row(r) for _, r in sorted(by_date.items())]
    devices.append({"campaign_id": campaign_id, "device": device, "daily": daily})

# ============================================================
# 7. Страны — на уровне кампаний
# ============================================================
geo_raw = api_get_chunked(f"{ACCOUNT_ID}/insights", {
    'time_increment': 1,
    'level': 'campaign',
    'breakdowns': 'country',
    'fields': 'campaign_id,spend,clicks,inline_link_clicks,impressions,actions',
    'limit': 500,
}, start_date, end_date)

geo_by_bucket = {}
for r in geo_raw:
    bucket = (r.get('campaign_id'), r.get('country', 'unknown'))
    geo_by_bucket.setdefault(bucket, {})[r['date_start']] = r

geo = []
for (campaign_id, country), by_date in geo_by_bucket.items():
    daily = [to_day_row(r) for _, r in sorted(by_date.items())]
    geo.append({"campaign_id": campaign_id, "country": country, "daily": daily})

# ============================================================
# 8. Охват — отдельными некумулятивными запросами по стандартным периодам,
# отдельно на каждый кемп (фильтр по campaign.id — без задвоения людей между
# кампаниями одного кемпа) и один раз общий по всему аккаунту.
# ============================================================
def fetch_reach(since, until, campaign_ids=None):
    params = {
        'time_range': json.dumps({'since': since, 'until': until}),
        'fields': 'reach',
        'limit': 1,
    }
    if campaign_ids:
        params['filtering'] = json.dumps([{'field': 'campaign.id', 'operator': 'IN', 'value': campaign_ids}])
    raw = api_get(f"{ACCOUNT_ID}/insights", params)
    return int(raw[0].get('reach', 0)) if raw else 0


month_start = end_date[:8] + '01'
PRESET_RANGES = {
    '7d': ((datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=6)).strftime('%Y-%m-%d'), end_date),
    '14d': ((datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=13)).strftime('%Y-%m-%d'), end_date),
    '30d': ((datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=29)).strftime('%Y-%m-%d'), end_date),
    'month': (month_start, end_date),
    'all': (start_date, end_date),
}

reach_by_preset = {
    'all': {key: fetch_reach(since, until) for key, (since, until) in PRESET_RANGES.items()}
}
for camp_name in sorted(set(CAMP_KEYWORDS.values())):
    camp_campaign_ids = [c['id'] for c in campaigns if c['camp'] == camp_name]
    if camp_campaign_ids:
        reach_by_preset[camp_name] = {key: fetch_reach(since, until, camp_campaign_ids) for key, (since, until) in PRESET_RANGES.items()}
    else:
        reach_by_preset[camp_name] = {key: 0 for key in PRESET_RANGES}


# ============================================================
# Итоговый файл
# ============================================================
report_data = {
    "last_updated": datetime.now().strftime('%d.%m.%Y, %H:%M'),
    "fetched_range": {"since": start_date, "until": end_date},
    "plan": {"per_camp_budget": PLAN_PER_CAMP_BUDGET},
    "camps": sorted(set(CAMP_KEYWORDS.values())),
    "account_daily": account_daily,
    "campaigns": campaigns,
    "adsets": adsets,
    "creatives": creatives,
    "age_groups": age_groups,
    "devices": devices,
    "geo": geo,
    "reach_by_preset": reach_by_preset,
}

os.makedirs('data', exist_ok=True)
with open('data/report.json', 'w', encoding='utf-8') as f:
    json.dump(report_data, f, ensure_ascii=False, indent=2)

total_spend = sum(d['spend'] for d in account_daily)
total_messages = sum(d['messages'] for d in account_daily)
total_followers = sum(d['followers'] for d in account_daily)
campaigns_without_camp = [c['name'] for c in campaigns if not c['camp']]

print(f"Готово: {len(account_daily)} дней, {len(campaigns)} кампаний, {len(adsets)} аудиторий, {len(creatives)} объявлений.")
print(f"Итого: расход ${total_spend:.2f}, начатых переписок {total_messages}, подписчиков {total_followers}.")
if campaigns_without_camp:
    print(f"Кампании без определённого кемпа ({len(campaigns_without_camp)}): {campaigns_without_camp}")

# ============================================================
# ДИАГНОСТИКА action_type — раскомментировано специально для проверки подписчиков.
# После того как найдём правильный тип и впишем в FOLLOWER_ACTION_TYPES —
# этот блок нужно закомментировать обратно (вернуть # в начало каждой строки).
# ============================================================
action_type_totals = {}
for r in account_raw:
    for action in r.get('actions', []) or []:
        t = action.get('action_type')
        v = int(action.get('value', 0))
        action_type_totals[t] = action_type_totals.get(t, 0) + v
print("Все типы конверсий за период и их суммы:")
for t, v in sorted(action_type_totals.items(), key=lambda x: -x[1]):
    print(f"  {t}: {v}")
