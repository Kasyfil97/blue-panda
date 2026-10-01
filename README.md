# MAGE Metadata Generation Workflow

## Overview

Complete workflow untuk fetch, process, dan enhance metadata table dari *** API, dengan AI-powered column description generation.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    MAGE Metadata Pipeline                        │
└─────────────────────────────────────────────────────────────────┘

┌──────────────────────┐
│   API Metadata       │
│   (MAGE API)     │
└──────────┬───────────┘
           │
           │ fetch_table_metadata.py
           │ atau
           │ batch_fetch_metadata.py
           ▼
┌─────────────────────────────────────────┐
│   metadata_output/                      │
│   ├── table1.json                       │
│   ├── table2.json                       │
│   └── ... (353 files)                   │
└──────────┬──────────────────────────────┘
           │
           │ generate_metadata.py
           │ atau
           │ batch_generate_metadata_parallel.py
           ▼
┌──────────────────────────────────────┐
│   generated_metadata_results/        │
│   ├── table1.json (enhanced)        │
│   ├── table2.json (enhanced)        │
│   ├── SUMMARY.json                  │
│   └── ... (353 files)               │
└──────────┬─────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│   Data Catalog / Documentation       │
│   System Integration                 │
└──────────────────────────────────────┘
```

## Files & Scripts

### 1. Fetch Metadata from API

#### **fetch_table_metadata.py** - Single Table Fetch
```bash
python fetch_table_metadata.py asrs_fact_savingmaster --out table_metadata.json
```

**Features:**
- Fetch single table metadata dari API
- Reformat ke format generate_metadata.py
- Handle berbagai struktur API response

**Output:** Single JSON file siap untuk generate_metadata.py

---

#### **batch_fetch_metadata.py** - Batch Fetch
```bash
python batch_fetch_metadata.py list_of_etutor_used_tables.txt --output metadata_output
```

**Features:**
- Fetch multiple tables dalam batch
- Error handling & retry logic
- Progress tracking
- Generate SUMMARY.json

**Output:** 
- `metadata_output/` folder dengan 353+ JSON files
- `metadata_output/SUMMARY.json` dengan statistik

---

### 2. Generate Enhanced Metadata

#### **generate_metadata.py** - Single Table Generation
```bash
# Basic
python generate_metadata.py metadata_output/asrs_fact_savingmaster.json

# With options
python generate_metadata.py metadata_output/asrs_fact_savingmaster.json \
  --out result.json \
  --business-title \
  --quiet \
  --force
```

**Options:**
- `--out FILE` - Output JSON file
- `--business-title` - Generate column business titles
- `--quiet` - Suppress verbose output
- `--force` - Regenerate even if descriptions exist
- `--kata` - Enable KATA evidence (needs KATA service)
- `--confluence` - Enable Confluence fallback (needs access)

**Output:**
```json
{
  "TableName": "asrs_fact_savingmaster",
  "TableDescription": "Generated description",
  "Columns": [
    {
      "ColumnName": "acctno",
      "ColumnDescription": "Enhanced description (AI or from evidence)",
      "ColumnDataType": "decimal",
      "Knowledge": [...],  // Evidence from search
      "ColumnBusinessTitle": "User-friendly name"  // if enabled
    }
  ],
  "GenerationSummary": {
    "total_columns": 63,
    "failed_columns": 0,
    "outcomes": [
      {
        "column_name": "acctno",
        "resolution": "skipped|exact_match|bm25_*|llm|..."
      }
    ]
  }
}
```

---

#### **batch_generate_metadata_parallel.py** - Batch Processing (RECOMMENDED)
```bash
# Default (4 workers)
python batch_generate_metadata_parallel.py \
  --input metadata_output \
  --output generated_metadata_results \
  --quiet

# With more workers
python batch_generate_metadata_parallel.py \
  --input metadata_output \
  --output generated_metadata_results \
  --workers 8 \
  --quiet \
  --business-title
