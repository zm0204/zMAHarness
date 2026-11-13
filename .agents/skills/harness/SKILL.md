---
name: harness
description: 當任務符合 T1～T5 且協作有明確收益時，用 zMAHarness 提議唯讀分析，依使用者的對話決定派工、獨立驗收並繼續原任務。
---

# zMAHarness 協作

主控 Agent（日常預設 Codex Sol High）平常直接完成原任務。一般工作不預載 Harness，也不為了提議協作主動查額度。
只有符合下列條件且有實際收益，才提出一次協作建議。門檻沿用本機 TOML 的 `[triggers]`。

| 觸發 | 可觀察的理由 |
| --- | --- |
| T1 | 同一錯誤經不同修正達 `t1_failed_fix_rounds` 輪仍失敗 |
| T2 | 變更觸及 `t2_risky_paths` 且差異行數達 `t2_risky_diff_lines` |
| T3 | 預計讀取達 `t3_read_files` 或 `t3_read_kb`，預期產出很小 |
| T4 | 既有 Codex 額度快照達 `t4_codex_used_percent`，且有低成本驗收方法。缺快照時不用 T4 |
| T5 | 使用者主動要求協作，或呼叫 `$harness`（Codex）/`/harness`（Claude Code） |

## 對話內提議

卡片與回覆一律使用使用者提問的語言。`zMAHarness`、模型 ID 與指令名（`$harness`、`/harness`、`mah ...`）保留原文。
下方標題與選項為中文範本，翻譯時須保留語意與順序。中文環境使用「【zMAHarness 協作建議】」，
其他語言使用對應翻譯，例如英文「[zMAHarness Collaboration Proposal]」。

使用主控的原生互動提問工具（Codex Desktop 原生提問卡；Claude Code 用 AskUserQuestion）
送出**單一問題**，把資訊、三個選項與「或自行輸入回覆」放在同一頁，一步完成，不拆題、不翻頁。
問題文字第一行放依語言規則顯示的卡片標題，空一行後，每項資訊獨立一行，格式為「【欄位名稱】內容」。
問題文字一律使用純文字，禁止 Markdown 與「｜」分隔符號。

- 【觸發原因】例如「【觸發原因】T5 · 使用者主動要求」。
- 【建議模型】顯示主控 Agent 依本次任務類型、觸發原因及難度選定的模型與 effort。
- 【任務與範圍】顯示 Review、Second Opinion 或 Research，以及 Context-Bundle 或 Disposable Worktree。
  列出實際涵蓋的檔案名稱，必要時用相對路徑區分同名檔。若 Worker 可查閱工作樹內的關聯檔案，也要明列可查閱範圍。
- 【授權限制】顯示 `僅 1 次 · 唯讀（自動等待模型完成）`。
- 【為什麼建議協作？】用一至兩句白話說明預期收益及選擇該模型的原因。

依提問工具實際提供的欄位排版。下方選項範本定義各選項的意思，依工具能力分配到相應欄位。

- **有 `preview`**（例如 Claude Code 的 AskUserQuestion）。`header` 填 `zMAHarness`。
  問題文字只放標題、空行、【觸發原因】和【任務與範圍】。範圍摘要在一行列出任務類型、檔案數與模式。
  選項用短 `label`。每個協作選項的 `description` 用一行列出模型 · effort、任務、授權。
  每個協作選項的 `preview` 使用 Markdown，以 `## 建議方案` 或 `## 備選方案` 開頭，
  依序列出粗體欄位「模型」、「任務」、「模式」、「範圍」、「授權」。模型 ID 與 effort 用行內程式碼，
  範圍以條列顯示實際檔案與可查閱的關聯範圍。最後以 `### 為什麼？` 加一至兩句說明該方案。
  「不同意」的 `preview` 以 Markdown 條列四項：不派工、不呼叫外部模型、立即繼續原任務、同一任務不再追問。
