import os
import json
import requests
import datetime
import math
import re
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 1. 環境變數與金鑰設定
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN")
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS")
WEB_APP_URL = "https://hanjhou2000716.github.io/tgolaf-fin-tracker/"


HISTORY_EXTRA_COLUMNS = ["TW_Stock_Value", "US_Stock_Value", "Cash_Value", "Fund_Value", "NVDA_QQQM_Weight", "NVDA_SPYG_Weight", "NVDA_VOO_Weight"]
ETF_NVDA_WEIGHT_FALLBACKS = {"QQQM": 0.095, "SPYG": 0.075, "VOO": 0.070}


def ensure_history_columns(history_sheet):
    if history_sheet is None:
        return
    header = history_sheet.row_values(1)
    for column in HISTORY_EXTRA_COLUMNS:
        if column not in header:
            history_sheet.update_cell(1, len(header) + 1, column)
            header.append(column)


def upsert_history_snapshot(history_sheet, snapshot_date, values):
    """Keep one end-of-day snapshot per Taiwan calendar date."""
    if history_sheet is None:
        return "skipped"

    snapshot = [snapshot_date, *values]
    rows = history_sheet.get_all_values()
    for row_number in range(len(rows), 1, -1):
        row = rows[row_number - 1]
        if row and str(row[0]).strip()[:10] == snapshot_date:
            history_sheet.update(f"A{row_number}:{chr(64 + len(snapshot))}{row_number}", [snapshot])
            return "updated"

    history_sheet.append_row(snapshot)
    return "created"


def get_etf_nvda_weight(symbol, history_records):
    try:
        holdings = yf.Ticker(symbol).funds_data.top_holdings
        for index, row in holdings.iterrows():
            text = f"{index} {' '.join(str(value) for value in row.values)}".upper()
            if "NVDA" in text or "NVIDIA" in text:
                value = next((float(item) for item in row.values if isinstance(item, (float, int)) and 0 < float(item) < 1), None)
                if value is not None:
                    return value, "official"
    except Exception:
        pass

    field = f"NVDA_{symbol}_Weight"
    for row in reversed(history_records):
        try:
            value = float(str(row.get(field, "")).replace("%", ""))
            if value > 0:
                return value, "history"
        except (TypeError, ValueError):
            pass
    return ETF_NVDA_WEIGHT_FALLBACKS[symbol], "fallback"


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)

