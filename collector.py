import os
import datetime
import time
import requests
import xml.etree.ElementTree as ET
import pandas as pd

now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
DB_FILE = "history_db.csv"
REPORT_FILE = "WIN_RATE_REPORT.md"
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

def get_major_universe():
    tickers = {}
    for market in ["KOSPI", "KOSDAQ"]:
        for page in [1, 2, 3]:
            url = f"https://m.stock.naver.com/api/stocks/marketValue/{market}?page={page}&pageSize=50"
            try:
                res = requests.get(url, headers=HEADERS, timeout=10)
                if res.status_code == 200:
                    for s in res.json().get("stocks", []):
                        code, name = s.get("itemCode"), s.get("stockName")
                        if code and name:
                            tickers[code] = name
            except Exception:
                pass
    return tickers

def get_naver_chart_df(ticker, count=90):
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={ticker}&timeframe=day&count={count}&requestType=0"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        root = ET.fromstring(res.text)
        rows = []
        for item in root.findall('.//item'):
            d = item.attrib['data'].split('|')
            if len(d) >= 6:
                rows.append({"Date": d[0], "Open": float(d[1]), "High": float(d[2]), "Low": float(d[3]), "Close": float(d[4]), "Volume": float(d[5])})
        df = pd.DataFrame(rows)
        if not df.empty:
            df["Prev_Close"] = df["Close"].shift(1)
            df["Fluc"] = ((df["Close"] - df["Prev_Close"]) / df["Prev_Close"] * 100).fillna(0.0)
            df["Val_Approx"] = df["Close"] * df["Volume"]
        return df
    except Exception:
        return pd.DataFrame()

def to_float(val):
    if val is None:
        return 0.0
    s = str(val).replace(",", "").replace("+", "").strip()
    try:
        return float(s)
    except Exception:
        return 0.0

def get_investor_data(ticker):
    inv_map = {}
    for page in [1, 2, 3]:
        url = f"https://m.stock.naver.com/api/stock/{ticker}/trend?page={page}&pageSize=20"
        try:
            res = requests.get(url, headers=HEADERS, timeout=8)
            if res.status_code == 200:
                data = res.json()
                trends = data if isinstance(data, list) else data.get("trends", data.get("item", []))
                for item in trends:
                    if not isinstance(item, dict):
                        continue
                    b_date = str(item.get("bizdate", "")).replace("-", "").replace(".", "").strip()
                    if len(b_date) == 8:
                        f_q = to_float(item.get("foreignerPureBuyQuant", 0))
                        o_q = to_float(item.get("organPureBuyQuant", 0))
                        inv_map[b_date] = (o_q, f_q)
        except Exception:
            pass
    return inv_map

