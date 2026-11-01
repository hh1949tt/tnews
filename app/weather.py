"""今日天气：Open-Meteo 免费接口（无需 API Key）。

geocode()   地名 → 坐标（支持中文）
reverse()   坐标 → 地名（用于「使用当前位置」）
report()    坐标 → 一句话天气播报文案
"""
from __future__ import annotations

import httpx

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
REVERSE_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

UA = "TNews/1.5 (+weather)"

# WMO weather code → 中文描述
WMO = {
    0: "晴", 1: "大致晴朗", 2: "局部多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "中阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}


def _desc(code) -> str:
    try:
        return WMO.get(int(code), "天气多变")
    except (TypeError, ValueError):
        return "天气多变"


def _wind_txt(kmh) -> str:
    try:
        v = float(kmh)
    except (TypeError, ValueError):
        return ""
    if v < 6:
        return "微风"
    if v < 20:
        return "风力和缓"
    if v < 39:
        return "风力较大"
    return "风很大，注意防风"


async def geocode(query: str, count: int = 5) -> list[dict]:
    """地名搜索，返回 [{label, name, admin, country, lat, lon}]。"""
    if not query.strip():
        return []
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": UA}) as client:
        r = await client.get(GEO_URL, params={
            "name": query.strip(), "count": count, "language": "zh", "format": "json",
        })
        r.raise_for_status()
        results = r.json().get("results") or []
    out = []
    for it in results:
        name = it.get("name") or ""
        admin = it.get("admin1") or ""
        country = it.get("country") or ""
        # 国内城市常直接叫「朝阳」「西湖」，带上省名避免选错
        label = name if not admin or admin in name else f"{name}（{admin}）"
        if country and country != "中国":
            label += f" · {country}"
        out.append({
            "label": label, "name": name, "admin": admin, "country": country,
            "lat": it["latitude"], "lon": it["longitude"],
        })
    return out


async def reverse(lat: float, lon: float) -> str | None:
    """坐标 → 城市名（尽力而为，失败返回 None）。"""
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": UA}) as client:
            r = await client.get(REVERSE_URL, params={
                "latitude": lat, "longitude": lon, "localityLanguage": "zh",
            })
            r.raise_for_status()
            d = r.json()
        return d.get("city") or d.get("locality") or d.get("principalSubdivision")
    except Exception:
        return None


async def report(lat: float, lon: float, place: str = "") -> dict:
    """拉取当天天气并生成一句话播报。返回 {place, desc, temp, tmax, tmin, pop, text}。"""
    params = {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    }
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": UA}) as client:
        r = await client.get(FORECAST_URL, params=params)
        r.raise_for_status()
        d = r.json()

    cur = d.get("current") or {}
    daily = d.get("daily") or {}
    desc = _desc(cur.get("weather_code"))
    temp = cur.get("temperature_2m")
    tmax = (daily.get("temperature_2m_max") or [None])[0]
    tmin = (daily.get("temperature_2m_min") or [None])[0]
    pop = (daily.get("precipitation_probability_max") or [None])[0]
    wind = _wind_txt(cur.get("wind_speed_10m"))

    def deg(v):
        try:
            return f"{round(float(v))} 度"
        except (TypeError, ValueError):
            return ""

    parts = []
    where = f"{place}" if place else "今天"
    lead = f"今天{where}{desc}" if place else f"今天{desc}"
    if temp is not None and tmax is not None and tmin is not None:
        # 早晚听节目时报当天区间更实用，当前气温做参考
        parts.append(f"{lead}，当前{deg(temp)}，全天{deg(tmin)}到{deg(tmax)}")
    elif temp is not None:
        parts.append(f"{lead}，当前{deg(temp)}")
    else:
        parts.append(f"{lead}")
    if pop is not None and int(pop) >= 30:
        parts.append(f"降水概率{int(pop)}%，记得带伞")
    elif desc in ("晴", "大致晴朗"):
        parts.append("天气不错")
    if wind:
        parts.append(wind)
    text = "，".join(parts) + "。"
    text = text.replace("，，", "，").replace("。。", "。")
    return {
        "place": place or "当前位置", "desc": desc, "temp": temp,
        "tmax": tmax, "tmin": tmin, "pop": pop,
        "text": text,
    }
