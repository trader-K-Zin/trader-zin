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
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

def get_naver_chart_df(ticker, count=90):
    """네이버 금융 차트 API로 특정 종목(또는 지수)의 과거 일봉 데이터를 초고속으로 가져옵니다 (차단 없음)"""
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={ticker}&timeframe=day&count={count}&requestType=0"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        root = ET.fromstring(res.text)
        rows = []
        for item in root.findall('.//item'):
            data = item.attrib['data'].split('|')
            if len(data) >= 6:
                dt, o, h, l, c, v = data[0], float(data[1]), float(data[2]), float(data[3]), float(data[4]), float(data[5])
                rows.append({"Date": dt, "Open": o, "High": h, "Low": l, "Close": c, "Volume": v})
        df = pd.DataFrame(rows)
        if not df.empty:
            df["Prev_Close"] = df["Close"].shift(1)
            df["Fluc"] = ((df["Close"] - df["Prev_Close"]) / df["Prev_Close"] * 100).fillna(0.0)
            df["Val_Approx"] = df["Close"] * df["Volume"]
        return df
    except Exception:
        return pd.DataFrame()

def get_trading_days(n=25):
    """삼성전자(005930) 일봉 날짜를 기준으로 최근 정확한 영업일(개장일) 리스트 추출"""
    df = get_naver_chart_df("005930", count=n+10)
    if df.empty:
        return []
    return df["Date"].tolist()[-n:]

def get_popular_tickers():
    """네이버 금융 실시간 검색 상위 종목 추출"""
    tickers = set()
    try:
        url = "https://finance.naver.com/sise/lastsearch2.naver"
        res = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(res.content.decode('euc-kr', 'replace'), 'html.parser')
        for a in soup.select('a.tltle'):
            href = a.get('href', '')
            if 'code=' in href:
                tickers.add(href.split('code=')[-1])
    except Exception:
        pass
    return tickers

def get_candidate_universe():
    """네이버 금융 코스피(0) + 코스닥(1) 거래대금/거래량 상위 메이저 주도주 풀 추출"""
    candidates = {}
    for sosok in [0, 1]:
        try:
            # 네이버 시가총액 및 거래 활발 상위 페이지 스캔
            for page in [1, 2]:
                url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}"
                res = requests.get(url, headers=HEADERS, timeout=10)
                soup = BeautifulSoup(res.content.decode('euc-kr', 'replace'), 'html.parser')
                for a in soup.select('a.tltle'):
                    href = a.get('href', '')
                    if 'code=' in href:
                        code = href.split('code=')[-1]
                        candidates[code] = a.text.strip()
            # 당일 거래량 상위 스캔 추가
            url_quant = f"https://finance.naver.com/sise/sise_quant.naver?sosok={sosok}"
            res_q = requests.get(url_quant, headers=HEADERS, timeout=10)
            soup_q = BeautifulSoup(res_q.content.decode('euc-kr', 'replace'), 'html.parser')
            for a in soup_q.select('a.tltle'):
                href = a.get('href', '')
                if 'code=' in href:
                    code = href.split('code=')[-1]
                    candidates[code] = a.text.strip()
        except Exception:
            continue
    return candidates

def get_investor_history(ticker):
    """네이버 금융 종목별 투자자별 매매동향(외국인/기관 순매수) 최근 30일치 추출"""
    inv_map = {}
    try:
        for page in [1, 2]:
            url = f"https://finance.naver.com/item/frgn.naver?code={ticker}&page={page}"
            res = requests.get(url, headers=HEADERS, timeout=10)
            soup = BeautifulSoup(res.content.decode('euc-kr', 'replace'), 'html.parser')
            for tr in soup.select('table.type2 tr'):
                tds = tr.find_all('td')
                if len(tds) >= 7:
                    dt_txt = tds[0].text.strip().replace('.', '')
                    if len(dt_txt) == 8 and dt_txt.isdigit():
                        try:
                            inst_net = float(tds[5].text.strip().replace(',', '').replace('+', ''))
                            for_net = float(tds[6].text.strip().replace(',', '').replace('+', ''))
                            inv_map[dt_txt] = (inst_net, for_net)
                        except Exception:
                            pass
    except Exception:
        pass
    return inv_map

