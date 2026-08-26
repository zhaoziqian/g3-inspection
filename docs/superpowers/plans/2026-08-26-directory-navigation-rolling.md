# Tencent Inspection Directory Rolling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Tencent Docs `目录!M1:O8` a fixed navigation block containing summaries, the latest five paired weeks in oldest-to-newest order, and archives, with verified links to the current Sheet IDs.

**Architecture:** Add one focused directory-navigation module that converts `get_sheet_info` data into an immutable 8×3 text/link plan and applies/verifies that plan through a small client protocol. Both weekly upload and archive workflows call the same module, so upload rolls the directory as soon as a sixth week appears and archive rebuilds it after old Sheet deletion.

**Tech Stack:** Python 3.13 standard library, `unittest`, Tencent Docs `sheet-mcp` (`get_sheet_info`, `get_cell_data`, `set_range_value`, `set_link`).

## Global Constraints

- The only writable directory-navigation range is `目录!M1:O8` (`sheet_id=BB08J2`, zero-based rows 0–7 and columns 12–14).
- Row 1 is the header, row 2 is summary navigation, rows 3–7 are exactly the latest five paired weekly periods, and row 8 is archive navigation.
- Weekly periods are ordered oldest-to-newest from top to bottom.
- Only exact paired names `慢服务MMDD-MMDD` and `慢SQLMMDD-MMDD` participate; `作废`, `汇总`, `归档`, and unpaired Sheets are excluded.
- N2:O8 links are rebuilt from the current Sheet IDs and verified after writing.
- No directory record may be appended at row 9 or below.
- A missing fixed Sheet, fewer than five paired weeks, an unpaired candidate week, or any verification mismatch must fail loudly.
- Preserve all cells and links outside `M1:O8`.

---

### Task 1: Shared navigation planner and verified writer

**Files:**
- Create: `scripts/directory_navigation.py`
- Create: `tests/test_directory_navigation.py`

**Interfaces:**
- Consumes: raw Sheet dictionaries returned by `sheet.get_sheet_info`.
- Produces: `NavigationPlan(rows: tuple[tuple[str, str, str], ...], links: tuple[NavigationLink, ...])`, `build_navigation_plan(sheets, keep_weeks=5)`, and `rebuild_directory_navigation(client, sheets, directory_sheet_id="BB08J2")`.
- Client protocol: `set_values(sheet_id, values)`, `set_link(sheet_id, row, col, url, display_text)`, and `get_cells(sheet_id, start_row, end_row, start_col, end_col)`.

- [ ] **Step 1: Write failing planner tests**

Add literal fixtures for the four fixed target Sheets, six paired weeks, an obsolete Sheet, and a business Sheet. Assert the oldest of six weeks is omitted and rows 3–7 remain oldest-to-newest:

```python
def test_plan_keeps_latest_five_paired_weeks_oldest_to_newest():
    plan = build_navigation_plan(SIX_WEEK_SHEETS)
    assert plan.rows == (
        ("周期", "慢服务扫描报告", "慢SQL扫描报告"),
        ("", "慢服务汇总", "慢SQL汇总"),
        ("0727-0802", "慢服务0727-0802", "慢SQL0727-0802"),
        ("0803-0809", "慢服务0803-0809", "慢SQL0803-0809"),
        ("0810-0816", "慢服务0810-0816", "慢SQL0810-0816"),
        ("0817-0823", "慢服务0817-0823", "慢SQL0817-0823"),
        ("0824-0830", "慢服务0824-0830", "慢SQL0824-0830"),
        ("", "慢服务归档", "慢SQL归档"),
    )
    assert {(link.row, link.col) for link in plan.links} == {
        (row, col) for row in range(1, 8) for col in (13, 14)
    }
```

Also add separate tests that `作废/汇总/归档` are excluded, an unpaired canonical week raises `NavigationError`, fewer than five paired weeks raises, and a fixed target name/ID mismatch raises.

- [ ] **Step 2: Run planner tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_directory_navigation -v
```

Expected: import failure because `scripts.directory_navigation` does not exist.

- [ ] **Step 3: Implement the pure plan types and builder**

Create the constants and types:

```python
DIRECTORY_SHEET_ID = "BB08J2"
TENCENT_FILE_URL = "https://docs.qq.com/sheet/DWHBzb1ZFZWhFREZa"
FIXED_TARGETS = {
    "慢服务汇总": "z776s9",
    "慢SQL汇总": "cczg56",
    "慢服务归档": "7i67j3",
    "慢SQL归档": "8i29ez",
}

