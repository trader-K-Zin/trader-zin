import os
import datetime
import time
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import pandas as pd

now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
DB_FILE = "history_db.csv"
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

def get_naver_chart_df(ticker, count=95):
    """네이버 공식 XML 차트 API로 과거 일봉 로드"""
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
            df["Val_Approx"] = df["Close"] * df["Volume"]
        return df
    except Exception:
        return pd.DataFrame()

def get_investor_data(ticker):
    """외국인/기관 수급 데이터 수집 (1차 모바일 API + 2차 웹 테이블 이중 안전장치)"""
    inv_map = {}
    for page in [1, 2, 3]:
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
        for page in [1, 2]:
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

def extract_stock_traits(code, rank, target_day, d_m1, d_m2, chart_cache, inv_cache, popular_now):
    """특정 기준일(D0)에 포착된 주도주의 시그널 및 이후 주가 흐름 요약"""
    df_c = chart_cache[code]
    df_until = df_c[df_c["Date"] <= target_day]
    cur = df_until.iloc[-1]
    c0 = float(cur["Close"])
    h0 = float(cur["High"])
    fluc0 = float(cur["Fluc"])
    val0 = float(cur["Val_Approx"])
    avg_val_20d = float(df_until["Val_Approx"].iloc[-21:-1].mean()) if len(df_until) > 20 else val0
    high_60d = float(df_until["High"].iloc[-60:].max())

    inv_data = inv_cache.get(code, {})
    inst_0, for_0 = inv_data.get(target_day, (0.0, 0.0))
    inst_m1, for_m1 = inv_data.get(d_m1, (0.0, 0.0))
    inst_m2, for_m2 = inv_data.get(d_m2, (0.0, 0.0))

    s1_pop = 1 if (code in popular_now or rank <= 10) else 0
    s2_for = 1 if for_0 > 0 else 0
    s3_for3 = 1 if (for_0 > 0 and for_m1 > 0 and for_m2 > 0) else 0
    s4_inst = 1 if inst_0 > 0 else 0
    s5_inst3 = 1 if (inst_0 > 0 and inst_m1 > 0 and inst_m2 > 0) else 0
    s6_high = 1 if c0 >= high_60d * 0.95 else 0
    s7_vol = 1 if (avg_val_20d > 0 and val0 >= avg_val_20d * 2.5) else 0
    s8_twin = 1 if (s2_for == 1 and s4_inst == 1) else 0
    s10_candle = 1 if (fluc0 >= 3.0 and h0 > 0 and c0 >= h0 * 0.98) else 0

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
    if s6_high:
        traits.append("신고가가시권")
    if s7_vol:
        traits.append("대금폭발")
    if s10_candle:
        traits.append("꽉찬양봉")

    today_row = df_c.iloc[-1]
    today_fluc = round(float(today_row["Fluc"]), 2)
    today_close = int(float(today_row["Close"]))

    return {
        "Rank": rank,
        "Ticker": code,
        "Base_Fluc": round(fluc0, 2),
        "Today_Fluc": today_fluc,
        "Today_Close": today_close,
        "Sig_Count": len(traits),
        "Traits": ", ".join(traits) if traits else "양봉 주도주"
    }

