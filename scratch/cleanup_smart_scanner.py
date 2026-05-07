import re
import os

path = r'd:\prj\kiwoom-auto\scanner\smart_scanner.py'
with open(path, 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

# 1140번 라인(index 1139)까지는 유지
# 그 다음부터 def run_periodic_scan 전까지 삭제
# 하지만 그 사이에 리턴 값이 있어야 함.

new_lines = lines[:1140]
found_start = False
for i in range(1140, len(lines)):
    if "def run_periodic_scan" in lines[i]:
        new_lines.extend(lines[i:])
        break

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print("Cleanup completed.")
