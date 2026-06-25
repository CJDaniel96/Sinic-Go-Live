# SINIC Go Live Machine Interface Test

這個測試程式用來確認與機台的檔案式對接流程：

1. 監看機台輸出根目錄下的時間戳資料夾。
2. 等待資料夾中的 CSV 與 JPG/JPEG 影像檔寫入完成並穩定。
3. 讀取 CSV，將每一列的 `is_pass` 欄位改成 `23`。
4. 將處理後的 CSV 原子寫入指定回傳目錄，避免機台讀到半寫入檔案。

目前不會執行 AI 推論，固定回傳 `23` (`AING`)。

## 環境

Python 版本：`3.12`

使用 uv 執行：

```bash
uv run python main.py --input-dir /path/to/machine/output --return-dir /path/to/machine/return
```

## 常用指令

持續監看機台輸出根目錄：

```bash
uv run python main.py \
  --input-dir /path/to/machine/output \
  --return-dir /path/to/machine/return
```

只處理目前已存在的資料夾，處理完就結束：

```bash
uv run python main.py \
  --input-dir /path/to/machine/output \
  --return-dir /path/to/machine/return \
  --once
```

如果要直接測試單一時間戳資料夾，也可以把 `--input-dir` 指到該資料夾：

```bash
uv run python main.py \
  --input-dir /path/to/machine/output/20260625160600 \
  --return-dir /path/to/machine/return \
  --once
```

## 參數

- `--input-dir`：機台輸出根目錄，通常底下會出現時間戳資料夾。
- `--return-dir`：處理後 CSV 要回傳給機台的目錄。
- `--result-code`：寫入 `is_pass` 的值，預設是 `23`。
- `--poll-interval`：監看輪詢秒數，預設 `1.0`。
- `--settle-seconds`：檔案大小穩定多久才開始處理，預設 `2.0`。
- `--ready-timeout`：資料夾長時間缺 CSV 或 JPG 時的警告/失敗時間，預設 `300` 秒。
- `--once`：只處理目前資料後結束。
- `--overwrite`：回傳 CSV 已存在時仍覆寫。
- `--allow-no-images`：沒有 JPG/JPEG 時也允許處理 CSV。
- `--preserve-folder`：回傳到 `return-dir/<時間戳資料夾>/CSV檔名`，預設是直接放在 `return-dir`。

## 注意事項

- CSV 必須包含 `is_pass` 欄位，否則程式會報錯。
- 程式會保留原本 CSV 欄位順序，並盡量保留原本編碼與換行格式。
- 寫回檔案時會先寫暫存檔，再用原子替換方式放到回傳目錄。