# ==========================================
# 2. Google Sheets 動態資產結算核心
# ==========================================
def calculate_current_assets():
    if not GCP_CREDENTIALS_JSON:
        return {}, None
        
    try:
        creds_dict = json.loads(GCP_CREDENTIALS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        
        available_sheets = client.openall()
        sheet = None
        for s in available_sheets:
            if "PRStK" in s.title: sheet = s; break
        if not sheet:
            for s in available_sheets:
                if "Growth" in s.title or "資產" in s.title: sheet = s; break
        if not sheet: raise ValueError("找不到檔案")
            
        data_rows, history_sheet = [], None
        for ws in sheet.worksheets():
            title_clean = ws.title.strip().lower()
            if "history" in title_clean or "歷史" in title_clean or "紀錄" in title_clean:
                history_sheet = ws
            elif "表單" in title_clean or "form" in title_clean or "回覆" in title_clean or "異動" in title_clean:
                rows = ws.get_all_values()
                if len(rows) > 1: data_rows.extend(rows[1:])
                    
        if not data_rows: return {}, history_sheet
            
        def parse_date(row):
            if not row: return datetime.datetime.min
            ts_str = str(row[0]).strip()
            match = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', ts_str)
            if match:
                y, m, d = match.groups()
                try: return datetime.datetime(int(y), int(m), int(d))
                except: pass
            return datetime.datetime.min

        data_rows.sort(key=parse_date)

        inventory = {
            "台股": {}, "美股": {}, "基金": {}, 
            "現金_TWD": {"TWD": 0.0}, "現金_USD": {"USD": 0.0},
            "質押負債": {"Current_Debt": 0.0, "History": []},
            "質押利率": {"Rate": 3.3, "History": []}, "擔保品": {}  
        }
        symbol_overrides = {'6208': '006208', '403A': '00403A', '886': '00886', '895': '00895', '878': '00878', '685L': '00685L'}
        known_symbols = ['6208', '006208', '403A', '00403A', '886', '00886', '895', '00895', '878', '00878', '3455', '8033', '2330', '3665', '685L', '00685L', 'QQQM', 'NVDA', 'SPYG', 'TSM', 'VOO', 'VTI', 'TSLA', 'AAPL', 'QQQ', 'FUND', 'TWD', 'USD', 'CURRENT_DEBT', 'RATE']
        
        for row in data_rows:
            row_date = parse_date(row).date()
            raw_cells = [str(c).strip() for c in row if str(c).strip() != ""]
            if not raw_cells: continue
            
            cells = []
            for c in raw_cells:
                match = re.match(r'^([0-9,.]+)\s*(股|張|萬|元|塊|%)$', c)
                if match:
                    num_part = match.group(1).replace(',', '')
                    cells.append(str(float(num_part) * 10000) if match.group(2) == '萬' else num_part)
                else: cells.append(c)
            
            asset_type, mode, symbol, potential_numbers = "", "", "", []
            for cell in cells:
                c_upper = cell.upper()
                if any(x in cell for x in ["台股", "美股", "基金", "現金", "質押", "負債", "擔保", "利率"]): asset_type = cell
                elif any(x in cell for x in ["買入", "存入", "賣出", "提領", "取代", "覆蓋", "更新"]): mode = cell
                elif c_upper in known_symbols or any(char.isalpha() for char in c_upper):
                    if "/" not in cell and "-" not in cell: symbol = cell
                else:
                    try: float(cell.replace(",", "").replace("$", "")); potential_numbers.append(cell)
                    except: pass
                        
            if not symbol and len(potential_numbers) >= 2: symbol, amount_str = potential_numbers[0], potential_numbers[-1]
            elif len(potential_numbers) >= 1: amount_str = potential_numbers[-1]
            else: amount_str = "0"
                
            if not asset_type: continue
            if not mode: mode = "取代"
            
            if "台" in asset_type and "股" in asset_type: asset_type = "台股"
            elif "美" in asset_type and "股" in asset_type: asset_type = "美股"
            elif "基" in asset_type and "金" in asset_type: asset_type = "基金"
            elif "USD" in asset_type or "美金" in asset_type: asset_type = "現金_USD"
            elif "TWD" in asset_type or "台幣" in asset_type or "現金" in asset_type: asset_type = "現金_TWD"
            elif "利率" in asset_type: asset_type = "質押利率"
            elif "質押" in asset_type or "負債" in asset_type: asset_type = "質押負債"
            elif "擔保" in asset_type: asset_type = "擔保品"
            
            if asset_type not in inventory: continue
            try: amount = float(amount_str.replace(",", "").replace("$", ""))
            except: continue
                
            symbol = symbol_overrides.get(symbol, symbol)
            if asset_type in ["現金_TWD", "現金_USD", "質押負債", "質押利率"] and not symbol:
                symbol = {"現金_TWD": "TWD", "現金_USD": "USD", "質押負債": "Current_Debt", "質押利率": "Rate"}[asset_type]
                
            if not symbol: continue
            if symbol not in inventory[asset_type] and symbol != "History": inventory[asset_type][symbol] = 0.0
                
            if "買入" in mode or "存入" in mode or "+" in mode: inventory[asset_type][symbol] += amount
            elif "賣出" in mode or "提領" in mode or "-" in mode: inventory[asset_type][symbol] -= amount
            elif "取代" in mode or "覆蓋" in mode or "更新" in mode: inventory[asset_type][symbol] = amount

            if asset_type == "質押負債": inventory["質押負債"]["History"].append((row_date, inventory["質押負債"]["Current_Debt"]))
            elif asset_type == "質押利率": inventory["質押利率"]["History"].append((row_date, inventory["質押利率"]["Rate"]))

        return inventory, history_sheet
    except Exception as e:
        print(f"Google Sheet 連線或解析失敗: {e}")
        return {}, None

# ==========================================
# 3. 金融市場報價模組
# ==========================================
def get_usd_twd_rate():
    try: return float(requests.get("https://query1.finance.yahoo.com/v8/finance/chart/TWD=X?interval=1d&range=1d", headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()['chart']['result'][0]['meta']['regularMarketPrice'])
    except:
        try: return yf.Ticker("TWD=X").history(period="1d")['Close'].iloc[-1]
        except: return 32.5

def get_us_stock_price(symbol):
    try: return float(requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d", headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()['chart']['result'][0]['meta']['regularMarketPrice'])
    except:
        try: return yf.Ticker(symbol).history(period="1d")['Close'].iloc[-1]
        except: return 0

def get_tw_stock_price(symbol):
    try: 
        if not FINMIND_TOKEN:
            raise Exception("No FinMind Token")
        start_date = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        return requests.get("https://api.finmindtrade.com/api/v4/data", params={"dataset": "TaiwanStockPrice", "data_id": str(symbol), "start_date": start_date, "token": FINMIND_TOKEN}).json()["data"][-1]["close"]
    except: 
        try:
            return yf.Ticker(f"{symbol}.TW").history(period="1d")['Close'].iloc[-1]
        except:
            return 0

# ==========================================
# 4. 主程序與 HTML (Web App) 生成
# ==========================================
def main():
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today_str = tw_now.strftime("%m-%d")
    display_date = tw_now.strftime("%m/%d")
        
    inventory, history_sheet = calculate_current_assets()
    
    # 為了避免沒有 Google Creds 時崩潰，給定預設值
    history_records = []
    if history_sheet:
        try: history_records = history_sheet.get_all_records()
        except: pass
    etf_nvda_weights = {symbol: get_etf_nvda_weight(symbol, history_records) for symbol in ETF_NVDA_WEIGHT_FALLBACKS}
        
    usd_rate = get_usd_twd_rate()
    tw_stock_value, us_stock_value_usd, tsmc_exposure_twd, price_006208, leveraged_etf_value = 0, 0, 0, 0, 0
    position_values_twd, tw_position_values, us_position_values = {}, {}, {}
    
    # 防止 inventory 為空字典崩潰
    cash_twd = inventory.get("現金_TWD", {}).get("TWD", 0)
    cash_usd = inventory.get("現金_USD", {}).get("USD", 0)
    fund_value = sum(v for k, v in inventory.get("基金", {}).items() if k != "History")

    for symbol, shares in inventory.get("台股", {}).items():
        if symbol == "History" or shares <= 0: continue
        price = get_tw_stock_price(symbol)
        value = price * shares
        tw_stock_value += value 
        position_values_twd[symbol] = value
        tw_position_values[symbol] = value
        if symbol == '2330': tsmc_exposure_twd += (value * 1.0)
        elif symbol == '006208': tsmc_exposure_twd += (value * 0.594); price_006208 = price
        elif symbol == '00685L': tsmc_exposure_twd += (value * 0.728); leveraged_etf_value = value

    if price_006208 <= 0 and inventory.get("擔保品", {}).get("006208", 0) > 0:
        price_006208 = get_tw_stock_price("006208")

    pledged_value, pledged_006208_value = 0, 0
    for symbol, shares in inventory.get("擔保品", {}).items():
        if symbol == "History" or shares <= 0:
            continue
        price = price_006208 if symbol == "006208" and price_006208 > 0 else get_tw_stock_price(symbol)
        value = price * shares
        pledged_value += value
        if symbol == "006208":
            pledged_006208_value += value

    for symbol, shares in inventory.get("美股", {}).items():
        if symbol == "History" or shares <= 0: continue
        value = get_us_stock_price(symbol) * shares
        us_stock_value_usd += value
        position_values_twd[symbol] = value * usd_rate
        us_position_values[symbol] = value * usd_rate
        if symbol == 'TSM': tsmc_exposure_twd += (value * usd_rate * 1.0)

    us_stock_value_twd = us_stock_value_usd * usd_rate
    total_cash_twd = cash_twd + (cash_usd * usd_rate)
    
    debt = inventory.get("質押負債", {}).get("Current_Debt", 0)
    debt_history = inventory.get("質押負債", {}).get("History", [])
    rate_history = inventory.get("質押利率", {}).get("History", [])

    def get_val(hist, target, default):
        val = default
        for d, v in hist:
            if d <= target: val = v
        return val

    loan_start = datetime.date(2026, 6, 10) 
    accumulated_interest = sum(get_val(debt_history, loan_start + datetime.timedelta(days=i), debt_history[0][1] if debt_history else debt) * ((get_val(rate_history, loan_start + datetime.timedelta(days=i), 3.3) / 100) / 365) for i in range(max(0, (tw_now.date() - loan_start).days)))

    total_debt = debt + accumulated_interest
    total_asset = tw_stock_value + us_stock_value_twd + total_cash_twd + fund_value
    net_asset = total_asset - total_debt
    
    invested_assets = tw_stock_value + us_stock_value_twd + fund_value
    effective_leverage = ((invested_assets + leveraged_etf_value) / net_asset) if net_asset > 0 else 0
    half_kelly_limit = 0.08 / (2 * (0.18 ** 2))
    
    debt_ratio = ((total_debt / total_asset) * 100) if total_asset > 0 else 0
    maintenance_ratio = (pledged_value / total_debt) * 100 if total_debt > 0 else 0
    ratio_status = "🟢安全" if maintenance_ratio >= 190 else "🟡注意" if maintenance_ratio >= 150 else "🔴警戒" if maintenance_ratio >= 130 else "🆘危險" if maintenance_ratio > 0 else "✅無借款"

    tw_free_value = max(0, tw_stock_value - total_debt)
    tsmc_pct = (tsmc_exposure_twd / total_asset) * 100 if total_asset > 0 else 0
    nvda_exposure_twd = position_values_twd.get("NVDA", 0) + sum(position_values_twd.get(symbol, 0) * weight for symbol, (weight, _) in etf_nvda_weights.items())
    nvda_pct = (nvda_exposure_twd / total_asset * 100) if total_asset > 0 else 0
    largest_symbol, largest_position_value = max(position_values_twd.items(), key=lambda item: item[1], default=("—", 0))
    largest_position_pct = (largest_position_value / total_asset * 100) if total_asset > 0 else 0
    largest_position_status = "警示" if largest_position_pct >= 35 else "觀察" if largest_position_pct >= 20 else "正常"
    asset_006208_value = position_values_twd.get("006208", 0)
    qqqm_value = position_values_twd.get("QQQM", 0)
    tw_largest_symbol, tw_largest_value = max(tw_position_values.items(), key=lambda item: item[1], default=("—", 0))
    us_largest_symbol, us_largest_value = max(us_position_values.items(), key=lambda item: item[1], default=("—", 0))
    tw_largest_pct = (tw_largest_value / total_asset * 100) if total_asset else 0
    us_largest_pct = (us_largest_value / total_asset * 100) if total_asset else 0

    def stressed_maintenance_ratio(decline):
        stressed_collateral = max(0, pledged_value - (pledged_006208_value * decline))
        return (stressed_collateral / total_debt * 100) if total_debt > 0 else 0

    stress_scenarios = [
        {"label": "006208 下跌 10%", "netImpact": asset_006208_value * -0.10, "netAsset": net_asset - asset_006208_value * 0.10, "maintenance": stressed_maintenance_ratio(0.10)},
        {"label": "006208 下跌 20%", "netImpact": asset_006208_value * -0.20, "netAsset": net_asset - asset_006208_value * 0.20, "maintenance": stressed_maintenance_ratio(0.20)},
        {"label": "QQQM 下跌 10%", "netImpact": qqqm_value * -0.10, "netAsset": net_asset - qqqm_value * 0.10, "maintenance": None},
        {"label": "QQQM 下跌 20%", "netImpact": qqqm_value * -0.20, "netAsset": net_asset - qqqm_value * 0.20, "maintenance": None},
    ]

    yesterday_net = next((float(str(row.get('Net_Asset', 0)).replace(',', '')) for row in reversed(history_records) if float(str(row.get('Net_Asset', 0)).replace(',', '')) > 0 and str(row.get('Date', ''))[-5:] != today_str), 0)
    daily_diff = net_asset - yesterday_net if yesterday_net else 0
    daily_pct = (daily_diff / yesterday_net * 100) if yesterday_net else 0
    sign, emoji = ("+", "📈") if daily_diff >= 0 else ("", "📉")

    progress_pct = (net_asset / 10000000) * 100 if net_asset > 0 else 0
    bar_blocks = max(0, min(10, int(progress_pct / 10)))
    bar_str = "[" + "█" * bar_blocks + "░" * (10 - bar_blocks) + f"] {progress_pct:.1f}%"

    category_values = {"TW_Stock_Value": tw_stock_value, "US_Stock_Value": us_stock_value_twd, "Cash_Value": total_cash_twd, "Fund_Value": fund_value}
    previous_categories = next((row for row in reversed(history_records) if any(str(row.get(key, "")).strip() for key in category_values)), None)
    category_daily_changes = {}
    for key, value in category_values.items():
        try:
            previous = float(str(previous_categories.get(key, "")).replace(",", "")) if previous_categories else None
        except (TypeError, ValueError):
            previous = None
        category_daily_changes[key] = None if previous is None else {"amount": round(value - previous, 2), "percent": round((value - previous) / previous * 100, 2) if previous else 0}

    snapshot_result = "skipped"
    if total_asset > 0:
        try:
            ensure_history_columns(history_sheet)
            snapshot_result = upsert_history_snapshot(
                history_sheet,
                tw_now.strftime("%Y-%m-%d"),
                [round(total_asset, 2), round(net_asset, 2), round(total_debt, 2), round(tsmc_exposure_twd, 2), round(tw_stock_value, 2), round(us_stock_value_twd, 2), round(total_cash_twd, 2), round(fund_value, 2), *[round(etf_nvda_weights[symbol][0], 6) for symbol in ETF_NVDA_WEIGHT_FALLBACKS]],
            )
        except Exception as error:
            print(f"History snapshot update failed: {error}")

    # ==========================================
    # ★ 產生網頁專用即時數據 (data.json) ★
    # ==========================================
    if not os.path.exists('public'):
        os.makedirs('public')
    
    try:
        taiex_val = yf.Ticker("^TWII").history(period="1d")['Close'].iloc[-1]
        ma200_val = yf.Ticker("^TWII").history(period="200d")['Close'].mean()
    except:
        taiex_val, ma200_val = 22000, 20000

    try:
        vix_val = yf.Ticker("^VIX").history(period="1d")['Close'].iloc[-1]
    except:
        vix_val = 16.5
        
    try:
        peak_006208 = yf.Ticker("006208.TW").history(period="6mo")['High'].max()
    except:
        peak_006208 = 249.85
        
    # 如果 006208 沒有被計算到，給予預設或即時報價
    if price_006208 == 0:
        try: price_006208 = get_tw_stock_price('006208')
        except: price_006208 = 249.1

    allocation_items = [
        {"label": "台股", "value": round(tw_stock_value, 2), "color": "#727a6d"},
        {"label": "美股", "value": round(us_stock_value_twd, 2), "color": "#9a9387"},
        {"label": "現金", "value": round(total_cash_twd, 2), "color": "#c8c1b5"},
        {"label": "基金", "value": round(fund_value, 2), "color": "#b58a72"},
    ]
    for item in allocation_items:
        item["percent"] = round((item["value"] / total_asset * 100), 1) if total_asset > 0 else 0

    risk_level = "attention" if ((maintenance_ratio and maintenance_ratio < 150) or largest_position_pct >= 35) else "watch" if (debt_ratio >= 25 or tsmc_pct >= 35 or largest_position_pct >= 20) else "stable"
    risk_summary = {
        "level": risk_level,
        "debtRatio": round(debt_ratio, 1),
        "maintenanceRatio": round(maintenance_ratio, 1),
        "tsmcExposureRatio": round(tsmc_pct, 1),
        "effectiveLeverage": round(effective_leverage, 2),
        "largestPosition": {"symbol": largest_symbol, "value": round(largest_position_value, 2), "percent": round(largest_position_pct, 1), "status": largest_position_status},
        "twLargestPosition": {"symbol": tw_largest_symbol, "percent": round(tw_largest_pct, 1)},
        "usLargestPosition": {"symbol": us_largest_symbol, "percent": round(us_largest_pct, 1)},
        "nvdaExposureRatio": round(nvda_pct, 1),
    }

    data_for_web = {
        "taiex": round(taiex_val, 2),
        "ma200": round(ma200_val, 2),
        "vix": round(vix_val, 2),
        "peak_006208": round(peak_006208, 2),
        "asset_006208": round(price_006208, 2),
        "lastUpdated": tw_now.strftime("%Y/%m/%d %H:%M:%S"),
        "portfolio": {
            "totalAsset": round(total_asset, 2),
            "netAsset": round(net_asset, 2),
            "totalDebt": round(total_debt, 2),
            "allocation": allocation_items,
            "risk": risk_summary,
            "stressTests": stress_scenarios,
            "categoryDailyChanges": category_daily_changes,
            "nvdaExposure": {"value": round(nvda_exposure_twd, 2), "percent": round(nvda_pct, 1), "etfWeights": {symbol: {"weight": round(weight * 100, 2), "source": source} for symbol, (weight, source) in etf_nvda_weights.items()}},
        },
    }

    data_for_web["status"] = "ok" if total_asset > 0 else "degraded"
    data_for_web["generatedAt"] = tw_now.isoformat()
    data_for_web["snapshotResult"] = snapshot_result
    write_json("public/data.json", data_for_web)
    write_json("public/status.json", {
        "status": "ok" if total_asset > 0 else "degraded",
        "generatedAt": tw_now.isoformat(),
        "snapshotResult": snapshot_result,
        "portfolioValueAvailable": total_asset > 0,
    })

    # --- 判斷每日損益，動態生成推播文字 ---
    if daily_diff >= 0:
        msg_body = f"🚀 厲害的阿洲，今天賺了 {int(daily_diff):,} 元 (+{daily_pct:.1f}%)"
    else:
        msg_body = f"💸 可憐的阿洲，今天賠了 {abs(int(daily_diff)):,} 元 ({daily_pct:.1f}%)"

    tg_text = f"✅ {display_date} 結算完畢！\n{msg_body}\n\n@PRStK Lab & SFC.e. All right reserve"

    # --- 傳送 Telegram 訊息 ---
    keyboard = {
        "inline_keyboard": [
            [{"text": "🦎 Growth 儀表板", "web_app": {"url": WEB_APP_URL}}],
            [{"text": "📡 Skynet Monitoring", "web_app": {"url": "https://hanjhou2000716.github.io/skynet-monitoring/"}}]
        ]
    }
    
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": TELEGRAM_CHAT_ID, 
                "text": tg_text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            })
        except: pass

if __name__ == "__main__":
    main()