@dataclass(frozen=True)
class NavigationLink:
    row: int
    col: int
    display_text: str
    sheet_id: str

@dataclass(frozen=True)
class NavigationPlan:
    rows: tuple[tuple[str, str, str], ...]
    links: tuple[NavigationLink, ...]
```

Use `scan_archive.select_archive_periods()` for canonical paired-week parsing and cross-year ordering. Reject `selection.unpaired`, require `len(selection.keep) == 5`, validate the four fixed IDs, and construct exactly eight rows and fourteen links.

- [ ] **Step 4: Run planner tests and verify GREEN**

Run:

```bash
python3 -m unittest tests.test_directory_navigation -v
```

Expected: all planner tests pass.

- [ ] **Step 5: Write failing writer and verification tests**

Use an in-memory client that records write coordinates and returns structured cells with `hyperlinks`. Assert:

```python
def test_rebuild_writes_only_m1_to_o8_and_verifies_all_links():
    client = NavigationClientDouble()
    rebuild_directory_navigation(client, SIX_WEEK_SHEETS)
    assert {(cell["row"], cell["col"]) for cell in client.values} == {
        (row, col) for row in range(8) for col in range(12, 15)
    }
    assert len(client.links) == 14
    assert all(0 <= row <= 7 and 12 <= col <= 14 for row, col in client.touched)
```

Add a test where one returned hyperlink points to the wrong Sheet ID and assert `NavigationError("目录链接校验失败")`. Cover both API representations: a raw Sheet ID and a full URL containing `?tab=<sheet_id>`.

- [ ] **Step 6: Run writer tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_directory_navigation -v
```

Expected: failure because `rebuild_directory_navigation` is missing.

- [ ] **Step 7: Implement bounded write and complete read-back verification**

Implement `rebuild_directory_navigation()` to:

1. Build all 24 text cells in memory with zero-based coordinates rows 0–7, columns 12–14.
2. Call one `set_values` operation for the bounded matrix.
3. Call `set_link` for the fourteen non-header navigation cells using `f"{TENCENT_FILE_URL}?tab={sheet_id}"`.
4. Read back exactly rows 0–7 and columns 12–14.
5. Compare every text value, link coordinate, display text, and normalized link target.

Do not add fallback writes outside the fixed range.

- [ ] **Step 8: Run shared-module tests and commit**

Run:

```bash
python3 -m unittest tests.test_directory_navigation -v
git diff --check
```

Expected: all tests pass and `git diff --check` is silent.

Commit:

```bash
git add scripts/directory_navigation.py tests/test_directory_navigation.py
git commit -m "feat: add fixed directory navigation"
```

---

### Task 2: Roll directory during weekly upload

**Files:**
- Modify: `scripts/tencent_upload_scan_reports.py:25-33,130-180,294-325`
- Replace tests: `tests/test_tencent_upload_scan_reports.py`

**Interfaces:**
- Consumes: `rebuild_directory_navigation(client, sheets)` from Task 1.
- Produces: `UploadDirectoryClient`, an adapter over the existing `sheetengine()` boundary.

- [ ] **Step 1: Replace append-row tests with a failing upload integration test**

Remove tests for `choose_directory_row()` and `find_dir_row()`. Add an adapter/workflow test with six paired weeks after new Sheet creation:

```python
@patch("scripts.tencent_upload_scan_reports.rebuild_directory_navigation")
@patch("scripts.tencent_upload_scan_reports.sheetengine")
def test_upload_rebuilds_fixed_directory_after_both_week_sheets_exist(sheetengine, rebuild):
    fixed = [
        {"sheet_id": "BB08J2", "sheet_name": "目录"},
        {"sheet_id": "z776s9", "sheet_name": "慢服务汇总"},
        {"sheet_id": "cczg56", "sheet_name": "慢SQL汇总"},
        {"sheet_id": "7i67j3", "sheet_name": "慢服务归档"},
        {"sheet_id": "8i29ez", "sheet_name": "慢SQL归档"},
    ]
    periods = ("0720-0726", "0727-0802", "0803-0809", "0810-0816", "0817-0823", "0824-0830")
    sheets = fixed + [
        {"sheet_id": f"{kind}-{period}", "sheet_name": f"{prefix}{period}"}
        for period in periods
        for kind, prefix in (("svc", "慢服务"), ("sql", "慢SQL"))
    ]
    sheetengine.return_value = {"sheets": sheets}
    refresh_directory_after_upload()
    rebuild.assert_called_once()
    assert rebuild.call_args.args[1] == sheets
```

