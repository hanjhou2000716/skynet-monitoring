import React, { useState, useEffect } from "react";
import { Activity, RefreshCw, CheckCircle2, ShieldAlert, BarChart2, TrendingDown, Crosshair, AlertTriangle, Info } from "lucide-react";

const App = () => {
  const [data, setData] = useState(null);

  useEffect(() => {
    // 從我們 Python 產生的 data.json 讀取資料
    fetch('./data.json')
      .then(res => res.json())
      .then(json => setData(json))
      .catch(err => console.error("讀取數據失敗:", err));
  }, []);

  if (!data) return <div className="p-10 text-white">Loading System Data...</div>;

  // 使用 API 數據渲染 (將原本寫死的 state 替換為 data.xxx)
  return (
    <div className="min-h-screen bg-slate-950 text-slate-200 p-4 font-sans">
      <div className="max-w-6xl mx-auto space-y-6">
        <header className="border-b border-slate-800 pb-4 mb-8 flex justify-between items-center">
          <div className="flex items-center space-x-3">
            <Activity className="text-cyan-500 w-8 h-8" />
            <h1 className="text-2xl font-bold text-white tracking-wider uppercase">Skynet Monitoring</h1>
          </div>
          <div className="text-sm font-mono text-slate-300">Last Sync: {data.lastUpdated}</div>
        </header>

        {/* 這裡填入您原本的卡片 UI，把原本的 taiex 改成 data.taiex，以此類推 */}
        <div className="bg-slate-900 p-5 rounded-lg">
           <h3 className="text-lg font-semibold text-white">目前加權指數</h3>
           <div className="text-2xl text-emerald-400">{data.taiex}</div>
        </div>
        
        {/* ...其餘 UI 結構保持不變，將變數替換為 data.xxx ... */}
      </div>
    </div>
  );
};

export default App;
