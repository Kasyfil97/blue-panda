"""Parallel batch processor untuk generate_metadata.py dengan concurrent processing.

Usage:
    python batch_generate_metadata_parallel.py [--input metadata_output] [--output results] [--workers 4]

Features:
    - Parallel processing dengan multiple workers
    - Progress bar tracking
    - Better error handling
    - Faster processing
"""

import argparse
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
from datetime import datetime


def process_file(input_file: Path, output_dir: Path, quiet: bool = True, business_title: bool = False) -> dict:
    """Process single file dengan generate_metadata.py"""
    output_file = output_dir / f"{input_file.stem}.json"
    table_name = input_file.stem

    try:
        # Build command
        cmd = [
            "python",
            "generate_metadata.py",
            str(input_file),
            "--out", str(output_file)
        ]

        if quiet:
            cmd.append("--quiet")
        if business_title:
            cmd.append("--business-title")

        # Run process
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=300,  # 5 minutes timeout per file
            text=True
        )

        # Check if successful
        if result.returncode == 0 and output_file.exists():
            return {
                "table": table_name,
                "status": "success",
                "output_file": str(output_file),
                "error": None
            }
        else:
            return {
                "table": table_name,
                "status": "failed",
                "output_file": None,
                "error": result.stderr[:200] if result.stderr else "Unknown error"
            }

    except subprocess.TimeoutExpired:
        return {
            "table": table_name,
            "status": "timeout",
            "output_file": None,
            "error": "Processing timeout (>5 minutes)"
        }
    except Exception as e:
        return {
            "table": table_name,
            "status": "error",
            "output_file": None,
            "error": str(e)[:200]
        }


def main():
    parser = argparse.ArgumentParser(
        description="Batch process metadata files with parallel workers"
    )
    parser.add_argument(
        "--input",
        default="metadata_output",
        help="Input directory (default: metadata_output)"
    )
    parser.add_argument(
        "--output",
        default="generated_metadata_results",
        help="Output directory (default: generated_metadata_results)"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel workers (default: 4)"
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Quiet mode (suppress verbose output)"
    )
    parser.add_argument(
        "--business-title",
        action="store_true",
        help="Enable business title generation"
    )

    args = parser.parse_args()

    # Setup paths
    input_dir = Path(args.input)
    output_dir = Path(args.output)

    print("=" * 70)
    print("Parallel Batch Generate Metadata")
    print("=" * 70)
    print(f"Input directory   : {input_dir.absolute()}")
    print(f"Output directory  : {output_dir.absolute()}")
    print(f"Workers           : {args.workers}")
    print(f"Quiet mode        : {args.quiet}")
    print(f"Business title    : {args.business_title}")
    print()

    # Create output directory
    output_dir.mkdir(exist_ok=True)

    # Get input files
    json_files = [
        f for f in input_dir.glob("*.json")
        if f.name != "SUMMARY.json"
    ]
    json_files.sort()

    print(f"Found {len(json_files)} files to process\n")

    if not json_files:
        print("✗ No JSON files found")
        sys.exit(1)

    # Process files in parallel
    start_time = datetime.now()
    results = []
    successful = 0
    failed = 0

    print("Processing...")
    print("-" * 70)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                process_file,
                json_file,
                output_dir,
                args.quiet,
                args.business_title
            ): json_file
            for json_file in json_files
        }

        completed = 0
        for future in as_completed(futures):
            completed += 1
            result = future.result()
            results.append(result)

            # Print progress
            percentage = (completed / len(json_files)) * 100
            status_icon = "✓" if result["status"] == "success" else "✗"
            print(
                f"[{completed:3d}/{len(json_files)}] {percentage:5.1f}% - "
                f"{result['table']:50s} {status_icon}"
            )

            if result["status"] == "success":
                successful += 1
            else:
                failed += 1

    # Calculate statistics
    end_time = datetime.now()
    elapsed = end_time - start_time
    success_rate = (successful / len(json_files)) * 100

    # Print summary
    print("-" * 70)
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total files       : {len(json_files)}")
    print(f"Successful        : {successful}")
    print(f"Failed            : {failed}")
    print(f"Success rate      : {success_rate:.1f}%")
    print(f"Elapsed time      : {elapsed.total_seconds():.0f}s "
          f"({int(elapsed.total_seconds()//60)}m {int(elapsed.total_seconds()%60)}s)")
    print(f"Output directory  : {output_dir.absolute()}")
    print()

    # Save summary
    summary = {
        "timestamp": start_time.isoformat(),
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "elapsed_seconds": int(elapsed.total_seconds()),
        "total_files": len(json_files),
        "successful": successful,
        "failed": failed,
        "success_rate": round(success_rate, 2),
        "output_directory": str(output_dir.absolute()),
        "input_directory": str(input_dir.absolute()),
        "workers": args.workers,
        "results": results
    }

    summary_file = output_dir / "SUMMARY.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"✓ Summary saved to: {summary_file}")
    print()

    # Print failed files if any
    if failed > 0:
        print("Failed files:")
        for result in results:
            if result["status"] != "success":
                print(f"  ✗ {result['table']}: {result['error']}")
        print()

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