```

**Parameters:**
- `--input DIR` - Input folder (default: metadata_output)
- `--output DIR` - Output folder (default: generated_metadata_results)
- `--workers N` - Number of parallel workers (default: 4)
- `--quiet` - Suppress verbose output per-file
- `--business-title` - Generate business titles

**Output:**
- `generated_metadata_results/` dengan 353+ enhanced JSON files
- `generated_metadata_results/SUMMARY.json` dengan:
  - Total/successful/failed counts
  - Elapsed time
  - Per-file results

**Performance:**
- Single worker: ~5-10s per file × 353 = 30-60 minutes
- 4 workers: ~15-20 minutes (parallel)
- 8 workers: ~10-15 minutes (parallel)

---

#### **batch_generate_metadata.ps1** - PowerShell Batch Script
```powershell
.\batch_generate_metadata.ps1 `
  -OutputDir generated_metadata_results `
  -Quiet `
  -BusinessTitle
```

**Features:**
- Sequential processing
- Progress display per-file
- Summary report

---

## Resolver Chain

Setiap kolom di-resolve melalui 6-stage chain:

1. **Exact Match** - Direct lookup di AS400/Confluence
2. **BM25 Full-Text** - Search di AS400 + Confluence
3. **KATA Evidence** - Database lookup (if available)
4. **BM25 Informatica** - Informatica search
5. **Confluence Fallback** - Confluence-only search
6. **LLM Generation** - AI-powered description if all fail

### Resolution Status Codes

| Status | Meaning |
|--------|---------|
| `skipped` | Column sudah punya deskripsi bagus, skip processing |
| `exact_match` | Exact match ditemukan (highest confidence) |
| `bm25_as4` | BM25 search di AS400 database |
| `bm25_confluence` | BM25 search di Confluence |
| `confluence` | Confidence-only lookup |
| `kata_*` | KATA database evidence |
| `informatica` | Informatica lookup |
| `llm` | AI/LLM generated description |
| `failed` | Failed to resolve |

---

## Workflow

### Complete Workflow (from scratch)

```bash
# Step 1: Fetch metadata dari API
python batch_fetch_metadata.py list_of_etutor_used_tables.txt --output metadata_output
# Output: 353 files di metadata_output/

# Step 2: Generate enhanced metadata (parallel processing)
python batch_generate_metadata_parallel.py \
  --input metadata_output \
  --output generated_metadata_results \
  --workers 4 \
  --quiet
# Output: 353 enhanced JSON files + SUMMARY.json

# Step 3: Import ke data catalog atau system lain
# Gunakan JSON files dari generated_metadata_results/
```

### Individual File Processing

```bash
# Fetch single table
python fetch_table_metadata.py TABLE_NAME --out table.json

# Generate metadata untuk single file
python generate_metadata.py table.json --out result.json --business-title

# Check result
type result.json | ConvertFrom-Json | Format-Table
```

---

## Configuration

Edit settings dalam:

1. **mage_flow/config.py** - Feature flags & parameters
2. **mage_flow/prompts.py** - AI prompts untuk generation

---

## Output Structure

```
generated_metadata_results/
├── DWH_BRANCH.json
├── asrs_fact_savingmaster.json
├── AS4_GLHIST.json
├── ... (350 more files)
├── SUMMARY.json
└── *(additional metadata)*

File JSON per table berisi:
- TableName
- SourceSchema
- TableDescription (original atau generated)
- Columns[] dengan:
  - ColumnName
  - ColumnDescription (enhanced)
  - ColumnDataType
  - ColumnBusinessTitle (jika enabled)
  - Knowledge (evidence dari search)

SUMMARY.json berisi:
- timestamp & elapsed time
- total_files / successful / failed
- success_rate
- results[] dengan per-file status
```

---

## Performance & Resource Usage

| Component | Time | Memory | Disk |
|-----------|------|--------|------|
| batch_fetch_metadata.py | 5-10 min | ~200MB | 50-100MB |
| batch_generate_metadata.py (1 worker) | 30-60 min | ~150MB | 300-500MB |
| batch_generate_metadata_parallel.py (4 workers) | 15-20 min | ~400MB | 300-500MB |
| Total (all steps) | ~30-40 min | - | 400-600MB |

---