- **有 `header`、`description`，無 `preview`**（例如 Codex Plan 模式的 `request_user_input`）。
  `header` 填 `zMAHarness`，問題文字放完整五項，每項一行。選項用 `label` 加 `description`，
  協作選項在說明中列出該選項的模型 · effort、任務、授權。
- **只有問題文字與選項字串**（例如 Codex 一般模式的 `request_user_input_async`）。
  問題文字放完整五項，每項一行。每個協作選項的字串都須寫出模型 ID 和 effort，
  例如「繼續協作（建議：gemini-4-argon-medium · medium）」。不得只寫「繼續協作（建議）」或「繼續協作（備選）」。

依下表選擇模型。T4、T5 也依實際任務的複雜度與所需視角判斷。

| 任務 | 建議模型與 effort |
| --- | --- |
| T3 大量資料整理／研究，或小型常規檢查 | `gemini-3.8-flash-low · low`。需要整合較多線索時用 `gemini-3.8-flash-medium · medium`。 |
| T2 核心邏輯／跨模組高風險 Review | `gemini-4-argon-medium · medium`。跨模組交互複雜時用 `gemini-4-argon-high · high`。 |
| T1 連續修復失敗的 Second Opinion，或需要異質視角深度推理 | `claude-sonnet-5-5-high · high`、`claude-opus-5-5-high · high` 或 `gemini-4-argon-high · high`，依所需視角與深度選擇。 |

T1 第二意見或需要異質視角時，優先選擇與主控 Agent 不同家族的模型。主控 Agent 是 Claude 時，
這類任務的選項 2 建議 `gemini-4-argon-high · high`，選項 3 沿用備選規則。使用者手打指定時不受此限。

依檔案大小及可讀範圍在出卡前決定模式，並把模式及範圍列在卡上。

- **Context-Bundle**：只用於 1～2 個核心檔案，且完整檔案加上任務指示封裝後，總大小不超過本機 TOML 的 `limits.max_task_bytes`（預設 32 KB）。
- **Disposable Worktree**：超過 2 個檔案、單檔太大、封裝後超過上限，或需查閱關聯檔案時使用。卡片須列出 Worker 可查閱的範圍。
- 嚴禁為了符合上限而截斷或刪減檔案。

核准後嚴禁自動從 Context-Bundle 改成可讀範圍更大的 Disposable Worktree。需要擴大範圍時，重新提議並等待回覆。

選項 2 顯示與卡片相同的建議模型。若選項 2 是 Argon 或 Claude，選項 3 用
`gemini-3.8-flash-low · low` 供節省額度。若選項 2 是 Flash Low 或 Flash Medium，
選項 3 用 `gemini-4-argon-high · high` 供一鍵升級。

1. `不同意（由主控 Agent 自己繼續完成）`
2. `繼續協作（主控 Agent 建議：<本次建議模型 · effort>）`
3. `繼續協作（改用備選：<本次備選模型 · effort>）`

**T1～T5 都必須暫停等待。** 原生卡片一送出，立即結束或暫停本輪，等待使用者對該卡片
點選或手打回覆。初始的 T5 請求或 `$harness`／`/harness` 呼叫只觸發提議。即使初始請求已指定模型與
檔案，也不得在顯示卡片的同一輪執行 `card create`、`grant approve` 或 `dispatch`。
只有收到該卡片的回覆後，才能在下一輪依該回覆進行授權與派工。原生互動工具不可用時，
回報限制並由主控 Agent 本地繼續原任務。Card ID、雜湊、Grant、工作目錄及證據檔留在背景。

- **不同意**　主控 Agent 立即在本地繼續原任務，同一問題不再提議或追問。若已有待核准卡片，
  在收到回覆後以 `mah card reject <card_id>` 記錄拒絕。寫入失敗時仍繼續原任務。
