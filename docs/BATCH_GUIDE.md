# Batch Metadata Generation Guide

## Overview

Script PowerShell (`batch_generate_metadata.ps1`) untuk batch processing semua JSON files metadata melalui `generate_metadata.py` dan menyimpan hasil ke folder baru.

## Files

1. **batch_generate_metadata.ps1** - Main batch processing script
2. **generate_metadata.py** - Individual metadata generator (dipanggil per file)
3. **batch_fetch_metadata.py** - Fetch metadata dari API (dijalankan sebelumnya)

## Usage

### Basic Usage
```powershell
cd D:\MAGE\research
.\batch_generate_metadata.ps1
```

### With Custom Output Directory
```powershell
.\batch_generate_metadata.ps1 -OutputDir my_output_folder
```

### With All Options
```powershell
.\batch_generate_metadata.ps1 `
  -InputDir metadata_output `
  -OutputDir generated_metadata_results `
  -Quiet `
  -BusinessTitle `
  -Force
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `-InputDir` | String | `metadata_output` | Folder containing input JSON files |
| `-OutputDir` | String | `generated_metadata` | Folder untuk menyimpan hasil |
| `-Quiet` | Switch | False | Suppress verbose output per-step |
| `-BusinessTitle` | Switch | False | Generate column business titles |
| `-Force` | Switch | False | Regenerate even if descriptions exist |

## Examples

### Example 1: Basic Processing (Default)
```powershell
.\batch_generate_metadata.ps1
```

Output folder: `generated_metadata`

### Example 2: Process dengan Business Titles
```powershell
.\batch_generate_metadata.ps1 -OutputDir results_with_titles -BusinessTitle
```

### Example 3: Force Regenerate Semua
```powershell
.\batch_generate_metadata.ps1 -OutputDir force_regenerated -Force -Quiet
```

### Example 4: Custom Input & Output
```powershell
.\batch_generate_metadata.ps1 `
  -InputDir metadata_output `
  -OutputDir final_results `
  -Quiet `
  -BusinessTitle
```

## Output Structure

```
generated_metadata_results/
├── asrs_fact_savingmaster.json
├── DWH_BRANCH.json
├── AS4_GLHIST.json
├── ... (350 files)
└── SUMMARY.json
```

Setiap file JSON berisi:
- **TableName** - Nama table
- **TableDescription** - Deskripsi table (generated atau original)
- **Columns** - Array dari kolom dengan:
  - ColumnName
  - ColumnDescription (generated/enhanced)
  - ColumnDataType
  - ColumnBusinessTitle (jika enabled)
  - Knowledge (evidence dari mana description diambil)
- **GenerationSummary** - Statistik processing

## SUMMARY.json Format

```json
{
  "timestamp": "2026-08-26T13:38:06Z",
  "start_time": "2026-08-26T13:38:07Z",
  "end_time": "2026-08-26T15:42:30Z",
  "elapsed_seconds": 7343,
  "total_files": 353,
  "successful": 351,
  "failed": 2,
  "success_rate": 99.43,
  "output_directory": "D:\\MAGE\\research\\generated_metadata_results",
  "input_directory": "D:\\MAGE\\research\\metadata_output",
  "options": {
    "quiet": true,
    "business_title": false,
    "force": false
  },
  "failed_files": [
    {
      "Table": "table_name",
      "Error": "error message"
    }
  ]
}
```

## Processing Flow

```
1. Read all *.json files from InputDir (except SUMMARY.json)
2. For each file:
   ├── Call: python generate_metadata.py <input.json> --out <output.json> [options]
   ├── Track: Success/Failure
   └── Display: Progress with percentage
3. Generate SUMMARY.json dengan statistik
4. Print final report
```

## Resolver Chain yang Digunakan

Setiap kolom melalui 6-stage resolver:

1. **Exact AS400_Confluence** - Exact lookup
2. **BM25 AS400_Confluence** - Full-text search
3. **KATA Evidence** - Database lookup (jika available)
4. **BM25 Informatica** - Informatica search
5. **Confluence Fallback** - Confluence search fallback
6. **Pure LLM** - AI generation jika semua gagal

### Resolution Status

- `skipped` - Sudah ada deskripsi yang bagus
- `exact_match` - Ditemukan exact match
- `bm25_*` - Ditemukan via full-text search
- `kata_*` - Ditemukan di KATA database
- `confluence` - Ditemukan di Confluence
- `llm` - Di-generate oleh AI/LLM
- `failed` - Gagal di-resolve

## Performance Notes

- Processing time: ~5-10 detik per file (tergantung service availability)
- Total untuk 353 files: ~30-60 menit
- Memory usage: Minimal (~100-200MB)
- Disk space: ~300-500MB untuk output

## Troubleshooting

### Issue: BM25 Connection Error
**Cause**: BM25 service (localhost:8003) tidak running
**Solution**: Service optional - script akan continue dengan fallback

### Issue: KATA Connection Error
**Cause**: KATA database (localhost:5432) tidak running
**Solution**: Service optional - script akan skip KATA stage

### Issue: Processing Lambat
**Cause**: Banyak files dan services tidak available
**Solution**: Normal - script akan retry dan fallback ke LLM

### Issue: Beberapa Files Failed
**Check**: SUMMARY.json untuk detail error pada file tertentu
**Solution**: Dapat di-reprocess individual file dengan generate_metadata.py

## Reprocessing Individual Files

Jika ada file yang gagal, bisa di-reprocess:

```powershell
python generate_metadata.py metadata_output/table_name.json `
  --out generated_metadata_results/table_name.json `
  --quiet --business-title
```

## Integration dengan Workflows

### Step 1: Fetch metadata dari API
```powershell
python batch_fetch_metadata.py list_of_etutor_used_tables.txt --output metadata_output
```

### Step 2: Generate enhanced metadata
```powershell
.\batch_generate_metadata.ps1 -OutputDir generated_metadata_results -Quiet
```

### Step 3: Use hasil untuk documentation/data catalog
- Import generated_metadata_results/*.json ke data catalog system
- Gunakan ColumnDescription untuk documentation
- Gunakan ColumnBusinessTitle untuk business user interface

## Best Practices

1. **Always use -Quiet flag** untuk batch processing agar lebih cepat
2. **Monitor SUMMARY.json** untuk track progress dan errors
3. **Keep backup** dari input metadata_output folder
4. **Process during off-hours** jika ada production services yang akan diquery
5. **Incrementally add tables** jika dataset sangat besar

## File Retention

Recommend struktur folder:

```
mage_flow/
├── metadata_output/                 # Dari batch_fetch_metadata.py
├── generated_metadata_results/      # Dari batch_generate_metadata.ps1
│   ├── *.json                       # Generated files
│   └── SUMMARY.json                 # Processing report
├── batch_fetch_metadata.py          # Script untuk fetch
├── batch_generate_metadata.ps1      # Script untuk generate
└── generate_metadata.py             # Single file generator
```

## Monitoring & Validation

Check hasil:

```powershell
# Count successful outputs
(Get-ChildItem generated_metadata_results -Filter "*.json" -File | Measure-Object).Count

# Check SUMMARY for details
Get-Content generated_metadata_results/SUMMARY.json | ConvertFrom-Json | Format-Table

# Validate output file
Get-Content generated_metadata_results/asrs_fact_savingmaster.json | ConvertFrom-Json
```