## Troubleshooting

### Issue: BM25 Service Unavailable
```
[BM25] /api/v1/system/context error: Failed to connect localhost:8003
```
**Solution:** BM25 service optional - script will continue with fallback

### Issue: KATA Database Connection Failed
```
[kata] technical-relation lookup FAILED: connection refused localhost:5432
```
**Solution:** KATA optional - script will skip and use other resolvers

### Issue: File Processing Timeout
**Solution:** Increase timeout in script, or process individually

### Issue: Low Success Rate
**Check:** SUMMARY.json for which columns failed and why
**Action:** 
- Check services are running
- Check network connectivity
- Reprocess with different options

---

## Integration Examples

### Example 1: Integrate dengan Data Catalog
```python
import json
from pathlib import Path

# Load all generated metadata
results_dir = Path("generated_metadata_results")
for json_file in results_dir.glob("*.json"):
    if json_file.name != "SUMMARY.json":
        with open(json_file) as f:
            metadata = json.load(f)
        
        # Push ke data catalog API
        catalog_api.register_table(
            table_name=metadata["TableName"],
            schema=metadata["SourceSchema"],
            description=metadata["TableDescription"],
            columns=[
                {
                    "name": col["ColumnName"],
                    "type": col["ColumnDataType"],
                    "description": col["ColumnDescription"]
                }
                for col in metadata["Columns"]
            ]
        )
```

### Example 2: Generate Documentation
```python
import json
import markdown

results_dir = Path("generated_metadata_results")

for json_file in results_dir.glob("*.json"):
    if json_file.name == "SUMMARY.json":
        continue
    
    with open(json_file) as f:
        metadata = json.load(f)
    
    # Create markdown documentation
    md = f"""# {metadata['TableName']}

**Schema:** {metadata.get('SourceSchema', 'N/A')}

## Description
{metadata.get('TableDescription', 'No description')}

## Columns

| Column | Type | Description |
|--------|------|-------------|
"""
    
    for col in metadata["Columns"]:
        md += f"| {col['ColumnName']} | {col['ColumnDataType']} | {col.get('ColumnDescription', '')} |\n"
    
    # Save markdown file
    output_file = f"docs/{metadata['TableName']}.md"
    Path(output_file).parent.mkdir(exist_ok=True)
    Path(output_file).write_text(md)
```

---

## Best Practices

1. **Always use batch processing** untuk multiple files
2. **Use parallel workers** (4-8) untuk faster processing
3. **Enable --quiet flag** untuk cleaner output
4. **Check SUMMARY.json** untuk track progress & errors
5. **Keep backup** dari metadata_output folder
6. **Monitor disk space** sebelum batch processing
7. **Process during off-hours** jika production services akan diquery

---

## Support & Debugging

1. Check `generate_metadata.py --help` untuk all options
2. Check `batch_generate_metadata_parallel.py --help` untuk parallel options
3. Review SUMMARY.json untuk per-file results
4. Enable verbose logging untuk debugging
5. Check mage_flow/config.py untuk feature toggles

---

## File Reference

```
D:\MAGE\research\
├── fetch_table_metadata.py                    # Single table fetch
├── batch_fetch_metadata.py                    # Batch fetch from API
├── generate_metadata.py                       # Single file generation
├── batch_generate_metadata_parallel.py        # RECOMMENDED: Parallel batch
├── batch_generate_metadata.ps1                # PowerShell batch (alternative)
├── list_of_etutor_used_tables.txt             # Table name list
├── metadata_output/                           # Fetched metadata (input)
│   ├── *.json
│   └── SUMMARY.json
├── generated_metadata_results/                # Generated metadata (output)
│   ├── *.json (enhanced)
│   └── SUMMARY.json
├── BATCH_GUIDE.md                             # Detailed batch guide
├── README.md                                  # This file
└── mage_flow/
    ├── config.py                              # Configuration
    ├── prompts.py                             # AI prompts
    ├── flow.py                                # Main flow engine
    └── ...
```

---

**Last Updated:** 2026-08-26  
**Version:** 1.0  
**Status:** Production Ready ✓
