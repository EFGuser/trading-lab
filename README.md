# AI Swing Trading Lab

Autonomous, synthetic-price PAPER trading simulator. It uses a **rules-based optimizer**, not the ChatGPT model, no actual stock market data and no brokerage orders.

- Four simulated competitors each start with $10,000 virtual money.
- The adaptive researcher experiments with trading rules every five fictional trading days, validates parameter changes on separate generated scenarios and logs outcomes.
- Paper orders are subject to virtual fees, slippage and position sizing.
- GitHub Actions runs every six hours UTC (best effort). The GitHub Pages dashboard is a read-only snapshot, not a continuously operating AI.

## One-time setup
1. In **Settings → Actions → General**, enable workflow runs and allow GitHub Actions **Read and write permissions**.
2. In **Settings → Pages**, choose **GitHub Actions** as the build source.
3. In **Actions → Paper Trading Research**, click **Run workflow**. After a successful run, GitHub Pages will provide the dashboard URL.

Schedules can be delayed or skipped. All repository data is public: do not commit private financial details or secrets. No paid model API calls are made. Synthetic performance is not a prediction of actual stock returns.