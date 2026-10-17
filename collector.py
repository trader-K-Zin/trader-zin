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
    """코스피/코스닥 시가총액 및 거래 활발 상위 200여 개 순수 개별주(ETF 제외) 확보"""
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
    # 1차: 모바일 JSON API 시도
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

    # 2차: 만약 모바일 API에서 빈 값이 왔다면 웹 테이블에서 직접 추출 (Fallback)
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

def main():
    print("Step 1: 종목 유니버스 및 영업일 로드 중...")
    universe = get_major_universe()
    if not universe:
        print("유니버스 로드 실패")
        return

    df_ref = get_naver_chart_df("005930", count=75)
    if df_ref.empty:
        print("기준 영업일 로드 실패")
        return
    trading_days = df_ref["Date"].tolist()[-55:]

    print("Step 2: 개별 종목 일봉 차트 캐싱 중...")
    chart_cache = {}
    for code in universe.keys():
        df_c = get_naver_chart_df(code, count=90)
        if not df_c.empty and len(df_c) >= 25:
            chart_cache[code] = df_c

    # D+1과 D+2를 모두 평가하기 위해 최근 22거래일 역추적 (최신 2일은 D+1, D+2 검증용 여유분)
    eval_days = trading_days[-24:-2]
    latest_day = trading_days[-1]

    # 날짜별 [등락률 +1% 이상 & 양봉(종가>시가)] 조건을 만족하는 거래대금 상위 40위 선정
    top40_by_day = {}
    top_vol_top10_by_day = {}
    tracked_tickers = set()

    for d_str in eval_days + [latest_day]:
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
                # 핵심 기준: +1% 이상 오른 양봉 종목만 모수로 채택
                if is_bull and fluc >= 1.0:
                    bullish_pool.append((code, val))

        bullish_pool.sort(key=lambda x: x[1], reverse=True)
        all_vol_pool.sort(key=lambda x: x[1], reverse=True)

        selected_40 = [x[0] for x in bullish_pool[:40]]
        top40_by_day[d_str] = selected_40
        top_vol_top10_by_day[d_str] = set([x[0] for x in all_vol_pool[:10]])
        tracked_tickers.update(selected_40)

    print(f"Step 3: 엄선된 양봉 주도주 {len(tracked_tickers)}개의 수급 데이터 수집 중...")
    inv_cache = {}
    for code in tracked_tickers:
        inv_cache[code] = get_investor_data(code)
        time.sleep(0.02)

    popular_now = get_popular_top10()

    print("Step 4: 12대 시그널 판별 및 D+1 / D+2 단기스윙 성과 채점 중...")
    records = []

    for d0_date in eval_days:
        idx = trading_days.index(d0_date)
        d_minus1 = trading_days[idx - 1]
        d_minus2 = trading_days[idx - 2]
        d_plus1 = trading_days[idx + 1]
        d_plus2 = trading_days[idx + 2]

        pop_ proxies = top_vol_top10_by_day.get(d0_date, set())

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

            # 12대 옵션 시그널 산출
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

            # D+1 및 D+2 주가 추적
            sub_p1 = df_c[df_c["Date"] == d_plus1]
            sub_p2 = df_c[df_c["Date"] == d_plus2]
            if sub_p1.empty or sub_p2.empty:
                continue

            h_p1 = float(sub_p1.iloc[0]["High"])
            c_p1 = float(sub_p1.iloc[0]["Close"])
            h_p2 = float(sub_p2.iloc[0]["High"])
            c_p2 = float(sub_p2.iloc[0]["Close"])

            # 1) D+1 수익률 및 승률
            d1_max_ret = round(((h_p1 - c0) / c0) * 100, 2)
            d1_close_ret = round(((c_p1 - c0) / c0) * 100, 2)
            d1_win_3pct = 1 if d1_max_ret >= 3.0 else 0

            # 2) D+2 단기스윙 수익률 및 승률 (D+1 ~ D+2 이틀 보유 시 최고가 및 종가)
            max_h_2days = max(h_p1, h_p2)
            d2_swing_max_ret = round(((max_h_2days - c0) / c0) * 100, 2)
            d2_only_max_ret = round(((h_p2 - c0) / c0) * 100, 2)
            d2_close_ret = round(((c_p2 - c0) / c0) * 100, 2)

            d2_win_3pct = 1 if d2_swing_max_ret >= 3.0 else 0
            d2_win_5pct = 1 if d2_swing_max_ret >= 5.0 else 0

            records.append({
                "Base_Date": d0_date,
                "D1_Date": d_plus1,
                "D2_Date": d_plus2,
                "Rank": rank,
                "Ticker": code,
                "Name": name,
                "Base_Close": int(c0),
                "Base_Fluc": round(fluc0, 2),
                "D1_Max_Ret": d1_max_ret,
                "D1_Close_Ret": d1_close_ret,
                "D1_Win_3Pct": d1_win_3pct,
                "D2_Swing_Max_Ret": d2_swing_max_ret,
                "D2_Only_Max_Ret": d2_only_max_ret,
                "D2_Close_Ret": d2_close_ret,
                "D2_Win_3Pct": d2_win_3pct,
                "D2_Win_5Pct": d2_win_5pct,
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

    db_df = pd.DataFrame(records)
    if db_df.empty:
        print("생성된 레코드가 없습니다.")
        return
    db_df.to_csv(DB_FILE, index=False, encoding="utf-8-sig")

    signal_cols = [c for c in db_df.columns if c.startswith("S")]
    total_cnt = len(db_df)
    base_d1_win = round(db_df["D1_Win_3Pct"].mean() * 100, 1)
    base_d1_ret = round(db_df["D1_Max_Ret"].mean(), 2)
    base_d2_win3 = round(db_df["D2_Win_3Pct"].mean() * 100, 1)
    base_d2_win5 = round(db_df["D2_Win_5Pct"].mean() * 100, 1)
    base_d2_ret = round(db_df["D2_Swing_Max_Ret"].mean(), 2)

    lines = [
        "# 🎯 트레이더 진 주도주(+1% 양봉) 단타(D+1) & 단기스윙(D+2) 승률 DB 성적표",
        f"- **최종 업데이트:** {now.strftime('%Y-%m-%d %H:%M')} (KST)",
        f"- **모수(Base) 기준:** 거래대금 상위권 중 **등락률 +1.0% 이상 양봉 마감** 순수 개별주 (ETF 제외)",
        f"- **누적 검증 표본:** 총 **{total_cnt:,}개** 양봉 주도주\n",
        "## 📌 0. 아무 옵션 없이 '+1% 이상 양봉 주도주' 매수 시 기본(Base) 확률",
        "| 전략 구분 | +3% 슈팅 승률 | +5% 슈팅 승률 | 평균 최고상승률 | 전략 코멘트 |",
        "| :--- | :---: | :---: | :---: | :--- |",
        f"| **D+1 익일 단타 관점** | **{base_d1_win}%** | - | +{base_d1_ret}% | 매수 다음 날 하루 동안 +3% 도달 확률 |",
        f"| **D+2 단기스윙 관점 (2일 보유)** | **{base_d2_win3}%** | **{base_d2_win5}%** | +{base_d2_ret}% | 별도 조건 없이 D+2까지 홀딩 시 도달 확률 |\n",
        "---",
        "## 📊 1. 12대 옵션 추가 대입 시 [D+1 단타] vs [D+2 단기스윙] 승률 비교 랭킹",
        "| 순위 | 추가 대입 옵션 (시그널) | 표본 수 | **D+1 승률(+3%)** | D+1 평균최고 | **D+2 스윙승률(+3%)** | **D+2 스윙승률(+5%)** | D+2 스윙최고 |",
        "| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: |"
    ]

    stats = []
    for col in signal_cols:
        sub = db_df[db_df[col] == 1]
        cnt = len(sub)
        if cnt > 0:
            w_d1 = round(sub["D1_Win_3Pct"].mean() * 100, 1)
            r_d1 = round(sub["D1_Max_Ret"].mean(), 2)
            w_d2_3 = round(sub["D2_Win_3Pct"].mean() * 100, 1)
            w_d2_5 = round(sub["D2_Win_5Pct"].mean() * 100, 1)
            r_d2 = round(sub["D2_Swing_Max_Ret"].mean(), 2)
        else:
            w_d1, r_d1, w_d2_3, w_d2_5, r_d2 = 0.0, 0.0, 0.0, 0.0, 0.0
        s_name = col.split("_", 1)[1]
        stats.append((s_name, cnt, w_d1, r_d1, w_d2_3, w_d2_5, r_d2))

    # D+1 승률 기준 내림차순 정렬 (동률 시 D+2 승률 우선)
    stats.sort(key=lambda x: (x[2], x[4]), reverse=True)
    for idx, (s_name, cnt, w_d1, r_d1, w_d2_3, w_d2_5, r_d2) in enumerate(stats, 1):
        row_str = (
            f"| {idx}위 | **{s_name}** | {cnt:,}건 | "
            f"**{w_d1}%** | +{r_d1}% | "
            f"**{w_d2_3}%** | {w_d2_5}% | +{r_d2}% |"
        )
        lines.append(row_str)

    # 최근 마감일 기준 -> 다음 거래일(D+1 & D+2) 공략 후보 리스트
    next_rows = []
    d_m1 = trading_days[-2]
    d_m2 = trading_days[-3]
    for rank, code in enumerate(top40_by_day.get(latest_day, []), 1):
        df_c = chart_cache[code]
        cur = df_c.iloc[-1]
        c0, h0, fluc0, val0 = float(cur["Close"]), float(cur["High"]), float(cur["Fluc"]), float(cur["Val_Approx"])
        avg_val_20d = float(df_c["Val_Approx"].iloc[-21:-1].mean()) if len(df_c) > 20 else val0
        high_60d = float(df_c["High"].iloc[-60:].max())

        inv_data = inv_cache.get(code, {})
        inst_0, for_0 = inv_data.get(latest_day, (0.0, 0.0))
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

        sig_cnt = len(traits)
        next_rows.append({
            "Rank": rank,
            "Name": universe.get(code, code),
            "Ticker": code,
            "Fluc": round(fluc0, 2),
            "Sig_Count": sig_cnt,
            "Traits": ", ".join(traits) if traits else "양봉 주도주"
        })

    next_df = pd.DataFrame(next_rows)
    if not next_df.empty:
        lines.append(f"\n---\n## 🚀 2. 다음 거래일 D+1 단타 & D+2 스윙 실전 타점 후보 ({latest_day} 마감 기준)\n")
        lines.append("| 양봉대금순위 | 종목명 (티커) | 당일 등락률 | 보유 옵션 수 | 포착된 핵심 옵션 상세 |")
        lines.append("| :---: | :--- | :---: | :---: | :--- |")
        for _, r in next_df.sort_values(by=["Sig_Count", "Fluc"], ascending=[False, False]).head(15).iterrows():
            rk = r["Rank"]
            nm = r["Name"]
            tk = r["Ticker"]
            fl = r["Fluc"]
            sc = r["Sig_Count"]
            tr = r["Traits"]
            lines.append(f"| {rk}위 | **{nm}** ({tk}) | +{fl}% | **{sc}개** | {tr} |")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("리포트 및 DB 생성이 오류 없이 완벽히 완료되었습니다!")

if __name__ == "__main__":
    main()
