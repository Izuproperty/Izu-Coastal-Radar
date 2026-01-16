#!/usr/bin/env python3
"""
Run the scraper and save all debug output to files for analysis
"""
import sys
import subprocess
from datetime import datetime

output_file = f"scraper_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

print(f"Running generate_listings.py and saving output to {output_file}")
print("This will take a moment...")

# Run the scraper and capture all output
result = subprocess.run(
    [sys.executable, "generate_listings.py"],
    capture_output=True,
    text=True
)

# Save both stdout and stderr
with open(output_file, 'w', encoding='utf-8') as f:
    f.write("=" * 80 + "\n")
    f.write("SCRAPER OUTPUT\n")
    f.write("=" * 80 + "\n\n")
    f.write("STDOUT:\n")
    f.write(result.stdout)
    f.write("\n\n" + "=" * 80 + "\n")
    f.write("STDERR:\n")
    f.write(result.stderr)
    f.write("\n\n" + "=" * 80 + "\n")
    f.write(f"Exit code: {result.returncode}\n")

print(f"\n✓ Output saved to: {output_file}")
print(f"\nTo view: cat {output_file}")
print(f"To share: You can paste the contents or git add it to the repo")

# Also print a summary
print("\n" + "=" * 80)
print("QUICK SUMMARY")
print("=" * 80)

stdout_lines = result.stdout.split('\n')
for line in stdout_lines:
    if 'Izu Taiyo' in line or 'TARGET PROPERTY' in line or 'FOUND' in line or 'Property IDs' in line:
        print(line)

sys.exit(result.returncode)
