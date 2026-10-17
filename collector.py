import os
import datetime
import time
import requests
from bs4 import BeautifulSoup
import pandas as pd
from pykrx import stock

now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
today_str = now.strftime("%Y%m%d")

DB_FILE = "history_db.csv"
REPORT_FILE = "WIN_RATE_REPORT.md"

def get_trading_days(n=50):
    """최근 실제 영업일만 정확히 추출 (주말 및 공휴일 원천 배제)"""
    start_dt = (now - datetime.timedelta(days=100)).strftime("%Y%m%d")
    days = stock.get_previous_business_days(fromdate=start_dt, todate=today_str)
    return [d.strftime("%Y%m%d") for d in days[-n:]]

def get_popular_tickers_now():
    """네이버 금융 실시간 인기 검색 종목"""
    tickers = set()
    try:
        url = "https://finance.naver.com/sise/lastsearch2.naver"
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')
        for a in soup.select('a.tltle'):
            href = a.get('href', '')
            if 'code=' in href:
                tickers.add(href.split('code=')[-1])
    except Exception:
        pass
    return tickers

def evaluate_day_signals(d1_date, trading_days, popular_set=None):
    """D-1 거래대금 상위 40개 주도주 및 12대 시그널 전수 조사 (예외 완벽 방어)"""
    try:
        idx = trading_days.index(d1_date)
    except ValueError:
        return pd.DataFrame()

    d2_date = trading_days[idx - 1]
    d3_date = trading_days[idx - 2]
    start_60d = trading_days[max(0, idx - 60)]

    try:
        df_k1 = stock.get_market_ohlcv_by_ticker(d1_date, market="KOSPI")
        df_k2 = stock.get_market_ohlcv_by_ticker(d1_date, market="KOSDAQ")
        df_all = pd.concat([df_k1, df_k2])
    except Exception:
        return pd.DataFrame()

    if df_all.empty or "거래대금" not in df_all.columns:
        return pd.DataFrame()

    df_all = df_all[df_all["거래량"] > 0]
    top40 = df_all.sort_values(by="거래대금", ascending=False).head(40)

    def fetch_net_map(dt, inv_name):
        try:
            a = stock.get_market_net_purchases_of_equities_by_ticker(dt, dt, "KOSPI", inv_name)
            b = stock.get_market_net_purchases_of_equities_by_ticker(dt, dt, "KOSDAQ", inv_name)
            merged = pd.concat([a, b])
            return merged["순매수거래대금"].to_dict() if not merged.empty else {}
        except Exception:
            return {}

    for_d1 = fetch_net_map(d1_date, "외국인")
    for_d2 = fetch_net_map(d2_date, "외국인")
    for_d3 = fetch_net_map(d3_date, "외국인")
    inst_d1 = fetch_net_map(d1_date, "기관합계")
    inst_d2 = fetch_net_map(d2_date, "기관합계")
    inst_d3 = fetch_net_map(d3_date, "기관합계")
    pension_d1 = fetch_net_map(d1_date, "연기금등")

    rows = []
    for rank, (ticker, row) in enumerate(top40.iterrows(), 1):
        try:
            name = stock.get_market_ticker_name(ticker)
            close_p = float(row["종가"])
            high_p = float(row["고가"])
            fluc = float(row["등락률"])
            val_d1 = float(row["거래대금"])

            df_hist = stock.get_market_ohlcv_by_date(start_60d, d1_date, ticker)
            if df_hist.empty or len(df_hist) < 15:
                continue

            high_60d = float(df_hist["고가"].max())
            avg_val_20d = float(df_hist["거래대금"].iloc[-21:-1].mean()) if len(df_hist) > 20 else val_d1
            ma5 = float(df_hist["종가"].iloc[-5:].mean())
            ma20 = float(df_hist["종가"].iloc[-20:].mean())
            ma60 = float(df_hist["종가"].mean())

            s1_pop = 1 if (popular_set and ticker in popular_set) else (1 if rank <= 10 else 0)
            s2_for = 1 if for_d1.get(ticker, 0) > 0 else 0
            s3_for3 = 1 if (for_d1.get(ticker, 0) > 0 and for_d2.get(ticker, 0) > 0 and for_d3.get(ticker, 0) > 0) else 0
            s4_inst = 1 if inst_d1.get(ticker, 0) > 0 else 0
            s5_inst3 = 1 if (inst_d1.get(ticker, 0) > 0 and inst_d2.get(ticker, 0) > 0 and inst_d3.get(ticker, 0) > 0) else 0
            s6_high = 1 if close_p >= high_60d * 0.95 else 0
            s7_vol = 1 if (avg_val_20d > 0 and val_d1 >= avg_val_20d * 3.0) else 0
            s8_twin = 1 if (s2_for == 1 and s4_inst == 1) else 0
            s9_pension = 1 if pension_d1.get(ticker, 0) > 0 else 0
            s10_candle = 1 if (fluc >= 3.0 and high_p > 0 and close_p >= high_p * 0.98) else 0
            s11_trend = 1 if (ma5 > ma20 > ma60) else 0
            s12_scombo = 1 if (s8_twin == 1 and s6_high == 1 and s10_candle == 1) else 0

            rows.append({
                "D1_Date": d1_date,
                "Rank": rank,
                "Ticker": ticker,
                "Name": name,
                "D1_Close": close_p,
                "D1_Fluc": round(fluc, 2),
                "D1_Val_100M": round(val_d1 / 100000000, 1),
                "S1_실시간검색상위": s1_pop,
                "S2_외국인순매수": s2_for,
                "S3_외국인3일연속": s3_for3,
                "S4_기관순매수": s4_inst,
                "S5_기관3일연속": s5_inst3,
                "S6_신고가가시권": s6_high,
                "S7_거래대금3배폭발": s7_vol,
                "S8_외인기관양매수": s8_twin,
                "S9_연기금순매수": s9_pension,
                "S10_꽉찬양봉마감": s10_candle,
                "S11_이평선정배열": s11_trend,
                "S12_S급복합시그널": s12_scombo
            })
        except Exception:
            continue
    return pd.DataFrame(rows)

