#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SQLite 데이터베이스 구조 및 데이터 확인
"""

import sqlite3
from pathlib import Path

db_path = Path("data/trading.db")

if not db_path.exists():
    print(f"데이터베이스를 찾을 수 없습니다: {db_path}")
    exit(1)

conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

# 테이블 목록
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()

print("="*70)
print("SQLite 데이터베이스 구조")
print("="*70)

for table in tables:
    table_name = table[0]
    print(f"\n[Table: {table_name}]")

    # 컬럼 정보
    cursor.execute(f"PRAGMA table_info({table_name});")
    columns = cursor.fetchall()

    print("  컬럼:")
    for col in columns:
        print(f"    - {col[1]} ({col[2]})")

    # 행 개수
    cursor.execute(f"SELECT COUNT(*) FROM {table_name};")
    count = cursor.fetchone()[0]
    print(f"  행 개수: {count}")

    # 샘플 데이터 (처음 3개)
    if count > 0:
        cursor.execute(f"SELECT * FROM {table_name} LIMIT 3;")
        rows = cursor.fetchall()
        print(f"  샘플 데이터:")
        for row in rows:
            print(f"    {row}")

conn.close()
