import streamlit as st
import pandas as pd
import requests
import re
from bs4 import BeautifulSoup
from pykrx import stock
import FinanceDataReader as fdr
import OpenDartReader
import plotly.graph_objects as go
from datetime import datetime, timedelta

st.set_page_config(page_title="안전 주식 분석기", layout="wide")
st.title("📈 안전 주식 분석기")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

CORE_BLUECHIPS = [
    ("005930", "삼성전자"), ("000660", "SK하이닉스"), ("005380", "현대차"),
    ("000270", "기아"), ("005490", "POSCO홀딩스"), ("035420", "NAVER"),
    ("035720", "카카오"), ("068270", "셀트리온"), ("105560", "KB금융"),
    ("055550", "신한지주"), ("086790", "하나금융지주"), ("012330", "현대모비스"),
    ("034020", "두산에너빌리티"), ("012450", "한화에어로스페이스"), ("009540", "HD한국조선해양"),
    ("032830", "삼성생명"), ("000810", "삼성화재"), ("017670", "SK텔레콤"),
    ("030200", "KT"), ("015760", "한국전력")
]

# 1. 전체 상장 종목 마스터 불러오기
@st.cache_data(ttl=86400)
def get_all_tickers():
    try:
        df_krx = fdr.StockListing('KRX')
        if not df_krx.empty and 'Code' in df_krx.columns:
            return df_krx[df_krx['Code'].str.len() == 6][['Code', 'Name']]
    except Exception:
        pass

    try:
        df_desc = fdr.StockListing('KRX-DESC')
        if not df_desc.empty and 'Code' in df_desc.columns:
            return df_desc[df_desc['Code'].str.len() == 6][['Code', 'Name']]
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

    return pd.DataFrame(CORE_BLUECHIPS, columns=["Code", "Name"])

# 2. 최근 영업일 탐색
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

# 3. 기관·외국인 최근 5일 수급 조회 (네이버 증권 모바일 JSON API + pykrx)
@st.cache_data(ttl=1800)
def _fetch_naver_supply_cached(ticker):
    url = f"https://m.stock.naver.com/api/stock/{ticker}/trend"
    res = requests.get(url, headers=HEADERS, timeout=5)
    data = res.json()
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError("Empty trend data")

    inst_sum = 0.0
    frgn_sum = 0.0
    for row in data[:5]:
        close_p = float(str(row.get("closePrice", "0")).replace(",", ""))
        inst_vol = float(str(row.get("organPureBuyQuant", "0")).replace(",", "").replace("+", ""))
        frgn_vol = float(str(row.get("foreignerPureBuyQuant", "0")).replace(",", "").replace("+", ""))
        inst_sum += (inst_vol * close_p) / 100_000_000
        frgn_sum += (frgn_vol * close_p) / 100_000_000

    return {"inst": inst_sum, "frgn": frgn_sum, "indiv": -(inst_sum + frgn_sum)}

def get_investor_supply(ticker):
    try:
        return _fetch_naver_supply_cached(ticker)
    except Exception:
        pass

    end_dt = get_latest_bday()
    start_dt = (datetime.strptime(end_dt, "%Y%m%d") - timedelta(days=12)).strftime("%Y%m%d")
    try:
        df_inv = stock.get_market_trading_value_by_date(start_dt, end_dt, ticker)
        if not df_inv.empty:
            recent_5 = df_inv.tail(5)
            return {
                "inst": recent_5['기관합계'].sum() / 100_000_000,
                "frgn": recent_5['외국인합계'].sum() / 100_000_000,
                "indiv": recent_5['개인'].sum() / 100_000_000
            }
    except Exception:
        pass
    return None

# 4. 개별 종목 PER·PBR 조회 (네이버 증권 모바일 JSON API + pykrx)
@st.cache_data(ttl=3600)
def _fetch_naver_fund_cached(ticker):
    url = f"https://m.stock.naver.com/api/stock/{ticker}/integration"
    res = requests.get(url, headers=HEADERS, timeout=5)
    data = res.json()
    infos = data.get("totalInfos", [])
    per_val = 0.0
    pbr_val = 0.0
    for item in infos:
        code = item.get("code")
        val_str = str(item.get("value", "")).replace(",", "").replace("배", "").strip()
        if code == "per" and val_str and val_str != "N/A":
            per_val = float(val_str)
        elif code == "pbr" and val_str and val_str != "N/A":
            pbr_val = float(val_str)
    if per_val == 0.0 and pbr_val == 0.0:
        raise ValueError("No PER/PBR")
    return {"PER": per_val, "PBR": pbr_val}