Add adapter tests proving `set_values`, `set_link`, and structured `get_cells` use only `BB08J2` and preserve the requested coordinates.

- [ ] **Step 2: Run upload tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_tencent_upload_scan_reports -v
```

Expected: failure because `refresh_directory_after_upload` and `UploadDirectoryClient` do not exist.

- [ ] **Step 3: Implement the upload adapter and replace Step 3**

Add:

```python
class UploadDirectoryClient:
    def set_values(self, sheet_id, values):
        write_values(sheet_id, values)

    def set_link(self, sheet_id, row, col, url, display_text):
        sheetengine("set_link", {
            "file_id": TENCENT_FILE_ID,
            "sheet_id": sheet_id,
            "row": row,
            "col": col,
            "url": url,
            "display_text": display_text,
        })

    def get_cells(self, sheet_id, start_row, end_row, start_col, end_col):
        response = sheetengine("get_cell_data", {
            "file_id": TENCENT_FILE_ID,
            "sheet_id": sheet_id,
            "start_row": start_row,
            "end_row": end_row,
            "start_col": start_col,
            "end_col": end_col,
        })
        return response.get("cells", [])

def refresh_directory_after_upload():
    info = sheetengine("get_sheet_info", {"file_id": TENCENT_FILE_ID})
    rebuild_directory_navigation(UploadDirectoryClient(), info["sheets"])
```

Delete `choose_directory_row()` and `find_dir_row()`. Replace the old per-period append/overwrite prompt at Step 3 with one call to `refresh_directory_after_upload()` after both Sheet IDs exist. The refresh must occur even when the current week Sheets already existed and their data overwrite was skipped.

- [ ] **Step 4: Run upload and shared tests**

Run:

```bash
python3 -m unittest tests.test_tencent_upload_scan_reports tests.test_directory_navigation -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit upload integration**

```bash
git add scripts/tencent_upload_scan_reports.py tests/test_tencent_upload_scan_reports.py
git commit -m "feat: roll directory after weekly upload"
```

---

### Task 3: Rebuild directory after archive deletion

**Files:**
- Modify: `scripts/tencent_archive_scan_reports.py:53-66,381-445,475-579,598-647,689-721`
- Modify: `tests/test_tencent_archive_scan_reports.py:311-429,431-505`

**Interfaces:**
- Consumes: `rebuild_directory_navigation(client, sheets)` from Task 1.
- Extends: `TencentArchiveClient.get_cells(...)` and `TencentArchiveClient.set_link(...)` to satisfy the shared client protocol.

- [ ] **Step 1: Write failing archive sequencing test**

Replace the old row-clearing test with a pipeline test that records events:

```python
@patch("scripts.tencent_archive_scan_reports.prepare_archive")
@patch("scripts.tencent_archive_scan_reports.apply_archive_sheet")
@patch("scripts.tencent_archive_scan_reports.verify_archive_pair")
@patch("scripts.tencent_archive_scan_reports.delete_verified_sources")
@patch("scripts.tencent_archive_scan_reports.rebuild_directory_navigation")
def test_archive_rebuilds_directory_only_after_verified_source_deletion(
    rebuild, delete_sources, verify, apply_sheet, prepare
):
    events = []
    final_sheets = [
        {"sheet_id": "BB08J2", "sheet_name": "目录", "row_count": 8, "col_count": 26},
        {"sheet_id": "svc-a", "sheet_name": "慢服务归档", "row_count": 2, "col_count": 12},
        {"sheet_id": "sql-a", "sheet_name": "慢SQL归档", "row_count": 2, "col_count": 12},
    ]

    class Client:
        def sheet_info(self):
            events.append("sheet_info")
            return final_sheets

    prepare.return_value = SimpleNamespace(
        report={},
        periods=("0518-0525",),
        target_metas={
            "slow_service": SheetMeta("svc-a", "慢服务归档", 2, 12),
            "slow_sql": SheetMeta("sql-a", "慢SQL归档", 2, 12),
        },
        current_rows={"slow_service": [], "slow_sql": []},
        sources={"slow_service": {"0518-0525": []}, "slow_sql": {"0518-0525": []}},
        source_metas={"slow_service": {}, "slow_sql": {}},
    )
    apply_sheet.side_effect = [[], []]
    verify.side_effect = lambda *args: events.append("verify_archives")
    delete_sources.side_effect = lambda *args, **kwargs: events.append("delete_sources") or []
    rebuild.side_effect = lambda *args: events.append("rebuild_directory")

    client = Client()
    run_archive(client, apply=True)

    assert events.index("verify_archives") < events.index("delete_sources")
    assert events.index("delete_sources") < events.index("rebuild_directory")
    rebuild.assert_called_once_with(client, final_sheets)
```

