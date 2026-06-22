"""
Batch Evaluate - Evaluate multiple benchmark result files in PARALLEL
Generate PDF summary report for all evaluations
"""

import argparse
import glob
import os

# import from existing evaluator script
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd

sys.path.append(os.path.dirname(__file__))
from AI_evaluation import (
    COL_AI_ANSWER,
    COL_AI_CHECK_PASS,
    COL_AI_CHECK_SCORE,
    COL_QUESTION,
    COL_STATUS,
    DEFAULT_THRESHOLD,
    DeepInfraEvaluator,
    evaluate_batch,
)

# ============================================================================
# CONFIGURATION
# ============================================================================
# DEFAULT_THRESHOLD = 70
DEFAULT_EVAL_BATCH_SIZE = 5
DEFAULT_FILE_PARALLEL = 3  # Process 3 files in parallel


# ============================================================================
# FILE UTILITIES
# ============================================================================
def get_benchmark_result_files(folder_path):
    """
    Get all files ending with _benchmark_results.xlsx

    Args:
            folder_path: Path to folder

    Returns:
            List of file paths
    """
    pattern = os.path.join(folder_path, "*_benchmark_results.xlsx")
    files = glob.glob(pattern)
    return sorted(files)


# ============================================================================
# SINGLE FILE EVALUATOR
# ============================================================================
def evaluate_single_file(input_file, threshold=DEFAULT_THRESHOLD, eval_batch_size=DEFAULT_EVAL_BATCH_SIZE):
    """
    Evaluate a single benchmark result file

    Args:
            input_file: Path to benchmark result file
            threshold: Pass threshold
            eval_batch_size: Batch size for evaluation

    Returns:
            dict: {
                    'success': bool,
                    'file': str,
                    'total_questions': int,
                    'evaluated': int,
                    'pass_count': int,
                    'fail_count': int,
                    'average_score': float,
                    'duration': float,
                    'error': str (if failed)
            }
    """
    start_time = time.time()
    result = {
        "success": False,
        "file": input_file,
        "total_questions": 0,
        "evaluated": 0,
        "pass_count": 0,
        "fail_count": 0,
        "average_score": 0,
        "duration": 0,
        "error": None,
    }

    try:
        # Read file
        print("   Reading file...")
        df = pd.read_excel(input_file)
        result["total_questions"] = len(df)

        # Validate columns
        required_cols = [COL_QUESTION, COL_AI_ANSWER, COL_STATUS]
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            result["error"] = f"Missing columns: {missing_cols}"
            return result

        # Count to evaluate
        eval_rows = df[df[COL_STATUS] == "Success"]
        result["evaluated"] = len(eval_rows)
        print(f"   {result['evaluated']} successful answers to evaluate")

        if result["evaluated"] == 0:
            result["error"] = "No successful answers to evaluate"
            return result

        # Create evaluator
        evaluator = DeepInfraEvaluator()

        # Suppress verbose output during batch processing
        import contextlib
        import io

        # Evaluate (capture output to avoid clutter)
        print("   Running AI evaluation...")
        with contextlib.redirect_stdout(io.StringIO()):
            df = evaluate_batch(df, evaluator, threshold=threshold, batch_size=eval_batch_size)

        # Save results
        print("   Saving results...")
        df.to_excel(input_file, index=False, engine="openpyxl")

        # Calculate statistics
        evaluated_df = df[df[COL_AI_CHECK_SCORE] > 0]
        result["pass_count"] = len(df[df[COL_AI_CHECK_PASS] == "Pass"])
        result["fail_count"] = len(df[df[COL_AI_CHECK_PASS] == "Fail"])
        result["average_score"] = evaluated_df[COL_AI_CHECK_SCORE].mean() if len(evaluated_df) > 0 else 0

        print(
            f"   Evaluation completed: {result['pass_count']}/{result['evaluated']} pass ({result['average_score']:.1f}% avg)"
        )

        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        print(f"   Failed: {result['error']}")

    result["duration"] = time.time() - start_time
    return result


