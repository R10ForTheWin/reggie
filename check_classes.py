#!/usr/bin/env python3
"""
Quick test: log in via API and dump raw class list JSON
to see what fields iClassPro returns for full/enrolled classes.
"""

import json
import os

import requests

# Load .env
env_path = os.path.join(os.path.dirname(__file__), ".env")
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

EMAIL    = os.environ["REGGIE_EMAIL"]
PASSWORD = os.environ["REGGIE_PASSWORD"]
ORG      = "scaq"
LOC_ID   = 1

BASE = "https://app.iclasspro.com/api/jwt/v1"

session = requests.Session()
session.headers.update({
    "Content-Type": "application/json",
    "Accept":       "application/json, text/plain, */*",
    "Origin":       "https://portal.iclasspro.com",
    "Referer":      "https://portal.iclasspro.com/scaq/login",
    "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "sec-ch-ua":    '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
})

# ── 1. Login ──────────────────────────────────────────────────────────────
print("Logging in...")
resp = session.post(f"{BASE}/login", json={
    "email":    EMAIL,
    "password": PASSWORD,
    "account":  ORG,
})
print(f"  Status: {resp.status_code}")

if resp.status_code != 200:
    print("  Response:", resp.text[:500])
    raise SystemExit("Login failed")

data  = resp.json()
token = data.get("token") or data.get("access_token")
if not token:
    print("  Full response:", json.dumps(data, indent=2))
    raise SystemExit("No token in response")

print(f"  Got JWT token: {token[:40]}...")

# ── 2. Get student ID ─────────────────────────────────────────────────────
print("\nFetching students...")
resp = session.get(f"{BASE}/students", params={"token": token})
print(f"  Status: {resp.status_code}")
students = resp.json()
print("  Raw:", json.dumps(students, indent=2)[:600])

lst = students.get("data") or students.get("students") or (students if isinstance(students, list) else [])
student_id = lst[0].get("id") or lst[0].get("studentId") if lst else 3328
print(f"  Student ID: {student_id}")

# ── 3. Fetch class list ───────────────────────────────────────────────────
print("\nFetching classes...")
resp = session.get(f"{BASE}/classes", params={
    "locationId": LOC_ID,
    "limit":      50,
    "page":       1,
    "students":   student_id,
    "token":      token,
})
print(f"  Status: {resp.status_code}")

if resp.status_code != 200:
    print("  Response:", resp.text[:500])
    raise SystemExit("Classes fetch failed")

classes_raw = resp.json()

# Dump first 3 classes in full so we can see all available fields
lst = (classes_raw.get("data") or
       classes_raw.get("classes") or
       (classes_raw if isinstance(classes_raw, list) else []))

print(f"\n  Total classes returned: {len(lst)}")
print("\n── First 3 classes (full JSON) ──────────────────────────────────────")
for cls in lst[:3]:
    print(json.dumps(cls, indent=2))
    print("─" * 60)

# Show a summary of every class with key status fields
print("\n── All classes — key fields ─────────────────────────────────────────")
for cls in lst:
    name   = cls.get("className") or cls.get("name") or str(cls.get("id"))
    spots  = cls.get("openSpots")
    status = cls.get("status") or cls.get("classStatus")
    enr    = cls.get("isEnrolled") or cls.get("enrolled") or cls.get("studentEnrolled")
    full   = cls.get("isFull") or cls.get("full")
    print(f"  {name:<40} openSpots={str(spots):<5} status={str(status):<15} isEnrolled={enr}  isFull={full}")
