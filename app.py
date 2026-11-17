import streamlit as st
import pandas as pd
from pykrx import stock
import FinanceDataReader as fdr
import OpenDartReader
import plotly.graph_objects as go
from datetime import datetime, timedelta

st.set_page_config(page_title="아버지 전용 주식 아가방 & 수급/차트 검색기", layout="wide")
st.title("📈 [실적 + 수급 + 차트] 안전 종목 검색기")

# ---------------------------------------------------------
# 0. 실전 투자 판단 가이드
# ---------------------------------------------------------
with st.expander("📖 [주식 아가방 & 수급·차트] 안전 투자 가이드 (클릭하여 열기)", expanded=False):
    g1, g2, g3, g4 = st.columns(4)
    with g1:
        st.info(
            "**1️⃣ 실적 & 가치 안전장치**\n\n"
            "차트 신호가 떠도 적자 기업은 피합니다. **PBR·PER이 적정**하고 "
            "**DART 영업이익이 흑자·성장**인 기업만 선별합니다."
        )
    with g2:
        st.success(
            "**2️⃣ 진짜 큰손(기관·외국인) 수급**\n\n"
            "개인이 띄우는 급등주는 위험합니다. 최근 5거래일간 **기관(연기금 등)과 외국인이 실제 순매수**하는 종목을 확인합니다."
        )
    with g3:
        st.warning(
            "**3️⃣ 갭상승 & 거래대금 신호**\n\n"
            "전일 종가보다 시가가 강하게 출발(갭상승)하고 **거래대금이 터지며 이동평균선 정배열**을 유지할 때 추세가 살아납니다."
        )
    with g4:
        st.error(
            "**4️⃣ 분할 매수 & -10% 손절 원칙**\n\n"
            "신호 화살표가 떠도 한 번에 다 사지 않고 **나눠서 매수**하며, "
            "매수가 대비 **-10% 이탈 시 기계적으로 손절**해 원금을 지킵니다."
        )

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
def run_condition_screener(market="KOSPI", min_amt_uk=300, min_gap_pct=1.0, max_pbr=3.0):
    bday = get_latest_bday()
    try:
        df_ohlcv = stock.get_market_ohlcv(bday, market=market)
        df_fund = stock.get_market_fundamental(bday, market=market)
        
        if df_ohlcv.empty:
            return pd.DataFrame(), bday

        df = df_ohlcv.join(df_fund[['PER', 'PBR', 'DIV']], how='left')
        df['종목명'] = [stock.get_market_ticker_name(t) for t in df.index]
        
        # 전일 종가 역산: 종가 / (1 + 등락률/100)
        df['전일종가'] = df['종가'] / (1 + df['등락률'] / 100)
        df['시가갭(%)'] = ((df['시가'] - df['전일종가']) / df['전일종가']) * 100
        df['거래대금(억)'] = (df['거래대금'] / 100_000_000).round(0)
        
        # 조건 필터링: 거래대금(억) 이상 & 시가 갭상승(%) 이상 & PBR 이하
        cond = (
            (df['거래대금(억)'] >= min_amt_uk) &
            (df['시가갭(%)'] >= min_gap_pct) &
            (df['PBR'].fillna(0) <= max_pbr)
        )
        res = df[cond].sort_values(by='거래대금(억)', ascending=False)
        cols = ['종목명', '종가', '등락률', '시가갭(%)', '거래대금(억)', 'PER', 'PBR', 'DIV']
        res['등락률'] = res['등락률'].round(2)
        res['시가갭(%)'] = res['시가갭(%)'].round(2)
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
        inst_sum = recent_5['기관합계'].sum() / 100_000_000  # 억원
        frgn_sum = recent_5['외국인합계'].sum() / 100_000_000 # 억원
        indiv_sum = recent_5['개인'].sum() / 100_000_000     # 억원
        return {
            "inst": inst_sum,
            "frgn": frgn_sum,
            "indiv": indiv_sum,
            "days": len(recent_5)
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
            (current_year - 1, '11011', '연간결산'),
            (current_year - 1, '11014', '전년 3분기'),
        ]
        for yr, reprt_code, label in reports:
            fs = dart.finstate(ticker, yr, reprt_code=reprt_code)
            if fs is None or fs.empty:
                continue
            fs_sub = fs[fs['fs_div'] == 'CFS']
            if fs_sub.empty:
                fs_sub = fs[fs['fs_div'] == 'OFS']
                
            op_row = fs_sub[fs_sub['account_nm'].str.contains('영업이익', na=False)]
            rev_row = fs_sub[fs_sub['account_nm'].str.contains('매출액|수익\\(매출액\\)', na=False)]
            
            if not op_row.empty:
                def parse_num(val):
                    if pd.isna(val) or str(val).strip() in ['', '-']:
                        return 0
                    return int(str(val).replace(',', '').strip())
                
                curr_op = parse_num(op_row.iloc[0]['thstrm_amount'])
                prev_op = parse_num(op_row.iloc[0]['frmtrm_amount'])
                curr_rev = parse_num(rev_row.iloc[0]['thstrm_amount']) if not rev_row.empty else 0
                prev_rev = parse_num(rev_row.iloc[0]['frmtrm_amount']) if not rev_row.empty else 0
                
                op_diff = curr_op - prev_op
                op_growth = ((curr_op - prev_op) / abs(prev_op) * 100) if prev_op != 0 else 0.0
                rev_growth = ((curr_rev - prev_rev) / abs(prev_rev) * 100) if prev_rev != 0 else 0.0
                
                return {
                    "status": "success",
                    "period": f"{yr}년 {label}",
                    "curr_op": curr_op,
                    "prev_op": prev_op,
                    "op_diff": op_diff,
                    "op_growth": op_growth,
                    "curr_rev": curr_rev,
                    "rev_growth": rev_growth
                }
        return {"status": "no_data", "msg": "최근 공시된 재무제표 데이터를 찾지 못했습니다."}
    except Exception as e:
        return {"status": "error", "msg": f"DART 조회 오류: {e}"}