def main():
    universe = get_major_universe()
    if not universe:
        return
    df_ref = get_naver_chart_df("005930", count=70)
    if df_ref.empty:
        return
    trading_days = df_ref["Date"].tolist()[-50:]

    chart_cache = {}
    for code in universe.keys():
        df_c = get_naver_chart_df(code, count=85)
        if not df_c.empty and len(df_c) >= 20:
            chart_cache[code] = df_c

    eval_days = trading_days[-22:-1]
    top40_by_day = {}
    tracked_tickers = set()

    for d_str in eval_days + [trading_days[-1]]:
        day_pool = []
        for code, df_c in chart_cache.items():
            sub = df_c[df_c["Date"] == d_str]
            if not sub.empty:
                day_pool.append((code, float(sub.iloc[0]["Val_Approx"])))
        day_pool.sort(key=lambda x: x[1], reverse=True)
        selected = [x[0] for x in day_pool[:40]]
        top40_by_day[d_str] = selected
        tracked_tickers.update(selected)

    inv_cache = {}
    for code in tracked_tickers:
        inv_cache[code] = get_investor_data(code)
        time.sleep(0.02)

    records = []
    for d1_date in eval_days:
        idx = trading_days.index(d1_date)
        d_day = trading_days[idx + 1]
        d2_date = trading_days[idx - 1]
        d3_date = trading_days[idx - 2]

        for rank, code in enumerate(top40_by_day.get(d1_date, []), 1):
            name = universe.get(code, code)
            df_c = chart_cache[code]
            df_until = df_c[df_c["Date"] <= d1_date]
            if len(df_until) < 15:
                continue

            cur = df_until.iloc[-1]
            close_p, high_p, fluc, val_d1 = float(cur["Close"]), float(cur["High"]), float(cur["Fluc"]), float(cur["Val_Approx"])
            high_60d = float(df_until["High"].iloc[-60:].max())
            avg_val_20d = float(df_until["Val_Approx"].iloc[-21:-1].mean()) if len(df_until) > 20 else val_d1
            ma5 = float(df_until["Close"].iloc[-5:].mean())
            ma20 = float(df_until["Close"].iloc[-20:].mean())
            ma60 = float(df_until["Close"].iloc[-60:].mean()) if len(df_until) >= 60 else ma20

            inv_data = inv_cache.get(code, {})
            inst_d1, for_d1 = inv_data.get(d1_date, (0, 0))
            inst_d2, for_d2 = inv_data.get(d2_date, (0, 0))
            inst_d3, for_d3 = inv_data.get(d3_date, (0, 0))

            s1_pop = 1 if rank <= 10 else 0
            s2_for = 1 if for_d1 > 0 else 0
            s3_for3 = 1 if (for_d1 > 0 and for_d2 > 0 and for_d3 > 0) else 0
            s4_inst = 1 if inst_d1 > 0 else 0
            s5_inst3 = 1 if (inst_d1 > 0 and inst_d2 > 0 and inst_d3 > 0) else 0
            s6_high = 1 if close_p >= high_60d * 0.95 else 0
            s7_vol = 1 if (avg_val_20d > 0 and val_d1 >= avg_val_20d * 2.5) else 0
            s8_twin = 1 if (s2_for == 1 and s4_inst == 1) else 0
            s9_major = 1 if (inst_d1 > 0 and val_d1 >= avg_val_20d * 1.5) else 0
            s10_candle = 1 if (fluc >= 3.0 and high_p > 0 and close_p >= high_p * 0.98) else 0
            s11_trend = 1 if (ma5 > ma20 > ma60) else 0
            s12_scombo = 1 if (s8_twin == 1 and s6_high == 1 and s10_candle == 1) else 0

            sub_next = df_c[df_c["Date"] == d_day]
            ret = round(((float(sub_next.iloc[0]["High"]) - close_p) / close_p) * 100, 2) if (not sub_next.empty and close_p > 0) else 0.0
            win = 1 if ret >= 3.0 else 0

            records.append({
                "D1_Date": d1_date, "D_Day": d_day, "Rank": rank, "Ticker": code, "Name": name,
                "D1_Close": int(close_p), "D1_Fluc": round(fluc, 2), "D1_Val_100M": round(val_d1 / 100000000, 1),
                "Morning_Max_Ret": ret, "Win_3Pct": win,
                "S1_거래대금TOP10": s1_pop, "S2_외국인순매수": s2_for, "S3_외국인3일연속": s3_for3,
                "S4_기관순매수": s4_inst, "S5_기관3일연속": s5_inst3, "S6_신고가가시권": s6_high,
                "S7_거래대금폭발": s7_vol, "S8_외인기관양매수": s8_twin, "S9_메이저수급집중": s9_major,
                "S10_꽉찬양봉마감": s10_candle, "S11_이평선정배열": s11_trend, "S12_S급복합시그널": s12_scombo
            })

    db_df = pd.DataFrame(records)
    if db_df.empty:
        return
    db_df.to_csv(DB_FILE, index=False, encoding="utf-8-sig")

    signal_cols = [c for c in db_df.columns if c.startswith("S")]
    total_samples = len(db_df)
    base_win_rate = round(db_df["Win_3Pct"].mean() * 100, 1) if total_samples > 0 else 0.0

    lines = [
        "# 🎯 트레이더 진 12대 시그널 주도주 역추적 승률 DB 성적표",
        f"* **최종 업데이트:** {now.strftime('%Y-%m-%d %H:%M')} (KST) | **분석 엔진:** Naver Official Mobile API",
        f"* **누적 검증 표본:** 총 **{total_samples:,}개** 주도주 (전일 거래대금 상위 40위)",
        f"* **전체 평균 성공률(Base Rate):** **{base_win_rate}%** (조건 없이 상위 40위 매수 시 익일 +3% 슈팅 도달 확률)\n",
        "---",
        "## 📊 1. 전일 시그널별 익일 +3% 슈팅 승률 랭킹 (Top 12)\n",
        "| 순위 | 전일(D-1) 발생 시그널 조건 | 누적 발생 횟수 | +3% 성공 건수 | **조건별 승률(%)** | 평균 최고상승률(%) |",
        "| :---: | :--- | :---: | :---: | :---: | :---: |"
    ]

    stats = []
    for col in signal_cols:
        sub = db_df[db_df[col] == 1]
        cnt = len(sub)
        wins = int(sub["Win_3Pct"].sum()) if cnt > 0 else 0
        w_rate = round((wins / cnt) * 100, 1) if cnt > 0 else 0.0
        avg_ret = round(sub["Morning_Max_Ret"].mean(), 2) if cnt > 0 else 0.0
        stats.append((col.split("_", 1)[1], cnt, wins, w_rate, avg_ret))

    stats.sort(key=lambda x: x[3], reverse=True)
    for idx, (s_name, cnt, wins, w_rate, avg_ret) in enumerate(stats, 1):
        lines.append(f"| {idx}위 | **{s_name}** | {cnt:,}건 | {wins:,}건 | **{w_rate}%** | +{avg_ret}% |")

    latest_day = trading_days[-1]
    next_rows = []
    for rank, code in enumerate(top40_by_day.get(latest_day, []), 1):
        df_c = chart_cache[code]
        cur = df_c.iloc[-1]
        inst_d1, for_d1 = inv_cache.get(code, {}).get(latest_day, (0, 0))
        s2_for = 1 if for_d1 > 0 else 0
        s4_inst = 1 if inst_d1 > 0 else 0
        s8_twin = 1 if (s2_for == 1 and s4_inst == 1) else 0
        s6_high = 1 if float(cur["Close"]) >= float(df_c["High"].iloc[-60:].max()) * 0.95 else 0
        s10_candle = 1 if (float(cur["Fluc"]) >= 3.0 and float(cur["Close"]) >= float(cur["High"]) * 0.98) else 0
        sig_cnt = s2_for + s4_inst + s8_twin + s6_high + s10_candle
        next_rows.append({"Rank": rank, "Name": universe.get(code, code), "Ticker": code, "Fluc": round(float(cur["Fluc"]), 2), "Sig_Count": sig_cnt, "For": s2_for, "Inst": s4_inst, "Twin": s8_twin, "High": s6_high, "Candle": s10_candle})

    next_df = pd.DataFrame(next_rows)
    if not next_df.empty:
        lines.append(f"\n---\n## 🚀 2. 다음 거래일 오전(08:00~10:00) +3% 노림수 타점 후보 ({latest_day} 마감 기준)\n")
        lines.append("| 전일 대금순위 | 종목명 (티커) | 전일 등락률 | 핵심 시그널 개수 | 핵심 포인트 |")
        lines.append("| :---: | :--- | :---: | :---: | :--- |")
        for _, r in next_df.sort_values(by="Sig_Count", ascending=False).head(15).iterrows():
            traits = []
            if r["Twin"]: traits.append("양매수")
            elif r["For"]: traits.append("외인순매수")
            elif r["Inst"]: traits.append("기관순매수")
            if r["High"]: traits.append("신고가도전")
            if r["Candle"]: traits.append("종가고가마감")
            t_str = ", ".join(traits) if traits else "거래대금 상위"
            r_rk, r_nm, r_tk, r_fl, r_sc = r["Rank"], r["Name"], r["Ticker"], r["Fluc"], r["Sig_Count"]
            lines.append(f"| {r_rk}위 | **{r_nm}** ({r_tk}) | {r_fl}% | **{r_sc}개** | {t_str} |")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

if __name__ == "__main__":
    main()
