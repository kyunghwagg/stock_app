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
# 1. 전체 상장 종목 마스터 불러오기 (해외 서버 차단 대비 다중 백업)
# ---------------------------------------------------------
@st.cache_data(ttl=86400)
def get_all_tickers():
    try:
        df_krx = fdr.StockListing('KRX')
        if not df_krx.empty and 'Code' in df_krx.columns:
            df_krx = df_krx[df_krx['Code'].str.len() == 6][['Code', 'Name']]
            return df_krx
    except Exception:
        pass

    try:
        df_desc = fdr.StockListing('KRX-DESC')
        if not df_desc.empty and 'Code' in df_desc.columns:
            df_desc = df_desc[df_desc['Code'].str.len() == 6][['Code', 'Name']]
            return df_desc
    except Exception:
        pass

    try:
        rows = []
        for mkt in ["KOSPI", "KOSDAQ"]:
            for t in stock.get_market_ticker_list(market=mkt):
                rows.append({"Code": t, "Name": stock.get_market_ticker_name(t)})
        if rows:
            return pd.DataFrame(rows)
    except Exception:
        pass

    fallback = [
        {"Code": "005930", "Name": "삼성전자"},
        {"Code": "000660", "Name": "SK하이닉스"},
        {"Code": "005380", "Name": "현대차"},
        {"Code": "000270", "Name": "기아"},
        {"Code": "034020", "Name": "두산에너빌리티"},
        {"Code": "012450", "Name": "한화에어로스페이스"},
        {"Code": "035420", "Name": "NAVER"},
        {"Code": "035720", "Name": "카카오"},
        {"Code": "068270", "Name": "셀트리온"},
        {"Code": "105560", "Name": "KB금융"},
    ]
    return pd.DataFrame(fallback)

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
# ---------------------------------------------------------
def render_stock_analysis(ticker, stock_name, dart_api_key, tab_id="main"):
    fund_data, _ = get_single_fundamental(ticker)
    supply = get_investor_supply(ticker)
    dart_res = check_dart_performance(dart_api_key, ticker) if dart_api_key else {"status": "none"}

    end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=250)
    try:
        ohlcv = fdr.DataReader(ticker, start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
    except Exception:
        ohlcv = pd.DataFrame()

    if ohlcv.empty:
        st.error(f"'{stock_name} ({ticker})'의 주가 차트 데이터를 불러오지 못했습니다. 종목코드를 다시 확인해 주세요.")
        return

    latest = ohlcv.iloc[-1]

    # 상장 후 거래일이 5일 미만인 신규 상장주 예외 처리
    if len(ohlcv) < 5:
        st.divider()
        st.subheader(f"📌 {stock_name} ({int(latest['Close']):,}원)")
        st.warning(
            f"🆕 **신규 상장 종목 (상장 {len(ohlcv)}일차)** — "
            "아직 차트 이동평균선과 5일 수급 데이터가 쌓이지 않았습니다. "
            "상장 초기 변동성이 매우 크니 추격 매수에 각별히 주의하세요!"
        )

        c1, c2, c3 = st.columns(3)
        c1.metric("당일 시가", f"{int(latest['Open']):,} 원")
        c2.metric("당일 고가", f"{int(latest['High']):,} 원")
        c3.metric("당일 저가", f"{int(latest['Low']):,} 원")

        if dart_res["status"] == "success":
            st.info(
                f"📊 최근 공시 실적({dart_res['period']}): "
                f"영업이익 **{dart_res['curr_op']/100_000_000:,.1f}억 원** "
                f"(전년 대비 {dart_res['op_growth']:+.1f}%)"
            )
        return

    for ma in [5, 20, 60, 120]:
        ohlcv[f'MA{ma}'] = ohlcv['Close'].rolling(window=ma).mean()

    body = (ohlcv['Close'] - ohlcv['Open']).abs()
    lower_shadow = ohlcv[['Open', 'Close']].min(axis=1) - ohlcv['Low']
    upper_shadow = ohlcv['High'] - ohlcv[['Open', 'Close']].max(axis=1)
    ohlcv['Hammer'] = (lower_shadow >= body * 2) & (upper_shadow <= body * 0.5) & (body > 0)

    ohlcv['Prev_Close'] = ohlcv['Close'].shift(1)
    ohlcv['Vol_MA20'] = ohlcv['Volume'].rolling(window=20).mean()
    ohlcv['Gap_Surge'] = (
        (ohlcv['Open'] >= ohlcv['Prev_Close'] * 1.01) &
        (ohlcv['Close'] > ohlcv['Open']) &
        (ohlcv['Volume'] >= ohlcv['Vol_MA20'] * 1.5)
    )

    latest = ohlcv.iloc[-1]
    is_aligned = (latest['MA5'] > latest['MA20'] > latest['MA60'] > latest['MA120'])

    # [최상단] 신호등 종합 판정 계산
    reasons = []
    score = 0
    is_deficit = False

    if dart_res["status"] == "success":
        if dart_res['curr_op'] <= 0:
            is_deficit = True
            reasons.append("❌ 영업이익 적자 기업")
        elif dart_res['op_diff'] > 0:
            score += 2
            reasons.append(f"✅ 영업이익 전년 대비 {dart_res['op_growth']:+.0f}% 증가")
        else:
            score += 1
            reasons.append("⚠️ 흑자이나 영업이익 감소")

    pbr_val = fund_data.get('PBR', 0) if fund_data else 0
    if 0 < pbr_val <= 1.2:
        score += 1
        reasons.append(f"✅ 저평가 상태 (PBR {pbr_val:.2f}배)")
    elif pbr_val > 3.0:
        reasons.append(f"⚠️ 주가 고평가 주의 (PBR {pbr_val:.2f}배)")

    if supply:
        big_money = supply['inst'] + supply['frgn']
        if big_money > 0:
            score += 1
            reasons.append("✅ 최근 5일 기관·외국인 매수 유입")
        else:
            reasons.append("⚠️ 최근 5일 큰손(기관·외국인) 매도 우위")

    if is_aligned:
        score += 1
        reasons.append("✅ 차트 상승 추세(정배열)")
    else:
        reasons.append("⏸️ 차트 방향 탐색 중(비정배열)")

    st.divider()
    st.subheader(f"📌 {stock_name} ({int(latest['Close']):,}원)")

    if is_deficit:
        st.error("### 🔴 [위험] 적자 기업 — 피하세요")
    elif score >= 4:
        st.success("### 🟢 [안전] 실적·수급 우수 — 분할 매수 검토")
    elif score >= 2:
        st.warning("### 🟡 [주의] 일부 조건 부족 — 관망 추천")
    else:
        st.error("### 🔴 [위험] 수급·추세 약함 — 신중 접근")

    for r in reasons:
        st.markdown(f"**{r}**")

    # [중단] 핵심 숫자 3개 표시
    st.divider()
    m1, m2, m3 = st.columns(3)

    if dart_res["status"] == "success":
        m1.metric("최근 영업이익", f"{dart_res['curr_op']/100_000_000:,.0f} 억", f"{dart_res['op_growth']:+.1f}%")
    else:
        m1.metric("PER (수익비율)", f"{fund_data.get('PER', 0):.1f} 배" if fund_data else "-")

    if supply:
        m2.metric("기관+외국인(5일)", f"{supply['inst']+supply['frgn']:+,.0f} 억")
    else:
        m2.metric("큰손 수급", "-")

    m3.metric("PBR (1이하면 저평가)", f"{pbr_val:.2f} 배" if pbr_val > 0 else "-")

    # [하단] 일봉 + 이동평균선 차트
    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=