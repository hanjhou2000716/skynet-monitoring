import React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";

global.IS_REACT_ACT_ENVIRONMENT = true;

describe("Skynet incomplete market data rendering", () => {
  let container;
  let root;
  let originalFetch;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    originalFetch = global.fetch;
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    if (originalFetch === undefined) delete global.fetch;
    else global.fetch = originalFetch;
  });

  it("shows pending confirmation and never turns missing numbers into safe zeros", async () => {
    const payload = {
      status: "degraded",
      schemaVersion: 2,
      lastUpdated: "2026/09/29 15:00:00",
      taiex: null,
      ma200: "",
      daysBelowMa: null,
      vix: "NaN",
      daysVixAbove20: null,
      peak_006208: null,
      asset_006208: null,
      dataQuality: { sources: { taiex: "unavailable", vix: "unavailable", "006208": "unavailable" } },
      service: { status: "ok" },
      calendar: { status: "unavailable" },
      markets: {
        taiwan: { status: "calendar_unverified", reasonCode: "CALENDAR_UNVERIFIED" },
        us: { status: "unavailable", reasonCode: "SOURCE_UNAVAILABLE" },
      },
    };
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => payload });

    await act(async () => {
      root.render(<App />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("資料待確認");
    expect(container.textContent).not.toContain("SYSTEM SAFE");
    expect(container.textContent).toContain("行情來源暫不可用");
    expect(container.textContent).toContain("—");
    expect(container.textContent).toContain("連續跌破天數— 天");
    expect(container.textContent).not.toContain("連續跌破天數0 天");
  });

  it("identifies the lagging Taiwan instrument and both session dates", async () => {
    const payload = {
      status: "degraded", schemaVersion: 2, generatedAt: "2026-10-01T10:22:00+08:00",
      lastUpdated: "2026/10/01 10:22:00", taiex: 47940, ma200: 45000, daysBelowMa: 0,
      vix: 18, daysVixAbove20: 0, peak_006208: 260, asset_006208: 256,
      dataQuality: { sources: { taiex: "ok", vix: "ok", "006208": "stale" } },
      service: { status: "ok", dataStatus: "degraded" }, calendar: { status: "verified" },
      markets: {
        taiwan: { status: "stale", latestSessionDate: "2026-09-29", expectedSessionDate: "2026-09-30", reasonCode: "MARKET_DATA_STALE" },
        us: { status: "fresh", latestSessionDate: "2026-09-30", expectedSessionDate: "2026-09-30" },
      },
      instruments: {
        "^TWII": { status: "fresh", latestSessionDate: "2026-09-30", expectedSessionDate: "2026-09-30", reasonCode: "OK" },
        "006208": { status: "stale", latestSessionDate: "2026-09-29", expectedSessionDate: "2026-09-30", reasonCode: "SOURCE_LAGGING" },
      },
    };
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => payload });

    await act(async () => {
      root.render(<App />);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("資料待確認");
    expect(container.textContent).toContain("006208 實際 2026-09-29，應有 2026-09-30（SOURCE_LAGGING）");
    expect(container.textContent).not.toContain("SYSTEM SAFE");
  });
});