def main():
    trading_days = get_trading_days(40)
    popular_set = get_popular_tickers_now()

    if os.path.exists(DB_FILE):
        try:
            db_df = pd.read_csv(DB_FILE, dtype={"Ticker": str, "D1_Date": str, "D_Day": str})
        except Exception:
            db_df = pd.DataFrame()
    else:
        db_df = pd.DataFrame()

    existing_days = set(db_df["D1_Date"].unique()) if not db_df.empty else set()
    backfill_days = [d for d in trading_days[-25:-1] if d not in existing_days]

    if backfill_days:
        print(f"총 {len(backfill_days)}거래일 과거 주도주 일괄 역추적 시작...")
        new_records = []
        for d1_dt in backfill_days:
            idx = trading_days.index(d1_dt)
            d_day = trading_days[idx + 1]
            df_sig = evaluate_day_signals(d1_dt, trading_days, popular_set)
            if df_sig.empty:
                continue

            try:
                k1 = stock.get_market_ohlcv_by_ticker(d_day, market="KOSPI")
                k2 = stock.get_market_ohlcv_by_ticker(d_day, market="KOSDAQ")
                ohlcv_d = pd.concat([k1, k2])
            except Exception:
                continue

            df_sig["D_Day"] = d_day
            rets, wins = [], []
            for _, r in df_sig.iterrows():
                tk = r["Ticker"]
                prev_c = r["D1_Close"]
                if tk in ohlcv_d.index and prev_c > 0:
                    high_d = float(ohlcv_d.loc[tk, "고가"])
                    max_r = round(((high_d - prev_c) / prev_c) * 100, 2)
                else:
                    max_r = 0.0
                rets.append(max_r)
                wins.append(1 if max_r >= 3.0 else 0)

            df_sig["Morning_Max_Ret"] = rets
            df_sig["Win_3Pct"] = wins
            new_records.append(df_sig)
            time.sleep(0.2)

        if new_records:
            db_df = pd.concat([db_df] + new_records, ignore_index=True)

    db_df.to_csv(DB_FILE, index=False, encoding="utf-8-sig")

    signal_cols = [c for c in db_df.columns if c.startswith("S")]
    total_samples = len(db_df)
    base_win_rate = round(db_df["Win_3Pct"].mean() * 100, 1) if total_samples > 0 else 0.0

    lines = [
        f"# 🎯 트레이더 진 12대 시그널 주도주 역추적 승률 DB 성적표",
        f"* **데이터 기준:** 최근 검증 기간 전수 역추적 결과",
        f"* **누적 검증 표본:** 총 **{total_samples:,}개** 주도주 (전일 거래대금 상위 40위)",
        f"* **전체 평균 성공률(Base Rate):** **{base_win_rate}%** (조건 없이 상위 40위 매수 시 익일 +3% 슈팅 도달 확률)\n",
        "---",
        "## 📊 1. 전일 시그널별 익일 오전 +3% 슈팅 승률 랭킹 (Top 12)\n",
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

    # 최근 거래일 마감 기준 -> 다음 거래일 오전 타점 후보
    latest_day = trading_days[-1]
    df_next = evaluate_day_signals(latest_day, trading_days, popular_set)
    if not df_next.empty:
        df_next["Sig_Count"] = df_next[signal_cols].sum(axis=1)
        lines.append(f"\n---\n## 🚀 2. 다음 거래일 오전(08:00~10:00) +3% 노림수 타점 후보 (최근 마감 기준)\n")
        lines.append("| 전일 대금순위 | 종목명 (티커) | 전일 등락률 | 핵심 시그널 개수 | 보유 시그널 상세 |")
        lines.append("| :---: | :--- | :---: | :---: | :--- |")
        for _, r in df_next.sort_values(by="Sig_Count", ascending=False).head(15).iterrows():
            active_sigs = [c.split("_", 1)[1] for c in signal_cols if r[c] == 1]
            lines.append(f"| {r['Rank']}위 | **{r['Name']}** ({r['Ticker']}) | {r['D1_Fluc']}% | **{int(r['Sig_Count'])}개** | {', '.join(active_sigs)} |")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

if __name__ == "__main__":
    main()
