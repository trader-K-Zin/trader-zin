import os
import datetime
import requests
from bs4 import BeautifulSoup
import pandas as pd
from pykrx import stock

# 한국 시간(KST) 기준 현재 시각
now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
today_str = now.strftime("%Y%m%d")

DB_FILE = "history_db.csv"
REPORT_FILE = "WIN_RATE_REPORT.md"

def get_recent_trading_days(n=35):
    """최근 영업일 리스트 가져오기 (휴장일 자동 제외)"""
    start_dt = (now - datetime.timedelta(days=60)).strftime("%Y%m%d")
    days = stock.get_previous_business_days(fromdate=start_dt, todate=today_str)
    return [d.strftime("%Y%m%d") for d in days[-n:]]

def get_naver_popular_tickers():
    """네이버 금융 실시간 인기 검색 상위 종목코드 추출"""
    tickers = set()
    try:
        url = "https://finance.naver.com/sise/lastsearch2.naver"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        for a in soup.select('a.tltle'):
            href = a.get('href', '')
            if 'code=' in href:
                tickers.add(href.split('code=')[-1])
    except Exception:
        pass
    return tickers

def get_morning_high_return(ticker, prev_close):
    """당일 오전(08:00~10:00) 최고가 수익률 확인 (네이버 실시간 분봉/시세 연동)"""
    try:
        # 오전 10시경 실행 시 당일 시가/고가 조회를 통해 아침 최고가 정확히 포착
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')
        rate_info = soup.select_one('div.rate_info')
        if rate_info:
            # 고가 추출
            high_tag = soup.select_one('table.no_info tr:nth-of-type(1) td:nth-of-type(2) em span.blind')
            if high_tag:
                morning_high = int(high_tag.text.replace(',', ''))
                if morning_high > 0 and prev_close > 0:
                    return round(((morning_high - prev_close) / prev_close) * 100, 2)
    except Exception:
        pass
    return None

def analyze_d1_signals(d1_date, trading_days, popular_set):
    """특정일(D-1) 거래대금 상위 40개 종목 추출 및 12대 시그널 전수 조사"""
    idx = trading_days.index(d1_date)
    d2_date = trading_days[idx - 1]
    d3_date = trading_days[idx - 2]
    start_60d = trading_days[max(0, idx - 60)]

    # 코스피 + 코스닥 전일 시세 및 거래대금 통합
    df_kospi = stock.get_market_ohlcv_by_ticker(d1_date, market="KOSPI")
    df_kosdaq = stock.get_market_ohlcv_by_ticker(d1_date, market="KOSDAQ")
    df_all = pd.concat([df_kospi, df_kosdaq])
    df_all = df_all[df_all["거래량"] > 0]

    # 전일 거래대금 상위 40위 추출
    top40 = df_all.sort_values(by="거래대금", ascending=False).head(40)

    # 최근 3일 투자자별 순매수 데이터 로드 (외국인, 기관합계, 연기금 등)
    def get_inv_map(dt, inv_type):
        k1 = stock.get_market_net_purchases_of_equities_by_ticker(dt, dt, "KOSPI", inv_type)
        k2 = stock.get_market_net_purchases_of_equities_by_ticker(dt, dt, "KOSDAQ", inv_type)
        combined = pd.concat([k1, k2])
        return combined["순매수거래대금"].to_dict() if not combined.empty else {}

    for_d1 = get_inv_map(d1_date, "외국인")
    for_d2 = get_inv_map(d2_date, "외국인")
    for_d3 = get_inv_map(d3_date, "외국인")

    inst_d1 = get_inv_map(d1_date, "기관합계")
    inst_d2 = get_inv_map(d2_date, "기관합계")
    inst_d3 = get_inv_map(d3_date, "기관합계")

    pension_d1 = get_inv_map(d1_date, "연기금등")

    rows = []
    for rank, (ticker, row) in enumerate(top40.iterrows(), 1):
        name = stock.get_market_ticker_name(ticker)
        close_p = row["종가"]
        high_p = row["고가"]
        fluc = row["등락률"]
        val_d1 = row["거래대금"]

        # 개별 종목 최근 60일 일봉 (이평선, 거래대금 평균, 60일 신고가 계산)
        df_hist = stock.get_market_ohlcv_by_date(start_60d, d1_date, ticker)
        if len(df_hist) < 20:
            continue

        high_60d = df_hist["고가"].max()
        avg_val_20d = df_hist["거래대금"].iloc[-21:-1].mean() if len(df_hist) > 20 else val_d1
        ma5 = df_hist["종가"].iloc[-5:].mean()
        ma20 = df_hist["종가"].iloc[-20:].mean()
        ma60 = df_hist["종가"].mean()

        # 12대 시그널 판별 (True=1, False=0)
        s1_popular = 1 if ticker in popular_set else 0
        s2_for_buy = 1 if for_d1.get(ticker, 0) > 0 else 0
        s3_for_3d = 1 if (for_d1.get(ticker, 0) > 0 and for_d2.get(ticker, 0) > 0 and for_d3.get(ticker, 0) > 0) else 0
        s4_inst_buy = 1 if inst_d1.get(ticker, 0) > 0 else 0
        s5_inst_3d = 1 if (inst_d1.get(ticker, 0) > 0 and inst_d2.get(ticker, 0) > 0 and inst_d3.get(ticker, 0) > 0) else 0
        s6_newhigh_zone = 1 if close_p >= high_60d * 0.95 else 0
        s7_vol_surge = 1 if (avg_val_20d > 0 and val_d1 >= avg_val_20d * 3.0) else 0
        s8_twin_buy = 1 if (s2_for_buy == 1 and s4_inst_buy == 1) else 0
        s9_pension_buy = 1 if pension_d1.get(ticker, 0) > 0 else 0
        s10_strong_close = 1 if (fluc >= 3.0 and high_p > 0 and close_p >= high_p * 0.98) else 0
        s11_ma_align = 1 if (ma5 > ma20 > ma60) else 0
        s12_super_combo = 1 if (s8_twin_buy == 1 and s6_newhigh_zone == 1 and s10_strong_close == 1) else 0

        rows.append({
            "D1_Date": d1_date,
            "Rank": rank,
            "Ticker": ticker,
            "Name": name,
            "D1_Close": close_p,
            "D1_Fluc": round(fluc, 2),
            "D1_Val_100M": round(val_d1 / 100000000, 1),
            "S1_실시간검색상위": s1_popular,
            "S2_외국인순매수": s2_for_buy,
            "S3_외국인3일연속": s3_for_3d,
            "S4_기관순매수": s4_inst_buy,
            "S5_기관3일연속": s5_inst_3d,
            "S6_신고가가시권": s6_newhigh_zone,
            "S7_거래대금3배폭발": s7_vol_surge,
            "S8_외인기관양매수": s8_twin_buy,
            "S9_연기금순매수": s9_pension_buy,
            "S10_꽉찬양봉마감": s10_strong_close,
            "S11_이평선정배열": s11_ma_align,
            "S12_S급복합시그널": s12_super_combo,
        })
    return pd.DataFrame(rows)

