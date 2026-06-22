"""Benchmark Query Agent output with batched ADK API requests."""

import argparse
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
import requests

API_BASE_URL = "http://localhost:8004"
APP_NAME = "agents"
DEFAULT_INPUT_FILE = "neo4j_stress_questions_200.csv"
DEFAULT_OUTPUT_DIR = Path("tests/benchmark_outputs")
DEFAULT_BATCH_SIZE = 1
DEFAULT_REQUEST_TIMEOUT = 120
SESSION_TIMEOUT = 10

OUTPUT_COLUMNS = [
    "question",
    "response",
]


def parse_args():
    parser = argparse.ArgumentParser(description="Benchmark Query Agent output into Excel.")
    parser.add_argument("--style", choices=["old", "new"], required=True, help="Prompt style label for output naming.")
    parser.add_argument("--input", default=DEFAULT_INPUT_FILE, help="Input CSV/XLSX file containing a Question column.")
    parser.add_argument("--output", help="Output Excel/CSV file path.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Questions to process in parallel.")
    parser.add_argument("--api-base-url", default=API_BASE_URL, help="ADK API base URL.")
    parser.add_argument("--request-timeout", type=int, default=DEFAULT_REQUEST_TIMEOUT, help="Request timeout in seconds.")
    return parser.parse_args()


def load_input_dataframe(file_path):
    """Load benchmark input from CSV or Excel."""
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path)

    raise ValueError(f"Unsupported input format: {file_path}")


