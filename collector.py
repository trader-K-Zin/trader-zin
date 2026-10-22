import os
import datetime
import time
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import pandas as pd

now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
DB_FILE = "history_db.csv"
INV_DB_FILE = "inverse_db.csv"  # [신규] 코스피 인버스 1년 역추적 전용 영구 DB
REPORT_FILE = "WIN_RATE_REPORT.md"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

ETF_KEYWORDS = (
    "KODEX", "TIGER", "KBSTAR", "ACE", "SOL", "ARIRANG", "HANARO",
    "KOSEF", "PLUS", "RISE", "TIMEFOLIO", "WOORI", "히어로즈", "마이티", "ETN", "스팩"
)

def is_pure_stock(name):
    if not name:
        return False
    for kw in ETF_KEYWORDS:
        if kw in name.upper():
            return False
    return True

def to_float(val):
    if val is None:
        return 0.0
    s = str(val).replace(",", "").replace("+", "").replace("%", "").strip()
    if not s or s == "-":
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0

def get_popular_top10():
    """네이버 금융 실시간 인기 검색어 TOP 10 티커 추출"""
    top10 = set()
    try:
        url = "https://finance.naver.com/sise/lastsearch2.naver"
        res = requests.get(url, headers=HEADERS, timeout=8)
        html = res.content.decode("euc-kr", "replace")
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a.tltle"):
            href = a.get("href", "")
            if "code=" in href:
                code = href.split("code=")[-1].strip()
                if len(code) == 6:
                    top10.add(code)
            if len(top10) >= 10:
                break
    except Exception:
        pass
    return top10

def get_major_universe():
    """코스피/코스닥 시가총액 및 거래 활발 상위 순수 개별주(ETF 제외) 확보"""
    tickers = {}
    for market in ["KOSPI", "KOSDAQ"]:
        for page in [1, 2, 3, 4]:
            url = f"https://m.stock.naver.com/api/stocks/marketValue/{market}?page={page}&pageSize=50"
            try:
                res = requests.get(url, headers=HEADERS, timeout=8)
                if res.status_code == 200:
                    stocks = res.json().get("stocks", [])
                    for s in stocks:
                        code = s.get("itemCode")
                        name = s.get("stockName")
                        if code and name and is_pure_stock(name):
                            tickers[code] = name
            except Exception:
                pass
    return tickers

def get_naver_chart_df(ticker, count=260):
    """일봉 로드 (개별주 및 KOSPI 지수 공통 지원, 1년=260거래일 대응)"""
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={ticker}&timeframe=day&count={count}&requestType=0"
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        root = ET.fromstring(res.text)
        rows = []
        for item in root.findall(".//item"):
            d = item.attrib.get("data", "").split("|")
            if len(d) >= 6:
                rows.append({
                    "Date": d[0].strip(),
                    "Open": to_float(d[1]),
                    "High": to_float(d[2]),
                    "Low": to_float(d[3]),
                    "Close": to_float(d[4]),
                    "Volume": to_float(d[5])
                })
        df = pd.DataFrame(rows)
        if not df.empty:
            df["Prev_Close"] = df["Close"].shift(1)
            df["Fluc"] = ((df["Close"] - df["Prev_Close"]) / df["Prev_Close"] * 100).fillna(0.0)
            df["Is_Bullish"] = (df["Close"] > df["Open"]) & (df["Fluc"] >= 1.0)
            df["Is_Candle_Plus"] = df["Close"] > df["Open"]
            df["Val_Approx"] = df["Close"] * df["Volume"]
        return df
    except Exception:
        return pd.DataFrame()

def get_investor_data(ticker, max_pages=8):
    """외국인/기관 수급 데이터 로드"""
    inv_map = {}
    for page in range(1, max_pages + 1):
        url = f"https://m.stock.naver.com/api/stock/{ticker}/trend?page={page}&pageSize=20"
        try:
            res = requests.get(url, headers=HEADERS, timeout=6)
            if res.status_code == 200:
                data = res.json()
                items = data if isinstance(data, list) else data.get("trends", data.get("item", []))
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        b_date = str(item.get("bizdate", "")).replace("-", "").replace(".", "").strip()
                        if len(b_date) == 8 and b_date.isdigit():
                            f_q = to_float(item.get("foreignerPureBuyQuant", 0))
                            o_q = to_float(item.get("organPureBuyQuant", 0))
                            inv_map[b_date] = (o_q, f_q)
        except Exception:
            pass

    if not inv_map:
        for page in range(1, max_pages + 1):
            url = f"https://finance.naver.com/item/frgn.naver?code={ticker}&page={page}"
            try:
                res = requests.get(url, headers=HEADERS, timeout=6)
                html = res.content.decode("euc-kr", "replace")
                soup = BeautifulSoup(html, "html.parser")
                for tr in soup.select("table.type2 tr"):
                    tds = tr.find_all("td")
                    if len(tds) >= 7:
                        dt_txt = tds[0].text.strip().replace(".", "").replace("-", "")
                        if len(dt_txt) == 8 and dt_txt.isdigit():
                            o_q = to_float(tds[5].text)
                            f_q = to_float(tds[6].text)
                            inv_map[dt_txt] = (o_q, f_q)
            except Exception:
                pass
    return inv_map

def get_kospi_market_investor(max_pages=45):
    """코스피 시장 전체(현물) 외국인/기관 순매수 추이 로드 (최근 1년치 커버)"""
    mkt_map = {}
    # 1차: 모바일 API 또는 네이버 증권 투자자별 매매동향 일별 시세 크롤링
    today_str = now.strftime("%Y%m%d")
    for page in range(1, max_pages + 1):
        url = f"https://finance.naver.com/sise/investorDealTrendDay.naver?bizdate={today_str}&sosok=01&page={page}"
        try:
            res = requests.get(url, headers=HEADERS, timeout=6)
            html = res.content.decode("euc-kr", "replace")
            soup = BeautifulSoup(html, "html.parser")
            for tr in soup.select("table.type_1 tr"):
                tds = tr.find_all("td")
                if len(tds) >= 4:
                    dt_raw = tds[0].text.strip().replace(".", "").replace("-", "")
                    if len(dt_raw) == 6 and dt_raw.isdigit():
                        dt_str = "20" + dt_raw
                    elif len(dt_raw) == 8 and dt_raw.isdigit():
                        dt_str = dt_raw
                    else:
                        continue
                    # tds[1]: 개인, tds[2]: 외국인, tds[3]: 기관계
                    f_val = to_float(tds[2].text)
                    o_val = to_float(tds[3].text)
                    mkt_map[dt_str] = (o_val, f_val)
        except Exception:
            pass
    return mkt_map

