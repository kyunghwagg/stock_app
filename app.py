import streamlit as st
import pandas as pd
import requests
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

EXCLUDE_KEYWORDS = [
    "선물", "F 20", "F20", "코스피", "코스닥", "지수", "레버리지", "인버스",
    "ETN", "ETF", "스팩", "리츠", "KODEX", "TIGER", "KBSTAR", "RISE",
    "ACE", "SOL", "PLUS", "HANARO", "KOSEF", "ARIRANG", "TIMEFOLIO",
    "WOORI", "BNK", "히어로즈", "파워", "마이티", "FOCUS", "VITA"
]

def is_pure_common_stock(code, name):
    code_str = str(code).strip()
    name_str = str(name).strip()
    if len(code_str) != 6 or not code_str.isdigit() or not code_str.endswith("0"):
        return False
    if any(kw in name_str.upper() for kw in EXCLUDE_KEYWORDS):
        return False
    return True

# 0. [실시간 개선] 코스피 시장 온도계 (네이버 모바일 실시간 지수 + 60일 이평선 비교)
@st.cache_data(ttl=300)
def get_kospi_temperature():
    try:
        # 1) 60일 이동평균선 계산용 과거 일봉
        end_dt = datetime.today()
        start_dt = end_dt - timedelta(days=150)
        df_ks = fdr.DataReader("KS11", start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
        if df_ks.empty or len(df_ks) < 60:
            return None

        # 2) 실시간 현재 지수 (네이버 증권 모바일 API)
        curr = float(df_ks['Close'].iloc[-1])
        chg_ratio = 0.0
        try:
            url = "https://m.stock.naver.com/api/index/KOSPI/basic"
            res = requests.get(url, headers=HEADERS, timeout=4).json()
            live_price = float(str(res.get("closePrice", "0")).replace(",", ""))
            if live_price > 0:
                curr = live_price
            chg_ratio = float(str(res.get("fluctuationsRatio", "0")).replace(",", ""))
        except Exception:
            pass

        # 최근 59일 종가 + 실시간 현재 지수로 60일 평균 계산
        recent_59 = df_ks['Close'].iloc[-59:].tolist()
        ma60 = (sum(recent_59) + curr) / len(recent_59 + [curr])
        diff_pct = (curr - ma60) / ma60 * 100
        return {"curr": curr, "diff_pct": diff_pct, "chg_ratio": chg_ratio}
    except Exception:
        return None

# 1. 전체 상장 종목 마스터 불러오기
@st.cache_data(ttl=86400)
def get_all_tickers():
    df_res = pd.DataFrame()
    try:
        df_krx = fdr.StockListing('KRX')
        if not df_krx.empty and 'Code' in df_krx.columns:
            df_res = df_krx[['Code', 'Name']]
    except Exception:
        pass

    if df_res.empty:
        try:
            df_desc = fdr.StockListing('KRX-DESC')
            if not df_desc.empty and 'Code' in df_desc.columns:
                df_res = df_desc[['Code', 'Name']]
        except Exception:
            pass

    if df_res.empty:
        try:
            rows = []
            for mkt in ["KOSPI", "KOSDAQ"]:
                for t in stock.get_market_ticker_list(market=mkt):
                    rows.append({"Code": t, "Name": stock.get_market_ticker_name(t)})
            if rows:
                df_res = pd.DataFrame(rows)
        except Exception:
            pass

    if df_res.empty:
        df_res = pd.DataFrame(CORE_BLUECHIPS, columns=["Code", "Name"])

    mask = [is_pure_common_stock(c, n) for c, n in zip(df_res['Code'], df_res['Name'])]
    return df_res[mask].reset_index(drop=True)

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

# 6. 네이버 모바일 상승률/시가총액 상위 순수 기업 종목 확보
@st.cache_data(ttl=900)
def get_market_movers(market="KOSPI"):
    movers = []
    seen = set()
    endpoints = [
        f"https://m.stock.naver.com/api/stocks/up/{market}?page=1&pageSize=40",
        f"https://m.stock.naver.com/api/stocks/marketValue/{market}?page=1&pageSize=30"
    ]
    for url in endpoints:
        try:
            res = requests.get(url, headers=HEADERS, timeout=5)
            stocks = res.json().get("stocks", [])
            for s in stocks:
                code = s.get("itemCode", "")
                name = s.get("stockName", "")
                if code not in seen and is_pure_common_stock(code, name):
                    movers.append((code, name))
                    seen.add(code)
        except Exception:
            continue

    if not movers and market == "KOSPI":
        return CORE_BLUECHIPS
    return movers[:25]

# 7. [오늘의 상승주 & 안전 진단] 상승중인 주식 12~15개 나열
@st.cache_data(ttl=900)
def get_rising_stocks_board(dart_api_key, market="KOSPI"):
    bday = get_latest_bday()
    candidates = get_market_movers(market)

    rising_list = []
    fallback_list = []

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

            is_rising_now = (chg_pct > 0) or (curr_close > ma20 and ma5 >= ma20)
            is_up_trend = (curr_close > ma20) and (ma20 >= ma60)

            supply = get_investor_supply(ticker)
            big_money = (supply['inst'] + supply['frgn']) if supply else 0

            fund_data, _ = get_single_fundamental(ticker)
            pbr_val = round(fund_data.get('PBR', 0), 2) if fund_data else 0
            per_val = round(fund_data.get('PER', 0), 1) if fund_data else 0

            dart_res = check_dart_performance(dart_api_key, ticker)
            is_deficit = (dart_res["status"] == "success" and dart_res['curr_op'] <= 0)
            is_earnings_up = (dart_res["status"] == "success" and dart_res['op_diff'] > 0)
            is_profitable = (dart_res["status"] == "success" and dart_res['curr_op'] > 0)
            op_growth = dart_res.get('op_growth', 0) if dart_res["status"] == "success" else 0

            # 가치·성장주 저평가 판정 (PBR 저평가 OR 고성장·저PER)
            is_value_good = (0 < pbr_val <= 1.3) or (op_growth >= 30 and 0 < per_val <= 20)

            if is_deficit:
                status_badge = "🔴 위험(적자)"
                summary = "주가 상승 중이나 영업이익 적자 기업"
                priority = 3
            elif is_earnings_up and is_up_trend and big_money > 0:
                status_badge = "🟢 적극 검토"
                summary = "실적성장 + 큰손매수 + 상승추세 일치"
                priority = 1
            elif (is_earnings_up or is_profitable) and is_value_good and not is_up_trend:
                status_badge = "🔵 분할매수 기회"
                summary = "실적·가치 우량주 (눌림목 반등 시도)"
                priority = 2
            elif is_up_trend and big_money > 0:
                status_badge = "🟢 수급·추세 우수"
                summary = "기관·외국인 매수 유입 + 차트 정배열"
                priority = 1
            else:
                status_badge = "🟡 관망 추천"
                if big_money < 0 and is_earnings_up:
                    summary = "실적 양호·차트 상승이나 큰손 매도 우위"
                elif per_val > 30:
                    summary = f"단기 상승세이나 고평가 주의 (PER {per_val:.0f}배)"
                else:
                    summary = "단기 반등 중이나 큰손 수급·추세 확인 필요"
                priority = 2

            row = {
                "종목코드": ticker,
                "종목명": name,
                "안전판정": status_badge,
                "현재가": f"{curr_close:,}원",
                "등락률": f"{chg_pct:+.1f}%",
                "큰손수급(5일)": f"{big_money:+,.0f}억" if supply else "-",
                "PER/PBR": f"{per_val:.1f} / {pbr_val:.2f}" if (per_val > 0 or pbr_val > 0) else "-",
                "진단요약": summary,
                "_priority": priority,
                "_chg": chg_pct
            }

            if is_rising_now:
                rising_list.append(row)
            else:
                fallback_list.append(row)
        except Exception:
            continue

    rising_list.sort(key=lambda x: (x["_priority"], -x["_chg"]))
    fallback_list.sort(key=lambda x: (x["_priority"], -x["_chg"]))

    combined = rising_list[:15]
    if len(combined) < 12:
        combined.extend(fallback_list[:12 - len(combined)])

    if not combined:
        return pd.DataFrame(), bday

    return pd.DataFrame(combined).drop(columns=["_priority", "_chg"]), bday

# 8. 상세 진단 화면 렌더링 (1년 주가 위치 밴드 + 성장주 PER 평가 반영)
def render_stock_analysis(ticker, stock_name, dart_api_key, tab_id="main"):
    fund_data, _ = get_single_fundamental(ticker)
    supply = get_investor_supply(ticker)
    dart_res = check_dart_performance(dart_api_key, ticker)

    end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=365)  # 1년(52주) 주가 범위 계산을 위해 365일 조회
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

    # 1년(52주) 주가 밴드 위치 계산 (하단 0% ~ 상단 100%)
    year_high = float(ohlcv['High'].max())
    year_low = float(ohlcv['Low'].min())
    curr_price = float(latest['Close'])
    if year_high > year_low:
        band_pct = int(round((curr_price - year_low) / (year_high - year_low) * 100))
    else:
        band_pct = 50
    band_pct = max(0, min(100, band_pct))

    # 차트 렌더링용 최근 180거래일 슬라이싱
    chart_df = ohlcv.tail(180).copy()
    for ma in [5, 20, 60, 120]:
        chart_df[f'MA{ma}'] = ohlcv['Close'].rolling(window=ma).mean().tail(180)

    body = (chart_df['Close'] - chart_df['Open']).abs()
    lower_shadow = chart_df[['Open', 'Close']].min(axis=1) - chart_df['Low']
    upper_shadow = chart_df['High'] - chart_df[['Open', 'Close']].max(axis=1)
    chart_df['Hammer'] = (lower_shadow >= body * 2) & (upper_shadow <= body * 0.5) & (body > 0)

    chart_df['Prev_Close'] = chart_df['Close'].shift(1)
    chart_df['Vol_MA20'] = chart_df['Volume'].rolling(window=20).mean()
    chart_df['Gap_Surge'] = (chart_df['Open'] >= chart_df['Prev_Close'] * 1.01) & (chart_df['Close'] > chart_df['Open']) & (chart_df['Volume'] >= chart_df['Vol_MA20'] * 1.5)

    latest = chart_df.iloc[-1]
    prev = chart_df.iloc[-2]
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
    op_growth = 0.0

    if dart_res["status"] == "success":
        op_growth = dart_res['op_growth']
        if dart_res['curr_op'] <= 0:
            is_deficit = True
            fund_reasons.append(f"❌ 영업이익 적자 ({dart_res['period']})")
        elif dart_res['op_diff'] > 0:
            is_earnings_up = True
            is_profitable = True
            fund_reasons.append(f"✅ 영업이익 전년 대비 {op_growth:+.0f}% 성장 ({dart_res['period']})")
        else:
            is_profitable = True
            fund_reasons.append(f"⚠️ 흑자 유지 중이나 이익 감소 ({op_growth:+.0f}%)")
    else:
        fund_reasons.append("ℹ️ 공시 실적 데이터 미확인")

    pbr_val = fund_data.get('PBR', 0) if fund_data else 0
    per_val = fund_data.get('PER', 0) if fund_data else 0

    # 업종별 가치 판단: PBR 저평가 OR 고성장+합리적 PER 구제
    if 0 < pbr_val <= 1.3:
        is_undervalued = True
        fund_reasons.append(f"✅ 자산 대비 저평가 (PBR {pbr_val:.2f}배)")
    elif op_growth >= 30 and 0 < per_val <= 20:
        is_undervalued = True
        fund_reasons.append(f"✅ 실적 고성장 대비 저PER 우량주 (PER {per_val:.1f}배)")
    elif 1.3 < pbr_val <= 2.5:
        fund_reasons.append(f"🆗 적정 주가 수준 (PBR {pbr_val:.2f}배)")
    elif pbr_val > 2.5:
        fund_reasons.append(f"⚠️ 자산 대비 높은 주가 수준 (PBR {pbr_val:.2f}배)")

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
        st.info("### 🔵 [저평가 분할매수 기회] 실적 튼튼·장세 영향 눌림목\n회사는 흑자를 내는 우량주이나, 시장 분위기로 주가만 쉬어가는 중입니다. (천천히 나눠서 매수 검토)")
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

    # [신규] 1년 주가 위치 밴드 게이지 (적정가 하단~상단 위치 시각화)
    if band_pct <= 25:
        band_msg = f"🟢 **1년 범위 중 하위 {band_pct}% (바닥권·저평가 구간)** — 1년 최저가({int(year_low):,}원) 부근입니다."
    elif band_pct >= 80:
        band_msg = f"🟠 **1년 범위 중 상위 {band_pct}% (고점 부근·단기 과열 주의)** — 1년 최고가({int(year_high):,}원)에 가깝습니다."
    else:
        band_msg = f"🔵 **1년 범위 중 {band_pct}% 위치 (적정 중간 구간)** — 최저 {int(year_low):,}원 ~ 최고 {int(year_high):,}원 사이입니다."
    st.markdown(f"📏 **1년 주가 위치:** {band_msg}")
    st.progress(band_pct / 100)

    # 일봉 + 이동평균선 차트
    fig = go.Figure()
    candle = go.Candlestick(
        x=chart_df.index, open=chart_df['Open'], high=chart_df['High'], low=chart_df['Low'], close=chart_df['Close'],
        name='일봉', increasing_line_color='red', decreasing_line_color='blue'
    )
    fig.add_trace(candle)

    colors = {'MA5': 'green', 'MA20': 'orange', 'MA60': 'purple', 'MA120': 'gray'}
    for ma, color in colors.items():
        line_trace = go.Scatter(x=chart_df.index, y=chart_df[ma], mode='lines', name=ma, line=dict(color=color, width=1.5))
        fig.add_trace(line_trace)

    hammers = chart_df[chart_df['Hammer']]
    hammer_trace = go.Scatter(
        x=hammers.index, y=hammers['Low'] * 0.98, mode='markers',
        name='바닥지지(망치형)', marker=dict(symbol='triangle-up', size=11, color='green')
    )
    fig.add_trace(hammer_trace)

    surges = chart_df[chart_df['Gap_Surge']]
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