def save_results_dataframe(df, file_path):
    """Save benchmark results to CSV or Excel."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        df.to_csv(path, index=False)
        return
    if suffix == ".xlsx":
        write_xlsx_dataframe(df, path)
        return
    if suffix == ".xls":
        raise ValueError("Legacy .xls output is not supported. Use .xlsx instead.")

    raise ValueError(f"Unsupported output format: {file_path}")


def excel_column_name(index):
    """Convert a 1-based column index to an Excel column label."""
    result = []
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result.append(chr(65 + remainder))
    return "".join(reversed(result))


def format_excel_number(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if pd.isna(value):
            return None
        return format(value, ".15g")
    return None


def build_sheet_row_xml(row_index, values):
    cells = []
    for column_index, value in enumerate(values, start=1):
        cell_ref = f"{excel_column_name(column_index)}{row_index}"
        numeric_value = format_excel_number(value)
        if numeric_value is not None:
            cells.append(f'<c r="{cell_ref}"><v>{numeric_value}</v></c>')
            continue

        if value is None or pd.isna(value):
            text_value = ""
        else:
            text_value = str(value)
        escaped_text = escape(text_value)
        cells.append(
            f'<c r="{cell_ref}" t="inlineStr"><is><t xml:space="preserve">{escaped_text}</t></is></c>'
        )

    return f'<row r="{row_index}">{"".join(cells)}</row>'


def build_sheet_xml(df):
    rows = [build_sheet_row_xml(1, list(df.columns))]
    for row_index, row in enumerate(df.itertuples(index=False, name=None), start=2):
        rows.append(build_sheet_row_xml(row_index, row))

    sheet_data = "".join(rows)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheetData>{sheet_data}</sheetData>"
        "</worksheet>"
    )


def write_xlsx_dataframe(df, path):
    """Write a minimal .xlsx workbook without external Excel writer dependencies."""
    sheet_xml = build_sheet_xml(df)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as workbook:
        workbook.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            "</Types>",
        )
        workbook.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
            'Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
            'Target="docProps/app.xml"/>'
            "</Relationships>",
        )
        workbook.writestr(
            "docProps/app.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            "<Application>Codex</Application>"
            "</Properties>",
        )
        workbook.writestr(
            "docProps/core.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            "<dc:creator>Codex</dc:creator>"
            "<cp:lastModifiedBy>Codex</cp:lastModifiedBy>"
            "</cp:coreProperties>",
        )
        workbook.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Benchmark" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>",
        )
        workbook.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
            "</Relationships>",
        )
        workbook.writestr(
            "xl/styles.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>",
        )
        workbook.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def resolve_question_column(df):
    for column in ("Question", "QUESTION", "question"):
        if column in df.columns:
            return column
    raise ValueError("Input file must contain a Question column.")


def build_default_output_path(style):
    return DEFAULT_OUTPUT_DIR / f"{style}_prompt_style.xlsx"


def create_session(api_base_url, user_id, session_id):
    """Create a new session via API."""
    url = f"{api_base_url}/apps/{APP_NAME}/users/{user_id}/sessions/{session_id}"

    try:
        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={
                "customer_info": {
                    "name": "Trần Gia Long",
                    "phone": "0987654321",
                    "email": "SKDragon@yopmail.com",
                    "high_school": "THPT ABC",
                    "social_links": [],
                    "location": "",
                    "education_level": "",
                },
                "qna_mode": True,
                "sale_profile": {"name": "Giang"},
                "user_state": {"role": "student", "topic": [], "major": [], "student_profile": {}},
                "extra_data": {"attachments": None},
            },
            timeout=SESSION_TIMEOUT,
        )
        if response.status_code == 200:
            return True, None
        return False, f"Session creation returned {response.status_code}"
    except Exception as exc:
        return False, str(exc)


def extract_final_response(events):
    """Extract the final response text from the last event."""
    if not isinstance(events, list) or not events:
        raise ValueError("API response is empty or not an array.")

    last_event = events[-1]
    if not isinstance(last_event, dict):
        raise ValueError("Last event is not a JSON object.")

    content = last_event.get("content")
    parts = (content or {}).get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
        raise ValueError("Last event missing content.parts[0].")

    response_text = parts[0].get("text")
    if not isinstance(response_text, str):
        raise ValueError("Last event missing content.parts[0].text.")

    return response_text


def run_agent(api_base_url, request_timeout, user_id, session_id, question):
    """Run agent with question via API and return the final response text."""
    url = f"{api_base_url}/run"
    payload = {
        "app_name": APP_NAME,
        "user_id": user_id,
        "session_id": session_id,
        "new_message": {"role": "user", "parts": [{"text": question}]},
    }

    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=request_timeout,
    )
    if response.status_code != 200:
        raise ValueError(f"API returned {response.status_code}: {response.text[:300]}")

    return extract_final_response(response.json())


def test_single_question(question_data, api_base_url, request_timeout):
    """Run one benchmark question and return the output row payload."""
    idx, question, question_num = question_data
    user_id = f"benchmark_user_{question_num}"
    session_id = f"s_{uuid.uuid4().hex[:8]}"

    row = {
        "question": question,
        "response": "",
    }

    session_created, session_error = create_session(api_base_url, user_id, session_id)
    if not session_created:
        row["response"] = f"ERROR: Failed to create session: {session_error}"
        return idx, row, "Failed"

    try:
        row["response"] = run_agent(api_base_url, request_timeout, user_id, session_id, question)
        return idx, row, "Success"
    except requests.Timeout:
        row["response"] = f"ERROR: Timeout after {request_timeout}s"
        return idx, row, "Failed"
    except Exception as exc:
        row["response"] = f"ERROR: {exc}"
        return idx, row, "Failed"


def process_batch(batch_data, batch_num, total_batches, total_questions, api_base_url, request_timeout):
    """Process one batch of questions in parallel."""
    print(f"\n{'=' * 80}")
    print(f" BATCH {batch_num}/{total_batches} - Processing {len(batch_data)} questions in parallel")
    print(f"{'=' * 80}")

    batch_start = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=len(batch_data)) as executor:
        future_to_data = {
            executor.submit(test_single_question, data, api_base_url, request_timeout): data for data in batch_data
        }

        for future in as_completed(future_to_data):
            idx, question, question_num = future_to_data[future]

            try:
                idx, row, status = future.result()
                results.append((idx, row, status))
                print(f"  [{question_num}/{total_questions}] {status}")
                if status != "Success":
                    print(f"    Error: {row['response']}")
            except Exception as exc:
                error_row = {
                    "question": question,
                    "response": f"ERROR: {exc}",
                }
                results.append((idx, error_row, "Failed"))
                print(f"  [{question_num}/{total_questions}] Failed")
                print(f"    Exception: {exc}")

    batch_duration = time.time() - batch_start
    print(f"\n Batch {batch_num} completed in {batch_duration:.2f}s")
    print(f"  Average: {batch_duration / len(batch_data):.2f}s per question")
    return results


def build_results_dataframe(questions):
    return pd.DataFrame(
        {
            "question": questions,
            "response": [""] * len(questions),
        },
        columns=OUTPUT_COLUMNS,
    )


def main():
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else build_default_output_path(args.style)

    if args.batch_size < 1:
        raise ValueError("--batch-size must be >= 1")

    print("=" * 80)
    print(" QUERY AGENT BENCHMARK - BATCHED VERSION")
    print("=" * 80)
    print(f"Prompt Style: {args.style}")
    print(f"API URL:      {args.api_base_url}")
    print(f"Batch Size:   {args.batch_size} questions in parallel")
    print(f"Timeout:      {args.request_timeout}s per question")
    print(f"Input:        {input_path}")
    print(f"Output:       {output_path}")
    print("=" * 80)

    df = load_input_dataframe(input_path)
    question_column = resolve_question_column(df)
    questions = [str(question).strip() for question in df[question_column].fillna("")]
    total_questions = len(questions)

    print(f"\n Loaded {total_questions} questions from column: {question_column}")

    results_df = build_results_dataframe(questions)
    save_results_dataframe(results_df, output_path)
    print(f" Created output file: {output_path}")

    batches = []
    for i in range(0, total_questions, args.batch_size):
        batch = []
        for j in range(i, min(i + args.batch_size, total_questions)):
            batch.append((j, questions[j], j + 1))
        batches.append(batch)

    total_batches = len(batches)
    print(f" Created {total_batches} batches")

    total_start = time.time()
    success_count = 0
    failed_count = 0

    for batch_num, batch_data in enumerate(batches, 1):
        batch_results = process_batch(
            batch_data=batch_data,
            batch_num=batch_num,
            total_batches=total_batches,
            total_questions=total_questions,
            api_base_url=args.api_base_url,
            request_timeout=args.request_timeout,
        )

        for idx, row, status in batch_results:
            for column in OUTPUT_COLUMNS:
                results_df.at[idx, column] = row[column]
            if status == "Success":
                success_count += 1
            else:
                failed_count += 1

        save_results_dataframe(results_df, output_path)
        print(f" Progress saved ({min(batch_num * args.batch_size, total_questions)}/{total_questions})")

    total_duration = time.time() - total_start

    print("\n" + "=" * 80)
    print(" BENCHMARK COMPLETED")
    print("=" * 80)
    print(f"Total Questions: {total_questions}")
    print(f"Successful:      {success_count}")
    print(f"Failed:          {failed_count}")
    print(f"Total Duration:  {total_duration:.2f}s ({total_duration / 60:.2f} minutes)")
    print(f"Results saved to: {output_path}")
    print("=" * 80)


if __name__ == "__main__":
    main()
