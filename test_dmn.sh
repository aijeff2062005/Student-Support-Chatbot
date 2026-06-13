#!/bin/bash

URL="http://localhost:8080/CTA_Condition_Decision"

green() { printf "\033[32m$1\033[0m\n"; }
red()   { printf "\033[31m$1\033[0m\n"; }

run_case() {
  name="$1"
  expected="$2"
  json="$3"

  echo "-------------------------------------------------"
  echo "Test: $name"
  echo "Input:"
  echo "$json"

  result=$(curl -s -X POST "$URL" \
      -H "Content-Type: application/json" \
      -d "$json")

  echo "Response: $result"
  echo "Expected rule: $expected"

  if echo "$result" | grep -q "$expected"; then
    green "PASS"
  else
    red "FAIL"
  fi
  echo
}

# -------------------- TEST CASES ---------------------

# Rule 1 — Stage 1, role null
run_case \
  "Stage 1 — Role Missing" \
  "Tôi là học sinh" \
  '{
    "Stage": 1,
    "role": null,
    "topic": null,
    "major": null,
    "request_consult": null,
    "student_profile": null
  }'

# Rule 2 — Stage 2, topic null
run_case \
  "Stage 2 — Topic Missing" \
  "Tìm hiểu học bổng" \
  '{
    "Stage": 2,
    "role": "học sinh",
    "topic": null,
    "major": null,
    "request_consult": null,
    "student_profile": null
  }'

# Rule 3 — Stage 3, major null
run_case \
  "Stage 3 — Major Missing" \
  "Giới thiệu ngành phù hợp" \
  '{
    "Stage": 3,
    "role": "học sinh",
    "topic": "học bổng",
    "major": null,
    "request_consult": null,
    "student_profile": null
  }'

# Rule 4a — Stage 4 thiếu personal info
run_case \
  "Stage 4A — Missing Personal Info" \
  "vui lòng cung cấp thông tin" \
  '{
    "Stage": 4,
    "role": "học sinh",
    "topic": "học bổng",
    "major": "CNTT",
    "request_consult": true,
    "student_profile": null
  }'

# Rule 4b — Stage 4 đủ thông tin
run_case \
  "Stage 4B — Ready to Consult" \
  "Đăng ký ngay" \
  '{
    "Stage": 4,
    "role": "học sinh",
    "topic": "học bổng",
    "major": "CNTT",
    "request_consult": true,
    "student_profile": true
  }'

# Default rule — No match
run_case \
  "Default Rule" \
  '"none"' \
  '{
    "Stage": 999,
    "role": null,
    "topic": null,
    "major": null,
    "request_consult": null,
    "student_profile": null
  }'

echo "================= DONE ================="
