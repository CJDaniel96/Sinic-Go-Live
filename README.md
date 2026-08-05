# SINIC Go Live Machine Interface Test

這個工具用來測試機台與 AI 程式之間的檔案式對接流程。程式會持續監看機台輸出目錄，等待時間戳資料夾中的 CSV 與 JPG/JPEG 檔案穩定後，依設定檔挑選測試情境，產生回傳 CSV 或模擬異常狀況，並輸出測試報告。

目前不執行 AI 推論，只測試檔案取得、CSV 回寫、回傳位置、耗時與異常情境。

程式只會處理資料夾名稱符合 `YYYYMMDDHHMMSS`，且日期為執行當天的時間戳資料夾；其他日期或其他名稱的資料夾會略過。常駐執行跨過午夜後，會自動改為處理新日期的資料夾。

監控方式可在 `[watch] mode` 選擇 `poll`（定期掃描）或 `event`（檔案系統事件）。`event` 模式啟動時會先掃描一次，收到新增、搬移、修改或刪除事件時立即檢查，並以 `event_rescan_seconds` 低頻補掃，避免漏接事件。

## 環境

- Python: `3.12`
- Python 環境管理: `uv`
- 第三方套件: 無

## 快速開始

複製範例設定檔：

```bash
cp config.example.toml config.toml
```

修改 `config.toml` 裡的路徑：

```toml
[input]
input_dir = "/path/to/machine/output"
return_dir = "/path/to/machine/return"
```

啟動長時間測試：

```bash
uv run python main.py --config config.toml
```

程式會一直跑，直到你按 `Ctrl+C` 停止。停止時會更新最後一版報告。

## 單次測試

把 `config.toml` 的 `[run] once` 改成 `true`，或用命令列覆寫：

```bash
uv run python main.py \
  --config config.toml \
  --input-dir /path/to/machine/output/20260625160600 \
  --return-dir /path/to/machine/return \
  --once \
  --overwrite \
  --settle-seconds 0
```

## 測試情境

每個時間戳資料夾 ready 後，程式會依 `config.toml` 的 `[[scenario.cases]]` 權重隨機挑一個情境。

| 情境 | 目的 |
| --- | --- |
| `normal_return` | 正常回傳 CSV，確認基本流程 |
| `delayed_return` | 延遲回傳，確認機台 timeout/retry 容忍時間 |
| `no_return` | 故意不回傳，確認機台沒有收到結果時的行為 |
| `empty_csv` | 回傳空檔，確認機台如何處理壞檔 |
| `missing_is_pass_column` | 回傳缺少 `is_pass` 欄位的 CSV |
| `partial_rows` | 只回傳部分資料列，確認機台是否檢查 row 數 |
| `malformed_csv` | 回傳格式錯誤的 CSV |

## is_pass 測試

`[result] mode` 支援：

| 模式 | 行為 |
| --- | --- |
| `fixed` | 所有列都寫入 `fixed_code` |
| `random_row` | 每一列依權重隨機寫入 `22` 或 `23` |
| `random_file` | 每份 CSV 隨機選一個結果，整份 CSV 同值 |

範例：

```toml
[result]
mode = "random_row"
fixed_code = "23"

[result.weights]
"22" = 50
"23" = 50
```

## 報告輸出

預設每次執行會建立：

```text
reports/run_YYYYMMDD_HHMMSS/
```

裡面包含：

- `events.jsonl`：事件流水紀錄，每行一個 JSON，適合追查時間點。
- `summary.csv`：每個時間戳資料夾一列摘要，適合用 Excel 分析。
- `report.md`：人看的測試報告，包含情境統計、狀態統計、耗時統計。

每筆資料會記錄：

- 第一次發現資料夾時間
- 檔案穩定時間
- 開始處理時間
- 預計回傳時間
- 實際開始回傳時間
- 回傳完成時間
- 發現到穩定耗時
- 發現到回傳完成耗時
- 處理耗時
- CSV 數量、JPG/JPEG 數量、rows 數量
- `is_pass=22`、`is_pass=23` 數量
- 回傳檔案路徑

## 常用設定

只測正常回傳，且每列隨機 `22/23`：

```toml
[result]
mode = "random_row"

[[scenario.cases]]
name = "normal_return"
weight = 1
```

測機台最長可接受多久才回傳：

```toml
[[scenario.cases]]
name = "delayed_return"
weight = 1
min_delay_seconds = 30
max_delay_seconds = 300
```

測沒有回傳時機台行為：

```toml
[[scenario.cases]]
name = "no_return"
weight = 1
```

沒有 JPG 也處理 CSV：

```toml
[watch]
allow_no_images = true
```

使用檔案系統事件監聽：

```toml
[watch]
mode = "event"
event_rescan_seconds = 60.0
```

也可以使用命令列參數 `--watch-mode event` 切換。若輸入路徑位於不可靠的網路磁碟，可改用 `poll` 模式。

## 打包成 Windows exe

請在 Windows 環境 build：

```powershell
uv add --dev pyinstaller
uv run pyinstaller --onefile --name sinic-go-live main.py
```

執行：

```powershell
.\dist\sinic-go-live.exe --config config.toml
```

## 注意事項

- 正常回傳與延遲回傳會使用原子寫入，避免機台讀到半寫入檔案。
- 異常情境是刻意產生壞檔或不回傳，請在測試報告中確認該筆資料的 `scenario`。
- 若要重現隨機測試結果，請固定 `[run] random_seed`。