Keep the existing test that archive verification failure prevents deletion, and assert `rebuild_directory_navigation` is also not called on that failure.

- [ ] **Step 2: Run archive test and verify RED**

Run:

```bash
python3 -m unittest tests.test_tencent_archive_scan_reports -v
```

Expected: failure because the archive pipeline still calls `clear_directory_periods` instead of the shared rebuild.

- [ ] **Step 3: Replace historical directory cleanup with final rebuild**

After `delete_verified_sources()` confirms the old Sheet IDs are absent:

```python
final_sheets = client.sheet_info()
rebuild_directory_navigation(client, final_sheets)
report["directory_rebuilt"] = True
```

Remove `_directory_period_rows`, `read_directory_sentinels`, `clear_directory_periods`, and the `directory_rows`/`directory_sentinels` fields from `ArchivePreparation`. Dry-run should report `directory_preview` with the five periods selected by the shared navigation plan, not historical rows to clear.

- [ ] **Step 4: Add archive-client protocol methods**

Implement:

```python
def get_cells(self, sheet_id, start_row, end_row, start_col, end_col):
    return self.call("get_cell_data", {
        "sheet_id": sheet_id,
        "start_row": start_row,
        "end_row": end_row,
        "start_col": start_col,
        "end_col": end_col,
    }).get("cells", [])

def set_link(self, sheet_id, row, col, url, display_text):
    self.call("set_link", {
        "sheet_id": sheet_id,
        "row": row,
        "col": col,
        "url": url,
        "display_text": display_text,
    })
```

- [ ] **Step 5: Run archive and shared tests**

Run:

```bash
python3 -m unittest tests.test_tencent_archive_scan_reports tests.test_directory_navigation -v
```

Expected: all tests pass; deletion remains gated by full dual-archive verification.

- [ ] **Step 6: Commit archive integration**

```bash
git add scripts/tencent_archive_scan_reports.py tests/test_tencent_archive_scan_reports.py
git commit -m "feat: rebuild directory after archive"
```

---

### Task 4: Skill contract, full verification, and live no-op reconciliation

**Files:**
- Modify: `SKILL.md:378-388,517-558`
- Modify: `README.md:165-200`

**Interfaces:**
- Documents the upload/archive behavior implemented by Tasks 1–3.

- [ ] **Step 1: Update skill documentation**

Replace “write to the next empty directory row” with the fixed contract:

```text
M1:O8 = header + summaries + latest five paired weeks (oldest-to-newest) + archives.
Upload rebuilds the block after both current-week Sheets exist.
Archive rebuilds and verifies the block after old Sheet deletion.
Never append directory navigation at row 9 or below.
```

Document that N2:O8 links are rebuilt from current Sheet IDs and that any incomplete pair or read-back mismatch stops the workflow.

- [ ] **Step 2: Run the complete automated suite**

Run:

```bash
python3 -m unittest discover -s tests -v
git diff --check
```

Expected: all tests pass with zero failures; `git diff --check` is silent.

- [ ] **Step 3: Run a live reconciliation against the current workbook**

Invoke the shared directory refresh through the upload adapter without creating or changing weekly Sheets:

```bash
python3 -c 'from scripts.tencent_upload_scan_reports import refresh_directory_after_upload; refresh_directory_after_upload()'
```

Then independently call `sheet.get_cell_data` for `BB08J2` rows 0–7, columns 12–14 without CSV mode. Verify:

- exactly the expected 24 cells are present;
- rows 3–7 are `0720-0726` through `0817-0823`, oldest-to-newest for the current workbook state;
- N2:O8 contain fourteen links targeting `z776s9`, `cczg56`, the ten current weekly Sheet IDs, `7i67j3`, and `8i29ez`;
- no write was made outside `M1:O8`.

- [ ] **Step 4: Commit documentation and final verification checkpoint**

```bash
git add SKILL.md README.md
git commit -m "docs: define fixed directory rolling"
git status --short
```

Expected: clean working tree after generated `__pycache__` files are removed safely.
