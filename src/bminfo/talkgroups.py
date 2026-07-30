from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

logger = logging.getLogger(__name__)
UTC = timezone.utc
from .i18n import catalog


# Canonical labels used for storage are the English metadata catalog. Localized
# labels are served separately by /locales/{locale}; machine values remain codes.
COUNTRY_NAMES: dict[str, str] = catalog("en")["metadata"]["countries"]

# These locally defined talkgroups are part of the dashboard contract even if
# the upstream BrandMeister metadata feed omits or renames them.
PINNED_TALKGROUPS: tuple[tuple[int, str, str, str, str], ...] = (
    (214001, "Sala Andalucía", "ES", "Europe", "Spain"),
)


COUNTRY_TO_CONTINENT: dict[str, str] = {
    "WW": "Other", "Global": "Global", "XX": "Other", "Unknown": "Other",
    "EU": "Europe", "NA": "Africa", "SA": "Asia", "AF": "Asia", "AS": "Asia", "OC": "Oceania",
    "US": "North America", "CA": "North America", "MX": "North America", "GT": "North America",
    "BZ": "North America", "SV": "North America", "HN": "North America", "NI": "North America",
    "CR": "North America", "PA": "North America", "BR": "South America", "AR": "South America",
    "CL": "South America", "CO": "South America", "VE": "South America", "PE": "South America",
    "EC": "South America", "BO": "South America", "PY": "South America", "UY": "South America",
    "GY": "South America", "SR": "South America", "GF": "South America",
}

for _code in "GB DE FR ES IT NL BE CH AT PL CZ SE NO DK FI PT GR HU RO BG HR SI SK LT LV EE IE LU MT CY IS AL MK RS BA ME XK MD UA BY RU".split():
    COUNTRY_TO_CONTINENT[_code] = "Europe"
for _code in "TR IL AE QA KW OM BH JO LB SY IQ IR PK IN BD LK NP AF MM TH VN LA KH MY SG ID PH BN TL CN TW HK MO KR KP JP MN AM AZ GE KZ".split():
    COUNTRY_TO_CONTINENT[_code] = "Asia"
for _code in "AU NZ FJ PG NC WS TO VU SB".split():
    COUNTRY_TO_CONTINENT[_code] = "Oceania"
for _code in "EG DZ MA TN LY SD SS ET SO KE UG TZ RW BI DJ ER MG MU KM SC ZA NA BW ZW ZM MW MZ AO CD CG CF TD CM GQ GA ST GH NG BJ TG BF CI LR SL GN GW GM SN MR ML NE RE".split():
    COUNTRY_TO_CONTINENT[_code] = "Africa"
for _code in "AD AM AZ BS CU CW DO FO GD GE HT JM KZ LC LI PR RE SM TC TT".split():
    COUNTRY_TO_CONTINENT.setdefault(_code, "Europe" if _code in {"AD", "FO", "LI", "SM"} else "North America")


# ITU-T E.212 mobile country codes used by the reference service.
MCC_TO_COUNTRY: dict[str, str] = {
    "202": "GR", "204": "NL", "206": "BE", "208": "FR", "213": "AD", "214": "ES",
    "216": "HU", "218": "BA", "219": "HR", "220": "RS", "222": "IT", "226": "RO",
    "228": "CH", "230": "CZ", "231": "SK", "232": "AT", "235": "GB", "238": "DK",
    "240": "SE", "242": "NO", "244": "FI", "246": "LT", "247": "LV", "248": "EE",
    "255": "UA", "259": "MD", "260": "PL", "262": "DE", "263": "DE", "264": "DE",
    "265": "DE", "268": "PT", "270": "LU", "272": "IE", "274": "IS", "276": "AL",
    "278": "MT", "280": "CY", "282": "GE", "283": "AM", "284": "BG", "286": "TR",
    "288": "FO", "292": "SM", "293": "SI", "294": "MK", "295": "LI", "297": "ME",
    "302": "CA", "310": "US", "311": "US", "312": "US", "313": "US", "314": "US",
    "315": "US", "316": "US", "317": "US", "318": "US", "319": "US", "330": "PR",
    "334": "MX", "338": "JM", "352": "GD", "358": "LC", "362": "CW", "364": "BS",
    "368": "CU", "370": "DO", "372": "HT", "374": "TT", "376": "TC", "400": "AZ",
    "401": "KZ", "404": "IN", "410": "PK", "415": "LB", "420": "SA", "422": "OM",
    "425": "IL", "426": "BH", "427": "QA", "430": "AE", "440": "JP", "450": "KR",
    "452": "VN", "454": "HK", "460": "CN", "470": "BD", "502": "MY", "505": "AU",
    "510": "ID", "515": "PH", "520": "TH", "525": "SG", "530": "NZ", "602": "EG",
    "604": "MA", "655": "ZA", "647": "RE", "704": "GT", "706": "SV", "708": "HN",
    "710": "NI", "712": "CR", "714": "PA", "716": "PE", "722": "AR", "724": "BR",
    "730": "CL", "732": "CO", "734": "VE", "740": "EC", "748": "UY", "250": "RU",
}


