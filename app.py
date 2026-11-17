import streamlit as st
import pandas as pd
from pykrx import stock
import FinanceDataReader as fdr
import OpenDartReader
import plotly.graph_objects as go
from datetime import datetime, timedelta

st.set_page_config(page_title="안전 주식 분석기", layout="wide")
st.title("📈 안전 주식 분석기")

# ---------------------------------------------------------
# 1. 전체 상장 종목 마스터 불러오기
# ---------------------------------------------------------
@st.cache_data(ttl=86400)
def get_all_tickers():
    try:
        df_krx = fdr.StockListing('KRX')
        df_krx = df_krx[df_krx['Code'].str.len() == 6][['Code', 'Name', 'Market']]
        return df_krx
    except Exception:
        return pd.DataFrame(columns=['Code', 'Name', 'Market'])

# ---------------------------------------------------------
# 2. 최근 영업일 탐색 헬퍼
# ---------------------------------------------------------
@st.cache_data(ttl=3600)
def get_latest_bday():
    for i in range(10):
        target_dt = (datetime.today() - timedelta(days=i)).strftime("%Y%m%d")
        try:
            temp = stock.get_market_ohlcv(target_dt, market="KOSPI")
            if not temp.empty and temp['거래대금'].sum() > 0:
                return target_dt
        except Exception:
            continue
    return datetime.today().strftime("%Y%m%d")

# ---------------------------------------------------------
# 3. [조건검색기] 거래대금 + 시가 갭상승 + 저평가 스크리닝
# ---------------------------------------------------------
@st.cache_data(ttl=1800)
def run_condition_screener(market="KOSPI", min_amt_uk=300, min_gap_pct=0.5, max_pbr=2.5):
    bday = get_latest_bday()
    try:
        df_ohlcv = stock.get_market_ohlcv(bday, market=market)
        df_fund = stock.get_market_fundamental(bday, market=market)

        if df_ohlcv.empty:
            return pd.DataFrame(), bday

        df = df_ohlcv.join(df_fund[['PER', 'PBR']], how='left')
        df['종목명'] = [stock.get_market_ticker_name(t) for t in df.index]

        df['전일종가'] = df['종가'] / (1 + df['등락률'] / 100)
        df['시가갭(%)'] = (((df['시가'] - df['전일종가']) / df['전일종가']) * 100).round(1)
        df['거래대금(억)'] = (df['거래대금'] / 100_000_000).round(0)
        df['등락률'] = df['등락률'].round(1)

        cond = (
            (df['거래대금(억)'] >= min_amt_uk) &
            (df['시가갭(%)'] >= min_gap_pct) &
            (df['PBR'].fillna(0) <= max_pbr)
        )
        res = df[cond].sort_values(by='거래대금(억)', ascending=False)
        cols = ['종목명', '종가', '등락률', '거래대금(억)', 'PBR']
        return res[cols], bday
    except Exception:
        return pd.DataFrame(), bday

# ---------------------------------------------------------
# 4. 개별 종목 기관·외국인 최근 5일 수급 조회
# ---------------------------------------------------------
@st.cache_data(ttl=1800)
def get_investor_supply(ticker):
    end_dt = get_latest_bday()
    start_dt = (datetime.strptime(end_dt, "%Y%m%d") - timedelta(days=12)).strftime("%Y%m%d")
    try:
        df_inv = stock.get_market_trading_value_by_date(start_dt, end_dt, ticker)
        if df_inv.empty:
            return None
        recent_5 = df_inv.tail(5)
        return {
            "inst": recent_5['기관합계'].sum() / 100_000_000,
            "frgn": recent_5['외국인합계'].sum() / 100_000_000,
            "indiv": recent_5['개인'].sum() / 100_000_000
        }
    except Exception:
        return None

# ---------------------------------------------------------
# 5. 개별 종목 PER·PBR 및 OpenDART 실적 검증
# ---------------------------------------------------------
@st.cache_data(ttl=3600)
def get_single_fundamental(ticker):
    bday = get_latest_bday()
    try:
        df = stock.get_market_fundamental(bday, bday, ticker)
        if not df.empty:
            return df.iloc[0].to_dict(), bday
    except Exception:
        pass
    return None, bday

@st.cache_data(ttl=86400)
def check_dart_performance(api_key, ticker):
    try:
        dart = OpenDartReader(api_key)
        current_year = datetime.today().year
        reports = [
            (current_year, '11014', '3분기'),
            (current_year, '11012', '반기'),
            (current_year, '11013', '1분기'),
            (current_year - 1, '11011', '작년결산'),
            (current_year - 1, '11014', '작년 3분기'),
        ]
        for yr, reprt_code, label in reports:
            fs = dart.finstate(ticker, yr, reprt_code=reprt_code)
            if fs is None or fs.empty:
                continue
            fs_sub = fs[fs['fs_div'] == 'CFS']
            if fs_sub.empty:
                fs_sub = fs[fs['fs_div'] == 'OFS']

            op_row = fs_sub[fs_sub['account_nm'].str.contains('영업이익', na=False)]
            if not op_row.empty:
                def parse_num(val):
                    if pd.isna(val) or str(val).strip() in ['', '-']:
                        return 0
                    return int(str(val).replace(',', '').strip())

                curr_op = parse_num(op_row.iloc[0]['thstrm_amount'])
                prev_op = parse_num(op_row.iloc[0]['frmtrm_amount'])
                op_diff = curr_op - prev_op
                op_growth = ((curr_op - prev_op) / abs(prev_op) * 100) if prev_op != 0 else 0.0

                return {
                    "status": "success",
                    "period": f"{yr}년 {label}",
                    "curr_op": curr_op,
                    "op_diff": op_diff,
                    "op_growth": op_growth
                }
        return {"status": "no_data"}
    except Exception:
        return {"status": "error"}

# ---------------------------------------------------------
# 6. 모바일 큰 글씨 최적화 상세 진단 렌더링