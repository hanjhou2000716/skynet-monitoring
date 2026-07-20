import React, { useState, useEffect } from "react";
import {
  AlertTriangle,
  TrendingDown,
  Activity,
  ShieldAlert,
  Crosshair,
  BarChart2,
  Info,
  RefreshCw,
  CheckCircle2,
} from "lucide-react";

const App = () => {
  const [isLoaded, setIsLoaded] = useState(false);
  const [isFetching, setIsFetching] = useState(false);

  // --- 狀態變數 (從真實數據載入，保留使用者微調能力) ---
  const [taiex, setTaiex] = useState(0);
  const [ma200, setMa200] = useState(0);
  const [daysBelowMa, setDaysBelowMa] = useState(0);

  const [vix, setVix] = useState(0);
  const [daysVixAbove20, setDaysVixAbove20] = useState(0);

  const [peak006208, setPeak006208] = useState(0);
  const [current006208, setCurrent006208] = useState(0);
  const [lastUpdated, setLastUpdated] = useState("");

  const fetchLatestData = () => {
    setIsFetching(true);
    // 讀取 Python 自動產生的真實數據檔案
    fetch('./data.json')
      .then(res => res.json())
      .then(json => {
        setTaiex(json.taiex);
        setMa200(json.ma200);
        setVix(json.vix);
        setPeak006208(json.peak_006208);
        setCurrent006208(json.asset_006208);
        setLastUpdated(json.lastUpdated);
        setIsLoaded(true);
        setIsFetching(false);
      })
      .catch(err => {
        console.error("讀取數據失敗:", err);
        setIsFetching(false);
      });
  };

  useEffect(() => {
    fetchLatestData();
  }, []);

  useEffect(() => {
    if (taiex >= ma200) setDaysBelowMa(0);
  }, [taiex, ma200]);

  useEffect(() => {
    if (vix <= 20) setDaysVixAbove20(0);
  }, [vix]);

  useEffect(() => {
    if (current006208 > peak006208) setPeak006208(current006208);
  }, [current006208, peak006208]);

  if (!isLoaded) {
    return (
      <div className="min-h-screen bg-slate-950 flex flex-col items-center justify-center text-slate-300 font-sans space-y-4">
        <Activity className="w-12 h-12 text-cyan-500 animate-pulse" />
        <h2 className="text-xl font-bold tracking-wider uppercase">Loading Skynet Core...</h2>
        <p className="text-sm text-slate-500 animate-pulse">正在透過 Python 節點獲取即時市況...</p>
      </div>
    );
  }

  const isTaiexBelowMA = taiex < ma200;
  const isTaiexTriggered = isTaiexBelowMA && daysBelowMa >= 3;

  const isVixHigh = vix > 20;
  const isVixTriggered = isVixHigh && daysVixAbove20 >= 2;

  const isProtocolTriggered = isTaiexTriggered || isVixTriggered;

  const drawdownPercent = (((current006208 - peak006208) / peak006208) * 100).toFixed(2);
  const isOpportunityTriggered = parseFloat(drawdownPercent) <= -8.0;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-200 p-4 md:p-8 font-sans">
      <div className="max-w-6xl mx-auto space-y-6">
        {/* Header & Sync Status */}
        <header className="border-b border-slate-800 pb-4 mb-8 flex flex-col md:flex-row justify-between items-start md:items-end gap-4">
          <div className="flex items-center space-x-3">
            <Activity className="text-cyan-500 w-8 h-8" />
            <div>
              <h1 className="text-2xl md:text-3xl font-bold text-white tracking-wider uppercase">
                Skynet Monitoring
              </h1>
              <p className="text-sm text-slate-400 mt-1">
                天眼監控與無情退場系統 (S10 DEVTPS 模型核心)
              </p>
            </div>
          </div>
          <div className="flex items-center space-x-4 bg-slate-900/50 p-2 md:p-3 rounded-lg border border-slate-800">
            <div className="flex flex-col text-right">
              <span className="text-xs text-slate-500">
                REAL MARKET DATA
              </span>
              <span className="text-sm font-mono text-slate-300">
                Data Sync: {lastUpdated}
              </span>
            </div>
            <button
              onClick={fetchLatestData}
              disabled={isFetching}
              className="p-2 bg-slate-800 hover:bg-slate-700 rounded-md transition-colors disabled:opacity-50"
              title="強制刷新市況"
            >
              {isFetching ? (
                <RefreshCw className="w-5 h-5 text-cyan-400 animate-spin" />
              ) : (
                <CheckCircle2 className="w-5 h-5 text-emerald-400" />
              )}
            </button>
          </div>
        </header>

        {/* Top Banner: Protocol Status */}
        <div
          className={`p-6 rounded-xl border-2 transition-all duration-500 flex items-center justify-between ${
            isProtocolTriggered
              ? "bg-red-950/50 border-red-500 shadow-[0_0_30px_rgba(239,68,68,0.3)]"
              : "bg-emerald-950/30 border-emerald-500/50"
          }`}
        >
          <div className="flex items-center space-x-4">
            {isProtocolTriggered ? (
              <ShieldAlert className="w-12 h-12 text-red-500 animate-pulse" />
            ) : (
              <ShieldAlert className="w-12 h-12 text-emerald-500" />
            )}
            <div>
              <h2
                className={`text-2xl font-bold tracking-wide ${
                  isProtocolTriggered ? "text-red-400" : "text-emerald-400"
                }`}
              >
                {isProtocolTriggered
                  ? "PROTOCOL ACTIVATED: 無情退場協議啟動"
                  : "SYSTEM SAFE: 監控系統安全"}
              </h2>
              <p className="text-slate-300 mt-1">
                {isProtocolTriggered
                  ? "⚠️ 警告：已觸發系統性風險指標！請立即啟動降槓桿程序，清算衛星部位 (00685L)，回歸 100% 核心原型資產。"
                  : "✅ 目前市場雜訊在容許範圍內，維持現有槓桿配置，持續享有逆價差與曝險增益。"}
              </p>
            </div>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Left Column: Indicators */}
          <div className="lg:col-span-2 space-y-6">
            {/* Indicator 1: TAIEX vs 200MA */}
            <div
              className={`p-5 rounded-xl border ${
                isTaiexTriggered
                  ? "bg-red-950/40 border-red-500/50"
                  : "bg-slate-900 border-slate-800"
              }`}
            >
              <div className="flex justify-between items-start mb-4">
                <div className="flex items-center space-x-2">
                  <BarChart2 className="w-5 h-5 text-indigo-400" />
                  <h3 className="text-lg font-semibold text-white">
                    宏觀趨勢：加權指數 vs 200MA
                  </h3>
                </div>
                <span
                  className={`px-3 py-1 rounded-full text-xs font-bold ${
                    isTaiexTriggered
                      ? "bg-red-500/20 text-red-400"
                      : "bg-slate-800 text-slate-400"
                  }`}
                >
                  條件 1
                </span>
              </div>
              <div className="grid grid-cols-3 gap-4 mb-4">
                <div className="bg-slate-950 p-3 rounded-lg">
                  <div className="text-xs text-slate-500 mb-1">
                    目前加權指數
                  </div>
                  <div
                    className={`text-xl font-mono ${
                      isTaiexBelowMA ? "text-orange-400" : "text-emerald-400"
                    }`}
                  >
                    {taiex.toLocaleString()}
                  </div>
                </div>
                <div className="bg-slate-950 p-3 rounded-lg">
                  <div className="text-xs text-slate-500 mb-1">
                    200日均線 (200MA)
                  </div>
                  <div className="text-xl font-mono text-indigo-400">
                    {ma200.toLocaleString()}
                  </div>
                </div>
                <div className="bg-slate-950 p-3 rounded-lg">
                  <div className="text-xs text-slate-500 mb-1">
                    連續跌破天數
                  </div>
                  <div
                    className={`text-xl font-mono ${
                      daysBelowMa >= 3
                        ? "text-red-500"
                        : daysBelowMa > 0
                        ? "text-orange-400"
                        : "text-slate-300"
                    }`}
                  >
                    {daysBelowMa} 天
                  </div>
                </div>
              </div>
              {isTaiexTriggered && (
                <div className="flex items-center space-x-2 text-sm text-red-400 bg-red-950/50 p-2 rounded">
                  <AlertTriangle className="w-4 h-4" />
                  <span>
                    觸發：指數實體跌破 200MA 且連續 3 個交易日未收復。
                  </span>
                </div>
              )}
            </div>

            {/* Indicator 2: VIX */}
            <div
              className={`p-5 rounded-xl border ${
                isVixTriggered
                  ? "bg-red-950/40 border-red-500/50"
                  : "bg-slate-900 border-slate-800"
              }`}
            >
              <div className="flex justify-between items-start mb-4">
                <div className="flex items-center space-x-2">
                  <TrendingDown className="w-5 h-5 text-purple-400" />
                  <h3 className="text-lg font-semibold text-white">
                    市場情緒：CBOE VIX 指數
                  </h3>
                </div>
                <span
                  className={`px-3 py-1 rounded-full text-xs font-bold ${
                    isVixTriggered
                      ? "bg-red-500/20 text-red-400"
                      : "bg-slate-800 text-slate-400"
                  }`}
                >
                  條件 2
                </span>
              </div>
              <div className="grid grid-cols-2 gap-4 mb-4">
                <div className="bg-slate-950 p-3 rounded-lg">
                  <div className="text-xs text-slate-500 mb-1">
                    VIX 目前數值
                  </div>
                  <div
                    className={`text-xl font-mono ${
                      vix > 20 ? "text-orange-400" : "text-emerald-400"
                    }`}
                  >
                    {vix.toFixed(2)}
                  </div>
                </div>
                <div className="bg-slate-950 p-3 rounded-lg">
                  <div className="text-xs text-slate-500 mb-1">
                    連續大於 20 天數
                  </div>
                  <div
                    className={`text-xl font-mono ${
                      daysVixAbove20 >= 2
                        ? "text-red-500"
                        : daysVixAbove20 > 0
                        ? "text-orange-400"
                        : "text-slate-300"
                    }`}
                  >
                    {daysVixAbove20} 天
                  </div>
                </div>
              </div>
              {isVixTriggered && (
                <div className="flex items-center space-x-2 text-sm text-red-400 bg-red-950/50 p-2 rounded">
                  <AlertTriangle className="w-4 h-4" />
                  <span>
                    觸發：VIX 指數異常飆升，並連續 2 個交易日收盤大於 20。
                  </span>
                </div>
              )}
            </div>

            {/* Indicator 3: Opportunistic Leverage */}
            <div
              className={`p-5 rounded-xl border ${
                isOpportunityTriggered
                  ? "bg-cyan-950/40 border-cyan-500/50 shadow-[0_0_15px_rgba(6,182,212,0.15)]"
                  : "bg-slate-900 border-slate-800"
              }`}
            >
              <div className="flex justify-between items-start mb-4">
                <div className="flex items-center space-x-2">
                  <Crosshair className="w-5 h-5 text-cyan-400" />
                  <h3 className="text-lg font-semibold text-white">
                    機會型槓桿指標：核心回撤監控
                  </h3>
                </div>
                <span
                  className={`px-3 py-1 rounded-full text-xs font-bold ${
                    isOpportunityTriggered
                      ? "bg-cyan-500/20 text-cyan-400 animate-pulse"
                      : "bg-slate-800 text-slate-400"
                  }`}
                >
                  進階疊加
                </span>
              </div>
              <div className="grid grid-cols-3 gap-4 mb-4">
                <div className="bg-slate-950 p-3 rounded-lg">
                  <div className="text-xs text-slate-500 mb-1">
                    006208 波段高點
                  </div>
                  <div className="text-xl font-mono text-slate-300">
                    {peak006208.toFixed(2)}
                  </div>
                </div>
                <div className="bg-slate-950 p-3 rounded-lg">
                  <div className="text-xs text-slate-500 mb-1">目前價格</div>
                  <div className="text-xl font-mono text-slate-300">
                    {current006208.toFixed(2)}
                  </div>
                </div>
                <div className="bg-slate-950 p-3 rounded-lg">
                  <div className="text-xs text-slate-500 mb-1">
                    技術性回撤幅度
                  </div>
                  <div
                    className={`text-xl font-mono ${
                      isOpportunityTriggered
                        ? "text-cyan-400 font-bold"
                        : "text-slate-400"
                    }`}
                  >
                    {drawdownPercent}%
                  </div>
                </div>
              </div>
              {isOpportunityTriggered && (
                <div className="flex items-center space-x-2 text-sm text-cyan-400 bg-cyan-950/50 p-2 rounded border border-cyan-800/50">
                  <Info className="w-4 h-4" />
                  <span>
                    多頭回檔達 -8%：此為啟動或增加正二 (00685L)
                    槓桿部位之絕佳節點！
                  </span>
                </div>
              )}
            </div>
          </div>

          {/* Right Column: Simulation Controls (The "Sand table") */}
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 h-fit">
            <h3 className="text-lg font-bold text-white mb-6 border-b border-slate-700 pb-2 flex items-center">
              <Activity className="w-4 h-4 mr-2" />
              動態市場參數模擬器
            </h3>

            {/* TAIEX Controls */}
            <div className="space-y-4 mb-6">
              <h4 className="text-sm font-semibold text-indigo-400 flex items-center justify-between">
                <span>大盤指數設定</span>
                {isFetching && (
                  <RefreshCw className="w-3 h-3 animate-spin text-slate-500" />
                )}
              </h4>
              <div>
                <label className="flex justify-between text-xs text-slate-400 mb-1">
                  <span>加權指數 (TAIEX)</span>
                  <span className="text-slate-300 transition-colors">
                    {taiex}
                  </span>
                </label>
                <input
                  type="range"
                  min="15000"
                  max="35000"
                  step="10"
                  value={taiex}
                  onChange={(e) => setTaiex(Number(e.target.value))}
                  className="w-full accent-indigo-500"
                />
              </div>
              <div>
                <label className="flex justify-between text-xs text-slate-400 mb-1">
                  <span>200日均線 (200MA)</span>
                  <span>{ma200}</span>
                </label>
                <input
                  type="range"
                  min="15000"
                  max="35000"
                  step="100"
                  value={ma200}
                  onChange={(e) => setMa200(Number(e.target.value))}
                  className="w-full accent-indigo-500"
                />
              </div>
              <div
                className={`${
                  isTaiexBelowMA
                    ? "opacity-100"
                    : "opacity-40 pointer-events-none"
                }`}
              >
                <label className="flex justify-between text-xs text-slate-400 mb-1">
                  <span>實體跌破 200MA 天數</span>
                  <span>{daysBelowMa} 天</span>
                </label>
                <input
                  type="range"
                  min="0"
                  max="5"
                  step="1"
                  value={daysBelowMa}
                  onChange={(e) => setDaysBelowMa(Number(e.target.value))}
                  className="w-full accent-red-500"
                />
              </div>
            </div>

            {/* VIX Controls */}
            <div className="space-y-4 mb-6 pt-4 border-t border-slate-800">
              <h4 className="text-sm font-semibold text-purple-400 flex items-center justify-between">
                <span>VIX 情緒設定</span>
              </h4>
              <div>
                <label className="flex justify-between text-xs text-slate-400 mb-1">
                  <span>CBOE VIX 數值</span>
                  <span className="text-slate-300 transition-colors">
                    {vix.toFixed(2)}
                  </span>
                </label>
                <input
                  type="range"
                  min="10"
                  max="40"
                  step="0.1"
                  value={vix}
                  onChange={(e) => setVix(Number(e.target.value))}
                  className="w-full accent-purple-500"
                />
              </div>
              <div
                className={`${
                  isVixHigh ? "opacity-100" : "opacity-40 pointer-events-none"
                }`}
              >
                <label className="flex justify-between text-xs text-slate-400 mb-1">
                  <span>連續大於 20 天數</span>
                  <span>{daysVixAbove20} 天</span>
                </label>
                <input
                  type="range"
                  min="0"
                  max="5"
                  step="1"
                  value={daysVixAbove20}
                  onChange={(e) => setDaysVixAbove20(Number(e.target.value))}
                  className="w-full accent-red-500"
                />
              </div>
            </div>

            {/* 006208 Controls */}
            <div className="space-y-4 pt-4 border-t border-slate-800">
              <h4 className="text-sm font-semibold text-cyan-400 flex items-center justify-between">
                <span>機會型加碼設定 (006208)</span>
              </h4>
              <div>
                <label className="flex justify-between text-xs text-slate-400 mb-1">
                  <span>前波歷史高點</span>
                  <span>{peak006208.toFixed(2)}</span>
                </label>
                <input
                  type="range"
                  min="50"
                  max="150"
                  step="0.5"
                  value={peak006208}
                  onChange={(e) => setPeak006208(Number(e.target.value))}
                  className="w-full accent-cyan-500"
                />
              </div>
              <div>
                <label className="flex justify-between text-xs text-slate-400 mb-1">
                  <span>目前現股市價</span>
                  <span className="text-slate-300 transition-colors">
                    {current006208.toFixed(2)}
                  </span>
                </label>
                <input
                  type="range"
                  min="50"
                  max="150"
                  step="0.5"
                  value={current006208}
                  onChange={(e) => setCurrent006208(Number(e.target.value))}
                  className="w-full accent-cyan-500"
                />
              </div>
            </div>

            <div className="mt-6 p-4 bg-slate-950 rounded-lg text-xs text-slate-400 leading-relaxed border border-slate-800">
              <p className="font-bold text-slate-300 mb-1">⚠️ 紀律提示：</p>
              將主觀情緒徹底剝離交易決策。當系統亮起紅燈，請無視市場上的任何利多消息，無情執行降槓桿協議。
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default App;