# ============================================================================
# BATCH EVALUATOR (PARALLEL)
# ============================================================================
def batch_evaluate(
    folder_path,
    threshold=DEFAULT_THRESHOLD,
    eval_batch_size=DEFAULT_EVAL_BATCH_SIZE,
    file_parallel=DEFAULT_FILE_PARALLEL,
    skip_errors=True,
):
    """
    Evaluate all benchmark result files in folder IN PARALLEL

    Args:
            folder_path: Path to folder
            threshold: Pass threshold
            eval_batch_size: Batch size for evaluation (questions)
            file_parallel: Number of files to process in parallel
            skip_errors: Continue if one file fails

    Returns:
            List of results for each file
    """
    print("=" * 80)
    print(" BATCH EVALUATION - Evaluate Multiple Files (PARALLEL)")
    print("=" * 80)
    print(f"Folder:           {folder_path}")
    print(f"Threshold:        {threshold}%")
    print(f"Eval batch size:  {eval_batch_size}")
    print(f"File parallel:    {file_parallel}")
    print("=" * 80)

    # Get files to evaluate
    print("\n Scanning folder...")
    files = get_benchmark_result_files(folder_path)

    if not files:
        print(" No benchmark result files found (*_benchmark_results.xlsx)")
        return []

    print(f" Found {len(files)} file(s) to evaluate")

    # Process files in parallel
    results = []
    start_time = time.time()

    print(f"\n Processing {len(files)} files in parallel (max {file_parallel} concurrent)...")
    print("=" * 80)

    with ThreadPoolExecutor(max_workers=file_parallel) as executor:
        # Submit all tasks
        future_to_file = {
            executor.submit(evaluate_single_file, file, threshold=threshold, eval_batch_size=eval_batch_size): file
            for file in files
        }

        # Collect results as they complete
        completed = 0
        for future in as_completed(future_to_file):
            file = future_to_file[future]
            completed += 1

            print(f"\n[{completed}/{len(files)}] {os.path.basename(file)}")
            print("-" * 80)

            try:
                result = future.result()
                results.append(result)

                if not result["success"] and not skip_errors:
                    print("\nStopping due to error (use --skip-errors to continue)")
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

            except Exception as e:
                print(f"   Exception: {str(e)}")
                results.append({"success": False, "file": file, "error": str(e)})

                if not skip_errors:
                    break

    total_duration = time.time() - start_time

    # Summary
    print("\n" + "=" * 80)
    print(" BATCH EVALUATION COMPLETED!")
    print("=" * 80)

    successful_files = sum(1 for r in results if r["success"])
    failed_files = len(results) - successful_files
    total_questions = sum(r.get("total_questions", 0) for r in results if r["success"])
    total_evaluated = sum(r.get("evaluated", 0) for r in results if r["success"])
    total_pass = sum(r.get("pass_count", 0) for r in results if r["success"])
    total_fail = sum(r.get("fail_count", 0) for r in results if r["success"])
    avg_score = sum(r.get("average_score", 0) for r in results if r["success"]) / max(successful_files, 1)

    print(f"Total files evaluated: {successful_files}/{len(files)}")
    if failed_files > 0:
        print(f"Failed files:          {failed_files}")
    print(f"Total questions:       {total_questions}")
    print(f"Evaluated:             {total_evaluated}")
    print(f"Pass:                  {total_pass} ({total_pass / max(total_evaluated, 1) * 100:.1f}%)")
    print(f"Fail:                  {total_fail} ({total_fail / max(total_evaluated, 1) * 100:.1f}%)")
    print(f"Average score:         {avg_score:.1f}%")
    print(f"Total duration:        {total_duration:.2f}s ({total_duration / 60:.2f} minutes)")

    # List successful files
    if successful_files > 0:
        print("\n Evaluated files:")
        for r in results:
            if r["success"]:
                print(
                    f"  • {os.path.basename(r['file'])} - {r['pass_count']}/{r['evaluated']} pass ({r['average_score']:.1f}%)"
                )

    # List errors
    if failed_files > 0:
        print("\n Failed files:")
        for r in results:
            if not r["success"]:
                print(f"   {os.path.basename(r['file'])}: {r.get('error', 'Unknown error')}")

    print("=" * 80)

    return results