# ---------------------------------------------------------
# 6. 종목 상세 분석 화면 렌더링 (실적 + 기관/외인 수급 + 신호 화살표 차트)
# ---------------------------------------------------------
def render_stock_analysis(ticker, stock_name, dart_api_key, tab_id="main"):
    st.divider()
    st.subheader(f"🔎 [{stock_name} ({ticker})] 종합 안전 진단")
    
    # [A] 가치 지표 (PER, PBR) & 기관/외국인 수급 ('큰형님 수급' 검증)
    c_left, c_right = st.columns(2)
    
    with c_left:
        st.markdown("#### 💎 1. 가치 평가 (KRX 공식)")
        fund_data, f_date = get_single_fundamental(ticker)
        if fund_data:
            f1, f2, f3 = st.columns(3)
            f1.metric("PER (수익비율)", f"{fund_data.get('PER', 0):.2f} 배")
            f2.metric("PBR (자산비율)", f"{fund_data.get('PBR', 0):.2f} 배")
            f3.metric("배당수익률", f"{fund_data.get('DIV', 0):.2f} %")
        else:
            st.info("가치 지표 데이터 조회 중")

    with c_right:
        st.markdown("#### 🏦 2. 최근 5일 큰손(기관·외국인) 수급")
        supply = get_investor_supply(ticker)
        if supply:
            s1, s2, s3 = st.columns(3)
            s1.metric("기관 순매수", f"{supply['inst']:+,.1f} 억", delta="매수 우위" if supply['inst'] > 0 else "매도 우위")
            s2.metric("외국인 순매수", f"{supply['frgn']:+,.1f} 억", delta="매수 우위" if supply['frgn'] > 0 else "매도 우위")
            s3.metric("개인 순매수", f"{supply['indiv']:+,.1f} 억")
        else:
            st.info("수급 데이터를 불러올 수 없습니다.")

    # [B] OpenDART 최근 실적 검증
    if dart_api_key:
        res = check_dart_performance(dart_api_key, ticker)
        if res["status"] == "success":
            st.markdown(f"#### 📊 3. 최신 공시 실적 점검 ({res['period']} 기준)")
            d1, d2, d3 = st.columns(3)
            d1.metric("영업이익 (당기)", f"{res['curr_op']/100_000_000:,.1f} 억원", delta=f"전년 대비 {res['op_growth']:+.1f}%")
            d2.metric("전년 동기 영업이익", f"{res['prev_op']/100_000_000:,.1f} 억원")
            d3.metric("매출액 (당기)", f"{res['curr_rev']/100_000_000:,.1f} 억원", delta=f"전년 대비 {res['rev_growth']:+.1f}%")
            
            if res['curr_op'] > 0 and res['op_diff'] > 0:
                st.success(f"✅ **실적 합격:** {res['period']} 영업이익이 작년보다 **{res['op_growth']:.1f}% 증가**한 흑자 기업입니다.")
            elif res['curr_op'] > 0:
                st.warning(f"⚠️ **주의:** 흑자이나 전년 대비 영업이익이 감소({res['op_growth']:.1f}%)했습니다.")
            else:
                st.error("❌ **적자 주의:** 영업이익이 적자 상태이므로 추격 매수를 피하세요.")

    # [C] 일봉 차트 + 신호 화살표 (수식관리자 대체)
    end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=250)
    ohlcv = fdr.DataReader(ticker, start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
    
    if ohlcv.empty or len(ohlcv) < 5:
        st.error("차트 데이터를 불러올 수 없습니다.")
        return

    for ma in [5, 20, 60, 120]:
        ohlcv[f'MA{ma}'] = ohlcv['Close'].rolling(window=ma).mean()
        
    # 신호 1: 망치형 지지 캔들
    body = (ohlcv['Close'] - ohlcv['Open']).abs()
    lower_shadow = ohlcv[['Open', 'Close']].min(axis=1) - ohlcv['Low']
    upper_shadow = ohlcv['High'] - ohlcv[['Open', 'Close']].max(axis=1)
    ohlcv['Hammer'] = (lower_shadow >= body * 2) & (upper_shadow <= body * 0.5) & (body > 0)
    
    # 신호 2: 갭상승 + 거래량 급증 양봉 (수급 돌파 화살표)
    ohlcv['Prev_Close'] = ohlcv['Close'].shift(1)
    ohlcv['Vol_MA20'] = ohlcv['Volume'].rolling(window=20).mean()
    ohlcv['Gap_Surge'] = (
        (ohlcv['Open'] >= ohlcv['Prev_Close'] * 1.01) & # 시가 1% 이상 갭상승
        (ohlcv['Close'] > ohlcv['Open']) &              # 양봉 마감
        (ohlcv['Volume'] >= ohlcv['Vol_MA20'] * 1.5)    # 20일 평균 거래량 1.5배 이상
    )

    latest = ohlcv.iloc[-1]
    prev = ohlcv.iloc[-2]
    is_aligned = (latest['MA5'] > latest['MA20'] > latest['MA60'] > latest['MA120'])
    is_golden = (prev['MA5'] <= prev['MA20']) and (latest['MA5'] > latest['MA20'])

    st.markdown("#### 🕯️ 4. 차트 추세 & 매수 신호 진단")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("현재가 (최근 종가)", f"{int(latest['Close']):,} 원")
    col2.metric("이동평균선 배열", "정배열 (상승추세)" if is_aligned else "비정배열")
    col3.metric("단기 추세 신호", "골든크로스 발생!" if is_golden else "특이신호 없음")
    col4.metric("최근 망치형/돌파신호", "포착됨" if (latest['Hammer'] or latest['Gap_Surge']) else "해당 없음")

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=ohlcv.index, open=ohlcv['Open'], high=ohlcv['High'],
        low=ohlcv['Low'], close=ohlcv['Close'], name='일봉',
        increasing_line_color='red', decreasing_line_color='blue'
    ))
    
    colors = {'MA5': 'green', 'MA20': 'orange', 'MA60': 'purple', 'MA120': 'gray'}
    for ma, color in colors.items():
        fig.add_trace(go.Scatter(x=ohlcv.index, y=ohlcv[ma], mode='lines', name=ma, line=dict(color=color, width=1.5)))

    # 망치형 지지 마커
    hammers = ohlcv[ohlcv['Hammer']]
    fig.add_trace(go.Scatter(
        x=hammers.index, y=hammers['Low'] * 0.98,
        mode='markers', name='망치형(바닥지지)',
        marker=dict(symbol='triangle-up', size=10, color='green')
    ))

    # 갭상승+거래량 돌파 화살표 마커 (HTS 수식 신호 대체)
    surges = ohlcv[ohlcv['Gap_Surge']]
    fig.add_trace(go.Scatter(
        x=surges.index, y=surges['Low'] * 0.96,
        mode='markers', name='갭상승+수급돌파 신호',
        marker=dict(symbol='triangle-up', size=13, color='red')
    ))

    fig.update_layout(xaxis_rangeslider_visible=False, height=550, title=f"{stock_name} ({ticker}) 일봉 & 신호 화살표 차트")
    st.plotly_chart(fig, use_container_width=True, key=f"chart_{tab_id}_{ticker}")