def evaluate_signals(code, target_day, d_m1, d_m2, chart_cache, inv_cache, pop_set):
    """특정 기준일(D0)의 12대 시그널 및 태그를 일관되게 산출"""
    df_c = chart_cache[code]
    df_until = df_c[df_c["Date"] <= target_day]
    cur = df_until.iloc[-1]
    c0 = float(cur["Close"])
    h0 = float(cur["High"])
    fluc0 = float(cur["Fluc"])
    val0 = float(cur["Val_Approx"])

    high_60d = float(df_until["High"].iloc[-60:].max())
    avg_val_20d = float(df_until["Val_Approx"].iloc[-21:-1].mean()) if len(df_until) > 20 else val0
    ma5 = float(df_until["Close"].iloc[-5:].mean())
    ma20 = float(df_until["Close"].iloc[-20:].mean())
    ma60 = float(df_until["Close"].iloc[-60:].mean()) if len(df_until) >= 60 else ma20

    inv_data = inv_cache.get(code, {})
    inst_0, for_0 = inv_data.get(target_day, (0.0, 0.0))
    inst_m1, for_m1 = inv_data.get(d_m1, (0.0, 0.0))
    inst_m2, for_m2 = inv_data.get(d_m2, (0.0, 0.0))

    s1_pop = 1 if (code in pop_set) else 0
    s2_for = 1 if for_0 > 0 else 0
    s3_for3 = 1 if (for_0 > 0 and for_m1 > 0 and for_m2 > 0) else 0
    s4_inst = 1 if inst_0 > 0 else 0
    s5_inst3 = 1 if (inst_0 > 0 and inst_m1 > 0 and inst_m2 > 0) else 0
    s6_high = 1 if c0 >= high_60d * 0.95 else 0
    s7_vol = 1 if (avg_val_20d > 0 and val0 >= avg_val_20d * 2.5) else 0
    s8_twin = 1 if (s2_for == 1 and s4_inst == 1) else 0
    s9_major = 1 if ((s2_for == 1 or s4_inst == 1) and val0 >= avg_val_20d * 1.5) else 0
    s10_candle = 1 if (fluc0 >= 3.0 and h0 > 0 and c0 >= h0 * 0.98) else 0
    s11_trend = 1 if (ma5 > ma20 > ma60) else 0
    s12_scombo = 1 if (s8_twin == 1 and (s6_high == 1 or s10_candle == 1)) else 0

    sig_dict = {
        "S1_인기검색TOP10": s1_pop,
        "S2_외국인순매수": s2_for,
        "S3_외국인3일연속": s3_for3,
        "S4_기관순매수": s4_inst,
        "S5_기관3일연속": s5_inst3,
        "S6_신고가가시권": s6_high,
        "S7_거래대금폭발": s7_vol,
        "S8_외인기관양매수": s8_twin,
        "S9_메이저수급집중": s9_major,
        "S10_꽉찬양봉마감": s10_candle,
        "S11_이평선정배열": s11_trend,
        "S12_S급복합시그널": s12_scombo
    }
    sig_count = sum(sig_dict.values())

    traits = []
    if s1_pop:
        traits.append("🔥인기TOP10")
    if s8_twin:
        traits.append("양매수")
    elif s2_for:
        traits.append("외인매수")
    elif s4_inst:
        traits.append("기관매수")
    if s3_for3 or s5_inst3:
        traits.append("3일연속수급")
    if s9_major:
        traits.append("메이저수급")
    if s6_high:
        traits.append("신고가가시권")
    if s7_vol:
        traits.append("대금폭발")
    if s10_candle:
        traits.append("꽉찬양봉")
    if s11_trend:
        traits.append("정배열")

    return c0, fluc0, sig_dict, sig_count, (", ".join(traits) if traits else "양봉 주도주")


