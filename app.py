import streamlit as st
import pandas as pd
from pykrx import stock
import FinanceDataReader as fdr
import OpenDartReader
import plotly.graph_objects as go
from datetime import datetime, timedelta

st.set_page_config(page_title="주식 아가방 차트 & 저평가 스크리너", layout="wide")
st.title("📈 실적·저평가 종목 스크리너 & 차트 분석기")

# ---------------------------------------------------------
# 0. 대시보드 내장 실전 투자 판단 가이드
# ---------------------------------------------------------
with st.expander("📖 [주식 아가방 & 12시에 만나요] 실전 차트·재무 판단 가이드 (클릭하여 열기)", expanded=False):
    g1, g2, g3, g4 = st.columns(4)
    with g1:
        st.info(
            "**1️⃣ 가치 & 실적 먼저 확인**\n\n"
            "차트보다 숫자가 먼저입니다. **PBR 1.0 이하·저PER**이면서 "
            "DART 분기 보고서상 **영업이익이 전년 대비 증가**한 억울한 저평가주를 찾습니다."
        )
    with g2:
        st.success(
            "**2️⃣ 일봉(캔들)의 의미**\n\n"
            "하루 동안의 매수·매도 전쟁 기록입니다.\n"
            "- 🔴 **양봉:** 시가보다 종가가 상승\n"
            "- 🔵 **음봉:** 시가보다 종가가 하락\n"
            "- **꼬리:** 장중 변동성(몸통보다 중요)"
        )
    with g3:
        st.warning(
            "**3️⃣ 망치형(Hammer) 캔들**\n\n"
            "몸통 아래로 긴 꼬리가 달린 모양(▲)입니다. "
            "장중 크게 밀렸으나 매수 세력이 종가까지 끌어올린 흔적으로, **바닥 지지선 형성** 신호로 봅니다."
        )
    with g4:
        st.error(
            "**4️⃣ 이동평균선 & 대응 원칙**\n\n"
            "- **정배열(5>20>60>120일):** 건강한 상승 추세\n"
            "- **골든크로스:** 단기선이 장기선을 상향 돌파\n"
            "- **대응:** 분할 매수 후 이유 훼손 또는 지수 대비 **-10% 이탈 시 기계적 손절**"
        )

# ---------------------------------------------------------
# 1. 전체 상장 종목 마스터(이름 <-> 코드 매핑용) 불러오기
# ---------------------------------------------------------
@st.cache_data(ttl=86400)
def get_all_tickers():
    try:
        df_krx = fdr.StockListing('KRX')
        # 일반 주식 종목만 필터링 (6자리 코드)
        df_krx = df_krx[df_krx['Code'].str.len() == 6][['Code', 'Name', 'Market']]
        return df_krx
    except Exception:
        return pd.DataFrame(columns=['Code', 'Name', 'Market'])

# ---------------------------------------------------------
# 2. KRX 전종목 저평가 스크리닝 함수
# ---------------------------------------------------------
@st.cache_data(ttl=3600)
def get_undervalued_stocks(market="KOSPI", max_pbr=1.0, max_per=12.0):
    df_fund = pd.DataFrame()
    bday = ""
    
    for i in range(10):
        target_dt = (datetime.today() - timedelta(days=i)).strftime("%Y%m%d")
        try:
            temp_fund = stock.get_market_fundamental(target_dt, market=market)
            if not temp_fund.empty and temp_fund['PER'].sum() > 0:
                df_fund = temp_fund
                bday = target_dt
                break
        except Exception:
            continue

    if not df_fund.empty:
        df_cap = stock.get_market_cap(bday, market=market)
        df = df_fund.join(df_cap[['종가', '거래량', '시가총액']])
        df['종목명'] = [stock.get_market_ticker_name(t) for t in df.index]
        
        cond = (
            (df['PBR'] > 0) & (df['PBR'] <= max_pbr) &
            (df['PER'] > 0) & (df['PER'] <= max_per) &
            (df['거래량'] > 100000)
        )
        filtered = df[cond].sort_values(by='PBR', ascending=True)
        return filtered[['종목명', '종가', 'PER', 'PBR', 'DIV', '거래량', '시가총액']], bday, "KRX"
    else:
        df_krx = fdr.StockListing(market)
        df_krx = df_krx.set_index('Code')
        df_krx = df_krx.rename(columns={'Name': '종목명', 'Close': '종가', 'Volume': '거래량', 'Marcap': '시가총액'})
        filtered = df_krx[df_krx['거래량'] > 100000].sort_values(by='시가총액', ascending=False).head(100)
        return filtered[['종목명', '종가', '거래량', '시가총액']], datetime.today().strftime("%Y%m%d"), "FDR"