# ---------------------------------------------------------
# 사이드바 설정
# ---------------------------------------------------------
try:
    default_dart_key = st.secrets.get("DART_API_KEY", "")
except Exception:
    default_dart_key = ""

with st.sidebar:
    st.header("🏢 OpenDART 실적 연동")
    if default_dart_key:
        dart_api_key = default_dart_key
        st.success("✅ DART 공시 시스템 자동 연결됨")
    else:
        dart_api_key = st.text_input("OpenDART API Key 입력", type="password")

# ---------------------------------------------------------
# 메인 탭 구성
# ---------------------------------------------------------
tab1, tab2 = st.tabs(["🔍 특정 종목 직접 검색 (실적·수급·차트)", "⚡ 조건검색기 (거래대금·시가갭·저평가)"])

with tab1:
    st.markdown("궁금한 종목명을 입력하면 **실적(DART) + 기관/외국인 수급 + 차트 신호 화살표**를 한 번에 보여줍니다.")
    all_tickers = get_all_tickers()
    if not all_tickers.empty:
        all_tickers['Display'] = all_tickers['Name'] + " (" + all_tickers['Code'] + " / " + all_tickers['Market'] + ")"
        options = all_tickers['Display'].tolist()
        default_idx = next((i for i, s in enumerate(options) if s.startswith("삼성전자 (")), 0)
        selected_display = st.selectbox("종목명 또는 종목코드를 입력하세요 (타이핑 검색)", options, index=default_idx)
        if selected_display:
            sel_row = all_tickers[all_tickers['Display'] == selected_display].iloc[0]
            render_stock_analysis(sel_row['Code'], sel_row['Name'], dart_api_key, tab_id="search")