SPECIAL_PREFIX_TO_COUNTRY: dict[str, str] = {
    "2570": "BY", "6470": "RE", "6471": "RE", "899": "XX", "907": "XX",
    "910": "DE", "913": "XX", "914": "XX", "915": "XX", "916": "XX", "918": "XX",
    "920": "DE", "922": "NL", "923": "XX", "924": "SE", "927": "XX", "930": "GR",
    "937": "FR", "940": "XX", "955": "XX", "969": "XX", "971": "ES", "973": "XX",
}


@dataclass(frozen=True)
class TalkgroupRecord:
    talkgroup_id: int
    name: str
    country: str
    continent: str
    full_country_name: str
    last_updated: datetime


def classify_talkgroup(talkgroup_id: int) -> tuple[str, str, str]:
    """Return country code, continent, and display country name."""
    text = str(int(talkgroup_id))
    if text.startswith("9"):
        country = "Global"
    elif 46600 <= talkgroup_id <= 46699:
        country = "TW"
    elif 250000 <= talkgroup_id <= 250999:
        country = "RU"
    else:
        country = next(
            (value for prefix, value in sorted(SPECIAL_PREFIX_TO_COUNTRY.items(), key=lambda item: -len(item[0])) if text.startswith(prefix)),
            None,
        )
        if country is None:
            country = MCC_TO_COUNTRY.get(text[:3], "XX") if len(text) >= 3 else "XX"
    full_name = COUNTRY_NAMES.get(country, country)
    continent = COUNTRY_TO_CONTINENT.get(country, "Global" if country == "Global" else "Other")
    return country, continent, full_name


def _normalise_records(payload: Any) -> list[tuple[int, str]]:
    if isinstance(payload, Mapping):
        items = payload.items()
    elif isinstance(payload, list):
        items = ((item.get("talkgroup_id", item.get("id")), item.get("name")) for item in payload if isinstance(item, Mapping))
    else:
        return []
    records: list[tuple[int, str]] = []
    for raw_id, raw_name in items:
        try:
            talkgroup_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        name = str(raw_name or "").strip()
        if talkgroup_id > 0 and name and talkgroup_id != 9:
            records.append((talkgroup_id, name))
    return records


def fetch_talkgroups(url: str = "https://api.brandmeister.network/v2/talkgroup") -> list[TalkgroupRecord]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "bminfo/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    updated = datetime.now(tz=UTC)
    records = {
        tg_id: TalkgroupRecord(tg_id, name, *classify_talkgroup(tg_id), updated)
        for tg_id, name in _normalise_records(payload)
    }
    for tg_id, name, country, continent, full_country_name in PINNED_TALKGROUPS:
        records[tg_id] = TalkgroupRecord(
            tg_id, name, country, continent, full_country_name, updated
        )
    return list(records.values())


def sync_talkgroups(dsn: str, url: str = "https://api.brandmeister.network/v2/talkgroup") -> int:
    import psycopg

    records = fetch_talkgroups(url)
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            for record in records:
                cursor.execute(
                    """
                    INSERT INTO talkgroups
                        (talkgroup_id, name, country, continent, full_country_name, last_updated)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (talkgroup_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        country = EXCLUDED.country,
                        continent = EXCLUDED.continent,
                        full_country_name = EXCLUDED.full_country_name,
                        last_updated = EXCLUDED.last_updated
                    """,
                    (record.talkgroup_id, record.name, record.country, record.continent,
                     record.full_country_name, record.last_updated),
                )
    logger.info("synchronized %d BrandMeister talkgroups", len(records))
    return len(records)