# ---------------------------------------------------------
# 3. 개별 종목의 최신 PER·PBR 조회 함수
# ---------------------------------------------------------
@st.cache_data(ttl=3600)
def get_single_fundamental(ticker):
    for i in range(10):
        target_dt = (datetime.today() - timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = stock.get_market_fundamental(target_dt, target_dt, ticker)
            if not df.empty and df['PER'].iloc[0] > 0:
                return df.iloc[0].to_dict(), target_dt
        except Exception:
            continue
    return None, ""

# ---------------------------------------------------------
# 4. OpenDART 분기/결산 영업이익·매출액 성장 여부 검증 함수
# ---------------------------------------------------------
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
# 5. 공통 종목 상세 분석 화면 렌더링 함수 (실적 + 차트)
# ---------------------------------------------------------
def render_stock_analysis(ticker, stock_name, dart_api_key, tab_id="main"):
    st.divider()
    st.subheader(f"🔎 [{stock_name} ({ticker})] 정밀 분석 결과")
    
    # [A] 기본 가치 지표 (PER, PBR, 배당수익률)
    fund_data, f_date = get_single_fundamental(ticker)
    if fund_data:
        st.caption(f"KRX 기준일: {f_date}")
        f1, f2, f3 = st.columns(3)
        f1.metric("PER (주가수익비율)", f"{fund_data.get('PER', 0):.2f} 배")
        f2.metric("PBR (주가순자산비율)", f"{fund_data.get('PBR', 0):.2f} 배")
        f3.metric("배당수익률", f"{fund_data.get('DIV', 0):.2f} %")

    # [B] OpenDART 최근 실적 검증
    if dart_api_key:
        with st.spinner(f"DART에서 {stock_name}의 최신 분기 재무제표를 확인하는 중..."):
            res = check_dart_performance(dart_api_key, ticker)
            
        if res["status"] == "success":
            st.markdown(f"#### 📊 최신 공시 실적 점검 ({res['period']} 기준)")
            d1, d2, d3 = st.columns(3)
            curr_op_uk = res['curr_op'] / 100_000_000
            prev_op_uk = res['prev_op'] / 100_000_000
            curr_rev_uk = res['curr_rev'] / 100_000_000
            
            d1.metric("영업이익 (당기)", f"{curr_op_uk:,.1f} 억원", delta=f"전년 동기 대비 {res['op_growth']:+.1f}%")
            d2.metric("전년 동기 영업이익", f"{prev_op_uk:,.1f} 억원")
            d3.metric("매출액 (당기)", f"{curr_rev_uk:,.1f} 억원", delta=f"전년 동기 대비 {res['rev_growth']:+.1f}%")
            
            if res['curr_op'] > 0 and res['op_diff'] > 0:
                st.success(f"✅ **실적 합격:** {res['period']} 영업이익이 작년보다 **{res['op_growth']:.1f}% 증가**한 흑자 성장 기업입니다.")
            elif res['curr_op'] > 0 and res['op_diff'] <= 0:
                st.warning(f"⚠️ **주의:** 흑자는 유지 중이나 전년 동기 대비 영업이익이 감소({res['op_growth']:.1f}%)했습니다.")
            else:
                st.error("❌ **적자 주의:** 해당 분기 영업이익이 적자 상태입니다.")
        else:
            st.info(res["msg"])
    else:
        st.caption("💡 좌측 사이드바에 OpenDART API Key를 입력하면 최근 분기 영업이익 성장 여부를 자동 검증합니다.")

    # [C] 일봉 차트 및 이동평균선 분석
    end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=250)
    ohlcv = fdr.DataReader(ticker, start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
    
    if ohlcv.empty or len(ohlcv) < 5:
        st.error("차트 데이터를 불러올 수 없습니다.")
        return

    for ma in [5, 20, 60, 120]:
        ohlcv[f'MA{ma}'] = ohlcv['Close'].rolling(window=ma).mean()
        
    body = (ohlcv['Close'] - ohlcv['Open']).abs()
    lower_shadow = ohlcv[['Open', 'Close']].min(axis=1) - ohlcv['Low']
    upper_shadow = ohlcv['High'] - ohlcv[['Open', 'Close']].max(axis=1)
    ohlcv['Hammer'] = (lower_shadow >= body * 2) & (upper_shadow <= body * 0.5) & (body > 0)

    latest = ohlcv.iloc[-1]
    prev = ohlcv.iloc[-2]
    is_aligned = (latest['MA5'] > latest['MA20'] > latest['MA60'] > latest['MA120'])
    is_golden = (prev['MA5'] <= prev['MA20']) and (latest['MA5'] > latest['MA20'])

    st.markdown("#### 🕯️ 차트 기술적 신호 진단")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("현재가 (최근 종가)", f"{int(latest['Close']):,} 원")
    col2.metric("이동평균선 배열", "정배열 (상승추세)" if is_aligned else "비정배열")
    col3.metric("단기 추세 신호", "골든크로스 발생!" if is_golden else "특이신호 없음")
    col4.metric("최근 망치형 캔들", "포착됨 (지지선 확인)" if latest['Hammer'] else "해당 없음")

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=ohlcv.index, open=ohlcv['Open'], high=ohlcv['High'],
        low=ohlcv['Low'], close=ohlcv['Close'], name='일봉',
        increasing_line_color='red', decreasing_line_color='blue'
    ))
    
    colors = {'MA5': 'green', 'MA20': 'orange', 'MA60': 'purple', 'MA120': 'gray'}
    for ma, color in colors.items():
        fig.add_trace(go.Scatter(x=ohlcv.index, y=ohlcv[ma], mode='lines', name=ma, line=dict(color=color, width=1.5)))

    hammers = ohlcv[ohlcv['Hammer']]
    fig.add_trace(go.Scatter(
        x=hammers.index, y=hammers['Low'] * 0.98,
        mode='markers', name='망치형(지지신호)',
        marker=dict(symbol='triangle-up', size=10, color='black')
    ))

    fig.update_layout(xaxis_rangeslider_visible=False, height=550, title=f"{stock_name} ({ticker}) 일봉 & 이동평균선 차트")
    st.plotly_chart(fig, use_container_width=True, key=f"chart_{tab_id}_{ticker}")