def evaluate_inverse_signals(d0_date, df_kospi, kospi_mkt_inv, kodex200_inv, gobbus_inv, df_nasdaq_proxy):
    """
    [신규] 특정일(D0) 오후 3시 30분 기준 코스피 인버스(하락) 12대 요건 판독
    - 선물 외인 매도 프록시: KODEX 200선물인버스2X(252670) 외인/기관 순매수 (선물 숏 포지션과 직결)
    - 오후 3시 반 나스닥 흐름: TIGER 미국나스닥100레버리지(418660) 또는 KODEX 미국나스닥100(379810)의 15:30 종가 등락률 및 음봉 여부
    """
    df_k_until = df_kospi[df_kospi["Date"] <= d0_date]
    if len(df_k_until) < 20:
        return None

    cur = df_k_until.iloc[-1]
    c0 = float(cur["Close"])
    o0 = float(cur["Open"])
    h0 = float(cur["High"])
    l0 = float(cur["Low"])
    fluc0 = float(cur["Fluc"])

    # 최근 3일, 4일 연속 양봉(또는 상승) 체크
    last3 = df_k_until.iloc[-3:]
    last4 = df_k_until.iloc[-4:]
    is_3bull = int(all((r["Close"] > r["Open"]) or (r["Fluc"] > 0) for _, r in last3.iterrows()))
    is_4bull = int(all((r["Close"] > r["Open"]) or (r["Fluc"] > 0) for _, r in last4.iterrows()))

    # 단기 과열 (20일선 이격도 102.5% 이상 or 5일선 대비 급등)
    ma20 = float(df_k_until["Close"].iloc[-20:].mean())
    disparity20 = (c0 / ma20 * 100) if ma20 > 0 else 100.0
    is_overheat = 1 if (disparity20 >= 102.5 or fluc0 >= 1.5) else 0

    # 윗꼬리 저항 음봉/약세 (고가 대비 종가가 -0.6% 이상 밀림)
    is_upper_wick = 1 if (h0 > 0 and ((h0 - c0) / h0 * 100) >= 0.6) else 0

    # 20일선 데드크로스/역배열 진입
    ma5 = float(df_k_until["Close"].iloc[-5:].mean())
    is_below_ma20 = 1 if (c0 < ma20 and ma5 < ma20) else 0

    # 코스피 현물 수급 (없을 경우 KODEX 200(069500) 수급으로 자동 보완)
    inst_k, for_k = kospi_mkt_inv.get(d0_date, kodex200_inv.get(d0_date, (0.0, 0.0)))
    is_for_only_sell = 1 if (for_k < 0 and inst_k >= 0) else 0
    is_inst_only_sell = 1 if (inst_k < 0 and for_k >= 0) else 0
    is_for_sell = 1 if for_k < 0 else 0
    is_inst_sell = 1 if inst_k < 0 else 0
    is_twin_sell = 1 if (for_k < 0 and inst_k < 0) else 0

    # 선물/인버스 수급: KODEX 200선물인버스2X(252670)에서 외국인 또는 기관이 순매수(=하락 베팅/선물 매도 헷지)
    inst_gob, for_gob = gobbus_inv.get(d0_date, (0.0, 0.0))
    is_fut_short_bet = 1 if (for_gob > 0 or inst_gob > 0) else 0

    # 오후 3시 30분 기준 미국 나스닥 선물 흐름 프록시 (국내 상장 나스닥 ETF의 15:30 마감 등락)
    nas_sub = df_nasdaq_proxy[df_nasdaq_proxy["Date"] == d0_date] if not df_nasdaq_proxy.empty else pd.DataFrame()
    if not nas_sub.empty:
        nas_r = nas_sub.iloc[0]
        nas_fluc = float(nas_r["Fluc"])
        nas_bear = 1 if (nas_fluc <= -0.4 or (nas_r["Close"] < nas_r["Open"] and nas_fluc < 0)) else 0
    else:
        nas_fluc = 0.0
        nas_bear = 0

    # S급 인버스 복합 시그널: (양매도 or 외인매도+선물숏) AND (4연속양봉/과열 or 오후나스닥약세 or 윗꼬리)
    core_supply_short = (is_twin_sell == 1) or (is_for_sell == 1 and is_fut_short_bet == 1)
    core_tech_short = (is_4bull == 1) or (is_3bull == 1) or (is_overheat == 1) or (nas_bear == 1) or (is_upper_wick == 1)
    is_s_inverse = 1 if (core_supply_short and core_tech_short) else 0

    inv_signals = {
        "I1_코스피3연속양봉": is_3bull,
        "I2_코스피4연속양봉": is_4bull,
        "I3_외인현물순매도": is_for_sell,
        "I4_외인단독순매도": is_for_only_sell,
        "I5_기관단독순매도": is_inst_only_sell,
        "I6_외인기관양매도": is_twin_sell,
        "I7_선물인버스외인기관매집": is_fut_short_bet,
        "I8_오후3시반나스닥선물약세": nas_bear,
        "I9_단기과열_이격과다": is_overheat,
        "I10_장중윗꼬리저항밀림": is_upper_wick,
        "I11_20일선하회역배열": is_below_ma20,
        "I12_S급인버스복합시그널": is_s_inverse,
    }
    inv_sig_cnt = sum(inv_signals.values())

    active_tags = []
    if is_4bull:
        active_tags.append("🔥4연속양봉과열")
    elif is_3bull:
        active_tags.append("3연속양봉")
    if is_twin_sell:
        active_tags.append("🚨외인기관양매도")
    elif is_for_only_sell:
        active_tags.append("외인단독매도")
    elif is_inst_only_sell:
        active_tags.append("기관단독매도")
    if is_fut_short_bet:
        active_tags.append("📉선물인버스메이저매집")
    if nas_bear:
        active_tags.append(f"🇺🇸15:30나스닥약세({nas_fluc:.1f}%)")
    if is_overheat:
        active_tags.append("단기이격과열")
    if is_upper_wick:
        active_tags.append("윗꼬리출현")
    if is_s_inverse:
        active_tags.append("⭐S급숏복합")

    return {
        "KOSPI_Close": round(c0, 2),
        "KOSPI_Fluc": round(fluc0, 2),
        "Nasdaq_1530_Fluc": round(nas_fluc, 2),
        "Inv_Sig_Count": inv_sig_cnt,
        "Inv_Traits": ", ".join(active_tags) if active_tags else "특이 숏 시그널 없음",
        "Signals": inv_signals
    }