# ============================================================================
# PDF SUMMARY GENERATOR
# ============================================================================
def generate_pdf_summary(results, output_path, threshold):
    """
    Generate PDF summary report for all evaluations

    Args:
            results: List of evaluation results
            output_path: Path to save PDF
            threshold: Pass threshold used
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        print("\n Generating PDF summary report...")

        # Create PDF
        doc = SimpleDocTemplate(output_path, pagesize=A4)
        story = []
        styles = getSampleStyleSheet()

        # Custom styles
        title_style = ParagraphStyle(
            "CustomTitle",
            parent=styles["Heading1"],
            fontSize=24,
            textColor=colors.HexColor("#1a1a1a"),
            spaceAfter=30,
            alignment=TA_CENTER,
        )

        heading_style = ParagraphStyle(
            "CustomHeading",
            parent=styles["Heading2"],
            fontSize=16,
            textColor=colors.HexColor("#333333"),
            spaceAfter=12,
            spaceBefore=12,
        )

        # Title
        story.append(Paragraph("AI Evaluation Summary Report", title_style))
        story.append(Spacer(1, 12))

        # Timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        story.append(Paragraph(f"Generated: {timestamp}", styles["Normal"]))
        story.append(Spacer(1, 20))

        # Overall Statistics
        successful_files = [r for r in results if r["success"]]
        total_questions = sum(r["total_questions"] for r in successful_files)
        total_evaluated = sum(r["evaluated"] for r in successful_files)
        total_pass = sum(r["pass_count"] for r in successful_files)
        total_fail = sum(r["fail_count"] for r in successful_files)
        avg_score = sum(r["average_score"] for r in successful_files) / max(len(successful_files), 1)

        story.append(Paragraph("Overall Statistics", heading_style))

        stats_data = [
            ["Metric", "Value"],
            ["Total Files", str(len(results))],
            ["Successful Files", str(len(successful_files))],
            ["Total Questions", str(total_questions)],
            ["Evaluated", str(total_evaluated)],
            ["Pass", f"{total_pass} ({total_pass / max(total_evaluated, 1) * 100:.1f}%)"],
            ["Fail", f"{total_fail} ({total_fail / max(total_evaluated, 1) * 100:.1f}%)"],
            ["Average Score", f"{avg_score:.1f}%"],
            ["Pass Threshold", f"{threshold}%"],
        ]

        stats_table = Table(stats_data, colWidths=[3 * inch, 3 * inch])
        stats_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 12),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 12),
                    ("BACKGROUND", (0, 1), (-1, -1), colors.beige),
                    ("GRID", (0, 0), (-1, -1), 1, colors.black),
                ]
            )
        )

        story.append(stats_table)
        story.append(Spacer(1, 20))

        # Per-file details
        story.append(Paragraph("Per-File Details", heading_style))
        story.append(Spacer(1, 12))

        file_data = [["File", "Questions", "Evaluated", "Pass", "Fail", "Avg Score", "Status"]]

        for r in results:
            if r["success"]:
                file_data.append(
                    [
                        os.path.basename(r["file"])[:40],  # Truncate long names
                        str(r["total_questions"]),
                        str(r["evaluated"]),
                        str(r["pass_count"]),
                        str(r["fail_count"]),
                        f"{r['average_score']:.1f}%",
                        "",
                    ]
                )
            else:
                file_data.append([os.path.basename(r["file"])[:40], "-", "-", "-", "-", "-", ""])

        file_table = Table(
            file_data, colWidths=[2.5 * inch, 0.7 * inch, 0.7 * inch, 0.7 * inch, 0.7 * inch, 0.9 * inch, 0.5 * inch]
        )
        file_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 10),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 12),
                    ("BACKGROUND", (0, 1), (-1, -1), colors.beige),
                    ("GRID", (0, 0), (-1, -1), 1, colors.black),
                    ("FONTSIZE", (0, 1), (-1, -1), 9),
                ]
            )
        )

        story.append(file_table)

        # Build PDF
        doc.build(story)

        print(f" PDF summary saved to: {output_path}")

    except importError:
        print("️  reportlab not installed. Skipping PDF generation.")
        print("   Install with: pip install reportlab")
    except Exception as e:
        print(f"️  Failed to generate PDF: {str(e)}")


# ============================================================================
# MAIN
# ============================================================================
def main():
    parser = argparse.ArgumentParser(description="Batch evaluate multiple benchmark result files")
    parser.add_argument("--folder", type=str, required=True, help="Path to folder containing benchmark result files")
    parser.add_argument(
        "--threshold", type=int, default=DEFAULT_THRESHOLD, help=f"Pass threshold (default: {DEFAULT_THRESHOLD})"
    )
    parser.add_argument(
        "--eval-batch-size",
        type=int,
        default=DEFAULT_EVAL_BATCH_SIZE,
        help=f"Batch size for evaluation (default: {DEFAULT_EVAL_BATCH_SIZE})",
    )
    parser.add_argument(
        "--file-parallel",
        type=int,
        default=DEFAULT_FILE_PARALLEL,
        help=f"Number of files to process in parallel (default: {DEFAULT_FILE_PARALLEL})",
    )
    parser.add_argument(
        "--skip-errors", action="store_true", default=True, help="Continue processing if one file fails (default: True)"
    )
    parser.add_argument(
        "--pdf-output",
        type=str,
        default=None,
        help="Path to save PDF summary (default: {folder}/evaluation_summary.pdf)",
    )

    args = parser.parse_args()

    # Validate folder
    if not os.path.isdir(args.folder):
        print(f"Error: Folder does not exist: {args.folder}")
        return

    # Run batch evaluation
    results = batch_evaluate(
        folder_path=args.folder,
        threshold=args.threshold,
        eval_batch_size=args.eval_batch_size,
        file_parallel=args.file_parallel,
        skip_errors=args.skip_errors,
    )

    # Generate PDF summary
    if results:
        pdf_path = args.pdf_output or os.path.join(args.folder, "evaluation_summary.pdf")
        generate_pdf_summary(results, pdf_path, args.threshold)


if __name__ == "__main__":
    main()
