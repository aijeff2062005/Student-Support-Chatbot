"""
AI Evaluator - Evaluate benchmark results using DeepInfra LLM
Adds 3 columns: AI check (%), AI check (pass/fail), AI explanation
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from openai import OpenAI

# ============================================================================
# CONFIGURATION
# ============================================================================
DEEPINFRA_API_KEY = "BeArxAGaBlO1WM5LX1A1tgYs76e3Hrt2"
DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"
DEEPINFRA_MODEL = "Qwen/Qwen3-235B-A22B-Instruct-2507"

# Evaluation settings
DEFAULT_THRESHOLD = 70  # Pass if score >= 70%
EVALUATION_TIMEOUT = 30  # seconds
BATCH_SIZE = 10  # Number of evaluations to run in parallel
MAX_RETRIES = 3  # Retry if LLM fails

# Excel columns
COL_QUESTION = "Question"
COL_AI_ANSWER = "AI Answer"
COL_STATUS = "Status"
COL_AI_CHECK_SCORE = "AI check (%)"
COL_AI_CHECK_PASS = "AI check (pass/fail)"
COL_AI_EXPLANATION = "AI explanation"
COL_DETAILED_METRICS = "Detailed metrics"

# ============================================================================
# EVALUATION PROMPT
# ============================================================================
EVALUATION_PROMPT_TEMPLATE = """
/no_think
Bạn là một chuyên gia đánh giá chất lượng câu trả lời AI cho hệ thống tư vấn tuyển sinh đại học Gia Định.

Nhiệm vụ: Đánh giá xem câu trả lời của AI có đáp ứng tốt câu hỏi của người dùng không.

LƯU Ý: AI chỉ trả lời dựa trên dữ liệu có sẵn của trường, KHÔNG tìm kiếm thông tin bên ngoài. Nếu trường không có dữ liệu về câu hỏi, AI sẽ trả lời "không có thông tin" - điều này là ĐÚNG và không bị trừ điểm.

CÂU HỎI:
{question}

CÂU TRẢ LỜI CỦA AI:
{ai_answer}

Hãy đánh giá dựa trên 3 tiêu chí sau (mỗi tiêu chí 0-100 điểm):

1. Độ đầy đủ (Completeness): 
   - Có trả lời đủ nội dung câu hỏi không?
   - Nếu trường không có dữ liệu và AI nói rõ → vẫn được điểm cao
   - Nếu thiếu thông tin quan trọng mà trường CÓ dữ liệu → trừ điểm

2. Độ liên quan (Relevance):
   - Có tập trung vào câu hỏi không?
   - Có đi lạc đề hoặc nói về thứ không liên quan không?

3. Độ rõ ràng (Clarity):
   - Dễ hiểu, mạch lạc không?
   - Cấu trúc câu có rõ ràng không?
   - Ngôn ngữ có phù hợp với người hỏi không?

Trả về đánh giá theo format JSON với 5 trường:
{{
  "completeness": <điểm độ đầy đủ 0-100>,
  "relevance": <điểm độ liên quan 0-100>,
  "clarity": <điểm độ rõ ràng 0-100>,
  "overall_score": <điểm tổng = trung bình 3 điểm trên>,
  "pass": <true nếu overall_score >= {threshold}, false nếu ngược lại>,
  "explanation": "<giải thích chi tiết bằng tiếng Việt, nêu rõ điểm mạnh/yếu của câu trả lời>"
}}