def run_inverse_backtest_and_report(all_days):
    """
    [신규] 최근 1년(약 250거래일) 코스피 인버스 종가베팅 역추적 및 DB 영구 누적
    """
    print("Step 4: 코스피 지수 및 인버스/나스닥 프록시 최근 1년(260거래일) 데이터 수집 중...")
    df_kospi = get_naver_chart_df("KOSPI", count=260)
    df_gobbus = get_naver_chart_df("252670", count=260)  # KODEX 200선물인버스2X (곱버스)
    df_nasdaq = get_naver_chart_df("379810", count=260)  # KODEX 미국나스닥100TR (15:30 미 선물 흐름 반영)

    if df_kospi.empty or df_gobbus.empty:
        return []

    kospi_mkt_inv = get_kospi_market_investor(max_pages=30)
    kodex200_inv = get_investor_data("069500", max_pages=14)  # KODEX 200 수급 백업용
    gobbus_inv = get_investor_data("252670", max_pages=14)    # 곱버스 외인/기관 수급 (선물 숏 포지션 대용)

    if os.path.exists(INV_DB_FILE):
        try:
            inv_db = pd.read_csv(INV_DB_FILE, dtype={"Base_Date": str})
            existing_inv_dates = set(inv_db["Base_Date"].dropna().astype(str).unique())
        except Exception:
            inv_db = pd.DataFrame()
            existing_inv_dates = set()
    else:
        inv_db = pd.DataFrame()
        existing_inv_dates = set()

    k_days = df_kospi["Date"].tolist()
    eval_k_days = [d for d in k_days[:-1] if d not in existing_inv_dates]

    new_inv_rows = []
    for d0 in eval_k_days:
        idx = k_days.index(d0)
        if idx < 20 or idx + 1 >= len(k_days):
            continue
        d_next = k_days[idx + 1]

        res = evaluate_inverse_signals(d0, df_kospi, kospi_mkt_inv, kodex200_inv, gobbus_inv, df_nasdaq)
        if not res:
            continue

        # D+1일 코스피 하락폭 및 곱버스 상승폭 채점
        sub_k_next = df_kospi[df_kospi["Date"] == d_next]
        sub_g_cur = df_gobbus[df_gobbus["Date"] == d0]
        sub_g_next = df_gobbus[df_gobbus["Date"] == d_next]
        if sub_k_next.empty or sub_g_cur.empty or sub_g_next.empty:
            continue

        k_c0 = res["KOSPI_Close"]
        k_low_next = float(sub_k_next.iloc[0]["Low"])
        k_open_next = float(sub_k_next.iloc[0]["Open"])

        # 코스피 익일 최대 하락률(%) (음수값, 예: -1.2%)
        kospi_max_drop = round(((k_low_next - k_c0) / k_c0) * 100, 2)
        kospi_open_drop = round(((k_open_next - k_c0) / k_c0) * 100, 2)

        # 곱버스(252670) D0 종가 매수 -> D+1 장중 최고가 수익률(%)
        g_c0 = float(sub_g_cur.iloc[0]["Close"])
        g_high_next = float(sub_g_next.iloc[0]["High"])
        gobbus_max_ret = round(((g_high_next - g_c0) / g_c0) * 100, 2) if g_c0 > 0 else 0.0

        # 승리 기준 1: 익일 코스피 -0.5% 이상 하락 (또는 곱버스 +1.0% 이상 슈팅) -> 단타 익절권
        win_drop_05 = 1 if (kospi_max_drop <= -0.5 or gobbus_max_ret >= 1.0) else 0
        # 승리 기준 2: 익일 코스피 -1.0% 이상 하락 (또는 곱버스 +2.0% 이상 슈팅) -> 대승권
        win_drop_10 = 1 if (kospi_max_drop <= -1.0 or gobbus_max_ret >= 2.0) else 0

        row = {
            "Base_Date": d0,
            "Next_Date": d_next,
            "KOSPI_Close": k_c0,
            "KOSPI_Fluc": res["KOSPI_Fluc"],
            "Nasdaq_1530_Fluc": res["Nasdaq_1530_Fluc"],
            "Inv_Sig_Count": res["Inv_Sig_Count"],
            "Next_KOSPI_Open_Drop": kospi_open_drop,
            "Next_KOSPI_Max_Drop": kospi_max_drop,
            "Gobbus_Max_Ret": gobbus_max_ret,
            "Win_Drop_05Pct": win_drop_05,
            "Win_Drop_10Pct": win_drop_10,
        }
        row.update(res["Signals"])
        new_inv_rows.append(row)

    if new_inv_rows:
        inv_db = pd.concat([inv_db, pd.DataFrame(new_inv_rows)], ignore_index=True)
        inv_db.drop_duplicates(subset=["Base_Date"], keep="last", inplace=True)
        inv_db.sort_values(by="Base_Date", ascending=True, inplace=True)
        inv_db.to_csv(INV_DB_FILE, index=False, encoding="utf-8-sig")

    if inv_db.empty:
        return []

    inv_sig_cols = [c for c in inv_db.columns if c.startswith("I") and "_" in c]
    total_inv_days = len(inv_db)
    base_w05 = round(inv_db["Win_Drop_05Pct"].mean() * 100, 1)
    base_w10 = round(inv_db["Win_Drop_10Pct"].mean() * 100, 1)
    base_g_ret = round(inv_db["Gobbus_Max_Ret"].mean(), 2)

    inv_stats = []
    for col in inv_sig_cols:
        sub = inv_db[inv_db[col] == 1]
        cnt = len(sub)
        if cnt == 0:
            continue
        w05 = round(sub["Win_Drop_05Pct"].mean() * 100, 1)
        w10 = round(sub["Win_Drop_10Pct"].mean() * 100, 1)
        avg_drop = round(sub["Next_KOSPI_Max_Drop"].mean(), 2)
        avg_gob = round(sub["Gobbus_Max_Ret"].mean(), 2)
        s_title = col.split("_", 1)[1]
        inv_stats.append((s_title, cnt, w05, w10, avg_drop, avg_gob))

    # 코스피 -1.0% 이상 급락 확률(w10) -> -0.5% 하락 확률(w05) 순으로 내림차순 정렬
    inv_stats.sort(key=lambda x: (x[3], x[2], x[5]), reverse=True)

    latest_k_day = k_days[-1]
    today_inv_eval = evaluate_inverse_signals(
        latest_k_day, df_kospi, kospi_mkt_inv, kodex200_inv, gobbus_inv, df_nasdaq
    )

    sec_lines = [
        "\n---",
        "## 📉 4. [코스피 인버스 / 곱버스(252670) 종가베팅] 최근 1년 역추적 승률 TOP 5 & 당일 판독기",
        f"- **분석 모수:** 최근 **{total_inv_days}거래일** (`{inv_db['Base_Date'].min()}` ~ `{inv_db['Base_Date'].max()}`)",
        f"- **평상시(무조건 진입 시) 기본 확률:** 익일 코스피 -0.5%↓(곱버스 +1%↑) 확률 **{base_w05}%** / 코스피 -1.0%↓(곱버스 +2%↑) 확률 **{base_w10}%** (평균 곱버스 최고 **+{base_g_ret}%**)\n",
        "### 🏆 4-1. 익일 오전 10시 전 코스피 하락(인버스 수익) 확률이 가장 높은 조건 TOP 5",
        "| 순위 | 인버스(숏) 진입 요건 | 발생일수 | **익일 코스피 -0.5%↓ (곱버스 +1%↑)** | 🔥 **익일 코스피 -1.0%↓ (곱버스 +2%↑)** | 익일 코스피 평균최대하락 | **곱버스 평균최고수익** |",
        "| :---: | :--- | :---: | :---: | :---: | :---: | :---: |"
    ]

    for idx, (s_title, cnt, w05, w10, avg_drop, avg_gob) in enumerate(inv_stats[:5], 1):
        sec_lines.append(
            f"| **TOP {idx}** | **{s_title}** | {cnt}일 | **{w05}%** | 🔥 **{w10}%** | {avg_drop}% | **+{avg_gob}%** |"
        )

    sec_lines.extend([
        "\n### 📊 4-2. 전체 12대 인버스 요건별 상세 비교 (외인 단독 vs 기관 단독 vs 양매도 등)",
        "| 순위 | 인버스 세부 요건 | 표본수 | 익일 -0.5%↓ 승률 | **익일 -1.0%↓ 승률** | 코스피 평균저점 | 곱버스 평균최고 |",
        "| :---: | :--- | :---: | :---: | :---: | :---: | :---: |"
    ])
    for idx, (s_title, cnt, w05, w10, avg_drop, avg_gob) in enumerate(inv_stats, 1):
        sec_lines.append(
            f"| {idx}위 | **{s_title}** | {cnt}일 | {w05}% | **{w10}%** | {avg_drop}% | +{avg_gob}% |"
        )

    if today_inv_eval:
        t_cnt = today_inv_eval["Inv_Sig_Count"]
        t_traits = today_inv_eval["Inv_Traits"]
        recommend_msg = (
            "🚨 **[강력 인버스 종베 타점]** 복합 하락 시그널이 다수 포착되었습니다! (KODEX 200선물인버스2X / KODEX 인버스 종가 매수 유리)"
            if t_cnt >= 4 else
            "⚠️ **[관망 또는 소액 헷지]** 뚜렷한 지수 급락 시그널이 부족합니다. 인버스보다는 개별 주도주 종가베팅에 집중하세요."
        )
        sec_lines.extend([
            f"\n### 🎯 4-3. 금일({latest_k_day}) 오후 3시 30분 기준 코스피 인버스 진입 판독 결과",
            f"- **금일 코스피 종가:** `{today_inv_eval['KOSPI_Close']}` ({today_inv_eval['KOSPI_Fluc']:+}%) / **15:30 나스닥 프록시 등락:** `{today_inv_eval['Nasdaq_1530_Fluc']:+}%`",
            f"- **포착된 인버스 시그널 수:** **{t_cnt}개** ({t_traits})",
            f"- **수석비서 실전 판정:** {recommend_msg}\n"
        ])

    return sec_lines