- **點選繼續協作**　採用選項標明的模型與 effort，維持卡片上的任務、範圍和授權限制。
- **手打覆寫**　使用者可在自由輸入框指定任一可用模型、effort 或縮小檔案範圍，例如
  「換 4argon」、「用 medium」或「只看 io.py」。明確覆寫視為同意協作，其餘欄位沿用卡片。
  依本機 `agy models` 支援的模型 ID 對齊尾綴與 `--effort`，再用最終組合與範圍建立單次 Card 與 Grant。

| 模型 | `low` | `medium` | `high` |
| --- | --- | --- | --- |
| Gemini 3.8 Flash | `gemini-3.8-flash-low` | `gemini-3.8-flash-medium` | `gemini-3.8-flash-high` |
| Gemini 4 Argon | `gemini-4-argon-low` | `gemini-4-argon-medium` | `gemini-4-argon-high` |
| Claude Sonnet 5.5 | `claude-sonnet-5-5-low` | `claude-sonnet-5-5-medium` | `claude-sonnet-5-5-high` |
| Claude Opus 5.5 | `claude-opus-5-5-low` | `claude-opus-5-5-medium` | `claude-opus-5-5-high` |

只輸入 `argon` 或 `4argon` 時，使用 `gemini-4-argon-high` 搭配 `--effort high`。
只指定 effort 時保留卡片上的模型系列，改用相符尾綴。若同時指定 Argon 與 `medium`，
使用 `gemini-4-argon-medium` 搭配 `--effort medium`。

若別名、effort 或檔名有歧義，先用單頁卡片澄清再等待回覆。若原模式無法維持縮小後的範圍，
先提出符合新範圍的模式並等待核准。選定模型不可用時回報具體原因，由主控 Agent 繼續本地工作。

## 輕量主控

只有自然觸發 T1、T2 或 T5 時，才直接讀取 `%LOCALAPPDATA%\zMAHarness\harness.local.toml` 的
`[controller]`。缺少檔案或欄位時，`light_models` 預設 `[]`，`suggested_controller` 預設
`Codex Sol High`。只有從執行環境確認自己的模型 ID 且與 `light_models` 其中一項完全相符，
才算輕量主控。無法確認 ID 時一律視為非輕量。禁止模型自行評估能力。

輕量主控沿用「對話內提議」的語言、單頁、手打覆寫及強制暫停規則，並依提問工具欄位選用同樣的排版。
五項資訊以選項 3 的協作內容為準。有 `preview` 時，問題文字用摘要，選項 3 的 `preview` 用建議方案格式呈現完整資訊。
有 `description` 時，切換主控選項用短標籤，說明列出建議主控與在介面切換後回覆的步驟。
只支援選項字串時，切換主控保留建議主控與回覆步驟，協作選項寫出模型 ID、effort 與任務。
三個選項改為：

1. `不同意（目前主控自己繼續）`
2. `切換主控（建議：<suggested_controller>；請你在介面切換後回覆）`
3. 依情境填入推薦表中的強模型與 effort。
   - 仍在規劃、尚未開始實作，且本次要審查的是實作計畫時，使用 `繼續協作：用 <強模型 · effort> Review 目前計畫`。
     使用既有 `review` 模板，把實作計畫寫進 `task.md`。Context-Bundle 將計畫放入任務指示，
     隨 `bundle create` 封裝。相關原始檔沿用核准的 Context-Bundle／Worktree 規則。
   - 其他情況沿用原觸發對應的任務類型與範圍，只更換模型與 effort。例如 T1 卡關時使用
     `繼續協作：用 <強模型 · effort> 做 Second Opinion`。T5 指定 Research 或 Review 時沿用指定任務。

選項 1 立即繼續原任務，同一任務不再追問。選項 2 由使用者在介面切換後回覆，再繼續原任務。

`--via codex_prompt` 代表「主控對話內核准」，適用任何主控。
收到使用者同意協作的回覆後，才讀取 [references/dispatch.md](references/dispatch.md) 並照著執行。
拒絕或只選切換主控時不讀取。