CHỈ TRẢ VỀ JSON, KHÔNG CÓ TEXT KHÁC."""


# ============================================================================
# DEEPINFRA CLIENT
# ============================================================================
class DeepInfraEvaluator:
    """Client for DeepInfra LLM evaluation"""

    def __init__(self, api_key=DEEPINFRA_API_KEY, model=DEEPINFRA_MODEL):
        """Initialize DeepInfra client"""
        self.client = OpenAI(api_key=api_key, base_url=DEEPINFRA_BASE_URL)
        self.model = model
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def evaluate_answer(self, question, ai_answer, threshold=DEFAULT_THRESHOLD):
        """
        Evaluate a single answer using LLM

        Args:
            question: User question
            ai_answer: AI's answer to evaluate
            threshold: Pass threshold (default: 70)

        Returns:
            dict: {
                completeness, relevance, clarity,
                overall_score, pass, explanation
            } or None if failed
        """
        # Handle empty or error answers
        if not ai_answer or pd.isna(ai_answer) or str(ai_answer).startswith("ERROR:"):
            return {
                "completeness": 0,
                "relevance": 0,
                "clarity": 0,
                "overall_score": 0,
                "pass": False,
                "explanation": "Không có câu trả lời hợp lệ để đánh giá",
            }

        # Create prompt
        prompt = EVALUATION_PROMPT_TEMPLATE.format(question=question, ai_answer=ai_answer, threshold=threshold)

        # Call LLM with retry
        for attempt in range(MAX_RETRIES):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,  # Low temperature for consistent evaluation
                    timeout=EVALUATION_TIMEOUT,
                )

                # Track token usage
                self.total_prompt_tokens += response.usage.prompt_tokens
                self.total_completion_tokens += response.usage.completion_tokens

                # Parse response
                content = response.choices[0].message.content.strip()

                # Try to extract JSON from response
                result = self._parse_json_response(content)

                if result:
                    # Validate pass/fail matches threshold
                    result["pass"] = result["overall_score"] >= threshold
                    return result

                # If parsing failed, retry
                if attempt < MAX_RETRIES - 1:
                    time.sleep(1)
                    continue

                # Final attempt failed
                return {
                    "completeness": 0,
                    "relevance": 0,
                    "clarity": 0,
                    "overall_score": 0,
                    "pass": False,
                    "explanation": f"Không thể parse kết quả đánh giá: {content[:100]}",
                }

            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2**attempt)  # Exponential backoff
                    continue

                return {
                    "completeness": 0,
                    "relevance": 0,
                    "clarity": 0,
                    "overall_score": 0,
                    "pass": False,
                    "explanation": f"Lỗi khi đánh giá: {str(e)}",
                }

        return None

    def _parse_json_response(self, content):
        """
        Parse JSON from LLM response
        Handles cases where LLM adds markdown formatting or extra text
        """
        # Remove markdown code blocks if present
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        content = content.strip()

        try:
            result = json.loads(content)

            # Validate required fields
            required_fields = ["completeness", "relevance", "clarity", "overall_score", "pass", "explanation"]
            if all(field in result for field in required_fields):
                # Ensure scores are 0-100
                result["completeness"] = max(0, min(100, int(result["completeness"])))
                result["relevance"] = max(0, min(100, int(result["relevance"])))
                result["clarity"] = max(0, min(100, int(result["clarity"])))
                result["overall_score"] = max(0, min(100, int(result["overall_score"])))
                result["pass"] = bool(result["pass"])
                result["explanation"] = str(result["explanation"])
                return result

        except json.JSONDecodeError:
            pass

        return None

    def get_token_stats(self):
        """Get token usage statistics"""
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }


# ============================================================================
# BATCH EVALUATION
# ============================================================================
def evaluate_single_row(args):
    """
    Evaluate a single row (for parallel processing)

    Args:
        args: tuple of (idx, question, ai_answer, threshold, evaluator)

    Returns:
        tuple: (idx, completeness, relevance, clarity, overall_score, pass_flag, explanation)
    """
    idx, question, ai_answer, threshold, evaluator = args

    result = evaluator.evaluate_answer(question, ai_answer, threshold)

    if result:
        # Format detailed metrics string
        detailed_metrics = (
            f"Đầy đủ: {result['completeness']}% | Liên quan: {result['relevance']}% | Rõ ràng: {result['clarity']}%"
        )

        return (
            idx,
            result["completeness"],
            result["relevance"],
            result["clarity"],
            result["overall_score"],
            result["pass"],
            result["explanation"],
            detailed_metrics,
        )
    else:
        return (idx, 0, 0, 0, 0, False, "Đánh giá thất bại", "Đầy đủ: 0% | Liên quan: 0% | Rõ ràng: 0%")


def evaluate_batch(df, evaluator, threshold=DEFAULT_THRESHOLD, batch_size=BATCH_SIZE):
    """
    Evaluate all answers in batches using parallel processing

    Args:
        df: DataFrame with questions and AI answers
        evaluator: DeepInfraEvaluator instance
        threshold: Pass threshold
        batch_size: Number of evaluations to run in parallel

    Returns:
        Updated DataFrame with evaluation results
    """
    print("\n" + "=" * 80)
    print(" AI EVALUATION STEP")
    print("=" * 80)
    print(f"Model:      {evaluator.model}")
    print(f"Threshold:  {threshold}% (pass if >= {threshold}%)")
    print(f"Batch Size: {batch_size}")
    print("=" * 80)

    # Get rows to evaluate (only successful ones)
    total_rows = len(df)
    eval_rows = df[df[COL_STATUS] == "Success"]
    total_to_eval = len(eval_rows)

    print("\n Evaluation plan:")
    print(f"  Total questions:     {total_rows}")
    print(f"  To evaluate:         {total_to_eval} (successful answers only)")
    print(f"  Skip:                {total_rows - total_to_eval} (failed/error)")

    if total_to_eval == 0:
        print("\n️  No successful answers to evaluate!")
        return df

    # Initialize evaluation columns if not exist
    if COL_AI_CHECK_SCORE not in df.columns:
        df[COL_AI_CHECK_SCORE] = 0
    if COL_AI_CHECK_PASS not in df.columns:
        df[COL_AI_CHECK_PASS] = "N/A"
    if COL_AI_EXPLANATION not in df.columns:
        df[COL_AI_EXPLANATION] = ""
    if COL_DETAILED_METRICS not in df.columns:
        df[COL_DETAILED_METRICS] = ""

    # Prepare data for evaluation
    eval_data = []
    for idx, row in eval_rows.iterrows():
        eval_data.append((idx, row[COL_QUESTION], row[COL_AI_ANSWER], threshold, evaluator))

    # Create batches
    batches = [eval_data[i : i + batch_size] for i in range(0, len(eval_data), batch_size)]
    total_batches = len(batches)

    print(f"\n Starting evaluation ({total_batches} batches)...")

    start_time = time.time()

    # Process each batch
    for batch_num, batch_data in enumerate(batches, 1):
        print(f"\n{'=' * 80}")
        print(f" BATCH {batch_num}/{total_batches} - Evaluating {len(batch_data)} answers in parallel")
        print(f"{'=' * 80}")

        batch_start = time.time()

        # Parallel evaluation using ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(batch_data)) as executor:
            # Submit all tasks
            future_to_idx = {executor.submit(evaluate_single_row, data): data[0] for data in batch_data}

            # Collect results as they complete
            for future in as_completed(future_to_idx):
                idx, completeness, relevance, clarity, overall_score, pass_flag, explanation, detailed_metrics = (
                    future.result()
                )

                # Update DataFrame
                df.at[idx, COL_AI_CHECK_SCORE] = overall_score
                df.at[idx, COL_AI_CHECK_PASS] = "Pass" if pass_flag else "Fail"
                df.at[idx, COL_AI_EXPLANATION] = explanation
                df.at[idx, COL_DETAILED_METRICS] = detailed_metrics

                # Find row number (1-based)
                row_num = df.index.get_loc(idx) + 1

                # Log result
                status_icon = "" if pass_flag else ""
                print(
                    f"  [{row_num}/{total_rows}] {status_icon} Score: {overall_score}% → {'Pass' if pass_flag else 'Fail'}"
                )
                print(f"    {detailed_metrics}")
                print(f"    {explanation[:80]}...")

        batch_duration = time.time() - batch_start
        print(f"\n Batch {batch_num} completed in {batch_duration:.2f}s")

    total_duration = time.time() - start_time

    # Summary statistics
    evaluated = df[df[COL_AI_CHECK_SCORE] > 0]
    pass_count = len(df[df[COL_AI_CHECK_PASS] == "Pass"])
    fail_count = len(df[df[COL_AI_CHECK_PASS] == "Fail"])
    avg_score = evaluated[COL_AI_CHECK_SCORE].mean() if len(evaluated) > 0 else 0

    print("\n" + "=" * 80)
    print(" EVALUATION COMPLETED!")
    print("=" * 80)
    print(f"Total Questions:     {total_rows}")
    print(f"Evaluated:           {total_to_eval}")
    print(f"Pass:                {pass_count} ({pass_count / total_to_eval * 100:.1f}%)")
    print(f"Fail:                {fail_count} ({fail_count / total_to_eval * 100:.1f}%)")
    print(f"Average Score:       {avg_score:.1f}%")
    print(f"Total Duration:      {total_duration:.2f}s ({total_duration / 60:.2f} minutes)")

    # Token usage
    stats = evaluator.get_token_stats()
    print("\n Token Usage:")
    print(f"  Prompt tokens:       {stats['prompt_tokens']:,}")
    print(f"  Completion tokens:   {stats['completion_tokens']:,}")
    print(f"  Total tokens:        {stats['total_tokens']:,}")

    print("=" * 80)

    return df


# ============================================================================
# MAIN FUNCTION
# ============================================================================
def main(input_file, threshold=DEFAULT_THRESHOLD, batch_size=BATCH_SIZE):
    """
    Main evaluation function

    Args:
        input_file: Path to benchmark results Excel file
        threshold: Pass threshold (0-100)
        batch_size: Number of parallel evaluations
    """
    print("=" * 80)
    print(" AI EVALUATOR - Benchmark Quality Assessment")
    print("=" * 80)
    print(f"Input file:  {input_file}")
    print(f"Threshold:   {threshold}%")
    print(f"Batch size:  {batch_size}")
    print("=" * 80)

    # Read Excel file
    print("\n Reading benchmark results...")
    try:
        df = pd.read_excel(input_file)
        print(f" Loaded {len(df)} questions")
    except Exception as e:
        print(f" Error reading file: {e}")
        return

    # Validate required columns
    required_cols = [COL_QUESTION, COL_AI_ANSWER, COL_STATUS]
    missing_cols = [col for col in required_cols if col not in df.columns]

    if missing_cols:
        print(f" Missing required columns: {missing_cols}")
        print(f"  Available columns: {df.columns.tolist()}")
        return

    # Initialize evaluator
    print("\n Initializing DeepInfra evaluator...")
    evaluator = DeepInfraEvaluator()
    print(f" Evaluator ready (model: {evaluator.model})")

    # Run evaluation
    df = evaluate_batch(df, evaluator, threshold=threshold, batch_size=batch_size)

    # Save results (overwrite original file)
    print("\n Saving results...")
    try:
        df.to_excel(input_file, index=False, engine="openpyxl")
        print(f" Results saved to: {input_file}")
        print(
            f"  Added columns: {COL_AI_CHECK_SCORE}, {COL_AI_CHECK_PASS}, {COL_AI_EXPLANATION}, {COL_DETAILED_METRICS}"
        )
    except Exception as e:
        print(f" Error saving file: {e}")
        return

    print("\n" + "=" * 80)
    print(" EVALUATION COMPLETE!")
    print("=" * 80)


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Evaluate benchmark results using AI")
    parser.add_argument(
        "--input",
        type=str,
        default="/Users/giangtran/Documents/Aurora Tech/R&D/Projects/AI_Q1_Q50_CLEAN_FINAL_GDU_2026_benchmark_results.xlsx",
        help="Input Excel file with benchmark results",
    )
    parser.add_argument(
        "--threshold", type=int, default=DEFAULT_THRESHOLD, help=f"Pass threshold (0-100, default: {DEFAULT_THRESHOLD})"
    )
    parser.add_argument(
        "--batch-size", type=int, default=BATCH_SIZE, help=f"Number of parallel evaluations (default: {BATCH_SIZE})"
    )

    args = parser.parse_args()

    # Validate threshold
    if args.threshold < 0 or args.threshold > 100:
        print("Error: Threshold must be between 0 and 100")
        sys.exit(1)

    # Run evaluation
    main(input_file=args.input, threshold=args.threshold, batch_size=args.batch_size)