# [신규] 상단 코스피 시장 온도계 배너
kospi_temp = get_kospi_temperature()
if kospi_temp:
    diff = kospi_temp['diff_pct']
    curr_ks = kospi_temp['curr']
    chg_r = kospi_temp.get('chg_ratio', 0.0)
    header_txt = f"🌡️ **오늘의 코스피 실시간 지수 ({curr_ks:,.2f}pt, {chg_r:+.2f}%)"
    if diff <= -3.0:
        st.info(f"{header_txt}: [저평가 침체 구간]** — 3개월 평균보다 {abs(diff):.1f}% 낮아 우량주를 천천히 모아가기 좋은 시기입니다.")
    elif diff >= 5.0:
        st.warning(f"{header_txt}: [단기 과열 구간]** — 3개월 평균보다 {diff:.1f}% 높아 추격 매수를 자제하세요.")
    else:
        st.success(f"{header_txt}: [적정 안정 구간]** — 시장이 큰 무리 없이 정상 범위에서 움직이고 있습니다.")

# 메인 화면 탭
tab1, tab2 = st.tabs(["⚡ 오늘의 상승주 & 안전 진단", "🔍 원하는 종목 직접 검색"])

with tab1:
    st.markdown("현재 **상승 흐름을 타는 기업 종목**들을 한눈에 보여줍니다. **아래 종목 버튼을 누르면 상세 진단과 차트가 열립니다.**")
    market_sel = st.radio("시장 선택", ["KOSPI", "KOSDAQ"], horizontal=True)

    with st.spinner("현재 상승 중인 기업들의 실적과 큰손 수급을 진단하는 중입니다..."):
        df_board, bday_str = get_rising_stocks_board(dart_api_key, market_sel)

    st.caption(f"기준일: {bday_str} (선물·ETF·스팩 제외)")

    if df_board.empty:
        st.warning("현재 시장 데이터를 불러오지 못했습니다. 잠시 후 다시 시도하거나 두 번째 탭에서 직접 검색해 주세요.")
    else:
        st.dataframe(
            df_board[['종목명', '안전판정', '현재가', '등락률', '큰손수급(5일)', 'PER/PBR', '진단요약']],
            use_container_width=True,
            hide_index=True,
            height=420
        )

        st.markdown("#### 👆 자세히 볼 주식 이름을 바로 눌러주세요")
        state_key = f"selected_ticker_{market_sel}"
        if state_key not in st.session_state or st.session_state[state_key] not in df_board['종목코드'].values:
            st.session_state[state_key] = df_board.iloc[0]['종목코드']

        cols = st.columns(3)
        for idx, row in df_board.iterrows():
            col = cols[idx % 3]
            is_selected = (st.session_state[state_key] == row['종목코드'])
            btn_label = f"{row['종목명']} ({row['등락률']})\n{row['안전판정']}"
            if col.button(
                btn_label,
                key=f"btn_{market_sel}_{row['종목코드']}",
                use_container_width=True,
                type="primary" if is_selected else "secondary"
            ):
                st.session_state[state_key] = row['종목코드']
                st.rerun()

        sel_row = df_board[df_board['종목코드'] == st.session_state[state_key]].iloc[0]
        render_stock_analysis(sel_row['종목코드'], sel_row['종목명'], dart_api_key, tab_id="rising_board")

