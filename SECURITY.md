# 安全政策 / Security Policy

## 回報安全問題

請**不要**用公開的 Issue、Pull Request 或 Discussion 回報安全問題。

請使用 GitHub 的 private vulnerability reporting：到本 repo 的 **Security** 分頁，選 **Report a vulnerability**，填寫表單後送出。只有維護者看得到回報內容。

回報時請盡量提供：

- 受影響的版本或 commit。
- 重現步驟，以及你的 Windows、Python 與相關 CLI（`agy`、`claude`、`codex`）版本。
- 你觀察到的影響，例如 Worker 能寫入檔案、讀到核准範圍以外的內容，或未經同意就派工。

請不要在回報中附上真實的 API 金鑰、登入憑證、帳本或私人專案內容；用合成資料重現即可。

## 範圍

zMAHarness 是在同一個 Windows 帳號下執行的本機工具。已知的信任界線與殘餘風險寫在 [DISCLAIMER.md](DISCLAIMER.md) 和 [README](README.md)，例如對話核准由主控寫入、收據雜湊鏈沒有金鑰。由管理員管理的 ChatGPT 工作區帳號（Business、Enterprise、Edu）下，`codex-exec` 的正式執行會載入雲端管理設定與 requirements，派工前的工具探測看不到這份設定，詳見 [Adapter 說明](docs/ADAPTERS.md#codex-exec)。這些已記載的限制不算漏洞；能繞過已記載保護措施的方法（例如執行檔解析、唯讀限制、範圍綁定或登入狀態檢查）則歡迎回報。

本專案由個人維護，沒有固定的回覆時限，會盡力處理。

## Reporting a vulnerability

Please do **not** report security issues in public issues, pull requests, or discussions.

Use GitHub's private vulnerability reporting: open the **Security** tab of this repository, choose **Report a vulnerability**, and submit the form. Only the maintainer can see the report.

Include the affected version or commit, steps to reproduce, your Windows, Python, and CLI (`agy`, `claude`, `codex`) versions, and the impact you observed. Do not attach real API keys, credentials, ledgers, or private project content; reproduce with synthetic data.

The known trust boundaries and residual risks are documented in [DISCLAIMER.md](DISCLAIMER.md) and the [README](README.en.md). For example, with a ChatGPT workspace account managed by an administrator (Business, Enterprise, Edu), a real `codex-exec` run loads cloud-managed settings and requirements that the pre-dispatch tool probe cannot see; see the [adapter documentation](docs/ADAPTERS.md#codex-exec). Those documented limits are not vulnerabilities, but ways to bypass a documented safeguard are welcome reports. This project is maintained by one person on a best-effort basis.