with tab2:
    st.markdown("거래대금이 충분히 터지면서 시초가 갭상승이 나온 종목 중, **고평가 거품(PBR)을 걸러낸 안전한 후보군**입니다.")
    c1, c2, c3, c4 = st.columns(4)
    market_sel = c1.selectbox("시장 선택", ["KOSPI", "KOSDAQ"])
    min_amt = c2.number_input("최소 거래대금 (억 원)", min_value=10, max_value=5000, value=300, step=50)
    min_gap = c3.slider("최소 시초가 갭상승 (%)", -2.0, 10.0, 0.5, 0.5)
    max_pbr_val = c4.slider("최대 PBR 제한 (배)", 0.5, 10.0, 2.5, 0.5)

    df_cond, bday_str = run_condition_screener(market_sel, min_amt, min_gap, max_pbr_val)
    st.subheader(f"📋 검색 결과 (기준일: {bday_str} / 총 {len(df_cond)}종목)")
    st.dataframe(df_cond, use_container_width=True, height=280)

    if not df_cond.empty:
        sel_cond_name = st.selectbox("위 검색 결과에서 정밀 진단할 종목 선택", df_cond['종목명'].tolist())
        if sel_cond_name:
            t_code = df_cond[df_cond['종목명'] == sel_cond_name].index[0]
            render_stock_analysis(t_code, sel_cond_name, dart_api_key, tab_id="screener")