# ---------------------------------------------------------
# 사이드바 설정 (Secrets에 키가 있으면 자동 로드)
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
        dart_api_key = st.text_input(
            "OpenDART API Key 입력",
            type="password",
            help="발급받은 40자리 인증키를 입력하세요."
        )

    st.divider()
    st.header("🔍 저평가 스크리너 조건")
    market = st.selectbox("시장 선택", ["KOSPI", "KOSDAQ"])
    max_pbr = st.slider("최대 PBR (배)", 0.2, 3.0, 1.0, 0.1)
    max_per = st.slider("최대 PER (배)", 3.0, 30.0, 12.0, 1.0)

# ---------------------------------------------------------
# 메인 탭 구성: [특정 종목 직접 검색] vs [저평가 종목 리스트]
# ---------------------------------------------------------
tab1, tab2 = st.tabs(["🔍 특정 종목 직접 검색 (전종목)", "📋 저평가 후보 리스트"])

with tab1:
    st.markdown("궁금한 종목명을 입력하거나 선택하면 **실적(DART) + 가치(PER/PBR) + 차트 흐름**을 바로 조회합니다.")
    all_tickers = get_all_tickers()
    
    if not all_tickers.empty:
        # '삼성전자 (005930)' 형태로 선택 옵션 생성 (타이핑 자동완성 지원)
        all_tickers['Display'] = all_tickers['Name'] + " (" + all_tickers['Code'] + " / " + all_tickers['Market'] + ")"
        options = all_tickers['Display'].tolist()
        
        # 기본값으로 삼성전자 찾기
        default_idx = next((i for i, s in enumerate(options) if s.startswith("삼성전자 (")), 0)
        selected_display = st.selectbox("종목명 또는 종목코드를 입력하세요 (타이핑 검색 가능)", options, index=default_idx)
        
        if selected_display:
            sel_row = all_tickers[all_tickers['Display'] == selected_display].iloc[0]
            render_stock_analysis(sel_row['Code'], sel_row['Name'], dart_api_key, tab_id="search")
    else:
        # 마스터 목록 로드 실패 시 직접 6자리 코드 입력
        direct_code = st.text_input("종목코드 6자리를 직접 입력하세요 (예: 005930)", value="005930")
        if len(direct_code.strip()) == 6:
            render_stock_analysis(direct_code.strip(), direct_code.strip(), dart_api_key)

with tab2:
    df_list, base_date, source = get_undervalued_stocks(market, max_pbr, max_per)
    if source == "FDR":
        st.warning("⚠️ 현재 KRX 서버의 PER/PBR 직접 조회가 제한되어 거래량 상위 주요 종목 리스트로 대체 표시됩니다.")

    st.subheader(f"{market} 저평가 후보 리스트 (기준일: {base_date} / 총 {len(df_list)}개)")
    st.dataframe(df_list, use_container_width=True, height=280)

    if not df_list.empty:
        selected_name = st.selectbox("위 리스트에서 분석할 종목 선택", df_list['종목명'].tolist())
        if selected_name:
            ticker = df_list[df_list['종목명'] == selected_name].index[0]
            render_stock_analysis(ticker, selected_name, dart_api_key, tab_id="list")