def main():
    print("Step 1: 유니버스 및 영업일 로드 중...")
    universe = get_major_universe()
    if not universe:
        return

    df_ref = get_naver_chart_df("005930", count=80)
    if df_ref.empty:
        return
    trading_days = df_ref["Date"].tolist()[-60:]

    # 기존 DB 확인 (새 구조인 Bet3_Win_3Pct 컬럼이 없으면 자동으로 새 구조로 초기화)
    if os.path.exists(DB_FILE):
        try:
            db_df = pd.read_csv(DB_FILE, dtype={"Ticker": str, "Base_Date": str})
            if "Bet3_Win_3Pct" not in db_df.columns:
                print("신규 릴레이 종가베팅(1·2·3차) 구조 적용을 위해 DB를 재구축합니다.")
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

    print("Step 2: 개별 종목 일봉 차트 캐싱 중...")
    chart_cache = {}
    for code in universe.keys():
        df_c = get_naver_chart_df(code, count=90)
        if not df_c.empty and len(df_c) >= 30:
            chart_cache[code] = df_c

    # D0 기준 D+1, D+2, D+3까지 총 3번의 종가베팅 결과를 모두 채점하려면 최소 3거래일 여유 필요
    possible_eval_days = trading_days[-25:-3]
    eval_days = [d for d in possible_eval_days if d not in existing_dates]

    day_0_latest = trading_days[-1]
    day_m1_latest = trading_days[-2]
    day_m2_latest = trading_days[-3]

    top40_by_day = {}
    top_vol_top10_by_day = {}
    tracked_tickers = set()

    days_to_scan = list(set(eval_days + [day_0_latest, day_m1_latest, day_m2_latest]))
    for d_str in days_to_scan:
        bullish_pool = []
        all_vol_pool = []
        for code, df_c in chart_cache.items():
            sub = df_c[df_c["Date"] == d_str]
            if not sub.empty:
                r = sub.iloc[0]
                val = float(r["Val_Approx"])
                fluc = float(r["Fluc"])
                is_bull = bool(r["Is_Bullish"])
                all_vol_pool.append((code, val * abs(fluc)))
                if is_bull and fluc >= 1.0:
                    bullish_pool.append((code, val))

        bullish_pool.sort(key=lambda x: x[1], reverse=True)
        all_vol_pool.sort(key=lambda x: x[1], reverse=True)

        selected_40 = [x[0] for x in bullish_pool[:40]]
        top40_by_day[d_str] = selected_40
        top_vol_top10_by_day[d_str] = set([x[0] for x in all_vol_pool[:10]])
        tracked_tickers.update(selected_40)

    print(f"Step 3: 양봉 주도주 {len(tracked_tickers)}개의 수급 데이터 수집 중...")
    inv_cache = {}
    for code in tracked_tickers:
        inv_cache[code] = get_investor_data(code)
        time.sleep(0.02)

    popular_now = get_popular_top10()

    new_records = []
    if eval_days:
        print(f"신규 평가 대상일({len(eval_days)}일치) 1·2·3차 종가베팅 채점 시작...")
        for d0_date in eval_days:
            idx = trading_days.index(d0_date)
            d_minus1 = trading_days[idx - 1]
            d_minus2 = trading_days[idx - 2]
            d_plus1 = trading_days[idx + 1]
            d_plus2 = trading_days[idx + 2]
            d_plus3 = trading_days[idx + 3]
            pop_proxies = top_vol_top10_by_day.get(d0_date, set())

            for rank, code in enumerate(top40_by_day.get(d0_date, []), 1):
                name = universe.get(code, code)
                df_c = chart_cache[code]
                df_until = df_c[df_c["Date"] <= d0_date]
                if len(df_until) < 15:
                    continue

                cur = df_until.iloc[-1]
                c0 = float(cur["Close"])
                h0 = float(cur["High"])
                fluc0 = float(cur["Fluc"])
                val0 = float(cur["Val_Approx"])
                if c0 <= 0:
                    continue

                high_60d = float(df_until["High"].iloc[-60:].max())
                avg_val_20d = float(df_until["Val_Approx"].iloc[-21:-1].mean()) if len(df_until) > 20 else val0
                ma5 = float(df_until["Close"].iloc[-5:].mean())
                ma20 = float(df_until["Close"].iloc[-20:].mean())
                ma60 = float(df_until["Close"].iloc[-60:].mean()) if len(df_until) >= 60 else ma20

                inv_data = inv_cache.get(code, {})
                inst_0, for_0 = inv_data.get(d0_date, (0.0, 0.0))
                inst_m1, for_m1 = inv_data.get(d_minus1, (0.0, 0.0))
                inst_m2, for_m2 = inv_data.get(d_minus2, (0.0, 0.0))

                # 12대 옵션 시그널 (D0 기준)
                s1_pop = 1 if (code in popular_now or code in pop_proxies) else 0
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

                # D+1, D+2, D+3 일봉 데이터 확인
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

                # [1차 종가베팅] D0 종가 매수 -> D+1 오전(장중 고가) 매도
                bet1_ret = round(((h_p1 - c0) / c0) * 100, 2)
                bet1_win = 1 if bet1_ret >= 3.0 else 0

                # [2차 종가베팅] D+1 종가 매수(조건 무관) -> D+2 오전(장중 고가) 매도
                bet2_ret = round(((h_p2 - c_p1) / c_p1) * 100, 2)
                bet2_win = 1 if bet2_ret >= 3.0 else 0

                # [3차 종가베팅] D+2 종가 매수(조건 무관) -> D+3 오전(장중 고가) 매도
                bet3_ret = round(((h_p3 - c_p2) / c_p2) * 100, 2)
                bet3_win = 1 if bet3_ret >= 3.0 else 0

                new_records.append({
                    "Base_Date": d0_date,
                    "D1_Date": d_plus1,
                    "D2_Date": d_plus2,
                    "D3_Date": d_plus3,
                    "Rank": rank,
                    "Ticker": code,
                    "Name": name,
                    "D0_Close": int(c0),
                    "D0_Fluc": round(fluc0, 2),
                    "D1_Close": int(c_p1),
                    "D2_Close": int(c_p2),
                    "Bet1_Max_Ret": bet1_ret,
                    "Bet1_Win_3Pct": bet1_win,
                    "Bet2_Max_Ret": bet2_ret,
                    "Bet2_Win_3Pct": bet2_win,
                    "Bet3_Max_Ret": bet3_ret,
                    "Bet3_Win_3Pct": bet3_win,
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
                })

        if new_records:
            new_df = pd.DataFrame(new_records)
            db_df = pd.concat([db_df, new_df], ignore_index=True)
            db_df.drop_duplicates(subset=["Base_Date", "Ticker"], keep="last", inplace=True)
            db_df.to_csv(DB_FILE, index=False, encoding="utf-8-sig")
            print(f"신규 데이터 {len(new_records)}건이 영구 DB에 정상 누적되었습니다.")

    if db_df.empty:
        print("DB에 데이터가 없습니다.")
        return

    signal_cols = [c for c in db_df.columns if c.startswith("S")]
    total_cnt = len(db_df)

    b1_win = round(db_df["Bet1_Win_3Pct"].mean() * 100, 1)
    b1_ret = round(db_df["Bet1_Max_Ret"].mean(), 2)
    b2_win = round(db_df["Bet2_Win_3Pct"].mean() * 100, 1)
    b2_ret = round(db_df["Bet2_Max_Ret"].mean(), 2)
    b3_win = round(db_df["Bet3_Win_3Pct"].mean() * 100, 1)
    b3_ret = round(db_df["Bet3_Max_Ret"].mean(), 2)

    lines = [
        "# 🎯 트레이더 진 주도주 릴레이 종가베팅(1차·2차·3차) 승률 DB 성적표",
        f"- **최종 업데이트:** {now.strftime('%Y-%m-%d %H:%M')} (KST)",
        f"- **모수(Base) 기준:** `D0` 거래대금 상위권 중 **등락률 +1.0% 이상 양봉 마감** 순수 개별주 (ETF 제외)",
        f"- **영구 누적 검증 표본:** 총 **{total_cnt:,}개** 양봉 주도주\n",
        "## 📌 0. 아무 추가 옵션 없이 '+1% 양봉 주도주' 연속 종가베팅 시 기본(Base) 확률",
        "| 종가베팅 차수 | 진입 시점 (종가 매수) | 청산 시점 (오전 10시 전) | **+3% 슈팅 승률** | 평균 최고상승률 | 전략적 의미 |",
        "| :--- | :---: | :---: | :---: | :---: | :--- |",
        f"| **1차 종가베팅 (메인)** | **`D0` 종가** | **`D+1` 오전** | **{b1_win}%** | +{b1_ret}% | 주도주 발생 당일 종가베팅 -> 익일 오전 매도 |",
        f"| **2차 종가베팅 (D+1)** | **`D+1` 종가** | **`D+2` 오전** | **{b2_win}%** | +{b2_ret}% | D+1 양봉/대금 무관 종가 재진입 -> D+2 오전 매도 |",
        f"| **3차 종가베팅 (D+2)** | **`D+2` 종가** | **`D+3` 오전** | **{b3_win}%** | +{b3_ret}% | D+2 양봉/대금 무관 종가 재진입 -> D+3 오전 매도 |\n",
        "---",
        "## 📊 1. D0 시그널 옵션별 [1차 vs 2차 vs 3차 릴레이 종가베팅] +3% 승률 비교",
        "| 순위 | D0 발생 옵션 (시그널) | 표본 수 | **1차 종베 승률(D0➔D1)** | 1차 최고 | **2차 종베 승률(D1➔D2)** | 2차 최고 | **3차 종베 승률(D2➔D3)** | 3차 최고 |",
        "| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |"
    ]

    stats = []
    for col in signal_cols:
        sub = db_df[db_df[col] == 1]
        cnt = len(sub)
        if cnt > 0:
            w1 = round(sub["Bet1_Win_3Pct"].mean() * 100, 1)
            r1 = round(sub["Bet1_Max_Ret"].mean(), 2)
            w2 = round(sub["Bet2_Win_3Pct"].mean() * 100, 1)
            r2 = round(sub["Bet2_Max_Ret"].mean(), 2)
            w3 = round(sub["Bet3_Win_3Pct"].mean() * 100, 1)
            r3 = round(sub["Bet3_Max_Ret"].mean(), 2)
        else:
            w1, r1, w2, r2, w3, r3 = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        s_name = col.split("_", 1)[1]
        stats.append((s_name, cnt, w1, r1, w2, r2, w3, r3))

    stats.sort(key=lambda x: (x[2], x[4], x[6]), reverse=True)
    for idx, (s_name, cnt, w1, r1, w2, r2, w3, r3) in enumerate(stats, 1):
        row_str = (
            f"| {idx}위 | **{s_name}** | {cnt:,}건 | "
            f"**{w1}%** | +{r1}% | "
            f"**{w2}%** | +{r2}% | "
            f"**{w3}%** | +{r3}% |"
        )
        lines.append(row_str)

    # 실전 타점 후보: 1차(오늘 D0 발생), 2차(어제 D-1 발생 -> 오늘 D+1 종베), 3차(그저께 D-2 발생 -> 오늘 D+2 종베)
    lines.append(f"\n---\n## 🚀 2. 다음 거래일 오전(10시 전) +3% 매도 목표 — 금일({day_0_latest}) 종가베팅 실전 리스트\n")

    # (A) 1차 종가베팅 후보 (오늘 D0 발생 주도주)
    rows_bet1 = []
    for rank, code in enumerate(top40_by_day.get(day_0_latest, []), 1):
        info = extract_stock_traits(code, rank, day_0_latest, day_m1_latest, day_m2_latest, chart_cache, inv_cache, popular_now)
        info["Name"] = universe.get(code, code)
        rows_bet1.append(info)

    df_b1 = pd.DataFrame(rows_bet1)
    if not df_b1.empty:
        lines.append(f"### 🎯 [1차 종가베팅 타점] 당일({day_0_latest}) 신규 발생 주도주 (오늘 종가 매수 ➔ 내일 10시 전 매도)")
        lines.append("| 대금순위 | 종목명 (티커) | 당일 등락률 | 보유 옵션 수 | 포착된 핵심 옵션 상세 |")
        lines.append("| :---: | :--- | :---: | :---: | :--- |")
        for _, r in df_b1.sort_values(by=["Sig_Count", "Base_Fluc"], ascending=[False, False]).head(10).iterrows():
            lines.append(f"| {r['Rank']}위 | **{r['Name']}** ({r['Ticker']}) | +{r['Base_Fluc']}% | **{r['Sig_Count']}개** | {r['Traits']} |")

    # (B) 2차 종가베팅 후보 (어제 D-1 발생 주도주 -> 오늘 D+1 종가 재진입)
    rows_bet2 = []
    d_m3_latest = trading_days[-4]
    for rank, code in enumerate(top40_by_day.get(day_m1_latest, []), 1):
        info = extract_stock_traits(code, rank, day_m1_latest, day_m2_latest, d_m3_latest, chart_cache, inv_cache, popular_now)
        info["Name"] = universe.get(code, code)
        rows_bet2.append(info)

    df_b2 = pd.DataFrame(rows_bet2)
    if not df_b2.empty:
        lines.append(f"\n### 🔄 [2차 종가베팅 타점] 전일({day_m1_latest}) 주도주의 D+1일 차 (오늘 종가 재진입 ➔ 내일 10시 전 매도)")
        lines.append("| 전일순위 | 종목명 (티커) | 전일(D0) 등락 | **금일(D+1) 등락** | D0 당시 옵션 수 | D0 당시 핵심 옵션 |")
        lines.append("| :---: | :--- | :---: | :---: | :---: | :--- |")
        for _, r in df_b2.sort_values(by=["Sig_Count", "Base_Fluc"], ascending=[False, False]).head(8).iterrows():
            t_fl = f"+{r['Today_Fluc']}%" if r["Today_Fluc"] > 0 else f"{r['Today_Fluc']}%"
            lines.append(f"| {r['Rank']}위 | **{r['Name']}** ({r['Ticker']}) | +{r['Base_Fluc']}% | **{t_fl}** | **{r['Sig_Count']}개** | {r['Traits']} |")

    # (C) 3차 종가베팅 후보 (그저께 D-2 발생 주도주 -> 오늘 D+2 종가 재진입)
    rows_bet3 = []
    d_m4_latest = trading_days[-5]
    for rank, code in enumerate(top40_by_day.get(day_m2_latest, []), 1):
        info = extract_stock_traits(code, rank, day_m2_latest, d_m3_latest, d_m4_latest, chart_cache, inv_cache, popular_now)
        info["Name"] = universe.get(code, code)
        rows_bet3.append(info)

    df_b3 = pd.DataFrame(rows_bet3)
    if not df_b3.empty:
        lines.append(f"\n### ⚡ [3차 종가베팅 타점] 2일 전({day_m2_latest}) 주도주의 D+2일 차 (오늘 종가 재진입 ➔ 내일 10시 전 매도)")
        lines.append("| D0순위 | 종목명 (티커) | 2일전(D0) 등락 | **금일(D+2) 등락** | D0 당시 옵션 수 | D0 당시 핵심 옵션 |")
        lines.append("| :---: | :--- | :---: | :---: | :---: | :--- |")
        for _, r in df_b3.sort_values(by=["Sig_Count", "Base_Fluc"], ascending=[False, False]).head(8).iterrows():
            t_fl = f"+{r['Today_Fluc']}%" if r["Today_Fluc"] > 0 else f"{r['Today_Fluc']}%"
            lines.append(f"| {r['Rank']}위 | **{r['Name']}** ({r['Ticker']}) | +{r['Base_Fluc']}% | **{t_fl}** | **{r['Sig_Count']}개** | {r['Traits']} |")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("1·2·3차 릴레이 종가베팅 DB 및 리포트 생성이 완벽히 완료되었습니다!")

if __name__ == "__main__":
    main()
