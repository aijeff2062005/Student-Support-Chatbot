"""
Batch Benchmark - Process multiple Excel files in a folder
Sequentially benchmark each file and save results with _benchmark_results suffix
"""

import argparse
import glob
import os

# import from existing benchmark script
import sys
import time

import pandas as pd

sys.path.append(os.path.dirname(__file__))
from benchmark_QnA import (
    clean_text,
    test_single_question,
)

# ============================================================================
# CONFIGURATION
# ============================================================================
DEFAULT_BATCH_SIZE = 5


# ============================================================================
# FILE UTILITIES
# ============================================================================
def get_excel_files(folder_path, exclude_results=True):
    """
    Get all Excel files in folder

    Args:
            folder_path: Path to folder
            exclude_results: Exclude files ending with _benchmark_results.xlsx

    Returns:
            List of file paths
    """
    pattern = os.path.join(folder_path, "*.xlsx")
    all_files = glob.glob(pattern)

    if exclude_results:
        # Exclude files already processed
        all_files = [f for f in all_files if not f.endswith("_benchmark_results.xlsx")]

    return sorted(all_files)


def get_output_filename(input_file):
    """
    Generate output filename with _benchmark_results suffix

    Args:
            input_file: Path to input file

    Returns:
            Output filename
    """
    file_dir = os.path.dirname(input_file)
    filename = os.path.basename(input_file)
    name_without_ext = os.path.splitext(filename)[0]
    output_name = f"{name_without_ext}_benchmark_results.xlsx"

    return os.path.join(file_dir, output_name)


# ============================================================================
# SINGLE FILE PROCESSOR
# ============================================================================
def process_single_file(input_file, batch_size=DEFAULT_BATCH_SIZE, force=False):
    """
    Process a single Excel file

    Args:
            input_file: Path to input Excel file
            batch_size: Batch size for parallel processing
            force: Overwrite existing results

    Returns:
            dict: {
                    'success': bool,
                    'input_file': str,
                    'output_file': str,
                    'total_questions': int,
                    'successful': int,
                    'failed': int,
                    'duration': float,
                    'error': str (if failed)
            }
    """
    start_time = time.time()
    result = {
        "success": False,
        "input_file": input_file,
        "output_file": None,
        "total_questions": 0,
        "successful": 0,
        "failed": 0,
        "duration": 0,
        "error": None,
    }

    try:
        # Check if output already exists
        output_file = get_output_filename(input_file)
        if os.path.exists(output_file) and not force:
            result["error"] = "Output file already exists (use --force to overwrite)"
            return result

        result["output_file"] = output_file

        # Read Excel file
        print("   Reading file...")
        df = pd.read_excel(input_file)

        # Validate columns
        if "Q" not in df.columns or "A" not in df.columns:
            result["error"] = "Missing required columns: Q and/or A"
            return result

        # Clean data
        questions = [clean_text(q) for q in df["Q"]]
        expected_answers = [clean_text(a) for a in df["A"]]

        result["total_questions"] = len(questions)
        print(f"   Loaded {len(questions)} questions")

        # Create results DataFrame
        results_df = pd.DataFrame(
            {
                "Question": questions,
                "Expected Answer": expected_answers,
                "AI Answer": [""] * len(questions),
                "Status": ["Pending"] * len(questions),
                "Error Message": [""] * len(questions),
                "Duration (s)": [0.0] * len(questions),
            }
        )

        # Run benchmark (same logic as simple_benchmark_batched.py)
        print(f"   Running benchmark (batch size: {batch_size})...")

        # Create batches
        batches = []
        for i in range(0, len(questions), batch_size):
            batch = []
            for j in range(i, min(i + batch_size, len(questions))):
                batch.append((j, questions[j], j + 1))
            batches.append(batch)

        # Process batches
        from concurrent.futures import ThreadPoolExecutor, as_completed

        for batch_num, batch_data in enumerate(batches, 1):
            # Process batch in parallel
            with ThreadPoolExecutor(max_workers=len(batch_data)) as executor:
                future_to_data = {executor.submit(test_single_question, data): data for data in batch_data}

                for future in as_completed(future_to_data):
                    data = future_to_data[future]
                    idx, question, question_num = data

                    try:
                        idx, answer, duration, status = future.result()

                        results_df.at[idx, "AI Answer"] = answer
                        results_df.at[idx, "Status"] = status
                        results_df.at[idx, "Duration (s)"] = round(duration, 2)

                        if status == "Success":
                            result["successful"] += 1
                        else:
                            result["failed"] += 1
                            results_df.at[idx, "Error Message"] = answer

                        # Simple progress
                        status_icon = "" if status == "Success" else ""
                        print(f"    [{question_num}/{len(questions)}] {status_icon}", end="\r")

                    except Exception as e:
                        result["failed"] += 1
                        results_df.at[idx, "AI Answer"] = f"ERROR: {str(e)}"
                        results_df.at[idx, "Status"] = "Error"

        print()  # New line after progress

        # Save results
        print("   Saving results...")
        results_df.to_excel(output_file, index=False, engine="openpyxl")
        print(f"   Saved to: {os.path.basename(output_file)}")

        result["success"] = True

    except Exception as e:
        result["error"] = str(e)

    result["duration"] = time.time() - start_time
    return result