def main():
    print("1. 네이버 금융에서 영업일 및 주도주 유니버스 수집 중...")
    trading_days = get_trading_days(20)
    if len(trading_days) < 5:
        print("영업일 데이터를 가져오지 못했습니다.")
        return

    popular_set = get_popular_tickers()
    universe = get_candidate_universe()
    print(f"총 {len(universe)}개 핵심 후보 종목 차트 및 수급 로드 시작...")

    # 전 후보 종목의 최근 80일 일봉 로드
    chart_cache = {}
    for code, name in universe.items():
        df_c = get_naver_chart_df(code, count=85)
        if not df_c.empty and len(df_c) >= 25:
            chart_cache[code] = df_c

    # 날짜별 거래대금 상위 40위 선정 후 수급 데이터 캐싱
    target_days = trading_days[-12:] # 최근 12거래일(약 2.5주, 480개 주도주 표본) 정밀 역추적
    top40_by_day = {}
    needed_tickers = set()

    for d_str in target_days:
        day_list = []
        for code, df_c in chart_cache.items():
            sub = df_c[df_c["Date"] == d_str]
            if not sub.empty:
                val = float(sub.iloc[0]["Val_Approx"])
                day_list.append((code, val))
        day_list.sort(key=lambda x: x[1], reverse=True)
        top40 = [x[0] for x in day_list[:40]]
        top40_by_day[d_str] = top40
        needed_tickers.update(top40)

    print(f"상위 40위권 진입 주도주({len(needed_tickers)}개)의 외국인/기관 수급 데이터 수집 중...")
    inv_cache = {}
    for code in needed_tickers:
        inv_cache[code] = get_investor_history(code)
        time.sleep(0.05)

    def build_signals_for_date(d1_date):
        idx = trading_days.index(d1_date)
        d2_date = trading_days[idx - 1]
        d3_date = trading_days[idx - 2]
        codes = top40_by_day.get(d1_date, [])
        rows = []

        for rank, code in enumerate(codes, 1):
            name = universe.get(code, code)
            df_c = chart_cache[code]
            df_until = df_c[df_c["Date"] <= d1_date]
            if len(df_until) < 20:
                continue

            cur = df_until.iloc[-1]
            close_p = float(cur["Close"])
            high_p = float(cur["High"])
            fluc = float(cur["Fluc"])
            val_d1 = float(cur["Val_Approx"])

            high_60d = float(df_until["High"].iloc[-60:].max())
            avg_val_20d = float(df_until["Val_Approx"].iloc[-21:-1].mean()) if len(df_until) > 20 else val_d1
            ma5 = float(df_until["Close"].iloc[-5:].mean())
            ma20 = float(df_until["Close"].iloc[-20:].mean())
            ma60 = float(df_until["Close"].iloc[-60:].mean())

            inv_data = inv_cache.get(code, {})
            inst_d1, for_d1 = inv_data.get(d1_date, (0, 0))
            inst_d2, for_d2 = inv_data.get(d2_date, (0, 0))
            inst_d3, for_d3 = inv_data.get(d3_date, (0, 0))

            s1_pop = 1 if (code in popular_set or rank <= 10) else 0
            s2_for = 1 if for_d1 > 0 else 0
            s3_for3 = 1 if (for_d1 > 0 and for_d2 > 0 and for_d3 > 0) else 0
            s4_inst = 1 if inst_d1 > 0 else 0
            s5_inst3 = 1 if (inst_d1 > 0 and inst_d2 > 0 and inst_d3 > 0) else 0
            s6_high = 1 if close_p >= high_60d * 0.95 else 0
            s7_vol = 1 if (avg_val_20d > 0 and val_d1 >= avg_val_20d * 2.5) else 0
            s8_twin = 1 if (s2_for == 1 and s4_inst == 1) else 0
            s9_major_inst = 1 if (inst_d1 > 0 and val_d1 >= avg_val_20d * 1.5) else 0
            s10_candle = 1 if (fluc >= 3.0 and high_p > 0 and close_p >= high_p * 0.98) else 0
            s11_trend = 1 if (ma5 > ma20 > ma60) else 0
            s12_scombo = 1 if (s8_twin == 1 and s6_high == 1 and (s10_candle == 1 or s11_trend == 1)) else 0

            rows.append({
                "D1_Date": d1_date,
                "Rank": rank,
                "Ticker": code,
                "Name": name,
                "D1_Close": int(close_p),
                "D1_Fluc": round(fluc, 2),
                "D1_Val_100M": round(val_d1 / 100000000, 1),
                "S1_실시간검색상위": s1_pop,
                "S2_외국인순매수": s2_for,
                "S3_외국인3일연속": s3_for3,
                "S4_기관순매수": s4_inst,
                "S5_기관3일연속": s5_inst3,
                "S6_신고가가시권": s6_high,
                "S7_거래대금폭발": s7_vol,
                "S8_외인기관양매수": s8_twin,
                "S9_메이저수급집중": s9_major_inst,
                "S10_꽉찬양봉마감": s10_candle,
                "S11_이평선정배열": s11_trend,
                "S12_S급복합시그널": s12_scombo
            })
        return pd.DataFrame(rows)

    if os.path.exists(DB_FILE):
        try:
            db_df = pd.read_csv(DB_FILE, dtype={"Ticker": str, "D1_Date": str, "D_Day": str})
        except Exception:
            db_df = pd.DataFrame()
    else:
        db_df = pd.DataFrame()

    existing_days = set(db_df["D1_Date"].unique()) if not db_df.empty else set()
    eval_days = [d for d in target_days[:-1] if d not in existing_days]

    new_records = []
    for d1_dt in eval_days:
        idx = trading_days.index(d1_dt)
        d_day = trading_days[idx + 1]
        df_sig = build_signals_for_date(d1_dt)
        if df_sig.empty:
            continue

        df_sig["D_Day"] = d_day
        rets, wins = [], []
        for _, r in df_sig.iterrows():
            tk = r["Ticker"]
            prev_c = float(r["D1_Close"])
            df_c = chart_cache[tk]
            sub_d = df_c[df_c["Date"] == d_day]
            if not sub_d.empty and prev_c > 0:
                high_d = float(sub_d.iloc[0]["High"])
                max_r = round(((high_d - prev_c) / prev_c) * 100, 2)
            else:
                max_r = 0.0
            rets.append(max_r)
            wins.append(1 if max_r >= 3.0 else 0)

        df_sig["Morning_Max_Ret"] = rets
        df_sig["Win_3Pct"] = wins
        new_records.append(df_sig)

    if new_records:
        db_df = pd.concat([db_df] + new_records, ignore_index=True)

    db_df.to_csv(DB_FILE, index=False, encoding="utf-8-sig")

    signal_cols = [c for c in db_df.columns if c.startswith("S")]
    total_samples = len(db_df)
    base_win_rate = round(db_df["Win_3Pct"].mean() * 100, 1) if total_samples > 0 else 0.0

    lines = [
        f"# 🎯 트레이더 진 12대 시그널 주도주 역추적 승률 DB 성적표",
        f"* **최종 업데이트:** {now.strftime('%Y-%m-%d %H:%M')} (KST) | **엔진:** Naver Finance Direct (해외IP 무차단)",
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
        clean_name = col.split("_", 1)[1]
        stats.append((clean_name, cnt, wins, w_rate, avg_ret))

    stats.sort(key=lambda x: x[3], reverse=True)
    for idx, (s_name, cnt, wins, w_rate, avg_ret) in enumerate(stats, 1):
        lines.append(f"| {idx}위 | **{s_name}** | {cnt:,}건 | {wins:,}건 | **{w_rate}%** | +{avg_ret}% |")

    latest_day = trading_days[-1]
    df_next = build_signals_for_date(latest_day)
    if not df_next.empty:
        df_next["Sig_Count"] = df_next[signal_cols].sum(axis=1)
        lines.append(f"\n---\n## 🚀 2. 다음 거래일 오전(08:00~10:00) +3% 노림수 타점 후보 ({latest_day} 마감 기준)\n")
        lines.append("| 전일 대금순위 | 종목명 (티커) | 전일 등락률 | 핵심 시그널 개수 | 보유 시그널 상세 |")
        lines.append("| :---: | :--- | :---: | :---: | :--- |")
        for _, r in df_next.sort_values(by="Sig_Count", ascending=False).head(15).iterrows():
            active_sigs = [c.split("_", 1)[1] for c in signal_cols if r[c] == 1]
            lines.append(f"| {r['Rank']}위 | **{r['Name']}** ({r['Ticker']}) | {r['D1_Fluc']}% | **{int(r['Sig_Count'])}개** | {', '.join(active_sigs)} |")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("리포트 및 DB 생성 100% 완료!")

if __name__ == "__main__":
    main()