with tab2:
    st.markdown("**자주 찾는 대표 종목 버튼**을 누르시거나, 아래 칸에 회사 이름·종목코드를 입력해 주세요.")
    quick_picks = [
        ("005930", "삼성전자"), ("000660", "SK하이닉스"), ("005380", "현대차"),
        ("034020", "두산에너빌리티"), ("012450", "한화에어로스페이스"), ("030530", "원익홀딩스")
    ]
    if "quick_search_code" not in st.session_state:
        st.session_state["quick_search_code"] = ""

    q_cols = st.columns(3)
    for idx, (q_code, q_name) in enumerate(quick_picks):
        if q_cols[idx % 3].button(
            f"📌 {q_name}",
            key=f"qbtn_{q_code}",
            use_container_width=True,
            type="primary" if st.session_state["quick_search_code"] == q_code else "secondary"
        ):
            st.session_state["quick_search_code"] = q_code
            st.rerun()

    st.divider()
    all_tickers = get_all_tickers()
    all_tickers['Display'] = all_tickers['Name'] + " (" + all_tickers['Code'] + ")"
    options = all_tickers['Display'].tolist()
    default_idx = next((i for i, s in enumerate(options) if s.startswith("삼성전자 (")), 0)

    selected_display = st.selectbox("전체 상장사 목록에서 찾기", options, index=default_idx)
    custom_code = st.text_input("또는 6자리 종목코드 직접 입력 (예: 030530)", value="")

    if custom_code.strip() and len(custom_code.strip()) == 6:
        code_input = custom_code.strip()
        matched = all_tickers[all_tickers['Code'] == code_input]
        s_name = matched['Name'].iloc[0] if not matched.empty else code_input
        render_stock_analysis(code_input, s_name, dart_api_key, tab_id="search_custom")
    elif st.session_state["quick_search_code"]:
        q_code = st.session_state["quick_search_code"]
        q_name = dict(quick_picks).get(q_code, q_code)
        render_stock_analysis(q_code, q_name, dart_api_key, tab_id="search_quick")
    elif selected_display:
        sel_row = all_tickers[all_tickers['Display'] == selected_display].iloc[0]
        render_stock_analysis(sel_row['Code'], sel_row['Name'], dart_api_key, tab_id="search")