# ============================================================================
# BATCH PROCESSOR
# ============================================================================
def batch_benchmark(folder_path, batch_size=DEFAULT_BATCH_SIZE, force=False, skip_errors=True):
    """
    Process all Excel files in folder

    Args:
            folder_path: Path to folder containing Excel files
            batch_size: Batch size for parallel processing
            force: Overwrite existing results
            skip_errors: Continue processing if one file fails

    Returns:
            List of results for each file
    """
    print("=" * 80)
    print(" BATCH BENCHMARK - Process Multiple Files")
    print("=" * 80)
    print(f"Folder:      {folder_path}")
    print(f"Batch size:  {batch_size}")
    print(f"Force:       {force}")
    print("=" * 80)

    # Get files to process
    print("\n Scanning folder...")
    files = get_excel_files(folder_path, exclude_results=True)

    if not files:
        print(" No Excel files found in folder")
        return []

    print(f" Found {len(files)} file(s) to process")

    # Process each file
    results = []
    start_time = time.time()

    for file_num, input_file in enumerate(files, 1):
        print(f"\n{'=' * 80}")
        print(f"FILE {file_num}/{len(files)}: {os.path.basename(input_file)}")
        print(f"{'=' * 80}")

        result = process_single_file(input_file, batch_size=batch_size, force=force)
        results.append(result)

        if result["success"]:
            print(f"   Benchmark completed: {result['successful']}/{result['total_questions']} success")
            print(f"  ⏱️  Duration: {result['duration']:.2f}s")
        else:
            print(f"   Failed: {result['error']}")
            if not skip_errors:
                print("\nStopping due to error (use --skip-errors to continue)")
                break

    total_duration = time.time() - start_time

    # Summary
    print("\n" + "=" * 80)
    print(" BATCH BENCHMARK COMPLETED!")
    print("=" * 80)

    successful_files = sum(1 for r in results if r["success"])
    failed_files = len(results) - successful_files
    total_questions = sum(r["total_questions"] for r in results)
    total_successful = sum(r["successful"] for r in results)
    total_failed = sum(r["failed"] for r in results)

    print(f"Total files processed: {successful_files}/{len(files)}")
    if failed_files > 0:
        print(f"Failed files:          {failed_files}")
    print(f"Total questions:       {total_questions}")
    print(f"Successful:            {total_successful} ({total_successful / total_questions * 100:.1f}%)")
    print(f"Failed:                {total_failed} ({total_failed / total_questions * 100:.1f}%)")
    print(f"Total duration:        {total_duration:.2f}s ({total_duration / 60:.2f} minutes)")

    # List output files
    if successful_files > 0:
        print("\n Output files:")
        for r in results:
            if r["success"]:
                print(f"  • {os.path.basename(r['output_file'])}")

    # List errors
    if failed_files > 0:
        print("\n Failed files:")
        for r in results:
            if not r["success"]:
                print(f"   {os.path.basename(r['input_file'])}: {r['error']}")

    print("=" * 80)

    return results


# ============================================================================
# MAIN
# ============================================================================
def main():
    parser = argparse.ArgumentParser(description="Batch benchmark multiple Excel files")
    parser.add_argument("--folder", type=str, required=True, help="Path to folder containing Excel files")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Batch size for parallel processing (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing benchmark results")
    parser.add_argument(
        "--skip-errors", action="store_true", default=True, help="Continue processing if one file fails (default: True)"
    )

    args = parser.parse_args()

    # Validate folder
    if not os.path.isdir(args.folder):
        print(f"Error: Folder does not exist: {args.folder}")
        return

    # Run batch benchmark
    batch_benchmark(folder_path=args.folder, batch_size=args.batch_size, force=args.force, skip_errors=args.skip_errors)


if __name__ == "__main__":
    main()
