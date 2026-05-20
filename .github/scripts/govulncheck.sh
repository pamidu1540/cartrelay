#!/usr/bin/env bash
set -eo pipefail

echo "Running govulncheck in ${WORK_DIR}..."
cd "${WORK_DIR}"

set +e
govulncheck ./... > vuln_report.txt 2>&1
exit_code=$?
set -e
cat vuln_report.txt

if [ $exit_code -eq 3 ]; then
  # Fail safe: Ensure output matches expected format before bypassing
  if ! grep -q "Fixed in:" vuln_report.txt; then
    echo "❌ Vulnerabilities found, but 'Fixed in:' string not detected. Failing to be safe."
    exit 3
  fi

  # Check if any "Fixed in:" line does NOT contain "N/A"
  if grep "Fixed in:" vuln_report.txt | grep -v "N/A" > /dev/null; then
    echo "❌ Found fixable vulnerabilities! Failing the build."
    exit 3
  else
    echo "⚠️ All found vulnerabilities are marked as 'Fixed in: N/A'. Ignoring."
    exit 0
  fi
elif [ $exit_code -ne 0 ]; then
  echo "❌ govulncheck failed with exit code $exit_code"
  exit $exit_code
fi