def main():
    trading_days = get_recent_trading_days(35)
    popular_set = get_naver_popular_tickers()

    # 기존 누적 DB 로드 (없으면 최근 5거래일치 즉시 역추적하여 초기 DB 생성)
    if os.path.exists(DB_FILE):
        db_df = pd.read_csv(DB_FILE, dtype={"Ticker": str, "D1_Date": str, "D_Day": str})
    else:
        db_df = pd.DataFrame()
        # 처음 실행 시 즉시 승률표를 볼 수 있도록 최근 5거래일 자동 역추적 백필
        for i in range(-6, -1):
            d1_dt = trading_days[i]
            d_day = trading_days[i + 1]
            df_sig = analyze_d1_signals(d1_dt, trading_days, popular_set)
            ohlcv_k1 = stock.get_market_ohlcv_by_ticker(d_day, market="KOSPI")
            ohlcv_k2 = stock.get_market_ohlcv_by_ticker(d_day, market="KOSDAQ")
            ohlcv_dday = pd.concat([ohlcv_k1, ohlcv_k2])

            df_sig["D_Day"] = d_day
            max_rets, wins = [], []
            for _, r in df_sig.iterrows():
                tk = r["Ticker"]
                prev_c = r["D1_Close"]
                if tk in ohlcv_dday.index and prev_c > 0:
                    high_d = ohlcv_dday.loc[tk, "고가"]
                    ret = round(((high_d - prev_c) / prev_c) * 100, 2)
                else:
                    ret = 0.0
                max_rets.append(ret)
                wins.append(1 if ret >= 3.0 else 0)
            df_sig["Morning_Max_Ret"] = max_rets
            df_sig["Win_3Pct"] = wins
            db_df = pd.concat([db_df, df_sig], ignore_index=True)

    # 오늘(D_Day) 오전 10시 실행 시: 어제(D-1) 상위 40개 종목의 오늘 아침 +3% 달성 여부 업데이트
    latest_day = trading_days[-1]
    prev_day = trading_days[-2]

    if len(db_df) == 0 or prev_day not in db_df["D1_Date"].values:
        df_today_eval = analyze_d1_signals(prev_day, trading_days, popular_set)
        df_today_eval["D_Day"] = latest_day
        ohlcv_k1 = stock.get_market_ohlcv_by_ticker(latest_day, market="KOSPI")
        ohlcv_k2 = stock.get_market_ohlcv_by_ticker(latest_day, market="KOSDAQ")
        ohlcv_dday = pd.concat([ohlcv_k1, ohlcv_k2])

        max_rets, wins = [], []
        for _, r in df_today_eval.iterrows():
            tk = r["Ticker"]
            prev_c = r["D1_Close"]
            ret = get_morning_high_return(tk, prev_c)
            if ret is None:
                if tk in ohlcv_dday.index and prev_c > 0:
                    high_d = ohlcv_dday.loc[tk, "고가"]
                    ret = round(((high_d - prev_c) / prev_c) * 100, 2)
                else:
                    ret = 0.0
            max_rets.append(ret)
            wins.append(1 if ret >= 3.0 else 0)
        df_today_eval["Morning_Max_Ret"] = max_rets
        df_today_eval["Win_3Pct"] = wins
        db_df = pd.concat([db_df, df_today_eval], ignore_index=True)

    db_df.to_csv(DB_FILE, index=False, encoding="utf-8-sig")

    # 12대 시그널별 누적 승률 리포트 작성
    signal_cols = [c for c in db_df.columns if c.startswith("S")]
    total_samples = len(db_df)
    base_win_rate = round(db_df["Win_3Pct"].mean() * 100, 1) if total_samples > 0 else 0.0

    lines = [
        f"# 🎯 트레이더 진 주도주 역추적 & 조건별 +3% 슈팅 승률 DB",
        f"* **최종 업데이트:** {now.strftime('%Y-%m-%d %H:%M')} (KST)",
        f"* **누적 검증 표본:** 총 **{total_samples}개** 주도주 (전일 거래대금 상위 40위 기준)",
        f"* **전체 평균 성공률(Base Rate):** **{base_win_rate}%** (아무 조건 없이 상위 40위 매수 시 익일 +3% 달성 확률)\n",
        "---",
        "## 📊 1. 전일 시그널별 익일 오전 +3% 이상 상승 승률 랭킹\n",
        "| 순위 | 전일(D-1) 발생 시그널 조건 | 조건 발생 횟수 | +3% 슈팅 성공 | **조건별 승률(%)** | 평균 최고수익률(%) |",
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
        lines.append(f"| {idx}위 | **{s_name}** | {cnt}건 | {wins}건 | **{w_rate}%** | +{avg_ret}% |")

    # 가장 최근 검증일(오늘 아침) +3% 성공 종목 및 보유 시그널 역추적 명단
    latest_eval = db_df[db_df["D_Day"] == latest_day].sort_values(by="Morning_Max_Ret", ascending=False)
    lines.append(f"\n---\n## 🔥 2. 최근 거래일({latest_day}) +3% 슈팅 성공 주도주 역추적 결과\n")
    lines.append("| 종목명 | 전일 거래대금 순위 | 당일 최고 상승률 | 전일(D-1)에 나타났던 핵심 시그널 흔적 |")
    lines.append("| :--- | :---: | :---: | :--- |")
    for _, r in latest_eval[latest_eval["Win_3Pct"] == 1].head(15).iterrows():
        active_sigs = [c.split("_", 1)[1] for c in signal_cols if r[c] == 1]
        sig_str = ", ".join(active_sigs) if active_sigs else "단순 거래대금 상위"
        lines.append(f"| **{r['Name']}** ({r['Ticker']}) | {r['Rank']}위 ({r['D1_Val_100M']}억) | **+{r['Morning_Max_Ret']}%** | {sig_str} |")

    # 오늘 마감 기준 -> 내일 아침(08~10시) 공략용 거래대금 상위 40위 시그널 포착 리스트
    df_next = analyze_d1_signals(latest_day, trading_days, popular_set)
    lines.append(f"\n---\n## 🚀 3. 다음 거래일 오전(08:00~10:00) +3% 노림수 타점 후보 (최근 마감 기준)\n")
    lines.append("| 전일 대금순위 | 종목명 | 전일 등락률 | 포착된 핵심 시그널 개수 | 보유 시그널 상세 |")
    lines.append("| :---: | :--- | :---: | :---: | :--- |")
    df_next["Sig_Count"] = df_next[signal_cols].sum(axis=1)
    for _, r in df_next.sort_values(by="Sig_Count", ascending=False).head(15).iterrows():
        active_sigs = [c.split("_", 1)[1] for c in signal_cols if r[c] == 1]
        lines.append(f"| {r['Rank']}위 | **{r['Name']}** ({r['Ticker']}) | {r['D1_Fluc']}% | **{int(r['Sig_Count'])}개** | {', '.join(active_sigs)} |")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

if __name__ == "__main__":
    main()