def get_single_fundamental(ticker):
    bday = get_latest_bday()
    try:
        return _fetch_naver_fund_cached(ticker), bday
    except Exception:
        pass

    try:
        df = stock.get_market_fundamental(bday, bday, ticker)
        if not df.empty and (df.iloc[0]['PBR'] > 0 or df.iloc[0]['PER'] > 0):
            return df.iloc[0].to_dict(), bday
    except Exception:
        pass
    return None, bday

# 5. OpenDART 공시 영업이익 검증
@st.cache_data(ttl=86400)
def check_dart_performance(api_key, ticker):
    if not api_key:
        return {"status": "none"}
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

# 6. [자동 추천 엔진] 상승장 추세주 + 하락장 저평가 우량주 TOP 5 보장
@st.cache_data(ttl=1800)
def get_auto_recommended_stocks(dart_api_key, market="KOSPI"):
    bday = get_latest_bday()
    candidates = []
    seen = set()

    try:
        df_ohlcv = stock.get_market_ohlcv(bday, market=market)
        df_fund = stock.get_market_fundamental(bday, market=market)
        if not df_ohlcv.empty and not df_fund.empty:
            df = df_ohlcv.join(df_fund[['PER', 'PBR']], how='left')
            df['거래대금(억)'] = df['거래대금'] / 100_000_000
            cond = (df['거래대금(억)'] >= 100) & (df['PBR'] > 0) & (df['PBR'] <= 2.0)
            top_df = df[cond].sort_values(by='거래대금(억)', ascending=False).head(15)
            for t in top_df.index:
                candidates.append((t, stock.get_market_ticker_name(t)))
                seen.add(t)
    except Exception:
        pass

    if market == "KOSPI":
        for t, name in CORE_BLUECHIPS:
            if t not in seen:
                candidates.append((t, name))
                seen.add(t)

    primary_results = []
    value_backups = []

    end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=100)

    for ticker, name in candidates:
        try:
            ohlcv = fdr.DataReader(ticker, start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
            if ohlcv.empty or len(ohlcv) < 20:
                continue

            ma5 = ohlcv['Close'].rolling(5).mean().iloc[-1]
            ma20 = ohlcv['Close'].rolling(20).mean().iloc[-1]
            ma60 = ohlcv['Close'].rolling(60).mean().iloc[-1] if len(ohlcv) >= 60 else ma20
            curr_close = int(ohlcv['Close'].iloc[-1])
            prev_close = int(ohlcv['Close'].iloc[-2])
            chg_pct = round((curr_close - prev_close) / prev_close * 100, 1)

            supply = get_investor_supply(ticker)
            big_money = (supply['inst'] + supply['frgn']) if supply else 0

            fund_data, _ = get_single_fundamental(ticker)
            pbr_val = round(fund_data.get('PBR', 0), 2) if fund_data else 0

            dart_res = check_dart_performance(dart_api_key, ticker)
            is_deficit = (dart_res["status"] == "success" and dart_res['curr_op'] <= 0)
            is_earnings_up = (dart_res["status"] == "success" and dart_res['op_diff'] > 0)
            is_profitable = (dart_res["status"] == "success" and dart_res['curr_op'] > 0)

            if is_deficit:
                continue

            is_up_trend = (curr_close >= ma20) and (ma5 >= ma20 or ma20 >= ma60)

            row_base = {
                "종목코드": ticker,
                "종목명": name,
                "현재가": f"{curr_close:,}원",
                "등락률": f"{chg_pct:+.1f}%",
                "큰손수급(5일)": f"{big_money:+,.0f}억" if supply else "-",
                "PBR": f"{pbr_val:.2f}배" if pbr_val > 0 else "-",
            }

            if is_earnings_up and is_up_trend and big_money > 0:
                primary_results.append({
                    **row_base,
                    "판정": "🟢 적극 검토",
                    "추천이유": "실적성장 + 큰손매수 + 상승추세",
                    "_score": 100 + big_money
                })
            elif is_up_trend and big_money > 0:
                primary_results.append({
                    **row_base,
                    "판정": "🟢 수급·추세 우수",
                    "추천이유": "기관·외국인 매수 유입",
                    "_score": 70 + big_money
                })
            elif (is_earnings_up or is_profitable) and (0 < pbr_val <= 1.3):
                growth_bonus = max(dart_res.get('op_growth', 0), 0) * 0.2 if dart_res["status"] == "success" else 0
                val_score = (1.5 - pbr_val) * 30 + growth_bonus
                reason_txt = "실적성장 + 저평가 눌림목" if is_earnings_up else f"흑자 우량주 + 저평가(PBR {pbr_val:.2f})"
                value_backups.append({
                    **row_base,
                    "판정": "🔵 저평가 분할매수",
                    "추천이유": reason_txt,
                    "_score": val_score
                })
        except Exception:
            continue

    primary_results.sort(key=lambda x: x["_score"], reverse=True)
    value_backups.sort(key=lambda x: x["_score"], reverse=True)

    combined = primary_results[:5]
    if len(combined) < 5:
        combined.extend(value_backups[:5 - len(combined)])

    if not combined:
        return pd.DataFrame(), bday

    return pd.DataFrame(combined).drop(columns=["_score"]), bday

# 7. 상세 진단 화면 렌더링
def render_stock_analysis(ticker, stock_name, dart_api_key, tab_id="main"):
    fund_data, _ = get_single_fundamental(ticker)
    supply = get_investor_supply(ticker)
    dart_res = check_dart_performance(dart_api_key, ticker)

    end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=250)
    try:
        ohlcv = fdr.DataReader(ticker, start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
    except Exception:
        ohlcv = pd.DataFrame()

    if ohlcv.empty:
        st.error(f"'{stock_name} ({ticker})'의 주가 차트 데이터를 불러오지 못했습니다.")
        return

    latest = ohlcv.iloc[-1]

    if len(ohlcv) < 5:
        st.divider()
        st.subheader(f"📌 {stock_name} ({int(latest['Close']):,}원)")
        st.warning(f"🆕 신규 상장 종목 (상장 {len(ohlcv)}일차) — 차트 추세 데이터가 아직 부족합니다. 초기 변동성에 주의하세요!")
        c1, c2, c3 = st.columns(3)
        c1.metric("당일 시가", f"{int(latest['Open']):,} 원")
        c2.metric("당일 고가", f"{int(latest['High']):,} 원")
        c3.metric("당일 저가", f"{int(latest['Low']):,} 원")
        if dart_res["status"] == "success":
            st.info(f"📊 최근 실적({dart_res['period']}): 영업이익 {dart_res['curr_op']/100_000_000:,.1f}억 원 ({dart_res['op_growth']:+.1f}%)")
        return

    for ma in [5, 20, 60, 120]:
        ohlcv[f'MA{ma}'] = ohlcv['Close'].rolling(window=ma).mean()

    body = (ohlcv['Close'] - ohlcv['Open']).abs()
    lower_shadow = ohlcv[['Open', 'Close']].min(axis=1) - ohlcv['Low']
    upper_shadow = ohlcv['High'] - ohlcv[['Open', 'Close']].max(axis=1)
    ohlcv['Hammer'] = (lower_shadow >= body * 2) & (upper_shadow <= body * 0.5) & (body > 0)

    ohlcv['Prev_Close'] = ohlcv['Close'].shift(1)
    ohlcv['Vol_MA20'] = ohlcv['Volume'].rolling(window=20).mean()
    ohlcv['Gap_Surge'] = (ohlcv['Open'] >= ohlcv['Prev_Close'] * 1.01) & (ohlcv['Close'] > ohlcv['Open']) & (ohlcv['Volume'] >= ohlcv['Vol_MA20'] * 1.5)

    latest = ohlcv.iloc[-1]
    prev = ohlcv.iloc[-2]
    is_aligned = (latest['MA5'] > latest['MA20'] > latest['MA60'] > latest['MA120'])
    is_mid_up = (latest['Close'] > latest['MA20']) and (latest['MA20'] > latest['MA60'])
    is_golden = (prev['MA5'] <= prev['MA20']) and (latest['MA5'] > latest['MA20'])

    fund_reasons = []
    tech_reasons = []
    is_deficit = False
    is_earnings_up = False
    is_profitable = False
    is_undervalued = False
    is_big_buying = False

    if dart_res["status"] == "success":
        if dart_res['curr_op'] <= 0:
            is_deficit = True
            fund_reasons.append(f"❌ 영업이익 적자 ({dart_res['period']})")
        elif dart_res['op_diff'] > 0:
            is_earnings_up = True
            is_profitable = True
            fund_reasons.append(f"✅ 영업이익 전년 대비 {dart_res['op_growth']:+.0f}% 성장 ({dart_res['period']})")
        else:
            is_profitable = True
            fund_reasons.append(f"⚠️ 흑자 유지 중이나 이익 감소 ({dart_res['op_growth']:+.0f}%)")
    else:
        fund_reasons.append("ℹ️ 공시 실적 데이터 미확인")

    pbr_val = fund_data.get('PBR', 0) if fund_data else 0
    per_val = fund_data.get('PER', 0) if fund_data else 0
    if 0 < pbr_val <= 1.3:
        is_undervalued = True
        fund_reasons.append(f"✅ 자산 대비 저평가 (PBR {pbr_val:.2f}배)")
    elif 1.3 < pbr_val <= 2.5:
        fund_reasons.append(f"🆗 적정 주가 수준 (PBR {pbr_val:.2f}배)")
    elif pbr_val > 2.5:
        fund_reasons.append(f"⚠️ 고평가 구간 주의 (PBR {pbr_val:.2f}배)")

    if supply:
        big_money = supply['inst'] + supply['frgn']
        if supply['inst'] > 0 and supply['frgn'] > 0:
            is_big_buying = True
            tech_reasons.append(f"✅ 기관·외국인 쌍끌이 매수 ({big_money:+,.0f}억)")
        elif big_money > 0:
            is_big_buying = True
            tech_reasons.append(f"✅ 큰손(기관+외인) 순매수 유입 ({big_money:+,.0f}억)")
        else:
            tech_reasons.append(f"⚠️ 최근 5일 큰손 매도 우위 ({big_money:+,.0f}억)")

    if is_aligned:
        tech_reasons.append("✅ 이동평균선 완전 정배열 (강한 상승세)")
    elif is_mid_up:
        tech_reasons.append("🆗 20일·60일선 위 우상향 추세 유지")
    elif latest['Hammer'] or is_golden:
        tech_reasons.append("🌱 하락 멈춤 및 단기 반등(망치형/골든크로스) 포착")
    else:
        tech_reasons.append("⏸️ 단기 조정 구간 (이동평균선 아래 숨고르기)")

    st.divider()
    st.subheader(f"📌 {stock_name} ({int(latest['Close']):,}원)")

    if is_deficit:
        st.error("### 🔴 [위험 / 회피] 영업이익 적자 기업\n실적이 뒷받침되지 않으므로 추격 매수를 피하세요.")
    elif is_earnings_up and (is_aligned or is_mid_up) and is_big_buying:
        st.success("### 🟢 [적극 검토] 실적·수급·추세 3박자 우수\n이익이 늘고 큰손도 매수 중인 건강한 상승 흐름입니다. (분할 매수 검토)")
    elif (is_earnings_up or is_profitable) and is_undervalued and not (is_aligned or is_mid_up):
        st.info("### 🔵 [저평가 분할매수 기회] 실적 튼튼·장세 영향 눌림목\n회사는 흑자를 내는 저평가 우량주이나, 시장 분위기로 주가만 쉬어가는 중입니다. (천천히 나눠서 매수 검토)")
    elif (is_aligned or is_mid_up) and is_big_buying and not is_earnings_up:
        st.warning("### 🟡 [단기 수급주 / 주의] 차트·수급은 좋으나 실적 확인 필요\n큰손 매수세로 오르고 있으나 이익 성장이 약합니다. -10% 손절선을 꼭 지키세요.")
    else:
        st.warning("### 🟡 [관망 추천] 추세 및 수급 회복 대기\n아직 큰손 수급이나 상승 추세가 확실히 자리 잡지 않았습니다. 바닥 확인 후 접근하세요.")

    c_f, c_t = st.columns(2)
    with c_f:
        st.markdown("**🏢 기업 체력 (실적·가치)**")
        for r in fund_reasons:
            st.markdown(f"- {r}")
    with c_t:
        st.markdown("**📊 시장 분위기 (수급·차트)**")
        for r in tech_reasons:
            st.markdown(f"- {r}")

    # [중단] 핵심 숫자 4개 (영업이익 + 큰손수급 + PER + PBR 모두 표시)
    st.divider()
    m1, m2, m3, m4 = st.columns(4)
    if dart_res["status"] == "success":
        m1.metric("최근 영업이익", f"{dart_res['curr_op']/100_000_000:,.0f} 억", f"{dart_res['op_growth']:+.1f}%")
    else:
        m1.metric("최근 영업이익", "-")

    if supply:
        m2.metric("큰손수급(5일)", f"{supply['inst']+supply['frgn']:+,.0f} 억")
    else:
        m2.metric("큰손수급(5일)", "-")

    m3.metric("PER (주가수익비율)", f"{per_val:.1f} 배" if per_val > 0 else "-")
    m4.metric("PBR (1이하면 저평가)", f"{pbr_val:.2f} 배" if pbr_val > 0 else "-")

    # [하단] 일봉 + 이동평균선 차트
    fig = go.Figure()
    candle = go.Candlestick(
        x=ohlcv.index, open=ohlcv['Open'], high=ohlcv['High'], low=ohlcv['Low'], close=ohlcv['Close'],
        name='일봉', increasing_line_color='red', decreasing_line_color='blue'
    )
    fig.add_trace(candle)

    colors = {'MA5': 'green', 'MA20': 'orange', 'MA60': 'purple', 'MA120': 'gray'}
    for ma, color in colors.items():
        line_trace = go.Scatter(x=ohlcv.index, y=ohlcv[ma], mode='lines', name=ma, line=dict(color=color, width=1.5))
        fig.add_trace(line_trace)

    hammers = ohlcv[ohlcv['Hammer']]
    hammer_trace = go.Scatter(
        x=hammers.index, y=hammers['Low'] * 0.98, mode='markers',
        name='바닥지지(망치형)', marker=dict(symbol='triangle-up', size=11, color='green')
    )
    fig.add_trace(hammer_trace)

    surges = ohlcv[ohlcv['Gap_Surge']]
    surge_trace = go.Scatter(
        x=surges.index, y=surges['Low'] * 0.96, mode='markers',
        name='수급돌파 신호', marker=dict(symbol='triangle-up', size=14, color='red')
    )
    fig.add_trace(surge_trace)

    fig.update_layout(
        xaxis_rangeslider_visible=False,
        height=420,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    st.plotly_chart(fig, use_container_width=True, key=f"chart_{tab_id}_{ticker}")
    st.caption("▲ 빨간 세모: 거래량 동반 돌파 신호 / ▲ 초록 세모: 바닥 지지 신호")

# 사이드바 설정
try:
    default_dart_key = st.secrets.get("DART_API_KEY", "")
except Exception:
    default_dart_key = ""

with st.sidebar:
    st.header("⚙️ 설정")
    if default_dart_key:
        dart_api_key = default_dart_key
        st.success("✅ 공시 데이터 자동 연결됨")
    else:
        dart_api_key = st.text_input("OpenDART API Key", type="password")

# 메인 화면 탭
tab1, tab2 = st.tabs(["⚡ 오늘의 안전 추천주 (자동선별)", "🔍 원하는 종목 직접 검색"])

with tab1:
    st.markdown("상승 추세 수급주와 **하락장에서도 튼튼한 저평가 우량주 TOP 5**를 자동으로 골라줍니다.")
    market_sel = st.radio("시장 선택", ["KOSPI", "KOSDAQ"], horizontal=True)

    with st.spinner("공식 데이터로 안전한 추천 종목을 자동 선별하는 중입니다..."):
        df_rec, bday_str = get_auto_recommended_stocks(dart_api_key, market_sel)

    st.caption(f"기준일: {bday_str} (적자 기업 및 고평가 거품주 자동 제외됨)")

    if df_rec.empty:
        st.warning("현재 조건을 만족하는 종목을 불러오지 못했습니다. 잠시 후 다시 시도하거나 두 번째 탭에서 직접 검색해 주세요.")
    else:
        st.dataframe(df_rec[['종목명', '판정', '현재가', '등락률', '큰손수급(5일)', 'PBR', '추천이유']], use_container_width=True, hide_index=True)

        rec_names = df_rec['종목명'].tolist()
        selected_rec = st.radio("👇 아래에서 종목을 누르면 상세 진단과 차트가 열립니다", rec_names, horizontal=True)
        if selected_rec:
            rec_row = df_rec[df_rec['종목명'] == selected_rec].iloc[0]
            render_stock_analysis(rec_row['종목코드'], rec_row['종목명'], dart_api_key, tab_id="auto_rec")

with tab2:
    all_tickers = get_all_tickers()
    all_tickers['Display'] = all_tickers['Name'] + " (" + all_tickers['Code'] + ")"
    options = all_tickers['Display'].tolist()
    default_idx = next((i for i, s in enumerate(options) if s.startswith("삼성전자 (")), 0)

    selected_display = st.selectbox("회사 이름을 선택하거나 입력하세요", options, index=default_idx)
    custom_code = st.text_input("또는 6자리 종목코드 직접 입력 (예: 030530)", value="")

    if custom_code.strip() and len(custom_code.strip()) == 6:
        code_input = custom_code.strip()
        matched = all_tickers[all_tickers['Code'] == code_input]
        s_name = matched['Name'].iloc[0] if not matched.empty else code_input
        render_stock_analysis(code_input, s_name, dart_api_key, tab_id="search_custom")
    elif selected_display:
        sel_row = all_tickers[all_tickers['Display'] == selected_display].iloc[0]
        render_stock_analysis(sel_row['Code'], sel_row['Name'], dart_api_key, tab_id="search")