def main():
    print("Step 1: 유니버스 및 전체 영업일 로드 중...")
    universe = get_major_universe()
    if not universe:
        return

    df_ref = get_naver_chart_df("005930", count=210)
    if df_ref.empty:
        return
    all_days = df_ref["Date"].tolist()

    if os.path.exists(DB_FILE):
        try:
            db_df = pd.read_csv(DB_FILE, dtype={"Ticker": str, "Base_Date": str})
            has_april = any(str(d).startswith("202604") for d in db_df["Base_Date"].dropna().unique())
            if "Bet3_Win_3Pct" not in db_df.columns or "Logic_V2" not in db_df.columns or not has_april:
                print("거래대금 상위 40위 선(先)필터링 기준 적용을 위해 전체 기간 통합 DB 역추적을 다시 시작합니다.")
                db_df = pd.DataFrame()
                existing_dates = set()
            else:
                existing_dates = set(db_df["Base_Date"].dropna().astype(str).unique())
        except Exception:
            db_df = pd.DataFrame()
            existing_dates = set()
    else:
        db_df = pd.DataFrame()
        existing_dates = set()

    print("Step 2: 개별 종목 210일 일봉 차트 캐싱 중...")
    chart_cache = {}
    for code in universe.keys():
        df_c = get_naver_chart_df(code, count=210)
        if not df_c.empty and len(df_c) >= 60:
            chart_cache[code] = df_c

    possible_eval_days = [d for d in all_days[:-3] if d >= "20260401"]
    eval_days = [d for d in possible_eval_days if d not in existing_dates]

    day_0_latest = all_days[-1]
    day_m1_latest = all_days[-2]
    day_m2_latest = all_days[-3]

    top40_by_day = {}
    top_vol_top10_by_day = {}
    tracked_tickers = set()

    days_to_scan = sorted(list(set(eval_days + [day_0_latest, day_m1_latest, day_m2_latest])))
    for d_str in days_to_scan:
        daily_stocks = []
        all_vol_pool = []
        for code, df_c in chart_cache.items():
            sub = df_c[df_c["Date"] == d_str]
            if not sub.empty:
                r = sub.iloc[0]
                val = float(r["Val_Approx"])
                fluc = float(r["Fluc"])
                is_bull = bool(r["Is_Bullish"])
                daily_stocks.append((code, val, is_bull, fluc))
                all_vol_pool.append((code, val * abs(fluc)))

        daily_stocks.sort(key=lambda x: x[1], reverse=True)
        top40_vol_stocks = daily_stocks[:40]

        selected_bullish_in_top40 = [
            (vol_rank, code)
            for vol_rank, (code, val, is_bull, fluc) in enumerate(top40_vol_stocks, 1)
            if is_bull and fluc >= 1.0
        ]

        all_vol_pool.sort(key=lambda x: x[1], reverse=True)

        top40_by_day[d_str] = selected_bullish_in_top40
        top_vol_top10_by_day[d_str] = set([x[0] for x in all_vol_pool[:10]])
        tracked_tickers.update([code for _, code in selected_bullish_in_top40])

    pages_to_fetch = 8 if len(eval_days) > 10 else 2
    print(f"Step 3: 대금 상위 40위 내 양봉 주도주 {len(tracked_tickers)}개의 수급 데이터({pages_to_fetch}페이지) 수집 중...")
    inv_cache = {}
    for code in tracked_tickers:
        inv_cache[code] = get_investor_data(code, max_pages=pages_to_fetch)
        time.sleep(0.01)

    popular_now = get_popular_top10()

    new_records = []
    if eval_days:
        print(f"신규 평가 대상일({len(eval_days)}거래일) 1·2·3차 종가베팅 채점 시작...")
        for d0_date in eval_days:
            idx = all_days.index(d0_date)
            if idx < 2 or idx + 3 >= len(all_days):
                continue
            d_minus1 = all_days[idx - 1]
            d_minus2 = all_days[idx - 2]
            d_plus1 = all_days[idx + 1]
            d_plus2 = all_days[idx + 2]
            d_plus3 = all_days[idx + 3]
            pop_proxies = top_vol_top10_by_day.get(d0_date, set())

            for vol_rank, code in top40_by_day.get(d0_date, []):
                name = universe.get(code, code)
                df_c = chart_cache[code]
                df_until = df_c[df_c["Date"] <= d0_date]
                if len(df_until) < 20:
                    continue

                c0, fluc0, sig_dict, sig_count, _ = evaluate_signals(
                    code, d0_date, d_minus1, d_minus2, chart_cache, inv_cache, pop_proxies
                )
                if c0 <= 0:
                    continue

                sub_p1 = df_c[df_c["Date"] == d_plus1]
                sub_p2 = df_c[df_c["Date"] == d_plus2]
                sub_p3 = df_c[df_c["Date"] == d_plus3]
                if sub_p1.empty or sub_p2.empty or sub_p3.empty:
                    continue

                h_p1, c_p1 = float(sub_p1.iloc[0]["High"]), float(sub_p1.iloc[0]["Close"])
                h_p2, c_p2 = float(sub_p2.iloc[0]["High"]), float(sub_p2.iloc[0]["Close"])
                h_p3, c_p3 = float(sub_p3.iloc[0]["High"]), float(sub_p3.iloc[0]["Close"])
                if c_p1 <= 0 or c_p2 <= 0:
                    continue

                bet1_ret = round(((h_p1 - c0) / c0) * 100, 2)
                bet1_win = 1 if bet1_ret >= 3.0 else 0

                bet2_ret = round(((h_p2 - c_p1) / c_p1) * 100, 2)
                bet2_win = 1 if bet2_ret >= 3.0 else 0

                bet3_ret = round(((h_p3 - c_p2) / c_p2) * 100, 2)
                bet3_win = 1 if bet3_ret >= 3.0 else 0

                rec = {
                    "Base_Date": d0_date,
                    "YearMonth": f"{str(d0_date)[:4]}-{str(d0_date)[4:6]}",
                    "D1_Date": d_plus1,
                    "D2_Date": d_plus2,
                    "D3_Date": d_plus3,
                    "Rank": vol_rank,
                    "Ticker": code,
                    "Name": name,
                    "D0_Close": int(c0),
                    "D0_Fluc": round(fluc0, 2),
                    "D1_Close": int(c_p1),
                    "D2_Close": int(c_p2),
                    "Sig_Count": sig_count,
                    "Pick_Rank": 0,
                    "Is_Top5": 0,
                    "Bet1_Max_Ret": bet1_ret,
                    "Bet1_Win_3Pct": bet1_win,
                    "Bet2_Max_Ret": bet2_ret,
                    "Bet2_Win_3Pct": bet2_win,
                    "Bet3_Max_Ret": bet3_ret,
                    "Bet3_Win_3Pct": bet3_win,
                    "Logic_V2": 1,
                }
                rec.update(sig_dict)
                new_records.append(rec)

        if new_records:
            new_df = pd.DataFrame(new_records)
            db_df = pd.concat([db_df, new_df], ignore_index=True)

    if db_df.empty:
        print("DB에 데이터가 없습니다.")
        return

    if "Regime" in db_df.columns:
        db_df.drop(columns=["Regime"], inplace=True)

    signal_cols = [c for c in db_df.columns if c.startswith("S") and "_" in c]
    db_df["Base_Date"] = db_df["Base_Date"].astype(str)
    db_df["YearMonth"] = db_df["Base_Date"].str.slice(0, 4) + "-" + db_df["Base_Date"].str.slice(4, 6)
    db_df["Sig_Count"] = db_df[signal_cols].sum(axis=1)

    db_df.drop_duplicates(subset=["Base_Date", "Ticker"], keep="last", inplace=True)
    db_df.sort_values(by=["Base_Date", "Sig_Count", "D0_Fluc", "Rank"], ascending=[True, False, False, True], inplace=True)
    db_df["Pick_Rank"] = db_df.groupby("Base_Date").cumcount() + 1
    db_df["Is_Top5"] = (db_df["Pick_Rank"] <= 5).astype(int)

    db_df.sort_values(by=["Base_Date", "Rank"], ascending=[True, True], inplace=True)
    db_df.to_csv(DB_FILE, index=False, encoding="utf-8-sig")
    print(f"영구 DB(총 {len(db_df)}건) 및 TOP 5 태깅 업데이트 완료!")

    total_cnt = len(db_df)
    unique_dates = sorted(db_df["Base_Date"].unique())
    total_days = len(unique_dates)
    min_dt = unique_dates[0]
    max_dt = unique_dates[-1]

    top5_df = db_df[db_df["Is_Top5"] == 1]
    top5_cnt = len(top5_df)

    def calc_metrics(sub):
        if len(sub) == 0:
            return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        return (
            round(sub["Bet1_Win_3Pct"].mean() * 100, 1),
            round(sub["Bet1_Max_Ret"].mean(), 2),
            round(sub["Bet2_Win_3Pct"].mean() * 100, 1),
            round(sub["Bet2_Max_Ret"].mean(), 2),
            round(sub["Bet3_Win_3Pct"].mean() * 100, 1),
            round(sub["Bet3_Max_Ret"].mean(), 2),
        )

    b1_w, b1_r, b2_w, b2_r, b3_w, b3_r = calc_metrics(db_df)
    t1_w, t1_r, t2_w, t2_r, t3_w, t3_r = calc_metrics(top5_df)

    lines = [
        "# 🎯 트레이더 진 주도주 릴레이 종가베팅(1·2·3차) & 코스피 인버스 영구 누적 리포트",
        f"- **최종 업데이트:** {now.strftime('%Y-%m-%d %H:%M')} (KST)",
        f"- **누적 검증 기간:** `{min_dt}` ~ `{max_dt}` (총 **{total_days}거래일** / 전체 표본 **{total_cnt:,}개** / TOP 5 표본 **{top5_cnt:,}개**)",
        f"- **선정 기준:** 당일 **거래대금 상위 40위 선(先)확정** 후 ➔ 그중 **+1% 이상 양봉 마감 종목**만 엄선 vs **깃허브 픽 TOP 5**\n",
        "## 🏆 0-1. [전체 모수(대금 40위 내 양봉) vs 깃허브 추천 픽(TOP 5)] 실전 승률 비교",
        "| 구분 | 1차 종베(D0➔D1) 승률 | 1차 평균최고 | **2차 종베(D1➔D2) 승률** | 2차 평균최고 | 3차 종베(D2➔D3) 승률 | 3차 평균최고 |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
        f"| **대금 40위 내 양봉 주도주** | {b1_w}% | +{b1_r}% | **{b2_w}%** | +{b2_r}% | {b3_w}% | +{b3_r}% |",
        f"| ⭐ **깃허브 추천 TOP 5 (일 5개)** | **{t1_w}%** | **+{t1_r}%** | 🔥 **{t2_w}%** | **+{t2_r}%** | **{t3_w}%** | **+{t3_r}%** |\n",
        "## 📈 0-2. 최근 시황 흐름별(Rolling Window) 동적 승률 추이 (장세 자동 감지)",
        "| 조회 구간 (시황 온도계) | 거래일수 | 전체 1차 종베 | 전체 2차 종베 | ⭐ **TOP 5 1차 종베** | 🔥 **TOP 5 2차 종베** | ⭐ **TOP 5 3차 종베** |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |"
    ]

    windows = [
        ("🌐 **전체 누적 (All-Time)**", unique_dates),
        ("📅 **최근 60거래일 (분기 추세)**", unique_dates[-60:]),
        ("🗓️ **최근 20거래일 (한 달 시황)**", unique_dates[-20:]),
        ("⚡ **최근 5거래일 (이번 주 체감)**", unique_dates[-5:])
    ]
    for w_label, d_list in windows:
        sub_all = db_df[db_df["Base_Date"].isin(d_list)]
        sub_t5 = sub_all[sub_all["Is_Top5"] == 1]
        aw1, _, aw2, _, _, _ = calc_metrics(sub_all)
        tw1, _, tw2, _, tw3, _ = calc_metrics(sub_t5)
        lines.append(f"| {w_label} | {len(d_list)}일 | {aw1}% | {aw2}% | **{tw1}%** | **{tw2}%** | **{tw3}%** |")

    lines.extend([
        "\n## 🗓️ 0-3. 월별(YYYY-MM) 자동 누적 성적표 (미래 영구 확장형)",
        "| 년-월 (YYYY-MM) | 검증 표본(전체/TOP5) | 전체 1차 승률 | 전체 2차 승률 | ⭐ **TOP 5 1차 승률** | 🔥 **TOP 5 2차 승률** | ⭐ **TOP 5 3차 승률** |",
        "| :---: | :---: | :---: | :---: | :---: | :---: | :---: |"
    ])

    for ym in sorted(db_df["YearMonth"].unique()):
        sub_ym = db_df[db_df["YearMonth"] == ym]
        sub_ym_t5 = sub_ym[sub_ym["Is_Top5"] == 1]
        aw1, _, aw2, _, _, _ = calc_metrics(sub_ym)
        tw1, _, tw2, _, tw3, _ = calc_metrics(sub_ym_t5)
        lines.append(f"| **{ym}** | {len(sub_ym)}건 / {len(sub_ym_t5)}건 | {aw1}% | {aw2}% | **{tw1}%** | **{tw2}%** | **{tw3}%** |")

    lines.extend([
        "\n---",
        "## 📊 1. 12대 개별 시그널 옵션별 [1차 vs 2차 vs 3차 종가베팅] 누적 승률 랭킹",
        "| 순위 | D0 발생 옵션 (시그널) | 누적 표본 | **1차 종베 승률(D0➔D1)** | 1차 최고 | **2차 종베 승률(D1➔D2)** | 2차 최고 | **3차 종베 승률(D2➔D3)** | 3차 최고 |",
        "| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |"
    ])

    stats = []
    for col in signal_cols:
        sub = db_df[db_df[col] == 1]
        cnt = len(sub)
        w1, r1, w2, r2, w3, r3 = calc_metrics(sub)
        s_name = col.split("_", 1)[1]
        stats.append((s_name, cnt, w1, r1, w2, r2, w3, r3))

    stats.sort(key=lambda x: (x[4], x[2], x[6]), reverse=True)
    for idx, (s_name, cnt, w1, r1, w2, r2, w3, r3) in enumerate(stats, 1):
        lines.append(
            f"| {idx}위 | **{s_name}** | {cnt:,}건 | "
            f"**{w1}%** | +{r1}% | "
            f"**{w2}%** | +{r2}% | "
            f"**{w3}%** | +{r3}% |"
        )

    lines.extend([
        "\n---",
        "## 🏅 2. 최근 깃허브 추천 TOP 5 종목의 실제 채점 결과 (최근 3거래일 검증 완료분)",
        "| 기준일(D0) | 추천순위 | 종목명 (티커) | 대금순위 | 보유옵션 | 1차 종베(D0➔D1) | **2차 종베(D1➔D2)** | 3차 종베(D2➔D3) |",
        "| :---: | :---: | :--- | :---: | :---: | :---: | :---: | :---: |"
    ])
    recent_3days = unique_dates[-3:]
    recent_t5 = top5_df[top5_df["Base_Date"].isin(recent_3days)].sort_values(by=["Base_Date", "Pick_Rank"], ascending=[False, True])
    for _, r in recent_t5.iterrows():
        m1 = f"⭕ +{r['Bet1_Max_Ret']}%" if r["Bet1_Win_3Pct"] == 1 else f"❌ {r['Bet1_Max_Ret']}%"
        m2 = f"⭕ **+{r['Bet2_Max_Ret']}%**" if r["Bet2_Win_3Pct"] == 1 else f"❌ {r['Bet2_Max_Ret']}%"
        m3 = f"⭕ +{r['Bet3_Max_Ret']}%" if r["Bet3_Win_3Pct"] == 1 else f"❌ {r['Bet3_Max_Ret']}%"
        lines.append(f"| {r['Base_Date']} | **TOP {r['Pick_Rank']}** | **{r['Name']}** ({r['Ticker']}) | {r['Rank']}위 | {r['Sig_Count']}개 | {m1} | {m2} | {m3} |")

    lines.append(f"\n---\n## 🚀 3. 다음 거래일 오전(10시 전) +3% 매도 목표 — 금일({day_0_latest}) 실전 TOP 5 추천 리스트\n")

    def build_candidate_table(target_d0, d_m1, d_m2):
        rows = []
        pop_set = popular_now.union(top_vol_top10_by_day.get(target_d0, set())) if target_d0 == day_0_latest else top_vol_top10_by_day.get(target_d0, set())
        for vol_rank, code in top40_by_day.get(target_d0, []):
            c0, fluc0, _, sig_cnt, traits_str = evaluate_signals(code, target_d0, d_m1, d_m2, chart_cache, inv_cache, pop_set)
            today_row = chart_cache[code].iloc[-1]
            rows.append({
                "Rank": vol_rank,
                "Ticker": code,
                "Name": universe.get(code, code),
                "Base_Fluc": round(fluc0, 2),
                "Today_Fluc": round(float(today_row["Fluc"]), 2),
                "Sig_Count": sig_cnt,
                "Traits": traits_str
            })
        df_cand = pd.DataFrame(rows)
        if not df_cand.empty:
            df_cand.sort_values(by=["Sig_Count", "Base_Fluc", "Rank"], ascending=[False, False, True], inplace=True)
            df_cand["Pick_Rank"] = range(1, len(df_cand) + 1)
        return df_cand

    d_m3_latest = all_days[-4]
    d_m4_latest = all_days[-5]

    df_b1 = build_candidate_table(day_0_latest, day_m1_latest, day_m2_latest)
    if not df_b1.empty:
        lines.append(f"### 🎯 [1차 종가베팅 TOP 5] 당일({day_0_latest}) 신규 주도주 (오늘 종가 매수 ➔ 내일 10시 전 매도)")
        lines.append("| 픽 순위 | 실제대금순위 | 종목명 (티커) | 당일 등락률 | 보유 옵션 수 | 포착된 핵심 옵션 상세 |")
        lines.append("| :---: | :---: | :--- | :---: | :---: | :--- |")
        for _, r in df_b1.head(5).iterrows():
            lines.append(f"| ⭐ **TOP {r['Pick_Rank']}** | {r['Rank']}위 | **{r['Name']}** ({r['Ticker']}) | +{r['Base_Fluc']}% | **{r['Sig_Count']}개** | {r['Traits']} |")

    df_b2 = build_candidate_table(day_m1_latest, day_m2_latest, d_m3_latest)
    if not df_b2.empty:
        lines.append(f"\n### 🔄 [2차 종가베팅 TOP 5] 전일({day_m1_latest}) 주도주의 D+1일 차 (오늘 종가 재진입 ➔ 내일 10시 전 매도)")
        lines.append("| 픽 순위 | 전일대금순위 | 종목명 (티커) | 전일(D0) 등락 | **금일(D+1) 등락** | D0 옵션 수 | D0 핵심 옵션 |")
        lines.append("| :---: | :---: | :--- | :---: | :---: | :---: | :--- |")
        for _, r in df_b2.head(5).iterrows():
            t_fl = f"+{r['Today_Fluc']}%" if r["Today_Fluc"] > 0 else f"{r['Today_Fluc']}%"
            lines.append(f"| 🔥 **TOP {r['Pick_Rank']}** | {r['Rank']}위 | **{r['Name']}** ({r['Ticker']}) | +{r['Base_Fluc']}% | **{t_fl}** | **{r['Sig_Count']}개** | {r['Traits']} |")

    df_b3 = build_candidate_table(day_m2_latest, d_m3_latest, d_m4_latest)
    if not df_b3.empty:
        lines.append(f"\n### ⚡ [3차 종가베팅 TOP 5] 2일 전({day_m2_latest}) 주도주의 D+2일 차 (오늘 종가 재진입 ➔ 내일 10시 전 매도)")
        lines.append("| 픽 순위 | D0대금순위 | 종목명 (티커) | 2일전(D0) 등락 | **금일(D+2) 등락** | D0 옵션 수 | D0 핵심 옵션 |")
        lines.append("| :---: | :---: | :--- | :---: | :---: | :---: | :--- |")
        for _, r in df_b3.head(5).iterrows():
            t_fl = f"+{r['Today_Fluc']}%" if r["Today_Fluc"] > 0 else f"{r['Today_Fluc']}%"
            lines.append(f"| ⭐ **TOP {r['Pick_Rank']}** | {r['Rank']}위 | **{r['Name']}** ({r['Ticker']}) | +{r['Base_Fluc']}% | **{t_fl}** | **{r['Sig_Count']}개** | {r['Traits']} |")

    # [신규] 코스피 인버스 1년 역추적 및 금일 타점 판독 섹션 병합
    inv_report_lines = run_inverse_backtest_and_report(all_days)
    if inv_report_lines:
        lines.extend(inv_report_lines)

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("미래 확장형 롤링 윈도우 & TOP 5 + 코스피 인버스 통합 리포트 생성이 완료되었습니다!")

if __name__ == "__main__":
    main()
